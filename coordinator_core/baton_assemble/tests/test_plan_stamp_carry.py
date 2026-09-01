"""coordinator_core.baton_assemble.tests.test_plan_stamp_carry

R5 (2026-08-21, rebuild-the-three-ceremony-assemblers plan C5): a baton
stores its own plan path, stamped at mint, and carries that stamp across the
successor hop -- mirroring `deliverable_id`'s carry-not-remint shape (see
`resolve_lineage`'s own "plan -> predecessor -> mint" docstring order) but
storing the plan's PATH rather than an id, matching `state/sizings/*.yaml`'s
literal `plan:` field shape.

Three things pinned here, per this chunk's own dispatch brief:
  1. stamp at mint -- a session with a currently-claimed plan and no
     predecessor mints a baton carrying that plan's path;
  2. survival across a successor hop -- a continuation baton (artifact_path
     IS the predecessor's own handoff record) carries the predecessor's own
     `governing_plan` frontmatter field forward when it has no fresher plan rung
     of its own;
  3. null stays null -- absent every rung, `governing_plan` is `None`, with no
     `d1c` directive emitted at all (no search, no warning-on-absence).

Mirrors `test_replay_carries_union.py`'s idiom: exercise `resolve_lineage`/
`_build_directives` directly (no `brief()`/ledger self-resolution needed for
cases 2/3), and dispatch `baton-stamp-carried-ids` directly for the actual
frontmatter write in case 1.

Run: python3 -m pytest
coordinator_core/baton_assemble/tests/test_plan_stamp_carry.py -q
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.baton_assemble as ba
import coordinator_core.baton_assemble.apply as ba_apply
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.session import claims as session_claims
from coordinator_core.test_baton_assemble import _init_repo, _write_artifact

# `_dispatch_baton_stamp_carried_ids` routes through `locked_rmw`, which
# shells out to real git (`git_common_dir`) to locate its lock sidecar, and
# `session_claims.claim_plan` shells out to real git too -- needs a real
# repo, not merely a directory. Runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def test_governing_plan_stamped_at_mint_from_claimed_plan(tmp_path):
    """AC1: a standalone mint (no predecessor) under a currently-claimed plan
    carries that plan's own path onto the baton via the `d1c` directive --
    the sole writer for this field (see `_dispatch_baton_stamp_carried_ids`'s
    own docstring; `coordinator-doc-new` never receives a `--governing-plan` flag,
    that CLI is out of this chunk's `writes:` scope)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    plan_slug = "2026-08-21-plan-stamp-mint"
    plan_rel = f"docs/plans/{plan_slug}.md"
    _write_artifact(repo / plan_rel, ["deliverable_id: DEL-PLAN-STAMP"])
    session_claims.claim_plan(plan_slug, cwd=str(repo))

    lineage = ba.resolve_lineage("handoff", "", repo)
    assert lineage["governing_plan"] == plan_rel

    directives = ba._build_directives("handoff", lineage, root=repo)
    d1c = next((d for d in directives if d["id"] == "d1c"), None)
    assert d1c is not None, "the governing-plan stamp directive must be emitted at mint"
    assert d1c["cli"] == "baton-stamp-carried-ids"
    assert d1c["depends_on"] == ["d1"]
    assert d1c["already_satisfied"] is False
    assert d1c["args"] == ["--file", lineage["output_path"], f"--governing-plan={plan_rel}"]

    d2 = next(d for d in directives if d["id"] == "d2")
    assert "d1c" in d2["depends_on"], "d2 must not lint ahead of the governing-plan stamp"

    # Simulate d1's own scaffold (out of this chunk's scope to invoke for
    # real) so the stamp dispatch below has a real file to write onto.
    d1_out = lineage["output_path"]
    _write_artifact(repo / d1_out, ["kind: session-handoff"])

    dispatch = ba_apply._resolve_cli("baton-stamp-carried-ids")
    result = dispatch(d1c["args"], repo)
    assert result["stamped"] == ["governing_plan"]

    after_text = (repo / d1_out).read_text(encoding="utf-8")
    fm_dict = yaml.safe_load(split_frontmatter(after_text).fm_text)
    assert fm_dict["governing_plan"] == plan_rel

    # Read-back-equal short-circuits: a second application writes nothing.
    second = dispatch(d1c["args"], repo)
    assert second["stamped"] == []
    assert (repo / d1_out).read_text(encoding="utf-8") == after_text


def test_governing_plan_survives_the_successor_hop(tmp_path):
    """AC2: a continuation baton (artifact_path IS the predecessor's own
    handoff record) with no fresher plan rung of its own carries the
    predecessor's `governing_plan` forward -- the successor-hop carry this
    chunk's dispatch brief names as load-bearing (RULED, not a judgment
    point)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    predecessor = _write_artifact(
        repo / "state" / "handoffs" / "2026-08-21-predecessor.md",
        [
            "handoff_id: hnd-predecessor-aaa111",
            "deliverable_id: DEL-CARRY-1",
            "governing_plan: docs/plans/2026-08-21-carried-plan.md",
            'predecessor: "none"',
        ],
    )

    lineage = ba.resolve_lineage("handoff", str(predecessor), repo)
    assert lineage["governing_plan"] == "docs/plans/2026-08-21-carried-plan.md"

    directives = ba._build_directives("handoff", lineage, root=repo)
    d1c = next((d for d in directives if d["id"] == "d1c"), None)
    assert d1c is not None
    assert d1c["args"] == [
        "--file",
        lineage["output_path"],
        "--governing-plan=docs/plans/2026-08-21-carried-plan.md",
    ]


def test_governing_plan_stays_null_absent_any_rung(tmp_path):
    """AC3: absent plan is the common case and must be cheap and truthful --
    no `governing_plan` on the predecessor, no claimed plan, no plan-input
    artifact -- `None`, and no `d1c` directive at all (no search, no
    warning-on-absence-at-mint)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    predecessor = _write_artifact(
        repo / "state" / "handoffs" / "2026-08-21-predecessor-no-plan.md",
        [
            "handoff_id: hnd-predecessor-bbb222",
            "deliverable_id: DEL-CARRY-2",
            'predecessor: "none"',
        ],
    )

    lineage = ba.resolve_lineage("handoff", str(predecessor), repo)
    assert lineage["governing_plan"] is None

    directives = ba._build_directives("handoff", lineage, root=repo)
    ids = [d["id"] for d in directives]
    assert "d1c" not in ids

    d2 = next(d for d in directives if d["id"] == "d2")
    assert d2["depends_on"] == ["d1"], "d2's depends_on is unchanged with no governing-plan stamp"


def test_governing_plan_stamp_conflict_raises(tmp_path):
    """A recorded `governing_plan` that disagrees with this run's resolved value
    must refuse rather than silently overwrite -- same conflict posture as
    the `deliverable_ids`/`plan_ids` plural carriers this handler already
    enforces."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    successor = _write_artifact(
        repo / "state" / "handoffs" / "2026-08-21-conflict-successor.md",
        ["kind: session-handoff", "governing_plan: docs/plans/2026-08-21-already-there.md"],
    )

    dispatch = ba_apply._resolve_cli("baton-stamp-carried-ids")
    with pytest.raises(RuntimeError, match="already carries governing_plan"):
        dispatch(
            [
                "--file",
                str(successor.relative_to(repo)),
                "--governing-plan=docs/plans/2026-08-21-a-different-plan.md",
            ],
            repo,
        )


def test_governing_plan_not_stamped_when_the_claimed_plan_does_not_exist(
    tmp_path, capsys
):
    """A plan claim outlives its plan file, and a dead edge is worse than none.

    `coordinator-doc-new --type plan` takes a plan claim at SCAFFOLD time and
    nothing releases it when the file goes away, so a scaffold-and-discard
    leaves a claim behind. Example-game-repo-em hit this 2026-09-01: their baton minted
    with `governing_plan: docs/plans/plan-probe.md`, a throwaway written to a
    scratchpad 40 minutes earlier and deleted, while the baton's real plan sat
    unnamed in its own `predecessor_handoff`.

    The stamp site refuses rather than writing a path that resolves to nothing
    -- a reader cannot tell a dead edge from a typo, and `governing_plan`'s own
    contract already makes absence the cheap, truthful, common answer.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    plan_slug = "2026-09-01-scaffolded-then-discarded"
    plan_rel = f"docs/plans/{plan_slug}.md"
    _write_artifact(repo / plan_rel, ["deliverable_id: DEL-DISCARDED"])
    session_claims.claim_plan(plan_slug, cwd=str(repo))

    # The discard: the claim stays, the file goes.
    (repo / plan_rel).unlink()

    lineage = ba.resolve_lineage("handoff", "", repo)

    assert lineage["governing_plan"] is None
    assert plan_slug in capsys.readouterr().err

    directives = ba._build_directives("handoff", lineage, root=repo)
    assert not [d for d in directives if d["id"] == "d1c"], (
        "no stamp directive may be emitted for an edge that resolves to nothing"
    )


def test_governing_plan_follows_a_plan_archived_out_from_under_the_session(
    tmp_path,
):
    """`fleet.archive_completed_plans` archives on terminal status ALONE, so a
    session holding a live claim can have its plan git-mv'd to
    `archive/specs/<YYYY-MM>/` underneath it. The claim stays valid; only the
    assumed location goes stale.

    Before `claimed_plan._resolve_plan_slug_path`, this case stamped a
    `docs/plans/` path with nothing behind it -- 34 of this repo's 79 plan
    claims were in exactly that state (measured 2026-09-01). The archived plan
    is the RIGHT answer for a baton that executed it, which is why this
    resolves rather than degrading to the no-stamp branch above.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    plan_slug = "2026-08-01-shipped-mid-session"
    plan_rel = f"docs/plans/{plan_slug}.md"
    _write_artifact(repo / plan_rel, ["deliverable_id: DEL-ARCHIVED"])
    session_claims.claim_plan(plan_slug, cwd=str(repo))

    # The archival sweep, as `fleet.archive_completed_plans` performs it.
    archived_rel = f"archive/specs/2026-08/{plan_slug}.md"
    (repo / archived_rel).parent.mkdir(parents=True)
    (repo / plan_rel).rename(repo / archived_rel)

    lineage = ba.resolve_lineage("handoff", "", repo)

    assert lineage["governing_plan"] == archived_rel


def test_a_dead_claim_does_not_mask_the_carried_edge(tmp_path):
    """The claim rung falls THROUGH when its file is missing, rather than
    short-circuiting to no-stamp. A session holding a scaffold-and-discard
    claim while continuing a real baton must still carry that baton's own
    `governing_plan` forward -- otherwise one stale claim silently strips
    provenance off an unrelated continuation."""
    repo = tmp_path / "repo"
    _init_repo(repo)

    discarded_slug = "2026-09-01-discarded-while-continuing"
    discarded_rel = f"docs/plans/{discarded_slug}.md"
    _write_artifact(repo / discarded_rel, ["deliverable_id: DEL-DISCARDED-2"])
    session_claims.claim_plan(discarded_slug, cwd=str(repo))
    (repo / discarded_rel).unlink()

    carried_rel = "docs/plans/2026-08-21-really-executing.md"
    _write_artifact(repo / carried_rel, ["deliverable_id: DEL-CARRY-2"])
    predecessor = _write_artifact(
        repo / "state" / "handoffs" / "2026-09-01-continuing.md",
        [
            "handoff_id: hnd-continuing-bbb222",
            "deliverable_id: DEL-CARRY-2",
            f"governing_plan: {carried_rel}",
            'predecessor: "none"',
        ],
    )

    lineage = ba.resolve_lineage("handoff", str(predecessor), repo)

    assert lineage["governing_plan"] == carried_rel
