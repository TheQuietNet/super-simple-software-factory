"""Run each agent phase against a disposable COPY of the repo, never the real
worktree — the durable fix for the gap `permissions.py`'s own docstring
already names: a builder with bash inside a REAL git checkout can defeat
every rail in that module through git plumbing itself (`git commit`,
`git config`, a planted hook, `.git/info/exclude`, `assume-unchanged`...).
None of that is available to an agent that cannot reach the real `.git` at
all.

THE SHAPE
---------
`prepare()` mirrors the real worktree's tracked + untracked-not-ignored files
(`git ls-files -co --exclude-standard`) into a throwaway directory, then
`git init`s a BRAND NEW, unrelated git repo there with one baseline commit —
so the agent's own `git status`/`git diff` still work, but there is no path
from that `.git` back to the real one. The agent's subprocess env additionally
has every `GIT_*` variable scrubbed (`scrub_git_env`, wired into
`utils.operator_env()`), and `agent_env()` further rewrites path-revealing
variables (`PWD`/`OLDPWD`) so the REAL repo's absolute path never appears in
the agent subprocess's own environment — see THREAT MODEL below for what this
does and does not achieve.

`sync()` is called after every turn (`agents.execute`'s `send()`) so gates
that inspect `run.repo_root` mid-phase see accurate state: it diffs the copy
against its OWN pinned baseline BY CONTENT (never through the copy's git,
which the agent fully controls) and copies across anything
`permissions.permitted()` allows — the exact same allowlist (`writes:` /
`protected_files`) that governs a direct write today, PLUS a compare-and-swap
against the real tree's own recorded state (a concurrent/operator edit to a
path the agent also touched is a conflict, never silently overwritten — see
`apply_back`) and a case-insensitive-filesystem-aware re-check of
`protected_files` (see `_case_insensitive_protection_bypass`). For a
BUILDER-CLASS call (`call.output_type` carries `changed_files` — the same set
`gates.BUILDER_GATES` applies to, passed down as `require_scope=True`), a
path must ALSO be in the PINNED requested scope (`gates.requested_scope()` —
`Where:`, else a planner's frozen `plan.md` tokens, never a live re-read).
Anything not permitted (or in scope, or conflict-free, or a safe symlink) is
left sitting in the copy, untouched in the real tree, and never silently
dropped.

`finalize()` is the one call that can FAIL the phase: it re-syncs once more,
then raises `permissions.PermissionBreach` (the existing class — same
correction-retry / non-correctable semantics `agents.execute` already has)
if anything is still sitting unapplied. `permissions.enforce()` still runs
immediately afterward, against the now-updated real tree, as defense in
depth — see that module's own docstring; it should never find anything to
do here, since apply-back already filtered through the identical predicate,
but a bug in THIS module must not silently defeat it.

RESUME: the copy lives at a path keyed by `(repo-hash, adw_id, agent.name)`,
under a temp root OUTSIDE the repo (`SSSF_ISOLATION_ROOT`, else the system
temp dir) — stable across `--adw-id` resume, which may re-invoke this in a
brand new process. `prepare()` reuses an existing copy + its persisted
baseline rather than rebuilding, and — on every call, not just a fresh one —
`_refresh_untouched_paths()` rebases any path the agent has NOT itself edited
in the copy to the real tree's CURRENT state (a path the agent IS mid-editing
is left completely alone; `apply_back`'s own compare-and-swap is what catches
the real tree having moved under a path the agent does go on to touch).
Every `prepare()` call also reseeds the copy's `context_handoff/` from the
real one, so a later-running agent (builder, reviewer) can see an earlier
one's (planner, scout) handoff output, not just its own.

SESSION RUNTIME stays out of the agent's own visible surface too: pi's own
session bookkeeping (`--session`/`--session-dir`) and raw event output live
under the COPY (`Isolation.runtime_rel`), never an absolute path into the
real repo, and are harvested back to the real `data_dir` after every turn —
see `agents.execute`.

BUDGET: #53193's whole-phase deadline governs more than agent sends.
`agents.execute()` pins it, wraps it in a `Budget`, and passes that into
`prepare()` — BEFORE prepare() does any of its own (unbounded: every
tracked+untracked file in the repo) copy/scan work, which used to run with
no deadline concept at all. The `Budget` then rides on the returned
`Isolation` object, so `sync()`/`finalize()`/`apply_back()`/
`harvest_handoff()`/`harvest_runtime()` need no new parameter of their own —
they read `iso.budget`. Every scan/copy loop in this module (`_copy_tree`,
`_walk_copy`, `_refresh_untouched_paths`, `_seed_handoff`, `harvest_handoff`,
`harvest_runtime`) calls `_check_deadline()` on every iteration —
`time.monotonic()` is cheap enough that there is no reason to batch it, and
checking every item catches a single slow file immediately rather than only
at a batch boundary. `apply_back`'s actual WRITE loop is the one exception,
by design: the change set it applies is already computed and bounded (only
the paths `permissions.changed_paths()` found different, a small set), so
the deadline is checked ONCE, right before that loop starts, and never
again inside it — once a write to the REAL tree begins, the (small, bounded)
batch runs to completion rather than leaving the real worktree half-applied.
A `budget=None` (the default on every function that takes one) means no
deadline at all — every direct call in this module's own test suite that
predates the budget wiring is unaffected.

CLEANUP: `cleanup_run()` removes the whole `<root>/<repo-hash>/<adw_id>/`
tree on success. It is called from THREE places, so an abnormal exit cannot
leak a copy forever: `Run.finish()` (the normal end of a run), `Run.phase()`'s
own exception handler (a phase that raises propagates past every ADW script's
`try`, but the run is already over at that point regardless of what the
script does next), and an `atexit` hook registered in `session.ensure()` (the
last-resort net — idempotent with the other two, since it no-ops once the
directory is already gone). On failure it is kept only when
`SSSF_KEEP_ISOLATION_ON_FAILURE` is truthy, for post-mortem debugging.

THREAT MODEL: isolation contains a CARELESS agent and accidental/plumbing-
level git damage (the class `permissions.py`'s own docstring names), and
removes the obvious, casual path to the real repo (cwd, session paths, a
fixed allowlist of cwd/project-path ENV VARS, rendered prompts). It is NOT a
security boundary against a DETERMINED adversary with host filesystem
access: `cwd` is not a jail, bash can `cd` anywhere the OS lets the process
go, and a sufficiently motivated agent enumerating the filesystem (or
reading its own harness's source, which names `SSSF_ISOLATION_ROOT` and the
real `data_dir` layout) can still find and touch the real repo directly,
bypassing this module entirely. A REAL boundary needs OS-level containment
— a separate OS user, a container, or a VM/WSL instance with only the copy
mounted — tracked separately, not attempted here.

ACCEPTED, KNOWN EXPOSURE: this module deliberately leaves `PATH` completely
untouched — `agent_env()` passes it through exactly as `utils.operator_env()`
produced it, rewriting nothing on it, for any repo-local tool directory it
names (`node_modules/.bin`, a `.venv/Scripts` or `bin/`), because the copy
never mirrors a gitignored dependency install and a broken/empty PATH entry
would silently kill the agent's own tool discovery — a functional regression
worse than the narrow exposure leaving it alone creates. Any such entry that
survives to the agent's subprocess resolves into the REAL repo, not the
copy. (`operator_env()` itself — for reasons unrelated to isolation, see its
own docstring — already pops an active `VIRTUAL_ENV` and strips exactly that
venv's POSIX `bin/` from PATH before `agent_env()` ever sees it, so on POSIX
a repo-local venv's `bin/` entry may already be gone by the time this layer
runs; this module still never touches PATH itself, on any platform, and the
Windows `Scripts/` equivalent is unaffected by that upstream strip either
way.) `agent_env()`'s own env scrubbing is an ALLOWLIST of a handful of
cwd/project-path variables (`PWD`, `OLDPWD`, `INIT_CWD`,
`npm_config_local_prefix`, `npm_package_json`, `VIRTUAL_ENV`, `PYTHONPATH`,
`UV_PROJECT*`) — an earlier version genericly scrubbed EVERY env var whose
value merely contained the repo root as a substring, which corrupted an
unrelated value (a webhook URL, a JSON blob, a credential) that happened to
reference the repo path inside something else entirely; narrowed back to
only the variables this is actually meant to cover.

LIMITATION: a submodule's own working tree is not mirrored into the copy,
only the gitlink path itself (inert here, out of scope for this fix, same
stance as `permissions.assert_no_dirty_submodules`'s).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

from . import agent_budget, gates, permissions
from .data_types import AgentConfig

GIT_ENV_PREFIX = "GIT_"
KEEP_ON_FAILURE_ENV = "SSSF_KEEP_ISOLATION_ON_FAILURE"
ROOT_OVERRIDE_ENV = "SSSF_ISOLATION_ROOT"
_BASELINE_FILE = "baseline.json"
_HANDOFF_SEED_FILE = "handoff_seed.json"


#: The floor `Budget.remaining()` clamps to, so a caller handing it straight
#: to `subprocess.run(timeout=...)` never passes zero or a negative value —
#: `subprocess` raises before the process is even given a chance to run for
#: either. A deadline already past still gets this small, bounded window to
#: let an in-flight git call finish rather than being refused a timeout
#: value entirely; if it genuinely hangs, it still gets killed at (at most)
#: this many seconds past the deadline, which `_run_git`'s own
#: `TimeoutExpired` -> `PhaseTimeout` conversion then reports.
_GIT_TIMEOUT_FLOOR = 0.05


@dataclass(frozen=True)
class Budget:
    """The phase's absolute #53193 wall-clock deadline, threaded through
    every isolation scan/copy/apply step — see the module docstring's own
    BUDGET section for why. `deadline` is a `time.monotonic()`-comparable
    absolute value (never re-derived per call, same discipline as
    `PiRequest.deadline`); `nominal_seconds` is carried only for the
    `agent_budget.PhaseTimeout` message this raises (matches
    `PiRequest.timeout_seconds`'s own docstring reasoning — the phase's
    ORIGINAL configured total, not a shrinking remainder).
    """
    deadline: float
    nominal_seconds: int

    def remaining(self) -> float:
        """Seconds left before `deadline`, floored at `_GIT_TIMEOUT_FLOOR`
        — for a caller (`_run_git`) that hands this straight to
        `subprocess.run(timeout=...)`, which treats zero/negative specially
        rather than failing fast the way `_check_deadline()` does.
        """
        return max(_GIT_TIMEOUT_FLOOR, self.deadline - time.monotonic())


def _check_deadline(budget: Optional[Budget]) -> None:
    """Call on every iteration of a scan/copy loop in this module. A no-op
    when `budget` is None — every call site in this module's own test suite
    that predates the budget wiring passes no budget at all and is
    unaffected. `time.monotonic()` is cheap enough (tens of nanoseconds)
    that checking every item, rather than batching every Nth one, has no
    measurable cost even over a large tree, and catches a single
    pathologically slow file immediately instead of only at a batch
    boundary.
    """
    if budget is not None and time.monotonic() >= budget.deadline:
        raise agent_budget.PhaseTimeout(budget.nominal_seconds)


@dataclass(frozen=True)
class _CopyLayout:
    """The three paths `_copy_tree`/`_refresh_untouched_paths` both need,
    bundled into one object (four-param rule, `data_types.py`'s own
    docstring) — `prepare()` builds one and passes it to both rather than
    each carrying `repo_root`/`copy_root`/`session_prefix` as three separate
    positional params, which the added `budget` param would otherwise have
    pushed past four.
    """
    repo_root: Path
    copy_root: Path
    session_prefix: Path


def scrub_git_env(env: dict[str, str]) -> dict[str, str]:
    """Strip every `GIT_*` variable from an env dict headed to a subprocess.

    A leaked `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE`... from the parent
    process's own environment would otherwise let an agent's git commands
    silently operate on the REAL repo despite `cwd` pointing at the copy —
    `git` honors these over `cwd` unconditionally. Wired into
    `utils.operator_env()`, which every agent runner (`agent_pi`,
    `agent_cc`, `agent_grok`) builds its subprocess env from, so this is
    unconditional for every agent phase, not opt-in per call site.
    """
    return {k: v for k, v in env.items() if not k.startswith(GIT_ENV_PREFIX)}


#: An earlier GENERIC rule — scrub any env var whose value merely CONTAINS
#: the repo root as a substring — corrupted non-path values that happened to
#: reference the repo root inside something else entirely: a webhook URL
#: with the repo path in a query string, a JSON config blob, a credential.
#: Only cwd/project-path variables are in scope now; every other variable
#: passes through byte-identical, regardless of what its value looks like.
#: Matched case-insensitively — Windows uppercases env var NAMES on
#: assignment (`os.environ["npm_x"] = ...` iterates back as `"NPM_X"`), a
#: platform quirk of `os.environ` itself, unrelated to what this set means.
#:
#: `PATH` is deliberately NOT on this list: a repo-local tool directory on
#: it (`node_modules/.bin`, `.venv/Scripts`) must keep resolving so the
#: agent's own tool discovery keeps working — see `agent_env`'s docstring
#: and the module's own THREAT MODEL section for why this is an accepted,
#: deliberate exposure, not an oversight.
_AGENT_ENV_ALLOWLIST = frozenset({
    "PWD", "OLDPWD", "INIT_CWD", "NPM_CONFIG_LOCAL_PREFIX", "NPM_PACKAGE_JSON",
    "VIRTUAL_ENV", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT", "UV_PROJECT",
})


def _rewrite_or_drop_path_entry(entry: str, repo_root_norm: str, copy_root_norm: str) -> Optional[str]:
    """One `PATH`-style entry (or a whole single-path var's value, treated as
    a one-element list — see `_scrub_env_value`): rewritten onto the copy if
    it resolves under the real repo root, dropped if it references the real
    root without a clean rewrite, left untouched otherwise.

    Only ever called for a value from `_AGENT_ENV_ALLOWLIST` — see that
    set's own docstring for why this is no longer applied to every env var.

    Pure STRING comparison, never `Path.resolve()` against the current
    process cwd: an unrelated bare token (an API key, a short flag value)
    would otherwise resolve AS IF it were a relative path off cwd — which,
    since the ADW process's own cwd IS `repo_root`, would falsely look like
    it "resolves under" it. Comparing prefixes/substrings directly avoids
    that entirely, and is also what makes this naturally case-insensitive
    (Windows) and slash-style-insensitive (an authored/inherited env value
    may use either separator regardless of host OS).
    """
    if not entry:
        return entry
    norm_entry = entry.replace("\\", "/")
    if norm_entry.casefold().startswith(repo_root_norm.casefold()):
        suffix = norm_entry[len(repo_root_norm):]
        rewritten = (copy_root_norm + suffix).replace("/", os.sep)
        # "rewrite to the copy root where that's meaningful, else drop": a
        # PATH entry under a gitignored, never-mirrored directory (a
        # project-local .venv is the common case) would rewrite to a path
        # that does not exist in the copy at all — pointing the agent at a
        # broken, silently-empty PATH entry is worse than simply not having
        # it.
        return rewritten if Path(rewritten).exists() else None
    if repo_root_norm.casefold() in norm_entry.casefold():
        return None   # references the real root but not as a clean prefix — drop rather than guess
    return entry


def _scrub_env_value(value: str, repo_root_norm: str, copy_root_norm: str) -> str:
    """Split on `os.pathsep` (a no-op for a single-path value — it simply
    yields one "entry" — and the right thing for `PYTHONPATH`, which CAN
    genuinely be a multi-entry list), rewrite or drop each entry, rejoin.
    Splitting and rejoining with the SAME separator is a lossless identity
    operation for any value with no matching entry. Only ever called for a
    value from `_AGENT_ENV_ALLOWLIST` (see its own docstring).
    """
    if not value:
        return value
    kept = []
    for entry in value.split(os.pathsep):
        rewritten = _rewrite_or_drop_path_entry(entry, repo_root_norm, copy_root_norm)
        if rewritten is not None:
            kept.append(rewritten)
    return os.pathsep.join(kept)


def agent_env(run, iso: "Isolation") -> dict[str, str]:
    """The env handed to THIS agent's subprocess specifically.

    `utils.operator_env()` already strips `GIT_*` (`scrub_git_env`, shared
    with the quality/test phase, which legitimately runs in the real tree).
    This layer is agent-only and ALLOWLIST-based — corrects an earlier
    "scrub every env var generically" over-reach, which corrupted a
    non-path value that merely happened to CONTAIN the repo root somewhere
    inside it — a webhook URL, a JSON config blob, a credential: only
    `_AGENT_ENV_ALLOWLIST` (cwd/project-path variables — `PWD`, `OLDPWD`,
    `INIT_CWD`, `npm_config_local_prefix`, `npm_package_json`,
    `VIRTUAL_ENV`, `PYTHONPATH`, `UV_PROJECT*`) is ever rewritten; every
    other variable, `PATH` included (see `_AGENT_ENV_ALLOWLIST`'s own
    docstring), passes through exactly as `operator_env()` produced it.
    """
    from .utils import operator_env

    env = operator_env()
    repo_root_norm = str(Path(run.repo_root).resolve()).replace("\\", "/")
    copy_root_norm = str(iso.copy_root.resolve()).replace("\\", "/")
    result: dict[str, str] = {}
    for key, value in env.items():
        if key.upper() in _AGENT_ENV_ALLOWLIST:
            result[key] = _scrub_env_value(value, repo_root_norm, copy_root_norm)
        else:
            result[key] = value
    return result


def isolation_root() -> Path:
    override = os.environ.get(ROOT_OVERRIDE_ENV, "").strip()
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "sssf-isolation"


def _run_root(run) -> Path:
    """`<isolation_root>/<repo-hash>/<adw_id>/` — every copy for this run.

    Keyed by a hash of the REAL repo_root, not just adw_id: adw_ids are
    short and operator-chosen/test-reused (several fixtures across this
    module's own test suite pin the same literal id against a fresh
    tmp_path repo each time), and this lives under a FIXED system temp
    directory shared by every repo on the machine — without the repo in the
    key, two unrelated repos (or two test runs) reusing the same adw_id
    would silently hand each other's agent a stale copy and baseline.
    """
    repo_key = hashlib.sha256(str(Path(run.repo_root).resolve()).encode("utf-8")).hexdigest()[:16]
    return isolation_root() / repo_key / run.adw_id


@lru_cache(maxsize=None)
def _fs_is_case_insensitive(root: str) -> bool:
    """Probe the ACTUAL filesystem at `root`, not the OS default: Windows and
    a default-configured macOS volume (APFS/HFS+) are case-insensitive (and
    case-PRESERVING); Linux, and an explicitly case-sensitive macOS volume,
    are not. Trusting `os.name` alone would get macOS wrong in either
    direction depending on how the volume was formatted, and this is a
    security question, not a cosmetic one, so it checks the real mount
    instead of guessing.

    Cached per resolved root path — this repo's own filesystem does not
    change case-sensitivity mid-run.

    A probe FAILURE (permissions, a read-only mount, an exotic filesystem)
    used to fall back to `os.name == "nt"` — wrong on a case-insensitive
    macOS volume (`os.name` there is `posix`), which would silently DISABLE
    the case-folded `protected_files` re-check on exactly the platform most
    likely to need it. Fail CLOSED instead: assume case-INSENSITIVE (the
    stricter posture — it can only make `apply_back` reject something an
    exact-case check alone would have allowed, never the other way around)
    whenever the probe itself cannot answer, on every platform.
    """
    probe_root = Path(root)
    probe = probe_root / f".sssf-case-probe-{os.getpid()}"
    try:
        probe.write_text("x", encoding="utf-8")
        return probe.with_name(probe.name.upper()).exists()
    except OSError:
        return True
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def _mode_component(path: Path) -> str:
    """The executable-bit half of a fingerprint — POSIX only.

    `permissions._fingerprint()` (content-hash only) is blind to a
    mode-only change (`chmod +x`, no byte touched), so such a change never
    appears in `changed_paths()` and is silently dropped by apply-back even
    though `shutil.copy2`/the explicit `chmod` in `_copy_one` WOULD have
    carried it correctly had the path been in the changed-set for any other
    reason. Folded into every fingerprint this module computes so a
    mode-only change is detected too.

    Windows has no real POSIX exec bit — Python's `st_mode` there is
    synthesized from the extension/ACL, not meaningful the way it is on
    POSIX — so this is unconditionally the empty string there, documented
    as N/A rather than emulated.
    """
    if os.name == "nt":
        return ""
    try:
        if path.is_file() and not path.is_symlink():
            return ":x" if path.stat().st_mode & 0o111 else ""
    except OSError:
        pass
    return ""


def _fp(path: Path) -> str:
    """This module's own fingerprint: `permissions._fingerprint()` (content,
    by kind — file/symlink/submodule/absent) plus the POSIX executable-bit
    component. Used everywhere a path is compared — the copy's baseline,
    its current state, and the real tree's own recorded state for
    compare-and-swap — so all three always agree on what "the same" means.
    """
    return permissions._fingerprint(path) + _mode_component(path)


@dataclass
class Isolation:
    """One agent's disposable copy for one run. Returned by `prepare()`,
    threaded through `sync()`/`finalize()`, never persisted itself — its
    `baseline_path` on disk is what survives across a `--adw-id` resume."""

    copy_root: Path
    baseline_path: Path
    session_prefix: Path          # relative: <data_dir>/sessions/<adw_id> — excluded from apply-back
    context_handoff_rel: Path     # relative: session_prefix/context_handoff
    real_context_handoff_dir: Path
    runtime_rel: Path             # relative: session_prefix/<agent.name> — pi's own session bookkeeping
    real_runtime_dir: Path
    # PERSISTED (not just in-memory — see _load_handoff_seed/
    # _save_handoff_seed) alongside baseline_path, so it survives across
    # every prepare() call for this (repo, adw_id, agent), not just within
    # one Isolation object's lifetime.
    handoff_seed_path: Path
    case_insensitive: bool = False
    # The #53193 whole-phase deadline, set once by prepare() from whatever
    # `budget` its own caller passed in (None if none) — see the module
    # docstring's BUDGET section. Rides on this object so
    # sync()/finalize()/apply_back()/harvest_handoff()/harvest_runtime()
    # need no new parameter of their own.
    budget: Optional[Budget] = None
    # Fingerprint of every context_handoff/ file as SEEDED from the real one
    # — harvest_handoff() only copies back a file whose CURRENT fingerprint
    # differs from this, so one agent's harvest can never clobber a sibling
    # agent's own file that this agent merely inherited by being seeded
    # with it. Loaded from handoff_seed_path by _seed_handoff() on every
    # prepare() call — never starts fresh just because a NEW Isolation
    # object was constructed (it used to, which let a resumed copy's
    # unchanged-since-first-seed file be mistaken for agent-authored and
    # re-harvested over a newer real one).
    handoff_seed: dict[str, str] = field(default_factory=dict)


@dataclass
class ApplyResult:
    applied: list[str] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)


def _run_git(args: list[str], cwd, budget: Optional[Budget] = None) -> subprocess.CompletedProcess:
    """Every git subprocess `prepare()` spawns (`ls-files`, the copy's own
    `init`/`config`/`add`/`commit`) is bounded by the SAME phase budget as
    everything else in this module — an unbounded git call was the one
    remaining way `prepare()` could outlive the phase deadline regardless of
    the per-iteration/post-loop scan checks, since git itself is never on an
    iteration boundary this module controls. `budget=None` (the default —
    every pre-budget-wiring caller) means no timeout at all, same as
    `subprocess.run` without one. A `TimeoutExpired` is converted to the
    same `agent_budget.PhaseTimeout` every other deadline-governed path in
    this module raises, not left as a raw stdlib exception a caller would
    have no reason to expect from a git helper.
    """
    kwargs = dict(cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if budget is not None:
        kwargs["timeout"] = budget.remaining()
    try:
        return subprocess.run(["git", *args], **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise agent_budget.PhaseTimeout(budget.nominal_seconds) from exc


def _list_source_paths(repo_root: Path, budget: Optional[Budget] = None) -> list[str]:
    """Every tracked + untracked-not-ignored path, repo-relative.

    `-co --exclude-standard`: cached (tracked) + others (untracked), minus
    anything gitignored. The copy only ever holds what the real repo's own
    `.gitignore` already scopes an agent's normal view to.
    """
    result = _run_git(["ls-files", "-co", "--exclude-standard", "-z"], repo_root, budget)
    if result.returncode != 0:
        raise RuntimeError(f"git ls-files failed in {repo_root}: {result.stderr.strip()}")
    return [p for p in result.stdout.split("\0") if p]


def _copy_one(src: Path, dst: Path) -> None:
    """Copy `src` onto `dst`, never THROUGH an existing destination symlink.

    An earlier version only unlinked `dst` when `src` was ALSO a symlink. A
    two-turn attack — turn 1 installs a PERMITTED symlink (applied for real:
    `dst` becomes a symlink), turn 2 replaces the same copy-side path with an
    ordinary file — used to fall into the plain `shutil.copy2(src, dst)`
    branch, which OPENS `dst` FOR WRITING and so writes straight through the
    now-still-a-symlink `dst` to whatever it points at. Unlinking any
    existing destination symlink FIRST, regardless of what `src` currently
    is, closes that unconditionally — paired with
    `_symlink_target_is_within_repo()` in `apply_back`, which refuses to
    ever create a real-side symlink whose target would escape the repo or
    point into `.git` in the first place.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink():
        dst.unlink()
    if src.is_symlink():
        target = os.readlink(src)
        if dst.exists():
            dst.unlink()
        try:
            os.symlink(target, dst)
        except OSError:
            # Windows without symlink privilege: best-effort, skip rather
            # than fail the whole copy over a link this repo does not use.
            pass
    elif src.is_file():
        shutil.copy2(src, dst)
        if os.name != "nt":
            # Explicit, not just relying on copy2's own metadata copy — the
            # exec bit is security-relevant and this makes the guarantee
            # visible and directly testable.
            os.chmod(dst, stat.S_IMODE(src.stat().st_mode))
    # A submodule gitlink directory is neither — see module docstring.


def _copy_tree(layout: _CopyLayout, budget: Optional[Budget]) -> None:
    """Mirror every tracked + untracked-not-ignored real-repo path into the
    copy, EXCEPT the session-runtime subtree (`layout.session_prefix` —
    `data_dir/sessions/<adw_id>`). Production's own `.gitignore` should
    already keep that subtree out of `git ls-files`, but this does not rely
    on that: a repo whose `.gitignore` does not (yet) cover it — every test
    fixture in this module's own suite, none of which sets one up — would
    otherwise leak the harness's OWN session bookkeeping into the initial
    copy before `_seed_handoff()` ever gets a chance to seed
    `context_handoff/` on its own terms, corrupting its "already seeded vs.
    inherited from the plain mirror" bookkeeping.

    This is the exact unbounded-scan gap the phase-budget interaction
    finding named — `_check_deadline()` on every path, so a slow/large
    initial copy cannot silently outlive the phase's own budget (see the
    module docstring's BUDGET section).

    A per-iteration check ALONE still misses the LAST item — it only ever
    catches a slow item on the NEXT iteration's check, and there is no next
    iteration for the last one. A final `_check_deadline()` after the loop
    closes that gap; `git ls-files` itself (`_list_source_paths`) is now
    also bounded by `budget` — see `_run_git`'s own docstring.
    """
    exclude_parts = layout.session_prefix.parts
    for rel in _list_source_paths(layout.repo_root, budget):
        _check_deadline(budget)
        if _under(Path(rel).parts, exclude_parts):
            continue
        _copy_one(layout.repo_root / rel, layout.copy_root / rel)
    _check_deadline(budget)


def _git_init_baseline(copy_root: Path, budget: Optional[Budget] = None) -> None:
    """A brand-new, unrelated git repo — never related to the real one by
    object history, remotes, or config — so the agent's own `git status`/
    `git diff` behave normally without any path back to the real `.git`.
    Each of the five git calls below is bounded by `budget` (see
    `_run_git`'s own docstring)."""
    def run(*args: str) -> None:
        result = _run_git(list(args), copy_root, budget)
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed in {copy_root}: "
                               f"{result.stderr.strip()}")
    run("init", "-q", "-b", "main")
    run("config", "user.email", "sssf-isolation@local.invalid")
    run("config", "user.name", "sssf-isolation")
    run("add", "-A")
    run("commit", "-q", "--allow-empty", "-m", "sssf isolation baseline")


def _under(rel_parts: tuple[str, ...], prefix_parts: tuple[str, ...]) -> bool:
    return rel_parts[:len(prefix_parts)] == prefix_parts


def _walk_copy(copy_root: Path, exclude_rel: Path, budget: Optional[Budget] = None) -> dict[str, str]:
    """Content+mode fingerprint of every current file/symlink in the copy,
    keyed by POSIX-style repo-relative path. Walks the raw filesystem, never
    the copy's own git — that git is fully agent-controlled and answers a
    different, untrustworthy question. `.git/` and the session-runtime
    subtree (harvested separately by `sync`/`finalize`) are excluded.

    `_check_deadline()` on every path — called both from `prepare()` (the
    initial baseline walk) and `apply_back()` (the per-sync/finalize
    current-state walk), both unbounded full-tree scans the phase-budget
    interaction finding named.

    A final `_check_deadline()` after the loop — a per-iteration check
    alone only catches a slow item on the NEXT iteration, missing the LAST
    one (there is no next iteration to catch it on). `_fp()` is the
    expensive per-item work here (content hashing).
    """
    exclude_parts = exclude_rel.parts
    fingerprints: dict[str, str] = {}
    for path in copy_root.rglob("*"):
        _check_deadline(budget)
        if path.is_dir() and not path.is_symlink():
            continue
        rel = path.relative_to(copy_root)
        parts = rel.parts
        if not parts:
            continue
        if parts[0] == ".git":
            continue
        if _under(parts, exclude_parts):
            continue
        fingerprints[rel.as_posix()] = _fp(path)
    _check_deadline(budget)
    return fingerprints


def _symlink_target_is_within_repo(copy_root: Path, rel: str, repo_root: Path) -> bool:
    """A symlink is only ever applied back if it has a RELATIVE target that
    stays inside `repo_root` and outside `.git` when resolved from the REAL
    destination path — never the copy's, and never an absolute target at
    all.

    An ABSOLUTE target that happens to resolve INSIDE THE COPY used to pass
    an earlier version of this check (which validated against `copy_root`)
    — but `_copy_one` reproduces an absolute target VERBATIM on the real
    side (an absolute path is unaffected by which directory contains the
    link), so the real tree would end up with a symlink pointing at the
    disposable copy, breaking the moment it's cleaned up. Absolute targets
    are refused outright, unconditionally — not "allowed if they happen to
    resolve somewhere that currently looks safe". A relative target is
    validated as it will actually resolve once applied — from
    `repo_root / rel`'s own directory, not the copy's — which is also just
    more directly correct than relying on the copy and the real tree always
    having identical relative structure (true today, but not something this
    check should have to assume).
    """
    link_path = copy_root / rel
    try:
        target = os.readlink(link_path)
    except OSError:
        return False
    if os.path.isabs(target):
        return False
    real_link_path = repo_root / rel
    target_path = real_link_path.parent / target
    try:
        resolved = target_path.resolve(strict=False)
        repo_root_resolved = repo_root.resolve()
        rel_target = resolved.relative_to(repo_root_resolved)
    except (ValueError, OSError):
        return False
    return not (rel_target.parts and rel_target.parts[0] == ".git")


def _case_insensitive_protection_bypass(rel: str, agent: AgentConfig, cfg) -> bool:
    """True when `rel` would otherwise be applied ONLY because a case
    difference the real filesystem does not actually distinguish let it
    slip past `protected_files` — e.g. `PACKAGE.JSON` vs a protected
    `package.json` on Windows/default macOS.

    Mirrors `permissions.permitted()`'s own precedence for the escape
    hatch: a case-folded match against the agent's OWN `writes:` list still
    unlocks it, exactly like naming the exact-case path does today.
    """
    folded = rel.casefold()
    protected_hit = any(permissions._matches(folded, p.casefold())
                        for p in cfg.defaults.protected_files)
    if not protected_hit:
        return False
    unlocked = any(permissions._matches(folded, p.casefold()) for p in (agent.writes or []))
    return not unlocked


def _load_baseline_dict(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_baseline_dict(path: Path, data: dict[str, str]) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _seed_handoff(iso: Isolation) -> None:
    """Mirror the REAL `context_handoff/` into the copy's own, so THIS
    agent can see prior agents' handoff output (the planner's `plan.md`,
    the scout's `findings.md`, ...) — separate per-agent copies otherwise
    each start with an EMPTY handoff dir, and a builder/reviewer never sees
    what came before it. Called on EVERY `prepare()`, not just a fresh
    copy's, since another agent's phase may have completed (and harvested)
    since this agent's copy was last prepared, including across a
    `--adw-id` resume in a new process.

    `iso.handoff_seed_path` is loaded here as the starting point, not
    `iso.handoff_seed` (which is always a fresh, empty dict on a brand new
    `Isolation` object — an earlier version treated "no in-memory record of
    ever seeding this path" as license to reseed it, indistinguishable from
    "genuinely never seeded" even when the PERSISTED record said otherwise.
    Two failure modes followed from that:

      1. A resumed copy's OWN unchanged-since-first-seed file (e.g.
         plan.md, never touched by this agent) would fail to record itself
         in the fresh, empty in-memory dict — `harvest_handoff()` would
         then see "no seed record" for it, treat it as agent-authored, and
         re-harvest it over whatever the REAL file had become since (a
         genuinely newer version another agent wrote) — silent data loss.
      2. An agent-DELETED copy-side file (the agent removed a stale
         handoff doc) would look identical to "never seeded at all" (both
         are simply "target does not exist"), so the old code reseeded —
         RESURRECTED — a deletion the agent made deliberately.

    Both are fixed by loading the PERSISTED seed record (which is missing
    a path if and only if it was truly never seeded) and branching on it
    explicitly: a path with NO record is seeded fresh; a path WITH a
    record whose target is now missing was DELETED by the agent and is
    left alone; a path WITH a record whose target still matches it is
    re-seeded (picks up upstream changes); a path WITH a record whose
    target has DIVERGED from it is an in-flight agent edit and is left
    alone.

    `_check_deadline(iso.budget)` on every path — handoff dirs are normally
    small, but this is called on EVERY `prepare()` (including a fast
    in-process retry turn) so it gets the same treatment as this module's
    other scans rather than an assumption about size.

    A final `_check_deadline(iso.budget)` after the loop — see
    `_copy_tree`'s matching note for why a per-iteration check alone misses
    the last item.
    """
    dst_root = iso.copy_root / iso.context_handoff_rel
    dst_root.mkdir(parents=True, exist_ok=True)
    known = _load_baseline_dict(iso.handoff_seed_path)
    src_root = iso.real_context_handoff_dir
    if src_root.is_dir():
        for path in src_root.rglob("*"):
            _check_deadline(iso.budget)
            if path.is_dir():
                continue
            rel = path.relative_to(src_root)
            rel_key = rel.as_posix()
            target = dst_root / rel
            recorded = known.get(rel_key)
            target_current = (_fp(target) if target.exists() or target.is_symlink()
                              else permissions.DELETED)

            if recorded is not None and target_current == permissions.DELETED:
                continue   # the agent deliberately deleted this — never resurrect it
            if recorded is not None and target_current != recorded:
                continue   # the agent is mid-editing this — never clobber in-flight work

            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            known[rel_key] = _fp(target)
        _check_deadline(iso.budget)

    iso.handoff_seed = known
    _save_baseline_dict(iso.handoff_seed_path, known)


def _refresh_untouched_paths(layout: _CopyLayout, known: dict[str, str],
                             budget: Optional[Budget]) -> dict[str, str]:
    """On EVERY `prepare()` (not just a resumed one, so an in-process retry
    turn benefits too — cheap when nothing moved), bring every path the
    agent has NOT itself edited in the copy up to date with the CURRENT
    real tree, and rebase `known` to match.

    A/B/C staleness: copy mirrors real state A, the agent writes B, B
    applies back (and `apply_back`'s own compare-and-swap now rebases
    `known[rel]` to B for exactly the paths it touched) — but a path the
    agent never touched, that the REAL tree moves on to C independently (an
    operator commit, another process) BEFORE a `--adw-id` resume reuses this
    copy, would otherwise sit stale in the copy forever, and the persisted
    baseline would still claim A.

    A path the agent IS mid-editing (copy differs from `known`) is left
    completely alone here — refreshing it would destroy in-flight agent
    work. If the real tree has ALSO moved for that exact path, `known[rel]`
    is deliberately NOT rebased, so `apply_back`'s compare-and-swap later
    correctly reports a conflict instead of silently overwriting whichever
    side is applied last.

    `real_paths` explicitly excludes `session_prefix`, the same way
    `_copy_tree` does (see its own docstring) — found live while testing a
    resume interaction: a repo whose `.gitignore` does not cover
    `data_dir/sessions/` (every fixture in this suite) makes a harvested
    `context_handoff/` file show up in `git ls-files -co
    --exclude-standard` like any other untracked-not-ignored path. Without
    this exclusion, THIS function — not `_seed_handoff` — would "helpfully"
    copy that file back into the copy on every resumed `prepare()`,
    silently resurrecting a deletion `_seed_handoff`'s own, more careful,
    seed-record-aware logic had correctly chosen to respect.

    `_check_deadline()` on every path — this is a resumed `prepare()`'s own
    unbounded full-tree scan, the same shape the phase-budget interaction
    finding named for the initial-copy path in `_copy_tree`.

    A final `_check_deadline()` after the loop — see `_copy_tree`'s
    matching note for why a per-iteration check alone misses the last item.
    `git ls-files` (`_list_source_paths`) is now also bounded by `budget`.
    """
    copy_now = _walk_copy(layout.copy_root, layout.session_prefix, budget)
    session_prefix_parts = layout.session_prefix.parts
    real_paths = {p for p in _list_source_paths(layout.repo_root, budget)
                 if not _under(Path(p).parts, session_prefix_parts)}
    updated = dict(known)
    touched_by_agent = {p for p in copy_now if copy_now.get(p) != known.get(p, permissions.DELETED)}
    for rel in set(known) | real_paths:
        _check_deadline(budget)
        if rel in touched_by_agent:
            continue
        real_fp = _fp(layout.repo_root / rel) if rel in real_paths else permissions.DELETED
        if real_fp == known.get(rel, permissions.DELETED):
            continue
        dst = layout.copy_root / rel
        if rel in real_paths:
            _copy_one(layout.repo_root / rel, dst)
        elif dst.exists() or dst.is_symlink():
            dst.unlink()
        updated[rel] = real_fp
    _check_deadline(budget)
    return updated


def prepare(run, agent: AgentConfig, budget: Optional[Budget] = None) -> Isolation:
    """Create (or, for a resumed adw_id+agent, reuse and refresh) this
    agent's copy.

    `budget`: the phase's whole-budget deadline, established by
    `agents.execute()` BEFORE this is ever called — this function's own
    work (the initial full-tree copy, or a resumed copy's full-tree
    refresh) is exactly the unbounded scan the phase-budget interaction
    finding named as running outside the phase budget entirely. `None`
    (the default) means no deadline at all, so every direct call in this
    module's own test suite that predates the budget wiring is unaffected.
    Stored on the returned `Isolation` (`iso.budget`) so
    `sync()`/`finalize()`/`apply_back()`/`harvest_handoff()`/
    `harvest_runtime()` need no new parameter of their own.

    The git calls this makes — directly (`_git_init_baseline`) and through
    `_copy_tree`/`_refresh_untouched_paths` (`git ls-files`) — are also
    bounded by `budget`; see `_run_git`'s own docstring.
    """
    repo_root = Path(run.repo_root)
    root = _run_root(run)
    copy_root = root / agent.name
    baseline_path = root / f"{agent.name}.{_BASELINE_FILE}"
    handoff_seed_path = root / f"{agent.name}.{_HANDOFF_SEED_FILE}"

    context_handoff_rel = Path(os.path.relpath(Path(run.context_handoff_dir), repo_root))
    session_prefix = context_handoff_rel.parent
    runtime_rel = session_prefix / agent.name
    real_runtime_dir = repo_root / runtime_rel
    case_insensitive = _fs_is_case_insensitive(str(repo_root.resolve()))
    layout = _CopyLayout(repo_root=repo_root, copy_root=copy_root, session_prefix=session_prefix)

    if not copy_root.exists() or not baseline_path.exists():
        if copy_root.exists():
            shutil.rmtree(copy_root, onerror=_force_remove)
        copy_root.mkdir(parents=True, exist_ok=True)
        _copy_tree(layout, budget)
        _git_init_baseline(copy_root, budget)
        baseline = _walk_copy(copy_root, session_prefix, budget)
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        _save_baseline_dict(baseline_path, baseline)
    else:
        known = _load_baseline_dict(baseline_path)
        refreshed = _refresh_untouched_paths(layout, known, budget)
        if refreshed != known:
            _save_baseline_dict(baseline_path, refreshed)

    (copy_root / context_handoff_rel).mkdir(parents=True, exist_ok=True)
    (copy_root / runtime_rel).mkdir(parents=True, exist_ok=True)

    iso = Isolation(
        copy_root=copy_root,
        baseline_path=baseline_path,
        session_prefix=session_prefix,
        context_handoff_rel=context_handoff_rel,
        real_context_handoff_dir=Path(run.context_handoff_dir),
        runtime_rel=runtime_rel,
        real_runtime_dir=real_runtime_dir,
        handoff_seed_path=handoff_seed_path,
        case_insensitive=case_insensitive,
        budget=budget,
    )
    _seed_handoff(iso)
    return iso


def apply_back(run, agent: AgentConfig, iso: Isolation,
               require_scope: bool = False) -> ApplyResult:
    """Diff the copy against its pinned baseline, apply every PERMITTED (and,
    for a builder-class call, in-scope) changed path into the real worktree,
    and report the rest as rejected without touching the real tree for them
    at all.

    `permissions.permitted()` is the SAME predicate `permissions.enforce()`
    uses for a direct write today (`writes:` / `protected_files`) — reused,
    not reimplemented, so this module cannot silently drift from the rail it
    is meant to strengthen. `_case_insensitive_protection_bypass()` adds a
    second, case-folded pass on a case-insensitive real filesystem, so a
    case-only variant of a protected path cannot slip through purely
    because the string comparison is case-sensitive and the disk
    underneath is not — a case-only RENAME (`package.json` ->
    `PACKAGE.JSON`) then has BOTH halves rejected independently (the
    new-case add fails the bypass check, the old-case delete is directly
    protected), so the net effect on the real file is "untouched", the same
    outcome a dedicated rename-pairing detector would produce, without
    needing one.

    Compare-and-swap: before writing any path, the REAL file's CURRENT
    fingerprint must equal what this module last recorded for it in
    `baseline` (either the original copy-time snapshot, or whatever THIS
    function itself wrote there on a previous, successful apply —
    `baseline` is persisted back to disk after every call, so this holds
    across a `--adw-id` resume in a new process too). A mismatch means the
    real tree changed out from under the agent — an operator's own edit, a
    concurrent process — and is a REJECTION, never an overwrite: the
    operator's bytes are always what remains.

    Every symlink is additionally checked by `_symlink_target_is_within_repo`
    before being applied — see `_copy_one`'s docstring for the two-turn
    attack this closes.

    `require_scope`: for a `writes: [...]`-restricted agent (planner,
    documenter, scout, reviewer), that list IS its scope, and
    `permitted()` alone is the right and complete check. For a builder-class
    call (`call.output_type` carries `changed_files` — the exact set
    `gates.BUILDER_GATES` applies to), `permitted()` alone is NOT enough: an
    unrestricted builder (`writes: None`) passes it for almost any ordinary
    repo path, so an out-of-scope edit would apply-back straight into the
    REAL worktree before `claims_are_in_requested_scope`/`scoped_for_commit`
    ever got a chance to say no — this additionally requires
    `gates._in_scope(path, gates.requested_scope(run))`, the PINNED scope
    only, never a live plan.md read. No determinable scope for a
    builder-class call is a FAIL-CLOSED refusal (nothing applied), the same
    posture `gates.scoped_for_commit` already takes for the identical
    situation.
    """
    repo_root = Path(run.repo_root)
    baseline = _load_baseline_dict(iso.baseline_path)
    current = _walk_copy(iso.copy_root, iso.session_prefix, iso.budget)
    changed = permissions.changed_paths(baseline, current)
    tracked = permissions._tracked(run)

    # ONE more deadline check, right HERE — after the scan above (already
    # deadline-checked internally, per-path) but BEFORE a single real-tree
    # write happens below. `changed` is now a fixed, already-computed,
    # bounded set (only the paths that actually differ), so the loop below
    # is deliberately NOT checked per-iteration: once writing to the REAL
    # tree starts, it runs to completion rather than leaving the worktree
    # half-applied. See the module docstring's BUDGET section.
    _check_deadline(iso.budget)

    allow: set[str] | None = None
    if require_scope:
        allow = gates.requested_scope(run)

    applied: list[str] = []
    rejected: dict[str, str] = {}
    baseline_dirty = False
    for rel in changed:
        if not permissions.permitted(rel, agent, run.cfg, tracked) or (
                iso.case_insensitive and _case_insensitive_protection_bypass(rel, agent, run.cfg)):
            rejected[rel] = ("not permitted — protected (possibly via a "
                             "case-insensitive match), or outside this "
                             "agent's writes: list")
            continue
        if require_scope:
            if not allow:
                rejected[rel] = (
                    "no determinable scope for this build-class call — no "
                    "Where: line on the request and no planner phase pinned "
                    "a plan.md scope; refusing to apply ANY change rather "
                    "than guess (same fail-closed posture as "
                    "gates.scoped_for_commit)")
                continue
            if not gates._in_scope(rel, allow):
                allow_note = ", ".join(sorted(allow))
                rejected[rel] = f"not in requested scope ({allow_note})"
                continue

        src = iso.copy_root / rel
        dst = repo_root / rel

        if src.is_symlink() and not _symlink_target_is_within_repo(iso.copy_root, rel, repo_root):
            rejected[rel] = ("symlink target is absolute, escapes the repo, or points into "
                             ".git — refused")
            continue

        real_now = _fp(dst)
        expected = baseline.get(rel, permissions.DELETED)
        if real_now != expected:
            rejected[rel] = (
                f"real tree changed under the agent (expected {expected!r}, "
                f"found {real_now!r}) — refusing to overwrite; resolve the "
                f"conflict and it will be retried")
            continue

        if src.exists() or src.is_symlink():
            _copy_one(src, dst)
        elif dst.exists() or dst.is_symlink():
            dst.unlink()
        applied.append(rel)
        baseline[rel] = _fp(dst)
        baseline_dirty = True

    if baseline_dirty:
        _save_baseline_dict(iso.baseline_path, baseline)
    return ApplyResult(applied=applied, rejected=rejected)


def harvest_handoff(iso: Isolation) -> None:
    """Copy the copy's `context_handoff/` back to the REAL data_dir — but
    ONLY a path whose CURRENT fingerprint differs from what was SEEDED
    there (see `_seed_handoff`): a file this agent merely inherited from an
    earlier agent's phase, untouched, is never re-harvested, so it can
    never clobber that sibling's own concurrently-updated file. Session
    runtime, always allowed, never governed by `writes`.

    Every update to `iso.handoff_seed` here is also persisted to
    `iso.handoff_seed_path` — a file this agent harvests now must be
    recognized as "already accounted for" on this agent's NEXT `prepare()`
    call too (an in-process retry turn, or a resumed process), not just for
    the rest of this one.

    A final `_check_deadline(iso.budget)` after the loop — see
    `_copy_tree`'s matching note for why a per-iteration check alone misses
    the last item.
    """
    src = iso.copy_root / iso.context_handoff_rel
    if not src.is_dir():
        return
    dst = iso.real_context_handoff_dir
    dst.mkdir(parents=True, exist_ok=True)
    changed = False
    for path in src.rglob("*"):
        _check_deadline(iso.budget)
        if path.is_dir():
            continue
        rel = path.relative_to(src)
        rel_key = rel.as_posix()
        current_fp = _fp(path)
        if iso.handoff_seed.get(rel_key) == current_fp:
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        iso.handoff_seed[rel_key] = current_fp
        changed = True
    _check_deadline(iso.budget)
    if changed:
        _save_baseline_dict(iso.handoff_seed_path, iso.handoff_seed)


def harvest_runtime(iso: Isolation) -> None:
    """Copy the copy-side session runtime (pi's own `--session`/
    `--session-dir` bookkeeping, raw event output) back to the real
    `data_dir` so an operator can inspect it as before — the copy-side
    original is left in place, since pi itself needs it there to resume
    the session on the next turn/process. Simple mirror, no
    compare-and-swap: this is harness-owned tooling output, never
    something an operator hand-edits concurrently.

    A final `_check_deadline(iso.budget)` after the loop — see
    `_copy_tree`'s matching note for why a per-iteration check alone misses
    the last item.
    """
    src = iso.copy_root / iso.runtime_rel
    if not src.is_dir():
        return
    dst = iso.real_runtime_dir
    dst.mkdir(parents=True, exist_ok=True)
    for path in src.rglob("*"):
        _check_deadline(iso.budget)
        if path.is_dir():
            continue
        rel = path.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    _check_deadline(iso.budget)


def sync(run, agent: AgentConfig, iso: Isolation, require_scope: bool = False) -> ApplyResult:
    """Non-raising: apply whatever is currently permitted (and, for a
    builder-class call, in scope, and conflict-free — see `apply_back`'s
    docstring), harvest the handoff dir and the session runtime, and return
    the result for logging. Called after every agent turn so gates that
    read `run.repo_root` mid-phase see accurate state. `finalize()` is the
    only call that turns a persistent rejection into a failed phase.

    `iso.budget`, not a new parameter here — see `prepare()`'s own
    docstring and the module docstring's BUDGET section. A raised
    `agent_budget.PhaseTimeout` propagates out of this (not caught here,
    not `permissions.PermissionBreach`) straight past `agents.execute()`'s
    own try/except (which only catches `PermissionBreach`) to
    `Run.phase()`'s `except BaseException` handler — cleanup runs and the
    phase fails, the same as any other exception a phase call raises.
    """
    result = apply_back(run, agent, iso, require_scope=require_scope)
    harvest_handoff(iso)
    harvest_runtime(iso)
    return result


def finalize(run, agent: AgentConfig, iso: Isolation, require_scope: bool = False) -> list[str]:
    """The end-of-phase call: one more sync, then fail the phase (via the
    existing `permissions.PermissionBreach`/correction-retry machinery in
    `agents.execute`) if anything the agent wrote is still un-applied.

    `require_scope`: pass True for a builder-class call (`call.output_type`
    carries `changed_files`) so an out-of-scope edit is rejected here —
    never landing in the real tree for the quality/test phase to run
    against — rather than merely failing `claims_are_in_requested_scope`'s
    CLAIM check or `scoped_for_commit`'s COMMIT check afterward, both of
    which run too late to keep the real worktree clean.
    """
    result = sync(run, agent, iso, require_scope=require_scope)
    if result.rejected:
        scope = ("read-only" if agent.writes == []
                 else f"limited to {agent.writes}" if agent.writes
                 else f"barred from {run.cfg.defaults.protected_files}")
        detail = "\n".join(f"  - {p} — {reason}"
                           for p, reason in sorted(result.rejected.items()))
        raise permissions.PermissionBreach(
            f"{agent.name} is {scope} but its isolated copy still holds "
            f"{len(result.rejected)} unauthorized path(s), never applied to "
            f"the real repo:\n{detail}")
    return result.applied


def _force_remove(func, path, exc_info) -> None:
    """`shutil.rmtree` onerror hook: git marks some object files read-only,
    which raises PermissionError on Windows (and some POSIX configurations)
    before `ignore_errors` even gets a look. Clear the bit and retry once."""
    try:
        os.chmod(path, 0o700)
        func(path)
    except OSError:
        pass


def cleanup_run(run, ok: bool) -> None:
    """Sweep every agent's copy for this run. Idempotent — safe to call
    more than once (it no-ops once the directory is already gone), which is
    exactly what happens: it is called from `Run.finish()` (the normal end
    of a run), from `Run.phase()`'s own exception handler (a phase that
    raises past every ADW script's own error handling), and from an
    `atexit` hook registered in `session.ensure()` (the last-resort net for
    anything that escapes both of those). Kept on failure only when
    `SSSF_KEEP_ISOLATION_ON_FAILURE` is truthy, for post-mortem debugging;
    always removed on success.
    """
    base = _run_root(run)
    if not base.exists():
        return
    keep_on_failure = os.environ.get(KEEP_ON_FAILURE_ENV, "").strip().lower() not in ("", "0", "false")
    if ok or not keep_on_failure:
        shutil.rmtree(base, onerror=_force_remove)
