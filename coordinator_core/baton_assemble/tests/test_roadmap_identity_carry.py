"""Tests for the roadmap-baton succession identity carry (DR-172, C10 Part
2 -- plan docs/plans/2026-08-18-a-session-always-has-a-baton.md).

A `kind: roadmap-baton` predecessor's successor mints AS a roadmap-baton
(not the bare `handoff` doc type), carrying `roadmap_id`/`stub_id` forward
via `coordinator-doc-new`'s existing `--roadmap-id`/`--stub-id` flags.

`blocks` is now carried too, via `coordinator-doc-new`'s `--blocks`
(repeatable) -- the follow-on this module used to defer. Carrying `stub_id`
without it handed the successor the identity the whole down-graph resolves
against and an empty `blocks:` to answer it with, so every dependent's edge
read as severed the moment the successor became the chain head.

STILL not covered here (named, not silently assumed passing):
`blocked_by`, `sprint`, `wave`. `blocked_by` is excluded on purpose -- it is
derived readiness state, so inheriting a predecessor's gates would re-park a
successor on blockers that may have cleared. `sprint`/`wave` are
`bin/roadmap-number-stubs` topo outputs, not lineage. See
`_resolved_predecessor_roadmap_identity`'s own docstring in
`coordinator_core/baton_assemble/__init__.py`.

Spec backlink: coordinator_core/baton_assemble/__init__.py
`_resolved_predecessor_roadmap_identity`, `_build_directives`'s mint-kind
flip in its `d1_args` construction.
"""

from __future__ import annotations

import pytest

import coordinator_core.baton_assemble as ba
from coordinator_core.test_baton_assemble import _FAKE_OPERATOR_CONFIG, _init_repo, _write_artifact

# See test_fan_in_cardinality_judgment_point.py's own module docstring for
# why `_init_repo` (real git) forces this file onto the spawn-tracked tier.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))


def _roadmap_baton_predecessor(
    tmp_path, *, roadmap_id="rm-c10", stub_id="stub-c10", blocks=None
):
    return _write_artifact(
        tmp_path / "state" / "handoffs" / "2026-08-18-roadmap-baton-predecessor.md",
        [
            "deliverable_id: DEL-C10-IDENTITY",
            "handoff_id: hnd-c10-identity-1a2b90",
            "kind: roadmap-baton",
            f"roadmap_id: {roadmap_id}",
            f"stub_id: {stub_id}",
            'predecessor: "none"',
        ]
        + ([f"blocks: [{', '.join(blocks)}]"] if blocks else []),
    )


class TestRoadmapBatonSuccessorMintsAsRoadmapBaton:
    """The kind flip: d1's `--type` is `roadmap-baton`, not `handoff`, when
    the resolved predecessor's own `kind` canonicalizes to `roadmap-baton`."""

    def test_ordinary_predecessor_mints_as_handoff_unchanged(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-08-18-ordinary-predecessor.md",
            ["deliverable_id: DEL-ORDINARY", "handoff_id: hnd-ordinary-1a2b91"],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["cli"] == "coordinator-doc-new")
        assert "--type=handoff" in d1["args"]
        assert not any(a.startswith("--roadmap-id=") for a in d1["args"])
        assert not any(a.startswith("--stub-id=") for a in d1["args"])

    def test_roadmap_baton_predecessor_mints_successor_as_roadmap_baton(self, tmp_path):
        artifact = _roadmap_baton_predecessor(tmp_path)
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["cli"] == "coordinator-doc-new")
        assert "--type=roadmap-baton" in d1["args"], (
            "C10 Part 2: a roadmap-baton predecessor's successor must mint "
            f"as a roadmap-baton -- got args {d1['args']!r}"
        )
        assert "--type=handoff" not in d1["args"]

    def test_spinoff_kind_is_never_flipped(self, tmp_path):
        """The mint-kind flip is scoped to `kind == "handoff"` only -- a
        spinoff's `predecessor: none`-by-design shape (schema_validate.py
        Rule A3a-3) has no predecessor for this carry to read from."""
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-08-18-spinoff-origin.md",
            ["deliverable_id: DEL-SPINOFF-ORIGIN", "handoff_id: hnd-spinoff-origin-1a2b92"],
        )
        decision = ba.brief("spinoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["cli"] == "coordinator-doc-new")
        assert "--type=spinoff" in d1["args"]


class TestRoadmapIdentityFieldsCarried:
    """`roadmap_id`/`stub_id` are forwarded via the existing
    `--roadmap-id`/`--stub-id` flags -- the two fields
    `coordinator-doc-new` already knows how to accept for this doc type."""

    def test_roadmap_id_and_stub_id_are_forwarded(self, tmp_path):
        artifact = _roadmap_baton_predecessor(
            tmp_path, roadmap_id="rm-identity-test", stub_id="stub-identity-test"
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["cli"] == "coordinator-doc-new")
        assert "--roadmap-id=rm-identity-test" in d1["args"]
        assert "--stub-id=stub-identity-test" in d1["args"]

    def test_missing_roadmap_id_or_stub_id_on_predecessor_omits_the_flag(self, tmp_path):
        """Fail-open, matching every sibling `_resolved_predecessor_*`
        reader's own contract: a predecessor missing one of the two fields
        (a schema violation this reader does not itself enforce) omits that
        one flag rather than forwarding an empty string."""
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-08-18-partial-identity.md",
            [
                "deliverable_id: DEL-PARTIAL-IDENTITY",
                "handoff_id: hnd-partial-identity-1a2b93",
                "kind: roadmap-baton",
                "roadmap_id: rm-partial-only",
                'predecessor: "none"',
            ],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["cli"] == "coordinator-doc-new")
        assert "--roadmap-id=rm-partial-only" in d1["args"]
        assert not any(a.startswith("--stub-id=") for a in d1["args"])


class TestBlocksCarriedWithTheStubId:
    """The down-edges travel with the `stub_id` they are authored against.

    Before this, a continuation minted under the predecessor's `stub_id` with
    `blocks: []`; the whole down-graph still resolved on that `stub_id`, so
    `reconcile.gate_eval._has_asymmetry` read every dependent's edge as severed
    and reported a symmetric graph as a data defect. Measured on the live corpus:
    `ceremony-restore-01`, whose four pre-continuation records each named three
    dependents while every continuation named none.
    """

    def test_blocks_are_forwarded_one_flag_per_entry(self, tmp_path):
        artifact = _roadmap_baton_predecessor(
            tmp_path, blocks=["the-meter-02", "archival-sweeps-03"]
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["cli"] == "coordinator-doc-new")
        assert "--blocks=the-meter-02" in d1["args"]
        assert "--blocks=archival-sweeps-03" in d1["args"]

    def test_blocks_travel_with_the_stub_id_never_without_it(self, tmp_path):
        """The coupling, not just the flag: whenever `--stub-id` is emitted for a
        predecessor that authored `blocks`, the matching `--blocks` flags are
        emitted in the SAME directive. Emitting one without the other is the
        defect, so the invariant is asserted rather than the two halves
        independently."""
        artifact = _roadmap_baton_predecessor(tmp_path, blocks=["dep-01"])
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["cli"] == "coordinator-doc-new")
        assert any(a.startswith("--stub-id=") for a in d1["args"])
        assert any(a.startswith("--blocks=") for a in d1["args"])

    def test_predecessor_with_no_blocks_omits_the_flag(self, tmp_path):
        """Fail-open to the scaffold's own `blocks: []`, matching every sibling
        `_resolved_predecessor_*` reader -- never a forwarded empty string."""
        artifact = _roadmap_baton_predecessor(tmp_path)
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["cli"] == "coordinator-doc-new")
        assert not any(a.startswith("--blocks=") for a in d1["args"])

    def test_ordinary_handoff_predecessor_carries_no_blocks(self, tmp_path):
        """Scoped to the roadmap-baton mint-kind flip, same gate as
        `--roadmap-id`/`--stub-id`: a plain session-handoff successor has no
        roadmap identity for a down-edge to hang off."""
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-08-18-ordinary-with-blocks.md",
            [
                "deliverable_id: DEL-ORDINARY-BLOCKS",
                "handoff_id: hnd-ordinary-blocks-1a2b94",
                "blocks: [dep-01]",
            ],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["cli"] == "coordinator-doc-new")
        assert "--type=handoff" in d1["args"]
        assert not any(a.startswith("--blocks=") for a in d1["args"])


class TestSingleLiveStubIdAfterSuccession:
    """The negative this chunk's own body names: exactly one live record
    carries the `stub_id` after a succession, with the predecessor
    archived. `brief()` is Tier-B read-only and never archives anything
    itself -- the coupling this asserts is that d6 (which DOES archive the
    predecessor via `handoff.supersede_predecessor`) is armed in the SAME
    decision object that mints the roadmap-baton successor carrying
    `stub_id`, so the two halves of the invariant ("mint a new live
    stub_id" and "archive the one that used to hold it") always travel
    together rather than being independently reachable."""

    def test_d6_supersede_and_roadmap_baton_mint_are_the_same_decision(self, tmp_path):
        artifact = _roadmap_baton_predecessor(tmp_path)
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object

        d1 = next(d for d in decision["directives"] if d["cli"] == "coordinator-doc-new")
        d6 = next(
            (d for d in decision["directives"] if d["cli"] == "handoff.supersede_predecessor"),
            None,
        )

        assert "--type=roadmap-baton" in d1["args"], (
            "the successor must mint as a roadmap-baton, carrying a fresh "
            "live stub_id"
        )
        assert d6 is not None, (
            "d6 must arm in the SAME decision object -- an unarchived "
            "predecessor plus a freshly-minted roadmap-baton successor "
            "would leave two live records claiming the same stub_id"
        )
        assert d6["args"][0] == str(artifact)
