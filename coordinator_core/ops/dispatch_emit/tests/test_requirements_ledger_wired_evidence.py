"""A register row lands wired only with its consumer command and output: both writers read the
verdict item's `{command, observed}` and record `wired_evidence: {command, output}`; wired with none
lands unwired and partial. The ledger writer also re-runs the command at the landed tree
(`wired_check`): a failing, inadmissible or over-budget command lands unwired and is reported."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl, wired_check
from pathlib import Path

PLAN = "docs/plans/2026-11/p.md"
REL = "state/sizings/2026-11-04-evidence.yaml"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
EVIDENCE = {"command": "python ok.py", "observed": "R1 reached: 3 consumers"}


@pytest.fixture(autouse=True)
def _rerunnable(tmp_path, monkeypatch):
    """`ok.py` / `bad.py` are tracked scripts of the landing root; the re-run budget is loose."""
    (tmp_path / "ok.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
    monkeypatch.setattr(
        wired_check.census, "tracked_targets", lambda root, cands: frozenset(c for c in cands if c.endswith(".py"))
    )
    monkeypatch.setattr(rl, "WIRED_RERUN_BUDGET_S", 30.0)


def _sizing_text(status: str = "open", rows: int = 1):
    reg = [{"id": f"R{n}", "source_text": "wire it", "anchor": "wire it", "surface": "cli",
            "status": status, "claimed_by": [PLAN], "wired": False, "met_by": []} for n in range(1, rows + 1)]
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "requirement_register": {"rows": reg},
    }
    return yaml.safe_dump(doc, sort_keys=False)


def _land_items(tmp_path, items, demoted=None):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / REL).write_text(_sizing_text(rows=len(items)), encoding="utf-8")
    (tmp_path / ".git").mkdir()
    rl.apply_verdict(tmp_path, REL, {"items": items}, run_id="run-1", sha="abc1234", wired_demoted=demoted)
    return rr.read_register((tmp_path / REL).read_text(encoding="utf-8")).rows


def _ledger_row(tmp_path, item):
    return _land_items(tmp_path, [item])[0]


def _register_row(item):
    out = rr.apply_verdicts(_sizing_text(), [item], PLAN, "abc1234", NOW)
    return rr.read_register(out).rows[0]


@pytest.mark.parametrize("land", [_ledger_row, lambda _tmp, item: _register_row(item)], ids=["ledger", "register"])
class TestBothWriters:
    def test_wired_with_evidence_records_command_and_output(self, tmp_path: Path, land) -> None:
        row = land(tmp_path, {"id": "R1", "status": "met", "wired": True, **EVIDENCE})
        assert row["status"] == "met" and row["wired"] is True
        assert row["wired_evidence"] == {"command": EVIDENCE["command"], "output": EVIDENCE["observed"]}

    @pytest.mark.parametrize("missing", [{"command": ""}, {"observed": "  "}, {"command": None}, {"observed": 3}])
    def test_wired_with_incomplete_evidence_lands_unwired_partial(self, tmp_path: Path, land, missing) -> None:
        row = land(tmp_path, {"id": "R1", "status": "met", "wired": True, **{**EVIDENCE, **missing}})
        assert row["status"] == "partial" and row["wired"] is False
        assert "wired_evidence" not in row

    def test_wired_with_no_evidence_lands_unwired_partial(self, tmp_path: Path, land) -> None:
        row = land(tmp_path, {"id": "R1", "status": "met", "wired": True})
        assert row["status"] == "partial" and row["wired"] is False
        assert "wired_evidence" not in row

    def test_evidence_without_wired_true_records_nothing(self, tmp_path: Path, land) -> None:
        row = land(tmp_path, {"id": "R1", "status": "met", "wired": False, **EVIDENCE})
        assert row["wired"] is False and "wired_evidence" not in row


def test_wired_evidence_requires_both_non_empty_strings() -> None:
    assert rr.wired_evidence(EVIDENCE) == {"command": EVIDENCE["command"], "output": EVIDENCE["observed"]}
    assert rr.wired_evidence({"command": "x"}) is None


@pytest.mark.parametrize(
    "command,reason",
    [
        ("python bad.py", "exit 3"),
        ("python -m pytest ok.py", wired_check.NOT_RERUNNABLE),
        ("coordinator-invoke dispatch.land_run", wired_check.NOT_RERUNNABLE),
        ("python ok.py | wc -l", wired_check.NOT_RERUNNABLE),
    ],
)
def test_a_wired_command_that_fails_or_is_inadmissible_lands_unwired_and_is_reported(
    tmp_path: Path, command, reason
) -> None:
    demoted: list = []
    row = _land_items(tmp_path, [{"id": "R1", "status": "met", "wired": True, **EVIDENCE, "command": command}], demoted)[0]
    assert row["wired"] is False and row["status"] == "partial" and "wired_evidence" not in row
    assert demoted == [{"id": "R1", "command": command, "reason": reason}]


def test_an_inadmissible_command_is_never_spawned(tmp_path: Path, monkeypatch) -> None:
    spawned: list = []
    monkeypatch.setattr(wired_check.subprocess, "run", lambda *a, **k: spawned.append(a))
    _land_items(tmp_path, [{"id": "R1", "status": "met", "wired": True, **EVIDENCE, "command": "python -m pytest x.py"}])
    assert spawned == []


def test_a_passing_command_reports_no_demotion(tmp_path: Path) -> None:
    demoted: list = []
    row = _land_items(tmp_path, [{"id": "R1", "status": "met", "wired": True, **EVIDENCE}], demoted)[0]
    assert row["wired"] is True and row["status"] == "met" and demoted == []


def test_each_distinct_command_runs_once_per_landing(tmp_path: Path, monkeypatch) -> None:
    runs: list = []
    real = wired_check.rerun_wired_check
    monkeypatch.setattr(wired_check, "rerun_wired_check", lambda root, cmd, t: runs.append(cmd) or real(root, cmd, t))
    items = [{"id": f"R{n}", "status": "met", "wired": True, **EVIDENCE} for n in (1, 2, 3)]
    rows = _land_items(tmp_path, items)
    assert runs == ["python ok.py"] and all(r["wired"] is True for r in rows)


def test_a_landing_spawns_exactly_one_process_per_distinct_command(tmp_path: Path, monkeypatch) -> None:
    """The spawn count `wired_check.rerun_wired_check` contributes to every `apply_verdict` caller."""
    import subprocess

    real_run, spawns = subprocess.run, []

    def counting_run(argv, *a, **k):
        spawns.append(list(argv))
        return real_run(argv, *a, **k)

    monkeypatch.setattr(subprocess, "run", counting_run)
    items = [{"id": f"R{n}", "status": "met", "wired": True, **EVIDENCE} for n in (1, 2, 3)]
    rows = _land_items(tmp_path, items)
    assert len(spawns) == 1 and spawns[0][1:] == ["ok.py"] and all(r["wired"] is True for r in rows)


def test_a_spent_budget_demotes_every_further_command_unspawned(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(rl, "WIRED_RERUN_BUDGET_S", 0.0)
    spawned: list = []
    monkeypatch.setattr(wired_check.subprocess, "run", lambda *a, **k: spawned.append(a))
    demoted: list = []
    rows = _land_items(tmp_path, [{"id": "R1", "status": "met", "wired": True, **EVIDENCE}], demoted)
    assert spawned == [] and rows[0]["wired"] is False
    assert demoted[0]["reason"] == wired_check.NOT_RERUNNABLE


def test_the_resume_path_demotes_a_failing_command_too(tmp_path: Path) -> None:
    """terminal_commit_ledger's `_ledger_writeback` calls the same `apply_verdict`."""
    from types import SimpleNamespace

    from coordinator_core.ops.dispatch_emit import terminal_commit_ledger

    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / REL).write_text(_sizing_text(), encoding="utf-8")
    (tmp_path / ".git").mkdir()
    verdict = {"items": [{"id": "R1", "status": "met", "wired": True, **EVIDENCE, "command": "python bad.py"}]}
    terminal_commit_ledger._ledger_writeback(tmp_path, SimpleNamespace(sizing_rel=REL), verdict, "run-1", "abc1234")
    row = rr.read_register((tmp_path / REL).read_text(encoding="utf-8")).rows[0]
    assert row["wired"] is False and row["status"] == "partial"
