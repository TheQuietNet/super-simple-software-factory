"""What an agent may CHANGE, enforced in code after the fact.

`tools:` is a capability list, not a sandbox, and two holes make it
unenforceable on its own:

  * `bash` runs anything. A builder handed bash to run a test suite can also
    run `git checkout adws/` — which is not hypothetical: one did, discarding
    uncommitted changes to the very quality check it was about to be judged by.
  * `write` reaches any path, not just the one report file an agent was given
    it for. A reviewer configured with "no edit, so it cannot quietly fix"
    could still rewrite the code it was reviewing.

So permission is verified the way every other claim in this system is —
after the fact, against the repo itself. `snapshot()` fingerprints the working
tree's change-set before an agent runs; `enforce()` compares it afterwards and
fails the phase if the agent touched anything outside its allowlist.

Comparing change-sets, rather than watching for writes, is what catches the
`git checkout` case: a path that was modified before the agent ran and is clean
afterwards has been reverted, and a reversion is a modification. Appearing,
disappearing, and changing all count.

A breach is NOT a gate violation. Gates are for work an agent can be asked to
redo; a breach cannot be corrected by re-prompting, because the write already
happened. It aborts the phase and names every offending path.

Two keys drive it, both in sssf.config.yaml:
    defaults.protected_files   paths no agent may touch unless it names them itself
    agents[].writes      None = unrestricted · [] = read-only · [...] = only these

THREAT MODEL: this module defends against a careless or confused agent — one
that edits the wrong file, reverts an operator's WIP, or stages something it
shouldn't, without actively trying to defeat detection. A DELIBERATELY
adversarial agent with bash access to a real git checkout is only fully
contained by running it in an isolated copy with no `.git` write access and
diffing back from outside — see `adw_modules/isolation.py`. What's here
narrows the gap (pinned-HEAD diffing, control-plane fingerprinting, hooks-path
redirection) without closing it on its own.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .data_types import AgentConfig, SSSFConfig


class PermissionBreach(RuntimeError):
    """An agent modified a path it was not permitted to modify."""


def _git(args: list[str], cwd) -> str:
    """Run one git plumbing command; raise on a nonzero exit.

    This used to return `""` on failure, indistinguishable from "the command
    genuinely produced no output" — a failed `git diff`/`git ls-files` (git
    missing, repo corrupted, a transient lock) looked exactly like an EMPTY,
    CLEAN snapshot to every caller. `snapshot()`/`enforce()` would then compare
    two empty-looking states and report "nothing changed" — fail OPEN on the
    one class of error most likely to coincide with something actually being
    wrong. Every caller in this module wants information, never a "false means
    no" answer (unlike `git_helper.py`'s own `is_repo()`-style probes, which
    check `returncode` directly instead of going through a wrapper that
    raises), so raising here and letting it kill the phase (uncaught, all the
    way up through `agents.execute()`) is the fail-closed default this file's
    own docstring already promises everywhere else.
    """
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


DELETED = "absent"   # sentinel: truly nothing at this path — no file, symlink, or submodule dir
_HASH_CHUNK_BYTES = 1024 * 1024   # stream sha256 in 1 MiB chunks, never load a whole file at once
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"   # git's canonical empty-tree sha


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(path: Path) -> str:
    """Content fingerprint for one working-tree path — never two DIFFERENT
    real states collapsing to the same value, and never a stable
    placeholder for something this function could not actually read.

    A naive version follows symlinks (`Path.is_file()` resolves them) to hash
    their TARGET's bytes, and falls through to a flat `DELETED` sentinel for
    anything that isn't a regular file — a dangling symlink, a submodule
    directory, AND a genuine deletion would then fingerprint identically. Two
    DIFFERENT dangling symlinks (different missing targets), or a submodule
    retargeted to a different commit, would be indistinguishable from each
    other and from "nothing here" — an agent could retarget a broken symlink
    or bump a submodule pointer and `changed_paths()` would never see it,
    because both the before and after fingerprint were the same sentinel
    string.

    Fixed by fingerprinting each path's ACTUAL kind, checked in an order that
    never follows a link to answer the wrong question:
      - a symlink (`is_symlink()`, checked first — `is_dir()`/`is_file()`
        below would otherwise silently resolve through it) is fingerprinted
        by its OWN link text, not by hashing whatever it points at. Retarget
        it, even between two missing targets, and the fingerprint changes.
      - a directory (a submodule's checkout) is fingerprinted by the commit
        it's currently on (`git rev-parse HEAD` run INSIDE it).
      - a regular file is fingerprinted by the sha256 of its bytes, streamed
        in chunks rather than read whole.
      - anything genuinely absent gets `DELETED` — distinctly prefixed from
        every other branch's `"symlink:"`/`"submodule:"`/`"file:"` tag, so
        it can never collide with a real fingerprint either.
    An unreadable path (permission error, I/O error, an exotic type none of
    the above matches) is deliberately NOT caught here — a silent
    "unreadable" sentinel would make two different unreadable states
    indistinguishable too (fail open). It raises, uncaught, all the way up
    through `snapshot()` into a fail-closed phase error instead.

    Note on filters: this reads raw filesystem bytes directly
    (`Path.read_bytes`/`os.readlink`), never through `git show`/`git cat-file
    blob`/`git hash-object` — clean/smudge filters and `.gitattributes`
    textconv apply on git's own checkout/checkin path, not to an arbitrary
    process reading a file off disk, so there is no filter to bypass here in
    the first place. `--no-filters`-style precautions matter for the
    harness's WRITE-side git invocations instead — see
    `git_helper._hooks_override` and `_roll_back`'s checkout.
    """
    if path.is_symlink():
        return f"symlink:{os.readlink(path)}"
    if path.is_dir():
        return f"submodule:{_git(['rev-parse', 'HEAD'], path)}"
    if path.is_file():
        return f"file:{_sha256_file(path)}"
    return DELETED


def _index_shas(cwd) -> dict[str, str]:
    """Every currently-staged path's index blob sha, repo-relative.

    One `git ls-files -s` call for the whole index, rather than a
    `git rev-parse :path` subprocess per dirty file. Closes an index-bypass
    gap: `snapshot()` used to fingerprint the WORKING TREE only. An agent
    could `git add` a malicious edit to a pre-dirty protected path (staging
    it), then — in the retry after the resulting breach — revert only the
    WORKING TREE back to the baseline bytes. The working-tree-only
    fingerprint would then match the baseline again, `enforce()` would report
    no breach, and the STILL-STAGED malicious blob would ride into the next
    `git commit` untouched. Folding each path's index sha into its
    fingerprint (see `snapshot()`) closes that: the index and the working
    tree are now both part of "did this path change", not just the one an
    agent might think to revert.
    """
    shas: dict[str, str] = {}
    for line in _git(["ls-files", "-s"], cwd).splitlines():
        if not line:
            continue
        meta, _, path = line.partition("\t")
        fields = meta.split()
        if len(fields) >= 2 and path:
            shas[path.strip()] = fields[1]
    return shas


def _head_state(cwd) -> str:
    """The current branch's commit sha, or the canonical empty-tree sha if
    the repo has no commits yet (a fresh `git init` — a literal `git diff
    HEAD` raises on an unborn repo, since there is no HEAD to resolve;
    diffing against the empty tree is the correct "everything here is new"
    comparison instead).

    Used as BOTH the diff base for `snapshot()` AND the ref-movement
    fingerprint for `enforce()`: an agent that commits during its own phase
    (`git add && git commit`) makes the tree clean again relative to the NEW
    head, erasing the evidence a live-`HEAD`-relative diff would have shown —
    comparing against this value, PINNED once at phase start, instead of the
    literal string `"HEAD"` recomputed live each time, is what catches it.
    Pinning also catches the edge case where the agent's commit happens to
    put the tree back to looking identical to the pinned base (an empty or
    self-reverting commit): the diff would show nothing, but this value
    before vs. after still differs, because a NEW commit exists either way.
    """
    result = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"],
                            cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    return result.stdout.strip() if result.returncode == 0 else _EMPTY_TREE


def phase_base(run) -> str:
    """Pin `_head_state()` once, at phase start. See its docstring."""
    return _head_state(run.repo_root)


def symbolic_ref_state(cwd) -> str:
    """The branch HEAD currently points at (short form), or the fixed
    `"DETACHED"` marker when HEAD is not attached to a branch at all.

    `_head_state`'s sha-only pin catches `git commit`/`git reset --soft
    <other>` (the sha itself moves), but not `git switch other-branch` when
    `other-branch` happens to point at the exact SAME commit as the branch
    the phase started on — the sha comparison in `enforce()` sees no
    difference at all, even though HEAD is now attached to a different ref
    underneath the agent. Paired with the sha in `enforce()`'s own
    ref-movement check (see its docstring for `ref_before`), rather than
    folded into `_head_state`/`phase_base` itself: that value is also used
    directly as a git REVISION for `snapshot()`'s `git diff <base>` calls,
    and a composite like `<sha>:<branch>` is not a revision git understands.
    """
    result = subprocess.run(["git", "symbolic-ref", "-q", "--short", "HEAD"],
                            cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    return result.stdout.strip() if result.returncode == 0 else "DETACHED"


def _exists_at(base: str, path: str, cwd) -> bool:
    """Whether `path` exists in the pinned base commit — never resolved
    against the empty tree (nothing exists there, by definition; no need to
    ask git, and `git cat-file` support for that exact hash without the
    object being in this repo's odb is not something to depend on)."""
    if base == _EMPTY_TREE:
        return False
    result = subprocess.run(["git", "cat-file", "-e", f"{base}:{path}"],
                            cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    return result.returncode == 0


_GIT_META_SUBDIRS = ("info", "hooks")


def _resolve_git_dirs(repo_root) -> tuple[Path, Path]:
    """(git_dir, common_dir), both absolute.

    `control_plane_snapshot` must not assume `<repo_root>/.git` is a
    DIRECTORY holding config/hooks/info directly — that is only true for a
    normal checkout. Every SSSF worktree this harness might run in (created
    by `git worktree add`) has `.git` as a FILE containing `gitdir: <path>`
    instead — `<repo_root>/.git/config` then does not exist, `config.exists()`
    is False in BOTH the before and after snapshot, and `.git/config`/
    `.git/hooks`/`.git/info` tampering goes completely undetected in the one
    shape a worktree-based deployment actually uses.

    `git rev-parse --git-dir` resolves the WORKTREE-specific git directory
    (this worktree's own HEAD/index/etc — not fingerprinted here, out of
    scope for this fix); `--git-common-dir` resolves the directory a linked
    worktree and its main checkout both share, which is where `config`,
    `hooks/`, and `info/` actually live regardless of which worktree you're
    standing in. For an ordinary (non-worktree) checkout the two are
    identical. Both commands can return a relative path (plain `.git`, most
    commonly), so each is re-anchored to `repo_root` when not already
    absolute, then `.resolve()`d to collapse the `..` segments a linked
    worktree's `commondir` file typically introduces.
    """
    git_dir = Path(_git(["rev-parse", "--git-dir"], repo_root).strip())
    common_dir = Path(_git(["rev-parse", "--git-common-dir"], repo_root).strip())
    if not git_dir.is_absolute():
        git_dir = Path(repo_root) / git_dir
    if not common_dir.is_absolute():
        common_dir = Path(repo_root) / common_dir
    return git_dir.resolve(), common_dir.resolve()


def _index_flags(cwd) -> dict[str, str]:
    """Per-path assume-unchanged/skip-worktree flag state, keyed by path —
    deliberately NOT a hash of `git ls-files -v`'s whole output.

    Hashing the ENTIRE `ls-files -v` listing as one unit would mean ANY
    index-membership change at all — including a perfectly legitimate
    `git add` of an in-scope file the agent was told to write — changes that
    hash, tripping the control-plane's automatic, non-correctable breach
    ahead of the ordinary per-path `writes`/`protected_files` check in
    `enforce()`, and ahead of the staged-protected-file rollback too.
    `ls-files -v` lowercases a path's tag letter when its assume-unchanged
    bit is set, and uses `S` for skip-worktree — those two bits are the only
    thing this needs to catch (an agent hiding its own tracks from a later
    `git status`/`git diff` scan), so only paths carrying one of them are
    recorded at all. A path with neither flag never appears here, so adding,
    editing, or removing an ordinary tracked file — however that changes
    `ls-files -v`'s raw text — changes nothing this dict reports.
    """
    flags: dict[str, str] = {}
    for line in _git(["ls-files", "-v"], cwd).splitlines():
        if not line:
            continue
        tag, _, path = line.partition(" ")
        if tag and (tag.islower() or tag == "S"):
            flags[path.strip()] = tag
    return flags


def control_plane_snapshot(run) -> dict[str, str]:
    """Fingerprint git's own control plane — `.git/config`, `.git/info/*`
    (exclude, attributes), `.git/hooks/*` — and the index's per-path
    assume-unchanged/skip-worktree flags.

    NEVER governed by `writes`/`protected_files` — an agent has no
    legitimate reason to touch any of these, ever, regardless of roster
    config, so `enforce()` treats any difference here as an automatic,
    non-correctable breach (see its own body), ahead of the ordinary
    per-path allowlist check.

    Deliberately kept OUT of `snapshot()`'s own returned dict, even though
    both are "a before/after fingerprint dict `changed_paths` can diff" in
    shape: `snapshot()`'s result also becomes `run.phase_baseline_dirty`,
    which `gates.claims_are_in_requested_scope` diffs against its OWN fresh
    `snapshot(run)` call independent of any phase-start pin. Folding
    control-plane keys in there would make them show up as "touched" any
    time that gate's baseline was empty/unset (every test that does not wire
    up `phase_baseline_dirty`, and any gate check that runs before
    `enforce()` resolves a real one) — a false positive in a QUALITY gate for
    conditions `enforce()` below already handles authoritatively as a
    SECURITY breach. Kept as a wholly separate snapshot instead, diffed
    only here.

    `.git/config`/`.git/info`/`.git/hooks` are resolved via
    `_resolve_git_dirs`'s COMMON dir, not a naive `<repo_root>/.git`, so this
    is correct from inside a linked worktree too — see that function's
    docstring. The index-flag fingerprint is per-path (`_index_flags`), not a
    hash of the whole `ls-files -v` listing — see that function's docstring.

    The common dir is not the WHOLE story for a linked worktree. With
    `extensions.worktreeConfig=true` (an agent could set this itself, since
    `.git/config` is only checked for TAMPER, not for this one specific
    value), `git config --worktree` writes to `config.worktree` under the
    worktree's OWN `$GIT_DIR` — the one `_resolve_git_dirs` calls `git_dir`,
    not `common_dir` — entirely invisible to a common-dir-only fingerprint.
    Per-worktree `info/` (`sparse-checkout`, etc — distinct from the common
    dir's `info/exclude`/`attributes` already covered above) lives there too.
    Fingerprinted under both dirs is unavoidable overhead here: for an
    ORDINARY (non-worktree) checkout `git_dir == common_dir`, so
    `config.worktree` is simply `DELETED` (git never writes it there) and the
    `git_dir != common_dir` guard below skips the second `info/` walk
    entirely, since it would just re-fingerprint the exact same files the
    common-dir loop above already covered under a different key.
    """
    fp: dict[str, str] = {}
    git_dir, common_dir = _resolve_git_dirs(run.repo_root)
    config = common_dir / "config"
    fp["meta:.git/config"] = _fingerprint(config) if config.exists() else DELETED
    for sub in _GIT_META_SUBDIRS:
        base = common_dir / sub
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*")):
            if f.is_file() or f.is_symlink():
                rel = f.relative_to(common_dir).as_posix()
                fp[f"meta:.git/{rel}"] = _fingerprint(f)
    config_worktree = git_dir / "config.worktree"
    fp["meta:worktree/config.worktree"] = (
        _fingerprint(config_worktree) if config_worktree.exists() else DELETED)
    if git_dir != common_dir:
        info_dir = git_dir / "info"
        if info_dir.is_dir():
            for f in sorted(info_dir.rglob("*")):
                if f.is_file() or f.is_symlink():
                    rel = f.relative_to(git_dir).as_posix()
                    fp[f"meta:worktree/{rel}"] = _fingerprint(f)
    for path, tag in sorted(_index_flags(run.repo_root).items()):
        fp[f"meta:index-flag:{path}"] = tag
    return fp


def assert_no_dirty_submodules(run) -> None:
    """Refuse to even START an agent phase if any submodule is out of sync.

    `_fingerprint()`'s submodule branch only records the commit a submodule
    is CHECKED OUT to — it says nothing about uncommitted changes inside the
    submodule's OWN working tree, which this module has no machinery to diff
    at all. Rather than pretend to cover that ground, a dirty submodule at
    phase start is refused outright.

    `git status --porcelain=v2`'s 4-character `<sub>` field is the one git
    command that reports all three kinds of submodule drift independently:
    `S<c><m><u>`, where `c` = commit differs from the index, `m` = the
    submodule's own tracked files are modified, `u` = the submodule has
    untracked content. Any of the three being set (i.e. the field is not
    exactly `S...`) is refused — not just commit drift (the leading
    `+`/`-`/`U` marker `git submodule status --recursive` would give, which
    only reports commit drift and prints no marker at all — a leading space,
    identical to a genuinely clean submodule — for an uncommitted edit to a
    tracked file inside an otherwise-in-sync submodule).

    A repo with no submodules configured is a no-op here.

    `--ignore-submodules=none` forces full reporting regardless of a
    `submodule.<name>.ignore` config (`dirty`/`all`) an agent (or an
    already-committed `.gitmodules`) could set to make ordinary status output
    SUPPRESS a dirty submodule's entry entirely, silently defeating this
    whole check — the same way `-c core.hooksPath=...` forces a specific
    value regardless of what else is configured elsewhere.

    An UNMERGED submodule gitlink — a real merge conflict on the submodule's
    recorded commit — reports as a `u ` (not `1 `/`2 `) record. That is the
    same class of "cannot safely reason about this submodule's state" this
    function already refuses on for a stage-inconsistent `git submodule
    status` (the old `U` marker); an unmerged gitlink is refused
    UNCONDITIONALLY (regardless of its own commit/modified/untracked
    sub-flags — there is no single resolved state to compare them against
    while unmerged), whereas an ordinary (`1 `/`2 `) submodule record is
    refused only when one of those flags is actually set.
    """
    dirty = []
    for line in _git(["status", "--porcelain=v2", "--ignore-submodules=none"],
                     run.repo_root).splitlines():
        if not (line.startswith("1 ") or line.startswith("2 ") or line.startswith("u ")):
            continue                       # "? " (untracked) / "! " (ignored) lines
        fields = line.split(" ")
        if len(fields) < 3:
            continue
        sub = fields[2]
        if not sub.startswith("S"):
            continue                       # not a submodule gitlink at all
        if line.startswith("u ") or sub[1:] != "...":
            dirty.append(line)
    if dirty:
        detail = "\n".join(f"  - {line.strip()}" for line in dirty)
        raise PermissionBreach(
            f"refusing to start an agent phase: {len(dirty)} submodule(s) "
            f"have a commit, tracked-file, or untracked-content difference "
            f"from their pinned state, and this harness cannot safely "
            f"fingerprint uncommitted changes inside a submodule's own "
            f"working tree:\n{detail}")


def snapshot(run, base: str | None = None) -> dict[str, str]:
    """Fingerprint every path the working tree OR the index currently
    differs on (from `base`), BY CONTENT.

    Repo paths only — see `control_plane_snapshot` for git's own
    `.git/config`/`.git/info`/`.git/hooks`/index-flags fingerprint, kept
    deliberately separate (its own docstring explains why: folding it in
    here would leak into `run.phase_baseline_dirty`, which
    `gates.claims_are_in_requested_scope` also diffs against, unrelated to
    `enforce()`'s security check).

    `git diff HEAD --numstat` fingerprints a SHAPE (insertions, deletions),
    not content — two different one-line edits to the same file hash
    identically, so an agent's own edit to an already-dirty file could be
    invisible to `enforce()`'s before/after diff and so to write-boundary
    attribution entirely. Every dirty tracked path and every untracked path
    is now fingerprinted by `_fingerprint()` (content, by kind) instead.

    `git diff HEAD` alone reflects the WORKING TREE against HEAD and goes
    blind the moment an agent reverts just the working tree back to baseline
    after staging a malicious edit with `git add` — the INDEX still differs
    from HEAD, but this diff no longer sees it, since it compares
    tree-vs-worktree, not tree-vs-index. `git diff --cached` (index vs
    `base`) is unioned in alongside it so a staged-only difference is
    enumerated too, and each path's fingerprint below also folds in its
    current index blob sha (via `_index_shas`) — changing EITHER side changes
    the fingerprint.

    `base`: the pinned commit/empty-tree sha to diff against — see
    `phase_base`/`_head_state`. Defaults to a freshly-resolved live HEAD when
    not given, for callers (tests, ad hoc inspection) that do not care about
    pinning across a whole phase; `agents.execute` always passes an explicit,
    once-resolved value for both its `before` and `after` snapshots.

    Deletions and renames still register: `--no-renames` makes a rename show
    as a delete of the old path plus an add of the new one, each its own
    entry here — deliberately explicit rather than left to whatever
    `diff.renames` a given machine's gitconfig happens to default to, so the
    guarantee holds the same way in every environment, not just this one's.

    Only dirty/untracked paths are ever hashed, never the whole tree —
    `enforce()` calls this after every single send in a phase, so its cost
    must scale with what changed, not with repo size. Gitignored paths never
    appear, which is why the session runtime under `data_dir` — where
    handoff files legitimately land — needs no special case.
    """
    base = base if base is not None else _head_state(run.repo_root)
    fingerprints: dict[str, str] = {}
    repo_root = Path(run.repo_root)
    index_shas = _index_shas(run.repo_root)

    dirty_paths: set[str] = set()
    for line in _git(["diff", base, "--name-only", "--no-renames"], run.repo_root).splitlines():
        if line.strip():
            dirty_paths.add(line.strip())
    for line in _git(["diff", "--cached", base, "--name-only", "--no-renames"],
                     run.repo_root).splitlines():
        if line.strip():
            dirty_paths.add(line.strip())

    for path in dirty_paths:
        content = _fingerprint(repo_root / path)
        fingerprints[path] = f"{content}:index={index_shas.get(path, 'none')}"
    for line in _git(["ls-files", "--others", "--exclude-standard"],
                     run.repo_root).splitlines():
        path = line.strip()
        if path:
            content = _fingerprint(repo_root / path)
            fingerprints[path] = f"{content}:index={index_shas.get(path, 'none')}"

    return fingerprints


def changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Every path whose state differs — appeared, vanished, or was rewritten."""
    return sorted({p for p in set(before) | set(after)
                   if before.get(p) != after.get(p)})


def _glob(pattern: str) -> re.Pattern:
    """Translate a pattern, with `*` stopping at a path separator.

    fnmatch would let `*` cross `/`, which quietly widens every pattern:
    `adws/adw_*.py` would match `adws/adw_data/sessions/x/y.py` as well as the
    ADW scripts it means. `**` is the way to say "cross directories".

    `**/` matches ZERO or more leading directories, the way git and every other
    glob dialect read it. Translating it to `.*/` — which was the first version
    — silently requires at least one directory, so a roster pattern like
    `**/settings*.json` would cover `app/settings.json` but NOT a
    `settings.json` at the repo root — the Tier-C guard for exactly the file
    most likely to sit at the root.
    """
    out, i = [], 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")             # zero OR more leading directories
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out))


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):                      # directory prefix
        return path.startswith(pattern)
    if "*" in pattern or "?" in pattern:
        return _glob(pattern).fullmatch(path) is not None
    return path == pattern


def always_writable(cfg: SSSFConfig) -> list[str]:
    """The session runtime, which EVERY agent must be able to write.

    `context_handoff/` is the one place agents hand work to each other, and an
    agent's own prompts, raw_output.jsonl, and envelope.json land beside it.
    Scout writes its findings there, the reviewer its review, the planner its
    plan — a read-only agent is read-only with respect to the REPO, never with
    respect to its own report.

    This is granted from `data_dir` rather than left to .gitignore. The runtime
    is normally ignored, so it never even appears in a snapshot — but an agent's
    ability to record its work must not hang on a gitignore entry that someone
    can delete or that a changed `data_dir` can outgrow.
    """
    return [cfg.defaults.data_dir.rstrip("/") + "/"]


def _tracked(run) -> frozenset[str]:
    """Every path git tracks, repo-relative — `-z` so odd names are not quoted."""
    listing = _git(["ls-files", "-z"], run.repo_root)
    return frozenset(p.strip().replace("\\", "/") for p in listing.split("\0") if p.strip())


def permitted(path: str, agent: AgentConfig, cfg: SSSFConfig,
              tracked: frozenset[str] = frozenset()) -> bool:
    """The agent's own list, then what is protected, then the session runtime.

    Order is the whole security property here, and the first version had it
    backwards. `always_writable` was consulted FIRST, so a blanket `data_dir/`
    grant could outrank `protected_files` — and a roster's `data_dir` (e.g.
    `adws/adw_data`) can itself hold TRACKED files: every agent's `system.md`
    and `user.md`, plus harness extensions. The effect was that any agent,
    including a `writes: []` scout or reviewer, could rewrite the builder's
    system prompt and no breach would be raised. The module's whole premise is
    that an agent must not be able to edit the machinery that grades it; the
    prompts ARE that machinery, and they were the one part left open.

    So: protection outranks every blanket grant. The `data_dir` grant survives
    for what it was actually for — an agent's own report, envelope and raw
    output, which are UNTRACKED by design (see `always_writable`). A tracked
    file that happens to sit under `data_dir` is repo content, and stays
    governed by `writes` like any other repo content. `tracked` defaults to
    empty so a caller that cannot cheaply supply it degrades to the pre-existing
    prefix behaviour rather than failing open on a name it cannot classify.
    """
    if any(_matches(path, p) for p in (agent.writes or [])):
        return True                      # naming a path is what unlocks a protected one
    if any(_matches(path, p) for p in cfg.defaults.protected_files):
        return False
    if any(_matches(path, p) for p in always_writable(cfg)) and path not in tracked:
        return True
    return agent.writes is None          # None = unrestricted, [] = no repo writes


def _roll_back(run, path: str, before: dict[str, str], after: dict[str, str],
               tracked: frozenset[str] = frozenset(), base: str | None = None) -> str:
    """Undo one unauthorized change. Returns a word describing what happened.

    Only changes the agent INTRODUCED are undone. A path that was already dirty
    when the agent started is left exactly as it is: the operator had
    uncommitted work there, and discarding it to tidy up would be the same harm
    this module exists to prevent, committed by the cleanup instead of the agent.

    This is the flip side of `snapshot()`'s content-hash fingerprint. Whatever
    content an agent adds to a path that was ALREADY dirty and
    protected/unauthorized is left exactly here, mixed with the operator's own
    pre-existing edit — indistinguishable now that both are hashed the same
    way, and it was never distinguishable before either. `snapshot()` only
    records a hash, not the bytes behind it, so there is no pre-phase content
    to restore TO without discarding the operator's own work (`git checkout
    --` would reset to HEAD, which is worse, not better — it destroys the
    operator's legitimate edit along with the agent's). Genuine pre-phase-
    content restoration for that path would need `snapshot()` to keep a copy
    of the bytes, not just a hash — out of scope here.
    """
    if path in before:
        # Already dirty beforehand. If it is gone from the diff now, the agent
        # reverted an engineer's uncommitted work and the content is not ours
        # to reconstruct — say so loudly rather than pretend it was handled.
        return "REVERTED-BY-AGENT (uncommitted work lost, cannot restore)" \
            if path not in after else "left as-is (was already modified)"
    # This used to be `if path not in tracked`, where `tracked` is
    # `git ls-files` — the INDEX's CURRENT state. A brand-new file the agent
    # both created AND `git add`-ed is already "tracked" by that definition,
    # so it fell through to the `git checkout HEAD -- path` branch below,
    # which FAILS outright for a path with no version in HEAD at all — the
    # file and its index entry both survived, reported as "could not roll
    # back" only by accident of the checkout's own exit code. Checked against
    # the PINNED BASE COMMIT instead (did this path exist there, regardless
    # of what the index looks like right now): if not, there is no HEAD
    # version to check out either way, so remove both the index entry (a
    # harmless no-op via --ignore-unmatch if it was never staged) and the
    # file.
    exists_at_base = _exists_at(base, path, run.repo_root) if base is not None \
        else path in tracked   # no pinned base given (e.g. a direct test call): old behavior
    if not exists_at_base:
        # `git rm --cached` (no `--force`) refuses to unstage a path whose
        # staged content matches NEITHER HEAD's version (there isn't one —
        # the path doesn't exist at base) NOR the current working-tree
        # content — exactly the shape of an agent staging a new protected
        # file, then further editing the working copy without re-staging. A
        # discarded refusal here would still unlink the file from disk and
        # unconditionally report "deleted" while the malicious blob stayed
        # sitting in the index, untouched, ready to ride into the next
        # `commit_paths` call. `--force` makes the unstage succeed
        # regardless of that up-to-date check (this IS an unauthorized
        # write; there is nothing to protect by refusing), and the exit code
        # is checked — a genuine failure (permissions, a lock, something
        # unanticipated) is reported as a failed rollback, never silently
        # treated as success while the index entry survives.
        result = subprocess.run(
            ["git", "rm", "--cached", "--force", "--quiet", "--ignore-unmatch", "--", path],
            cwd=run.repo_root, capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        if result.returncode != 0:
            return f"could not unstage ({result.stderr.strip()})"
        try:
            (Path(run.repo_root) / path).unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            return f"could not delete ({error})"
        return "deleted"
    # HEAD, not bare `--`: `git checkout -- <path>` restores the WORKING
    # TREE from the INDEX, not from HEAD. If the agent staged its edit with
    # `git add` before this rollback runs, the index already holds the
    # malicious blob, so bare `git checkout -- path` is a no-op that reports
    # "rolled back" while leaving the malicious content in place in BOTH the
    # index and the working tree. `git checkout HEAD -- path` resets both to
    # HEAD's version explicitly, regardless of what the index currently
    # holds.
    #
    # `-c core.hooksPath=...` so this write-side invocation cannot trigger
    # whatever an agent may have planted in the real, untracked
    # `.git/hooks/` — repo-committed hooks in `.githooks/` (if the repo has
    # one) still run normally.
    result = subprocess.run(
        ["git", *_hooks_override(run.repo_root), "checkout", "HEAD", "--", path],
        cwd=run.repo_root, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    return "rolled back" if result.returncode == 0 else "could not roll back"


def _hooks_override(repo_root) -> list[str]:
    """`-c core.hooksPath=...`, pointed at the repo-committed `.githooks/`
    directory if the repo has one, else an empty scratch directory.

    The harness's OWN write-side git invocations (this module's `git
    checkout` rollback; `git_helper.commit_paths`'s `add`/`commit`) must not
    execute whatever an agent may have planted in the real, agent-writable
    `.git/hooks/` — but `--no-verify` would ALSO skip real, repo-committed
    hooks in `.githooks/`, which must still run. Redirecting `core.hooksPath`
    per invocation, rather than skipping hooks outright, keeps that
    distinction — and does not depend on the PERSISTENT `core.hooksPath`
    config staying untampered for the duration of a run: `.git/config`
    itself is one of the control-plane paths `control_plane_snapshot`
    watches, so an attempt to repoint it back to `.git/hooks` is caught as
    its own non-correctable breach independently of this.

    Read-side calls (snapshot's diff/ls-files/cat-file) are NOT given this
    override: none of them execute hooks (hooks fire on commit/checkout/
    merge/push, not on read-only plumbing), and `_fingerprint()` never
    reads content through git in the first place (see its own docstring on
    filters) — there is nothing for a hooks-path override to protect there.
    """
    committed = Path(repo_root) / ".githooks"
    if committed.is_dir():
        return ["-c", f"core.hooksPath={committed}"]
    empty = Path(tempfile.gettempdir()) / "sssf-empty-hooks"
    empty.mkdir(exist_ok=True)
    return ["-c", f"core.hooksPath={empty}"]


def enforce(run, phase, agent: AgentConfig, before: dict[str, str],
           base: str | None = None, meta_before: dict[str, str] | None = None,
           ref_before: str | None = None) -> list[str]:
    """Compare the tree against `before`; undo and raise if the agent overstepped.

    Returns the paths it legitimately changed, so the trace records what an
    agent actually touched rather than only what it claimed in its envelope.

    Detection alone would leave the repo holding the unauthorized change while
    reporting a failure, so anything the agent introduced outside its allowlist
    is rolled back before the phase dies. What it cannot undo, it names.

    `base`: the SAME pinned sha `before` was diffed against (see
    `phase_base`). Checked against the CURRENT `_head_state` first, ahead of
    everything else: if they differ, the branch moved during this agent's own
    phase — a commit, a reset, a checkout of another ref — and that is ALWAYS
    a non-correctable breach, regardless of what individual file diffs would
    or would not show (an agent's commit can make the tree look clean again
    relative to the NEW head, erasing the working-tree evidence entirely;
    this catches it even then). Only the harness's OWN code/commit phases
    move HEAD legitimately, and those call `git_helper.commit_paths`
    directly — `enforce()` is only ever reached from an agent phase in
    `agents.execute`, so this check is correctly scoped by construction, not
    by a special case here.

    `meta_before`: a `control_plane_snapshot` taken at the SAME moment as
    `before`, if the caller has one. Diffed against a fresh
    `control_plane_snapshot(run)` here; any difference is an automatic,
    non-correctable breach — never governed by writes/protected_files, and
    never something this module knows how to safely roll back (there is no
    single "path" semantics for "undo a hooks/config/index-flag change").
    Left `None` (the default) SKIPS this check entirely, rather than
    comparing against an empty dict — every current control-plane
    fingerprint would otherwise look "touched" relative to nothing, which is
    right for `agents.execute` (which always supplies a real one) and wrong
    for every other/test caller that does not.

    `ref_before`: a `symbolic_ref_state(run.repo_root)` taken at the SAME
    moment as `base`, if the caller has one. `git switch other-branch` to a
    branch pointing at the exact same commit as `base` leaves the sha check
    above silent — only the symbolic ref changed. Checked right alongside
    the sha, same non-correctable treatment, same `None`-skips-the-check
    default for backward compatibility with every caller that does not
    supply it.
    """
    base = base if base is not None else _head_state(run.repo_root)
    current = _head_state(run.repo_root)
    if current != base:
        raise PermissionBreach(
            f"{agent.name} moved HEAD during its own phase (pinned at "
            f"{base}, now {current}) — only the harness's own commit "
            f"phases may create commits or move a branch; this cannot be "
            f"corrected.")

    if ref_before is not None:
        ref_current = symbolic_ref_state(run.repo_root)
        if ref_current != ref_before:
            raise PermissionBreach(
                f"{agent.name} moved HEAD to a different branch during its "
                f"own phase (pinned ref {ref_before!r}, now {ref_current!r}) "
                f"— a branch switch to a same-commit ref is still "
                f"unauthorized ref movement; this cannot be corrected.")

    if meta_before is not None:
        meta_touched = changed_paths(meta_before, control_plane_snapshot(run))
        if meta_touched:
            detail = "\n".join(f"  - {p}" for p in meta_touched)
            raise PermissionBreach(
                f"{agent.name} touched git's own control plane (hooks, "
                f"config, info, or index flags) — never permitted, "
                f"regardless of writes/protected_files, and not something "
                f"this harness can safely undo:\n{detail}")

    after = snapshot(run, base)
    touched = changed_paths(before, after)
    tracked = _tracked(run)              # resolved once; `permitted`/`_roll_back` reuse it
    breaches = [p for p in touched if not permitted(p, agent, run.cfg, tracked)]
    if not breaches:
        return touched

    outcomes = {p: _roll_back(run, p, before, after, tracked, base) for p in breaches}
    scope = ("read-only" if agent.writes == []
             else f"limited to {agent.writes}" if agent.writes
             else f"barred from {run.cfg.defaults.protected_files}")
    detail = "\n".join(f"  - {p} — {outcome}" for p, outcome in outcomes.items())
    raise PermissionBreach(
        f"{agent.name} is {scope} but modified {len(breaches)} path(s):\n{detail}")
