"""Phase-timeout and process-tree-kill helpers shared by every coding-agent
adapter. Live incident: a builder finished its real edits, then looped
write/rm on a Report.json it misread from the prompt's `## Report` heading
for 18+ minutes with nothing to stop it.
"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from adw_modules import agent_budget


def test_phase_timeout_message_is_exact():
    err = agent_budget.PhaseTimeout(300)
    assert str(err) == "phase budget exceeded (300s)"
    assert err.timeout_seconds == 300


def test_popen_kwargs_shape_is_platform_appropriate():
    kwargs = agent_budget.popen_kwargs_for_tree_kill()
    if sys.platform == "win32":
        assert "creationflags" in kwargs
    else:
        assert kwargs.get("start_new_session") is True


def test_kill_process_tree_on_a_real_short_lived_process_is_a_noop_after_exit():
    proc = subprocess.Popen([sys.executable, "-c", "pass"],
                            **agent_budget.popen_kwargs_for_tree_kill())
    # On Windows the process is spawned CREATE_SUSPENDED — track_for_kill()
    # is what actually resumes it (see popen_kwargs_for_tree_kill's own
    # docstring); every real adapter calls it right after Popen() too.
    token = agent_budget.track_for_kill(proc.pid)
    try:
        proc.wait(timeout=5)
        # Must not raise even though the process already exited.
        agent_budget.kill_process_tree(proc.pid)
    finally:
        agent_budget.release_tracking(proc.pid, token)


def test_kill_process_tree_actually_kills_a_sleeping_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            **agent_budget.popen_kwargs_for_tree_kill())
    try:
        assert agent_budget.process_alive(proc.pid)
        agent_budget.kill_process_tree(proc.pid)
        proc.wait(timeout=5)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()


def test_timeout_line_reader_raises_when_deadline_passes_with_no_output():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace",
        **agent_budget.popen_kwargs_for_tree_kill())
    try:
        reader = agent_budget.TimeoutLineReader(proc.stdout)
        deadline = time.monotonic() + 0.2
        with pytest.raises(agent_budget.PhaseTimeout):
            list(reader.lines(deadline, 1))
        reader.close(join_timeout=1)
    finally:
        agent_budget.kill_process_tree(proc.pid)
        proc.wait(timeout=5)


def test_timeout_line_reader_yields_lines_as_they_arrive():
    proc = subprocess.Popen(
        [sys.executable, "-c", "print('one'); print('two')"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace",
        **agent_budget.popen_kwargs_for_tree_kill())
    # See the matching comment in test_kill_process_tree_on_a_real_short_
    # lived_process_is_a_noop_after_exit — CREATE_SUSPENDED on Windows needs
    # track_for_kill() to actually run.
    token = agent_budget.track_for_kill(proc.pid)
    try:
        reader = agent_budget.TimeoutLineReader(proc.stdout)
        deadline = time.monotonic() + 10
        lines = list(reader.lines(deadline, 10))
        proc.wait(timeout=5)
        assert [l.strip() for l in lines] == ["one", "two"]
    finally:
        agent_budget.release_tracking(proc.pid, token)


def test_release_tracking_is_a_safe_noop_with_no_token():
    agent_budget.release_tracking(999999, None)   # must not raise
