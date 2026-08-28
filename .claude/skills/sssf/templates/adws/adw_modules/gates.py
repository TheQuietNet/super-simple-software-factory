"""Validation gates: verify the envelope's CLAIMS, never guesses.

A gate is `gate(envelope, run) -> GateReport` — one check per item it looked at.
Violations are derived from the failed checks and sent back to the SAME agent
session as a correction. Every check is recorded either way, so a green gate
says WHAT it verified instead of only that it passed.

Gates check what is mechanically checkable; plan quality is a reviewer's job.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .data_types import EnvelopeBase, GateReport

TAIL_CHARS = 1000        # command output kept as evidence on a failure


def _size(path: Path) -> str:
    n = path.stat().st_size
    return f"{n}B" if n < 1024 else f"{n / 1024:.1f}KB"


def _porcelain_paths(out: str) -> set[str]:
    """Repo-relative paths from `git status --porcelain`.

    Renames and copies report `XY old -> new`; the path that carries the change
    is the new one, and taking the whole string would never match a claim.
    """
    paths: set[str] = set()
    for line in out.splitlines():
        entry = line[3:].strip()
        if not entry:
            continue
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1].strip()
        paths.add(entry.strip('"').replace("\\", "/"))
    return paths


def _repo_relative(path: str, repo_root) -> str:
    """Normalize a claimed path into the form `git status` reports.

    Agents claim paths inconsistently — absolute, `./`-prefixed, or with
    backslashes on Windows. Normalizing both sides is what lets the comparison
    below be exact instead of fuzzy.
    """
    norm = str(path).strip().strip('"').replace("\\", "/")
    try:
        candidate = Path(path)
        if candidate.is_absolute():
            norm = candidate.resolve().relative_to(Path(repo_root).resolve()).as_posix()
    except (ValueError, OSError):
        pass          # absolute and outside the repo: leave it, it cannot match
    while norm.startswith("./"):
        norm = norm[2:]
    return norm.lstrip("/")


def artifacts_exist(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = Path(a)
        report.check(a, p.exists(),
                     f"exists, {_size(p)}" if p.exists() else "declared artifact does not exist")
    return report


def files_non_empty(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = Path(a)
        if not (p.exists() and p.is_file()):
            continue                       # existence is artifacts_exist's job
        empty = p.stat().st_size == 0
        report.check(a, not empty, "declared artifact is empty" if empty else _size(p))
    return report


def json_parses(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = Path(a)
        if p.suffix != ".json" or not p.exists():
            continue
        try:
            parsed = json.loads(p.read_text())
            report.check(a, True, f"parses, {type(parsed).__name__}")
        except json.JSONDecodeError as e:
            report.check(a, False, f"declared JSON artifact does not parse: {e}")
    return report


def diff_matches_claims(envelope: EnvelopeBase, run) -> GateReport:
    """Every file claimed changed must exist on disk."""
    report = GateReport()
    for f in getattr(envelope, "changed_files", []):
        p = Path(f)
        report.check(f, p.exists(),
                     f"exists, {_size(p)}" if p.exists() else "claimed changed file does not exist")
    return report


def new_tests_are_discoverable(envelope: EnvelopeBase, run) -> GateReport:
    """A test file the runner cannot see is not a test.

    Written after a live false-done on 2026-08-23 (adw_id 615f4542). The builder
    was asked to add tests, reported success, and the suite went green — because
    it created `tests/test_paragraphs.js` while this repo's runner globs
    `tests/**/*.test.js`. The new file never executed. Test count before: 493.
    After: 493. Every existing gate passed, and the ADW committed.

    `tests_pass` cannot catch this: a suite that never grew still exits 0. This
    gate checks the NAMES instead — anything the agent added under tests/ must
    match the pattern the runner actually collects, so an unrunnable test fails
    the phase in-session and the builder gets a chance to rename it.

    Deliberately narrow: it says nothing about whether a test is any good, only
    that the runner can see it. That is the part a machine can settle.

    FAIL-CLOSED. The first version of this gate keyed on the substring "tests/"
    and returned "not applicable" when nothing matched. On the very next run
    (adw_id f501b92a) the planner dropped a letter and the builder wrote
    `ests/pull-video-paragraphs.test.js` — 45 real lines, in a directory that
    does not exist in this repo. "ests/" does not contain "tests/", so the gate
    said not-applicable and PASSED. It reproduced the exact defect it was
    written to catch, because it defaulted to allow.

    So: any claimed file that LOOKS like a test — by name, anywhere in the tree —
    must sit under tests/ AND end in .test.js. Claiming no test file at all is
    also a failure (9d3e1718). A default of "no opinion" is what let two
    false-dones through; there is no not-applicable branch any more.
    """
    report = GateReport()
    looks_like_test = [
        f for f in getattr(envelope, "changed_files", [])
        if ".test." in Path(f).name or Path(f).name.startswith("test_")
        or "test" in Path(f).parent.name.lower()
    ]
    if not looks_like_test:
        # 9d3e1718 claimed only substack/fetch-substack.js, no test, and this
        # branch PASSED. A build that names no test has not tested anything.
        report.check("test files claimed", False,
                     "envelope claims no test file — a build that adds no "
                     "discoverable test has not tested anything")
        return report
    for f in looks_like_test:
        norm = f.replace("\\", "/")
        ok = norm.startswith("tests/") and norm.endswith(".test.js")
        report.check(f, ok,
                     "under tests/ and matches the runner glob" if ok else
                     "WILL NEVER RUN — this repo's runner collects ONLY "
                     "tests/**/*.test.js. Place it directly under tests/ "
                     "(not ests/, not src/) and end the name in .test.js")
    return report


def claims_are_actually_modified(envelope: EnvelopeBase, run) -> GateReport:
    """Every file claimed changed must appear in git's change set.

    `diff_matches_claims` only asks whether the claimed path EXISTS, which is
    trivially true for any file already in the repo. Live on 2026-08-23
    (adw_id f501b92a) the builder claimed it modified `pull-video.js`, that gate
    reported "exists, 7.4KB", and the commit contained no change to it at all —
    the feature was never written. Existence is not evidence of work.

    This asks git instead: is the claimed path actually dirty right now?

    The match is EXACT, against normalized repo-relative paths. The first
    version compared suffixes — `d.endswith("/" + norm)` — which is defeated by
    the precise defect this gate was written to catch: run 615f4542's builder
    invented `src/pull-video.js` instead of editing the real `pull-video.js` at
    the repo root, and a claim of `pull-video.js` is a suffix of the impostor's
    path. That run was caught by the neighbouring discoverability gate, not by
    this one; against a claim with no test file involved it would have passed.
    A basename is not an identity, so a same-named file in another directory is
    now a FAILURE with the near-miss named in the note — the diagnostic an agent
    needs to correct itself, rather than a silent pass.
    """
    report = GateReport()
    claimed = list(getattr(envelope, "changed_files", []))
    if not claimed:
        report.check("changed_files", False,
                     "builder claimed no changed files — a build phase that "
                     "changes nothing has not built anything")
        return report
    try:
        # -uall, not the default: git collapses a wholly-untracked directory to
        # `?? src/` and never names the files inside it. A builder creating the
        # first file in a new directory would then be told git saw no change —
        # the gate failing an honest build, which is how a fail-closed check
        # gets switched off.
        out = subprocess.run(["git", "status", "--porcelain", "-uall"],
                             cwd=run.repo_root, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60).stdout
    except OSError as error:
        report.check("git status", False, f"could not read git status: {error}")
        return report
    dirty = _porcelain_paths(out)
    for f in claimed:
        norm = _repo_relative(f, run.repo_root)
        ok = norm in dirty
        if ok:
            note = "present in git's change set"
        else:
            note = "claimed as changed but git sees NO modification to it"
            basename = norm.rsplit("/", 1)[-1]
            near = sorted(d for d in dirty if d.rsplit("/", 1)[-1] == basename)
            if near:
                note += (f" — git DID modify {', '.join(near)}. A file with the "
                         f"same name in a different directory is NOT the file "
                         f"you claimed. Edit the path you named, or claim the "
                         f"path you actually edited.")
        report.check(f, ok, note)
    return report


# Repo-relative file tokens: `dir/file.ext` or a bare `file.ext` of source types.
# Rejects URLs. Used to read a Where: line and a planner file list without
# guessing prose.
_FILEISH = re.compile(
    r"[A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)+\.[A-Za-z0-9]+"
    r"|[A-Za-z0-9_.-]+\.(?:js|py|ts|tsx|mjs|cjs|md)"
)


def _fileish_tokens(text: str) -> set[str]:
    found: set[str] = set()
    for match in _FILEISH.finditer(text or ""):
        path = match.group(0).replace("\\", "/").lstrip("./")
        if "://" in path:
            continue
        found.add(path)
    return found


def _paths_from_where(text: str) -> set[str]:
    found: set[str] = set()
    for match in re.finditer(r"(?im)^Where:\s*(.+)$", text or ""):
        found |= _fileish_tokens(match.group(1))
    return found


def _plan_text(run) -> str:
    handoff = getattr(run, "context_handoff_dir", None)
    if not handoff:
        return ""
    path = Path(handoff) / "plan.md"
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        return ""


def requested_scope(run) -> set[str]:
    """Where: on the request wins; otherwise file tokens in context_handoff/plan.md."""
    where = _paths_from_where(getattr(run, "request", "") or "")
    if where:
        return where
    return _fileish_tokens(_plan_text(run))


def claims_are_in_requested_scope(envelope: EnvelopeBase, run) -> GateReport:
    """Claimed paths must be the files the request/plan named.

    `claims_are_actually_modified` asks whether git saw the named path. It does
    not ask whether that path was what was asked for. Live on 2026-08-27
    (adw_id 9d3e1718) the builder claimed `substack/fetch-substack.js`, git
    agreed, tests_pass stayed green, and the ADW committed a 1-line SKIP_EMBED
    change against a podcast-backfill ticket.

    Where: on the four-line prompt is the allowlist when present. Otherwise the
    planner's `plan.md` file tokens. No parseable scope is a failure — a gate
    that cannot form an opinion must refuse, not skip.
    """
    report = GateReport()
    allow = requested_scope(run)
    claimed = [
        _repo_relative(f, run.repo_root)
        for f in getattr(envelope, "changed_files", [])
    ]
    if not allow:
        report.check("requested scope", False,
                     "could not read a Where: line or plan file list — "
                     "a build with no named scope cannot be checked")
        return report
    extras = [c for c in claimed if c not in allow]
    in_scope = [c for c in claimed if c in allow]
    if extras:
        allow_note = ", ".join(sorted(allow))
        for extra in extras:
            report.check(extra, False,
                         f"not in requested scope ({allow_note})")
    if not in_scope:
        report.check("where files claimed", False,
                     "none of the requested paths were claimed: "
                     + ", ".join(sorted(allow)))
        return report
    for path in in_scope:
        report.check(path, True, "in requested scope")
    return report


def verdict_consistent(envelope: EnvelopeBase, run) -> GateReport:
    """A review's verdict must agree with the findings it just wrote down.

    Nothing here judges the code — that is the reviewer's job. This checks the
    envelope against itself: an approval that ships blocking items, or a
    rejection that names no problem, is a claim the harness can refute without
    reading a line of the diff.
    """
    report = GateReport()
    approved = bool(getattr(envelope, "approved", False))
    blocking = list(getattr(envelope, "blocking", []))
    unmet = [f.requirement for f in getattr(envelope, "findings", []) if not f.met]

    report.check("approved vs blocking", not (approved and blocking),
                 "no blocking items" if not blocking
                 else f"{len(blocking)} blocking item(s) while approved=true"
                 if approved else f"{len(blocking)} blocking item(s), not approved")
    report.check("approved vs findings", not (approved and unmet),
                 "every requirement met" if not unmet
                 else f"{len(unmet)} unmet requirement(s) while approved=true"
                 if approved else f"{len(unmet)} unmet requirement(s), not approved")
    report.check("rejection names a problem", approved or bool(blocking or unmet),
                 "verdict is supported" if approved or blocking or unmet
                 else "approved=false but no blocking item or unmet requirement was given")
    return report


#: Every phase where an agent claims to have CHANGED THE REPO runs this set.
#:
#: One exported policy rather than a gate list spelled out per call site. The
#: 2026-08-24 review found the hardened gates wired into ONE of the four ADWs
#: that commit — `adw_plan_build_test` — while `adw_plan_build`,
#: `adw_plan_build_test_quality` and `adw_simple_sdlc` each still passed
#: `[diff_matches_claims]`, the gate whose insufficiency is the reason the other
#: two exist. Not one of those three had been updated, and the branch was
#: described as uniformly hardened. Coverage that depends on remembering every
#: call site is coverage that drifts, so the call sites now name the POLICY and
#: `tests/test_gate_wiring.py` walks the tree to prove none of them opted out.
#:
#: A tuple, not a list: shared mutable default state that any caller can append
#: to is how a policy quietly becomes per-run. Pydantic coerces it on the way
#: into `AgentCall.gates`.
BUILDER_GATES = (artifacts_exist, claims_are_actually_modified,
                 claims_are_in_requested_scope, new_tests_are_discoverable)


def tests_pass(command: str):
    """Gate factory: the given shell command must exit 0."""
    def gate(envelope: EnvelopeBase, run) -> GateReport:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        ok = result.returncode == 0
        note = f"exit {result.returncode}"
        if not ok:
            note += "\n" + (result.stdout + result.stderr)[-TAIL_CHARS:]
        return GateReport().check(command, ok, note)
    gate.__name__ = f"tests_pass({command})"
    return gate
