"""
coordinator_core.pickup_assemble.tests.test_successor_continuation_chain —
regression for the `compute_successor_handoffs` continuation-chain miss
reported in cross-repo/inbox/2026-08-14-example-retrieval-repo-em-succession-heir-
never-archives.md (item 3): a `pickup-assemble brief` on a predecessor with
a LIVE succession child came back `claim_grant.verdict: granted`,
`coast: clear`, no successor named anywhere in the decision object.

Root cause: the DR-084 supersede verb
(`coordinator_core.archive_stamp.cs_supersede_archive_handoff`) stamps a
handoff `deployment_state: continued` + `continued_into: <successor>` and
`git mv`s it to `archive/handoffs/YYYY-MM/` in the SAME operation. A direct
child superseded this way is gone from `state/handoffs/` at (or before) the
moment `deployment_state: continued` exists on disk, so
`compute_successor_handoffs`'s single-hop, state-only
`dag.referenced_by` scan (plan 2026-08-01-wsc-completeness-gate-and-pickup-
successor.md, C5/AC7) never sees it as a referencer at all — and even when
it IS still scanned (the frontmatter-only, non-archiving `supersede` verb),
the old code dropped it once it saw `deployment_state == "continued"`,
never following `continued_into` to the chain's actual live tip.

Fix (this file's scope: `coordinator_core/pickup_assemble/__init__.py`
only): `_walk_continued_chain` follows `continued_into` — recursively,
since the target can itself be `continued` again — to the first node whose
own `deployment_state` is not `continued`, reading it directly off disk
(archived or not); `_scan_bounded_archive_handoff_dir` extends the
reverse-membership scan far enough (current + previous month's archive,
plus flat `archive/handoffs/*.md`) to find a DIRECT `continued` referencer
that already left `state/handoffs/` entirely. The chain's terminal node
still has to pass the SAME live-candidacy test (status + deployment_state,
+ holder liveness for `in_flight`) as any other candidate — AC8 ("archived
and terminal children surface neither gate nor JP") is unchanged for a
plain terminal (shipped/abandoned/closed) child.

Spec backlink: docs/plans/2026-08-01-wsc-completeness-gate-and-pickup-successor.md, C5/AC7/AC8
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.pickup_assemble import compute_successor_handoffs


def _write(path: Path, frontmatter: str, body: str = "body\n") -> None:
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")


class TestSuccessorContinuationChain:
    """AC1/AC2 of the dispatch: a live successor reached only via a
    `continued` chain is named in `gates.successor`, and a bare no-successor
    predecessor is unaffected."""

    def test_archived_continued_direct_child_surfaces_live_tip(self, tmp_path: Path) -> None:
        """The exact incident shape: the predecessor's ONLY direct child was
        already superseded-and-archived (moved out of `state/handoffs/`,
        `deployment_state: continued`) by the time the predecessor is
        briefed. Without the fix this returns zero candidates — the bug
        this test pins."""
        hd = tmp_path / "state" / "handoffs"
        hd.mkdir(parents=True)
        ad = tmp_path / "archive" / "handoffs" / "2026-08"
        ad.mkdir(parents=True)

        _write(
            hd / "2026-08-10-pred-slug.md",
            "status: open\ndeployment_state: ready_to_fire",
        )
        _write(
            ad / "2026-08-14-c1-slug.md",
            "status: claimed\n"
            "predecessor: state/handoffs/2026-08-10-pred-slug.md\n"
            "deployment_state: continued\n"
            "continued_into: state/handoffs/2026-08-14_220735_c2-slug.md",
        )
        _write(
            hd / "2026-08-14_220735_c2-slug.md",
            "status: open\n"
            "predecessor: state/handoffs/2026-08-14-c1-slug.md\n"
            "deployment_state: ready_to_fire",
        )

        result = compute_successor_handoffs(tmp_path, "state/handoffs/2026-08-10-pred-slug.md")

        assert len(result["candidates"]) == 1
        candidate = result["candidates"][0]
        assert candidate["path"] == "state/handoffs/2026-08-14_220735_c2-slug.md"
        assert candidate["kind"] == "ready_to_fire"
        assert candidate["deployment_state"] == "ready_to_fire"

    def test_multi_hop_continued_chain_resolves_to_final_live_tip(self, tmp_path: Path) -> None:
        """Two `continued` links in a row (both archived) before the live
        tip — the bounded walk must not stop at the first hop."""
        hd = tmp_path / "state" / "handoffs"
        hd.mkdir(parents=True)
        ad = tmp_path / "archive" / "handoffs" / "2026-08"
        ad.mkdir(parents=True)

        _write(hd / "p.md", "status: open\ndeployment_state: ready_to_fire")
        _write(
            ad / "c1a.md",
            "status: claimed\n"
            "predecessor: state/handoffs/p.md\n"
            "deployment_state: continued\n"
            "continued_into: archive/handoffs/2026-08/c1b.md",
        )
        _write(
            ad / "c1b.md",
            "status: claimed\ndeployment_state: continued\ncontinued_into: state/handoffs/c2.md",
        )
        _write(hd / "c2.md", "status: open\ndeployment_state: ready_to_fire")

        result = compute_successor_handoffs(tmp_path, "state/handoffs/p.md")

        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["path"] == "state/handoffs/c2.md"

    def test_no_successor_is_unaffected(self, tmp_path: Path) -> None:
        """Byte-for-byte behavior for the common case: a predecessor with no
        successor at all still returns an empty candidate list."""
        hd = tmp_path / "state" / "handoffs"
        hd.mkdir(parents=True)
        _write(hd / "lonely.md", "status: open\ndeployment_state: ready_to_fire")

        result = compute_successor_handoffs(tmp_path, "state/handoffs/lonely.md")

        assert result == {"candidates": []}

    def test_ordinary_direct_live_successor_still_surfaces(self, tmp_path: Path) -> None:
        """AC3 guard rail: the pre-existing direct-hop path (no continuation
        involved at all) must keep working exactly as before."""
        hd = tmp_path / "state" / "handoffs"
        hd.mkdir(parents=True)
        _write(hd / "p.md", "status: open\ndeployment_state: ready_to_fire")
        _write(
            hd / "child.md",
            "status: open\npredecessor: state/handoffs/p.md\ndeployment_state: ready_to_fire",
        )

        result = compute_successor_handoffs(tmp_path, "state/handoffs/p.md")

        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["path"] == "state/handoffs/child.md"

    def test_terminal_archived_child_still_surfaces_neither_ac8(self, tmp_path: Path) -> None:
        """AC8 guard rail: an ordinary archived, non-continued (e.g.
        abandoned) direct child must still surface nothing — the recent-
        archive scan this fix adds only ever admits a `continued` link, per
        `compute_successor_handoffs`'s own docstring."""
        hd = tmp_path / "state" / "handoffs"
        hd.mkdir(parents=True)
        ad = tmp_path / "archive" / "handoffs" / "2026-08"
        ad.mkdir(parents=True)

        _write(hd / "p.md", "status: open\ndeployment_state: ready_to_fire")
        _write(
            ad / "abandoned-child.md",
            "status: claimed\n"
            "predecessor: state/handoffs/p.md\n"
            "deployment_state: abandoned",
        )

        result = compute_successor_handoffs(tmp_path, "state/handoffs/p.md")

        assert result == {"candidates": []}

    def test_cycle_in_continued_into_terminates_without_candidate(self, tmp_path: Path) -> None:
        """A corrupt `continued_into` cycle must not hang `brief()` — the
        walk's cycle guard bails, contributing no candidate."""
        hd = tmp_path / "state" / "handoffs"
        hd.mkdir(parents=True)
        ad = tmp_path / "archive" / "handoffs" / "2026-08"
        ad.mkdir(parents=True)

        _write(hd / "p.md", "status: open\ndeployment_state: ready_to_fire")
        _write(
            ad / "a.md",
            "status: claimed\n"
            "predecessor: state/handoffs/p.md\n"
            "deployment_state: continued\n"
            "continued_into: archive/handoffs/2026-08/b.md",
        )
        _write(
            ad / "b.md",
            "status: claimed\ndeployment_state: continued\ncontinued_into: archive/handoffs/2026-08/a.md",
        )

        result = compute_successor_handoffs(tmp_path, "state/handoffs/p.md")

        assert result == {"candidates": []}

    def test_self_referential_continued_into_terminates_without_candidate(
        self, tmp_path: Path
    ) -> None:
        """A `continued_into` pointer that names ITSELF is not caught on the
        first hop (`visited` starts empty — the node's own path is never
        pre-seeded) but IS caught on the second hop, when the same resolved
        path is re-visited — terminating in 2 iterations, well under the
        depth cap, without raising or hanging `brief()`.

        Review: coordinatorcode-reviewer-240bf49d Finding 1."""
        hd = tmp_path / "state" / "handoffs"
        hd.mkdir(parents=True)
        ad = tmp_path / "archive" / "handoffs" / "2026-08"
        ad.mkdir(parents=True)

        _write(hd / "p.md", "status: open\ndeployment_state: ready_to_fire")
        _write(
            ad / "self.md",
            "status: claimed\n"
            "predecessor: state/handoffs/p.md\n"
            "deployment_state: continued\n"
            "continued_into: archive/handoffs/2026-08/self.md",
        )

        result = compute_successor_handoffs(tmp_path, "state/handoffs/p.md")

        assert result == {"candidates": []}

    def test_chain_deeper_than_depth_cap_terminates_without_candidate(
        self, tmp_path: Path
    ) -> None:
        """A `continued_into` chain longer than `_MAX_CONTINUATION_CHAIN_DEPTH`
        (12) exhausts the bounded walk's loop and falls through to `None` —
        it must not raise or hang `brief()` even when no cycle is ever
        formed (each node's `continued_into` names a genuinely distinct,
        never-revisited successor).

        Review: coordinatorcode-reviewer-240bf49d Finding 1."""
        hd = tmp_path / "state" / "handoffs"
        hd.mkdir(parents=True)
        ad = tmp_path / "archive" / "handoffs" / "2026-08"
        ad.mkdir(parents=True)

        _write(hd / "p.md", "status: open\ndeployment_state: ready_to_fire")

        # `_MAX_CONTINUATION_CHAIN_DEPTH` (12) hops of `continued` archived
        # nodes, each pointing to the next distinct node — one hop more than
        # the loop's range, so it exhausts without ever resolving a live tip.
        chain_len = 13
        _write(
            ad / "c0.md",
            "status: claimed\n"
            "predecessor: state/handoffs/p.md\n"
            "deployment_state: continued\n"
            "continued_into: archive/handoffs/2026-08/c1.md",
        )
        for i in range(1, chain_len):
            next_name = f"c{i + 1}.md" if i + 1 < chain_len else "tip.md"
            _write(
                ad / f"c{i}.md",
                "status: claimed\n"
                "deployment_state: continued\n"
                f"continued_into: archive/handoffs/2026-08/{next_name}",
            )
        _write(hd / "tip.md", "status: open\ndeployment_state: ready_to_fire")

        result = compute_successor_handoffs(tmp_path, "state/handoffs/p.md")

        assert result == {"candidates": []}
