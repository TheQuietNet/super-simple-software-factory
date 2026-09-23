"""Detects a coding agent stuck repeating the same tool call(s) — #53193.

Live incident: a builder finished its real edits, then alternated `write
Report.json` / `bash rm Report.json` for 18+ minutes (~60 times) because it
misread the builder prompt's `## Report` heading as a file to create. Nothing
stopped it until a human killed it by hand.

Fed one normalized tool-call record per completed call — the SAME shape
every adapter's own ToolCallTracker already produces for the tracer (a tool
name plus its args dict) — so detection is identical across pi and
claude_code rather than three separate heuristics.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional

from .agent_budget import AgentInterrupt

# Same preference order as agent_pi._label / agent_cc._label — the arg that
# identifies a call at a glance, and the base of every signature.
_PRIMARY_ARGS = ("command", "path", "file_path", "pattern", "query", "url")

# Round-2 reviewer finding: the primary-arg-only signature collapsed four
# PAGINATED reads of the same file (different offset/limit) into one
# identical signature — a false loop. These are folded in as `@key=value`
# suffixes so two calls that share a path but differ in where/how much they
# read are NOT the same signature.
_DISTINGUISHING_ARGS = ("offset", "limit")

# Large or inherently-varying payloads (a write's full file content, an
# edit's replacement text) must never be dropped from the signature — an
# agent that keeps changing the file WOULD then look identical to one
# rewriting the exact same content — but embedding them raw would make the
# signature itself enormous and swamp the trace. Hashed instead.
#
# Round-3 reviewer finding (cross-family): pi's edit tool takes `edits`;
# Claude Code's Edit/MultiEdit tools take `old_string`/`new_string` instead
# — without those two, `file_path` alone was the WHOLE signature for a
# Claude edit, so four genuinely different edits to the same file collapsed
# into one identical signature and false-triggered.
_CONTENT_ARGS = ("content", "new_content", "text", "edits", "old_string", "new_string")

# Round-2 reviewer finding, non-blocking bonus: `rm x`, `rm -f x`, `del x`,
# `Remove-Item x` are all "delete x" — without normalizing them, alternating
# the SPELLING of the delete command across an otherwise-identical
# write/delete cycle would dodge the cycle detector.
_DELETE_COMMAND_RE = re.compile(
    r"^(?:rm(?:\s+-\S+)*|del(?:\s+/\S+)*|remove-item(?:\s+-\S+)*)\s+(.+)$",
    re.IGNORECASE,
)

# Round-2 reviewer finding: a read-only/inspection tool, or a bash command
# that just RUNS the test suite, repeated identically is often legitimate
# iteration (re-check a file, re-run tests after each fix) rather than a
# stuck agent — so these need a higher bar before they count as a loop. Only
# affects the CONSECUTIVE-repeat check; an A/B cycle mixing a low-risk call
# with anything else is unusual enough on its own to keep the normal bar.
#
# All lowercase, matched through `_canonical_tool()` below: pi's own tool
# names already arrive lowercase, but Claude Code's built-ins do not (Read,
# Grep, Glob, Bash, ...) — round-3 reviewer finding, see `_canonical_tool`.
_LOW_RISK_TOOLS = frozenset({"read", "ls", "grep", "find", "glob"})
_TEST_RUN_MARKERS = ("pytest", "node --test", "npm test", "npm run test",
                     "go test", "cargo test", "uv run pytest", "jest", "vitest")


def _canonical_tool(tool: str) -> str:
    """Round-3 reviewer finding (cross-family): pi lowercases its own tool
    names, but Claude Code's built-ins do not (`Read`, `Edit`, `Bash`,
    `Write`, `Grep`, `Glob`, `MultiEdit`, ...) — matched verbatim against
    `_LOW_RISK_TOOLS` (all lowercase), every one of Claude's read-only
    calls silently fell through to the MUTATING threshold instead of the
    read-only one. Every signature/classification decision below goes
    through this first, so a call is recognized the same way regardless of
    which coding agent reported it — pi, claude_code, or (once its tool
    calls are traced individually — see agent_grok.py's own note) grok.
    """
    return str(tool).strip().lower()


def _normalize_command(command: str) -> str:
    normalized = " ".join(command.split())
    delete_match = _DELETE_COMMAND_RE.match(normalized)
    if delete_match:
        return f"DELETE {' '.join(delete_match.group(1).split())}"
    return normalized


def signature(tool: str, args: dict) -> str:
    """tool name + whitespace-normalized primary-arg value (e.g.
    "write:Report.json"), plus `@key=value` suffixes for anything that
    distinguishes two calls sharing that primary value but NOT their real
    effect (pagination) or that is too large/volatile to embed raw (write
    content, or Claude's old_string/new_string edit payload — hashed). A
    bare `rm`/`del`/`Remove-Item` command is normalized to `DELETE <path>`
    first, so varying the delete spelling does not dodge detection. Tool
    name is canonicalized (lowercased) first — see `_canonical_tool`.
    """
    tool = _canonical_tool(tool)
    args = args or {}
    value = next((args[key] for key in _PRIMARY_ARGS
                  if isinstance(args.get(key), str) and args[key].strip()), "")
    if not value:
        value = next((v for v in args.values() if isinstance(v, str) and v.strip()), "")
    normalized = _normalize_command(str(value)) if value else ""
    base = f"{tool}:{normalized}" if normalized else tool

    suffixes: list[str] = []
    for key in _DISTINGUISHING_ARGS:
        if args.get(key) is not None:
            suffixes.append(f"{key}={args[key]}")
    for key in _CONTENT_ARGS:
        if args.get(key):
            digest = hashlib.sha256(repr(args[key]).encode("utf-8", "replace")).hexdigest()[:12]
            suffixes.append(f"{key}#{digest}")
    return f"{base}@{','.join(suffixes)}" if suffixes else base


def _is_low_risk(tool: str, args: dict) -> bool:
    """True for a call unlikely to BE the incident this guard exists for —
    nothing here writes, edits, or deletes anything."""
    tool = _canonical_tool(tool)
    if tool in _LOW_RISK_TOOLS:
        return True
    if tool == "bash":
        command = str((args or {}).get("command", "")).lower()
        return any(marker in command for marker in _TEST_RUN_MARKERS)
    return False


class LoopDetected(AgentInterrupt):
    """A repeating tool-call pattern was detected in the current turn.

    `signature` is the call (or "A / B" pair) that repeated. `str(this)` is
    exactly `"agent loop detected: <signature>"` — agents.py's `send()`
    reuses that string verbatim as the phase-failure reason on recurrence,
    and reads `.signature` to build the one-time correction prompt AND to
    scope which specific pattern has already been corrected once (a
    DIFFERENT loop later in the same phase must get its own correction, not
    an immediate fail just because some earlier, unrelated loop was already
    corrected).
    """

    def __init__(self, signature: str) -> None:
        self.signature = signature
        super().__init__(f"agent loop detected: {signature}")


@dataclass
class LoopGuardConfig:
    """Thresholds — configurable per ConfigDefaults."""

    repeat_count: int = 4              # N identical MUTATING calls in a row
    read_only_repeat_count: int = 8    # N identical low-risk calls in a row (round-2 finding)
    cycle_count: int = 3               # A,B,A,B,... full periods (2*cycle_count calls)


class LoopGuard:
    """One instance per coding-agent TURN — agents.py's `_event_forwarder`
    builds a fresh one on every `send()`, so a loop that spans a correction
    turn is tracked as a RECURRENCE one level up (in agents.py), not by
    carrying tool-call history across turns here.
    """

    def __init__(self, config: Optional[LoopGuardConfig] = None) -> None:
        self.config = config or LoopGuardConfig()
        self._history: list[str] = []
        self._low_risk: list[bool] = []   # parallel to _history

    def observe(self, tool: str, args: dict) -> None:
        """Feed one completed tool call. Raises LoopDetected the instant a
        pattern completes; returns None otherwise."""
        self._history.append(signature(tool, args))
        self._low_risk.append(_is_low_risk(tool, args))
        hit = self._consecutive_repeat() or self._cycle_repeat()
        if hit:
            raise LoopDetected(hit)

    def _consecutive_repeat(self) -> Optional[str]:
        # The class of the CURRENT (most recent) call decides the threshold —
        # a run of N identical signatures is, by definition, all the same
        # tool, so classifying the last one classifies the whole streak.
        low_risk = self._low_risk[-1] if self._low_risk else False
        n = self.config.read_only_repeat_count if low_risk else self.config.repeat_count
        if n < 1 or len(self._history) < n:
            return None
        tail = self._history[-n:]
        return tail[0] if len(set(tail)) == 1 else None

    def _cycle_repeat(self) -> Optional[str]:
        k = self.config.cycle_count
        if k < 1:
            return None
        window = 2 * k
        if len(self._history) < window:
            return None
        tail = self._history[-window:]
        a, b = tail[0], tail[1]
        if a == b:
            return None   # an identical pair is a consecutive-repeat, not a cycle
        if all(tail[i] == (a if i % 2 == 0 else b) for i in range(window)):
            return f"{a} / {b}"
        return None
