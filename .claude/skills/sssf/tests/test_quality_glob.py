"""quality.py must never report a silent green when it is unconfigured.

Matches the intent of the pilot's ywh#102 / task #53113 fix, generalized for
a template that cannot assume Node (or any language runtime) is installed:
  - TEST_GLOB is a single named constant, shipped as an obvious PLACEHOLDER
    value — gates.new_tests_are_discoverable's own _runner_test_glob() reads
    it and fails closed on that value (see test_gates_52882.py).
  - An empty TEST_GLOB match (whether still the placeholder, or a real glob
    that happens to match zero files) is a FAILURE, not node/pytest's own
    "0 tests, exit 0".
  - Every unconfigured block (test/lint/typecheck/build) fails LOUDLY —
    writes to stderr and exits 1 — never a silent `echo`/exit-0.
"""
from __future__ import annotations

import sys
from pathlib import Path

from adw_modules import quality
from conftest import TEMPLATES_ADWS


class _Phase:
    def __init__(self):
        self.seq = 1
        self.phase_id = "p1"


class _Console:
    def note(self, *a, **k) -> None:
        pass


class _Tracer:
    def event(self, *a, **k) -> None:
        pass


class _QualityRun:
    def __init__(self, repo_root: Path, context_handoff_dir: Path):
        self.repo_root = str(repo_root)
        self.context_handoff_dir = context_handoff_dir
        self.phases = [_Phase()]
        self.console = _Console()
        self.tracer = _Tracer()
        self.adw_id = "tqualityglob"


def _run(repo_root: Path, tmp_path: Path) -> _QualityRun:
    handoff = tmp_path / "handoff"
    handoff.mkdir(exist_ok=True)
    return _QualityRun(repo_root, handoff)


def test_test_glob_is_a_named_constant_with_a_placeholder_value():
    assert hasattr(quality, "TEST_GLOB")
    assert "PLACEHOLDER" in quality.TEST_GLOB.upper()


def test_placeholder_argv_never_uses_echo_or_node():
    """The unconfigured-block fallback must be portable — no assumption the
    stamped repo has node, bash's echo builtin, or any particular shell."""
    argv = quality._placeholder("test")
    assert argv[0] == sys.executable
    assert "node" not in argv
    assert "echo" not in argv


def test_placeholder_argv_exits_1(tmp_path: Path):
    run = _run(tmp_path, tmp_path)
    result = quality._run(quality.QualityCheckSpec(
        name="unconfigured", area="backend", operation="build",
        argv=quality._placeholder("unconfigured"), timeout_seconds=10,
    ), run)
    assert result.returncode == 1
    assert not result.passed


def test_empty_glob_fails_even_with_a_real_looking_glob(tmp_path: Path, monkeypatch):
    """A TEST_GLOB the operator set for real, but that currently matches
    ZERO files, must still fail — never the silent `0 tests, exit 0` a real
    test runner would itself report."""
    monkeypatch.setattr(quality, "TEST_GLOB", "tests/**/*.test.py")
    (tmp_path / "tests").mkdir()
    run = _run(tmp_path, tmp_path)
    result = quality.test(run)
    assert not result.passed
    assert result.returncode != 0
    assert "empty" in result.output_tail.lower() or "0 files" in result.output_tail.lower()


def test_glob_test_files_is_empty_for_an_empty_directory(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    assert quality.glob_test_files(tmp_path, "tests/**/*.test.py") == []


def test_glob_test_files_finds_a_real_match(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "sample.test.py").write_text("x", encoding="utf-8")
    found = quality.glob_test_files(tmp_path, "tests/**/*.test.py")
    assert len(found) == 1


def test_test_glob_matches_gates_placeholder_detection():
    """gates._runner_test_glob() must agree with quality.py's own shape:
    the shipped TEST_GLOB is recognized as a placeholder by BOTH modules."""
    from adw_modules import gates
    assert gates._runner_test_glob() is None


def test_run_quality_only_wires_the_test_block():
    """lint/typecheck/build stay defined but out of run_quality()'s list
    until given a real argv — see the module's own banner."""
    source = (TEMPLATES_ADWS / "adw_modules" / "quality.py").read_text(encoding="utf-8")
    blocks_section = source.split("def run_quality(run)", 1)[1]
    assert "blocks: list[Callable] = [" in blocks_section
    assert "test,\n" in blocks_section or "test," in blocks_section
