"""Unit tests for the Grok coding-agent adapter. No live grok process."""
from __future__ import annotations

import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "templates" / "adws"))

from adw_modules import agent_grok
from adw_modules.data_types import PiRequest


def _request(tmp: Path) -> PiRequest:
    return PiRequest(
        prompt="implement the plan",
        system_prompt="you are the builder",
        model="grok-4",
        thinking="off",
        session_id="sssf-abcd-builder-1",
        session_dir=str(tmp / "sess"),
        raw_output_path=str(tmp / "raw.jsonl"),
        tools=["read", "bash", "write"],
        cwd=str(tmp),
    )


def test_session_uuid_is_stable():
    a = agent_grok.session_uuid("sssf-abcd-builder-1")
    b = agent_grok.session_uuid("sssf-abcd-builder-1")
    c = agent_grok.session_uuid("other")
    assert a == b
    assert a != c
    assert len(a) == 36


def test_build_cmd_create_vs_resume(tmp_path: Path):
    req = _request(tmp_path)
    prompt_file = tmp_path / "p.md"
    prompt_file.write_text("hi", encoding="utf-8")
    sid = agent_grok.session_uuid(req.session_id)
    create = agent_grok._build_cmd(req, sid, resume=False, prompt_file=prompt_file)
    resume = agent_grok._build_cmd(req, sid, resume=True, prompt_file=prompt_file)
    assert create[0] == "grok"
    assert "--prompt-file" in create
    assert "--output-format" in create and "json" in create
    assert "--always-approve" in create
    assert "--session-id" in create and sid in create
    assert "--resume" not in create
    assert "--resume" in resume and sid in resume
    assert "--session-id" not in resume
    assert "--model" in create and "grok-4" in create
    assert "--tools" in create


def test_text_from_payload_variants():
    assert agent_grok._text_from_payload({"result": "hello"}) == "hello"
    assert agent_grok._text_from_payload(
        {"content": [{"type": "text", "text": "hi"}]}
    ) == "hi"
    assert agent_grok._text_from_payload({}) == ""


def test_run_parses_json_stdout(tmp_path: Path, monkeypatch):
    req = _request(tmp_path)
    payload = (
        '{"result": "{\\"status\\": \\"success\\"}", '
        '"usage": {"input_tokens": 10, "output_tokens": 5}}'
    )

    class FakeProc:
        def __init__(self):
            self.pid = 4242
            self.returncode = 0
            self.stdout = None
            self.stderr = None

        def communicate(self):
            return payload, ""

    def fake_popen(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(agent_grok.subprocess, "Popen", fake_popen)
    result = agent_grok.run(req)
    assert result.returncode == 0
    assert "success" in result.text
    assert result.tokens == 15
    raw = Path(req.raw_output_path).read_text(encoding="utf-8")
    assert "success" in raw


def test_coding_agent_literal_accepts_grok():
    from adw_modules.data_types import AgentConfig, PromptEngineering
    agent = AgentConfig(
        name="builder",
        coding_agent="grok",
        model="grok-4",
        prompt_engineering=PromptEngineering(system="s.md", user="u.md"),
    )
    assert agent.coding_agent == "grok"
