"""`plan-spine-check` at default arguments fails where `plan.prep_gate` halts on SPINE, REGISTER
and anchor-premise defects, and passes the control plan that prep_gate passes.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MOD = Path(__file__).resolve().parents[1] / "plan-spine-check.py"


def _load():
    spec = importlib.util.spec_from_file_location("plan_spine_check_prep_gate_parity", _MOD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plan_spine_check_prep_gate_parity"] = mod
    spec.loader.exec_module(mod)
    return mod


psc = _load()

_ROW = (
    '- id: C1\n  title: "do the thing"\n  change_kind: doc-edit\n  surface: "docs/a.md"\n'
    '  writes: ["docs/a.md"]\n  disposition: open\n  body: "Edit docs/a.md."\n'
)


def _plan(tmp_path: Path, *, anchors: bool, rows: str = _ROW) -> Path:
    (tmp_path / ".git").mkdir(exist_ok=True)
    fm = "title: t\nstatus: draft\n" + ("anchor_premises: []\n" if anchors else "")
    path = tmp_path / "plan.md"
    path.write_text(
        f"---\n{fm}---\n\n# A plan\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n",
        encoding="utf-8",
    )
    return path


def _prep_gate_classes(path: Path) -> dict:
    psc._ensure_engine_on_path()
    from coordinator_core.roadmap.prep_gate import gate_plan

    return gate_plan(path.parent, path, text=path.read_text(encoding="utf-8"))["classes"]


def test_missing_anchor_premises_fails_naming_the_defect(tmp_path, capsys):
    path = _plan(tmp_path, anchors=False)
    assert psc.main([str(path)]) == psc.EXIT_INVALID
    assert "anchor-premises-undeclared" in capsys.readouterr().out


def test_declared_empty_anchor_premises_is_not_an_anchor_defect(tmp_path):
    report = psc.check_plan(_plan(tmp_path, anchors=True))
    assert not [f for f in report["rows"] if f["at"] == "anchor_premises"]


def test_spine_defect_prep_gate_halts_fails_here(tmp_path, capsys):
    rows = _ROW + _ROW  # duplicate id C1
    path = _plan(tmp_path, anchors=True, rows=rows)
    classes = _prep_gate_classes(path)
    assert classes["SPINE"]["status"] in ("DEFECT", "REFUSE")
    assert psc.main([str(path)]) == psc.EXIT_INVALID
    assert "SPINE" in capsys.readouterr().out


def test_register_defect_prep_gate_halts_fails_here(tmp_path, capsys):
    path = _plan(tmp_path, anchors=True)
    text = path.read_text(encoding="utf-8").replace(
        "title: t\n",
        "title: t\nregister_claims:\n  sizing: state/sizings/absent.yaml\n  rows: [R1]\n",
        1,
    )
    path.write_text(text, encoding="utf-8")
    classes = _prep_gate_classes(path)
    assert classes["REGISTER"]["status"] in ("DEFECT", "REFUSE")
    assert psc.main([str(path)]) == psc.EXIT_INVALID
    assert "REGISTER" in capsys.readouterr().out


def test_control_plan_prep_gate_passes_spine_and_register_passes_here(tmp_path):
    path = _plan(tmp_path, anchors=True)
    classes = _prep_gate_classes(path)
    assert classes["SPINE"]["status"] == "PASS"
    assert classes["REGISTER"]["status"] == "PASS"
    report = psc.check_plan(path)
    assert not [f for f in report["rows"] if f["at"] in ("SPINE", "REGISTER", "anchor_premises")]
