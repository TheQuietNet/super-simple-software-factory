"""adw_ticket / adw_modules.ticket: board -> factory entry point.

Every agentctl/git/gh call goes through injectable callables — nothing here
shells out to a real board or repo unless a test explicitly wires a real
default in.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from adw_modules import ticket


def _fake_run(stdout: str = "", returncode: int = 0):
    def run(argv):
        return subprocess.CompletedProcess(argv, returncode, stdout, "")
    return run


# ── configurable AGENTCTL / WORKTREE_ROOT — never a hardcoded machine path ──

def test_agentctl_is_never_a_hardcoded_windows_user_path():
    """The board-CLI invocation must come from SSSF_AGENTCTL_CMD, not a
    baked-in C:\\Users\\... path — a stamped repo has no way to know where
    one specific operator's agentctl install lives."""
    for token in ticket.AGENTCTL:
        assert "C:\\Users" not in token
        assert "/Users/" not in token or "chris" not in token.lower()


def test_agentctl_respects_the_env_var_override(monkeypatch):
    import importlib
    monkeypatch.setenv("SSSF_AGENTCTL_CMD", "py -3 C:/example/agentctl.py")
    importlib.reload(ticket)
    try:
        assert ticket.AGENTCTL == ["py", "-3", "C:/example/agentctl.py"]
    finally:
        monkeypatch.delenv("SSSF_AGENTCTL_CMD", raising=False)
        importlib.reload(ticket)


def test_worktree_root_respects_the_env_var_override(monkeypatch):
    import importlib
    monkeypatch.setenv("SSSF_WORKTREE_ROOT", "/tmp/example-wt")
    importlib.reload(ticket)
    try:
        assert str(ticket.WORKTREE_ROOT).replace("\\", "/") == "/tmp/example-wt"
    finally:
        monkeypatch.delenv("SSSF_WORKTREE_ROOT", raising=False)
        importlib.reload(ticket)


# ── footprint -> Where: mapping ────────────────────────────────────────────────

def test_footprint_entry_inside_repo_maps_repo_relative(tmp_path: Path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "x.js").write_text("x", encoding="utf-8")
    mapping = ticket.map_footprint([str(tmp_path / "lib" / "x.js")], tmp_path)
    assert mapping.kept == ["lib/x.js"]
    assert not mapping.dropped


def test_vault_pointer_entry_is_dropped():
    mapping = ticket.map_footprint(["vault:Some Note.md"], Path("/repo"))
    assert mapping.kept == []
    assert "vault:Some Note.md" in mapping.dropped[0]


def test_parent_traversal_escape_is_dropped_not_rewritten(tmp_path: Path):
    """A relative footprint entry like `../outside.js` must be DROPPED, never
    silently rewritten into a fake in-repo-looking token."""
    outside = tmp_path.parent / "outside.js"
    mapping = ticket.map_footprint(["../outside.js"], tmp_path)
    assert mapping.kept == []
    assert mapping.dropped


def test_backslash_traversal_escape_is_also_dropped(tmp_path: Path):
    mapping = ticket.map_footprint(["..\\outside.js"], tmp_path)
    assert mapping.kept == []


def test_repo_root_itself_names_no_file(tmp_path: Path):
    mapping = ticket.map_footprint([str(tmp_path)], tmp_path)
    assert mapping.kept == []


def test_directory_entry_gets_a_trailing_slash(tmp_path: Path):
    (tmp_path / "lib").mkdir()
    mapping = ticket.map_footprint([str(tmp_path / "lib")], tmp_path)
    assert mapping.kept == ["lib/"]


# ── draft: build_ask / build_out_of_scope ────────────────────────────────────

def test_build_ask_combines_title_and_body():
    assert ticket.build_ask("Fix the thing", "It is broken") == "Fix the thing — It is broken"


def test_build_ask_drops_a_trivial_promoted_from_finding_body():
    assert ticket.build_ask("Fix the thing", "Promoted from finding #123.") == "Fix the thing"


def test_build_ask_avoids_duplicating_when_body_contains_title():
    assert ticket.build_ask("Fix", "Fix the thing properly") == "Fix the thing properly"


def test_build_out_of_scope_finds_an_explicit_section():
    body = "Do the thing.\nOut of scope: unrelated cleanup.\n"
    assert ticket.build_out_of_scope(body, "") == "unrelated cleanup."


def test_build_out_of_scope_falls_back_to_a_placeholder():
    assert "reviewer" in ticket.build_out_of_scope("no scope note here", "")


# ── draft(): protected-path refusal, empty-footprint refusal ────────────────

def test_draft_refuses_when_nothing_in_footprint_maps(tmp_path: Path):
    run = _fake_run('{"task": {"title": "t", "body": "b", "footprint": ["vault:x"]}}')
    with pytest.raises(ticket.DraftRefused):
        ticket.draft(1, repo_root=tmp_path, run=run)


def test_draft_writes_the_four_line_shape(tmp_path: Path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "x.js").write_text("x", encoding="utf-8")
    task = {
        "title": "Add a helper",
        "body": "Out of scope: the caller.",
        "footprint": [str(tmp_path / "lib" / "x.js")],
        "acceptance": "a test exists",
    }
    import json
    run = _fake_run(json.dumps({"task": task}))
    result = ticket.draft(1, repo_root=tmp_path, run=run)
    assert result.path == tmp_path / "requests" / "1.md"
    assert "Where: lib/x.js" in result.text
    assert "Done means: a test exists" in result.text
    assert "Out of scope: the caller." in result.text


def test_draft_refuses_when_mapped_token_is_protected(tmp_path: Path):
    (tmp_path / "adws").mkdir()
    (tmp_path / "adws" / "adw_modules").mkdir()
    cfg_dir = tmp_path / "adws" / "adw_sssf_config"
    cfg_dir.mkdir()
    (cfg_dir / "sssf.config.yaml").write_text(
        "defaults:\n  protected_files:\n    - adws/adw_modules/\n", encoding="utf-8")
    target = tmp_path / "adws" / "adw_modules" / "gates.py"
    target.write_text("x", encoding="utf-8")
    import json
    task = {"title": "t", "body": "", "footprint": [str(target)]}
    run = _fake_run(json.dumps({"task": task}))
    with pytest.raises(ticket.DraftRefused, match="protected_files"):
        ticket.draft(1, repo_root=tmp_path, run=run,
                     config="adws/adw_sssf_config/sssf.config.yaml")


# ── worktree_path / branch_name / session_label ──────────────────────────────

def test_worktree_path_and_branch_name():
    root = Path("/wt")
    assert ticket.worktree_path(Path("/repos/myrepo"), 53195, root) == root / "myrepo-53195-sssf"
    assert ticket.branch_name(53195) == "claude/53195-sssf"
    assert ticket.session_label("agent-x", 53195) == "agent-x#sssf-53195"


def test_chain_config_rejects_an_unknown_chain(tmp_path: Path):
    with pytest.raises(ValueError):
        ticket.ChainConfig(task_id=1, repo_root=tmp_path, chain="not-a-real-chain")


def test_chain_config_defaults_msg_to_to_the_agent(tmp_path: Path):
    cfg = ticket.ChainConfig(task_id=1, repo_root=tmp_path, agent="agent-x")
    assert cfg.msg_to == "agent-x"
