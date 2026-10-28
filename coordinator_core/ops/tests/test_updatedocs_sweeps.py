"""
Tests for the "updatedocs.sweeps" op (coordinator_core.ops.updatedocs_sweeps): the
mutating 11i queue prune and 11j sidecar reap that left "updatedocs.gates".
"""

from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

import coordinator_core.ops.updatedocs_sweeps as uds
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops import updatedocs_gates as udg


def _write_fake_cli(path: Path, exit_code: int, stdout: str = "", stderr: str = "") -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _no_spawn(*args, **kwargs):
    raise AssertionError(f"default route must not spawn: {args}")


# ---------------------------------------------------------------------------
# Registration and the read/write split
# ---------------------------------------------------------------------------


def test_sweeps_registered_mutating_and_gates_read_only():
    assert "updatedocs.sweeps" in _REGISTRY
    assert OP_CLASSIFICATION["updatedocs.sweeps"] is OpClass.MUTATING
    assert OP_CLASSIFICATION["updatedocs.gates"] is OpClass.COMPUTE_ONLY


@pytest.mark.parametrize("sweep_id", ["11i-queue-prune-sweep", "11j-reap-stale-sidecars"])
def test_gates_refuses_the_moved_sweeps_by_name(tmp_path, sweep_id):
    with pytest.raises(ValueError, match="unknown gate"):
        udg._updatedocs_gates({"repo_root": str(tmp_path), "gates": [sweep_id]})
    assert sweep_id not in udg._GATES
    assert sweep_id in uds._SWEEPS


def test_default_gates_run_writes_nothing(tmp_path, monkeypatch):
    share = tmp_path / ".coordinator-local" / "subagent-share" / "dead-session"
    share.mkdir(parents=True)
    old = share / "old.md"
    old.write_text("---\nstatus: complete\n---\n", encoding="utf-8")
    ancient = time.time() - 40 * 86400
    os.utime(old, (ancient, ancient))
    queue = tmp_path / "state" / "improvement-queue.md"
    queue.parent.mkdir()
    queue.write_text("## Open\n- a\n## Resolved\n- done\n", encoding="utf-8")
    before = queue.read_text(encoding="utf-8")
    monkeypatch.setattr("coordinator_core.session.liveness.session_live", lambda sid, cwd=None: False)

    udg._updatedocs_gates({"repo_root": str(tmp_path)})

    assert old.exists()
    assert queue.read_text(encoding="utf-8") == before


def test_sweeps_op_prunes_and_reaps_the_same_fixture(tmp_path, monkeypatch):
    share = tmp_path / ".coordinator-local" / "subagent-share" / "dead-session"
    share.mkdir(parents=True)
    old = share / "old.md"
    old.write_text("---\nstatus: complete\n---\n", encoding="utf-8")
    ancient = time.time() - 40 * 86400
    os.utime(old, (ancient, ancient))
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "improvement-queue.md").write_text("## Open\n- a\n", encoding="utf-8")
    monkeypatch.setattr("coordinator_core.session.liveness.session_live", lambda sid, cwd=None: False)

    out = uds._updatedocs_sweeps({"repo_root": str(tmp_path), "settings_home": str(tmp_path)})

    ids = [g["gate_id"] for g in out["gates"]]
    assert ids == ["11i-queue-prune-sweep", "11j-reap-stale-sidecars"]
    assert "rollup" in out
    assert not old.exists()


# ---------------------------------------------------------------------------
# 11i-queue-prune-sweep
# ---------------------------------------------------------------------------


def test_queue_prune_sweep_yaml_leg_failure_is_blocking_not_clean(tmp_path):
    (tmp_path / "state" / "improvement-queue").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_cli(bin_dir / "prune-closed-improvements.py", 1, stderr="cannot lock ref 'HEAD'\n")
    result = uds._gate_queue_prune_sweep(tmp_path, tmp_path, {})
    assert result.verdict == udg.GateVerdict.FINDING
    assert result.severity == udg.Severity.BLOCKING
    assert any("YAML prune FAILED" in line for line in result.detail["lines"])


def test_queue_prune_sweep_yaml_leg_clean_exit_is_not_blocking(tmp_path):
    (tmp_path / "state" / "improvement-queue").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_cli(bin_dir / "prune-closed-improvements.py", 0)
    result = uds._gate_queue_prune_sweep(tmp_path, tmp_path, {})
    assert result.verdict == udg.GateVerdict.CLEAN


def test_queue_prune_sweep_default_route_prunes_every_queue_in_process(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "improvement-queue.md").write_text("## Open\n- a\n")
    (tmp_path / "state" / "bug-backlog.md").write_text("## Open\n- b\n")
    monkeypatch.setattr(uds, "_run", _no_spawn)

    result = uds._gate_queue_prune_sweep(tmp_path, tmp_path, {})

    assert result.verdict in (udg.GateVerdict.CLEAN, udg.GateVerdict.FINDING)
    assert result.severity != udg.Severity.BLOCKING


def test_queue_prune_sweep_override_cli_keeps_one_spawn_per_queue(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "improvement-queue.md").write_text("## Open\n- a\n")
    (tmp_path / "state" / "bug-backlog.md").write_text("## Open\n- b\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_cli = bin_dir / "single-arg-prune.py"
    fake_cli.write_text(
        "import sys\n"
        "assert len(sys.argv) == 2, sys.argv\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )

    result = uds._gate_queue_prune_sweep(tmp_path, tmp_path, {"prune_cli": str(fake_cli)})

    assert result.verdict == udg.GateVerdict.CLEAN


def test_queue_prune_sweep_default_route_reports_which_queue_failed(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "improvement-queue.md").write_text("## Open\n- a\n")
    (tmp_path / "state" / "not-a-queue.md").write_text("## Open\n- b\n")

    result = uds._gate_queue_prune_sweep(
        tmp_path,
        tmp_path,
        {"queues": ["state/improvement-queue.md", "state/not-a-queue.md"]},
    )

    assert result.verdict == udg.GateVerdict.FINDING
    assert result.severity == udg.Severity.BLOCKING
    assert any("not-a-queue.md" in line for line in result.detail["lines"])


# ---------------------------------------------------------------------------
# 11j-reap-stale-sidecars
# ---------------------------------------------------------------------------


def test_reap_sidecars_gate_spawns_an_explicit_cli_override_instead_of_reporting_cli_not_found(tmp_path, monkeypatch):
    """`_run` is stubbed because a fabricated `.exe` cannot be spawned; what this
    asserts is that the sweep reached a spawn with the forwarder's path rather than
    short-circuiting to UNAVAILABLE on an exact-name miss."""
    monkeypatch.setattr(udg.sys, "platform", "win32")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    bin_dir = tmp_path / "settings" / "bin"
    bin_dir.mkdir(parents=True)
    forwarder = bin_dir / "reap-stale-subagent-sidecars.exe"
    forwarder.write_bytes(b"MZ")

    spawned: dict[str, Path] = {}

    def _fake_run(cli_path, args, **kwargs):
        spawned["cli"] = cli_path
        return subprocess.CompletedProcess([str(cli_path), *args], 0, "reaped 0 sidecars", "")

    monkeypatch.setattr(uds, "_run", _fake_run)
    result = uds._gate_reap_stale_sidecars(tmp_path, tmp_path / "settings", {"cli": str(forwarder)})

    assert spawned["cli"] == forwarder
    assert result.verdict is not udg.GateVerdict.UNAVAILABLE


def test_reap_gate_default_route_runs_in_process_and_reads_the_reap_count(tmp_path, monkeypatch):
    monkeypatch.setattr(uds, "_run", _no_spawn)
    share = tmp_path / ".coordinator-local" / "subagent-share" / "dead-session"
    share.mkdir(parents=True)
    old = share / "old.md"
    old.write_text("---\nstatus: complete\n---\n", encoding="utf-8")
    ancient = time.time() - 40 * 86400
    os.utime(old, (ancient, ancient))
    monkeypatch.setattr("coordinator_core.session.liveness.session_live", lambda sid, cwd=None: False)

    result = uds._gate_reap_stale_sidecars(tmp_path, tmp_path, {})

    assert result.verdict == udg.GateVerdict.FINDING
    assert result.detail == {"reaped": 1}
    assert not old.exists()


def test_reap_gate_default_route_keeps_a_blocked_sidecar_and_reports_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(uds, "_run", _no_spawn)
    share = tmp_path / ".coordinator-local" / "subagent-share" / "dead-session"
    share.mkdir(parents=True)
    kept = share / "blocked.md"
    kept.write_text("---\nstatus: blocked\n---\n", encoding="utf-8")
    ancient = time.time() - 40 * 86400
    os.utime(kept, (ancient, ancient))
    monkeypatch.setattr("coordinator_core.session.liveness.session_live", lambda sid, cwd=None: False)

    result = uds._gate_reap_stale_sidecars(tmp_path, tmp_path, {})

    assert result.verdict == udg.GateVerdict.CLEAN
    assert kept.exists()


def test_reap_gate_cli_override_still_spawns_that_path(tmp_path):
    cli = tmp_path / "fake-reap.py"
    _write_fake_cli(cli, 0, stdout="reaped 3 sidecars\n")
    result = uds._gate_reap_stale_sidecars(tmp_path, tmp_path, {"cli": str(cli)})
    assert result.verdict == udg.GateVerdict.FINDING
    assert result.detail == {"reaped": 3}


def test_reap_gate_default_route_crash_is_a_blocking_finding_not_clean(tmp_path, monkeypatch):
    (tmp_path / ".coordinator-local" / "subagent-share" / "s").mkdir(parents=True)

    def _boom(*args, **kwargs):
        raise RuntimeError("liveness exploded")

    monkeypatch.setattr("coordinator_core.sidecar_reap.classify_sidecars", _boom)
    result = uds._gate_reap_stale_sidecars(tmp_path, tmp_path, {})
    assert result.verdict == udg.GateVerdict.FINDING
    assert result.severity == udg.Severity.BLOCKING
    assert "liveness exploded" in result.detail["stderr"]
