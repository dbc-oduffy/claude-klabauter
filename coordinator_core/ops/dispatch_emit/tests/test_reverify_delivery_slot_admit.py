"""The reverify test brief gates each multi-file pytest batch on box-memory-snapshot --admit-peak."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import reverify_delivery as rd


def _clause(n: int) -> str:
    return rd._scope_clause([f"coordinator_core/tests/test_t_{i:03d}.py" for i in range(n)], "", "abc123")


def test_multi_batch_set_carries_the_per_batch_admit_gate():
    clause = _clause(100)
    assert "box-memory-snapshot.py --command-key reverify-pytest --admit-peak" in clause
    assert "--register-pid" in clause and "--release-pid" in clause and "--record-peak" in clause
    assert "Exit 4 (HOLD)" in clause and "re-run the same admit" in clause
    assert "never skips, fails or stops the batch" in clause


def test_gate_leaves_stdin_assembly_forbidden_and_adds_no_substitution():
    gate = rd._ADMIT_GATE
    assert "$(" not in gate and "xargs" not in gate and "`" in gate
    assert "never assemble a command from stdin" in _clause(100)


def test_single_file_and_empty_sets_carry_no_gate():
    assert "box-memory-snapshot" not in _clause(1)
    assert "box-memory-snapshot" not in _clause(0)
    assert "box-memory-snapshot" in _clause(2)


def test_gate_flags_are_accepted_by_the_snapshot_cli():
    import re
    from pathlib import Path

    src = (Path(rd.__file__).parents[3] / "coordinator/bin/box-memory-snapshot.py").read_text(encoding="utf-8")
    snapshot_part = rd._ADMIT_GATE.replace(rd._SLOT_ADMIT_CMD, "")
    for flag in re.findall(r"--[a-z-]+", snapshot_part):
        assert f'"{flag}"' in src, flag


def test_slot_admit_command_flags_are_accepted_by_the_engine_cli():
    import re
    from pathlib import Path

    src = Path(rd.__file__).read_text(encoding="utf-8")
    for flag in re.findall(r"--[a-z-]+", rd._SLOT_ADMIT_CMD):
        assert f'"{flag}"' in src, flag


def test_admit_slot_calls_request_and_wait_in_mb(monkeypatch):
    from coordinator_core.group_em import slot_wait

    seen = {}

    def fake(sid, run, peak, **kw):
        seen.update(sid=sid, run=run, peak=peak)
        return {"admitted": False, "reason": "timeout"}

    monkeypatch.setattr(slot_wait, "request_and_wait", fake)
    r = rd.admit_slot("reverify-pytest", 4, session_id="s1", host=_host(1))
    assert r["admitted"] is False and "slot wait: timeout" in r["reason"]
    assert seen == {"sid": "s1", "run": "reverify-pytest", "peak": 4096}


def test_admit_cli_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(rd, "admit_slot", lambda run, gib, **k: {"admitted": False, "reason": "timeout"})
    assert rd.main(["admit", "--run", "r", "--peak-gib", "4"]) == 3
    assert "DENIED reason=timeout" in capsys.readouterr().out
    monkeypatch.setattr(rd, "admit_slot", lambda run, gib, **k: {"admitted": True, "reason": "granted"})
    assert rd.main(["admit", "--run", "r", "--peak-gib", "4"]) == 0


def test_memory_admit_needs_no_grant(monkeypatch):
    from coordinator_core.group_em import slot_wait

    monkeypatch.setattr(slot_wait, "request_and_wait", lambda *a, **k: 1 / 0)
    assert rd.admit_slot("reverify-pytest", 4, session_id="s1", host=_host(20))["admitted"] is True


def test_granted_wait_admits_a_held_batch(monkeypatch):
    from coordinator_core.group_em import slot_wait

    monkeypatch.setattr(slot_wait, "request_and_wait", lambda *a, **k: {"admitted": True, "reason": "granted"})
    assert rd.admit_slot("reverify-pytest", 4, session_id="s1", host=_host(1))["admitted"] is True


def test_composer_neither_sleeps_nor_spawns(monkeypatch):
    import subprocess
    import time

    def boom(*a, **k):
        raise AssertionError("composer must not sleep or spawn")

    monkeypatch.setattr(time, "sleep", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    clause = _clause(100)
    assert clause.index("--admit-peak") < clause.index("--release-pid")
    assert "Exit 4 (HOLD)" in clause and "up to 60 times" in clause


def _host(avail_gib):
    from coordinator_core.bash_guards.tests.test_heavy_command_class import _fake_host

    return _fake_host(avail_gib)[0]
