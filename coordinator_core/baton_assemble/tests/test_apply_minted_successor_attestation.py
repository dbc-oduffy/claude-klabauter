"""
coordinator_core.baton_assemble.tests.test_apply_minted_successor_attestation

Pins C2 (docs/plans/2026-09-12-supersede-admits-an-apply-minted-success.md):
`_dispatch_handoff_supersede_predecessor` (`coordinator_core/baton_assemble/
apply.py`, apply's d6) sets the attested succession around its composition of
`handoff.archive_transition`. It is a process-local ContextVar carrying the
successor path, NOT a parameter -- a parameter proved forgeable through
`housekeeping.cycle`'s verbatim passthrough. `continued_into`/`exclude_path`
here is ALWAYS `lineage["output_path"]`, the SAME value `_build_directives`'s
d6 loop computes for d1's own `--out` target this run (fresh mint, replay
resumption, or a DR-242 Amendment A1 adoption), which is what makes it an
engine fact. See `_APPLY_MINTED_SUCCESSOR`'s own docstring.

Covers:
  - The DoE-claude reproduction AS REPORTED: apply's d6 (composing the real
    `handoff.archive_transition` op, unstubbed) lands the succession edge on
    a never-claimed predecessor whose successor is the FRESHLY MINTED, never-
    claimed one d1 just wrote. A2 section 7.3 clause 2 is discharged by
    provenance on this door, not re-checked -- see that clause's note in
    `handoff_archive_transition._attested_succession_refusal`.
  - A speculative successor-named child (no identity-checkable
    `predecessor_id` -- the § 3 Instance-1 shape) is still refused by clause 3
    fail-closed, DEGRADED not raised.
  - A refused attestation leaves the successor file on disk -- `_cleanup_
    successor` must not fire on this branch (2026-08-03 break-class fix,
    generalized to the attestation refusal shape).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.baton_assemble.apply as ba_apply
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.test_baton_assemble import _git, _init_repo, _write_artifact

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PRED_REL = "state/handoffs/predecessor.md"
_SUCC_REL = "state/handoffs/successor.md"

_NEVER_CLAIMED_PREDECESSOR_FM = [
    "handoff_id: hnd-pred-1a2b4c",
    "status: open",
    "deployment_state: ready_to_fire",
    "title: Predecessor handoff",
    "created: 2026-07-27",
    "branch: work/test/2026-01-01",
    'predecessor: "none"',
    "category: infra",
    "summary: a never-claimed predecessor, the DR-242 gap this plan closes",
    "pickup_ready: true",
]


def _seed_repo(repo: Path) -> Path:
    _init_repo(repo)
    predecessor = _write_artifact(repo / _PRED_REL, list(_NEVER_CLAIMED_PREDECESSOR_FM))
    _git(repo, "add", _PRED_REL)
    _git(repo, "commit", "-m", "add predecessor")
    return predecessor


def _seed_successor(repo: Path, claimed: bool) -> Path:
    fm = [
        "title: Successor handoff",
        "created: 2026-09-12",
        "branch: work/test/2026-01-01",
        "status: claimed" if claimed else "status: open",
        f'predecessor: "{_PRED_REL}"',
        'predecessor_id: "hnd-pred-1a2b4c"',
        "deployment_state: active",
    ]
    if claimed:
        fm.append('claimed_by: "successor-session"')
        fm.append('claimed_at: "2026-09-12T00:00:00Z"')
    successor = _write_artifact(repo / _SUCC_REL, fm)
    _git(repo, "add", _SUCC_REL)
    _git(repo, "commit", "-m", "add successor")
    return successor


# ---------------------------------------------------------------------------
# DoE-claude reproduction: real op, unstubbed
# ---------------------------------------------------------------------------


def test_lands_the_edge_over_a_never_claimed_predecessor_and_a_freshly_minted_successor(
    tmp_path,
):
    """The DoE-claude reproduction AS REPORTED: the successor is the one d1
    minted seconds ago, so it is `status: open` and has never been claimed.

    This is the regression test for the defect the first cut of this plan
    shipped past. Implementing A2 section 7.3 clause 2 literally ("the
    successor is itself claimed_or_shipped") refuses exactly this shape, and a
    suite that only ever seeded a CLAIMED successor went green while the
    reported bug stayed live. If clause 2 is ever "restored" as a literal
    check, this test is what goes red."""
    repo = tmp_path / "repo"
    predecessor = _seed_repo(repo)
    _seed_successor(repo, claimed=False)

    result = ba_apply._dispatch_handoff_supersede_predecessor(
        [_PRED_REL, _SUCC_REL, _SUCC_REL], repo
    )

    assert result.get("degraded") is None, result
    inner = result["result"]
    assert inner["superseded"] is True, inner
    assert inner["exit_code"] == 0, inner

    # The predecessor was archived + stamped, exactly as a claimed-or-shipped
    # predecessor's supersede already lands.
    assert not predecessor.exists()
    archived = list((repo / "archive" / "handoffs").rglob("predecessor.md"))
    assert len(archived) == 1, archived
    split = split_frontmatter(archived[0].read_text(encoding="utf-8"))
    assert split is not None
    assert read_fm_field(split.fm_text, "deployment_state") == "continued"
    assert read_fm_field(split.fm_text, "continued_into") == _SUCC_REL


# ---------------------------------------------------------------------------
# A bare successor-named child apply did not mint stays refused
# ---------------------------------------------------------------------------


def test_bare_successor_named_child_still_refuses(tmp_path, capsys):
    """The section 3 Instance-1 shape: a speculative child that NAMES the
    predecessor but carries no identity-checkable edge back to it.

    With clause 2 discharged by provenance on this door, clause 3 fail-closed
    is what excludes this shape -- so the child here is seeded WITHOUT a
    `predecessor_id`, which is what a speculative successor-named file
    actually looks like. Seeding one merely `status: open` would NOT be this
    shape: that is the legitimate freshly-minted successor above.
    """
    repo = tmp_path / "repo"
    predecessor = _seed_repo(repo)
    before = predecessor.read_text(encoding="utf-8")
    _write_artifact(
        repo / _SUCC_REL,
        [
            "title: Hopeful child",
            "created: 2026-09-12",
            "branch: work/test/2026-01-01",
            "status: open",
            f'predecessor: "{_PRED_REL}"',
            "deployment_state: active",
        ],
    )
    _git(repo, "add", _SUCC_REL)
    _git(repo, "commit", "-m", "add hopeful child")

    result = ba_apply._dispatch_handoff_supersede_predecessor(
        [_PRED_REL, _SUCC_REL, _SUCC_REL], repo
    )

    assert result.get("degraded") is not None, result
    assert result["degraded"]["reason"] == "predecessor-not-claimed-or-shipped"
    assert "clause 3" in (result["degraded"].get("error") or ""), result
    # DEGRADED, not raised -- the predecessor is byte-identical.
    assert predecessor.read_text(encoding="utf-8") == before
    assert "degraded" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# A refused attestation leaves the successor file on disk
# ---------------------------------------------------------------------------


def test_refused_attestation_leaves_the_successor_on_disk(tmp_path):
    repo = tmp_path / "repo"
    _seed_repo(repo)
    # Clause 4 refusal: the predecessor already carries a DIFFERENT
    # continued_into, so predecessor-side evidence wins.
    successor = _seed_successor(repo, claimed=False)
    pred = repo / _PRED_REL
    pred.write_text(
        pred.read_text(encoding="utf-8").replace(
            "pickup_ready: true",
            'pickup_ready: true\ncontinued_into: "state/handoffs/a-different-one.md"',
        ),
        encoding="utf-8",
    )

    result = ba_apply._dispatch_handoff_supersede_predecessor(
        [_PRED_REL, _SUCC_REL, _SUCC_REL], repo
    )

    assert result.get("degraded") is not None, result
    assert "clause 4" in (result["degraded"].get("error") or ""), result
    # `_cleanup_successor` must NOT fire on a choke-point refusal -- nothing
    # was mutated, and the successor this run's own d1 minted must survive
    # (2026-08-03 break-class fix, generalized to the attestation shape).
    # (The third refusal test this file used to carry was the intersection of
    # this one and its sibling -- deleted per overengineering review, with its
    # one distinct assertion folded in above.)
    assert successor.exists(), "a refused attestation must not delete the successor"
