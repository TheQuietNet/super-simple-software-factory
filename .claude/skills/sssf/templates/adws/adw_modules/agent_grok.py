"""Grok CLI interface (mirrors agent_cc.run).

Headless: `grok -p --output-format json --always-approve`.
Session ids are UUID5 of the SSSF session string (Grok requires a UUID).
`--resume` continues the same window for JSON/gate corrections.
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
from .utils import operator_env

GROK_PATH = os.environ.get("GROK_PATH", "grok")
_SESSION_NS = uuid.UUID("b8e4d3f2-6c05-4e7b-af19-2d3e4f5a6b7c")

_EFFORT_MAP = {
    "off": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


def session_uuid(sssf_session_id: str) -> str:
    return str(uuid.uuid5(_SESSION_NS, sssf_session_id))


def resolve_model(pattern: str) -> str:
    """Pass roster model through. Empty is invalid at validate()."""
    return (pattern or "").strip()


def _build_cmd(request: PiRequest, grok_session: str, resume: bool,
               prompt_file: Path) -> list[str]:
    effort = _EFFORT_MAP.get((request.thinking or "medium").lower(), "medium")
    cmd = [
        GROK_PATH,
        "--prompt-file", str(prompt_file),
        "--output-format", "json",
        "--always-approve",
        "--cwd", request.cwd,
        "--reasoning-effort", effort,
    ]
    if request.model:
        cmd += ["--model", resolve_model(request.model)]
    if resume:
        cmd += ["--resume", grok_session]
    else:
        cmd += ["--session-id", grok_session]
    if request.system_prompt:
        cmd += ["--system-prompt-override", request.system_prompt]
    if request.tools:
        cmd += ["--tools", ",".join(request.tools)]
    return cmd


def _text_from_payload(payload: dict) -> str:
    for key in ("result", "text", "content", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            nested = _text_from_payload(value)
            if nested:
                return nested
        if isinstance(value, list):
            parts = []
            for block in value:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text") or "")
                elif isinstance(block, str):
                    parts.append(block)
            joined = "".join(parts).strip()
            if joined:
                return joined
    return ""


def _usage_from_payload(payload: dict, breakdown: UsageBreakdown) -> tuple[int, float]:
    usage = payload.get("usage") or {}
    input_t = int(usage.get("input_tokens") or usage.get("inputTokens") or 0)
    output_t = int(usage.get("output_tokens") or usage.get("outputTokens") or 0)
    total = input_t + output_t
    cost = float(payload.get("total_cost_usd") or usage.get("cost") or 0.0)
    breakdown.input_tokens += input_t
    breakdown.output_tokens += output_t
    breakdown.total_tokens += total
    breakdown.total_cost += cost
    return total, cost


def _parse_stdout(stdout: str) -> dict:
    text = (stdout or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    last: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            last = event
    return last


def run(request: PiRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None,
        resume: bool = False) -> PiResult:
    """Run one non-interactive Grok turn (create or continue session)."""
    grok_session = session_uuid(request.session_id)
    session_dir = Path(request.session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = session_dir / f"{request.session_id}.prompt.md"
    prompt_file.write_text(request.prompt, encoding="utf-8")
    cmd = _build_cmd(request, grok_session, resume=resume, prompt_file=prompt_file)

    raw_path = Path(request.raw_output_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    result = PiResult(session_id=request.session_id)
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=request.cwd,
        # request.env when the caller supplied one (isolation.agent_env(),
        # PWD/OLDPWD scrubbed too), else the old default.
        env=request.env if request.env is not None else operator_env(),
        **agent_budget.popen_kwargs_for_tree_kill(),
    )
    # See agent_pi.run's matching comment — CREATE_SUSPENDED + atomic Job
    # Object assignment + resume; `kill_token` released exactly once in the
    # `finally` below, on every exit path. Both this and `on_spawn()` run
    # INSIDE the protected try below — see agent_pi.run's matching comment
    # for why.
    kill_token: Optional[int] = None
    try:
        kill_token = agent_budget.track_for_kill(process.pid)
        if on_spawn:
            on_spawn(process.pid)
        # Absolute, sub-second-precise deadline, same as pi/claude_code —
        # but `communicate(timeout=...)` takes a RELATIVE seconds value, so
        # the absolute deadline is converted to "however much is left" as
        # LATE as possible (right here, immediately before the blocking
        # call) rather than earlier, to minimize skew. Never floored to an
        # int — communicate() accepts a float natively.
        deadline = (request.deadline if request.deadline is not None
                   else time.monotonic() + request.timeout_seconds)
        remaining = max(0.0, deadline - time.monotonic())
        try:
            stdout, stderr = process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            # grok's CLI is not streamed (communicate() blocks for the whole
            # turn), so a hang here can only be caught as a total wall-clock
            # budget — not mid-turn like pi/claude_code's line-by-line loop.
            # Same kill-tree + close-processes-row contract as those two.
            agent_budget.kill_process_tree(process.pid)
            if on_exit:
                on_exit(process.pid)
            try:
                process.communicate(timeout=5)
            except (subprocess.TimeoutExpired, ValueError):
                pass
            raise agent_budget.PhaseTimeout(request.timeout_seconds)
        if on_exit:
            on_exit(process.pid)
        result.returncode = process.returncode or 0

        raw_mode = "a" if resume else "w"
        with raw_path.open(raw_mode, encoding="utf-8") as raw:
            raw.write(stdout or "")
            if stderr:
                raw.write("\n")
                raw.write(stderr)

        payload = _parse_stdout(stdout or "")
        if on_event and payload:
            on_event(payload)
        result.text = _text_from_payload(payload)
        tokens, cost = _usage_from_payload(payload, result.usage)
        result.tokens += tokens
        result.cost += cost

        if result.returncode != 0 and not result.text:
            raise RuntimeError(
                f"grok exited {result.returncode}: {(stderr or '').strip()[-800:]}"
            )
        return result
    except Exception:
        # See agent_pi.run's matching comment — catches anything that
        # escapes above without the process already being handled (most
        # notably track_for_kill/on_spawn raising, or the inner
        # TimeoutExpired handler's own PhaseTimeout re-raise).
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
    return run(request, resume=True, **kwargs)
