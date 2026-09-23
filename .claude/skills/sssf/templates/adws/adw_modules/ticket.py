"""adw_ticket: board -> factory entry point.

Two operations, both driven off one board ticket (``agentctl --json show
--id N`` — or your own ticket-tracker CLI, see AGENTCTL below):

``draft``  Turns the ticket into the four-line ask the cookbook
           (``how_to_prompt_for_the_eng.md``) describes — ``Where:`` built
           from the ticket's own ``footprint``, mapped repo-relative to
           THIS repo, never hand-transcribed. Refuses when nothing in the
           footprint maps here, rather than guessing a scope.

``run``    Requires that draft to already exist and be reviewed
           (``--from-draft``) — without it, drafts and stops. Once
           confirmed: creates a fresh worktree on ``claude/<N>-sssf``,
           claims the ticket, runs the chosen ADW chain there, and on
           success pushes + opens a PR citing ``task #N`` (reusing
           ``ship.py``'s PR-body/title/create_pr/route_reviews pieces —
           NEVER ``ship.merge_auto``: a PR this tool opens still needs
           review). On failure it releases the claim and posts the
           adw_id/failing phase/violations back to the ticket.

Every ``agentctl``/``git``/``gh`` call and the chain subprocess itself go
through injectable callables (`RunDeps`), so tests never shell out for real.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import agents, gates, ship
from .utils import ensure_dir, new_id

_IS_WINDOWS = os.name == "nt"
# `subprocess.CREATE_NEW_PROCESS_GROUP` does not exist as a module attribute
# AT ALL on non-Windows Python builds (GitHub CI's ubuntu runner: an
# AttributeError, not merely a no-op flag). `getattr` with the real Windows
# value as a fallback means this module can be IMPORTED and its Windows
# branch logic exercised (e.g. under a forced `_IS_WINDOWS = True` in a
# test) on any platform without ever raising just from referencing the name.
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

# The board-CLI invocation. Configurable via SSSF_AGENTCTL_CMD (a
# shell-quoted command string), so the harness never hardcodes one
# operator's path — a stamped repo has no way to know where a given
# deployment's agentctl (or equivalent ticket-tracker CLI) actually lives.
# Falls back to a bare `agentctl` resolved off PATH. Example override
# matching a Windows-hosted install:
#   SSSF_AGENTCTL_CMD=py -3 C:\Users\you\projects\agent-orchestrator\agentctl.py
AGENTCTL = shlex.split(os.environ.get("SSSF_AGENTCTL_CMD", "agentctl"))
# The agent identity this tool authenticates its agentctl calls as.
# Repo-specific — set SSSF_TICKET_AGENT to your own board's agent name.
DEFAULT_AGENT = os.environ.get("SSSF_TICKET_AGENT", "sssf-ticket-agent")
# Where `run`'s fresh worktrees are created. Configurable via
# SSSF_WORKTREE_ROOT; defaults to a `_wt` directory next to the user's home,
# never a hardcoded machine-specific path.
WORKTREE_ROOT = Path(os.environ.get("SSSF_WORKTREE_ROOT", str(Path.home() / "_wt")))

CHAIN_SCRIPTS = {
    "simple_sdlc": "adw_simple_sdlc.py",
    "plan_build_test": "adw_plan_build_test.py",
}
DEFAULT_CHAIN = "simple_sdlc"

POLL_SECONDS = 15.0
HEARTBEAT_SECONDS = 300.0


class TicketFetchError(RuntimeError):
    pass


class DraftRefused(RuntimeError):
    """No footprint entry mapped into this repo as a path/glob-shaped token,
    or a mapped token falls under the roster's protected_files."""


class RunRefused(RuntimeError):
    """The run subcommand cannot proceed (missing draft, dirty/unregistered
    worktree, wrong branch, ...)."""


class HeartbeatRejected(RunRefused):
    """A heartbeat sent WHILE the chain was running came back rejected — the
    lease is gone. The chain is killed rather than left running against a
    claim this process no longer holds."""


class ClaimReleaseFailed(RunRefused):
    """The run failed/aborted AND the claim release itself failed — the
    ticket is still shown claimed by this session. Always surfaced loudly
    (never swallowed into a quiet failure report) since a stuck claim blocks
    every other agent from picking the ticket back up."""


# ── agentctl CLI wrappers ─────────────────────────────────────────────────────
#
# One thin wrapper per verb this module needs, each taking the generic
# ``run`` callable (argv -> CompletedProcess) so every call site is a fake in
# tests. No verb here mutates a REAL ticket unless a caller supplies a real
# `run` — the tests in this repo never do.

def default_run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def default_git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo_dir), *args],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def _agentctl_argv(agentctl: list[str], agent: str, *rest: str) -> list[str]:
    return [*agentctl, "--agent", agent, *rest]


def show_ticket(task_id: int, *, agent: str = DEFAULT_AGENT,
                agentctl: list[str] | None = None,
                run: Callable[[list[str]], subprocess.CompletedProcess] = default_run) -> dict:
    """The ticket's ``task`` object, via ``agentctl --json show --id N``. Read-only."""
    argv = [*(agentctl or AGENTCTL), "--json", "--agent", agent, "show", "--id", str(task_id)]
    result = run(argv)
    if result.returncode != 0:
        raise TicketFetchError(
            f"agentctl show --id {task_id} failed: {(result.stderr or result.stdout).strip()}")
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise TicketFetchError(
            f"agentctl show --id {task_id} did not return JSON: {exc}") from exc
    task = payload.get("task") if isinstance(payload, dict) else None
    if not isinstance(task, dict):
        raise TicketFetchError(f"agentctl show --id {task_id} JSON had no 'task' object")
    return task


def heartbeat(session: str, *, agent: str = DEFAULT_AGENT, agentctl: list[str] | None = None,
             run: Callable[[list[str]], subprocess.CompletedProcess] = default_run
             ) -> subprocess.CompletedProcess:
    return run(_agentctl_argv(agentctl or AGENTCTL, agent, "heartbeat", "--session", session))


def claim(task_id: int, session: str, *, agent: str = DEFAULT_AGENT,
         agentctl: list[str] | None = None,
         run: Callable[[list[str]], subprocess.CompletedProcess] = default_run
         ) -> subprocess.CompletedProcess:
    return run(_agentctl_argv(agentctl or AGENTCTL, agent, "claim", "--id", str(task_id),
                              "--session", session))


def release(task_id: int, reason: str, *, agent: str = DEFAULT_AGENT,
           agentctl: list[str] | None = None,
           run: Callable[[list[str]], subprocess.CompletedProcess] = default_run
           ) -> subprocess.CompletedProcess:
    """Hand the claim back by setting the task's state to ``open``.

    ``agentctl --help`` / ``claim --help`` may have no dedicated
    release/unclaim verb — check your board CLI's own docs. ``state --id N
    --state open --reason ...`` is agentctl's documented general-purpose
    state setter and is what puts the task back in the claimable pool.
    """
    return run(_agentctl_argv(agentctl or AGENTCTL, agent, "state", "--id", str(task_id),
                              "--state", "open", "--reason", reason))


def post_msg(task_id: int, body: str, *, to: str, agent: str = DEFAULT_AGENT,
            kind: str = "status", agentctl: list[str] | None = None,
            run: Callable[[list[str]], subprocess.CompletedProcess] = default_run
            ) -> subprocess.CompletedProcess:
    return run(_agentctl_argv(agentctl or AGENTCTL, agent, "msg", "--to", to,
                              "--task", str(task_id), "--kind", kind, "--body", body))


# ── footprint -> Where: mapping ────────────────────────────────────────────────

_VAULT_PREFIX = "vault:"


@dataclass
class FootprintMapping:
    kept: list[str] = field(default_factory=list)     # repo-relative Where: tokens
    dropped: list[str] = field(default_factory=list)  # "<entry>: <reason>" notes


def _repo_relative_footprint_token(entry: str, repo_root: Path) -> Optional[str]:
    """A footprint entry mapped repo-relative to `repo_root`, or None when it
    does not map here (a `vault:` pointer, another repo, escapes the repo
    root via `..`, empty).

    A RELATIVE entry used to be taken at face value —
    `text.replace("\\\\", "/").lstrip("./")` — with no containment check at
    all, so `"../outside.js"` (or `"..\\outside.js"`) mapped straight to the
    in-repo-looking token `"outside.js"`, silently REWRITING a path that
    actually names a file outside the repo into one that looks like it's
    inside. Both absolute and relative entries now go through the SAME
    resolve-then-check-containment path: a relative entry is resolved
    against `repo_root` first (`Path.resolve()` normalizes `..` without
    requiring the target to exist), then anything that resolves outside
    `repo_root` is DROPPED — never rewritten into a fake in-repo token —
    exactly like an absolute path naming another repo.

    Comparison is done as normalized, lower-cased strings rather than
    `Path.relative_to` — Windows paths are case-insensitive at the
    filesystem level, and a ticket's footprint entry may not match
    `git rev-parse --show-toplevel`'s drive-letter casing exactly. The
    returned token keeps the RESOLVED path's suffix (post-`..`-normalization),
    not the entry's raw text, since a relative entry's own casing may not
    reflect where it actually points once `..` is resolved.

    A footprint entry may also arrive backslash-separated on a Windows-
    authored ticket. Backslash is an ordinary filename character on POSIX,
    not a separator — a raw `Path` built from that string there is ONE
    relative path component, not a `..` segment followed by a filename, so
    `(repo_root / text).resolve()` never actually walks up a directory and
    the containment check above saw it as staying inside `repo_root`,
    keeping a forward-slash-rewritten "../outside.js" as a scope token that
    names a real escape once anything else resolves it as a path.
    Normalizing backslash to forward slash in the entry TEXT itself, before
    any `Path`/`resolve()` call, makes this platform-independent: the
    traversal is caught by `Path.resolve()` (which DOES split on `/`
    everywhere) regardless of which OS this code happens to run under.
    """
    text = (entry or "").strip().replace("\\", "/")
    if not text or text.lower().startswith(_VAULT_PREFIX):
        return None
    candidate = Path(text)
    base = candidate if candidate.is_absolute() else (repo_root / text)
    try:
        cand_str = str(base.resolve()).replace("\\", "/")
    except OSError:
        return None
    root_str = str(repo_root.resolve()).replace("\\", "/").rstrip("/")
    if cand_str.lower() == root_str.lower():
        return None                     # the repo root itself names no file
    prefix = root_str.lower() + "/"
    if not cand_str.lower().startswith(prefix):
        return None                     # outside this repo — another repo, or `..` escape
    rel_str = cand_str[len(prefix):]
    if not rel_str:
        return None
    if (repo_root / rel_str).is_dir():
        rel_str = rel_str.rstrip("/") + "/"
    return rel_str


def map_footprint(footprint: list[str], repo_root: Path) -> FootprintMapping:
    mapping = FootprintMapping()
    for entry in footprint or []:
        text = (entry or "").strip()
        if not text:
            continue
        token = _repo_relative_footprint_token(text, repo_root)
        if token is None:
            mapping.dropped.append(
                f"{text}: does not map into this repo ({repo_root}) — "
                f"another repo, a vault: pointer, or unresolvable")
            continue
        mapping.kept.append(token)
    return mapping


def _validate_where_tokens(tokens: list[str], repo_root: Path) -> tuple[list[str], list[str]]:
    """Mapped tokens, split into (path/glob-shaped, warnings) — via gates'
    own Where:-token validator, so the shape rule lives in exactly one place.
    `repo_root` is passed through so an extensionless token that actually
    exists there (`justfile`, `Makefile`) parses clean too."""
    if not tokens:
        return [], []
    joined = ", ".join(tokens)
    warnings = gates.where_warnings(joined, repo_root)
    parsed = gates._paths_from_where(f"Where: {joined}", repo_root)
    shaped = [t for t in tokens if t.replace("\\", "/").lstrip("./") in parsed]
    return shaped, warnings


def _protected_footprint_hits(tokens: list[str], repo_root: Path, config: str) -> list[str]:
    """Mapped Where: tokens that fall under the roster's own
    `protected_files` — a run built against them is doomed before it starts
    (the builder agent it hands the work to is walled off from writing
    there by `permissions.py`). Non-blocking best-effort per the roster
    config: an unreadable/missing/malformed config means this check is
    skipped, never that drafting is blocked on something unrelated to the
    ticket itself (the same fail-open-on-transport shape as
    `gates.ask_matches_diff`'s Jev skip).

    `config` is resolved against `repo_root`, not the process cwd — this
    runs from `draft()`, which takes an arbitrary `repo_root` regardless of
    where the process happens to be launched from.
    """
    config_path = Path(config)
    if not config_path.is_absolute():
        config_path = repo_root / config_path
    try:
        cfg = agents.load_config(str(config_path))
    except Exception:  # noqa: BLE001 — best-effort; never block drafting on this
        return []
    protected = cfg.defaults.protected_files or []
    hits: list[str] = []
    for token in tokens:
        for guard in protected:
            if gates._scope_matches(token, guard):
                hits.append(f"{token}: protected by roster config ({guard})")
                break
    return hits


# ── draft ────────────────────────────────────────────────────────────────────

_TRIVIAL_BODY_RE = re.compile(r"^promoted from finding #\d+\.?$", re.IGNORECASE)
_OUT_OF_SCOPE_RE = re.compile(r"(?im)^\s*(?:\d+[)\.]?\s*)?out of scope:?\s*(.+)$")


def build_ask(title: str, body: str) -> str:
    """title + the actionable part of body — the ticket's own words, not a
    rewrite. A body that is only the board's auto-generated 'Promoted from
    finding #N.' stub carries no actionable content of its own."""
    title = " ".join((title or "").split())
    body = " ".join((body or "").split())
    if not body or _TRIVIAL_BODY_RE.match(body):
        return title
    if body in title:
        return title
    if title in body:
        return body
    return f"{title} — {body}"


def build_out_of_scope(body: str, acceptance: str) -> str:
    """An explicit 'Out of scope' the ticket already names, else a
    reviewer-fills placeholder — never invented scope."""
    for text in (acceptance or "", body or ""):
        m = _OUT_OF_SCOPE_RE.search(text)
        if m:
            return m.group(1).strip()
    return "(reviewer: confirm nothing beyond Done means was implied)"


def draft_path(repo_root: Path, task_id: int) -> Path:
    return Path(repo_root) / "requests" / f"{task_id}.md"


@dataclass
class DraftResult:
    path: Path
    text: str
    dropped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def draft(task_id: int, *, repo_root: Path, agent: str = DEFAULT_AGENT,
          agentctl: list[str] | None = None,
          run: Callable[[list[str]], subprocess.CompletedProcess] = default_run,
          config: str = "adws/adw_sssf_config/sssf.config.yaml",
          ) -> DraftResult:
    repo_root = Path(repo_root)
    agentctl = list(agentctl or AGENTCTL)
    task = show_ticket(task_id, agent=agent, agentctl=agentctl, run=run)

    mapping = map_footprint(task.get("footprint") or [], repo_root)
    shaped, warnings = _validate_where_tokens(mapping.kept, repo_root)
    unshaped_notes = [f"{t}: not path/glob-shaped after mapping"
                      for t in mapping.kept if t not in shaped]
    dropped = [*mapping.dropped, *unshaped_notes]

    if not shaped:
        raise DraftRefused(
            f"task #{task_id}: no footprint entry maps into this repo "
            f"({repo_root}) as a path/glob-shaped Where: token. "
            f"footprint={task.get('footprint') or []!r}"
            + (f"; dropped: {'; '.join(dropped)}" if dropped else "; footprint was empty"))

    protected_hits = _protected_footprint_hits(shaped, repo_root, config)
    if protected_hits:
        raise DraftRefused(
            f"task #{task_id}: mapped Where: token(s) fall under this "
            f"roster's protected_files ({config}) — a build against them is "
            f"doomed before it starts, the builder agent cannot write there: "
            f"{'; '.join(protected_hits)}")

    where_line = ", ".join(sorted(shaped))
    ask = build_ask(task.get("title", ""), task.get("body", ""))
    done_means = (task.get("acceptance") or "").strip()
    out_of_scope = build_out_of_scope(task.get("body", ""), task.get("acceptance", ""))

    text = (f"{ask}\n"
           f"Where: {where_line}\n"
           f"Done means: {done_means}\n"
           f"Out of scope: {out_of_scope}\n")

    path = draft_path(repo_root, task_id)
    ensure_dir(path.parent)
    path.write_text(text, encoding="utf-8")
    return DraftResult(path=path, text=text, dropped=dropped, warnings=warnings)


# ── run ──────────────────────────────────────────────────────────────────────

def worktree_path(repo_root: Path, task_id: int,
                  worktree_root: Path = WORKTREE_ROOT) -> Path:
    return Path(worktree_root) / f"{Path(repo_root).name}-{task_id}-sssf"


def branch_name(task_id: int) -> str:
    return f"claude/{task_id}-sssf"


def session_label(agent: str, task_id: int) -> str:
    return f"{agent}#sssf-{task_id}"


@dataclass
class ChainConfig:
    """Everything `run_ticket` needs about the ask. One object, per the
    four-param rule."""

    task_id: int
    repo_root: Path
    config: str = "adws/adw_sssf_config/sssf.config.yaml"
    chain: str = DEFAULT_CHAIN
    from_draft: bool = False
    agent: str = DEFAULT_AGENT
    msg_to: str = ""             # who a failure report is addressed to; "" -> self (`agent`)
    review_repo: str = ship.DEFAULT_REPO
    agentctl: list[str] = field(default_factory=lambda: list(AGENTCTL))
    # Injectable so tests never create/inspect anything under a real
    # worktree root — production callers leave this at the module default
    # (WORKTREE_ROOT, itself configurable via SSSF_WORKTREE_ROOT).
    worktree_root: Path = WORKTREE_ROOT

    def __post_init__(self):
        self.repo_root = Path(self.repo_root)
        self.worktree_root = Path(self.worktree_root)
        if not self.msg_to:
            self.msg_to = self.agent
        if self.chain not in CHAIN_SCRIPTS:
            raise ValueError(
                f"unknown --chain {self.chain!r}; choose one of {sorted(CHAIN_SCRIPTS)}")


def _popen_kwargs_for_tree_kill() -> dict:
    """Extra `Popen` kwargs so the whole process TREE can be reached by
    `_default_kill_tree` later, not just the direct child.

    `_default_kill_tree` used to be Windows-`taskkill`-only, with a
    `proc.kill()` fallback that reaches only the immediate child (`uv`) —
    on POSIX, every descendant `uv` spawned (the coding-agent process, and
    whatever IT spawns) would be left running, still burning tokens against
    a claim this process no longer holds. Windows: `CREATE_NEW_PROCESS_GROUP`,
    reached later via `taskkill /T`. POSIX: `start_new_session=True` makes
    the child its own process GROUP leader, reached later via `os.killpg` —
    everything the child spawned inherits that same pgid unless it
    explicitly detaches.
    """
    if _IS_WINDOWS:
        return {"creationflags": _CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _default_kill_tree(proc: "subprocess.Popen") -> None:
    """Best-effort: kill the WHOLE subprocess tree, not just the direct
    child. `uv run adws/adw_*.py` spawns coding-agent children of its own,
    and `Popen.kill()` alone only ever reaches the immediate `uv` process —
    everything under it would be left running, still burning tokens against
    a claim this process no longer holds.

    Windows: `taskkill /T` walks the tree rooted at `proc.pid` — works
    because `default_chain_runner` spawned it with `CREATE_NEW_PROCESS_GROUP`
    (`_popen_kwargs_for_tree_kill`). POSIX: `os.killpg` signals the whole
    process GROUP `proc.pid` leads (same precondition: `start_new_session`
    at spawn time). Either branch failing is swallowed (best-effort) since
    a plain `proc.kill()` on the direct child still follows as a fallback.
    """
    if _IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        except OSError:
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def default_chain_runner(argv: list[str], cwd: Path, heartbeat_fn: Callable[[], None], *,
                         poll_seconds: float = POLL_SECONDS,
                         heartbeat_seconds: float = HEARTBEAT_SECONDS,
                         popen: Callable[..., "subprocess.Popen"] = subprocess.Popen,
                         sleep: Callable[[float], None] = time.sleep,
                         clock: Callable[[], float] = time.monotonic,
                         kill_tree: Callable[["subprocess.Popen"], None] = _default_kill_tree,
                         ) -> subprocess.CompletedProcess:
    """Spawn the chain script, heartbeating the claim every `heartbeat_seconds`
    while it runs.

    stdout used to be `subprocess.PIPE`, read only once at the very end
    (`proc.stdout.read()`). Nothing drained that pipe WHILE the child ran,
    so a chatty ADW (the trace visualizer, a verbose agent turn) fills the
    OS pipe buffer, the child blocks on its next `write()`, and this loop
    keeps heartbeating a run that can no longer make any progress —
    forever. Redirected straight to a log FILE instead: the OS writes it
    directly, no pipe, no buffer to fill, no thread needed to drain one.
    The file is read back once at the end for the tail used in reports.

    A rejected heartbeat (`HeartbeatRejected`, raised by `heartbeat_fn`)
    kills the whole process tree (`kill_tree`) and re-raises — continuing to
    run a chain against a lease this process no longer holds would let an
    agent keep writing after `run_ticket` has already told the ticket the
    claim is gone.

    `popen`/`sleep`/`clock`/`kill_tree` are injectable so a test can drive
    the loop without a real subprocess, a real wait, or a real `taskkill`.
    """
    log_path = Path(cwd) / "adw_ticket_run.log"
    with open(log_path, "w", encoding="utf-8") as log_file:
        proc = popen(argv, cwd=str(cwd), stdout=log_file, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    **_popen_kwargs_for_tree_kill())
        last_hb = clock()
        try:
            while proc.poll() is None:
                sleep(poll_seconds)
                now = clock()
                if now - last_hb >= heartbeat_seconds:
                    heartbeat_fn()
                    last_hb = now
        except HeartbeatRejected:
            kill_tree(proc)
            raise
    tail = ""
    try:
        tail = log_path.read_text(encoding="utf-8")[-4000:]
    except OSError:
        pass
    return subprocess.CompletedProcess(argv, proc.returncode, tail, "")


@dataclass
class RunDeps:
    """Injectable seams for `run_ticket`. Real ones shell out; tests fake
    them — nothing here ever touches a real ticket, repo, or GitHub PR
    unless a caller wires the real defaults in explicitly."""

    run: Callable[[list[str]], subprocess.CompletedProcess] = None
    git: Callable[..., subprocess.CompletedProcess] = None
    chain_runner: Callable[[list[str], Path, Callable[[], None]],
                           subprocess.CompletedProcess] = None
    pr_creator: Callable[..., str] = None
    reviewer: Callable[[str], str] = None
    # The adw_id pinned into the chain via --adw-id (never guess "whichever
    # session is latest" from a reused worktree's db). Injectable so a test
    # can assert against a KNOWN id instead of a fresh random one.
    new_adw_id: Callable[[], str] = None

    def __post_init__(self):
        if self.run is None:
            self.run = default_run
        if self.git is None:
            self.git = default_git
        if self.chain_runner is None:
            self.chain_runner = default_chain_runner
        if self.pr_creator is None:
            self.pr_creator = lambda **kw: ship.create_pr(**kw)
        if self.reviewer is None:
            self.reviewer = lambda repo: ship.route_reviews(repo)
        if self.new_adw_id is None:
            self.new_adw_id = lambda: new_id(8)


@dataclass
class RunOutcome:
    ok: bool
    stopped_for_review: bool = False
    message: str = ""
    worktree: Optional[Path] = None
    adw_id: str = ""
    pr_url: str = ""
    failing_phase: str = ""
    violations: list[str] = field(default_factory=list)


def _worktree_is_registered(cfg: ChainConfig, deps: RunDeps, wt: Path) -> bool:
    """True when `wt` is a real, registered worktree of THIS repo, per
    `git worktree list --porcelain` run from `repo_root` — a directory that
    merely happens to sit at the expected path (stale, hand-created, left
    over from a wholly different repo) is not the same thing."""
    listing = deps.git(cfg.repo_root, "worktree", "list", "--porcelain")
    if listing.returncode != 0:
        return False
    wt_str = str(wt.resolve()).replace("\\", "/").lower()
    for line in listing.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip().replace("\\", "/").lower()
            if path == wt_str:
                return True
    return False


def ensure_worktree(cfg: ChainConfig, deps: RunDeps) -> tuple[Path, str]:
    """`_wt/<repo>-<N>-sssf` on `claude/<N>-sssf`, off origin/main. Returns
    `(worktree_path, branch_actually_checked_out)`.

    An existing clean directory used to be reused on trust alone — nothing
    proved it was actually a REGISTERED worktree of this repo (as opposed to
    a stale directory, one hand-created by an operator, or a leftover from
    something else entirely) or that it was even on the right branch. Now
    verified two ways before reuse: `git worktree list --porcelain` (from
    `repo_root`) must list this exact path, AND
    `git -C <path> rev-parse --abbrev-ref HEAD` must equal
    `claude/<N>-sssf`. Either check failing refuses outright — nothing here
    guesses. The branch this function reports back is always the one
    ACTUALLY checked out (verified on reuse; known by construction on a
    fresh `worktree add -b`), so `run_ticket` never has to assume
    `branch_name(cfg.task_id)` still describes reality.
    """
    wt = worktree_path(cfg.repo_root, cfg.task_id, cfg.worktree_root)
    expected_branch = branch_name(cfg.task_id)
    if wt.exists():
        if not _worktree_is_registered(cfg, deps, wt):
            raise RunRefused(
                f"{wt} exists but is not a registered git worktree of "
                f"{cfg.repo_root} (per `git worktree list --porcelain`) — "
                f"refusing to reuse a directory that merely sits at the "
                f"expected path")
        branch_check = deps.git(wt, "rev-parse", "--abbrev-ref", "HEAD")
        actual_branch = branch_check.stdout.strip()
        if branch_check.returncode != 0 or actual_branch != expected_branch:
            raise RunRefused(
                f"{wt} is a registered worktree but on branch "
                f"{actual_branch or '(unknown)'!r}, expected "
                f"{expected_branch!r} — refusing to reuse it")
        status = deps.git(wt, "status", "--porcelain")
        if status.returncode != 0:
            raise RunRefused(
                f"could not read git status in existing worktree {wt}: "
                f"{(status.stderr or status.stdout).strip()}")
        if status.stdout.strip():
            raise RunRefused(
                f"worktree {wt} already exists and is dirty — resolve or "
                f"remove it before re-running")
        return wt, actual_branch
    result = deps.git(cfg.repo_root, "worktree", "add", "-b", expected_branch,
                      str(wt), "origin/main")
    if result.returncode != 0:
        raise RunRefused(
            f"git worktree add failed: {(result.stderr or result.stdout).strip()}")
    return wt, expected_branch


def claim_ticket(cfg: ChainConfig, deps: RunDeps) -> str:
    label = session_label(cfg.agent, cfg.task_id)
    hb = heartbeat(label, agent=cfg.agent, agentctl=cfg.agentctl, run=deps.run)
    if hb.returncode != 0:
        raise RunRefused(
            f"heartbeat failed for session {label}: "
            f"{(hb.stderr or hb.stdout).strip()} — a labeled session needs a "
            f"live heartbeat before claim will accept it")
    cl = claim(cfg.task_id, label, agent=cfg.agent, agentctl=cfg.agentctl, run=deps.run)
    if cl.returncode != 0:
        raise RunRefused(
            f"claim failed for task #{cfg.task_id}: {(cl.stderr or cl.stdout).strip()}")
    return label


def run_chain_script(cfg: ChainConfig, deps: RunDeps, worktree: Path,
                     request_text: str, adw_id: str) -> subprocess.CompletedProcess:
    """Runs the chosen chain PINNED to `adw_id` (`--adw-id`, which every
    chain script already accepts — `session.ensure(cfg, adw_id)`), so the
    trace this run writes is unambiguously the one `read_trace_failure`
    reads back, never "whichever session happens to be latest" in a reused
    worktree's db."""
    script = CHAIN_SCRIPTS[cfg.chain]
    argv = ["uv", "run", f"adws/{script}", request_text, "--config", cfg.config,
           "--adw-id", adw_id]
    label = session_label(cfg.agent, cfg.task_id)

    def hb() -> None:
        result = heartbeat(label, agent=cfg.agent, agentctl=cfg.agentctl, run=deps.run)
        if result.returncode != 0:
            raise HeartbeatRejected(
                f"heartbeat for session {label} was REJECTED mid-run — "
                f"{(result.stderr or result.stdout).strip()}. Terminating the "
                f"chain rather than leaving it running against a lease this "
                f"process no longer holds.")

    return deps.chain_runner(argv, worktree, hb)


def read_trace_failure(worktree: Path, adw_id: str) -> tuple[str, str, list[str]]:
    """(adw_id, failing phase name, violation notes) for the SPECIFIC run
    `adw_id` names — read from that run's own `adws/adw_data/sssf.db`.

    This used to fall back to `SELECT adw_id FROM sessions ORDER BY
    started_at DESC LIMIT 1` whenever no `adw_id` was supplied — in a
    REUSED worktree (the db from a prior run is gitignored, so
    `ensure_worktree`'s cleanliness check never sees it), that reads the
    PREVIOUS run's session, not this one's, and a failure report could name
    the wrong adw_id/phase/violations entirely. `run_chain_script` now pins
    `--adw-id` explicitly, and this function queries by that exact id: when
    no session row exists for it (the chain died before ever writing one —
    a crash before `session.ensure()`, or a corrupt db caught by the
    caller), it reports "no session recorded" (`("", "", [])`) rather than
    silently returning someone else's data.
    """
    db_path = worktree / "adws" / "adw_data" / "sssf.db"
    if not db_path.is_file():
        return "", "", []
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT adw_id FROM sessions WHERE adw_id=?", (adw_id,)).fetchone()
        if not row:
            return "", "", []          # no session recorded for THIS run
        phase_row = conn.execute(
            "SELECT phase_id, name FROM phases WHERE adw_id=? AND status='fail' "
            "ORDER BY seq DESC LIMIT 1", (adw_id,)).fetchone()
        if not phase_row:
            return adw_id, "", []
        phase_id, phase_name = phase_row
        violations: list[str] = []
        for (violations_json,) in conn.execute(
                "SELECT violations_json FROM gate_results WHERE phase_id=? AND passed=0",
                (phase_id,)):
            try:
                violations.extend(json.loads(violations_json) or [])
            except (TypeError, ValueError):
                pass
        return adw_id, (phase_name or ""), violations
    finally:
        conn.close()


def _safe_read_trace_failure(worktree: Path, adw_id: str) -> tuple[str, str, list[str]]:
    """`read_trace_failure`, but a corrupt/unreadable trace db is reported
    INTO the failure path rather than escaping cleanup — `sqlite3.Error` (a
    genuinely corrupt db file, a locked db) must not propagate out of the
    claim-release/report flow. The minted `adw_id` we launched with is
    still known even when the db itself can't be read, so it is kept
    rather than blanked."""
    try:
        return read_trace_failure(worktree, adw_id)
    except sqlite3.Error as exc:
        return adw_id, "", [f"trace db error: {type(exc).__name__}: {exc}"]


def _report_failure(cfg: ChainConfig, deps: RunDeps, adw_id: str, phase: str,
                    violations: list[str], extra: str = "") -> str:
    """Release the claim and post the failure report — the one place every
    failure/abort path in `run_ticket` converges on.

    Both halves of this used to be fire-and-forget, return codes never
    checked:
      - RELEASE failing is surfaced LOUDLY: raises `ClaimReleaseFailed`
        (non-zero exit at the CLI, stderr names the ticket still claimed)
        rather than silently leaving the ticket stuck claimed by a dead
        session with no one told.
      - MSG failing degrades instead of disappearing: the agentctl error is
        folded into the RETURNED message text, so whatever prints/logs the
        `RunOutcome` still carries the full failure report even though the
        board itself never got it.
    """
    label = session_label(cfg.agent, cfg.task_id)
    reason = f"sssf run {adw_id or '?'} failed in phase {phase or '?'}"
    if extra:
        reason += f": {extra}"
    rel = release(cfg.task_id, reason, agent=cfg.agent, agentctl=cfg.agentctl, run=deps.run)
    if rel.returncode != 0:
        raise ClaimReleaseFailed(
            f"task #{cfg.task_id} FAILED and the claim release ALSO FAILED — "
            f"the ticket is still shown claimed by session {label!r}. "
            f"Release error: {(rel.stderr or rel.stdout).strip()}. "
            f"Original failure: {reason}")
    message = (f"sssf run failed. adw_id={adw_id or '?'} phase={phase or '?'} "
              f"violations={violations or ['(none captured in the trace db)']}")
    if extra:
        message += f" error={extra}"
    msg_result = post_msg(cfg.task_id, message, to=cfg.msg_to, agent=cfg.agent,
                          agentctl=cfg.agentctl, run=deps.run)
    if msg_result.returncode != 0:
        message += (
            f" [agentctl msg FAILED to post this report to the ticket: "
            f"{(msg_result.stderr or msg_result.stdout).strip()} — printed "
            f"here so it is not lost]")
    return message


def run_ticket(cfg: ChainConfig, deps: RunDeps | None = None) -> RunOutcome:
    deps = deps or RunDeps()
    draft_file = draft_path(cfg.repo_root, cfg.task_id)

    if not cfg.from_draft:
        result = draft(cfg.task_id, repo_root=cfg.repo_root, agent=cfg.agent,
                       agentctl=cfg.agentctl, run=deps.run, config=cfg.config)
        return RunOutcome(
            ok=True, stopped_for_review=True,
            message=(f"draft written to {result.path} — review the draft, "
                     f"then re-run with --from-draft"))

    if not draft_file.is_file():
        raise RunRefused(
            f"--from-draft given but {draft_file} does not exist — run "
            f"`adw_ticket.py draft --task {cfg.task_id}` first")
    request_text = draft_file.read_text(encoding="utf-8")

    worktree, branch = ensure_worktree(cfg, deps)
    claim_ticket(cfg, deps)

    # Everything from here on runs under a live claim, and the try starts
    # IMMEDIATELY: the try used to start AFTER `deps.new_adw_id()`, and the
    # trace read following a completed chain run sat OUTSIDE any try at
    # all. An exception minting the id, or an OSError/whatever surfacing
    # from the trace read despite `_safe_read_trace_failure`'s own
    # sqlite3.Error catch, would both leak the claim entirely — no release,
    # no report. One try now covers minting the id, running the chain,
    # reading the trace, AND (on a green chain) push + PR + review, all the
    # way through a successful PR-open.
    #
    # `except ClaimReleaseFailed: raise` comes FIRST, ahead of the general
    # handler: `_report_failure` can itself raise that (a failed `state
    # --state open`), and since everything is now ONE try, that exception
    # would otherwise be caught by the very `except BaseException` clause
    # whose job is to CALL `_report_failure` in the first place — a second
    # attempt would double-fire release/msg. Catching it first and simply
    # re-raising means a report was already attempted (successfully or not)
    # before that exception could ever exist, so nothing further to do here.
    adw_id = ""
    found_adw_id = ""
    phase = ""
    violations: list[str] = []
    pr_opened = False
    try:
        adw_id = deps.new_adw_id()   # pinned into the chain via --adw-id
        result = run_chain_script(cfg, deps, worktree, request_text, adw_id)
        found_adw_id, phase, violations = _safe_read_trace_failure(worktree, adw_id)

        if result.returncode != 0:
            # Ordinary chain failure (non-zero exit, no exception): report
            # and return a failed RunOutcome rather than raising.
            message = _report_failure(cfg, deps, found_adw_id, phase, violations)
            return RunOutcome(ok=False, worktree=worktree, adw_id=found_adw_id,
                              failing_phase=phase, violations=violations, message=message)

        ship.assert_not_main(branch)
        push = deps.git(worktree, "push", "-u", "origin", branch)
        if push.returncode != 0:
            raise RunRefused(f"push failed: {(push.stderr or push.stdout).strip()}")
        summary = f"sssf({found_adw_id})" if found_adw_id else "sssf"
        title = ship.pr_title([cfg.task_id], summary)
        body = ship.pr_body([cfg.task_id], f"adw_id: {found_adw_id}" if found_adw_id else "")
        url = deps.pr_creator(repo=cfg.review_repo, title=title, body=body, head=branch)
        pr_opened = True              # the PR is LIVE — no release/report past this point
        deps.reviewer(cfg.review_repo)
        return RunOutcome(ok=True, worktree=worktree, adw_id=found_adw_id, pr_url=url)
    except ClaimReleaseFailed:
        raise               # a report was already attempted; never double-fire
    except BaseException as exc:
        if pr_opened:
            raise            # the PR already exists; nothing to release or report
        found_adw_id, phase, violations = _safe_read_trace_failure(worktree, adw_id or "")
        _report_failure(cfg, deps, found_adw_id, phase, violations,
                       extra=f"{type(exc).__name__}: {exc}")
        raise
