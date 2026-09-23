"""Claude Code interface — replaces the v1 NotImplemented stub.

Mirrors `agent_pi.run` so `agents.execute` can dispatch by coding_agent.

Uses:
  claude -p --output-format stream-json --verbose
  --session-id <uuid>   first turn of a phase (create)
  --resume <uuid>       JSON/gate correction turns (same context window)

Session ids are UUIDs (Claude requirement). SSSF's human-readable session
strings are hashed into a deterministic UUID5 under a fixed namespace.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from . import agent_budget
from .data_types import PiRequest, PiResult, UsageBreakdown
from .utils import now_iso, operator_env

CLAUDE_PATH = os.environ.get("CLAUDE_PATH", "claude")
# Namespace for stable UUID5 mapping from SSSF session strings → Claude session-id
_SESSION_NS = uuid.UUID("a7f3c2e1-5b94-4d6a-9e08-1c2d3e4f5a6b")

# Thinking levels in SSSF config → Claude --effort
_EFFORT_MAP = {
    "off": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

# Model pattern → Claude --model (bare aliases or provider/model-id)
_MODEL_ALIASES = {
    "anthropic/claude-opus-4": "opus",
    "anthropic/claude-sonnet-4": "sonnet",
    "anthropic/claude-haiku": "haiku",
    "claude-opus": "opus",
    "claude-sonnet": "sonnet",
    "claude-haiku": "haiku",
    "opus": "opus",
    "sonnet": "sonnet",
    "haiku": "haiku",
}

RESULT_SNIPPET_CHARS = 20_000
ARG_VALUE_CHARS = 20_000
LABEL_CHARS = 80
PRIMARY_ARGS = ("command", "path", "file_path", "pattern", "query", "url")


def session_uuid(sssf_session_id: str) -> str:
    """Deterministic UUID for Claude --session-id / --resume from an SSSF id."""
    return str(uuid.uuid5(_SESSION_NS, sssf_session_id))


def resolve_model(pattern: str) -> str:
    """Map roster model string to a Claude CLI --model value."""
    if not pattern:
        return "sonnet"
    if pattern in _MODEL_ALIASES:
        return _MODEL_ALIASES[pattern]
    if pattern.startswith("anthropic/"):
        return pattern.split("/", 1)[1]
    # OpenRouter-style or other provider prefixes: take the leaf if it looks Claude-ish
    if "/" in pattern:
        leaf = pattern.split("/", 1)[1]
        if "claude" in leaf or leaf in ("opus", "sonnet", "haiku"):
            return leaf
    return pattern


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _label(tool: str, args: dict) -> str:
    value = next((args[key] for key in PRIMARY_ARGS
                  if isinstance(args.get(key), str) and args[key].strip()), "")
    if not value:
        value = next((v for v in args.values() if isinstance(v, str) and v.strip()), "")
    value = " ".join(str(value).split())
    return f"{tool}: {_clip(value, LABEL_CHARS)}" if value else tool


class ToolCallTracker:
    """Normalize Claude stream-json tool events into one record per completed call.

    Claude emits assistant tool_use content blocks and later user tool_result
    blocks. We open on tool_use id and close on matching tool_result.
    """

    def __init__(self) -> None:
        self._open: dict[str, dict] = {}

    def observe(self, event: dict) -> Optional[dict]:
        etype = event.get("type", "")

        if etype == "assistant":
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    call_id = str(block.get("id") or "")
                    if not call_id:
                        continue
                    args = block.get("input") or {}
                    if not isinstance(args, dict):
                        args = {"input": args}
                    self._open[call_id] = {
                        "tool": block.get("name") or "tool",
                        "args": args,
                        "started_at": now_iso(),
                        "clock": time.monotonic(),
                    }
            return None

        # tool results often arrive as type=user with tool_result content
        if etype in ("user", "tool_result"):
            message = event.get("message") or event
            content = message.get("content") if isinstance(message, dict) else None
            if content is None and etype == "tool_result":
                content = [event]
            if not isinstance(content, list):
                return None
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                call_id = str(block.get("tool_use_id") or block.get("toolUseId") or "")
                opened = self._open.pop(call_id, {})
                tool = str(opened.get("tool") or "tool")
                args = opened.get("args") or {}
                result_text = block.get("content") or block.get("output") or ""
                if isinstance(result_text, list):
                    result_text = "".join(
                        part.get("text", "") if isinstance(part, dict) else str(part)
                        for part in result_text
                    )
                record = {
                    "tool": tool,
                    "tool_call_id": call_id,
                    "args": {
                        k: _clip(v, ARG_VALUE_CHARS) if isinstance(v, str) else v
                        for k, v in args.items()
                    },
                    "ok": not bool(block.get("is_error") or block.get("isError")),
                    "label": _label(tool, args),
                    "ended_at": now_iso(),
                }
                if result_text:
                    record["result_snippet"] = _clip(str(result_text), RESULT_SNIPPET_CHARS)
                if opened.get("clock"):
                    record["duration_ms"] = int((time.monotonic() - opened["clock"]) * 1000)
                if opened.get("started_at"):
                    record["started_at"] = opened["started_at"]
                return record
        return None


def _usage_from_result(payload: dict, breakdown: UsageBreakdown) -> tuple[int, float, int, int]:
    """Fold Claude result/usage into Pi-compatible UsageBreakdown.

    Returns (total_tokens, total_cost, context_tokens, context_window).
    """
    usage = payload.get("usage") or {}
    input_t = int(usage.get("input_tokens") or 0)
    output_t = int(usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_create = int(usage.get("cache_creation_input_tokens") or 0)
    total = input_t + output_t + cache_read + cache_create
    if total == 0:
        # some payloads only have modelUsage
        for mu in (payload.get("modelUsage") or {}).values():
            if isinstance(mu, dict):
                input_t += int(mu.get("inputTokens") or 0)
                output_t += int(mu.get("outputTokens") or 0)
                cache_read += int(mu.get("cacheReadInputTokens") or 0)
                cache_create += int(mu.get("cacheCreationInputTokens") or 0)
        total = input_t + output_t + cache_read + cache_create

    cost = float(payload.get("total_cost_usd") or 0.0)
    breakdown.input_tokens += input_t
    breakdown.output_tokens += output_t
    breakdown.cache_read_tokens += cache_read
    breakdown.cache_write_tokens += cache_create
    breakdown.total_tokens += total
    breakdown.total_cost += cost

    context_window = 0
    for mu in (payload.get("modelUsage") or {}).values():
        if isinstance(mu, dict) and mu.get("contextWindow"):
            context_window = int(mu["contextWindow"])
            break
    # Occupancy estimate: last prompt-ish tokens still in window
    context_tokens = cache_read + cache_create + input_t
    return total, cost, context_tokens, context_window


def _build_cmd(request: PiRequest, claude_session: str, resume: bool) -> list[str]:
    model = resolve_model(request.model)
    effort = _EFFORT_MAP.get((request.thinking or "medium").lower(), "medium")
    cmd = [
        CLAUDE_PATH, "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
        "--effort", effort,
        # Factory phases need tools; acceptEdits matches a normal agent lane
        "--permission-mode", os.environ.get("SSSF_CLAUDE_PERMISSION_MODE", "acceptEdits"),
    ]
    if resume:
        cmd += ["--resume", claude_session]
    else:
        cmd += ["--session-id", claude_session]

    if request.system_prompt:
        # Full replace of default system prompt so SSSF agent identity sticks
        cmd += ["--system-prompt", request.system_prompt]

    if request.tools:
        # Claude expects space/comma separated tool names
        cmd += ["--allowedTools", ",".join(request.tools)]

    # Optional turn cap for cheap scout; builders leave unbounded unless set
    max_turns = os.environ.get("SSSF_CLAUDE_MAX_TURNS")
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]

    cmd.append(request.prompt)
    return cmd


def run(request: PiRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None,
        resume: bool = False) -> PiResult:
    """Run one non-interactive Claude Code turn (create or continue session)."""
    claude_session = session_uuid(request.session_id)
    cmd = _build_cmd(request, claude_session, resume=resume)

    raw_path = Path(request.raw_output_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    result = PiResult(session_id=request.session_id)
    tracker = ToolCallTracker()
    final_payload: dict = {}

    process = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        cwd=request.cwd,
        # #53192: operator_env(), not a raw os.environ.copy() — the agent
        # subprocess must never see the real repo's GIT_* vars once cwd is
        # routed to a disposable isolation copy. request.env, when the caller
        # supplied one (isolation.agent_env(), PWD/OLDPWD scrubbed too).
        env=request.env if request.env is not None else operator_env(),
        **agent_budget.popen_kwargs_for_tree_kill(),
    )
    # #53193: CREATE_SUSPENDED + atomic Job Object assignment + resume (see
    # agent_pi.run's matching comment); `kill_token` released exactly once
    # in the `finally` below, on every exit path. Both this and `on_spawn()`
    # run INSIDE the protected try below (declared here so the `finally`
    # always has a real value even if `track_for_kill` itself is what
    # raises) — see agent_pi.run's matching comment for why.
    kill_token: Optional[int] = None
    try:
        kill_token = agent_budget.track_for_kill(process.pid)
        if on_spawn:
            on_spawn(process.pid)
        deadline = (request.deadline if request.deadline is not None
                   else time.monotonic() + request.timeout_seconds)
        # Resume turns append; first create truncates so a phase file is one agent call stream
        raw_mode = "a" if resume else "w"
        with raw_path.open(raw_mode, encoding="utf-8") as raw:
            assert process.stdout is not None
            reader = agent_budget.TimeoutLineReader(process.stdout)
            try:
                for line in reader.lines(deadline, request.timeout_seconds):
                    raw.write(line)
                    raw.flush()
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Final result line (also appears as last event in stream-json)
                    if event.get("type") == "result":
                        final_payload = event
                        text = event.get("result") or ""
                        if isinstance(text, str):
                            result.text = text
                        tokens, cost, ctx_tok, ctx_win = _usage_from_result(event, result.usage)
                        result.tokens += tokens
                        result.cost += cost
                        if ctx_tok:
                            result.context_tokens = ctx_tok
                        if ctx_win:
                            result.context_window = ctx_win
                        if event.get("session_id"):
                            # Keep SSSF id in result.session_id; Claude uuid is in raw
                            pass

                    # Streaming assistant text (partial) — last full text wins if no result yet
                    if event.get("type") == "assistant" and not result.text:
                        message = event.get("message") or {}
                        parts = []
                        for block in message.get("content") or []:
                            if isinstance(block, dict) and block.get("type") == "text":
                                parts.append(block.get("text") or "")
                        if parts:
                            result.text = "".join(parts)

                    # Tool tracking for tracer
                    tool_record = tracker.observe(event)
                    if tool_record and on_event:
                        # agents._event_forwarder expects pi-shaped events; pass a synthetic
                        # tool_execution_end so the existing tracker path still works if used,
                        # OR emit directly via on_event with our record wrapped.
                        on_event({
                            "type": "tool_execution_end",
                            "toolCallId": tool_record.get("tool_call_id"),
                            "toolName": tool_record.get("tool"),
                            "args": tool_record.get("args"),
                            "isError": not tool_record.get("ok", True),
                            "result": {"content": [{"type": "text",
                                                    "text": tool_record.get("result_snippet", "")}]},
                            "_qn_tool_record": tool_record,
                        })
                    elif on_event:
                        on_event(event)
            except agent_budget.AgentInterrupt:
                # Same contract as agent_pi.run — see its docstring note.
                agent_budget.kill_process_tree(process.pid)
                reader.close()
                if on_exit:
                    on_exit(process.pid)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                raise

        stderr = process.stderr.read() if process.stderr else ""
        result.returncode = process.wait()
        if on_exit:
            on_exit(process.pid)

        # Claude sometimes exits non-zero after a usable stream (hooks, SIGPIPE on
        # early reader close). Prefer a parsed result text when present.
        if not result.text and final_payload.get("result"):
            text = final_payload.get("result")
            if isinstance(text, str):
                result.text = text

        if final_payload.get("is_error") and not result.text:
            err_bits = final_payload.get("errors") or final_payload.get("subtype") or final_payload.get("terminal_reason")
            raise RuntimeError(
                f"claude error: {err_bits!r} (returncode={result.returncode}; "
                f"stderr={(stderr or '').strip()[-400:]!r})"
            )
        if result.returncode != 0 and not result.text:
            raise RuntimeError(
                f"claude exited {result.returncode}: {(stderr or '').strip()[-800:]}"
            )
        return result
    except Exception:
        # Catches anything that escapes above without the process already
        # being handled (most notably track_for_kill/on_spawn raising).
        # kill_process_tree is a safe no-op on an already-dead pid.
        agent_budget.kill_process_tree(process.pid)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        agent_budget.release_tracking(process.pid, kill_token)


def run_continue(request: PiRequest, **kwargs) -> PiResult:
    """Correction / gate-retry turn — resume the same Claude session."""
    return run(request, resume=True, **kwargs)
