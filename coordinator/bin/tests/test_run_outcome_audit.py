"""Tests for coordinator/bin/run-outcome-audit.py over a fixture corpus under tmp_path."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

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


def _rows(root: Path, **kw) -> dict:
    return {r["run_id"]: r for r in audit_mod.audit(str(root), **kw)}


def test_one_row_per_run_dir_skipping_dot_dirs(corpus):
    assert sorted(_rows(corpus)) == [
        "warp-a-green",
        "warp-b-nojudge",
        "warp-c-noevidence",
        "warp-d-halted",
        "warp-e-owed",
        "warp-f-live",
        "warp-g-stage-landed",
    ]


def test_committed_met_with_evidence_is_green(corpus):
    row = _rows(corpus)["warp-a-green"]
    assert (row["terminal"], row["judge"], row["backed"], row["red"]) == ("committed", "met", True, False)
    assert "run=12" in row["evidence"]
    assert row["pm_verbatim"] == PM_TEXT and row["exit_criterion"] == CRITERION


def test_committed_without_judge_is_red(corpus):
    row = _rows(corpus)["warp-b-nojudge"]
    assert (row["judge"], row["red"]) == ("none", True)


def test_closed_met_without_command_output_is_red(corpus):
    row = _rows(corpus)["warp-c-noevidence"]
    assert (row["terminal"], row["judge"], row["backed"], row["red"]) == ("closed", "met", False, True)


def test_met_with_blank_observation_is_red(tmp_path):
    root = tmp_path / "r"
    _run(root, "warp-x", halted={"status": "landed"}, outcome=_outcome(observation="  "), sizing=False)
    assert _rows(root)["warp-x"]["red"] is True


def test_halted_and_live_are_never_red(corpus):
    rows = _rows(corpus)
    assert rows["warp-d-halted"]["terminal"] == "halted" and not rows["warp-d-halted"]["red"]
    assert rows["warp-d-halted"]["halt_kind"] == "commit"
    assert rows["warp-f-live"]["terminal"] == "live" and not rows["warp-f-live"]["red"]


def test_stage_landed_without_halted_json_is_committed(corpus):
    assert _rows(corpus)["warp-g-stage-landed"]["terminal"] == "committed"


def test_owed_rows_listed_from_manifest(corpus):
    assert _rows(corpus)["warp-e-owed"]["owed"] == ["C2:em-performed"]


def test_all_flag_reads_columns_for_halted_runs(corpus):
    assert _rows(corpus)["warp-d-halted"]["judge"] == "-"
    assert _rows(corpus, read_all=True)["warp-d-halted"]["judge"] == "not_met"


def test_corrupt_halted_json_is_unknown_not_a_crash(tmp_path):
    root = tmp_path / "r"
    d = root / "state" / "warp" / "warp-bad"
    d.mkdir(parents=True)
    (d / "halted.json").write_text("{not json", encoding="utf-8")
    row = _rows(root)["warp-bad"]
    assert row["terminal"] == "unknown" and row["halt_kind"] == "unreadable halted.json" and not row["red"]


def test_missing_sizing_prints_marker(tmp_path):
    root = tmp_path / "r"
    _run(root, "warp-x", halted={"status": "landed"}, outcome=_outcome())
    assert _rows(root)["warp-x"]["pm_verbatim"] == "(sizing missing)"


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


def test_audit_spawns_no_subprocess(corpus, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    assert audit_mod.main(["--root", str(corpus)]) == 1


_EM_ROWS = [
    {"id": "A", "performer": "em"},
    {"id": "B", "em_run": True},
    {"id": "C", "agent_type": "coordinator:em"},
    {"id": "D", "agent_type": "coordinator:executor"},
    {"id": "E", "performer": "executor", "em_run": False},
    {"id": "F"},
]


@pytest.mark.parametrize("raw", _EM_ROWS, ids=lambda r: r["id"])
def test_em_performed_mirror_agrees_with_engine_predicate(raw):
    pytest.importorskip("coordinator_core.ops.dispatch_emit.em_only_act")
    from coordinator_core.ops.dispatch_emit.em_only_act import row_is_em_performed

    assert audit_mod.em_performed_row(raw) == row_is_em_performed(raw)


def test_em_performed_mirror_documents_body_leg_gap():
    body_only = {"id": "R", "title": "ask coordinator:apm for a ruling", "body": "ask `coordinator:apm` for a ruling"}
    assert audit_mod.em_performed_row(body_only) is False


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


def test_owed_legs_agree_with_engine_open_owed_legs(tmp_path):
    from coordinator_core.ops.dispatch_emit.owed_leg_queue import open_owed_legs

    root = tmp_path / "r"
    _plan_with_owed(root, {"C1": "open", "C2": "resolved", "C3": "open"}, discharged=["C3"])
    assert [r["id"] for r in audit_mod.open_owed_legs(str(root))] == [r["id"] for r in open_owed_legs(root)] == ["C1"]


def _applied_run(root: Path, name: str, *, fixes: int, diff_paths, edited_in_outcome=False):
    """A committed run whose wave sidecar ledgers five applied rows, F1..F5 citing f1.py..f5.py."""
    ledger = [{"id": f"F{i}", "file": f"f{i}.py", "before": "a", "after": "b"} for i in range(1, 6)]
    sidecar = root / ".coordinator-local" / "subagent-share" / "s" / f"{name}.md"
    _write(
        sidecar,
        "---\nagent_type: coordinator:code-reviewer\n---\n## Findings\n\n### Finding 1: a\n\n"
        f"## Findings Ledger\n\n```json\n{json.dumps(ledger)}\n```\n",
    )
    outcome = _outcome()
    outcome["inline_review"].update({"fixes": fixes, "wave_sidecar_paths": [sidecar.relative_to(root).as_posix()]})
    if edited_in_outcome:
        outcome["inline_review"]["tests"]["edited"] = list(diff_paths)
    _run(root, name, halted={"status": "landed"}, outcome=outcome)
    if not edited_in_outcome:
        _write(root / "state" / "warp" / name / "manifest.json", {"write_baseline": {p: None for p in diff_paths}})


def test_applied_above_diff_evidence_is_overstated_and_red(tmp_path):
    root = tmp_path / "repo"
    _applied_run(root, "warp-over", fixes=5, diff_paths=["f2.py"])

    row = _rows(root)["warp-over"]

    assert (row["applied_recorded"], row["applied_evidenced"], row["applied_overstated"]) == (5, 1, True)
    assert row["backed"] is True and row["red"] is True
    assert "OVERSTATED applied=5/1" in audit_mod.format_row(row, 100)


def test_applied_matching_diff_evidence_stays_green(tmp_path):
    root = tmp_path / "repo"
    _applied_run(root, "warp-ok", fixes=2, diff_paths=["./f1.py", "f2.py"])

    row = _rows(root)["warp-ok"]

    assert (row["applied_recorded"], row["applied_evidenced"], row["applied_overstated"], row["red"]) == (2, 2, False, False)
    assert "OVERSTATED" not in audit_mod.format_row(row, 100)


def test_run_outcome_recorded_diff_wins_over_manifest_footprint(tmp_path):
    root = tmp_path / "repo"
    _applied_run(root, "warp-diff", fixes=3, diff_paths=["f1.py", "f2.py"], edited_in_outcome=True)
    _write(root / "state" / "warp" / "warp-diff" / "manifest.json", {"write_baseline": {f"f{i}.py": None for i in range(1, 6)}})

    row = _rows(root)["warp-diff"]

    assert (row["applied_evidenced"], row["applied_overstated"]) == (2, True)


def test_run_recording_no_diff_evidences_nothing(tmp_path):
    root = tmp_path / "repo"
    _applied_run(root, "warp-nodiff", fixes=1, diff_paths=[])

    row = _rows(root)["warp-nodiff"]

    assert (row["applied_evidenced"], row["applied_overstated"], row["red"]) == (0, True, True)


def test_wave_entry_applied_is_the_recorded_count_over_fixes(tmp_path):
    root = tmp_path / "repo"
    _applied_run(root, "warp-wave", fixes=0, diff_paths=["f1.py"])
    out = root / "state" / "warp" / "warp-wave" / "run-outcome.json"
    doc = json.loads(out.read_text(encoding="utf-8"))
    doc["inline_review"]["wave_reviews"] = [{"verdict": "WARN", "applied": 4}]
    _write(out, doc)

    assert _rows(root)["warp-wave"]["applied_recorded"] == 4


def test_halted_run_with_overstated_applied_is_never_red(tmp_path):
    root = tmp_path / "repo"
    _applied_run(root, "warp-h", fixes=5, diff_paths=["f2.py"])
    _write(root / "state" / "warp" / "warp-h" / "halted.json", {"status": "halted", "halted": "commit"})

    assert _rows(root, read_all=True)["warp-h"]["red"] is False


def test_stdlib_evidence_agrees_with_diff_evidenced_rows_on_the_0ec98fb89e_shape(tmp_path):
    from coordinator_core.ops.review_findings_ledger import diff_evidenced_rows, normalize_run_diff

    root = tmp_path / "repo"
    _applied_run(root, "warp-shape", fixes=5, diff_paths=["f2.py"])
    ledger = audit_mod._ledger_rows((root / ".coordinator-local/subagent-share/s/warp-shape.md").read_text(encoding="utf-8"))
    ledger.append({"id": "F6", "status": "applied"})
    ledger.append({"id": "F7", "file": "f2.py", "status": "applied", "out_of_ask": True})

    engine = len(diff_evidenced_rows(ledger, normalize_run_diff(["./f2.py"])))

    assert engine == _rows(root)["warp-shape"]["applied_evidenced"] == 1
