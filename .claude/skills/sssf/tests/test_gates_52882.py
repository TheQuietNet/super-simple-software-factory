"""#52882: fail-closed build gates + commit_paths in the skill templates.

Acceptance: claims_are_actually_modified / new_tests_are_discoverable exist
with no not-applicable branch; ests/-style near-miss fails; a claimed-but-
unmodified file fails; empty changed_files fails; commit_paths refuses an
empty change-set; a fresh stamp carries the port.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SKILL = Path(__file__).resolve().parents[1]
TEMPLATES_ADWS = SKILL / "templates" / "adws"
sys.path.insert(0, str(TEMPLATES_ADWS))

from adw_modules import gates, git_helper, quality  # noqa: E402
from adw_modules.data_types import BuildOutput  # noqa: E402

COMMITTING_ADWS = (
    "adw_plan_build.py",
    "adw_plan_build_test.py",
    "adw_plan_build_test_quality.py",
    "adw_simple_sdlc.py",
)


def _env(changed):
    return BuildOutput(status="success", summary="t", changed_files=changed)


def _git_repo(tmp: Path) -> Path:
    subprocess.run(["git", "init"], cwd=tmp, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp, check=True, capture_output=True)
    (tmp / "README").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=tmp, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp, check=True, capture_output=True)
    return tmp


def test_empty_changed_files_fails_build_gate():
    report = gates.claims_are_actually_modified(_env([]), SimpleNamespace(repo_root="."))
    assert not report.passed
    assert any(not c.ok and c.item == "changed_files" for c in report.checks)


def test_claimed_unmodified_file_fails(tmp_path: Path):
    repo = _git_repo(tmp_path)
    (repo / "pull-video.js").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "pull-video.js"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
    report = gates.claims_are_actually_modified(
        _env(["pull-video.js"]), SimpleNamespace(repo_root=str(repo)))
    assert not report.passed
    assert any("NO modification" in c.note for c in report.checks if not c.ok)


def test_claimed_dirty_file_passes(tmp_path: Path):
    repo = _git_repo(tmp_path)
    target = repo / "pull-video.js"
    target.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "pull-video.js"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
    target.write_text("new\n", encoding="utf-8")
    report = gates.claims_are_actually_modified(
        _env(["pull-video.js"]), SimpleNamespace(repo_root=str(repo)))
    assert report.passed


def test_ests_near_miss_fails_discoverability():
    report = gates.new_tests_are_discoverable(
        _env(["ests/pull-video-paragraphs.test.js"]), SimpleNamespace())
    assert not report.passed
    assert any("WILL NEVER RUN" in c.note for c in report.checks if not c.ok)


def test_discoverable_test_js_passes():
    """Passes when TEST_GLOB (tests/**/*.test.js) matches."""
    report = gates.new_tests_are_discoverable(
        _env(["tests/pull-video-paragraphs.test.js"]), SimpleNamespace())
    assert report.passed


def test_pytest_style_path_fails_against_node_test_glob():
    """F1: tests/test_foo.py is NOT collectable by tests/**/*.test.js.
    This is adw_id 615f4542 (tests/test_paragraphs.js, suite stayed 493/493)."""
    report = gates.new_tests_are_discoverable(
        _env(["tests/test_foo.py"]), SimpleNamespace())
    assert not report.passed
    assert any("WILL NEVER RUN" in c.note for c in report.checks if not c.ok)


def test_nested_test_js_matches_glob():
    report = gates.new_tests_are_discoverable(
        _env(["tests/nested/foo.test.js"]), SimpleNamespace())
    assert report.passed


def test_unknown_test_glob_fails_closed(monkeypatch):
    monkeypatch.setattr(quality, "TEST_GLOB", "")
    report = gates.new_tests_are_discoverable(
        _env(["tests/foo.test.js"]), SimpleNamespace())
    assert not report.passed
    assert any("PLACEHOLDER" in c.note or "missing" in c.note
               for c in report.checks if not c.ok)


def test_pytest_glob_allows_test_underscore_py(monkeypatch):
    monkeypatch.setattr(quality, "TEST_GLOB", "tests/test_*.py")
    report = gates.new_tests_are_discoverable(
        _env(["tests/test_foo.py"]), SimpleNamespace())
    assert report.passed
    report_js = gates.new_tests_are_discoverable(
        _env(["tests/foo.test.js"]), SimpleNamespace())
    assert not report_js.passed


def test_latest_dir_is_not_a_test():
    report = gates.new_tests_are_discoverable(
        _env(["src/latest/widget.js"]), SimpleNamespace())
    # src/latest/widget.js is not a test-like name, so #52939 (9d3e1718)
    # fail-closed: envelope claims no test file.
    assert not report.passed
    assert all(c.item != "src/latest/widget.js" for c in report.checks)


def test_new_untracked_nested_file_passes(tmp_path: Path):
    repo = _git_repo(tmp_path)
    nested = repo / "src" / "feature" / "foo.js"
    nested.parent.mkdir(parents=True)
    nested.write_text("x\n", encoding="utf-8")
    report = gates.claims_are_actually_modified(
        _env(["src/feature/foo.js"]), SimpleNamespace(repo_root=str(repo)))
    assert report.passed


def test_no_test_claim_is_still_a_check_not_not_applicable():
    report = gates.new_tests_are_discoverable(_env(["pull-video.js"]), SimpleNamespace())
    assert report.checks, "fail-closed: must record a check, not return empty/not-applicable"
    assert not report.passed  # #52939 9d3e1718: no test claimed is a failure
    src = ast.parse((TEMPLATES_ADWS / "adw_modules" / "gates.py").read_text(encoding="utf-8"))
    fn = next(n for n in src.body if isinstance(n, ast.FunctionDef) and n.name == "new_tests_are_discoverable")
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    # Fail-closed: every return is after report.check has run; never `return GateReport()`.
    for ret in returns:
        assert ret.value is not None
        assert not (isinstance(ret.value, ast.Call)
                    and isinstance(ret.value.func, ast.Name)
                    and ret.value.func.id == "GateReport")


def test_commit_paths_refuses_empty_set(tmp_path: Path, monkeypatch):
    repo = _git_repo(tmp_path)
    monkeypatch.chdir(repo)
    with pytest.raises(RuntimeError, match="changed nothing"):
        git_helper.commit_paths("x", [])


def test_commit_paths_stages_only_listed(tmp_path: Path, monkeypatch):
    repo = _git_repo(tmp_path)
    (repo / "agent.js").write_text("a\n", encoding="utf-8")
    (repo / "operator.js").write_text("o\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    sha = git_helper.commit_paths("agent only", ["agent.js"])
    assert sha
    names = subprocess.check_output(
        ["git", "show", "--name-only", "--pretty=", "HEAD"], cwd=repo, text=True)
    assert "agent.js" in names
    assert "operator.js" not in names


def test_four_adws_call_commit_paths_and_drain():
    for name in COMMITTING_ADWS:
        text = (TEMPLATES_ADWS / name).read_text(encoding="utf-8")
        assert "git_helper.commit_paths" in text, name
        assert "run.agent_touched_paths = []" in text, name
        assert "commit_all(" not in text, name


def test_agents_accumulates_touched_paths():
    text = (TEMPLATES_ADWS / "adw_modules" / "agents.py").read_text(encoding="utf-8")
    assert "run.agent_touched_paths" in text
    assert "permissions.enforce" in text


def test_fresh_stamp_carries_ported_gates(tmp_path: Path):
    scratch = tmp_path / "stamped"
    scratch.mkdir()
    subprocess.run(["git", "init"], cwd=scratch, check=True, capture_output=True)
    install = SKILL / "scripts" / "install.py"
    subprocess.run([sys.executable, str(install)], cwd=scratch, check=True, capture_output=True)
    stamped_gates = (scratch / "adws" / "adw_modules" / "gates.py").read_text(encoding="utf-8")
    assert "def claims_are_actually_modified" in stamped_gates
    assert "def new_tests_are_discoverable" in stamped_gates
    assert "not-applicable" in stamped_gates  # docstring of the defect, not a branch
    stamped_git = (scratch / "adws" / "adw_modules" / "git_helper.py").read_text(encoding="utf-8")
    assert "def commit_paths" in stamped_git
    stamped_agents = (scratch / "adws" / "adw_modules" / "agents.py").read_text(encoding="utf-8")
    assert "agent_touched_paths" in stamped_agents
    for name in COMMITTING_ADWS:
        text = (scratch / "adws" / name).read_text(encoding="utf-8")
        assert "commit_paths" in text, name
        assert "agent_touched_paths = []" in text, name
    stamped_quality = (scratch / "adws" / "adw_modules" / "quality.py").read_text(encoding="utf-8")
    assert "TEST_GLOB" in stamped_quality
