"""Agent phases run against a disposable COPY of the repo, never the real
worktree — the durable fix for a builder with bash access defeating
permissions.py through git plumbing itself.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from adw_modules import isolation, permissions
from conftest import FakeRun, agent, config, dirty


@pytest.fixture(autouse=True)
def _isolated_root(tmp_path_factory, monkeypatch):
    """Every test gets its own isolation root — never the shared system temp
    dir (so parallel test runs and reruns never collide on a stale copy),
    and deliberately NOT nested under the `repo` fixture's own `tmp_path`
    (a `tmp_path`-derived subdirectory of the real repo would make every
    isolation-copy path contain the repo path as a literal prefix, which
    defeats the point of several assertions below)."""
    monkeypatch.setenv(isolation.ROOT_OVERRIDE_ENV,
                       str(tmp_path_factory.mktemp("iso-root")))


def _run(repo: Path, adw_id: str = "tiso") -> FakeRun:
    run = FakeRun(repo, adw_id=adw_id)
    run.cfg = config(protected=["adws/adw_modules/"])
    return run


def test_prepare_creates_a_disposable_copy_with_its_own_git(repo: Path):
    run = _run(repo)
    builder = agent("builder", writes=None)
    iso = isolation.prepare(run, builder)
    assert iso.copy_root.is_dir()
    assert (iso.copy_root / ".git").exists()
    assert (iso.copy_root / "seed.js").is_file()
    # A brand-new, unrelated repo — never the same git dir as the real one.
    assert (iso.copy_root / ".git").resolve() != (repo / ".git").resolve()


def test_scrub_git_env_strips_every_git_prefixed_var():
    env = {"GIT_DIR": "/x", "GIT_WORK_TREE": "/y", "PATH": "/bin", "OTHER": "1"}
    scrubbed = isolation.scrub_git_env(env)
    assert "GIT_DIR" not in scrubbed
    assert "GIT_WORK_TREE" not in scrubbed
    assert scrubbed["PATH"] == "/bin"
    assert scrubbed["OTHER"] == "1"


def test_apply_back_applies_a_permitted_edit_into_the_real_tree(repo: Path):
    run = _run(repo)
    builder = agent("builder", writes=None)
    iso = isolation.prepare(run, builder)
    (iso.copy_root / "seed.js").write_text("// agent edit\n", encoding="utf-8")
    result = isolation.apply_back(run, builder, iso)
    assert "seed.js" in result.applied
    assert (repo / "seed.js").read_text(encoding="utf-8") == "// agent edit\n"


def test_apply_back_rejects_an_edit_to_a_protected_path(repo: Path):
    run = _run(repo)
    (repo / "adws").mkdir()
    (repo / "adws" / "adw_modules").mkdir()
    (repo / "adws" / "adw_modules" / "gates.py").write_text("orig\n", encoding="utf-8")
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed adws"], cwd=repo, check=True, capture_output=True)

    builder = agent("builder", writes=None)
    iso = isolation.prepare(run, builder)
    (iso.copy_root / "adws" / "adw_modules" / "gates.py").write_text(
        "tampered\n", encoding="utf-8")
    result = isolation.apply_back(run, builder, iso)
    assert "adws/adw_modules/gates.py" in result.rejected
    assert (repo / "adws" / "adw_modules" / "gates.py").read_text(
        encoding="utf-8") == "orig\n"


def test_apply_back_requires_scope_for_a_builder_class_call(repo: Path):
    """A builder-class call (require_scope=True) must additionally stay
    inside the PINNED requested scope, not just permissions.permitted()."""
    run = _run(repo)
    run.request = "Add a helper.\nWhere: query.js\n"
    builder = agent("builder", writes=None)
    iso = isolation.prepare(run, builder)
    (iso.copy_root / "query.js").write_text("in scope\n", encoding="utf-8")
    (iso.copy_root / "lib.js").write_text("out of scope\n", encoding="utf-8")
    result = isolation.apply_back(run, builder, iso, require_scope=True)
    assert "query.js" in result.applied
    assert "lib.js" in result.rejected
    assert not (repo / "lib.js").exists()


def test_finalize_raises_when_something_stays_rejected(repo: Path):
    run = _run(repo)
    (repo / "adws").mkdir()
    (repo / "adws" / "adw_modules").mkdir()
    (repo / "adws" / "adw_modules" / "gates.py").write_text("orig\n", encoding="utf-8")
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed adws"], cwd=repo, check=True, capture_output=True)

    builder = agent("builder", writes=None)
    iso = isolation.prepare(run, builder)
    (iso.copy_root / "adws" / "adw_modules" / "gates.py").write_text(
        "tampered\n", encoding="utf-8")
    with pytest.raises(permissions.PermissionBreach):
        isolation.finalize(run, builder, iso)


def test_agent_env_never_leaks_the_real_repo_root_in_pwd(repo: Path):
    run = _run(repo)
    builder = agent("builder", writes=None)
    iso = isolation.prepare(run, builder)
    import os
    old_pwd = os.environ.get("PWD")
    try:
        os.environ["PWD"] = str(repo)
        env = isolation.agent_env(run, iso)
    finally:
        if old_pwd is None:
            os.environ.pop("PWD", None)
        else:
            os.environ["PWD"] = old_pwd
    if "PWD" in env:
        assert str(repo) not in env["PWD"].replace("\\", "/")


def test_prepare_reuses_an_existing_copy_across_calls(repo: Path):
    run = _run(repo)
    builder = agent("builder", writes=None)
    iso1 = isolation.prepare(run, builder)
    marker = iso1.copy_root / "agent_only.txt"
    marker.write_text("still here?\n", encoding="utf-8")
    iso2 = isolation.prepare(run, builder)
    assert iso2.copy_root == iso1.copy_root
    assert marker.is_file(), "a resumed prepare() must not wipe the agent's own in-flight edits"


def test_cleanup_run_removes_the_copy_on_success(repo: Path):
    run = _run(repo)
    builder = agent("builder", writes=None)
    iso = isolation.prepare(run, builder)
    assert iso.copy_root.exists()
    isolation.cleanup_run(run, ok=True)
    assert not iso.copy_root.exists()
