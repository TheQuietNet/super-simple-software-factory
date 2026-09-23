"""Content-fingerprint snapshots: the scope/permission machinery must see an
edit to an already-dirty file, an index-only (staged) change, and git's own
control-plane tamper — not just a numstat shape.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from adw_modules import permissions
from conftest import FakeRun, agent, config, dirty


def _git(args, cwd):
    import subprocess
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True, encoding="utf-8", errors="replace")


def test_snapshot_detects_a_second_edit_to_an_already_dirty_file(repo: Path):
    """numstat (insertions/deletions counts) hashes two different one-line
    edits identically — content fingerprinting must not."""
    dirty(repo, "seed.js", "// edit one\n")
    run = FakeRun(repo)
    before = permissions.snapshot(run)
    dirty(repo, "seed.js", "// edit two, different content\n")
    after = permissions.snapshot(run)
    assert before.get("seed.js") != after.get("seed.js")


def test_snapshot_folds_in_the_index_state(repo: Path):
    """An agent that `git add`s a malicious edit, then reverts only the
    WORKING TREE back to the ORIGINAL bytes (leaving the malicious blob
    staged), must still show up in the snapshot — `git diff` alone (worktree
    vs base) would report NO difference once the working tree matches base
    again; only the unioned `git diff --cached` catches the still-staged
    edit. Without the index fold, `seed.js` would never even be a key here,
    so `enforce()` would report no breach at all."""
    dirty(repo, "seed.js", "// staged edit\n")
    _git(["add", "seed.js"], repo)
    # Revert ONLY the working tree back to the committed baseline bytes —
    # NOT via `git checkout` (which restores from the INDEX, a no-op here
    # since the index already holds the staged edit) — the index still
    # holds the malicious blob throughout.
    (repo / "seed.js").write_text("// original\n", encoding="utf-8")
    run = FakeRun(repo)
    snap = permissions.snapshot(run)
    assert "seed.js" in snap, (
        "a staged-only difference (worktree reverted to base, index still "
        "dirty) must still be visible in the snapshot")


def test_changed_paths_reports_appear_change_and_vanish():
    before = {"a.js": "file:1"}
    after = {"a.js": "file:1", "b.js": "file:2"}
    assert permissions.changed_paths(before, after) == ["b.js"]
    assert permissions.changed_paths(after, before) == ["b.js"]


def test_fingerprint_distinguishes_symlink_from_deleted(repo: Path, tmp_path):
    target = repo / "real.txt"
    target.write_text("x", encoding="utf-8")
    link = repo / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks not permitted on this filesystem/host")
    fp_symlink = permissions._fingerprint(link)
    fp_absent = permissions._fingerprint(repo / "does-not-exist")
    assert fp_symlink != fp_absent
    assert fp_symlink.startswith("symlink:")
    assert fp_absent == permissions.DELETED


def test_permitted_protects_a_path_even_under_always_writable_data_dir(repo: Path):
    """protected_files outranks the blanket data_dir grant — a tracked file
    that happens to sit under data_dir stays governed by writes."""
    cfg = config(protected=["adws/adw_data/prompt_engineering/"],
                 data_dir="adws/adw_data")
    a = agent("scout", writes=[])
    tracked = frozenset({"adws/adw_data/prompt_engineering/builder/system.md"})
    assert not permissions.permitted(
        "adws/adw_data/prompt_engineering/builder/system.md", a, cfg, tracked)


def test_permitted_allows_the_session_runtime_for_an_untracked_path():
    cfg = config(protected=["adws/adw_modules/"], data_dir="adws/adw_data")
    a = agent("scout", writes=[])
    assert permissions.permitted(
        "adws/adw_data/sessions/x/context_handoff/plan.md", a, cfg, frozenset())


def test_permitted_none_writes_means_unrestricted_except_protected():
    cfg = config(protected=["adws/adw_modules/"])
    a = agent("builder", writes=None)
    assert permissions.permitted("src/feature.js", a, cfg, frozenset())
    assert not permissions.permitted("adws/adw_modules/gates.py", a, cfg, frozenset())


# ── control-plane tamper detection ───────────────────────────────────────────

def test_control_plane_snapshot_detects_a_hooks_path_repoint(repo: Path):
    run = FakeRun(repo)
    before = permissions.control_plane_snapshot(run)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    (hooks_dir / "pre-commit").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    after = permissions.control_plane_snapshot(run)
    assert permissions.changed_paths(before, after)


def test_enforce_raises_on_control_plane_tamper(repo: Path):
    run = FakeRun(repo)
    a = agent("builder", writes=None)
    run.cfg = config(protected=[])
    before = permissions.snapshot(run)
    meta_before = permissions.control_plane_snapshot(run)
    (repo / ".git" / "hooks").mkdir(exist_ok=True)
    (repo / ".git" / "hooks" / "pre-commit").write_text("evil\n", encoding="utf-8")
    with pytest.raises(permissions.PermissionBreach, match="control plane"):
        permissions.enforce(run, phase=None, agent=a, before=before, meta_before=meta_before)


def test_enforce_raises_when_head_moves_during_the_phase(repo: Path):
    run = FakeRun(repo)
    a = agent("builder", writes=None)
    run.cfg = config(protected=[])
    base = permissions.phase_base(run)
    before = permissions.snapshot(run, base)
    dirty(repo, "new.js")
    _git(["add", "new.js"], repo)
    _git(["commit", "-m", "agent committed during its own phase"], repo)
    with pytest.raises(permissions.PermissionBreach, match="moved HEAD"):
        permissions.enforce(run, phase=None, agent=a, before=before, base=base)


# ── submodule refusal ────────────────────────────────────────────────────────

def test_assert_no_dirty_submodules_is_a_noop_with_no_submodules(repo: Path):
    run = FakeRun(repo)
    permissions.assert_no_dirty_submodules(run)   # must not raise
