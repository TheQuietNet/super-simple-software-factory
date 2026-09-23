"""Deterministic lint, typecheck, build, and test blocks.

A known command is not a judgement call. Anything whose invocation you can write
down belongs here as code — it runs in milliseconds, costs nothing, and returns
the same answer every time. Agents are for the parts that need reading and
deciding.

╔══════════════════════════════════════════════════════════════════════════════╗
║  REPLACE THE PLACEHOLDER COMMANDS BELOW.                                     ║
║                                                                              ║
║  Every unconfigured block FAILS LOUDLY (writes to stderr, exits 1). They are  ║
║  placeholders on purpose: a stamped repo has no way to guess your test       ║
║  runner, and a wrong-but-plausible command that silently passes is worse     ║
║  than one that says so out loud. Same for TEST_GLOB below matching zero      ║
║  files — an empty glob is a FAILURE, never a quiet "0 tests, exit 0".        ║
║                                                                              ║
║  For each block you want: swap `_placeholder(...)` for the real argv, e.g.    ║
║      argv=["bun", "test", "apps/web/server.test.ts"]                         ║
║      argv=["uv", "run", "pytest", "-q"]                                      ║
║      argv=["npm", "run", "lint"]                                             ║
║  Delete the blocks you don't need, and drop them from run_quality()'s list.   ║
║                                                                              ║
║  Two rules when you write the real command:                                  ║
║    1. argv LIST, never a shell string — no quoting bugs, no shell injection.  ║
║    2. Call binaries by BARE NAME. These blocks inherit the operator's         ║
║       environment (see utils.operator_env), so `bun`, `uv`, `pytest` resolve  ║
║       exactly as they do in their terminal. Never hard-code an absolute path  ║
║       like /Users/you/.bun/bin/bun — that bakes your machine into the trace.  ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from .data_types import (EventRecord, QualityCheckResult, QualityCheckSpec, QualityResult,
                         VerifyOutput)
from .utils import now_iso, operator_env

# How much of a failing command's output rides back inside the envelope. Enough
# for a builder to act on without opening the artifact; bounded so a runaway
# stack trace can't swamp the next agent's context.
TAIL_CHARS = 4_000

# Single named constant `gates.new_tests_are_discoverable` reads too (both
# must agree on what the runner actually collects). PLACEHOLDER value — set
# this to your real test runner's own glob before wiring up `test()` below,
# e.g. "tests/**/*.test.js", "tests/**/test_*.py", "src/**/*_test.go".
TEST_GLOB = "tests/**/*.test.PLACEHOLDER"


def glob_test_files(repo_root: str | Path, pattern: str = TEST_GLOB) -> list[Path]:
    """Files matching TEST_GLOB. Empty is a FAILURE, not a pass — a stamped-
    but-unwired repo (or a real runner whose glob quietly matches nothing)
    must never report a silent green."""
    return sorted(p for p in Path(repo_root).glob(pattern) if p.is_file())


def _fail_loud(message: str) -> list[str]:
    """Portable (no shell, no node/echo dependency) fail-closed argv — writes
    to stderr and exits 1. Used for every unconfigured quality block and for
    an empty TEST_GLOB match, so a stamped-but-unwired repo never reports a
    silent green. `sys.executable -c` rather than `echo`/`node`: it resolves
    on every platform this harness runs on without assuming a language
    runtime the target repo may not have.
    """
    return [sys.executable, "-c",
            f"import sys; print({message!r}, file=sys.stderr); sys.exit(1)"]


def _placeholder(name: str) -> list[str]:
    """Unconfigured gate — must fail loudly, never a silent echo-0 pass."""
    return _fail_loud(
        f"PLACEHOLDER {name}: unconfigured — edit adws/adw_modules/quality.py "
        f"and replace this with the real {name} command")


def _check_dir(run, name: str) -> Path:
    seq = run.phases[-1].seq if run.phases else 0
    path = run.context_handoff_dir / "quality" / f"{seq:02d}_{name}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run(spec: QualityCheckSpec, run, extra_env: dict[str, str] | None = None) -> QualityCheckResult:
    phase = run.phases[-1]
    output_dir = _check_dir(run, spec.name)
    output_artifact = output_dir / "command.log"
    command = shlex.join(spec.argv)
    env = operator_env()             # the engineer's own shell environment
    if extra_env:
        env = {**env, **extra_env}

    run.console.note(f"quality {spec.name}: {command}")
    started_at = now_iso()
    clock = time.monotonic()
    stdout = ""
    stderr = ""
    try:
        completed = subprocess.run(
            spec.argv,
            cwd=run.repo_root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=spec.timeout_seconds,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as error:
        returncode = 124
        stdout = error.stdout or ""
        stderr = (error.stderr or "") + f"\nTimed out after {spec.timeout_seconds}s."
    except OSError as error:
        # A missing binary lands here as exit 127 with the real message — no
        # pre-flight probe needed, and none wanted.
        returncode = 127
        stderr = str(error)

    duration = time.monotonic() - clock
    output_artifact.write_text(
        f"$ {command}\nexit: {returncode}\nduration_seconds: {duration:.3f}\n"
        f"\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n",
        encoding="utf-8",
    )
    passed = returncode == 0
    run.tracer.event(EventRecord(
        adw_id=run.adw_id,
        phase_id=phase.phase_id,
        type="tool_call",
        name=f"quality:{spec.name}",
        payload={
            "area": spec.area,
            "operation": spec.operation,
            "command": command,
            "returncode": returncode,
            "passed": passed,
            "output_artifact": str(output_artifact),
        },
        started_at=started_at,
        ended_at=now_iso(),
    ))
    run.console.note(
        f"quality {spec.name}: {'passed' if passed else 'failed'} "
        f"(exit {returncode}, {duration:.1f}s)"
    )
    return QualityCheckResult(
        name=spec.name,
        area=spec.area,
        operation=spec.operation,
        command=command,
        returncode=returncode,
        passed=passed,
        duration_seconds=duration,
        output_artifact=str(output_artifact),
        output_tail=(stdout + stderr)[-TAIL_CHARS:],
    )


# ── Blocks ────────────────────────────────────────────────────────────────────
# Replace every argv below. See the banner at the top of this file.

def test(run) -> QualityCheckResult:
    """Run the project's test suite. The highest-value block to wire up first.

    Unconfigured (TEST_GLOB still the PLACEHOLDER value, or your real glob
    matching zero files) FAILS LOUDLY rather than silently passing — see the
    module banner and `_fail_loud`.
    """
    files = glob_test_files(run.repo_root, TEST_GLOB)
    if not files:
        return _run(QualityCheckSpec(
            name="test",
            area="backend",
            operation="build",
            argv=_fail_loud(f"empty test glob {TEST_GLOB}: 0 files matched — "
                            f"set quality.TEST_GLOB to your real test runner's "
                            f"glob and wire up the real test command"),
            timeout_seconds=30,
        ), run)
    return _run(QualityCheckSpec(
        name="test",
        area="backend",
        operation="build",
        # Placeholder until wired up — see the module banner. Replace with the
        # real invocation, e.g. ["uv", "run", "pytest", "-q"] or
        # ["bun", "test"]. Keep argv a LIST (never a shell string) and call
        # binaries by bare name (never an absolute, machine-specific path).
        argv=_placeholder("test"),
        timeout_seconds=600,
    ), run)


def lint(run) -> QualityCheckResult:
    return _run(QualityCheckSpec(
        name="lint",
        area="backend",
        operation="lint",
        argv=_placeholder("lint"),        # e.g. ["bun", "x", "oxlint@1.36.0", "src"]
    ), run)


def typecheck(run) -> QualityCheckResult:
    return _run(QualityCheckSpec(
        name="typecheck",
        area="backend",
        operation="typecheck",
        argv=_placeholder("typecheck"),   # e.g. ["bun", "x", "tsc", "--noEmit"]
    ), run)


def build(run) -> QualityCheckResult:
    output_dir = _check_dir(run, "build") / "bundle"
    return _run(QualityCheckSpec(
        name="build",
        area="backend",
        operation="build",
        argv=_placeholder("build"),       # e.g. ["bun", "build", "src/index.ts", "--outdir", str(output_dir)]
    ), run)


def run_tests(run) -> QualityResult:
    """The test suite alone, as a QualityResult — the deterministic test phase.

    This is what replaces a `tester` agent once the command is written down. An
    agent rediscovering the runner on every run costs a fortune to learn what a
    subprocess already knows; the repair loop is unchanged, because a failure
    still reaches the builder through `as_envelope` below.
    """
    check = test(run)
    failures = ([] if check.passed else
                [f"{check.name}: `{check.command}` exited {check.returncode}\n"
                 f"{check.output_tail}".rstrip()])
    return QualityResult(passed=check.passed, checks=[check], failures=failures,
                         artifacts=[check.output_artifact])


def as_envelope(result: QualityResult, what: str) -> VerifyOutput:
    """Wrap a deterministic result so an agent can be handed it directly.

    Agents hand each other typed envelopes; code blocks return QualityResult.
    This is the adapter, so a failing lint or test run flows back into the
    builder through exactly the same door an agent's report would — the ADW
    script is the only thing that knows the difference.
    """
    return VerifyOutput(
        status="success" if result.passed else "fail",
        summary=(f"{what}: all {len(result.checks)} check(s) passed" if result.passed
                 else f"{what}: {len(result.failures)} of {len(result.checks)} check(s) failed"),
        artifacts=result.artifacts,
        notes_for_next_agent=("" if result.passed else
                              "Fix every failure below. The output is verbatim from the "
                              "command — trust it over any summary."),
        passed=result.passed,
        failures=result.failures,
    )


def run_quality(run) -> QualityResult:
    """Run every block and collect ALL failures — one pass tells you everything.

    Ordering contract for the caller: a failing block does NOT fail the phase.
    The runner did its job; the CODE is what failed. Hand this result to the
    builder and let the bounded repair loop decide the run's fate.
    """
    # ONLY `test` is wired here by default. lint / typecheck / build are left
    # defined but deliberately OUT of this list until they have a real argv:
    # an unconfigured block now fails loudly rather than echoing 0, but a
    # check that always fails is still noise, not signal — add one back the
    # same day you give it a real argv, not before.
    blocks: list[Callable] = [
        test,
    ]
    checks = [block(run) for block in blocks]
    # A failure is the command, its exit code, and what it actually printed —
    # everything a builder needs to repair without opening a log or being told
    # what the error "means" by a parser that guessed.
    failures = [
        f"{check.name}: `{check.command}` exited {check.returncode}\n{check.output_tail}".rstrip()
        for check in checks if not check.passed
    ]
    return QualityResult(
        passed=not failures,
        checks=checks,
        failures=failures,
        artifacts=[check.output_artifact for check in checks],
    )
