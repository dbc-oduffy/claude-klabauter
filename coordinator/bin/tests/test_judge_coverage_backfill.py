"""Tests for coordinator/bin/judge-coverage-backfill.py over a tmp_path state/warp fixture."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_BIN = Path(__file__).resolve().parents[1]


def _mod(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, _BIN / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


backfill = _mod("judge_coverage_backfill", "judge-coverage-backfill.py")
audit = _mod("run_outcome_audit_for_backfill", "run-outcome-audit.py")


def _run(root: Path, name: str, *, halted=None, stage=None, outcome=None) -> Path:
    d = root / "state" / "warp" / name
    d.mkdir(parents=True)
    if halted is not None:
        (d / "halted.json").write_text(json.dumps(halted), encoding="utf-8")
    if stage is not None:
        (d / "run-stage.json").write_text(json.dumps({"stage": stage}), encoding="utf-8")
    if outcome is not None:
        (d / "run-outcome.json").write_text(json.dumps(outcome), encoding="utf-8")
    return d


MET = {"inline_review": {"criterion": {"status": "met", "observation": "ok"}}}


def _fixture(tmp_path: Path) -> dict:
    return {
        "landed": _run(tmp_path, "r-landed", stage="landed"),
        "closed": _run(tmp_path, "r-closed", halted={"status": "closed", "closed": {"reason": "x"}}),
        "met": _run(tmp_path, "r-met", stage="landed", outcome=MET),
        "halted": _run(tmp_path, "r-halted", halted={"status": "halted", "halted": "k"}),
    }


def _snapshot(paths) -> dict:
    return {p.name: {f.name: f.read_bytes() for f in p.iterdir()} for p in paths}


def _judge_none_concluded(root: Path) -> int:
    return sum(1 for r in audit.audit(str(root)) if r["terminal"] in ("committed", "closed") and r["judge"] == "none")


def test_backfill_clears_concluded_judge_none(tmp_path, capsys):
    runs = _fixture(tmp_path)
    before = _snapshot([runs["met"], runs["halted"]])
    assert _judge_none_concluded(tmp_path) == 2
    assert backfill.main(["--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "scanned=3 recorded=2"
    assert _judge_none_concluded(tmp_path) == 0
    assert _snapshot([runs["met"], runs["halted"]]) == before
    assert not (runs["halted"] / "run-outcome.json").exists()


def test_backfill_is_idempotent(tmp_path, capsys):
    _fixture(tmp_path)
    backfill.main(["--root", str(tmp_path)])
    capsys.readouterr()
    backfill.main(["--root", str(tmp_path)])
    assert capsys.readouterr().out.strip() == "scanned=3 recorded=0"


def test_dry_run_writes_nothing_and_prints_count(tmp_path, capsys):
    runs = _fixture(tmp_path)
    before = _snapshot(runs.values())
    assert backfill.main(["--root", str(tmp_path), "--dry-run"]) == 0
    assert capsys.readouterr().out.strip() == "scanned=3 recorded=2"
    assert _snapshot(runs.values()) == before


def test_missing_state_warp_exits_2(tmp_path):
    assert backfill.main(["--root", str(tmp_path)]) == 2
