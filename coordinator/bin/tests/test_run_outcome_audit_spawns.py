"""Tests for coordinator/bin/run-outcome-audit.py over a fixture corpus under tmp_path."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

_SCRIPT = Path(__file__).resolve().parents[1] / "run-outcome-audit.py"
_spec = importlib.util.spec_from_file_location("run_outcome_audit", _SCRIPT)
audit_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit_mod)

PM_TEXT = "wire the thing together"
CRITERION = "the thing is wired"
SIZING_REL = "state/sizings/2026-11/s1.yaml"


def _write(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc) if not isinstance(doc, str) else doc, encoding="utf-8")


def _outcome(status="met", observation="verified", tests_status="pass", run=12):
    return {
        "inline_review": {
            "criterion": {"status": status, "observation": observation},
            "tests": {"status": tests_status, "run": run, "failed": 0},
        }
    }


def _run(root: Path, name: str, *, halted=None, stage=None, outcome=None, owed=None, sizing=True):
    d = root / "state" / "warp" / name
    d.mkdir(parents=True, exist_ok=True)
    if halted is not None:
        _write(d / "halted.json", halted)
    if stage is not None:
        _write(d / "run-stage.json", {"stage": stage})
    if sizing:
        _write(d / "run.json", {"sizing_rel": SIZING_REL})
    if outcome is not None:
        _write(d / "run-outcome.json", outcome)
    if owed is not None:
        _write(d / "manifest.json", {"owed": owed})


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(
        root / SIZING_REL,
        "title: x\n"
        "exit_criterion:\n"
        f"  statement: {CRITERION}\n"
        "pm_verbatims:\n"
        "  - turn: 0\n"
        "    ts: '2026-11-04T00:00:00Z'\n"
        f"    text: '{PM_TEXT}'\n"
        "requirement_register:\n  rows: []\n",
    )
    _run(root, "warp-a-green", halted={"status": "landed"}, outcome=_outcome())
    _run(root, "warp-b-nojudge", halted={"status": "landed"})
    _run(root, "warp-c-noevidence", halted={"status": "closed"}, outcome=_outcome(tests_status="not_run", run=0))
    _run(root, "warp-d-halted", halted={"status": "halted", "halted": "commit"}, outcome=_outcome(status="not_met"))
    _run(root, "warp-e-owed", halted={"status": "landed"}, outcome=_outcome(), owed=[{"id": "C2", "reason": "em-performed"}])
    _run(root, "warp-f-live", stage="staged")
    _run(root, "warp-g-stage-landed", stage="landed", outcome=_outcome())
    (root / "state" / "warp" / ".coordinator-local").mkdir()
    return root


def _cli(root: Path, cwd: Path, *args: str):
    return subprocess.run(
        [sys.executable, "-I", str(_SCRIPT), "--root", str(root), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_cli_exits_1_with_red_rows_from_foreign_cwd(corpus, tmp_path):
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()
    res = _cli(corpus, foreign)
    assert res.returncode == 1
    assert "RED warp-b-nojudge" in res.stdout
    assert "runs=7 red=2" in res.stdout


def test_cli_exits_0_when_no_red(tmp_path):
    root = tmp_path / "r"
    _run(root, "warp-ok", halted={"status": "landed"}, outcome=_outcome(), sizing=False)
    assert _cli(root, tmp_path).returncode == 0


def test_cli_red_only_hides_other_rows(corpus, tmp_path):
    res = _cli(corpus, tmp_path, "--red-only")
    assert "warp-a-green" not in res.stdout and "warp-b-nojudge" in res.stdout


def test_cli_missing_root_exits_2(tmp_path):
    assert _cli(tmp_path / "nope", tmp_path).returncode == 2


_EM_ROWS = [
    {"id": "A", "performer": "em"},
    {"id": "B", "em_run": True},
    {"id": "C", "agent_type": "coordinator:em"},
    {"id": "D", "agent_type": "coordinator:executor"},
    {"id": "E", "performer": "executor", "em_run": False},
    {"id": "F"},
]


def _plan_with_owed(root: Path, dispositions: dict, discharged=()):
    plan = root / "docs" / "plans" / "p.md"
    rows = "".join(f"- id: {k}\n  title: t\n  disposition: {v}\n" for k, v in dispositions.items())
    _write(plan, f"# p\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n")
    if discharged:
        ev = "em_discharged:\n" + "".join(f"  {i}:\n    recorded_at: '2026-11-04T00:00:00Z'\n    text: x\n" for i in discharged)
        _write(root / "docs" / "plans" / "p.evidence.yaml", ev)
    leg_dir = root / "state" / "owed-legs"
    for row_id in dispositions:
        _write(
            leg_dir / f"run-1--{row_id}.yaml",
            f"id: {row_id}\nrun_id: run-1\nplan: docs/plans/p.md\nreason: em_performed\ndetail: ''\ncreated: '2026-11-04T00:00:00Z'\n",
        )


def _owed_lines(root: Path, tmp_path: Path) -> list:
    """OWED lines the default CLI run prints."""
    return [ln for ln in _cli(root, tmp_path).stdout.splitlines() if ln.startswith("OWED ")]


def test_cli_prints_open_owed_legs_only(tmp_path):
    root = tmp_path / "r"
    _run(root, "warp-ok", halted={"status": "landed"}, outcome=_outcome(), sizing=False)
    _plan_with_owed(root, {"C1": "open", "C2": "resolved", "C3": "open", "C4": "open"}, discharged=["C3"])
    assert _owed_lines(root, tmp_path) == [
        "OWED run-1 C1 (em_performed) docs/plans/p.md",
        "OWED run-1 C4 (em_performed) docs/plans/p.md",
    ]


def test_owed_leg_disappears_when_disposition_changes(tmp_path):
    root = tmp_path / "r"
    _run(root, "warp-ok", halted={"status": "landed"}, outcome=_outcome(), sizing=False)
    _plan_with_owed(root, {"C1": "open"})
    assert len(_owed_lines(root, tmp_path)) == 1
    _plan_with_owed(root, {"C1": "resolved"})
    assert _owed_lines(root, tmp_path) == []


def test_owed_leg_with_unreadable_plan_stays_open(tmp_path):
    root = tmp_path / "r"
    _run(root, "warp-ok", halted={"status": "landed"}, outcome=_outcome(), sizing=False)
    _plan_with_owed(root, {"C1": "open"})
    (root / "docs" / "plans" / "p.md").unlink()
    assert len(_owed_lines(root, tmp_path)) == 1
