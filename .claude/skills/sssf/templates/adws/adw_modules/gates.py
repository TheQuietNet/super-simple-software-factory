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

from . import permissions, quality
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
            parsed = json.loads(p.read_text(encoding="utf-8"))
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


def _runner_test_glob() -> str | None:
    """quality.TEST_GLOB, or None if missing/placeholder (fail closed)."""
    raw = getattr(quality, "TEST_GLOB", None)
    if raw is None:
        return None
    text = str(raw).strip().replace("\\", "/")
    if not text or "PLACEHOLDER" in text.upper():
        return None
    return text


def _glob_to_anchored_re(pattern: str) -> re.Pattern[str]:
    """Translate a posix glob to an anchored regex. Not glob.translate (3.13)
    and not PurePath.match (matches from the right; 3.12 `**` is one segment).

    `**` → `(?:.*/)*` (zero or more directories — oracle needs
    tests/a/b/foo.test.js; `(?:.*/)?` would only allow one extra segment).
    `*` → `[^/]*`. `?` → `[^/]`. Everything else escaped. Wrapped `^...$`.
    """
    pat = pattern.replace("\\", "/").lstrip("./")
    i = 0
    out: list[str] = []
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)*")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _matches_test_glob(path: str, pattern: str) -> bool:
    """True iff posix-normalised `path` fullmatches `pattern` from the LEFT."""
    norm = path.replace("\\", "/").lstrip("./")
    return _glob_to_anchored_re(pattern).fullmatch(norm) is not None


def new_tests_are_discoverable(envelope: EnvelopeBase, run) -> GateReport:
    """A test file the runner cannot see is not a test.

    Written after a live false-done: the builder was asked to add tests,
    reported success, and the suite went green — because it created a test
    file the runner's own glob never collected. The new file never executed.
    Every existing gate passed, and the ADW committed.

    `tests_pass` cannot catch this: a suite that never grew still exits 0.
    This gate checks the NAMES instead — anything the agent added that looks
    like a test must match `quality.TEST_GLOB` (the same glob `test()` passes
    to the runner). Language-agnostic means the glob is the SoT, not a
    baked-in suffix and not "anything under tests/".

    FAIL-CLOSED. The first version keyed on a substring and returned "not
    applicable" when nothing matched — a planner typo in the directory name
    then slipped through with the gate saying not-applicable and PASSING.
    There is no not-applicable branch any more. A missing/PLACEHOLDER
    TEST_GLOB also fails closed.

    Claiming no test file at all is also a failure.
    """
    report = GateReport()
    looks_like_test = [
        f for f in getattr(envelope, "changed_files", [])
        if ".test." in Path(f).name or Path(f).name.startswith("test_")
        or Path(f).parent.name.lower() in ("test", "tests", "__tests__")
    ]
    if not looks_like_test:
        report.check("test files claimed", False,
                     "envelope claims no test file — a build that adds no "
                     "discoverable test has not tested anything")
        return report
    glob = _runner_test_glob()
    for f in looks_like_test:
        norm = f.replace("\\", "/")
        if glob is None:
            report.check(
                f, False,
                "WILL NEVER RUN — quality.TEST_GLOB is missing or PLACEHOLDER; "
                "fail closed (no not-applicable). Set TEST_GLOB to the same "
                "glob the test runner collects.")
            continue
        ok = _matches_test_glob(norm, glob)
        report.check(
            f, ok,
            f"matches TEST_GLOB {glob}" if ok else
            f"WILL NEVER RUN — {norm!r} does not match quality.TEST_GLOB {glob!r}")
    return report


def claims_are_actually_modified(envelope: EnvelopeBase, run) -> GateReport:
    """Every file claimed changed must appear in git's change set.

    `diff_matches_claims` only asks whether the claimed path EXISTS, which is
    trivially true for any file already in the repo. Existence is not
    evidence of work.

    This asks git instead: is the claimed path actually dirty right now?

    The match is EXACT, against normalized repo-relative paths. Comparing
    suffixes (`d.endswith("/" + norm)`) is defeated by a builder that invents
    `src/pull-video.js` instead of editing the real `pull-video.js` at the
    repo root — a claim of `pull-video.js` is a suffix of the impostor's
    path. A basename is not an identity, so a same-named file in another
    directory is now a FAILURE with the near-miss named in the note — the
    diagnostic an agent needs to correct itself, rather than a silent pass.
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


# Tokens that would cover EVERY path rather than name a real scope. `**`
# alone (crosses every directory) and bare `*` (matches any top-level file)
# are rejected outright below — honoring either would silently disable this
# gate entirely. `.` and `/` are NOT listed here: `lstrip("./")` below
# already reduces both to the empty string before this check ever runs, so
# they already fall out via the `if not token` branch — named here only so
# that safety property is visible in code, not an accidental side effect
# nobody could point to.
_DEGENERATE_SCOPE_TOKENS = frozenset({"**", "*"})


def _split_where_entries(text: str) -> list[str]:
    """Split a `Where:` line's value on commas OUTSIDE parentheses.

    `Where: a.js, b.js (repo root), lib/gmail.js (the shared implementation —
    reuse it, do not rewrite it), tests/*.test.js` must not be split on EVERY
    comma, including the one inside the parenthetical note — that would
    produce `do not rewrite it)` as its own entry and truncate the
    `lib/gmail.js` entry. A human annotating a Where: entry in parentheses is
    explaining the path, not adding a second one; a comma inside that
    annotation is not a list separator.
    """
    entries: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in text or "":
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            entries.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    entries.append("".join(buf))
    return entries


def _strip_where_annotation(entry: str) -> str:
    """Drop a SINGLE, BALANCED `(...)` annotation from the END of the entry,
    keeping the leading path/glob.

    Taking `entry.find("(")` — the FIRST `(` anywhere — and keeping only what
    comes before it, unconditionally, goes wrong two ways:

    1. `"documentation (not a path)"` would strip down to `"documentation"` —
       a bare word, not a path — and get honored as a scope token anyway if
       the shape check only looks at the CHARACTER CLASS, which a bare word
       trivially satisfies. Fixed on the shape-check side below
       (`_is_where_token_shaped` requires an extension, a `/`, or a glob
       char) — this function still strips it the same way, since a single
       trailing annotation IS what this is.
    2. `"lib/g.js (note) trailing garbage"` would ALSO strip to `"lib/g.js"`
       — silently discarding `trailing garbage`, content that comes AFTER
       the closing paren, as if it had never been there. That is not an
       annotation at the end of the entry; the entry is malformed. This
       version only strips when the entry actually ENDS in `)`, and then
       only the single balanced parenthetical that closes there — anything
       with real content after the last `)` is left untouched, so it falls
       through to the shape check and comes back as a warning instead of
       being silently truncated into something that merely LOOKS like a
       clean path.

    Nested parens inside the trailing annotation (`"lib/g.js (impl (do not
    touch))"`) are handled by depth-counting back to the MATCHING `(` for
    the final `)`, not just the first `(` found. Multiple separate trailing
    annotations (`"lib/g.js (impl) (do not touch)"`) only have their LAST
    one stripped — "a single balanced parenthetical", not "every
    parenthetical" — so the result still isn't path-shaped and still warns,
    rather than this function guessing how many to peel off.
    """
    stripped = entry.strip()
    if not stripped.endswith(")"):
        return stripped               # no trailing annotation to strip
    depth = 0
    open_idx = None
    for i in range(len(stripped) - 1, -1, -1):
        ch = stripped[i]
        if ch == ")":
            depth += 1
        elif ch == "(":
            depth -= 1
            if depth == 0:
                open_idx = i
                break
    if open_idx is None:
        return stripped               # unbalanced — leave it, do not guess
    return stripped[:open_idx].strip()


# A parsed Where: token, after annotation-stripping, must look like a path or
# glob, not merely be built from path-safe CHARACTERS: `"documentation"`
# matches a bare character-class check (letters only) just as well as
# `"lib/gmail.js"` does, so a character class alone would let bare-word prose
# through as if it named a file. A real token here has at least one of: a
# `/` (a path with a directory, or a directory-prefix token), a glob
# metacharacter (`*` or `?`), a dot-extension at the end (a bare top-level
# file: `query.js`, `.gitignore`), OR it names an ENTRY THAT ACTUALLY EXISTS
# at the repo root (`justfile`, `Makefile`, `Dockerfile`, `LICENSE` are all
# real, legitimate Where: targets in real repos and none of them carry an
# extension or a `/`), OR it matches a small allowlist of well-known
# extensionless names for when `repo_root` isn't available to check against
# (a fresh draft, a unit test with no real filesystem). Prose that is none
# of these (`"documentation"`, with no such file on disk) is still rejected
# — the fail-closed default stays "warn, don't guess" for anything that
# isn't demonstrably a path.
_WHERE_TOKEN_CHARS = re.compile(r"^[A-Za-z0-9_.\-/*?]+$")
_WHERE_TOKEN_EXTENSION = re.compile(r"\.[A-Za-z0-9]+$")
_WHERE_TOKEN_KNOWN_EXTENSIONLESS = frozenset({
    "justfile", "Justfile", "JUSTFILE",
    "makefile", "Makefile", "GNUmakefile",
    "Dockerfile", "dockerfile", "Containerfile",
    "LICENSE", "LICENSE.txt", "LICENSE.md", "LICENCE",
    "Procfile", "Vagrantfile", "Rakefile", "Gemfile", "Brewfile",
})


def _is_where_token_shaped(token: str, repo_root: "Path | None" = None) -> bool:
    if not _WHERE_TOKEN_CHARS.match(token):
        return False
    if "/" in token or "*" in token or "?" in token:
        return True
    core = token[:-1] if token.endswith("/") else token
    if _WHERE_TOKEN_EXTENSION.search(core):
        return True
    if core in _WHERE_TOKEN_KNOWN_EXTENSIONLESS:
        return True
    if repo_root is not None:
        try:
            return (Path(repo_root) / core).exists()
        except OSError:
            return False
    return False


def _where_tokens_and_warnings(text: str, repo_root=None) -> tuple[set[str], list[str]]:
    """Parse a `Where:` line's value into (scope tokens, warnings).

    A `Where:` line is authored scope, not prose — `Where: a.js, tests/*.test.js,
    lib/` — so it is split directly rather than routed through
    `_fileish_tokens`'s prose-extraction regex. That regex requires an
    extension-terminated path segment and its character class excludes `*`
    and `?`, so `tests/*.test.js` was never matched whole: it could only
    recover the bare trailing token `test.js`, silently dropping the
    directory and the glob. A token here can be an exact path (today's
    behavior, unchanged), a directory prefix (ends in `/`), or an
    fnmatch-style glob (contains `*` or `?`) — `_scope_matches` interprets
    the shape. A token that would match EVERYTHING (`_DEGENERATE_SCOPE_TOKENS`,
    plus `.`/`/` via the empty-string path below) is dropped rather than
    honored, so a typo'd or over-broad Where: line fails closed (falls
    through to "could not read a Where: line", same as no Where: at all)
    instead of silently waving every claim through.

    Entries are split OUTSIDE parentheses (`_split_where_entries`), then
    each entry's trailing `(...)` annotation is dropped
    (`_strip_where_annotation`) before the leading path/glob is taken. A
    leftover that still doesn't look path-shaped after that — stray prose,
    an unbalanced paren — is never silently used as scope: it comes back as
    a WARNING for the request phase to surface, same fail-closed shape as
    the degenerate-token rejection below.
    """
    tokens: set[str] = set()
    warnings: list[str] = []
    for raw in _split_where_entries(text):
        entry = _strip_where_annotation(raw)
        token = entry.strip().strip('"').replace("\\", "/").lstrip("./")
        if not token or token in _DEGENERATE_SCOPE_TOKENS:
            continue
        if not _is_where_token_shaped(token, repo_root):
            warnings.append(
                f"Where: entry is not path/glob-shaped, dropped from scope "
                f"(not silently used): {raw.strip()!r}")
            continue
        tokens.add(token)
    return tokens, warnings


def _where_tokens(text: str, repo_root=None) -> set[str]:
    tokens, _warnings = _where_tokens_and_warnings(text, repo_root)
    return tokens


def where_warnings(text: str, repo_root=None) -> list[str]:
    """Where: entries that did not parse into a path/glob token.

    For the request phase to print — never folded into scope, per
    `_where_tokens_and_warnings`. Empty when every entry parsed cleanly.
    `repo_root`, when given, lets a bare extensionless token that actually
    EXISTS there (`justfile`, a real file with no `.ext`) parse clean too.
    """
    _tokens, warnings = _where_tokens_and_warnings(text, repo_root)
    return warnings


def _paths_from_where(text: str, repo_root=None) -> set[str]:
    found: set[str] = set()
    for match in re.finditer(r"(?im)^Where:\s*(.+)$", text or ""):
        found |= _where_tokens(match.group(1), repo_root)
    return found


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a `Where:` glob into a regex.

    `*` matches within one path segment and never crosses `/`; `**` crosses
    freely (so `tests/*.test.js` cannot reach `tests/sub/x.test.js`, but
    `tests/**/*.test.js` can). `?` matches exactly one non-`/` character.
    Everything else is matched literally.
    """
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        if pattern[i:i + 2] == "**":
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile(f"^{''.join(out)}$")


def _scope_matches(path: str, token: str) -> bool:
    """True when repo-relative `path` is covered by one Where:/plan `token`."""
    if "*" in token or "?" in token:
        return bool(_glob_regex(token).match(path))
    if token.endswith("/"):
        return path == token[:-1] or path.startswith(token)
    return path == token


def _in_scope(path: str, allow: set[str]) -> bool:
    return any(_scope_matches(path, token) for token in allow)


def _plan_text(run) -> str:
    handoff = getattr(run, "context_handoff_dir", None)
    if not handoff:
        return ""
    path = Path(handoff) / "plan.md"
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        return ""


def pin_plan_scope(run) -> set[str]:
    """Capture the planner's plan.md file-scope ONCE and freeze it on the run.

    `plan.md` lives under `data_dir/sessions/<adw_id>/context_handoff/` —
    gitignored and untracked, so it sits entirely outside
    `permissions.snapshot()` (gitignored paths never appear in `git diff`/
    `git status`, by design, so agents can record their own work there).
    Without this pin, `requested_scope()` would read the file LIVE on every
    call — every gate check, and again at commit time — so a bash-capable
    builder could, with NO Where: line at all, append a path to plan.md
    AFTER the planner phase's own gates had already approved it, silently
    widening its own scope for both `claims_are_in_requested_scope` and
    `scoped_for_commit` and getting an out-of-scope edit committed.

    Call this exactly once, right after the planner phase's `ph.call(...)`
    returns (which only happens once its gates have passed) and before any
    builder phase runs. Every scope decision for the rest of THIS PROCESS'S
    run then reads the pin, never the live file again — see
    `requested_scope`. A build-only ADW that never ran a planner in this
    process (adw_build.py, adw_build_test.py, adw_build_review.py, or
    adw_ship.py joining one of them) never calls this, so it has no plan
    scope at all and must be given an explicit `Where:` line instead — see
    `scoped_for_commit`'s refusal when neither exists.
    """
    scope = _fileish_tokens(_plan_text(run))
    run.plan_scope = scope
    return scope


def requested_scope(run) -> set[str]:
    """Where: on the request wins; otherwise the planner's PINNED plan.md
    scope, if `pin_plan_scope()` was called for this run — plan.md is never
    read live here (see `pin_plan_scope`)."""
    where = _paths_from_where(getattr(run, "request", "") or "", getattr(run, "repo_root", None))
    if where:
        return where
    return getattr(run, "plan_scope", None) or set()


def _dirty_paths(run) -> set[str]:
    """Every path git currently sees as dirty (tracked or untracked), repo-relative.

    `-uall` so a wholly-untracked directory reports every file inside it by
    name — the default `?? dir/` would hide a builder's new files under a
    name nothing here can match against a claim or a scope token.

    An OSError (no git, bad cwd) used to degrade to "nothing is dirty" — the
    same fail-open shape `permissions._git`'s pre-fix "" return had. Its only
    caller, `fill_empty_changed_files`, is a plain function call in
    `agents.execute`'s gate loop, not a gate returning a report — an
    uncaught exception here propagates up the same way and kills the phase,
    which is what a git failure should do, not silently answer "no".

    A `git status` that RUNS and exits nonzero (not a git repo, a corrupted
    index) is also silently NOT "produced no output" — `subprocess.run`
    itself never raises on a nonzero exit; only `.returncode` says so. Now
    checked explicitly and raised, matching `permissions._git`'s fix for the
    identical shape of bug.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain", "-uall"],
        cwd=getattr(run, "repo_root", None),
        capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git status --porcelain -uall failed ({result.returncode}): "
            f"{result.stderr.strip()}")
    return {_repo_relative(p, run.repo_root) for p in _porcelain_paths(result.stdout)}


def scoped_for_commit(run, touched: list[str]) -> tuple[list[str], list[str]]:
    """Split an agent-touched change-set into (kept, excluded) against scope.

    Defense in depth: a builder edits a path out of requested scope, the
    scope gate fails the claim once, and the builder's fix is simply to DROP
    that path from `changed_files` — which satisfies
    `claims_are_in_requested_scope` (nothing claimed is out of scope) while
    the file sits dirty on disk. `commit_paths` stages
    `run.agent_touched_paths`, not `changed_files`, so the untouched-by-gates
    edit would have been committed anyway.

    "No requested scope -> keep everything" is FAIL-OPEN for a code commit —
    a build-only ADW (adw_build.py, adw_build_test.py via adw_ship.py) with
    no Where: line and no planner phase to pin a scope from would stage
    every touched path unconditionally, the exact hole `scoped_for_commit`
    exists to close. No scope means REFUSE, not "no opinion":
    `commit_plan`/`commit_docs` are unaffected (`adw_simple_sdlc.py`'s
    shared `commit()` closure only calls this when
    `hasattr(envelope, "changed_files")`, true only for a `BuildOutput`,
    never for `PlanOutput`/`DocumentOutput`), and a planner-in-process
    script (adw_plan_build.py and siblings) cannot reach this refusal in
    ordinary operation either: `claims_are_in_requested_scope` already reads
    the SAME pinned scope and refuses the build phase first if it is empty,
    so a build phase that got this far had a non-empty scope. The refusal
    is real and reachable for exactly the case it targets:
    `ship.commit_green`, called from `adw_ship.py` — always a build-only
    ADW, since it commits whatever `adw_build_test.run_pipeline` (no
    planner) produced.
    """
    allow = requested_scope(run)
    if not allow:
        raise RuntimeError(
            "refusing to stage code with no determinable scope: no Where: "
            "line on the request, and no planner phase pinned a plan.md "
            "scope for this run. A build-only ADW (adw_build, adw_build_test, "
            "adw_build_review, adw_ship) has no planner to pin a scope from — "
            "give it an explicit `Where: <paths>` line in the prompt.")
    repo_root = getattr(run, "repo_root", None)
    kept: list[str] = []
    excluded: list[str] = []
    for path in touched:
        norm = _repo_relative(path, repo_root) if repo_root else path
        (kept if _in_scope(norm, allow) else excluded).append(path)
    return kept, excluded


def fill_empty_changed_files(envelope: EnvelopeBase, run) -> bool:
    """Union porcelain ∩ requested scope onto ``changed_files``.

    A JSON retry often re-emits the Report with ``changed_files: []`` after
    the builder already wrote the Where: files — the fail-closed gates would
    then refuse a tree that had actually been built. Also fills a PARTIAL
    claim: the builder writes both the source file and its test, then claims
    only the source file; the empty-only fill would leave that partial claim
    alone, so ``new_tests_are_discoverable`` would fail a tree that had the
    test on disk and in scope.

    Mutates ``envelope.changed_files`` in place. Returns True when the list
    grew. Does NOT guess: dirty paths outside Where:/plan stay off the
    envelope, so ``claims_are_in_requested_scope`` still fails a wander.
    Existing claims are kept (phantoms still fail
    ``claims_are_actually_modified``). An empty intersection with an empty
    claim leaves the list empty so a no-op build still refuses.
    """
    if not hasattr(envelope, "changed_files"):
        return False
    allow = requested_scope(run)
    if not allow:
        return False
    dirty = _dirty_paths(run)
    allow_n = {_repo_relative(p, run.repo_root) for p in allow}
    in_scope = {p for p in dirty if _in_scope(p, allow_n)}
    if not in_scope:
        return False
    claimed = [
        _repo_relative(p, run.repo_root)
        for p in (getattr(envelope, "changed_files", []) or [])
    ]
    claimed_set = set(claimed)
    missing = in_scope - claimed_set
    if not missing:
        return False
    envelope.changed_files = sorted(claimed_set | in_scope)
    return True


def claims_are_in_requested_scope(envelope: EnvelopeBase, run) -> GateReport:
    """Claimed paths must be the files the request/plan named.

    `claims_are_actually_modified` asks whether git saw the named path. It
    does not ask whether that path was what was asked for.

    Where: on the four-line prompt is the allowlist when present. Otherwise the
    planner's `plan.md` file tokens. No parseable scope is a failure — a gate
    that cannot form an opinion must refuse, not skip.

    Also checks what the agent ACTUALLY touched THIS PHASE, not only what it
    claimed. A path modified out of scope, then dropped from `changed_files`
    after a correction, would otherwise satisfy this gate — nothing claimed
    was out of scope — while the file sat dirty, about to be staged by
    `commit_paths` regardless of the claim. "Touched this phase" is
    `run.phase_baseline_dirty` (a snapshot taken before the agent's first
    prompt) subtracted from the current dirty set — never the whole repo —
    so an operator's own pre-existing WIP, dirty before the agent ever ran,
    is never flagged. See `scoped_for_commit` for the second layer at commit
    time.
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
    extras = [c for c in claimed if not _in_scope(c, allow)]
    in_scope = [c for c in claimed if _in_scope(c, allow)]
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

    # A path the agent actually touched but then DROPPED from its claim is
    # not thereby exempt — commit_paths stages `agent_touched_paths`, not
    # `changed_files`, so dropping the claim would have landed the
    # out-of-scope edit anyway. Checks every path dirtied DURING this phase
    # (claimed or not) against the same scope.
    #
    # Diffing the WHOLE repo here would fail an operator's own pre-existing
    # WIP too — dirty before this agent ever ran. `agents.execute` stashes
    # `run.phase_baseline_dirty`, the dirty set it snapshotted BEFORE
    # sending the agent its first prompt (the same baseline
    # `permissions.enforce` diffs against for its OWN answer); this gate
    # runs INSIDE that same call, before `enforce` ever does, so it cannot
    # read `agent_touched_paths` yet and has to take the same before/after
    # shape itself. Subtracting that baseline here is what tells "the
    # agent's own edit" apart from "something already dirty when it
    # started" — a caller with no baseline (a gate exercised standalone, as
    # every test below does unless it sets one) degrades to the
    # pre-baseline behavior: every currently-dirty path is in play, which is
    # correct for those tests since nothing in them is dirty except what
    # they call `dirty()` on to represent the agent's own edit.
    #
    # PATH PRESENCE in the baseline alone is not enough to exempt a path
    # forever, even if the agent went on to edit it AGAIN during this same
    # phase — a pre-dirty, out-of-scope file the agent further modified
    # would get no correction if only the key set were subtracted, since it
    # was already a key in `baseline` regardless of content.
    # `phase_baseline_dirty` is now the FULL per-path content fingerprint
    # from `permissions.snapshot()` (not just its key set), so
    # `permissions.changed_paths` — the SAME before/after diff
    # `permissions.enforce` uses for its own answer — tells "genuinely
    # touched during this phase" apart from "was dirty and is STILL
    # identical to how it was when the phase started", not just "was it a
    # key". A missing/non-dict baseline (every test below unless it sets
    # one) degrades to `{}`, and diffing against an empty dict returns every
    # currently-dirty path — the same pre-baseline behavior as before.
    claimed_set = set(claimed)
    baseline = getattr(run, "phase_baseline_dirty", None)
    if not isinstance(baseline, dict):
        baseline = {}
    touched_this_phase = permissions.changed_paths(baseline, permissions.snapshot(run))
    unclaimed_out_of_scope = sorted(
        p for p in touched_this_phase
        if p not in claimed_set and not _in_scope(p, allow)
    )
    for path in unclaimed_out_of_scope:
        allow_note = ", ".join(sorted(allow))
        report.check(path, False,
                     f"modified but neither claimed nor in requested scope "
                     f"({allow_note}) — dropping a claim does not un-touch "
                     f"the file; revert it or bring it into scope")
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


def ask_matches_diff(envelope: EnvelopeBase, run) -> GateReport:
    """Semantic ask-vs-diff via TypeSafe Jev. Opt-in, skip-never-block.

    Mechanical BUILDER_GATES already proved the claimed files exist, are dirty,
    and sit in scope. This asks whether the patch satisfies the engineer ask.
    Off unless SSSF_JEV=1. Missing key or API error is a skip (pass), so the
    local-first roster stays offline-capable. Fail-closed only on high-confidence
    reject (accept != approve AND (confidence >= 0.7 OR meets_request <= 0.3)).
    Envelope-only is unsafe — the live git patch is the load-bearing field.
    """
    from . import jev

    report = GateReport()
    if not jev.enabled():
        return report.check("jev", True, "skipped: SSSF_JEV off")
    if not jev.api_key():
        return report.check("jev", True, "skipped: no TYPESAFE_API_KEY")
    try:
        answers = jev.evaluate(jev.collect_state(envelope, run))
    except Exception as exc:  # noqa: BLE001 — skip on transport/parse, never block
        return report.check("jev", True, f"skipped: {type(exc).__name__}: {exc}")
    reject = jev.high_conf_reject(answers)
    accept = (answers.get("accept") or {}).get("choice")
    conf = (answers.get("accept") or {}).get("confidence")
    meets = (answers.get("meets_request") or {}).get("noul")
    note = f"accept={accept}@{conf} meets_request={meets}"
    if reject:
        note += " — high-conf reject; send the builder back"
    return report.check("jev ask vs diff", not reject, note)


#: Every phase where an agent claims to have CHANGED THE REPO runs this set.
#:
#: One exported policy rather than a gate list spelled out per call site. A
#: review once found the hardened gates wired into only ONE of the ADWs that
#: commit, while the others each still passed `[diff_matches_claims]`, the
#: gate whose insufficiency is the reason the other gates exist — and the
#: branch was described as uniformly hardened. Coverage that depends on
#: remembering every call site is coverage that drifts, so the call sites
#: now name the POLICY and `tests/test_gate_wiring.py` walks the tree to
#: prove none of them opted out.
#:
#: A tuple, not a list: shared mutable default state that any caller can append
#: to is how a policy quietly becomes per-run. Pydantic coerces it on the way
#: into `AgentCall.gates`.
#:
#: ask_matches_diff is skip-if-off: SSSF_JEV unset → pass. It does not replace
#: the mechanical four.
BUILDER_GATES = (artifacts_exist, claims_are_actually_modified,
                 claims_are_in_requested_scope, new_tests_are_discoverable,
                 ask_matches_diff)


def tests_pass(command: str):
    """Gate factory: the given shell command must exit 0."""
    def gate(envelope: EnvelopeBase, run) -> GateReport:
        result = subprocess.run(command, shell=True, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        ok = result.returncode == 0
        note = f"exit {result.returncode}"
        if not ok:
            note += "\n" + (result.stdout + result.stderr)[-TAIL_CHARS:]
        return GateReport().check(command, ok, note)
    gate.__name__ = f"tests_pass({command})"
    return gate
