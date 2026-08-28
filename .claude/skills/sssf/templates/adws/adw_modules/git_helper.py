"""Low-level git operations for code phases. All low-level logic lives in adw_modules."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True)
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
                            capture_output=True, text=True)
    return result.returncode == 0


def repo_root() -> Path:
    """Absolute root of the codebase — where agents are spawned to work.

    The git toplevel when there is one, else the process cwd (ADWs run fine in a
    non-git dir; only a commit phase requires a repo). Always absolute, so it is
    safe to hand to a subprocess regardless of where the ADW was launched from.
    """
    if is_repo():
        return Path(_git("rev-parse", "--show-toplevel")).resolve()
    return Path.cwd().resolve()


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
    """
    _require_repo()
    if not paths:
        raise RuntimeError(
            "nothing to commit — no agent phase in this run touched a tracked file. "
            "A build phase that changed nothing has not built anything; refusing to "
            "commit the working tree on its behalf.")
    _git("add", "--", *paths)
    if not _git("diff", "--cached", "--name-only"):
        raise RuntimeError(
            f"nothing staged from the agent change-set ({len(paths)} path(s) claimed: "
            f"{', '.join(paths[:5])}). Every one is unchanged, ignored, or absent.")
    _git("commit", "-m", message)
    return _git("rev-parse", "--short", "HEAD")


def changed_files() -> list[str]:
    out = _git("status", "--porcelain")
    return [line[3:] for line in out.splitlines() if line]


# ── diff plumbing (composed into a ChangeSet by documentation.py) ────────────

def ref_exists(ref: str) -> bool:
    """True when `ref` resolves to a commit. Never raises — this is a question."""
    result = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                            capture_output=True, text=True)
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
