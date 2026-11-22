"""RULINGS class of the mise-prep bar: a plan deferring or contradicting an accepted PM ruling."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.tests.test_plan_prep_gate import _gate, _plan, _repo
from coordinator_core.roadmap import prep_gate as pg
from coordinator_core.rulings import store
from coordinator_core.rulings.contract import Ruling

_RULING = Ruling(
    id="private-repo-no-relitigate",
    pm_turn="state/sizings/x.yaml R5",
    scope=("private",),
    text="This is a private repo; do not relitigate it.",
    recorded="2026-11-21",
)


def _seed(root: Path) -> None:
    assert store.append(root, _RULING).get("applied") == [_RULING.id]


def _plan_with(root: Path, extra: str) -> str:
    rel = _plan(root)
    p = root / rel
    p.write_text(p.read_text(encoding="utf-8") + "\n" + extra + "\n", encoding="utf-8")
    return rel


def test_rulings_sits_before_register_and_done_clause():
    assert pg.CLASS_ORDER[-3:] == ("RULINGS", "REGISTER", "DONE_CLAUSE")


def test_deferring_plan_through_the_op_gets_a_blocking_rulings_finding(tmp_path):
    common = _repo(tmp_path)
    _seed(tmp_path)
    rel = _plan_with(tmp_path, "Whether the private repo needs a licence is pending a PM ruling?")
    result = _gate({"plan": rel}, common)
    cls = result["classes"]["RULINGS"]
    assert result["verdict"] == pg.NOT_PREPPED
    assert cls["status"] == "DEFECT"
    assert cls["kind"] == pg.RULINGS_KIND
    assert _RULING.id in cls["detail"]
    assert "coordinator:apm" in cls["detail"]
    assert "mise-prep-upgrade" not in result["message"]


def test_unnamed_deferral_is_blocking_even_with_an_empty_register(tmp_path):
    common = _repo(tmp_path)
    rel = _plan_with(tmp_path, "This part is awaiting the PM.")
    cls = _gate({"plan": rel}, common)["classes"]["RULINGS"]
    assert cls["status"] == "DEFECT"
    assert "unnamed" in cls["detail"]


def test_clean_plan_passes_the_class(tmp_path):
    common = _repo(tmp_path)
    _seed(tmp_path)
    result = _gate({"plan": _plan(tmp_path)}, common)
    assert result["classes"]["RULINGS"]["status"] == "PASS"
    assert result["verdict"] == pg.PREPPED


def test_contradiction_is_blocking(tmp_path):
    _repo(tmp_path)
    _seed(tmp_path)
    rel = _plan_with(tmp_path, f"We are not honouring {_RULING.id} here.")
    report = pg.gate_plan(tmp_path, tmp_path / rel)
    assert report["classes"]["RULINGS"]["status"] == "DEFECT"
    assert _RULING.id in report["classes"]["RULINGS"]["detail"]


def test_a_newer_pm_turn_citation_clears_the_finding(tmp_path):
    _repo(tmp_path)
    _seed(tmp_path)
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "turn.md").write_text("PM said so", encoding="utf-8")
    rel = _plan_with(
        tmp_path,
        "Whether the private repo needs a licence is pending a PM ruling?\n"
        f"pm_turn: state/turn.md 2026-11-22 {_RULING.id}",
    )
    assert pg.gate_plan(tmp_path, tmp_path / rel)["classes"]["RULINGS"]["status"] == "PASS"


def test_the_gate_reads_the_text_it_was_handed_not_the_file(tmp_path):
    _repo(tmp_path)
    _seed(tmp_path)
    rel = _plan(tmp_path)
    deferring = (tmp_path / rel).read_text(encoding="utf-8") + "\nThe private repo is pending a PM ruling?\n"
    report = pg.gate_plan(tmp_path, tmp_path / rel, text=deferring)
    assert report["classes"]["RULINGS"]["status"] == "DEFECT"
