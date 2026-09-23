"""TypeSafe Jev semantic check for SSSF builder phases.

Opt-in via SSSF_JEV=1. Skip (never block) if the flag is off, the API key is
missing, or the hosted call fails. Fail-closed only on a high-confidence
reject.

This is a gates.py-class function, not a coding_agent. Mechanical BUILDER_GATES
still run first; this complements them with ask-vs-diff.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/")
MODEL = os.environ.get("TYPESAFE_MODEL", "jev-1.13.0")
MAX_PATCH = 20000
REJECT_CONFIDENCE = 0.7
REJECT_MEETS = 0.3

QUESTIONS = {
    "meets_request": {
        "type": "noul",
        "instructions": (
            "Given ask, plan, builder envelope, and the live git patch, "
            "did the implementation satisfy the original ask on the real target files?"
        ),
    },
    "accept": {
        "type": "choice",
        "instructions": (
            "What should the factory do next? approve = land it; "
            "correct_in_session = send the builder back; human = stop for a person. "
            "Prefer correct_in_session when the ask was not met on the real files "
            "even if the envelope says success."
        ),
        "criteria": {
            "approve": "The ask is met on the real files and claims match the patch",
            "correct_in_session": "Builder should retry; missing, misplaced, or untested work",
            "human": "Not enough evidence, harness flake, or a mechanical issue code already owns",
        },
    },
}


def enabled() -> bool:
    return os.environ.get("SSSF_JEV", "").strip().lower() in ("1", "true", "yes")


def api_key() -> str:
    return os.environ.get("TYPESAFE_API_KEY", "").strip()


def high_conf_reject(answers: dict) -> bool:
    accept = answers.get("accept") or {}
    meets = float((answers.get("meets_request") or {}).get("noul") or 0)
    if accept.get("choice") == "approve":
        return False
    return float(accept.get("confidence") or 0) >= REJECT_CONFIDENCE or meets <= REJECT_MEETS


def _git_diff(repo_root: str, paths: list[str]) -> str:
    if not paths:
        return ""
    r = subprocess.run(
        ["git", "diff", "--", *paths],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", *paths],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    chunks = [r.stdout or ""]
    for rel in (untracked.stdout or "").splitlines():
        rel = rel.strip()
        if not rel:
            continue
        body = ""
        try:
            body = (Path(repo_root) / rel).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            continue
        posix = rel.replace("\\", "/")
        chunks.append(
            f"diff --git a/{posix} b/{posix}\n"
            f"new file mode 100644\n--- /dev/null\n+++ b/{posix}\n"
            + "".join(f"+{ln}" if ln.endswith("\n") else f"+{ln}\n"
                      for ln in body.splitlines(True))
        )
    out = "\n".join(c for c in chunks if c).strip()
    if len(out) > MAX_PATCH:
        out = out[:MAX_PATCH] + "\n...[truncated]..."
    return out


def collect_state(envelope, run) -> dict:
    claimed = [
        str(p).replace("\\", "/")
        for p in (getattr(envelope, "changed_files", []) or [])
    ]
    plan = ""
    handoff = getattr(run, "context_handoff_dir", None)
    if handoff:
        p = Path(handoff) / "plan.md"
        if p.is_file():
            plan = p.read_text(encoding="utf-8", errors="replace")[:2000]
    patch = _git_diff(str(getattr(run, "repo_root", ".")), claimed)
    return {
        "ask": (getattr(run, "request", None) or "")[:2000],
        "plan_summary": plan,
        "builder_envelope": {
            "status": getattr(envelope, "status", None),
            "summary": getattr(envelope, "summary", None),
            "changed_files": claimed,
        },
        "live_git_patch": patch or "(no product patch)",
    }


def evaluate(state: dict) -> dict:
    """POST /v1/systemone. Raises on HTTP errors so the gate can skip."""
    key = api_key()
    if not key:
        raise RuntimeError("TYPESAFE_API_KEY missing")
    body = {"state": state, "model": MODEL, "questions": QUESTIONS}
    req = urllib.request.Request(
        f"{API}/v1/systemone",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"TypeSafe HTTP {exc.code}: {err}") from exc
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise RuntimeError("TypeSafe response missing answers")
    return answers
