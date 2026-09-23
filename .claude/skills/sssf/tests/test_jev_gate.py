"""The Jev ask-vs-diff gate is opt-in and skip-never-block by default.

Off unless SSSF_JEV=1; skipped (not failed) with no TYPESAFE_API_KEY, so the
local-first roster stays offline-capable.
"""
from __future__ import annotations

from adw_modules import gates, jev
from adw_modules.data_types import BuildOutput
from conftest import FakeRun


def _env():
    return BuildOutput(status="success", summary="t", changed_files=["a.js"])


def test_jev_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SSSF_JEV", raising=False)
    assert jev.enabled() is False


def test_jev_enabled_only_on_explicit_truthy_values(monkeypatch):
    for value in ("1", "true", "yes", "TRUE"):
        monkeypatch.setenv("SSSF_JEV", value)
        assert jev.enabled() is True
    monkeypatch.setenv("SSSF_JEV", "0")
    assert jev.enabled() is False


def test_gate_skips_when_jev_is_off(monkeypatch, repo):
    monkeypatch.delenv("SSSF_JEV", raising=False)
    report = gates.ask_matches_diff(_env(), FakeRun(repo))
    assert report.passed
    assert any("SSSF_JEV off" in c.note for c in report.checks)


def test_gate_skips_when_no_api_key(monkeypatch, repo):
    monkeypatch.setenv("SSSF_JEV", "1")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    report = gates.ask_matches_diff(_env(), FakeRun(repo))
    assert report.passed
    assert any("TYPESAFE_API_KEY" in c.note for c in report.checks)


def test_gate_skips_on_transport_error_never_blocks(monkeypatch, repo):
    monkeypatch.setenv("SSSF_JEV", "1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-key")

    def _boom(state):
        raise RuntimeError("network down")

    monkeypatch.setattr(jev, "evaluate", _boom)
    report = gates.ask_matches_diff(_env(), FakeRun(repo))
    assert report.passed
    assert any("skipped" in c.note for c in report.checks)


def test_high_conf_reject_requires_confidence_or_low_meets():
    assert jev.high_conf_reject({
        "accept": {"choice": "correct_in_session", "confidence": 0.9},
        "meets_request": {"noul": 0.8},
    })
    assert jev.high_conf_reject({
        "accept": {"choice": "human", "confidence": 0.1},
        "meets_request": {"noul": 0.1},
    })
    assert not jev.high_conf_reject({
        "accept": {"choice": "approve", "confidence": 0.99},
        "meets_request": {"noul": 0.1},
    })
    assert not jev.high_conf_reject({
        "accept": {"choice": "human", "confidence": 0.3},
        "meets_request": {"noul": 0.9},
    })


def test_gate_fails_closed_on_a_high_confidence_reject(monkeypatch, repo):
    monkeypatch.setenv("SSSF_JEV", "1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-key")
    monkeypatch.setattr(jev, "collect_state", lambda envelope, run: {})
    monkeypatch.setattr(jev, "evaluate", lambda state: {
        "accept": {"choice": "correct_in_session", "confidence": 0.9},
        "meets_request": {"noul": 0.1},
    })
    report = gates.ask_matches_diff(_env(), FakeRun(repo))
    assert not report.passed


def test_builder_gates_includes_ask_matches_diff():
    assert jev is not None
    assert gates.ask_matches_diff in gates.BUILDER_GATES
