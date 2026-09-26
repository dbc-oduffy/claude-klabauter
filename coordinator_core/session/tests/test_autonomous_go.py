from __future__ import annotations

import subprocess

import pytest

from coordinator_core.session import autonomous_go
from coordinator_core.session import autonomous_sentinel


@pytest.fixture(autouse=True)
def _isolate_sentinel(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.session.autonomous_sentinel.tempfile.gettempdir",
        lambda: str(tmp_path),
    )
    return tmp_path


@pytest.fixture(autouse=True)
def _no_subprocess(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("autonomous_go must not spawn a process")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)


def _write_sentinel(session_id: str, content: str) -> None:
    path = autonomous_sentinel.sentinel_path(session_id)
    path.write_text(content, encoding="utf-8")


class TestEachSignalAlone:
    def test_autonomous_command_alone(self):
        _write_sentinel("s1", "autonomous")
        result = autonomous_go.active_signals("s1", {})
        assert result == [{"signal": "autonomous-command", "evidence": "autonomous"}]

    def test_job_mode_blitz_alone(self):
        result = autonomous_go.active_signals("s1", {"COORDINATOR_JOB_MODE": "blitz"})
        assert result == [
            {"signal": "job-mode", "evidence": "COORDINATOR_JOB_MODE=blitz"}
        ]

    def test_job_mode_cron_alone(self):
        result = autonomous_go.active_signals("s1", {"COORDINATOR_JOB_MODE": "cron"})
        assert result == [
            {"signal": "job-mode", "evidence": "COORDINATOR_JOB_MODE=cron"}
        ]

    def test_backgrounded_alone(self):
        result = autonomous_go.active_signals("s1", {}, backgrounded=True)
        assert result == [{"signal": "backgrounded", "evidence": "backgrounded"}]

    def test_pm_vocalized_alone(self):
        result = autonomous_go.active_signals("s1", {}, pm_go="just go")
        assert result == [{"signal": "pm-vocalized", "evidence": "just go"}]


class TestAllFourTogetherInOrder:
    def test_fixed_order(self):
        _write_sentinel("s1", "autonomous")
        result = autonomous_go.active_signals(
            "s1",
            {"COORDINATOR_JOB_MODE": "blitz"},
            backgrounded=True,
            pm_go="just go",
        )
        assert [r["signal"] for r in result] == [
            "autonomous-command",
            "job-mode",
            "backgrounded",
            "pm-vocalized",
        ]


class TestIgnoredInputs:
    def test_mise_en_place_sentinel_ignored(self):
        _write_sentinel("s1", "mise-en-place")
        assert autonomous_go.active_signals("s1", {}) == []

    def test_missing_sentinel_ignored(self):
        assert autonomous_go.active_signals("s1", {}) == []

    def test_interactive_job_mode_ignored(self):
        result = autonomous_go.active_signals("s1", {"COORDINATOR_JOB_MODE": "interactive"})
        assert result == []

    def test_empty_job_mode_ignored(self):
        assert autonomous_go.active_signals("s1", {"COORDINATOR_JOB_MODE": ""}) == []

    def test_unknown_job_mode_ignored(self):
        assert autonomous_go.active_signals("s1", {"COORDINATOR_JOB_MODE": "nightly"}) == []

    def test_fleet_record_saying_blitz_ignored(self, monkeypatch):
        def _boom():
            raise AssertionError("must never consult read_fleet_mode()")

        monkeypatch.setattr(
            "coordinator_core.session.fleet_mode.read_fleet_mode", _boom
        )
        assert autonomous_go.active_signals("s1", {}) == []

    def test_whitespace_only_pm_go_ignored(self):
        assert autonomous_go.active_signals("s1", {}, pm_go="   ") == []

    def test_none_pm_go_ignored(self):
        assert autonomous_go.active_signals("s1", {}, pm_go=None) == []

    def test_blank_session_id_contributes_nothing_never_raises(self):
        assert autonomous_go.active_signals("", {"COORDINATOR_JOB_MODE": "blitz"}) == [
            {"signal": "job-mode", "evidence": "COORDINATOR_JOB_MODE=blitz"}
        ]

    def test_none_env_never_raises(self):
        assert autonomous_go.active_signals("s1", None) == []


class TestPmGoStoredVerbatim:
    def test_not_stripped(self):
        result = autonomous_go.active_signals("s1", {}, pm_go="  just go  ")
        assert result == [{"signal": "pm-vocalized", "evidence": "  just go  "}]
