
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

import pytest

import coordinator_core.baton_assemble as ba
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.test_baton_assemble import (
    _FAKE_OPERATOR_CONFIG,
    _REPO_CLAUDE_KLABAUTER_BIN,
    _write_artifact,
)
import coordinator_core.baton_assemble.apply as ba_apply

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))
    monkeypatch.setattr(ba_apply, "_resolve_claude_klabauter_bin", lambda: _REPO_CLAUDE_KLABAUTER_BIN)


def _write_deliverable_carrier(root: Path, deliverable_id: str) -> Path:
    return _write_artifact(
        root / "docs" / "plans" / "2026-08-05-carrier-plan.md",
        [f'deliverable_id: "{deliverable_id}"'],
    )


def _write_handoff(
    root: Path,
    rel: str,
    deliverable_id: str,
    deployment_state: str,
    status: str = "claimed",
    claimed_by: str = "some-session-id",
    handoff_id: str | None = None,
) -> Path:
    lines = [
        f"deliverable_id: {deliverable_id}",
        f"status: {status}",
        f"deployment_state: {deployment_state}",
        f"claimed_by: {claimed_by}",
    ]
    if handoff_id:
        lines.append(f"handoff_id: {handoff_id}")
    return _write_artifact(root / rel, lines)


class TestTerminalSetBoundaryAC2:
    """`closed`/`abandoned` must NOT warn; `in_flight`/`ready_to_fire`/
    `awaiting_gate` MUST. This is the assertion that catches an import of
    either three-member `_TERMINAL_DEPLOYMENT_STATES` copy in the tree,
    which omits `abandoned` -- both of those omissions would (wrongly) warn
    on an already-dead baton."""

    @pytest.mark.parametrize("deployment_state", sorted(HANDOFF_TERMINAL_DEPLOYMENT))
    def test_a_terminal_deployment_state_never_warns(self, tmp_path, deployment_state):
        predecessor = _write_deliverable_carrier(tmp_path, "DEL-TERMINAL")
        _write_handoff(
            tmp_path,
            "state/handoffs/terminal-candidate.md",
            "DEL-TERMINAL",
            deployment_state,
        )
        lineage = ba.resolve_lineage("handoff", str(predecessor), tmp_path)
        assert lineage["deliverable_collision"] is None, (
            f"deployment_state={deployment_state!r} is in the canonical "
            "HANDOFF_TERMINAL_DEPLOYMENT set and must never warn"
        )

    @pytest.mark.parametrize(
        "deployment_state", ["in_flight", "ready_to_fire", "awaiting_gate"]
    )
    def test_a_non_terminal_deployment_state_always_warns(self, tmp_path, deployment_state):
        predecessor = _write_deliverable_carrier(tmp_path, "DEL-LIVE")
        assert deployment_state not in HANDOFF_TERMINAL_DEPLOYMENT
        _write_handoff(
            tmp_path,
            "state/handoffs/live-candidate.md",
            "DEL-LIVE",
            deployment_state,
        )
        lineage = ba.resolve_lineage("handoff", str(predecessor), tmp_path)
        assert lineage["deliverable_collision"] is not None, (
            f"deployment_state={deployment_state!r} is NOT in "
            "HANDOFF_TERMINAL_DEPLOYMENT and must warn"
        )


class TestCollisionRecordShapeAC1AC3:

    def test_no_collision_is_a_bare_none(self, tmp_path):
        predecessor = _write_deliverable_carrier(tmp_path, "DEL-LONE")
        lineage = ba.resolve_lineage("handoff", str(predecessor), tmp_path)
        assert lineage["deliverable_collision"] is None

    def test_a_hit_names_path_status_deployment_state_claimed_by(self, tmp_path):
        predecessor = _write_deliverable_carrier(tmp_path, "DEL-SHAPE")
        _write_handoff(
            tmp_path,
            "state/handoffs/colliding.md",
            "DEL-SHAPE",
            "in_flight",
            status="claimed",
            claimed_by="the-claim-holder",
        )
        lineage = ba.resolve_lineage("handoff", str(predecessor), tmp_path)
        collision = lineage["deliverable_collision"]
        assert collision is not None
        assert collision["path"] == "state/handoffs/colliding.md"
        assert collision["status"] == "claimed"
        assert collision["deployment_state"] == "in_flight"
        assert collision["claimed_by"] == "the-claim-holder"

    def test_a_hit_emits_exactly_one_stderr_advisory_line(self, tmp_path, capsys):
        predecessor = _write_deliverable_carrier(tmp_path, "DEL-STDERR")
        _write_handoff(
            tmp_path,
            "state/handoffs/colliding.md",
            "DEL-STDERR",
            "in_flight",
            status="claimed",
            claimed_by="the-claim-holder",
        )
        capsys.readouterr()
        ba.resolve_lineage("handoff", str(predecessor), tmp_path)
        captured = capsys.readouterr()
        collision_lines = [
            line
            for line in captured.err.splitlines()
            if "already held by a live baton" in line
        ]
        assert len(collision_lines) == 1, (
            f"expected exactly one collision advisory line, got {collision_lines!r} "
            f"(full stderr={captured.err!r})"
        )
        assert "state/handoffs/colliding.md" in collision_lines[0]
        assert "the-claim-holder" in collision_lines[0]

    def test_no_collision_emits_no_collision_advisory(self, tmp_path, capsys):
        predecessor = _write_deliverable_carrier(tmp_path, "DEL-QUIET")
        capsys.readouterr()
        ba.resolve_lineage("handoff", str(predecessor), tmp_path)
        captured = capsys.readouterr()
        assert "already held by a live baton" not in captured.err


class TestResolvedAddressWiringAC6:

    def test_a_reachable_claimed_by_prints_the_resolved_address(self, tmp_path, capsys, monkeypatch):
        import coordinator_core.session.reachability as reach

        predecessor = _write_deliverable_carrier(tmp_path, "DEL-ADDR")
        _write_handoff(
            tmp_path,
            "state/handoffs/colliding.md",
            "DEL-ADDR",
            "in_flight",
            claimed_by="the-claim-holder",
        )
        monkeypatch.setattr(
            reach,
            "resolve_address",
            lambda owner_id: reach.ResolveResult(
                outcome="reachable", session_id=owner_id, address="claude-klabauter-57 [b2afcd]"
            ),
        )
        capsys.readouterr()
        ba.resolve_lineage("handoff", str(predecessor), tmp_path)
        captured = capsys.readouterr()
        assert "claude-klabauter-57 [b2afcd]" in captured.err
        assert "the-claim-holder" in captured.err

    def test_a_not_reachable_claimed_by_renders_no_address(self, tmp_path, capsys, monkeypatch):
        import coordinator_core.session.reachability as reach

        predecessor = _write_deliverable_carrier(tmp_path, "DEL-ADDR-NOTREACH")
        _write_handoff(
            tmp_path,
            "state/handoffs/colliding.md",
            "DEL-ADDR-NOTREACH",
            "in_flight",
            claimed_by="the-claim-holder",
        )
        monkeypatch.setattr(
            reach,
            "resolve_address",
            lambda owner_id: reach.ResolveResult(
                outcome="not_reachable", session_id=owner_id
            ),
        )
        capsys.readouterr()
        ba.resolve_lineage("handoff", str(predecessor), tmp_path)
        captured = capsys.readouterr()
        assert "the-claim-holder" in captured.err
        assert "already held by a live baton" in captured.err
        assert "send-message-address" not in captured.err

    def test_an_ambiguous_claimed_by_renders_no_address(self, tmp_path, capsys, monkeypatch):
        import coordinator_core.session.reachability as reach

        predecessor = _write_deliverable_carrier(tmp_path, "DEL-ADDR-AMBIG")
        _write_handoff(
            tmp_path,
            "state/handoffs/colliding.md",
            "DEL-ADDR-AMBIG",
            "in_flight",
            claimed_by="the-claim-holder",
        )
        monkeypatch.setattr(
            reach,
            "resolve_address",
            lambda owner_id: reach.ResolveResult(
                outcome="ambiguous",
                session_id=owner_id,
                candidates=[
                    reach.Candidate(
                        session_id="session-aaaaaa",
                        name="claude-klabauter-11",
                        ref="aaaaaa",
                        address="claude-klabauter-11 [aaaaaa]",
                    ),
                    reach.Candidate(
                        session_id="session-bbbbbb",
                        name="claude-klabauter-11",
                        ref="bbbbbb",
                        address="claude-klabauter-11 [bbbbbb]",
                    ),
                ],
            ),
        )
        capsys.readouterr()
        ba.resolve_lineage("handoff", str(predecessor), tmp_path)
        captured = capsys.readouterr()
        assert "the-claim-holder" in captured.err
        assert "already held by a live baton" in captured.err
        assert "send-message-address" not in captured.err
        assert "claude-klabauter-11 [aaaaaa]" not in captured.err
        assert "claude-klabauter-11 [bbbbbb]" not in captured.err

    def test_own_session_claimed_by_renders_the_own_session_marker(self, tmp_path, capsys, monkeypatch):
        import coordinator_core.session.reachability as reach

        predecessor = _write_deliverable_carrier(tmp_path, "DEL-ADDR-OWN")
        _write_handoff(
            tmp_path,
            "state/handoffs/colliding.md",
            "DEL-ADDR-OWN",
            "in_flight",
            claimed_by="the-claim-holder",
        )
        monkeypatch.setattr(
            reach,
            "resolve_address",
            lambda owner_id: reach.ResolveResult(outcome="own_session", session_id=owner_id),
        )
        capsys.readouterr()
        ba.resolve_lineage("handoff", str(predecessor), tmp_path)
        captured = capsys.readouterr()
        assert "send-message-address='<this session>'" in captured.err
        assert "the-claim-holder" in captured.err

    def test_resolution_failure_degrades_to_the_uuid_only_message(self, tmp_path, capsys, monkeypatch):
        import coordinator_core.session.reachability as reach

        predecessor = _write_deliverable_carrier(tmp_path, "DEL-ADDR-FAIL")
        _write_handoff(
            tmp_path,
            "state/handoffs/colliding.md",
            "DEL-ADDR-FAIL",
            "in_flight",
            claimed_by="the-claim-holder",
        )

        def _raise(owner_id):
            raise RuntimeError("simulated resolution failure")

        monkeypatch.setattr(reach, "resolve_address", _raise)
        capsys.readouterr()
        ba.resolve_lineage("handoff", str(predecessor), tmp_path)
        captured = capsys.readouterr()
        assert "the-claim-holder" in captured.err
        assert "already held by a live baton" in captured.err
        assert "send-message-address" not in captured.err


class TestSelfExclusionAC5:
    """A re-run over an already-written baton carrying this `deliverable_id`
    must not warn about that same path. Reproduced via the module's own
    documented replay-resumption mechanism (`_resume_recorded_successor_
    path`): a claimed predecessor whose `deployment_state: continued` +
    `continued_into` a prior attempt already wrote pins `output_path` (the
    exclusion boundary `_scan_deliverable_collision` is handed) to that
    EXISTING successor file, which -- because it IS this deliverable's own
    baton -- necessarily carries the same `deliverable_id` and a live
    `deployment_state`. Without self-exclusion this would unconditionally
    warn on every replay."""

    def test_a_replay_does_not_warn_on_its_own_already_written_successor(self, tmp_path):
        successor_rel = "state/handoffs/2026-08-05_120000_successor.md"
        _write_handoff(
            tmp_path,
            successor_rel,
            "DEL-REPLAY",
            "in_flight",
            status="open",
            claimed_by="",
            handoff_id="HID-SUCCESSOR",
        )
        predecessor = _write_artifact(
            tmp_path / "state" / "handoffs" / "predecessor.md",
            [
                "deliverable_id: DEL-REPLAY",
                "handoff_id: HID-PRED",
                "status: claimed",
                "claimed_at: '2026-08-05T10:00:00Z'",
                "claimed_by: some-session",
                "deployment_state: continued",
                f"continued_into: {successor_rel}",
            ],
        )
        lineage = ba.resolve_lineage("handoff", str(predecessor), tmp_path)
        assert lineage["resumed_successor"] == successor_rel, (
            "fixture did not actually exercise the replay-resumption path -- "
            f"lineage={lineage}"
        )
        assert lineage["deliverable_collision"] is None, (
            "a replay must never warn about the very successor it is resuming"
        )


class TestDegradePathAC6:

    def test_unreadable_and_frontmatter_less_candidates_do_not_raise(self, tmp_path):
        predecessor = _write_deliverable_carrier(tmp_path, "DEL-DEGRADE")
        unreadable = _write_handoff(
            tmp_path,
            "state/handoffs/unreadable.md",
            "DEL-DEGRADE",
            "in_flight",
        )
        frontmatter_less = tmp_path / "state" / "handoffs" / "no-frontmatter.md"
        frontmatter_less.write_text("# Just a body, no frontmatter block.\n", encoding="utf-8")
        if sys.platform == "win32" or os.geteuid() == 0:
            pytest.skip(
                "permission bits do not block reads for root or on this platform"
            )
        os.chmod(unreadable, 0o000)
        try:
            _write_handoff(
                tmp_path,
                "state/handoffs/genuine-collision.md",
                "DEL-DEGRADE",
                "in_flight",
                claimed_by="genuine-holder",
            )
            lineage = ba.resolve_lineage("handoff", str(predecessor), tmp_path)
        finally:
            os.chmod(unreadable, 0o644)

        collision = lineage["deliverable_collision"]
        assert collision is not None
        assert collision["path"] == "state/handoffs/genuine-collision.md"
        assert collision["claimed_by"] == "genuine-holder"

    def test_frontmatter_less_candidate_alone_yields_no_collision(self, tmp_path):
        predecessor = _write_deliverable_carrier(tmp_path, "DEL-DEGRADE-ONLY")
        handoffs_dir = tmp_path / "state" / "handoffs"
        handoffs_dir.mkdir(parents=True)
        (handoffs_dir / "no-frontmatter.md").write_text(
            "# Just a body.\n", encoding="utf-8"
        )
        lineage = ba.resolve_lineage("handoff", str(predecessor), tmp_path)
        assert lineage["deliverable_collision"] is None


class TestAC4WriteAlwaysProceedsByteIdentical:

    def _build_predecessor(self, root: Path) -> Path:
        return _write_deliverable_carrier(root, "DEL-BYTEID")

    def test_resolve_lineage_return_is_additive_only(self, tmp_path_factory):
        clean_root = tmp_path_factory.mktemp("clean")
        predecessor = self._build_predecessor(clean_root)
        clean_lineage = ba.resolve_lineage("handoff", str(predecessor), clean_root)

        _write_handoff(
            clean_root,
            "state/handoffs/other-live.md",
            "DEL-BYTEID",
            "in_flight",
            claimed_by="someone-else",
        )
        colliding_lineage = ba.resolve_lineage("handoff", str(predecessor), clean_root)

        assert clean_lineage["deliverable_collision"] is None
        assert colliding_lineage["deliverable_collision"] is not None

        clean_compare = copy.deepcopy(clean_lineage)
        colliding_compare = copy.deepcopy(colliding_lineage)
        del clean_compare["deliverable_collision"]
        del colliding_compare["deliverable_collision"]
        assert clean_compare == colliding_compare, (
            "a deliverable_collision hit must change no OTHER key of the "
            "lineage dict -- additive-only, per AC4"
        )

    def test_brief_decision_object_is_byte_identical_but_for_the_collision_key(
        self, tmp_path_factory
    ):
        clean_root = tmp_path_factory.mktemp("clean")
        predecessor = self._build_predecessor(clean_root)

        clean_result = ba.brief("handoff", str(predecessor), repo_root=clean_root)

        _write_handoff(
            clean_root,
            "state/handoffs/other-live.md",
            "DEL-BYTEID",
            "in_flight",
            claimed_by="someone-else",
        )
        colliding_result = ba.brief("handoff", str(predecessor), repo_root=clean_root)

        assert clean_result.exit_code == colliding_result.exit_code, (
            "a collision must never change brief()'s exit code"
        )

        clean_decision = copy.deepcopy(clean_result.decision_object)
        colliding_decision = copy.deepcopy(colliding_result.decision_object)
        clean_lineage = clean_decision["artifact"]["lineage"]
        colliding_lineage = colliding_decision["artifact"]["lineage"]
        assert clean_lineage["deliverable_collision"] is None
        assert colliding_lineage["deliverable_collision"] is not None
        del clean_lineage["deliverable_collision"]
        del colliding_lineage["deliverable_collision"]

        assert clean_decision == colliding_decision, (
            "a collision must change no directive, no judgment point, and no "
            "other lineage field -- the ONLY permitted delta between these "
            "two decision objects is the deliverable_collision key itself"
        )


def _write_chain_handoff(
    root: Path,
    rel: str,
    deliverable_id: str,
    handoff_id: str,
    deployment_state: str = "in_flight",
    predecessor: str | None = None,
    claimed_by: str = "some-session-id",
) -> Path:
    lines = [
        f"deliverable_id: {deliverable_id}",
        "status: claimed",
        f"deployment_state: {deployment_state}",
        f"claimed_by: {claimed_by}",
        f"handoff_id: {handoff_id}",
    ]
    if predecessor is not None:
        lines.append(f"predecessor: {predecessor}")
    return _write_artifact(root / rel, lines)


class TestAncestorChainExclusion:

    def test_grandparent_exclusion_three_deep_chain(self, tmp_path):
        _write_chain_handoff(
            tmp_path, "state/handoffs/chain-a.md", "DEL-CHAIN", "chain-a-id"
        )
        _write_chain_handoff(
            tmp_path,
            "state/handoffs/chain-b.md",
            "DEL-CHAIN",
            "chain-b-id",
            predecessor="state/handoffs/chain-a.md",
        )
        chain_c = _write_chain_handoff(
            tmp_path,
            "state/handoffs/chain-c.md",
            "DEL-CHAIN",
            "chain-c-id",
            predecessor="state/handoffs/chain-b.md",
        )
        lineage = ba.resolve_lineage("handoff", str(chain_c), tmp_path)
        assert lineage["deliverable_collision"] is None, (
            "a 3-deep non-terminal chain (A <- B <- C) sharing one "
            "deliverable_id, none of them a roadmap-baton, must not warn "
            "when minting off C -- A (the grandparent) is the regression a "
            "one-hop-only exclusion misses"
        )

    def test_sibling_still_collides(self, tmp_path):
        _write_chain_handoff(
            tmp_path, "state/handoffs/parent.md", "DEL-SIB", "parent-id"
        )
        child = _write_chain_handoff(
            tmp_path,
            "state/handoffs/child.md",
            "DEL-SIB",
            "child-id",
            predecessor="state/handoffs/parent.md",
        )
        _write_chain_handoff(
            tmp_path,
            "state/handoffs/sibling.md",
            "DEL-SIB",
            "sibling-id",
            predecessor="state/handoffs/parent.md",
            claimed_by="genuine-holder",
        )
        lineage = ba.resolve_lineage("handoff", str(child), tmp_path)
        collision = lineage["deliverable_collision"]
        assert collision is not None, (
            "a sibling reached only via the SAME parent, not via child's own "
            "ancestor path, is not an ancestor and must still collide"
        )
        assert collision["path"] == "state/handoffs/sibling.md"
        assert collision["claimed_by"] == "genuine-holder"

    def test_cycle_is_safe(self, tmp_path):
        cycle_x = _write_chain_handoff(
            tmp_path,
            "state/handoffs/cycle-x.md",
            "DEL-CYCLE",
            "cycle-x-id",
            predecessor="state/handoffs/cycle-y.md",
        )
        _write_chain_handoff(
            tmp_path,
            "state/handoffs/cycle-y.md",
            "DEL-CYCLE",
            "cycle-y-id",
            predecessor="state/handoffs/cycle-x.md",
        )
        lineage = ba.resolve_lineage("handoff", str(cycle_x), tmp_path)
        assert lineage["deliverable_collision"] is None, (
            "a mutually-referencing predecessor cycle (X <-> Y) must "
            "terminate and return a verdict rather than hang or raise -- "
            "both members of the cycle are on X's own ancestor path"
        )

    def test_broken_chain_link_skipped_without_raising(self, tmp_path):
        _write_chain_handoff(
            tmp_path,
            "state/handoffs/broken-y.md",
            "DEL-BROKEN",
            "broken-y-id",
            predecessor="state/handoffs/does-not-exist-ancestor.md",
        )
        broken_c = _write_chain_handoff(
            tmp_path,
            "state/handoffs/broken-c.md",
            "DEL-BROKEN",
            "broken-c-id",
            predecessor="state/handoffs/broken-y.md",
        )
        lineage = ba.resolve_lineage("handoff", str(broken_c), tmp_path)
        assert lineage["deliverable_collision"] is None, (
            "a predecessor: pointer at a nonexistent path must be skipped "
            "without raising, and the reachable part of the chain (Y, "
            "C's own real predecessor) must still be excluded"
        )


class TestRoadmapBatonExclusion:
    """2026-08-05 example-market-data-repo-em cross-repo memo: a `kind:
    roadmap-baton` candidate carrying the same `deliverable_id` is the
    DESIGNED plan -> predecessor -> mint carry (`roadmap-planning/SKILL.md`
    § D1), not a duplicate -- `_scan_deliverable_collision` must exclude it
    regardless of `deployment_state`, per DoE-claude
    `coordinator/docs/wiki/coordinator-tripwires.md:1454`. Paired with a
    non-baton control to guard against an over-broad exclusion that would
    also swallow the genuine-detection case."""

    def test_a_roadmap_baton_candidate_never_warns(self, tmp_path):
        predecessor = _write_deliverable_carrier(tmp_path, "DEL-ROADMAP-BATON")
        _write_artifact(
            tmp_path / "state" / "handoffs" / "roadmap-baton.md",
            [
                "deliverable_id: DEL-ROADMAP-BATON",
                "status: open",
                "deployment_state: in_flight",
                "claimed_by: ''",
                "kind: roadmap-baton",
            ],
        )
        lineage = ba.resolve_lineage("handoff", str(predecessor), tmp_path)
        assert lineage["deliverable_collision"] is None, (
            "a non-terminal roadmap-baton candidate carrying the same "
            "deliverable_id is the designed carry, not a collision"
        )

    def test_a_non_baton_handoff_still_collides(self, tmp_path):
        predecessor = _write_deliverable_carrier(tmp_path, "DEL-ORDINARY-HANDOFF")
        _write_handoff(
            tmp_path,
            "state/handoffs/ordinary.md",
            "DEL-ORDINARY-HANDOFF",
            "in_flight",
            claimed_by="genuine-holder",
        )
        lineage = ba.resolve_lineage("handoff", str(predecessor), tmp_path)
        collision = lineage["deliverable_collision"]
        assert collision is not None, (
            "excluding roadmap-baton candidates must not suppress a genuine "
            "duplicate carried by an ordinary handoff"
        )
        assert collision["path"] == "state/handoffs/ordinary.md"
        assert collision["claimed_by"] == "genuine-holder"


class TestAC7LiveReproductionFixture:

    _DELIVERABLE_ID = "dlv-session-shape-attribution-key-the-gate-o-da2621"
    _FIRST_REL = "state/handoffs/2026-08-05-session-shape-attribution-structural-gate.md"
    _CLAIMED_BY = "20eb021a-a542-44bb-93d4-98742474f0b5"

    def test_authoring_the_second_handoff_off_the_shared_plan_pointer_collides(
        self, tmp_path
    ):
        _write_artifact(
            tmp_path / self._FIRST_REL,
            [
                'title: "Execute the session-shape attribution plan — the brake '
                'exists, wire it to the attribution"',
                "created: 2026-08-05",
                "status: claimed",
                "predecessor: none",
                "kind: session-handoff",
                "handoff_phase: continuation",
                "deployment_state: in_flight",
                "claimed_at: '2026-08-05T13:25:54Z'",
                f"claimed_by: {self._CLAIMED_BY}",
                "category: infra",
                f'deliverable_id: "{self._DELIVERABLE_ID}"',
                'handoff_id: "hnd-execute-session-shape-attrib-45c06c"',
                'authoring_session: "e1891438-c603-461c-ab8b-fbd5c92dcab9"',
            ],
        )
        plan = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-08-05-session-shape-attribution-structural-gate.md",
            [f'deliverable_id: "{self._DELIVERABLE_ID}"'],
        )

        lineage = ba.resolve_lineage("handoff", str(plan), tmp_path)
        assert lineage["deliverable_id"] == self._DELIVERABLE_ID

        collision = lineage["deliverable_collision"]
        assert collision is not None, (
            "authoring the second handoff off the shared plan pointer must "
            "warn: a live, non-terminal baton already holds this "
            "deliverable_id"
        )
        assert collision["path"] == self._FIRST_REL
        assert collision["status"] == "claimed"
        assert collision["deployment_state"] == "in_flight"
        assert collision["claimed_by"] == self._CLAIMED_BY


class TestLineageSourceExclusion:
    """An ordinary `/handoff` continuation must not warn about its own
    predecessor.

    The regression this pins is a false POSITIVE, and it fired on the most
    common flow in the system: a continuation inherits its `deliverable_id`
    from its predecessor, and that predecessor is live, non-terminal, and not
    `output_path` -- so the first cut of the scan reported it as a competing
    baton on every single continuation. A warn-only guard that cries wolf on
    the ordinary path is worse than no guard: it trains its reader to ignore
    the one case it exists to catch.

    The discriminator is NOT "is this candidate my predecessor" but "did this
    run read its own `deliverable_id` FROM that candidate". A genuine
    duplicate resolves the same id INDEPENDENTLY -- two sessions each running
    their own cascade off a shared `plan:` pointer -- so the competing baton
    is never the artifact the run read its lineage from. Both halves are
    asserted below; dropping either one is how this regression returns.
    """

    def _predecessor(self, root: Path, deliverable_id: str) -> Path:
        handoffs = root / "state" / "handoffs"
        handoffs.mkdir(parents=True, exist_ok=True)
        pred = handoffs / "2026-08-05-predecessor.md"
        pred.write_text(
            "---\n"
            'title: "the predecessor"\n'
            f"deliverable_id: {deliverable_id}\n"
            "status: claimed\n"
            "deployment_state: in_flight\n"
            "claimed_by: my-own-session-id\n"
            "---\nbody\n",
            encoding="utf-8",
        )
        return pred

    def test_continuation_does_not_warn_about_its_own_predecessor(self, tmp_path: Path):
        did = "dlv-shared-work-abc123"
        pred = self._predecessor(tmp_path, did)
        successor = tmp_path / "state" / "handoffs" / "2026-08-05-successor.md"
        assert (
            ba._scan_deliverable_collision(did, successor, tmp_path, pred) is None
        ), "a continuation must not report its own lineage source as a competing baton"

    def test_independently_resolved_duplicate_still_fires(self, tmp_path: Path):
        did = "dlv-shared-work-abc123"
        self._predecessor(tmp_path, did)
        successor = tmp_path / "state" / "handoffs" / "2026-08-05-successor.md"
        plan = tmp_path / "docs" / "plans" / "some-plan.md"
        hit = ba._scan_deliverable_collision(did, successor, tmp_path, plan)
        assert hit is not None, "excluding the lineage source must not suppress a real duplicate"
        assert hit["path"] == "state/handoffs/2026-08-05-predecessor.md"
        assert hit["claimed_by"] == "my-own-session-id"

    def test_absent_lineage_source_is_unchanged(self, tmp_path: Path):
        did = "dlv-shared-work-abc123"
        self._predecessor(tmp_path, did)
        successor = tmp_path / "state" / "handoffs" / "2026-08-05-successor.md"
        assert ba._scan_deliverable_collision(did, successor, tmp_path, None) is not None
