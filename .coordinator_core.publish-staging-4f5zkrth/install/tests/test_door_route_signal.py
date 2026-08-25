"""Tests for coordinator_core.install.door_route_signal -- the
through-the-door route discriminator (spec backlink:
docs/plans/2026-08-22-warm-engine-and-door-install-from-published-root.md
chunk C5).

AC5's shape: PASS only on a genuine WARM_SERVER-stamped row; a forced
fall-through must FAIL the check, not be silently absorbed. These tests
force both routes and both prove that distinction and prove the
discriminator-inert trap (module docstring) is caught rather than misread
as a fall-through.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core.install import door_route_signal


def _git_common_dir(tmp_path: Path) -> Path:
    common = tmp_path / ".git"
    common.mkdir()
    return common


def _sink_path(common_dir: Path) -> Path:
    return common_dir / "coordinator-sessions" / "logs" / "op-latency.jsonl"


def _write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _patch_repo(monkeypatch, tmp_path: Path) -> Path:
    common_dir = _git_common_dir(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir", lambda repo_root: common_dir
    )
    return common_dir


def test_read_door_route_classifies_warm_server_row(monkeypatch, tmp_path):
    common_dir = _patch_repo(monkeypatch, tmp_path)
    sink = _sink_path(common_dir)

    def fake_run(argv, **kwargs):
        now = time.time()
        _write_jsonl(
            sink,
            [{"op": "ping", "t_start": now, "kind": "complete", "route": "warm_server"}],
        )

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(door_route_signal.subprocess, "run", fake_run)

    result = door_route_signal.read_door_route(
        Path("/fake/door"), "ping", repo_root=tmp_path
    )
    assert result.route == door_route_signal.WARM_SERVER
    assert result.entry is not None


def test_read_door_route_classifies_forced_fall_through_as_in_process(monkeypatch, tmp_path):
    """AC5: a forced fall-through must be readable as a FAIL by the caller --
    proven here by asserting it is distinct from WARM_SERVER, never folded
    into it."""
    common_dir = _patch_repo(monkeypatch, tmp_path)
    sink = _sink_path(common_dir)

    def fake_run(argv, **kwargs):
        now = time.time()
        _write_jsonl(
            sink,
            [{"op": "ping", "t_start": now, "kind": "complete", "route": "in_process"}],
        )

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(door_route_signal.subprocess, "run", fake_run)

    result = door_route_signal.read_door_route(
        Path("/fake/door"), "ping", repo_root=tmp_path
    )
    assert result.route == door_route_signal.IN_PROCESS
    assert result.route != door_route_signal.WARM_SERVER


def test_read_door_route_no_row_is_unresolved(monkeypatch, tmp_path):
    _patch_repo(monkeypatch, tmp_path)

    def fake_run(argv, **kwargs):
        class _Result:
            returncode = 1

        return _Result()

    monkeypatch.setattr(door_route_signal.subprocess, "run", fake_run)

    result = door_route_signal.read_door_route(
        Path("/fake/door"), "ping", repo_root=tmp_path
    )
    assert result.route == door_route_signal.UNRESOLVED
    assert result.entry is None


def test_read_door_route_door_not_installed_is_unresolved_not_raising(monkeypatch, tmp_path):
    _patch_repo(monkeypatch, tmp_path)

    def fake_run(argv, **kwargs):
        raise FileNotFoundError("no such file: door")

    monkeypatch.setattr(door_route_signal.subprocess, "run", fake_run)

    result = door_route_signal.read_door_route(
        Path("/fake/door"), "ping", repo_root=tmp_path
    )
    assert result.route == door_route_signal.UNRESOLVED


def test_read_door_route_ignores_stale_rows_before_since(monkeypatch, tmp_path):
    """Only a row written AT OR AFTER the invocation's own start counts --
    an older warm_server row from a previous, unrelated call must not
    manufacture a false PASS for this invocation."""
    common_dir = _patch_repo(monkeypatch, tmp_path)
    sink = _sink_path(common_dir)
    stale_now = time.time() - 1000
    _write_jsonl(
        sink,
        [{"op": "ping", "t_start": stale_now, "kind": "complete", "route": "warm_server"}],
    )

    def fake_run(argv, **kwargs):
        class _Result:
            returncode = 1

        return _Result()

    monkeypatch.setattr(door_route_signal.subprocess, "run", fake_run)

    result = door_route_signal.read_door_route(
        Path("/fake/door"), "ping", repo_root=tmp_path
    )
    assert result.route == door_route_signal.UNRESOLVED


def test_read_door_route_ignores_started_rows(monkeypatch, tmp_path):
    common_dir = _patch_repo(monkeypatch, tmp_path)
    sink = _sink_path(common_dir)

    def fake_run(argv, **kwargs):
        now = time.time()
        _write_jsonl(
            sink,
            [
                {"op": "ping", "t_start": now, "kind": "started", "corr_id": "x", "route": "warm_server"},
            ],
        )

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(door_route_signal.subprocess, "run", fake_run)

    result = door_route_signal.read_door_route(
        Path("/fake/door"), "ping", repo_root=tmp_path
    )
    assert result.route == door_route_signal.UNRESOLVED


def test_run_cold_control_invocation_produces_in_process_row(monkeypatch, tmp_path):
    """Bypasses the door and the warm server entirely -- this process never
    stamps op_latency.ROUTE_ENV, so the sink row it writes must read back
    as in_process, guaranteed rather than assumed."""
    common_dir = _patch_repo(monkeypatch, tmp_path)
    monkeypatch.delenv(
        __import__("coordinator_core.telemetry.op_latency", fromlist=["ROUTE_ENV"]).ROUTE_ENV,
        raising=False,
    )

    result = door_route_signal.run_cold_control_invocation("ping", repo_root=tmp_path)
    assert result.route == door_route_signal.IN_PROCESS
    assert result.entry is not None
    assert result.entry["op"] == "ping"

    sink = _sink_path(common_dir)
    assert sink.is_file()


def test_run_cold_control_invocation_unresolved_when_sink_inert(monkeypatch, tmp_path):
    """The discriminator-inert trap (module docstring): when the sink itself
    cannot be resolved/written (kill switch here), even the guaranteed-cold
    control invocation reads back UNRESOLVED -- callers must read this as
    'discriminator unavailable', never as a genuine fall-through."""
    _patch_repo(monkeypatch, tmp_path)
    monkeypatch.setenv("COORDINATOR_OP_LATENCY_DISABLE", "1")

    result = door_route_signal.run_cold_control_invocation("ping", repo_root=tmp_path)
    assert result.route == door_route_signal.UNRESOLVED
    assert result.entry is None


def test_repo_root_is_required_keyword(tmp_path):
    """F5: repo_root must be an explicit, required argument -- no default
    that could silently fall back to an ambient sink."""
    with pytest.raises(TypeError):
        door_route_signal.read_door_route(Path("/fake/door"), "ping")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        door_route_signal.run_cold_control_invocation("ping")  # type: ignore[call-arg]
