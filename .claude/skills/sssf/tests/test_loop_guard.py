"""Detects a coding agent stuck repeating the same tool call(s).

Live incident: a builder finished its real edits, then alternated `write
Report.json` / `bash rm Report.json` for 18+ minutes because it misread the
builder prompt's `## Report` heading as a file to create.
"""
from __future__ import annotations

import pytest

from adw_modules import loop_guard


def test_consecutive_identical_mutating_calls_trigger_after_the_configured_count():
    guard = loop_guard.LoopGuard(loop_guard.LoopGuardConfig(repeat_count=3))
    guard.observe("write", {"path": "Report.json", "content": "x"})
    guard.observe("write", {"path": "Report.json", "content": "x"})
    with pytest.raises(loop_guard.LoopDetected):
        guard.observe("write", {"path": "Report.json", "content": "x"})


def test_ab_cycle_triggers_after_the_configured_period_count():
    guard = loop_guard.LoopGuard(loop_guard.LoopGuardConfig(cycle_count=2))
    guard.observe("write", {"path": "Report.json"})
    guard.observe("bash", {"command": "rm Report.json"})
    guard.observe("write", {"path": "Report.json"})
    with pytest.raises(loop_guard.LoopDetected):
        guard.observe("bash", {"command": "rm Report.json"})


def test_delete_command_spelling_is_normalized_across_the_cycle():
    """`rm x`, `rm -f x`, `del x`, `Remove-Item x` must all be recognized as
    the same DELETE signature — varying the spelling must not dodge the
    cycle detector."""
    guard = loop_guard.LoopGuard(loop_guard.LoopGuardConfig(cycle_count=2))
    guard.observe("write", {"path": "Report.json"})
    guard.observe("bash", {"command": "rm Report.json"})
    guard.observe("write", {"path": "Report.json"})
    with pytest.raises(loop_guard.LoopDetected):
        guard.observe("bash", {"command": "del /f Report.json"})


def test_read_only_calls_get_a_higher_repeat_threshold():
    guard = loop_guard.LoopGuard(
        loop_guard.LoopGuardConfig(repeat_count=2, read_only_repeat_count=5))
    for _ in range(4):
        guard.observe("read", {"path": "file.py"})
    # 4 < read_only_repeat_count(5): must not have triggered yet.
    with pytest.raises(loop_guard.LoopDetected):
        guard.observe("read", {"path": "file.py"})   # 5th — now triggers


def test_paginated_reads_are_distinguished_by_offset():
    """Four reads of the same file at different offsets must NOT collapse
    into one identical signature."""
    guard = loop_guard.LoopGuard(loop_guard.LoopGuardConfig(repeat_count=3))
    guard.observe("read", {"path": "big.py", "offset": 0, "limit": 100})
    guard.observe("read", {"path": "big.py", "offset": 100, "limit": 100})
    guard.observe("read", {"path": "big.py", "offset": 200, "limit": 100})
    guard.observe("read", {"path": "big.py", "offset": 300, "limit": 100})   # must not raise


def test_different_edit_content_is_distinguished_by_hash():
    """Four genuinely different edits to the same file must not collapse
    into one identical signature just because file_path repeats."""
    guard = loop_guard.LoopGuard(loop_guard.LoopGuardConfig(repeat_count=3))
    guard.observe("edit", {"file_path": "x.py", "old_string": "a", "new_string": "b"})
    guard.observe("edit", {"file_path": "x.py", "old_string": "b", "new_string": "c"})
    guard.observe("edit", {"file_path": "x.py", "old_string": "c", "new_string": "d"})
    guard.observe("edit", {"file_path": "x.py", "old_string": "d", "new_string": "e"})


def test_tool_name_is_canonicalized_case_insensitively():
    """Claude Code's built-in tool names (Read, Bash, ...) must be
    recognized the same as pi's own lowercase names."""
    assert loop_guard._is_low_risk("Read", {"path": "x"})
    assert loop_guard._is_low_risk("read", {"path": "x"})


def test_a_different_loop_signature_does_not_trip_early():
    guard = loop_guard.LoopGuard(loop_guard.LoopGuardConfig(repeat_count=3))
    guard.observe("write", {"path": "a.json"})
    guard.observe("write", {"path": "b.json"})
    guard.observe("write", {"path": "c.json"})   # different paths, no loop
