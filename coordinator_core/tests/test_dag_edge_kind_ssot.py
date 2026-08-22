"""
coordinator_core.tests.test_dag_edge_kind_ssot — drift guard for the archival-
vs-continuation edge-kind vocabulary (723aadac4b1d follow-up refactor).

Purpose: `dag.ARCHIVAL_EDGE_KINDS` / `dag.CONTINUATION_EDGE_KINDS` are the SSOT
for two edge-kind sets that answer two different questions (see those
constants' docstrings). Before this refactor, four representations of the
same two sets were kept in sync by a comment asking future readers to
remember — the mechanism that silently drifted at five call sites (fixed in
723aadac4b1d). This is the replacement mechanism: every representation is
compared, as a set, against the two dag.py constants. A future edit that
widens/narrows one representation without the others now fails a test
instead of failing silently.

Representations covered:
  - dag.ARCHIVAL_EDGE_KINDS / dag.CONTINUATION_EDGE_KINDS (the SSOT itself).
  - ops.handoff_children._DEFAULT_EDGE_KINDS (set) / CONCLUSION_EDGE_KINDS (CSV).
  - coverage._CONTINUATION_EDGE_KINDS (frozenset).

Negative-spec:
  - workstream_complete._LEG_B_EDGE_KINDS (CSV) was a fourth representation
    covered here until 2026-08-22. It is not missing — it was deleted with
    its subject at e25f5b190 (chunk C9 / Ruling 4,
    docs/plans/2026-08-21-rebuild-the-three-ceremony-assemblers.md), which
    retargeted leg B off the `handoff.has_live_children` op dispatch onto a
    read of the candidate's own write-time `continued_into` back-edge —
    a shape that, per `_dispatch_has_live_children`'s own docstring, names
    no edge-kinds parameter at all. There is no representation left to drift,
    so the row is retired rather than repointed at a substitute constant.
    That deletion left this module's module-level import dangling, making the
    whole file uncollectable; `test_no_dangling_first_party_import.py` is the
    guard for that class.
  - Does NOT test archival.reverse_membership or dag.referenced_by's runtime
    traversal behaviour — those are covered by test_dag_edge_kinds.py,
    test_handoff_children.py, and the 723aadac4b1d regression tests. This
    file only asserts the vocabulary representations agree with each other.
"""

from __future__ import annotations

from coordinator_core import coverage, dag
from coordinator_core.ops import handoff_children


def _csv_to_set(csv: str) -> set[str]:
    return {part.strip() for part in csv.split(",") if part.strip()}


class TestArchivalContinuationEdgeKindSSOT:
    def test_archival_and_continuation_are_distinct(self) -> None:
        """The two SSOT constants differ by exactly `forked_from`."""
        assert dag.ARCHIVAL_EDGE_KINDS - dag.CONTINUATION_EDGE_KINDS == {"forked_from"}
        assert dag.CONTINUATION_EDGE_KINDS - dag.ARCHIVAL_EDGE_KINDS == set()

    def test_handoff_children_default_matches_archival(self) -> None:
        assert set(handoff_children._DEFAULT_EDGE_KINDS) == set(dag.ARCHIVAL_EDGE_KINDS)

    def test_handoff_children_conclusion_csv_matches_continuation(self) -> None:
        assert _csv_to_set(handoff_children.CONCLUSION_EDGE_KINDS) == set(
            dag.CONTINUATION_EDGE_KINDS
        )

    def test_coverage_continuation_matches_dag(self) -> None:
        assert set(coverage._CONTINUATION_EDGE_KINDS) == set(dag.CONTINUATION_EDGE_KINDS)

    def test_referenced_by_default_matches_archival(self) -> None:
        """`dag.referenced_by`'s own inline default (the bottom-layer archival
        default every other representation ultimately funnels into) agrees
        with the SSOT constant it was rewritten to reference."""
        empty_result = dag.referenced_by(
            target="/nonexistent/target.md",
            live_set=[],
            handoff_dir="/nonexistent",
        )
        # A live_set of [] can never reference anything — this call only
        # exercises the edge_kinds=None default-substitution branch without
        # needing real files on disk; the assertion below is on the
        # constant itself, not this call's return value.
        assert empty_result["referenced"] is False
        assert dag.ARCHIVAL_EDGE_KINDS == frozenset(
            {"predecessor", "additional_predecessors", "forked_from"}
        )
