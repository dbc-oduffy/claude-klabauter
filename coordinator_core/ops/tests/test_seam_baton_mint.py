"""seam.mint_batons: cap and rollup, cross-call dedup, dry_run, schema validity, staged marking.

The branch is faked at the module's ``head_branch`` seam; the scaffolder is the real one.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import yaml

from coordinator_core.baton_assemble.apply import _load_doc_new_module
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.frontmatter.schema_validate import _SCHEMAS_DIR, validate_frontmatter
from coordinator_core.ops import seam_baton_mint as op


@pytest.fixture(autouse=True)
def _seams(monkeypatch):
    monkeypatch.setattr(op, "head_branch", lambda root: "work/test")
    monkeypatch.setattr(op, "main_worktree_root", lambda root: root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    doc_new = _load_doc_new_module()
    monkeypatch.setattr(doc_new, "_resolve_spinoff_origin", lambda: doc_new.SpinoffOrigin(None, None, None))


def _flags(n: int, start: int = 0) -> list:
    return [{"key": f"capabilities-undeclared|docs/plans/p{i}.md|", "plan": f"docs/plans/p{i}.md",
             "class": "capabilities-undeclared", "path": f"src/f{i}.py", "detail": f"gap {i}"}
            for i in range(start, start + n)]


def _mint(root: Path, flags: list, dry_run: bool = False, run_id: str = "run-1") -> dict:
    return op._mint_handler({"flags": flags, "run_id": run_id, "dry_run": dry_run}, repo_root=root)


def _fm(root: Path, rel: str) -> dict:
    return yaml.safe_load(split_frontmatter((root / rel).read_text(encoding="utf-8")).fm_text)


def test_cap_of_five_then_one_rollup(tmp_path):
    r = _mint(tmp_path, _flags(8))
    assert len(r["minted"]) == 5 and r["rollup"] and r["deduped"] == []
    keys = _fm(tmp_path, r["rollup"])["seam_keys"]
    assert keys == [f["key"] for f in _flags(3, 5)]
    assert len(list((tmp_path / "state" / "handoffs").glob("*.md"))) == 6


def test_no_rollup_at_or_under_the_cap(tmp_path):
    r = _mint(tmp_path, _flags(5))
    assert len(r["minted"]) == 5 and r["rollup"] is None


def test_dedup_across_two_calls_including_rolled_up_keys(tmp_path):
    first = _mint(tmp_path, _flags(7))
    second = _mint(tmp_path, _flags(9))
    assert second["deduped"] == [f["key"] for f in _flags(7)]
    assert len(second["minted"]) == 2 and second["rollup"] is None
    assert len(list((tmp_path / "state" / "handoffs").glob("*.md"))) == len(first["minted"]) + 1 + 2


def test_a_terminal_or_archived_baton_lets_its_key_mint_again(tmp_path):
    flag = _flags(1)
    rel = _mint(tmp_path, flag)["minted"][0]
    path = tmp_path / rel
    path.write_text(path.read_text(encoding="utf-8").replace("deployment_state: awaiting_gate", "deployment_state: closed"))
    again = _mint(tmp_path, flag)
    assert again["deduped"] == [] and len(again["minted"]) == 1
    (tmp_path / again["minted"][0]).unlink()
    assert len(_mint(tmp_path, flag)["minted"]) == 1


def test_repeated_keys_in_one_call_mint_once(tmp_path):
    r = _mint(tmp_path, _flags(1) + _flags(1))
    assert len(r["minted"]) == 1


def test_dry_run_writes_nothing_and_reports_the_same_shape(tmp_path):
    dry = _mint(tmp_path, _flags(7), dry_run=True)
    assert dry["dry_run"] is True and len(dry["minted"]) == 5 and dry["rollup"]
    assert not (tmp_path / "state").exists()
    wet = _mint(tmp_path, _flags(7))
    assert (wet["minted"], wet["rollup"]) == (dry["minted"], dry["rollup"])


def test_minted_batons_validate_against_the_vendored_handoff_schema(tmp_path):
    r = _mint(tmp_path, _flags(6))
    for rel in [*r["minted"], r["rollup"]]:
        assert validate_frontmatter(_fm(tmp_path, rel), _SCHEMAS_DIR / "handoff.schema.json") == []


def test_staged_marking_and_provenance(tmp_path):
    r = _mint(tmp_path, _flags(1))
    fm = _fm(tmp_path, r["minted"][0])
    assert fm["deployment_state"] == "awaiting_gate" and fm["pickup_ready"] is False
    assert fm["status"] == "open" and fm["plan_blitz_hold_reason"] and fm["gate_notes"]
    assert fm["seam_keys"] == [_flags(1)[0]["key"]] and fm["seam_run_id"] == "run-1"
    assert fm["producer"]["op_identity"] == "machine-minted"
    assert fm["origin_session"] == "11111111-2222-3333-4444-555555555555" and fm["origin_handoff"] is None
    index = json.loads((tmp_path / op.INDEX_REL).read_text(encoding="utf-8"))
    assert index == {_flags(1)[0]["key"]: r["minted"][0]}


@pytest.mark.parametrize("bad", [
    {"flags": "x", "run_id": "r"},
    {"flags": [{"key": "k", "plan": "p"}], "run_id": "r"},
    {"flags": [], "run_id": ""},
    {"flags": [], "run_id": "r", "dry_run": "yes"},
])
def test_malformed_params_are_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        op._mint_handler(bad, repo_root=tmp_path)


def test_empty_flags_mint_nothing(tmp_path):
    assert _mint(tmp_path, []) == {"minted": [], "rollup": None, "deduped": [], "dry_run": False}


def test_process_time_of_a_full_call_stays_under_the_bar(tmp_path):
    _mint(tmp_path, _flags(1), dry_run=True)
    start = time.process_time()
    _mint(tmp_path, _flags(8, 100))
    assert time.process_time() - start < 0.5
