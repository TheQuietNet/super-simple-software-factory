"""Shared phase-timeout and process-tree-kill helpers for coding-agent
adapters (agent_pi, agent_cc, agent_grok) — see #53193.

Live incident: a builder finished its real edits, then looped
`write Report.json` / `bash rm Report.json` for 18+ minutes (~60 times)
because it misread the builder prompt's `## Report` heading as a file to
create. No phase timeout existed, so nothing stopped it until a human killed
it by hand. This module is the fix's process-control half —
`PhaseTimeout`/`AgentInterrupt` plus the cross-platform tree kill; the
detection half (a repeating tool-call signature) is `loop_guard.py`, which
reuses `AgentInterrupt` so an adapter's streaming loop can catch both the
same way.

One helper set, not three copies: every adapter builds its subprocess with
`popen_kwargs_for_tree_kill()`, calls `track_for_kill()` right after spawn
(capturing the returned token), reads its stdout through `TimeoutLineReader`,
and on either exception kills the tree with `kill_process_tree()` before
re-raising — with `release_tracking()` in a `finally` around the whole call
so the Job Object handle is freed exactly once regardless of exit path.

Round-2 reviewer finding: the original kill path looked up the tree from the
parent pid AT KILL TIME — `os.getpgid(pid)` on POSIX, `taskkill /T /PID` on
Windows. Both depend on the parent still being resolvable when the kill runs.
Fixed by anchoring at SPAWN time instead: POSIX's `start_new_session=True`
makes `pid` usable directly as a pgid forever (no `getpgid()` re-derivation);
Windows gets a Job Object.

Round-3 reviewer finding: three more gaps in that round-2 fix.
  1. Job assignment ran AFTER Popen() returned — a fast child could spawn
     (or lose) descendants in that gap, and the round-2 "parent exits
     first" test passed via the `taskkill /T` fallback regardless of
     whether the Job Object path worked at all. Fixed: every adapter now
     spawns with `CREATE_SUSPENDED` (`popen_kwargs_for_tree_kill`), so
     the child cannot execute a single instruction — including spawning
     its own children — until `track_for_kill()` has assigned it to the
     job AND resumed it (via a fresh handle to its own primary thread,
     found through a Toolhelp32 snapshot — Python's `subprocess.Popen`
     closes ITS thread handle immediately, so `CREATE_SUSPENDED` alone
     would hang the child forever without this).
  2. The job-handle registry leaked: `_jobs` was only ever cleared by
     `kill_process_tree()`, so a NORMAL exit (no kill) leaked one job
     kernel handle per turn forever, and pid reuse could silently
     overwrite an entry without closing the old handle. Fixed: keyed by a
     monotonically-increasing token, not pid (pid is only an index into
     the token, refreshed on every `track_for_kill` call); every adapter
     now calls `release_tracking()` in a `finally` around its whole run(),
     and `release_tracking()`/`_terminate_kill_job()` both pop-then-close
     the SAME token-keyed entry, so whichever runs first does the actual
     work and the other is a safe no-op.
  3. `TimeoutLineReader.close()` closed the stream from the main thread
     BEFORE joining the pump thread — measured to block 10+ seconds on
     this host when the pump was mid-`readline()` and something still
     held the pipe's write end open. Fixed: join first, with a bound, and
     never touch the stream from the main thread at all — the kill
     (always called before `close()`) is what actually unblocks the pump
     thread once every writer is truly dead; a thread still alive after
     `join_timeout` is abandoned as a daemon rather than risked into a
     hang.
"""

from __future__ import annotations

import ctypes
import itertools
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from typing import IO, Iterator, Optional


class AgentInterrupt(RuntimeError):
    """Base for anything that means 'stop the current coding-agent turn NOW'.

    A phase-timeout expiry and a detected tool-call loop both kill the same
    process tree the same way, so adapters catch this ONE type to share that
    cleanup. Callers further upstream (agents.py) still catch the concrete
    subclass to tell the two apart and react differently.
    """


class PhaseTimeout(AgentInterrupt):
    """The coding-agent process ran longer than its configured timeout_seconds.

    `str(this)` is exactly `"phase budget exceeded (Ns)"`. `Run.phase()`'s
    own exception handler turns that string into the phase's `error` column
    and a traced `error` event without agents.py needing to do anything
    extra.

    `timeout_seconds` is the phase's NOMINAL configured budget (round-3
    reviewer finding: agents.py's `send()` now threads an absolute
    `PiRequest.deadline` for enforcement, and `timeout_seconds` separately
    stays the constant original total for this message — never a shrinking
    per-turn slice, so no re-wrapping is needed anywhere to report the
    right number).
    """

    def __init__(self, timeout_seconds: int) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(f"phase budget exceeded ({timeout_seconds}s)")


class ProcessResumeFailed(RuntimeError):
    """Windows: a CREATE_SUSPENDED coding-agent process could not be resumed.

    Round-4 reviewer finding: this used to be logged and swallowed, leaving
    a permanently-suspended orphan alive until the phase timeout eventually
    noticed and killed it — potentially minutes later. `track_for_kill()`
    now kills the process and releases its tracking immediately, THEN
    raises this, so the calling adapter fails the phase loudly and
    immediately instead.
    """


_CREATE_SUSPENDED = 0x00000004   # Win32 CREATE_SUSPENDED — see popen_kwargs_for_tree_kill


def _log(message: str) -> None:
    """Not the Tracer: this module has no `run` to log through, and a
    process-tree kill must never depend on one more thing that could itself
    be hung/broken. Stderr is enough for "job assignment silently fell back
    to taskkill" to be discoverable without adding a real logging dep here.
    """
    print(f"[agent_budget] {message}", file=sys.stderr)


def popen_kwargs_for_tree_kill() -> dict:
    """Extra Popen kwargs so a later kill_process_tree() can reach every
    descendant of the spawned process — and, on Windows, so the process
    cannot run a single instruction before `track_for_kill()` has a chance
    to assign it to a Job Object (round-3 reviewer finding).

    POSIX: `start_new_session=True` makes the child's pid double as its own
    process-group id for the life of that group, so SIGKILL to that pid
    (used AS a pgid — see kill_process_tree) reaches every grandchild too.
    Windows: `CREATE_SUSPENDED` — the process exists but its primary thread
    has not run yet. `track_for_kill()` MUST be called right after Popen()
    for a Windows-spawned process, or it hangs forever (nothing else
    resumes it).
    """
    if os.name == "nt":
        # `subprocess.CREATE_SUSPENDED` is not actually exposed by this
        # stdlib (verified: absent from both `subprocess` and `_winapi` on
        # CPython 3.12/Windows, despite being documented) — the raw Win32
        # constant is stable and well-known, so it is used directly.
        return {"creationflags": _CREATE_SUSPENDED}
    return {"start_new_session": True}


# ── Windows: Job Objects + suspended-process resume (ctypes only) ──────────

if os.name == "nt":
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_void_p),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", ctypes.c_long),
            ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
        ]

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JobObjectExtendedLimitInformation = 9
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_QUERY_INFORMATION = 0x0400
    _JOB_ASSIGN_ACCESS = _PROCESS_TERMINATE | _PROCESS_SET_QUOTA | _PROCESS_QUERY_INFORMATION
    _TH32CS_SNAPTHREAD = 0x00000004
    _THREAD_SUSPEND_RESUME = 0x0002
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    # Round-4 reviewer finding #1: ctypes' default restype (when unset) is
    # `c_int` — a SIGNED 32-bit value. `ResumeThread` returns a DWORD
    # (unsigned) whose documented failure sentinel is `(DWORD)-1` ==
    # `0xFFFFFFFF`; left undeclared, ctypes hands that bit pattern back as
    # the Python int `-1`, and `-1 != 0xFFFFFFFF` is TRUE — so a genuine
    # resume failure was being read as success. The same default-`c_int`
    # gap also risks silently TRUNCATING a HANDLE return (pointer-sized,
    # 64-bit on Win64) on every other call here, even though none of those
    # happened to manifest visibly yet (handle values are small in
    # practice). Every kernel32 entry point used in this module gets an
    # explicit, correct restype/argtypes as a result — not just the one
    # the reviewer's repro caught.
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)

    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)

    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)

    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)

    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)

    _kernel32.Thread32First.restype = wintypes.BOOL
    _kernel32.Thread32First.argtypes = (wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32))

    _kernel32.Thread32Next.restype = wintypes.BOOL
    _kernel32.Thread32Next.argtypes = (wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32))

    _kernel32.OpenThread.restype = wintypes.HANDLE
    _kernel32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)

    _kernel32.ResumeThread.restype = wintypes.DWORD
    _kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)

    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)

    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    # Round-3 reviewer finding #2: keyed by a token, not pid. A pid is only
    # ever used to LOOK UP the current token for it (`_token_by_pid`); the
    # authoritative store (`_jobs`) is token -> handle, so pid reuse can
    # never cause one entry to silently clobber another's still-open
    # handle. Both `release_tracking()` (normal exit) and
    # `_terminate_kill_job()` (kill path) pop-then-close through the same
    # token, so whichever runs first does the real work and the other is a
    # safe, idempotent no-op — a turn is released exactly once either way.
    _jobs_lock = threading.Lock()
    _next_token = itertools.count(1)
    _jobs: dict[int, int] = {}          # token -> job HANDLE
    _token_by_pid: dict[int, int] = {}  # pid -> most recent token for it

    def _create_kill_job() -> int:
        """A fresh Job Object with KILL_ON_JOB_CLOSE, or 0 on failure."""
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return 0
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
                job, _JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info)):
            _kernel32.CloseHandle(job)
            return 0
        return job

    def _assign_to_job(job: int, pid: int) -> bool:
        proc_handle = _kernel32.OpenProcess(_JOB_ASSIGN_ACCESS, False, pid)
        if not proc_handle:
            return False
        try:
            return bool(_kernel32.AssignProcessToJobObject(job, proc_handle))
        finally:
            _kernel32.CloseHandle(proc_handle)

    def _primary_thread_id(pid: int) -> Optional[int]:
        """The (single) thread id of a freshly `CREATE_SUSPENDED` process —
        nothing has run yet, so it cannot have spawned another thread. Found
        via a Toolhelp32 snapshot rather than any handle `subprocess.Popen`
        hands back: CPython's Windows `Popen._execute_child` closes its OWN
        thread handle unconditionally right after CreateProcess, regardless
        of `creationflags`, so there is nothing to resume from there."""
        snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        # With `restype = wintypes.HANDLE` declared, a NULL return comes
        # back as Python `None` (ctypes' convention for a null c_void_p-
        # family restype), not `0` — `snap in (0, _INVALID_HANDLE_VALUE)`
        # would silently miss that case.
        if snap is None or snap == _INVALID_HANDLE_VALUE:
            return None
        try:
            entry = _THREADENTRY32()
            entry.dwSize = ctypes.sizeof(_THREADENTRY32)
            found: list[int] = []
            ok = _kernel32.Thread32First(snap, ctypes.byref(entry))
            while ok:
                if entry.th32OwnerProcessID == pid:
                    found.append(entry.th32ThreadID)
                ok = _kernel32.Thread32Next(snap, ctypes.byref(entry))
            return min(found) if found else None   # created first == primary
        finally:
            _kernel32.CloseHandle(snap)

    def _resume_suspended_process(pid: int) -> bool:
        tid = _primary_thread_id(pid)
        if tid is None:
            return False
        thread_handle = _kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, tid)
        if not thread_handle:
            return False
        try:
            return _kernel32.ResumeThread(thread_handle) != 0xFFFFFFFF
        finally:
            _kernel32.CloseHandle(thread_handle)

    def _terminate_kill_job(pid: int) -> bool:
        """True if a job for `pid` existed and TerminateJobObject was called
        on it (killing every current member, including a grandchild whose
        immediate parent already exited — the whole point)."""
        with _jobs_lock:
            token = _token_by_pid.pop(pid, None)
            job = _jobs.pop(token, None) if token is not None else None
        if job is None:
            return False
        ok = bool(_kernel32.TerminateJobObject(job, 1))
        _kernel32.CloseHandle(job)   # KILL_ON_JOB_CLOSE fires here too, redundantly
        return ok
else:
    def _terminate_kill_job(pid: int) -> bool:   # pragma: no cover - posix
        return False


def track_for_kill(pid: int) -> Optional[int]:
    """Call once, right after Popen() returns, from every adapter. Returns
    an opaque token the caller MUST pass to `release_tracking()` exactly
    once (in a `finally`) — `None` on POSIX, or on total failure.

    POSIX: a no-op returning None — `popen_kwargs_for_tree_kill()`'s
    `start_new_session` already made `pid` usable directly as a pgid at
    kill time, no separate registration or resume needed (the process was
    never suspended there in the first place).

    Windows: the process was created with CREATE_SUSPENDED (round-3
    reviewer finding #1) specifically so NOTHING can run in it — including
    spawning its own children — before this function has assigned it to a
    Job Object. Resume happens LAST, after assignment, so the atomicity
    guarantee actually holds. If job assignment fails, the process is still
    resumed regardless (never leave it permanently suspended — that's a
    worse failure than falling back to `taskkill /T` at kill time).

    Raises `ProcessResumeFailed` if the resume itself fails (round-4
    reviewer finding #1): the process is killed and its tracking released
    FIRST, so a resume failure never leaves a permanently-suspended orphan
    alive until the phase timeout eventually notices — it fails immediately
    and loudly instead.
    """
    if os.name != "nt":
        return None
    job = _create_kill_job()
    token: Optional[int] = None
    if job and _assign_to_job(job, pid):
        token = next(_next_token)
        with _jobs_lock:
            _jobs[token] = job
            _token_by_pid[pid] = token
    else:
        if job:
            _kernel32.CloseHandle(job)
        _log(f"pid {pid}: Job Object assignment failed at spawn time — "
             "kill_process_tree() will fall back to taskkill /T")
    if not _resume_suspended_process(pid):
        kill_process_tree(pid)
        release_tracking(pid, token)
        raise ProcessResumeFailed(
            f"pid {pid}: failed to resume a CREATE_SUSPENDED process — "
            "killed it immediately rather than leave a permanently-"
            "suspended orphan alive until the phase timeout")
    return token


def release_tracking(pid: int, token: Optional[int]) -> None:
    """Call in a `finally` around the WHOLE adapter run() (round-3 reviewer
    finding #2) — every exit path, not just the kill path — so a Job Object
    handle from a normal (un-killed) turn is not leaked forever. Idempotent
    with `kill_process_tree()`: whichever of the two runs first actually
    closes the handle; the other finds nothing left to do.
    """
    # Both checks are belt-and-braces: `track_for_kill()` only ever returns
    # a non-None token on Windows, so `token is None` alone already makes
    # this a no-op on POSIX in practice — the explicit `os.name` check
    # means that stays true by construction, not by every caller's
    # discipline, and this function never references a Windows-only global
    # (_jobs_lock etc., defined only inside agent_budget's own `if
    # os.name == "nt":` block) on a platform where it does not exist.
    if os.name != "nt" or token is None:
        return
    with _jobs_lock:
        job = _jobs.pop(token, None)
        if _token_by_pid.get(pid) == token:
            _token_by_pid.pop(pid, None)
    if job is not None:
        _kernel32.CloseHandle(job)


def kill_process_tree(pid: int, *, allow_taskkill_fallback: bool = True) -> None:
    """Kill `pid` and every descendant it spawned. Best-effort: a process
    that has already exited is not treated as an error.

    `allow_taskkill_fallback=False` is TEST-ONLY (round-3 reviewer finding
    #1's revert-check): it isolates the Job Object path by refusing the
    `taskkill /T` fallback, so a test can prove the job alone is sufficient
    to kill an orphaned grandchild — `taskkill /T` reaching the same result
    via its own PPID-based tree walk would otherwise mask a broken Job
    Object path, the way it did in round 2.
    """
    if os.name == "nt":
        if _terminate_kill_job(pid):
            return
        if not allow_taskkill_fallback:
            _log(f"pid {pid}: no tracked Job Object and the taskkill "
                 "fallback is disabled (test-only) — nothing further attempted")
            return
        _log(f"pid {pid}: no tracked Job Object (assignment never succeeded) — "
             "falling back to taskkill /T, which cannot see a descendant "
             "whose immediate parent already exited")
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False,
        )
        return
    try:
        # `pid` IS the process group id here, by construction: every adapter
        # spawns with `start_new_session=True` (popen_kwargs_for_tree_kill),
        # which makes the new process both its own session leader AND its
        # own process group leader — pgid == pid, permanently, for as long
        # as any member of that group survives. Deriving the pgid via
        # `os.getpgid(pid)` instead would fail with ESRCH the moment the
        # ORIGINAL process (pid) has itself already exited, even while a
        # grandchild in the same group is still very much alive.
        os.killpg(pid, signal.SIGKILL)
        return
    except ProcessLookupError:
        return
    except OSError:
        pass
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def process_alive(pid: int) -> bool:
    """Best-effort liveness check — used to verify a kill actually took
    (tests) and available to callers that want the same check in production.
    """
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, just not ours to signal
    return True


class TimeoutLineReader:
    """Reads a subprocess's text-mode stdout line-by-line off a background
    thread, so the caller can enforce a wall-clock budget that also catches a
    process that writes NOTHING at all (the exact shape of the fake-hang
    test: a process that only `sleep`s).

    A plain `for line in stdout:` blocks inside the underlying read() with no
    way to time out that read, and on Windows a pipe handle is not
    select()-able — so the only portable fix is to do the blocking read on a
    separate daemon thread and let the caller wait on a queue with a timeout
    instead.
    """

    _EOF = object()

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream
        self._queue: "queue.Queue[object]" = queue.Queue()
        self._thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self._thread.start()

    def _pump(self, stream: IO[str]) -> None:
        try:
            for line in stream:
                self._queue.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._queue.put(self._EOF)

    def lines(self, deadline: float, nominal_seconds: int) -> Iterator[str]:
        """Yield lines as they arrive; raises PhaseTimeout the instant the
        ABSOLUTE `deadline` (a `time.monotonic()` value) passes with no EOF
        seen yet. `nominal_seconds` is carried only for the raised
        exception's message (round-3 reviewer finding #5: the deadline
        itself is threaded through with no flooring anywhere — a plain
        `float` comparison throughout, never converted to/from an int).
        """
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PhaseTimeout(nominal_seconds)
            try:
                item: Optional[object] = self._queue.get(timeout=remaining)
            except queue.Empty:
                raise PhaseTimeout(nominal_seconds)
            if item is self._EOF:
                return
            yield item  # type: ignore[misc]

    def close(self, join_timeout: float = 5.0) -> None:
        """Round-3 reviewer finding #3: bounded and deterministic. The OLD
        order — `stream.close()` from the main thread FIRST, then join —
        could itself block for 10+ seconds when the pump thread was mid-
        `readline()` and something still held the pipe's write end open;
        closing a stream out from under a thread actively reading it is not
        a safe, bounded operation on this platform. The kill
        (`kill_process_tree()`, always called by the adapter BEFORE this) is
        what actually unblocks the pump thread — once every process holding
        the write end is truly dead, the pipe reaches EOF (or the read
        errors, caught in `_pump`) on its own. This method's only job is to
        wait a BOUNDED amount of time for that; it never touches the stream
        from this thread. A pump thread still alive after `join_timeout` is
        abandoned as a daemon — it was never blocking process exit anyway.
        """
        self._thread.join(timeout=join_timeout)
