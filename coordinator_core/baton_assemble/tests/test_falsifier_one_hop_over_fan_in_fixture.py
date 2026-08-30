"""coordinator_core.baton_assemble.tests.test_falsifier_one_hop_over_fan_in_fixture

The ONE test the overengineering review (finding 4) asked for in place of a
`trace.py` module: mint a three-prior fan-in fixture successor via
`baton_assemble.resolve_lineage`'s own `plan_ids` union, write it in the
exact shape `coordinator-doc-new` would scaffold, and invoke
`docs/research/spike-verdicts/one-hop-plan-completeness-falsifier.py`
against it -- proving the falsifier (not a second implementation of its
logic) is the deliverable for "one hop answers plan completeness".

Fixture-only, never a live baton (`state/handoffs/`) or a live plan
(`docs/plans/`) -- the falsifier's own `REPO_ROOT`-anchored `docs/plans/*.md`
glob is monkeypatched to a tmp fixture root for the duration of this test so
no real corpus file is read or written.

Run: python3 -m pytest
coordinator_core/baton_assemble/tests/test_falsifier_one_hop_over_fan_in_fixture.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

import coordinator_core.baton_assemble as ba
from coordinator_core.test_baton_assemble import _write_artifact

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FALSIFIER_PATH = (
    _REPO_ROOT / "docs" / "research" / "spike-verdicts" / "one-hop-plan-completeness-falsifier.py"
)


def _load_falsifier_module():
    spec = importlib.util.spec_from_file_location("_one_hop_falsifier_under_test", _FALSIFIER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_plan(root: Path, rel: str, plan_id: str, status: str = "complete") -> Path:
    return _write_artifact(root / rel, [f"plan_id: {plan_id}", f"status: {status}"])


def _write_predecessor(
    root: Path, rel: str, plan_id: str, *, handoff_id: str | None = None
) -> Path:
    lines = [f"origin_plan_id: {plan_id}"]
    if handoff_id:
        lines.append(f"handoff_id: {handoff_id}")
    return _write_artifact(root / rel, lines)


def test_one_hop_answers_completion_for_a_three_prior_fan_in_successor(tmp_path, monkeypatch):
    module = _load_falsifier_module()
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)

    primary = _write_predecessor(
        tmp_path, "state/handoffs/primary.md", "pln-one-aaa111", handoff_id="hnd-primary-aaa111"
    )
    extra_a = _write_predecessor(tmp_path, "state/handoffs/extra-a.md", "pln-two-bbb222")
    extra_b = _write_predecessor(tmp_path, "state/handoffs/extra-b.md", "pln-three-ccc333")

    _write_plan(tmp_path, "docs/plans/plan-one.md", "pln-one-aaa111")
    _write_plan(tmp_path, "docs/plans/plan-two.md", "pln-two-bbb222")
    _write_plan(tmp_path, "docs/plans/plan-three.md", "pln-three-ccc333")

    lineage = ba.resolve_lineage(
        "handoff",
        str(primary),
        tmp_path,
        additional_predecessor_paths=[str(extra_a), str(extra_b)],
    )
    assert lineage["plan_ids"] == ["pln-one-aaa111", "pln-two-bbb222", "pln-three-ccc333"]

    successor = _write_artifact(
        tmp_path / "state/handoffs/successor.md",
        [f"deliverable_id: {lineage['deliverable_id']}"]
        + ["plan_ids:"]
        + [f"  - {pid}" for pid in lineage["plan_ids"]],
    )

    argv_backup = sys.argv
    try:
        sys.argv = [
            "one-hop-plan-completeness-falsifier.py",
            str(successor),
            str(primary),
            str(extra_a),
            str(extra_b),
        ]
        exit_code = module.main()
    finally:
        sys.argv = argv_backup

    assert exit_code == 0, (
        "the successor's own plan_ids union must be enough for the falsifier "
        "to answer completion for every prior's plan, in one hop, without "
        "opening any prior handoff"
    )


def test_one_hop_falsifies_when_plan_ids_is_missing(tmp_path, monkeypatch):
    """Negative control -- a successor minted WITHOUT the plan_ids union
    (the pre-C2 shape) must FAIL the falsifier, so a green result upstream
    is never mistaken for 'the criterion doesn't discriminate'."""
    module = _load_falsifier_module()
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)

    primary = _write_predecessor(tmp_path, "state/handoffs/primary.md", "pln-one-aaa111")
    extra = _write_predecessor(tmp_path, "state/handoffs/extra.md", "pln-two-bbb222")
    _write_plan(tmp_path, "docs/plans/plan-one.md", "pln-one-aaa111")
    _write_plan(tmp_path, "docs/plans/plan-two.md", "pln-two-bbb222")

    successor = _write_artifact(
        tmp_path / "state/handoffs/successor-bare.md", ["deliverable_id: DEL-BARE"]
    )

    argv_backup = sys.argv
    try:
        sys.argv = [
            "one-hop-plan-completeness-falsifier.py",
            str(successor),
            str(primary),
            str(extra),
        ]
        exit_code = module.main()
    finally:
        sys.argv = argv_backup

    assert exit_code == 1
