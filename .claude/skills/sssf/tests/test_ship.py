"""ship.py: post-green closeout — commit, push, PR, review routing.

DEFAULT_REPO/AGENTCTL must come from env, never a hardcoded org/repo or
machine path — a stamped repo has no way to know either.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from adw_modules import gates, git_helper, ship
from conftest import FakeRun, dirty


def test_default_repo_is_never_hardcoded_to_a_specific_org():
    assert "TheQuietNet" not in ship.DEFAULT_REPO
    assert "youtube-watch-history" not in ship.DEFAULT_REPO


def test_default_repo_respects_the_env_var_override(monkeypatch):
    import importlib
    monkeypatch.setenv("SSSF_SHIP_REPO", "example-org/example-repo")
    importlib.reload(ship)
    try:
        assert ship.DEFAULT_REPO == "example-org/example-repo"
    finally:
        monkeypatch.delenv("SSSF_SHIP_REPO", raising=False)
        importlib.reload(ship)


def test_pr_body_refuses_an_empty_task_list():
    with pytest.raises(ValueError):
        ship.pr_body([])


def test_pr_body_cites_every_task_id():
    body = ship.pr_body([1, 2], "did the thing")
    assert "task #1" in body and "task #2" in body
    assert "did the thing" in body


def test_pr_title_clips_a_long_summary():
    title = ship.pr_title([1], "x" * 200)
    assert title.startswith("task #1: ")
    assert len(title) <= 72 + len("task #1: ")


def test_assert_not_main_refuses_main_and_master():
    with pytest.raises(RuntimeError):
        ship.assert_not_main("main")
    with pytest.raises(RuntimeError):
        ship.assert_not_main("master")
    ship.assert_not_main("claude/53196-port")   # must not raise


def test_commit_green_stages_only_touched_paths(repo: Path, monkeypatch):
    monkeypatch.chdir(repo)
    import subprocess
    subprocess.run(["git", "checkout", "-q", "-b", "claude/1-sssf"], cwd=repo, check=True)

    class Run:
        repo_root = str(repo)
        request = "Fix the thing.\nWhere: lib/x.js\n"
        context_handoff_dir = repo / "adws" / "adw_data" / "sessions" / "x" / "context_handoff"

    dirty(repo, "lib/x.js")
    dirty(repo, "lib/y.js")   # out of scope — should be excluded
    run = Run()
    run.agent_touched_paths = ["lib/x.js", "lib/y.js"]
    ship.commit_green(run, "task #1: fix the thing")
    names = subprocess.check_output(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
        cwd=repo, text=True, encoding="utf-8", errors="replace")
    assert "lib/x.js" in names
    assert "lib/y.js" not in names
    assert run.agent_touched_paths == []
