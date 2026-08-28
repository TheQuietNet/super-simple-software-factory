"""#52939: port #52938 builder-scope gates + first-build retries into templates.

Covers the 9d3e1718 substack-vs-Where case, the empty test-claim fail, the
shared BUILDER_GATES policy, retries=1 on the first phase named `build`, and
a fresh stamp that carries those into adws/.
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
ADW_SCRIPTS = sorted(TEMPLATES_ADWS.glob("adw_*.py"))
POLICY = ("gates", "BUILDER_GATES")
GATES_PY = TEMPLATES_ADWS / "adw_modules" / "gates.py"

PODCAST_WHERE = (
    "Make podcast --backfill fail closed.\n"
    "Where: podcast/fetch-podcast.js, lib/transcribe.js, lib/podcast.js, "
    "tests/podcast.test.js\n"
    "Done means: a regression test drives the real entry point.\n"
)


def _pydantic_available() -> bool:
    try:
        import pydantic  # noqa: F401
        return True
    except ImportError:
        return False


needs_pydantic = pytest.mark.skipif(
    not _pydantic_available(), reason="pydantic not installed")


def _load_gates():
    sys.path.insert(0, str(TEMPLATES_ADWS))
    from adw_modules import gates
    from adw_modules.data_types import BuildOutput
    return gates, BuildOutput


def test_gates_py_declares_scope_gate_and_builder_gates():
    src = GATES_PY.read_text(encoding="utf-8")
    assert "def claims_are_in_requested_scope" in src
    assert "BUILDER_GATES" in src
    assert "claims_are_in_requested_scope" in src.split("BUILDER_GATES", 1)[1]
    assert "new_tests_are_discoverable" in src.split("BUILDER_GATES", 1)[1]


def test_no_empty_list_pass_in_new_tests_are_discoverable():
    src_text = GATES_PY.read_text(encoding="utf-8")
    assert "nothing to place" not in src_text
    fn = next(
        n for n in ast.parse(src_text).body
        if isinstance(n, ast.FunctionDef) and n.name == "new_tests_are_discoverable"
    )
    for ret in ast.walk(fn):
        if not isinstance(ret, ast.Return) or ret.value is None:
            continue
        assert not (
            isinstance(ret.value, ast.Call)
            and isinstance(ret.value.func, ast.Name)
            and ret.value.func.id == "GateReport"
        ), "empty GateReport() return is the old not-applicable pass"


def _build_output_calls(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "AgentCall"):
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        target = kwargs.get("output_type")
        if isinstance(target, ast.Name) and target.id == "BuildOutput":
            yield node, kwargs


@pytest.mark.parametrize("script", ADW_SCRIPTS, ids=lambda p: p.name)
def test_every_build_phase_uses_builder_gates(script: Path):
    for node, kwargs in _build_output_calls(script):
        where = f"{script.name}:{node.lineno}"
        gates_arg = kwargs.get("gates")
        assert gates_arg is not None, f"{where}: a build phase with NO gates"
        assert isinstance(gates_arg, ast.Attribute), (
            f"{where}: gates must be gates.BUILDER_GATES, not an inline list")
        actual = (getattr(gates_arg.value, "id", None), gates_arg.attr)
        assert actual == POLICY, f"{where}: gates={actual}, expected {POLICY}"


def test_first_build_phase_retries_once():
    checked = 0
    for script in ADW_SCRIPTS:
        tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "PhaseParams"):
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            name = kwargs.get("name")
            if not (isinstance(name, ast.Constant) and name.value == "build"):
                continue
            retries = kwargs.get("retries")
            where = f"{script.name}:{node.lineno}"
            assert retries is not None, f"{where}: first build phase has no retries="
            assert isinstance(retries, ast.Constant) and retries.value == 1, (
                f"{where}: retries={getattr(retries, 'value', retries)!r}, want 1")
            checked += 1
    assert checked >= 6, f"expected first-build retries pins, found {checked}"


def test_fresh_stamp_carries_52938_gates(tmp_path: Path):
    scratch = tmp_path / "stamped"
    scratch.mkdir()
    subprocess.run(["git", "init"], cwd=scratch, check=True, capture_output=True)
    install = SKILL / "scripts" / "install.py"
    subprocess.run([sys.executable, str(install)], cwd=scratch, check=True, capture_output=True)
    stamped_gates = (scratch / "adws" / "adw_modules" / "gates.py").read_text(encoding="utf-8")
    assert "def claims_are_in_requested_scope" in stamped_gates
    assert "BUILDER_GATES" in stamped_gates
    assert "def new_tests_are_discoverable" in stamped_gates
    assert "envelope claims no test file" in stamped_gates
    for name in (
        "adw_plan_build.py",
        "adw_plan_build_test.py",
        "adw_plan_build_test_quality.py",
        "adw_simple_sdlc.py",
        "adw_build.py",
        "adw_build_test.py",
        "adw_build_review.py",
    ):
        text = (scratch / "adws" / name).read_text(encoding="utf-8")
        assert "retries=1" in text, name
        if "BuildOutput" in text:
            assert "gates.BUILDER_GATES" in text, name


@needs_pydantic
def test_claims_are_in_requested_scope_is_in_builder_gates():
    gates, _ = _load_gates()
    names = {g.__name__ for g in gates.BUILDER_GATES}
    assert "claims_are_in_requested_scope" in names
    assert "new_tests_are_discoverable" in names
    assert not isinstance(gates.BUILDER_GATES, list)


@needs_pydantic
def test_substack_against_podcast_where_fails():
    """MUTATION BAR for 9d3e1718: honest claim of the wrong arm."""
    gates, BuildOutput = _load_gates()
    report = gates.claims_are_in_requested_scope(
        BuildOutput(status="success", summary="t",
                    changed_files=["substack/fetch-substack.js"]),
        SimpleNamespace(repo_root=".", request=PODCAST_WHERE,
                        context_handoff_dir=None),
    )
    assert not report.passed
    assert any(c.item == "substack/fetch-substack.js" and not c.ok for c in report.checks)
    assert any("not in requested scope" in c.note for c in report.checks)


@needs_pydantic
def test_where_named_and_none_claimed_fails():
    gates, BuildOutput = _load_gates()
    report = gates.claims_are_in_requested_scope(
        BuildOutput(status="success", summary="t", changed_files=[]),
        SimpleNamespace(repo_root=".", request=PODCAST_WHERE,
                        context_handoff_dir=None),
    )
    assert not report.passed
    assert any(c.item == "where files claimed" for c in report.checks)


@needs_pydantic
def test_claiming_no_test_file_fails_closed():
    """MUTATION BAR for 9d3e1718 empty-list pass."""
    gates, BuildOutput = _load_gates()
    report = gates.new_tests_are_discoverable(
        BuildOutput(status="success", summary="t",
                    changed_files=["substack/fetch-substack.js"]),
        SimpleNamespace(repo_root="."),
    )
    assert not report.passed
    assert "no test file" in report.checks[0].note


@needs_pydantic
def test_ests_near_miss_still_fails():
    gates, BuildOutput = _load_gates()
    report = gates.new_tests_are_discoverable(
        BuildOutput(status="success", summary="t",
                    changed_files=["ests/pull-video-paragraphs.test.js"]),
        SimpleNamespace(repo_root="."),
    )
    assert not report.passed
