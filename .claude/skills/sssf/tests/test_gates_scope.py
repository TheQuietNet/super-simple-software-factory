"""Where:/plan scope hardening: glob-aware tokens, annotation-safe parsing,
pinned planner scope, and the scope gate checking the ACTUAL change set —
not only what an envelope claimed.
"""
from __future__ import annotations

from pathlib import Path

from adw_modules import gates, permissions
from adw_modules.data_types import BuildOutput
from conftest import FakeRun, dirty

GLOB_WHERE = "Add a token helper.\nWhere: tests/*.test.js\n"
DIR_WHERE = "Add a token helper.\nWhere: tests/\n"
E_WHERE = "Add a query helper.\nWhere: query.js\n"


def build(changed_files):
    return BuildOutput(status="success", summary="t", changed_files=changed_files)


# ── glob / directory Where: tokens ───────────────────────────────────────────

def test_glob_where_token_is_parsed_whole_not_truncated():
    """MUTATION BAR: a prose-extraction regex recovers only `test.js`."""
    assert gates._paths_from_where(GLOB_WHERE) == {"tests/*.test.js"}


def test_glob_where_puts_a_matching_test_in_scope(repo: Path):
    report = gates.claims_are_in_requested_scope(
        build(["tests/getAccessToken.test.js"]),
        FakeRun(repo, request=GLOB_WHERE))
    assert all(c.ok for c in report.checks)


def test_glob_where_keeps_a_non_matching_file_out_of_scope(repo: Path):
    report = gates.claims_are_in_requested_scope(
        build(["lib/x.js"]), FakeRun(repo, request=GLOB_WHERE))
    assert not any(c.ok for c in report.checks)
    assert any("not in requested scope" in c.note for c in report.checks)


def test_glob_star_does_not_cross_a_directory_boundary():
    assert gates._scope_matches("tests/x.test.js", "tests/*.test.js")
    assert not gates._scope_matches("tests/sub/x.test.js", "tests/*.test.js")
    assert gates._scope_matches("tests/sub/x.test.js", "tests/**/*.test.js")


def test_directory_prefix_where_token_puts_nested_file_in_scope(repo: Path):
    report = gates.claims_are_in_requested_scope(
        build(["tests/getAccessToken.test.js"]),
        FakeRun(repo, request=DIR_WHERE))
    assert all(c.ok for c in report.checks)


def test_directory_prefix_where_keeps_a_sibling_out_of_scope(repo: Path):
    report = gates.claims_are_in_requested_scope(
        build(["lib/tests-helper.js"]), FakeRun(repo, request=DIR_WHERE))
    assert not any(c.ok for c in report.checks)


# ── degenerate "matches everything" tokens ───────────────────────────────────

def test_bare_double_star_where_token_is_rejected_not_honored(repo: Path):
    assert gates._paths_from_where("Where: **\n") == set()
    report = gates.claims_are_in_requested_scope(
        build(["anything/at/all.js"]), FakeRun(repo, request="Where: **\n"))
    assert not any(c.ok for c in report.checks)


def test_bare_star_where_token_is_also_rejected(repo: Path):
    assert gates._paths_from_where("Where: *\n") == set()


def test_dot_and_slash_where_tokens_are_also_rejected():
    assert gates._paths_from_where("Where: .\n") == set()
    assert gates._paths_from_where("Where: /\n") == set()


def test_degenerate_token_is_dropped_but_a_real_sibling_token_still_counts():
    assert gates._paths_from_where("Where: query.js, **\n") == {"query.js"}


# ── annotation-safe Where: parsing ───────────────────────────────────────────

def test_comma_inside_parenthetical_annotation_is_not_a_new_entry():
    text = ("Where: contacts.js, lib/gmail.js (the shared implementation — "
           "reuse it, do not rewrite it), tests/*.test.js\n")
    tokens = gates._paths_from_where(text)
    assert tokens == {"contacts.js", "lib/gmail.js", "tests/*.test.js"}


def test_bare_word_annotation_is_not_treated_as_a_path():
    # _where_tokens_and_warnings takes the Where: line's VALUE, not the
    # "Where:" label itself — matching how _paths_from_where calls it.
    tokens, warnings = gates._where_tokens_and_warnings(
        "query.js, documentation (not a path)")
    assert tokens == {"query.js"}
    assert warnings


def test_trailing_garbage_after_annotation_is_not_silently_truncated():
    tokens, warnings = gates._where_tokens_and_warnings(
        "lib/g.js (note) trailing garbage")
    assert "lib/g.js" not in tokens
    assert warnings


def test_extensionless_repo_root_file_parses_via_real_existence(repo: Path):
    (repo / "justfile").write_text("default:\n", encoding="utf-8")
    tokens = gates._paths_from_where("Where: justfile\n", repo)
    assert "justfile" in tokens


# ── pin_plan_scope: freeze plan.md's scope once ──────────────────────────────

def test_requested_scope_never_reads_plan_md_without_a_pin(repo: Path, tmp_path_factory):
    handoff = tmp_path_factory.mktemp("handoff")
    (handoff / "plan.md").write_text("Files: `lib/podcast.js`.\n", encoding="utf-8")
    run = FakeRun(repo, request="do the plan", context_handoff_dir=handoff)
    assert gates.requested_scope(run) == set()


def test_pinned_plan_scope_survives_a_later_mutation_of_plan_md(repo: Path, tmp_path_factory):
    handoff = tmp_path_factory.mktemp("handoff")
    (handoff / "plan.md").write_text("Files: `query.js`.\n", encoding="utf-8")
    run = FakeRun(repo, request="add a query helper", context_handoff_dir=handoff)
    gates.pin_plan_scope(run)
    assert gates.requested_scope(run) == {"query.js"}

    (handoff / "plan.md").write_text(
        "Files: `query.js`.\nAlso lib/db.js.\n", encoding="utf-8")

    assert gates.requested_scope(run) == {"query.js"}
    dirty(repo, "query.js")
    dirty(repo, "lib/db.js")
    report = gates.claims_are_in_requested_scope(
        build(["query.js", "lib/db.js"]), run)
    assert any(c.item == "lib/db.js" and not c.ok for c in report.checks)


# ── scoped_for_commit: defense in depth at commit time ───────────────────────

def test_scoped_for_commit_excludes_the_dropped_out_of_scope_path(repo: Path):
    dirty(repo, "query.js")
    dirty(repo, "lib/db.js")
    run = FakeRun(repo, request=E_WHERE)
    kept, excluded = gates.scoped_for_commit(run, ["query.js", "lib/db.js"])
    assert kept == ["query.js"]
    assert excluded == ["lib/db.js"]


def test_scoped_for_commit_refuses_when_no_scope_is_named(repo: Path):
    """No Where:/plan scope for a code commit is FAIL-CLOSED — a build-only
    ADW with no scope must refuse, not stage everything unconditionally."""
    import pytest
    run = FakeRun(repo, request="no where line here")
    with pytest.raises(RuntimeError, match="no determinable scope"):
        gates.scoped_for_commit(run, ["lib/db.js", "query.js"])


def test_scoped_for_commit_keeps_glob_and_directory_scoped_paths(repo: Path):
    run = FakeRun(repo, request=GLOB_WHERE)
    kept, excluded = gates.scoped_for_commit(
        run, ["tests/getAccessToken.test.js", "lib/x.js"])
    assert kept == ["tests/getAccessToken.test.js"]
    assert excluded == ["lib/x.js"]


# ── fill_empty_changed_files: recover an honest but empty claim ─────────────

def test_glob_where_fills_an_unclaimed_dirty_test_into_scope(repo: Path):
    dirty(repo, "tests/getAccessToken.test.js")
    env = build([])
    filled = gates.fill_empty_changed_files(env, FakeRun(repo, request=GLOB_WHERE))
    assert filled is True
    assert env.changed_files == ["tests/getAccessToken.test.js"]


def test_fill_ignores_a_dirty_file_outside_the_glob(repo: Path):
    dirty(repo, "tests/getAccessToken.test.js")
    dirty(repo, "lib/x.js")
    env = build([])
    gates.fill_empty_changed_files(env, FakeRun(repo, request=GLOB_WHERE))
    assert env.changed_files == ["tests/getAccessToken.test.js"]


# ── claims_are_in_requested_scope: actual-touched-this-phase, not the whole repo ─

def test_pre_existing_operator_wip_does_not_fail_the_gate_or_get_staged(repo: Path):
    """An operator's own WIP, dirty BEFORE the phase's agent ever ran, is
    not the agent's change — the baseline snapshot must exempt it."""
    dirty(repo, "unrelated_wip.md")
    run = FakeRun(repo, request=E_WHERE)
    # agents.execute() snapshots this BEFORE sending the agent its first
    # prompt — mirrored here.
    run.phase_baseline_dirty = permissions.snapshot(run)
    dirty(repo, "query.js")   # the agent's own, honest, in-scope edit
    report = gates.claims_are_in_requested_scope(build(["query.js"]), run)
    assert all(c.ok for c in report.checks)


def test_dropped_out_of_scope_edit_still_fails_the_gate(repo: Path):
    """A path the agent actually touched but then DROPPED from its claim is
    not thereby exempt."""
    dirty(repo, "query.js")
    dirty(repo, "lib/db.js")
    report = gates.claims_are_in_requested_scope(
        build(["query.js"]), FakeRun(repo, request=E_WHERE))
    assert any(c.item == "lib/db.js" and not c.ok for c in report.checks)
    assert any(c.item == "query.js" and c.ok for c in report.checks)


def test_a_further_edit_to_a_pre_dirty_path_is_still_caught(repo: Path):
    """Presence in the baseline is not a permanent exemption — a pre-dirty,
    out-of-scope file the agent goes on to modify AGAIN this phase must
    still be caught (content fingerprint diff, not a bare key-set diff)."""
    dirty(repo, "lib/db.js", "before\n")
    run = FakeRun(repo, request=E_WHERE)
    run.phase_baseline_dirty = permissions.snapshot(run)
    dirty(repo, "lib/db.js", "after — agent edited it again\n")
    dirty(repo, "query.js")
    report = gates.claims_are_in_requested_scope(build(["query.js"]), run)
    assert any(c.item == "lib/db.js" and not c.ok for c in report.checks)
