"""review-stamp register: writes review_stamp.register from the plan's register tally."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.frontmatter.schema_validate import validate_frontmatter
from coordinator_core.ops import review_stamp as m

SIZING_REL = "state/sizings/s.yaml"
SCHEMA = Path(__file__).resolve().parents[2] / "frontmatter" / "schemas" / "plan.schema.json"


def _row(rid, status="met", wired=True):
    return {
        "id": rid, "source_text": f"text {rid}", "anchor": "§1", "surface": "internal",
        "status": status, "claimed_by": [], "wired": wired, "met_by": [],
        "last_progress_at": None, "ruling": None,
    }


def _setup(tmp_path: Path, rows, *, stamp=True) -> Path:
    (tmp_path / "state/sizings").mkdir(parents=True)
    (tmp_path / SIZING_REL).write_text(
        "schema: sizing-object\n" + yaml.safe_dump(
            {"requirement_register": {"sources": [{"kind": "pm"}], "rows": rows}}, sort_keys=False),
        encoding="utf-8")
    fm = "title: T\nstatus: executing\nregister_claims:\n  sizing: " + SIZING_REL + "\n  rows: [A]\n"
    if stamp:
        fm += ("review_stamp:\n  terminal_commit_sha: abc\n  terminal_tree_sha: def\n"
               "  criterion:\n    status: met\n    observation: o\n    sidecar: null\n"
               "  stamped_at: '2026-10-01T00:00:00Z'\n")
    plan = tmp_path / "p.md"
    plan.write_text(f"---\n{fm}---\n\nBody.\n", encoding="utf-8")
    return plan


def _stamp(plan: Path) -> dict:
    return yaml.safe_load(plan.read_text(encoding="utf-8").split("---\n")[1])["review_stamp"]


def test_writes_block_then_idempotent(tmp_path):
    plan = _setup(tmp_path, [_row("A")])
    assert m.write_register(plan, tmp_path)["status"] == "registered"
    assert _stamp(plan)["register"] == {
        "sizing": SIZING_REL, "unclaimed": 0, "open": 0, "partial": 0, "unwired": 0}
    after = plan.read_bytes()
    assert m.write_register(plan, tmp_path)["status"] == "unchanged"
    assert plan.read_bytes() == after


def test_nonzero_count_refuses_without_write(tmp_path):
    plan = _setup(tmp_path, [_row("A", "open", False)])
    before = plan.read_bytes()
    with pytest.raises(m.MintRefusal, match="open=1"):
        m.write_register(plan, tmp_path)
    assert plan.read_bytes() == before


def test_stampless_plan_refuses(tmp_path):
    plan = _setup(tmp_path, [_row("A")], stamp=False)
    before = plan.read_bytes()
    with pytest.raises(m.MintRefusal, match="no review_stamp"):
        m.write_register(plan, tmp_path)
    assert plan.read_bytes() == before


def test_cli_verb_and_schema_accepts_block(tmp_path):
    plan = _setup(tmp_path, [_row("A")])
    assert m.main(["register", "--plan", str(plan), "--repo-root", str(tmp_path)]) == 0
    fm = yaml.safe_load(plan.read_text(encoding="utf-8").split("---\n")[1])
    errors = [e for e in validate_frontmatter(fm, SCHEMA) if "register" in str(e)]
    assert errors == []
