"""Low-level git operations for code phases. All low-level logic lives in adw_modules."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

# adws/adw_modules/ — fixed relative to this file regardless of process cwd.
_THIS_MODULE_DIR = Path(__file__).resolve().parent


def _hooks_override() -> list[str]:
    """`-c core.hooksPath=...`, pointed at the repo-committed `.githooks/`
    directory if it exists, else an empty scratch directory.

    Round-3 reviewer finding on PR #108, item 6: the harness's OWN
    write-side git invocations (`commit_paths`'s `add`/`commit` here;
    `permissions._roll_back`'s `checkout`, which carries its own copy of
    this same helper) must not execute whatever an agent may have planted
    in the real, agent-writable `.git/hooks/` — but `--no-verify` would
    ALSO skip real, repo-committed hooks in `.githooks/` (if this repo has
    one), which must still run. Redirecting `core.hooksPath` per
    invocation, rather than skipping hooks outright, keeps that distinction.

    Round-4 reviewer finding on PR #109, item 6: this used to resolve
    `.githooks` as `Path(".githooks")` — relative to the raw PROCESS cwd.
    Launched from a subdirectory (an ADW phase's working directory is not
    guaranteed to be the repo root), that silently missed a real,
    repo-committed `.githooks/` sitting at the actual root, falling through
    to the harmless-looking-but-wrong empty-scratch-dir branch instead —
    not a security hole (the empty dir still blocks the agent-planted real
    hook), but it silently skipped legitimate committed hooks too. `git
    rev-parse --show-toplevel` (deliberately WITHOUT `-C`, unlike this
    module's own `repo_root()`) resolves the toplevel of whatever repo the
    CURRENT process cwd is inside, walking up from wherever that is — the
    same property `assert_cwd_matches_repo` already relies on elsewhere in
    this file. Anchoring to `repo_root()` instead (this file's own module
    location) would resolve the wrong repo entirely for any test — or any
    future multi-repo host — where cwd is a DIFFERENT checkout than the one
    `adw_modules` happens to be vendored into.
    """
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    root = Path(result.stdout.strip()) if result.returncode == 0 else Path(".")
    committed = root / ".githooks"
    if committed.is_dir():
        return ["-c", f"core.hooksPath={committed}"]
    empty = Path(tempfile.gettempdir()) / "sssf-empty-hooks"
    empty.mkdir(exist_ok=True)
    return ["-c", f"core.hooksPath={empty}"]


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_at(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args],
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def current_branch() -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD")


def create_branch(name: str) -> str:
    _git("checkout", "-b", name)
    return name


def is_repo() -> bool:
    result = subprocess.run(["git", "rev-parse", "--git-dir"],
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    return result.returncode == 0


def repo_root() -> Path:
    """Absolute root of the codebase — where agents are spawned to work.

    Resolved from THIS MODULE's own location (`adws/adw_modules/`), never the
    process cwd. #53181: launching an ADW while cwd happened to be a different
    git repo (e.g. an unrelated checkout on the same machine) made that other
    repo the resolved root, and only a downstream config-validation failure
    kept agents from being spawned to operate on it. `git -C <script dir>
    rev-parse --show-toplevel` answers "what repo is this ADW part of", which
    is invariant to where the process was launched from — unlike bare
    `git rev-parse --show-toplevel`, which answers "what repo is the cwd
    part of".

    Falls back to the fixed `adw_modules/../..` layout when the codebase isn't
    a git repo at all yet (ADWs run fine in a non-git dir; only a commit phase
    requires one). Always absolute, so it is safe to hand to a subprocess
    regardless of where the ADW was launched from.
    """
    result = subprocess.run(["git", "-C", str(_THIS_MODULE_DIR), "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode == 0:
        return Path(result.stdout.strip()).resolve()
    return _THIS_MODULE_DIR.parent.parent.resolve()   # adw_modules -> adws -> repo root


def assert_cwd_matches_repo(repo_root_path: Path) -> None:
    """Refuse loudly, before any agent spawns, if cwd is inside a DIFFERENT repo.

    `repo_root()` above is now correct regardless of cwd, but most of the rest
    of the framework's own paths — the `--config` default, and the
    `prompt_engineering` / `data_dir` / `db` paths it points at — are still
    read relative to the process cwd, not repo_root. Until those are ported
    too, a cwd that resolves to some OTHER git repo is unsafe to proceed on
    quietly: it is exactly the shape of the #53181 incident, just caught
    earlier and louder instead of by luck.

    A cwd that is not inside any git repo is left alone (unchanged, pre-53181
    behavior) — that is the ordinary "launched from a non-git scratch dir"
    case, not a same-shape mismatch.
    """
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        return
    cwd_root = Path(result.stdout.strip()).resolve()
    if cwd_root != Path(repo_root_path).resolve():
        raise RuntimeError(
            f"refusing to start: this ADW belongs to {repo_root_path}, but the "
            f"process working directory is inside a different git repo "
            f"({cwd_root}). Launch it with cwd set to {repo_root_path} (or a "
            f"subdirectory of it) — config, prompt, and data paths are still "
            f"resolved relative to cwd and would silently target the wrong "
            f"repo otherwise.")


def _require_repo() -> None:
    if not is_repo():
        raise RuntimeError(
            "not a git repository — a commit phase needs one. Run `git init` in the "
            "repo root (and make a first commit) before running an ADW that commits.")


def commit_all(message: str) -> str:
    """Stage the ENTIRE working tree and commit it. Returns the new short sha.

    ⚠️  Prefer `commit_paths()`. This stages `git add -A`, so it commits every
    dirty path in the repo — including work the agents never touched. Live on
    2026-08-24 (adw_id f501b92a) that swept the operator's own uncommitted edits
    into the agent's commit, attributing them to the run. Kept for ADWs that
    genuinely mean "commit everything", and for a non-agent code phase where
    there is no attributed change-set to narrow to.
    """
    _require_repo()
    _git("add", "-A")
    if not _git("status", "--porcelain"):
        raise RuntimeError("nothing to commit — the preceding phases changed no files")
    _git("commit", "-m", message)
    return _git("rev-parse", "--short", "HEAD")


def commit_paths(message: str, paths: list[str]) -> str:
    """Stage ONLY `paths` and commit them. Returns the new short sha.

    `paths` is the run's agent-attributed change-set — what permissions.enforce
    observed each agent actually write, accumulated on the run as
    `agent_touched_paths`. Staging that set instead of the whole tree keeps a
    commit honest in both directions: unrelated dirty files stay out, and a
    builder that wrote nothing produces no commit at all rather than silently
    committing someone else's work under its message.

    Deletions are included — `git add --` records a removed path the same way it
    records a modified one, and a file the agent deleted is part of its change.

    Round-2 reviewer finding on PR #108 (index bypass): both the "nothing
    staged" check and the commit itself are now scoped with an explicit
    `-- <paths>` pathspec, not just the preceding `git add`. A bare
    `git commit -m message` (no pathspec) commits the WHOLE index — any path
    an agent `git add`-ed earlier in the phase for reasons unrelated to
    `paths` (e.g. a protected file it staged, then was told to leave alone,
    and appeared to comply by reverting only the WORKING TREE) rides along
    into the commit regardless of never having been in `paths` at all. A
    pathspec-scoped `git commit -- <paths>` commits (and, per git's own
    semantics for a pathspec-limited commit, re-stages from the working tree
    for) only the named paths, ignoring whatever else the index currently
    holds — the same property `git add -- <paths>` already gives the
    staging half, now given to the commit half too.
    """
    _require_repo()
    if not paths:
        raise RuntimeError(
            "nothing to commit — no agent phase in this run touched a tracked file. "
            "A build phase that changed nothing has not built anything; refusing to "
            "commit the working tree on its behalf.")
    hooks = _hooks_override()
    _git(*hooks, "add", "--", *paths)
    if not _git("diff", "--cached", "--name-only", "--", *paths):
        raise RuntimeError(
            f"nothing staged from the agent change-set ({len(paths)} path(s) claimed: "
            f"{', '.join(paths[:5])}). Every one is unchanged, ignored, or absent.")
    _git(*hooks, "commit", "-m", message, "--", *paths)
    return _git("rev-parse", "--short", "HEAD")


def changed_files() -> list[str]:
    out = _git("status", "--porcelain")
    return [line[3:] for line in out.splitlines() if line]


# ── diff plumbing (composed into a ChangeSet by documentation.py) ────────────

def ref_exists(ref: str) -> bool:
    """True when `ref` resolves to a commit. Never raises — this is a question."""
    result = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    return result.returncode == 0


def rev(ref: str = "HEAD") -> str:
    return _git("rev-parse", ref)


def short_sha(ref: str = "HEAD") -> str:
    return _git("rev-parse", "--short", ref)


def merge_base(ref: str, other: str = "HEAD") -> str:
    """The commit where `ref` and `other` diverged — the honest base of a branch.

    On the base branch itself this returns HEAD, which makes the diff exactly
    "what is not committed yet". Off it, the diff is the whole branch plus the
    working tree. One command covers both cases, so no ADW has to branch on it.
    """
    return _git("merge-base", ref, other)


def is_dirty() -> bool:
    return bool(_git("status", "--porcelain"))


def untracked_files() -> list[str]:
    out = _git("ls-files", "--others", "--exclude-standard")
    return [line for line in out.splitlines() if line]


def diff_files(base: str) -> list[str]:
    """Tracked files that differ between `base` and the working tree."""
    out = _git("diff", "--name-only", base)
    return [line for line in out.splitlines() if line]


def diff_stat(base: str) -> str:
    return _git("diff", "--stat", base)


def diff_counts(base: str) -> tuple[int, int]:
    """(insertions, deletions) across the diff. Binary files count as neither."""
    insertions = deletions = 0
    for line in _git("diff", "--numstat", base).splitlines():
        added, removed, *_ = line.split("\t")
        if added.isdigit():
            insertions += int(added)
        if removed.isdigit():
            deletions += int(removed)
    return insertions, deletions


def diff_text(base: str) -> str:
    return _git("diff", base)
