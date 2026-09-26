"""P143-T35: `_build_directives`'s `hard_block_ids` set, post-retirement.

Two dead/retired ids must never reappear in `hard_block_ids`:
  - `d_step4c_ubt_pending_merge_gate` — never constructed by any
    directive-builder call in this module (dead: it named a gate that was
    never actually emitted), removed here along with its stale docstring
    paragraph.
  - `d_step4b_4k_schema_drift` — the PM ruled vendored-schema drift
    advisory-only; it is not a directive this module emits at all (see
    `test_workweek_complete_contract.py`'s own assertion to that effect),
    so it must never be reintroduced as a hard-blocking id either.

Spec backlink: docs/plans/2026-09-22-inbox-blitz-bundled-xs-s-fixes-2026-09-11.md (P143-T35).
"""
from __future__ import annotations

from coordinator_core.workweek_complete.brief import _build_directives


def _hard_block_ids(directives: list[dict]) -> set[str]:
    return {d["id"] for d in directives if d.get("hard_block")}


class TestHardBlockIds:
    def test_dead_ubt_id_is_not_hard_blocking(self) -> None:
        directives = _build_directives()
        assert "d_step4c_ubt_pending_merge_gate" not in _hard_block_ids(directives)

    def test_dead_ubt_id_is_not_constructed_at_all(self) -> None:
        directives = _build_directives()
        assert "d_step4c_ubt_pending_merge_gate" not in {d["id"] for d in directives}

    def test_schema_drift_id_stays_out_of_hard_block_ids(self) -> None:
        directives = _build_directives()
        assert "d_step4b_4k_schema_drift" not in _hard_block_ids(directives)
        assert "d_step4b_4k_schema_drift" not in {d["id"] for d in directives}

    def test_remaining_hard_block_ids_are_the_two_the_census_named(self) -> None:
        directives = _build_directives()
        assert _hard_block_ids(directives) == {
            "d_step4b_4k_reverse_drift",
            "d_step4b_4k_version_consistency",
        }
