"""DONE_CLAUSE prep-gate class: every sizing done-clause must ride a live row's `acceptance:`."""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.roadmap import prep_gate as pg
from coordinator_core.roadmap.tests.test_prep_gate import _CLEAN_SPINE, _write_plan

SIZING = "state/sizings/2026-09-07-fixture.yaml"
CLAUSE_A = "the XL r34 plan fails it"
CLAUSE_B = "a mapped plan passes"
R1_TEXT = (
    "Done-clause lint (lever M2). Test: the XL r34 and L r40 plans both fail it; "
    "a plan whose every done-clause maps passes."
)


def _sizing(root: Path, body: str) -> None:
    path = root / SIZING
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _acceptance_sizing(*checks: str) -> str:
    rows = "".join(f"  - check: '{c}'\n" for c in checks)
    return f"acceptance_rows:\n{rows}"


def _plan(root: Path, extra_rows: str = "", *, sizing: str | None = SIZING) -> Path:
    (root / "coordinator_core").mkdir(parents=True, exist_ok=True)
    cited = f"sizing_object: {sizing}\n" if sizing else "sizing_object: null\n"
    fm = (
        "census: []\ncapabilities: []\n"
        "prime_exit_criterion:\n  statement: it lands\n  derived_from: " + SIZING + "\n" + cited
    )
    return _write_plan(root, frontmatter=fm, spine=_CLEAN_SPINE + extra_rows)


def _row(row_id: str, acceptance: str, extra: str = "") -> str:
    return (
        f"- id: {row_id}\n  title: t\n  body: b\n  change_kind: code-edit\n"
        "  surface: coordinator_core/roadmap/prep_gate.py\n"
        "  writes: [coordinator_core/roadmap/prep_gate.py]\n"
        f"  queue_scope: project\n  disposition: open\n  acceptance: {acceptance!r}\n{extra}"
    )


def _cls(root: Path, plan: Path) -> dict:
    return pg.gate_plan(root, plan)["classes"]["DONE_CLAUSE"]


def test_class_is_registered_after_register() -> None:
    assert pg.CLASS_ORDER[-1] == "DONE_CLAUSE"
    assert pg.CLASS_ORDER[-2] == "REGISTER"


def test_unmapped_plan_names_every_unmapped_clause(tmp_path: Path) -> None:
    _sizing(tmp_path, _acceptance_sizing(CLAUSE_A, CLAUSE_B))
    result = _cls(tmp_path, _plan(tmp_path))
    assert result["status"] == "DEFECT"
    assert result["kind"] == "done-clause-unmapped"
    assert f'"{CLAUSE_A}"' in result["detail"] and f'"{CLAUSE_B}"' in result["detail"]


def test_fully_mapped_plan_passes(tmp_path: Path) -> None:
    _sizing(tmp_path, _acceptance_sizing(CLAUSE_A, CLAUSE_B))
    rows = _row("C2", f"echo {CLAUSE_A}") + _row("C3", f"echo {CLAUSE_B}")
    assert _cls(tmp_path, _plan(tmp_path, rows))["status"] == "PASS"


def test_deferred_row_acceptance_does_not_count(tmp_path: Path) -> None:
    _sizing(tmp_path, _acceptance_sizing(CLAUSE_A))
    rows = _row("C2", f"echo {CLAUSE_A}", "  deferred: true\n")
    assert _cls(tmp_path, _plan(tmp_path, rows))["status"] == "DEFECT"
    held = _row("C2", f"echo {CLAUSE_A}", "  deferred_until: 2099-01-01\n")
    assert _cls(tmp_path, _plan(tmp_path, held))["status"] == "DEFECT"


def test_coded_row_acceptance_still_counts(tmp_path: Path) -> None:
    _sizing(tmp_path, _acceptance_sizing(CLAUSE_A))
    rows = _row("C2", f"echo {CLAUSE_A}").replace("disposition: open", "disposition: coded")
    assert _cls(tmp_path, _plan(tmp_path, rows))["status"] == "PASS"


def test_sizing_without_clause_source_is_not_applicable(tmp_path: Path) -> None:
    _sizing(tmp_path, "title: nothing\n")
    result = _cls(tmp_path, _plan(tmp_path))
    assert result["status"] == "PASS" and result["detail"].startswith("not applicable")


def test_null_sizing_object_passes(tmp_path: Path) -> None:
    result = _cls(tmp_path, _plan(tmp_path, sizing=None))
    assert result["status"] == "PASS" and result["detail"].startswith("not applicable")


def test_done_clauses_of_r1_source_text() -> None:
    doc = {"requirement_register": {"rows": [{"id": "R1", "status": "open", "source_text": R1_TEXT}]}}
    assert pg._done_clauses(doc) == [
        "the XL r34 and L r40 plans both fail it",
        "a plan whose every done-clause maps passes",
    ]


def test_done_clauses_skip_met_rows_and_dedupe() -> None:
    doc = {
        "acceptance_rows": [{"check": "X"}],
        "requirement_register": {
            "rows": [
                {"id": "R1", "status": "met", "source_text": "Test: gone."},
                {"id": "R2", "status": "partial", "source_text": "Test: X; Y."},
            ]
        },
    }
    assert pg._done_clauses(doc) == ["X", "Y"]


def test_slug_form_in_acceptance_maps_register_clause(tmp_path: Path) -> None:
    _sizing(
        tmp_path,
        "requirement_register:\n  rows:\n    - id: R1\n      status: open\n"
        f"      source_text: '{R1_TEXT}'\n",
    )
    cmd = "pytest -k the_xl_r34_and_l_r40_plans_both_fail_it"
    assert _cls(tmp_path, _plan(tmp_path, _row("C2", cmd)))["status"] == "DEFECT"
    both = cmd + " or a_plan_whose_every_done_clause_maps_passes"
    assert _cls(tmp_path, _plan(tmp_path, _row("C2", both)))["status"] == "PASS"


def test_unmapped_clause_is_mechanical_with_repair_naming_clause(tmp_path: Path) -> None:
    _sizing(tmp_path, _acceptance_sizing(CLAUSE_A))
    cls = _cls(tmp_path, _plan(tmp_path))
    assert cls["kind"] == "done-clause-unmapped"
    assert cls["mechanical"] is True
    assert "acceptance" in cls["repair"] and CLAUSE_A in cls["repair"]


def test_mapped_clause_carries_no_repair(tmp_path: Path) -> None:
    _sizing(tmp_path, _acceptance_sizing(CLAUSE_A))
    cls = _cls(tmp_path, _plan(tmp_path, _row("C2", f"pytest -k '{CLAUSE_A}'")))
    assert cls["status"] == "PASS" and cls["mechanical"] is False


def test_predicate_spawns_no_subprocess(tmp_path: Path, monkeypatch) -> None:
    _sizing(tmp_path, _acceptance_sizing(CLAUSE_A))
    plan = _plan(tmp_path)

    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    fm = pg.plan_frontmatter(plan.read_text(encoding="utf-8"))
    assert pg._done_clause(fm, tmp_path, pg.raw_spine_rows(plan.read_text(encoding="utf-8")))["status"] == "DEFECT"


def test_sizing_read_is_shared_through_the_cache(tmp_path: Path) -> None:
    _sizing(tmp_path, _acceptance_sizing(CLAUSE_A))
    cache: dict = {}
    first = pg._read_sizing_register(tmp_path, SIZING, cache)
    (tmp_path / SIZING).unlink()
    assert pg._read_sizing_register(tmp_path, SIZING, cache) == first
