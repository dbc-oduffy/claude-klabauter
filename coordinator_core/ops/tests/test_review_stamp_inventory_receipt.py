"""coordinator_core.ops.review_stamp.mint_receipt — the review receipt on a mise inventory record."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ops import review_stamp as m

SHA = "a" * 40
TREE = "b" * 40
BASE = "c" * 40


def _record(*, verdict: str = "PASS", unresolved=None) -> dict:
    return {
        "plan_id": "pln-example-abc123",
        "unresolved": unresolved or [],
        "confinement_violations": 0,
        "fixes_applied": 2,
        "slices": 3,
        "brief_conformance": {"items": 4, "met": 4, "unmet": 0},
        "prep": {"run_base_sha": BASE, "product_files": ["x.py"], "foreign_claims": [], "slice_files": ["x.py"]},
        "delivery": {"verdict": verdict, "product_files": 1, "claims_unbacked": 0},
        "tests": {"status": "pass", "run": 7, "failed": 0, "sidecar": "t.md"},
        "criterion": {"status": "met", "observation": "ok", "sidecar": None},
    }


def _repo(tmp_path: Path, *, run_id: bool = True) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    (repo / "state" / "mise-inventory").mkdir(parents=True)
    (repo / "docs" / "plans").mkdir(parents=True)
    inv = repo / "state" / "mise-inventory" / "run-1.md"
    inv.write_text(
        "---\n" + ("run_id: run-1\n" if run_id else "title: no run id\n") + "---\n\nbody\n", encoding="utf-8"
    )
    plan = repo / "docs" / "plans" / "p.md"
    plan.write_text("---\ntitle: P\nplan_id: pln-example-abc123\n---\n\nbody\n", encoding="utf-8")
    sidecar = repo / ".coordinator-local" / "subagent-share" / "s" / "integration.md"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("---\nplan_id: x\n---\n", encoding="utf-8")
    return repo, inv, sidecar


def _stamp_of(path: Path):
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    return (yaml.safe_load(split.fm_text) or {}).get("review_stamp")


def test_pass_run_writes_the_receipt_onto_the_inventory_record(tmp_path):
    repo, inv, sidecar = _repo(tmp_path)
    stamp = m.mint_receipt(inv, repo, resolved=(SHA, sidecar, _record()), terminal_tree_sha=TREE)
    written = _stamp_of(inv)
    assert written == stamp
    assert written["terminal_commit_sha"] == SHA
    assert written["terminal_tree_sha"] == TREE
    assert written["run_base_sha"] == BASE
    assert written["integration_sidecar"] == ".coordinator-local/subagent-share/s/integration.md"
    assert "criterion" not in written
    assert "run_id: run-1" in inv.read_text(encoding="utf-8")


def test_a_record_without_run_id_is_refused_and_unchanged(tmp_path):
    repo, inv, sidecar = _repo(tmp_path, run_id=False)
    before = inv.read_text(encoding="utf-8")
    with pytest.raises(m.MintRefusal, match="run_id"):
        m.mint_receipt(inv, repo, resolved=(SHA, sidecar, _record()), terminal_tree_sha=TREE)
    assert inv.read_text(encoding="utf-8") == before


def test_a_call_without_resolved_is_refused_and_writes_nothing(tmp_path):
    repo, inv, _ = _repo(tmp_path)
    before = inv.read_text(encoding="utf-8")
    with pytest.raises(m.MintRefusal, match="resolved"):
        m.mint_receipt(inv, repo)
    assert inv.read_text(encoding="utf-8") == before


def test_a_non_pass_delivery_is_refused_and_writes_nothing(tmp_path):
    repo, inv, sidecar = _repo(tmp_path)
    before = inv.read_text(encoding="utf-8")
    with pytest.raises(m.MintRefusal, match="delivery verdict"):
        m.mint_receipt(inv, repo, resolved=(SHA, sidecar, _record(verdict="FAIL")), terminal_tree_sha=TREE)
    assert inv.read_text(encoding="utf-8") == before


def test_an_unresolved_finding_is_refused_and_writes_nothing(tmp_path):
    repo, inv, sidecar = _repo(tmp_path)
    before = inv.read_text(encoding="utf-8")
    with pytest.raises(m.MintRefusal, match="unresolved"):
        m.mint_receipt(
            inv, repo, resolved=(SHA, sidecar, _record(unresolved=[{"line": "x"}])), terminal_tree_sha=TREE
        )
    assert inv.read_text(encoding="utf-8") == before


def test_the_receipt_shares_every_key_with_the_plan_stamp_mint_writes(tmp_path):
    repo, inv, sidecar = _repo(tmp_path)
    plan = repo / "docs" / "plans" / "p.md"
    resolved = (SHA, sidecar, _record())
    plan_stamp = m.mint(plan, repo, build_test_path=None, resolved=resolved, terminal_tree_sha=TREE)
    receipt = m.mint_receipt(inv, repo, resolved=resolved, terminal_tree_sha=TREE)
    drop = {"stamped_at", "criterion"}
    assert {k: v for k, v in receipt.items() if k not in drop} == {
        k: v for k, v in plan_stamp.items() if k not in drop
    }
    assert list(receipt) == [k for k in plan_stamp if k != "criterion"]
