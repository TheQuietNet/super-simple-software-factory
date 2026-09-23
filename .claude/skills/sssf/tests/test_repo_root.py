"""repo_root() must derive from the ADW's own script location, not cwd.

Launching an ADW while the process cwd happens to be a DIFFERENT git repo
(any unrelated checkout on the same machine) must not make that other repo
the resolved `run.repo_root` — where every agent is spawned to work — with
only a downstream config-validation failure keeping agents from actually
being spawned against it.

`git_helper.repo_root()` resolves from THIS MODULE's own file location
(`git -C <adw_modules dir> rev-parse --show-toplevel`), so it is invariant to
cwd. Two layers refuse loudly, before any phase opens, when cwd resolves to
some OTHER real git repo:
  1. `session.ensure()` — the real ADW entry point, checked FIRST, before
     Tracer() (the first real filesystem/db side effect) ever runs.
  2. `Run.__init__` — defense in depth for code that constructs a `Run`
     directly, bypassing `session.ensure()`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adw_modules import git_helper, session
from adw_modules.data_types import ConfigDefaults, ObservabilityConfig, SSSFConfig
from adw_modules.runner import Run
from adw_modules.tracer import Tracer
from conftest import TEMPLATES_ADWS, _git

THIS_REPO = git_helper.repo_root()   # the real repo this checkout lives in


def _other_repo(tmp_path: Path) -> Path:
    """A real, unrelated git repo — stands in for a different checkout."""
    _git(["init", "-q", "-b", "main"], tmp_path)
    _git(["config", "user.email", "test@example.invalid"], tmp_path)
    _git(["config", "user.name", "adw tests"], tmp_path)
    (tmp_path / "note.md").write_text("not the ADW repo\n", encoding="utf-8")
    _git(["add", "note.md"], tmp_path)
    _git(["commit", "-qm", "seed"], tmp_path)
    return tmp_path


# ── git_helper.repo_root() itself ────────────────────────────────────────────

def test_repo_root_resolves_to_the_script_repo_regardless_of_cwd(tmp_path, monkeypatch):
    """MUTATION BAR: revert repo_root() to a bare `git rev-parse` off cwd and this fails."""
    other = _other_repo(tmp_path)
    monkeypatch.chdir(other)
    resolved = git_helper.repo_root()
    assert resolved == THIS_REPO
    assert resolved != other.resolve()


def test_repo_root_is_unaffected_by_a_non_git_cwd(tmp_path, monkeypatch):
    """cwd with no git repo at all (the ordinary 'launched from a non-git scratch dir' case)."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.chdir(scratch)
    assert git_helper.repo_root() == THIS_REPO


# ── Run.__init__: refuse loudly before any phase opens ───────────────────────

def _cfg(tmp_path: Path) -> SSSFConfig:
    return SSSFConfig(
        defaults=ConfigDefaults(data_dir=str(tmp_path / "adw_data")),
        observability=ObservabilityConfig(db=str(tmp_path / "sssf.db")),
    )


def _build_run(tmp_path: Path, adw_id: str = "trepo") -> Run:
    tracer = Tracer(str(tmp_path / "sssf.db"), str(tmp_path / "events.jsonl"))
    return Run(cfg=_cfg(tmp_path), adw_id=adw_id, tracer=tracer, engineer="test")


def test_run_refuses_loudly_when_cwd_is_a_different_repo(tmp_path, monkeypatch):
    other = _other_repo(tmp_path)
    monkeypatch.chdir(other)
    with pytest.raises(RuntimeError, match="refusing to start"):
        _build_run(tmp_path)


def test_run_succeeds_when_cwd_is_not_in_any_git_repo(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.chdir(scratch)
    run = _build_run(tmp_path)
    assert run.repo_root == THIS_REPO


def test_run_succeeds_when_cwd_is_inside_the_scripts_own_repo(monkeypatch):
    monkeypatch.chdir(TEMPLATES_ADWS)
    assert git_helper.repo_root() == THIS_REPO
    git_helper.assert_cwd_matches_repo(THIS_REPO)   # must not raise


# ── session.ensure(): the REAL entry point, guarded before ANY side effect ──

def test_session_ensure_refuses_before_creating_anything_in_the_wrong_repo(tmp_path, monkeypatch):
    """Reproduced live on the pilot: cwd in an unrelated repo left a genuine,
    populated `<other-repo>/adws/adw_data/sssf.db` on disk before the
    eventual RuntimeError, because `Tracer()` — the first real side effect —
    used to run before `Run()` (where the guard lived, alone) was ever
    constructed. `session.ensure()`'s guard now runs FIRST, before anything
    is written.

    `SSSFConfig()`'s bare (relative) defaults are exactly what a real ADW
    uses, so this checks that no `adws/` directory was created in the
    mismatched repo — not just that the exception was raised.
    """
    other = _other_repo(tmp_path)
    monkeypatch.chdir(other)
    with pytest.raises(RuntimeError, match="refusing to start"):
        session.ensure(SSSFConfig())
    assert not (other / "adws").exists(), (
        "session.ensure() must not create ANY file or directory in a "
        "mismatched repo before the cwd guard fires")
