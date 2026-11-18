"""find_cli_cmd inside the warm engine never probes a PATH door (self-deadlock), and every
probe is bounded. Coordinator-content-repo-0e, 2026-11-17: workstream-complete apply with a lesson hung
the engine on `subprocess.run([cli_name, "--help"])` with no timeout."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "_queue_append_locator.py"
_loader = importlib.machinery.SourceFileLoader("_qal_warm_under_test", str(_MODULE_PATH))
_spec = importlib.util.spec_from_loader("_qal_warm_under_test", _loader)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_loader.exec_module(_mod)


def _recording_run(calls: list[list[str]], *, hang_on: str | None = None):
    def run(argv, **kwargs):
        calls.append(list(argv))
        assert "timeout" in kwargs, "every --help probe must be bounded"
        if hang_on is not None and argv[0] == hang_on:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run


def test_warm_engine_skips_path_door_and_resolves_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "coordinator-queue-append.py").write_text("", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setenv("COORDINATOR_EXECUTION_ROUTE", "warm_server")
    monkeypatch.setattr(_mod, "_resolve_python_interpreter", lambda: "python")
    monkeypatch.setattr(_mod.subprocess, "run", _recording_run(calls))

    cmd = _mod.find_cli_cmd(str(tmp_path), "coordinator-queue-append")

    assert cmd == ["python", str(tmp_path / "coordinator-queue-append.py")]
    assert all(c[0] == "python" for c in calls), f"probed a PATH door from warm: {calls}"


def test_cold_hung_door_probe_times_out_to_next_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.delenv("COORDINATOR_EXECUTION_ROUTE", raising=False)
    monkeypatch.setattr(
        _mod.subprocess, "run", _recording_run(calls, hang_on="coordinator-queue-append")
    )

    cmd = _mod.find_cli_cmd(str(tmp_path), "coordinator-queue-append")

    assert cmd == ["coordinator-queue-append.py"]
    assert calls[0][0] == "coordinator-queue-append"
