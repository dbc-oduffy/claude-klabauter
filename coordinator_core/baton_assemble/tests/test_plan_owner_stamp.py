"""coordinator_core.baton_assemble.tests.test_plan_owner_stamp

R5 reverse edge (2026-08-21, rebuild-the-three-ceremony-assemblers plan C6):
a plan records which baton currently owns it, stamped at the baton-claims-plan
moment. Write site: `coordinator_core.baton_assemble.apply._stamp_plan_owner_
back_edge`, called from `session.claims.claim_plan(..., for_execution=True)`
-- the plan-execution claim, and the only claim that can answer "which baton
is advancing this plan right now". `_compensate_d5_release_claim` calls the
same stamp on its partial-mutation reclaim path; that compensator was for a
while the ONLY caller, which is why the field landed on zero plans in a
corpus of 346 while these tests stayed green (AC6 below pins the happy path
so that cannot recur).

Pinned here, per this chunk's own dispatch brief:
  1. claiming stamps `claimed_by_handoff` (repo-relative POSIX path to the
     held baton) onto the plan named by that baton's own `governing_plan`.
  2. a plan already naming a DIFFERENT owning baton is overwritten
     (last-writer-wins), never raised -- CONTESTED-OWNERSHIP QUESTION: CLOSED,
     detect-and-warn, non-blocking (predecessor handoff's own "Contested-plan
     behaviour" section, ratified by the PM).
  3. a converged write (the plan already names the SAME held baton) is a
     silent no-op -- no mtime churn, no warning.
  4. a standalone session (no held claim) or a held baton with no
     `governing_plan` of its own has nothing to stamp and is a silent no-op.
  5. `_compensate_d5_release_claim`'s own reclaim path drives the stamp end
     to end (integration, not just the unit-level helper).
  6. `claim_plan(..., for_execution=True)` -- the PRODUCTION happy path --
     drives the stamp end to end. A green unit-level helper proves nothing
     about whether anything calls it.

`_resolve_held_handoff_for_session` is reused (never re-derived) per the
dispatch brief -- monkeypatched here at its `coordinator_core.baton_assemble`
call site rather than re-exercising its own ledger machinery, which is
covered by that function's own test surface elsewhere.

Run: python3 -m pytest
coordinator_core/baton_assemble/tests/test_plan_owner_stamp.py -q
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.baton_assemble as ba
import coordinator_core.baton_assemble.apply as ba_apply
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.test_baton_assemble import _init_repo, _write_artifact

# `_stamp_plan_owner_back_edge` routes through `locked_rmw`, which shells out
# to real git (`git_common_dir`) to locate its lock sidecar -- needs a real
# repo, not merely a directory. Runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _fm_dict(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    return yaml.safe_load(split_frontmatter(text).fm_text)


def _patch_held_handoff(monkeypatch, held_rel: str | None):
    def _fake(root: Path, *, allow_standalone: bool = False):
        return (held_rel, [], False)

    monkeypatch.setattr(ba, "_resolve_held_handoff_for_session", _fake)


def test_claim_stamps_plan_owner(tmp_path, monkeypatch):
    """AC1: the plan named by the held baton's own `governing_plan` gets a
    `claimed_by_handoff` back-edge naming that baton."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    plan_rel = "docs/plans/2026-08-21-owned-plan.md"
    _write_artifact(repo / plan_rel, ["title: Owned plan"])
    held_rel = "state/handoffs/2026-08-21-holder.md"
    _write_artifact(
        repo / held_rel,
        ["kind: session-handoff", f"governing_plan: {plan_rel}"],
    )
    _patch_held_handoff(monkeypatch, held_rel)

    ba_apply._stamp_plan_owner_back_edge(repo)

    fm = _fm_dict(repo / plan_rel)
    assert fm["claimed_by_handoff"] == held_rel


def test_claim_stamp_is_idempotent(tmp_path, monkeypatch):
    """AC3: a plan already naming the SAME held baton as owner is a silent
    no-op -- no mtime churn, byte-identical file."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    plan_rel = "docs/plans/2026-08-21-owned-plan.md"
    held_rel = "state/handoffs/2026-08-21-holder.md"
    _write_artifact(
        repo / plan_rel, ["title: Owned plan", f"claimed_by_handoff: {held_rel}"]
    )
    _write_artifact(
        repo / held_rel,
        ["kind: session-handoff", f"governing_plan: {plan_rel}"],
    )
    _patch_held_handoff(monkeypatch, held_rel)

    before = (repo / plan_rel).read_text(encoding="utf-8")
    ba_apply._stamp_plan_owner_back_edge(repo)
    after = (repo / plan_rel).read_text(encoding="utf-8")
    assert after == before


def test_claim_overwrites_a_different_prior_owner_last_writer_wins(tmp_path, monkeypatch, capsys):
    """AC2: CONTESTED-OWNERSHIP QUESTION CLOSED -- a plan naming a DIFFERENT
    prior owning baton is overwritten (last-writer-wins), never raised, with
    a stderr warning naming both the old and new owner."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    plan_rel = "docs/plans/2026-08-21-contested-plan.md"
    old_owner = "state/handoffs/2026-08-21-old-owner.md"
    new_owner = "state/handoffs/2026-08-21-new-owner.md"
    _write_artifact(
        repo / plan_rel, ["title: Contested plan", f"claimed_by_handoff: {old_owner}"]
    )
    _write_artifact(
        repo / new_owner,
        ["kind: session-handoff", f"governing_plan: {plan_rel}"],
    )
    _patch_held_handoff(monkeypatch, new_owner)

    ba_apply._stamp_plan_owner_back_edge(repo)

    fm = _fm_dict(repo / plan_rel)
    assert fm["claimed_by_handoff"] == new_owner
    err = capsys.readouterr().err
    assert old_owner in err
    assert new_owner in err


def test_standalone_session_is_noop(tmp_path, monkeypatch):
    """AC4a: no held claim (a standalone session) has no baton to name as
    owner -- silent no-op, matching `resolve_owner_handoff_id`'s own
    zero-held-claims posture."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    plan_rel = "docs/plans/2026-08-21-untouched-plan.md"
    _write_artifact(repo / plan_rel, ["title: Untouched"])
    _patch_held_handoff(monkeypatch, None)

    before = (repo / plan_rel).read_text(encoding="utf-8")
    ba_apply._stamp_plan_owner_back_edge(repo)
    after = (repo / plan_rel).read_text(encoding="utf-8")
    assert after == before
    assert "claimed_by_handoff" not in after


def test_held_baton_with_no_governing_plan_is_noop(tmp_path, monkeypatch):
    """AC4b: a held baton carrying no `governing_plan` of its own has no plan
    to stamp an owner onto -- silent no-op."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    held_rel = "state/handoffs/2026-08-21-no-plan-holder.md"
    _write_artifact(repo / held_rel, ["kind: session-handoff"])
    _patch_held_handoff(monkeypatch, held_rel)

    # No plan file exists at all; the helper must not raise or create one.
    ba_apply._stamp_plan_owner_back_edge(repo)
    assert not (repo / "docs" / "plans").exists()


def test_release_reclaim_compensator_drives_the_stamp(tmp_path, monkeypatch):
    """AC5: `_compensate_d5_release_claim`'s own reclaim path calls through
    to the back-edge stamp end to end -- not merely the unit-level helper in
    isolation."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    plan_rel = "docs/plans/2026-08-21-compensator-plan"
    plan_slug = "2026-08-21-compensator-plan"
    _write_artifact(repo / f"{plan_rel}.md", ["title: Compensator plan"])
    held_rel = "state/handoffs/2026-08-21-compensator-holder.md"
    _write_artifact(
        repo / held_rel,
        ["kind: session-handoff", f"governing_plan: {plan_rel}.md"],
    )
    _patch_held_handoff(monkeypatch, held_rel)

    directive = {"id": "d5", "args": ["release-artifact", "plan", plan_slug]}
    ba_apply._compensate_d5_release_claim(directive, repo, None)

    fm = _fm_dict(repo / f"{plan_rel}.md")
    assert fm["claimed_by_handoff"] == held_rel


def test_for_execution_claim_stamps_plan_owner(tmp_path, monkeypatch):
    """AC6: the production happy path stamps the back-edge.

    `claim_plan(..., for_execution=True)` is `/execute-plan` Step 0's own
    call. Pinned end to end rather than at the helper, because the defect
    this closes was exactly a helper with green tests and no production
    caller.
    """
    from coordinator_core.session import claims as session_claims

    repo = tmp_path / "repo"
    _init_repo(repo)
    slug = "2026-08-21-executed-plan"
    plan_rel = f"docs/plans/{slug}.md"
    _write_artifact(repo / plan_rel, ["title: Executed plan", "status: draft"])
    held_rel = "state/handoffs/2026-08-21-executing-baton.md"
    _write_artifact(
        repo / held_rel,
        ["kind: session-handoff", f"governing_plan: {plan_rel}"],
    )
    _patch_held_handoff(monkeypatch, held_rel)

    assert (
        session_claims.claim_plan(slug, cwd=str(repo), for_execution=True) is True
    )

    fm = _fm_dict(repo / plan_rel)
    assert fm["claimed_by_handoff"] == held_rel
    assert fm["status"] == "executing"
