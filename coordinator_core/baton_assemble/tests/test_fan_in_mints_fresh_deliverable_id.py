"""coordinator_core.baton_assemble.tests.test_fan_in_mints_fresh_deliverable_id

DR-388 (2026-08-30), authorized by
`state/sizings/2026-08-30-multi-baton-pickup-mints-a-successor-bat.yaml`
`pm_resolution.deliverable_id_construction` -- PM verbatim: "This becomes a
new deliverableID by construction."

Scoped narrowly to N>1 (a genuine fan-in, `additional_predecessors` present):
the successor mints a FRESH `deliverable_id`, never one carried or reused
from the primary predecessor, a claimed plan, or any fan-in leg -- a
deliberate, narrow departure from DR-207 DD#1's carry-verbatim rule. The
single-predecessor path is unchanged (covered by
`test_deliverable_ids_union_carry.py::test_single_predecessor_leaves_both_
keys_none` and the wider `test_j_divergent_deliverable_id.py` suite).

Run: python3 -m pytest
coordinator_core/baton_assemble/tests/test_fan_in_mints_fresh_deliverable_id.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.baton_assemble as ba
import coordinator_core.baton_assemble.apply as ba_apply
from coordinator_core.test_baton_assemble import (
    _FAKE_OPERATOR_CONFIG,
    _REPO_CLAUDE_KLABAUTER_BIN,
    _write_artifact,
)

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    """Restated per-module (autouse fixtures do not cross module boundaries)
    -- mirrors the sibling `test_deliverable_ids_union_carry.py` fixture."""
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))
    monkeypatch.setattr(ba_apply, "_resolve_claude_klabauter_bin", lambda: _REPO_CLAUDE_KLABAUTER_BIN)


def _write_predecessor(
    root: Path, rel: str, deliverable_id: str, plan_id: str, handoff_id: str | None = None
) -> Path:
    lines = [
        f"deliverable_id: {deliverable_id}",
        f"origin_plan_id: {plan_id}",
    ]
    if handoff_id:
        lines.append(f"handoff_id: {handoff_id}")
    return _write_artifact(root / rel, lines)


def test_fan_in_successor_mints_fresh_id_never_carrying_any_rung(tmp_path):
    primary = _write_predecessor(
        tmp_path,
        "state/handoffs/primary.md",
        "DEL-PRIMARY",
        "pln-primary-aaa111",
        handoff_id="hnd-primary-aaa111",
    )
    extra_a = _write_predecessor(
        tmp_path, "state/handoffs/extra-a.md", "DEL-EXTRA-A", "pln-extra-a-bbb222"
    )
    extra_b = _write_predecessor(
        tmp_path, "state/handoffs/extra-b.md", "DEL-EXTRA-B", "pln-extra-b-ccc333"
    )

    lineage = ba.resolve_lineage(
        "handoff",
        str(primary),
        tmp_path,
        additional_predecessor_paths=[str(extra_a), str(extra_b)],
    )

    assert lineage["deliverable_id"] not in {"DEL-PRIMARY", "DEL-EXTRA-A", "DEL-EXTRA-B"}, (
        "DR-388: the successor's own deliverable_id must be freshly minted, "
        "never carried verbatim from any rung"
    )
    assert lineage["discovery"] == "fan-in-mint"
    # The plural union still names every rung's OWN real id -- the fresh
    # mint is the successor's identity, not a fourth entry in this list.
    assert lineage["deliverable_ids"] == ["DEL-PRIMARY", "DEL-EXTRA-A", "DEL-EXTRA-B"]

    directives = ba._build_directives("handoff", lineage, root=tmp_path)
    d1 = next(d for d in directives if d["id"] == "d1")
    assert f"--deliverable-id={lineage['deliverable_id']}" in d1["args"]
    assert "--deliverable-id=DEL-PRIMARY" not in d1["args"]


def test_single_predecessor_still_carries_verbatim_dr207(tmp_path):
    """Negative control -- DR-388 is scoped to N>1 only; the ordinary
    single-predecessor cascade is byte-identical to before this change."""
    primary = _write_predecessor(
        tmp_path,
        "state/handoffs/primary.md",
        "DEL-LONE",
        "pln-lone-aaa111",
        handoff_id="hnd-primary-aaa111",
    )

    lineage = ba.resolve_lineage("handoff", str(primary), tmp_path)

    assert lineage["deliverable_id"] == "DEL-LONE"
    assert lineage["discovery"] != "fan-in-mint"


def test_fan_in_mint_is_reproducibly_unique_across_two_resolutions(tmp_path):
    """The fresh mint is genuinely fresh, not a deterministic function of
    the inputs -- two independent resolutions over the identical fan-in
    input must not collide (mint_deliverable_id.mint's own random-suffix
    contract, exercised through resolve_lineage rather than re-asserted
    against the minting helper directly)."""
    primary = _write_predecessor(
        tmp_path,
        "state/handoffs/primary.md",
        "DEL-PRIMARY",
        "pln-primary-aaa111",
        handoff_id="hnd-primary-aaa111",
    )
    extra = _write_predecessor(
        tmp_path, "state/handoffs/extra.md", "DEL-EXTRA", "pln-extra-bbb222"
    )

    first = ba.resolve_lineage(
        "handoff", str(primary), tmp_path, additional_predecessor_paths=[str(extra)]
    )
    second = ba.resolve_lineage(
        "handoff", str(primary), tmp_path, additional_predecessor_paths=[str(extra)]
    )

    assert first["deliverable_id"] != second["deliverable_id"]
