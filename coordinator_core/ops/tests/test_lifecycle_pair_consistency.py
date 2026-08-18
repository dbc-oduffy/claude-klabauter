"""
coordinator_core.ops.tests.test_lifecycle_pair_consistency — C7: one op owns
the lifecycle pair it writes.

Purpose: AC12 — after any of the lifecycle-writing ops touched by this
chunk, the fields that pair with `deployment_state` are left mutually
consistent, never half-written. Establishing WHICH field pairs with
`deployment_state` is itself the finding this chunk reports (see the run
report): `status` cannot be that field — `coordinator_core/frontmatter/
schemas/handoff.schema.json` narrows its live enum to `open`/`claimed` only
(DR-084 P4 narrow, `docs/plans/2026-07-22-handoff-lifecycle-vocabulary-
overhaul-scope.md`), a decision `handoff_transition.py` documents as
deliberate at every site that leaves `status` untouched ("status is
untouched — close is a deployment_state-only terminal stamp ... DR-084
does not couple status to closed"), and this plan's OWN Anti-scope names
`deployment_state: shipped` + non-terminal `status` a "benign twin" rather
than damage. `DR-207` DD#5 ("Design Decision #5 — Derivable lifecycle
status") governs a DIFFERENT, cockpit-emitted field (`HandoffSummary`'s
derived `proposed|planned|in-progress|in-review|shipped|abandoned` enum,
never authored in frontmatter) — not this one; it does not apply here.

`pickup_ready` DOES pair with `deployment_state` (schema: "Positive
pickup-authorized signal") and carries no such constraint. `_close` already
clears it on a terminal write (2026-08-10 fix, cross-repo/inbox/
2026-08-10-doe-claude-em-reconcile-close-terminal-and-scrub-key.md § 1 —
"The two fields are one logical state"). `build_ship_mutate` never did —
this chunk's actual fix. These tests assert THAT pair — `deployment_state`
and `pickup_ready` — after ship (`_ship` and, through it,
`deliverable_cascade._advance_one`) and after close (`_close`); and assert
that `status` is left untouched (the documented, deliberate contract) by
both, never silently coupled by this fix.

Spec backlink: docs/plans/2026-08-18-a-spinoff-is-not-its-parents-deliverable.md § C7 (AC12)

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_lifecycle_pair_consistency.py -q
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Optional

import pytest

import coordinator_core.ops.deliverable_cascade as cascade_mod
import coordinator_core.ops.handoff_archive_transition as hat
import coordinator_core.ops.handoff_transition as ht
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter

# Declared, not excused: this file spawns a real `git` process because
# locked_rmw (the write path every verb here routes through) resolves the
# git common dir via a real `git rev-parse` call, and the cascade's ship
# leg additionally resolves shipped_in evidence against a real commit — no
# fixture stands in for either. Spawn ratchet:
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_handler = ht._handler
_cascade_handler = cascade_mod._handler

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}

_TEST_SID = "22222222-2222-2222-2222-222222222222"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        timeout=15,
        stdin=subprocess.DEVNULL,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _run(params: dict, repo_root: Path) -> dict:
    return asyncio.run(_handler(params, repo_root=repo_root))


def _seed_handoff(
    repo: Path,
    name: str,
    *,
    deliverable_id: str,
    deployment_state: str = "in_flight",
    status: str = "claimed",
    pickup_ready: Optional[bool] = True,
    created: str = "2026-01-01",
    scope: Optional[list] = None,
    commit: bool = False,
) -> Path:
    # created stays pre-2026-05-29 throughout so _cf_shipped_in_required
    # never fires — this file's concern is the pickup_ready/deployment_state
    # pair, not shipped_in resolution (covered elsewhere, e.g.
    # test_deliverable_cascade.py's AC10 fixture).
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        f"created: {created}\n"
        "branch: work/test/2026-01-01\n"
        f"status: {status}\n"
        'predecessor: "none"\n'
        f"deployment_state: {deployment_state}\n"
        f"claimed_at: 2026-01-01T00:00:00Z\n"
        f'claimed_by: "{_TEST_SID}"\n'
        f"deliverable_id: {deliverable_id}\n"
    )
    if pickup_ready is not None:
        fm += f"pickup_ready: {'true' if pickup_ready else 'false'}\n"
    if scope is not None:
        fm += "scope:\n" + "".join(f"  - {p}\n" for p in scope)
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    if commit:
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-m", f"add {name}")
    return path


def _fm_field(path: Path, key: str) -> Optional[str]:
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    assert split is not None
    return read_fm_field(split.fm_text, key)


# ---------------------------------------------------------------------------
# AC12 — ship (`_ship`, via handoff_transition's own "ship" verb)
# ---------------------------------------------------------------------------


def test_ship_clears_pickup_ready_and_leaves_status_untouched(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_handoff(
        repo,
        "20260101-ship.md",
        deliverable_id="dlv-ship-pair-000000",
        deployment_state="in_flight",
        status="claimed",
        pickup_ready=True,
    )

    result = _run(
        {"verb": "ship", "handoff_path": str(handoff)},
        repo_root=repo / ".git",
    )

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert _fm_field(handoff, "deployment_state") == "shipped"
    assert _fm_field(handoff, "pickup_ready") == "false"
    # status is deliberately untouched — DR-084 P4 narrow decouples it from
    # terminality; it is NOT the field this chunk co-owns with
    # deployment_state (see module docstring).
    assert _fm_field(handoff, "status") == "claimed"


def test_ship_idempotent_no_op_only_at_full_target_state(tmp_path):
    """A record already at deployment_state:shipped but with a stale
    pickup_ready:true is NOT treated as already-converged — mirrors
    _close's own three-condition idempotency fix (2026-08-10)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_handoff(
        repo,
        "20260101-ship-stale.md",
        deliverable_id="dlv-ship-pair-stale-000000",
        deployment_state="shipped",
        status="claimed",
        pickup_ready=True,
    )

    result = _run(
        {"verb": "ship", "handoff_path": str(handoff)},
        repo_root=repo / ".git",
    )

    assert result["exit_code"] == 0, result
    assert result["applied"] is True, "a stale pickup_ready:true must still be written"
    assert _fm_field(handoff, "deployment_state") == "shipped"
    assert _fm_field(handoff, "pickup_ready") == "false"

    # A second call against the now-fully-converged record is a genuine no-op.
    result2 = _run(
        {"verb": "ship", "handoff_path": str(handoff)},
        repo_root=repo / ".git",
    )
    assert result2["exit_code"] == 0, result2
    assert result2["applied"] is False


# ---------------------------------------------------------------------------
# AC12 — close (`_close`) — already-fixed pair, asserted here for the full
# three-op inventory AC12 names, not just the ops this chunk edits.
# ---------------------------------------------------------------------------


def test_close_clears_pickup_ready_and_leaves_status_untouched(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_handoff(
        repo,
        "20260101-close.md",
        deliverable_id="dlv-close-pair-000000",
        deployment_state="ready_to_fire",
        status="open",
        pickup_ready=True,
    )

    result = _run(
        {"verb": "close", "handoff_path": str(handoff), "reason": "stale"},
        repo_root=repo / ".git",
    )

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert _fm_field(handoff, "deployment_state") == "closed"
    assert _fm_field(handoff, "pickup_ready") == "false"
    assert _fm_field(handoff, "status") == "open"


# ---------------------------------------------------------------------------
# AC12 — deliverable_cascade._advance_one composes build_ship_mutate, so the
# fix flows through the cascade's own ship leg without a second write site.
# ---------------------------------------------------------------------------


def test_cascade_advance_clears_pickup_ready_on_ship_leg(tmp_path, monkeypatch):
    session_id = "66666666-6666-6666-6666-666666666666"
    monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    repo = tmp_path / "repo"
    _init_repo(repo)
    scoped = repo / "feature.txt"
    scoped.write_text("feature body\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(
        repo, "commit", "-m",
        f"implement the feature this handoff scopes\n\nSession-Id: {session_id}",
    )

    deliverable_id = "dlv-cascade-pair-000000"
    handoff = _seed_handoff(
        repo,
        "20260101-cascade-ship.md",
        deliverable_id=deliverable_id,
        deployment_state="ready_to_fire",
        status="claimed",
        pickup_ready=True,
        scope=["feature.txt"],
        commit=True,
    )

    result = asyncio.run(
        _cascade_handler(
            {
                "deliverable_id": deliverable_id,
                "source_kind": "plan",
                "source_path": "docs/plans/dummy.md",
            },
            repo_root=repo / ".git",
        )
    )

    assert result["exit_code"] == 0, result
    assert len(result["advanced"]) == 1, result
    assert _fm_field(handoff, "deployment_state") == "shipped"
    assert _fm_field(handoff, "pickup_ready") == "false"
    assert _fm_field(handoff, "status") == "claimed"


# ---------------------------------------------------------------------------
# supersede (`handoff_archive_transition._supersede_continued`) — the third
# and last terminal-write leg. `continued` is terminal exactly as `shipped`
# and `closed` are, but this leg never cleared `pickup_ready`: the 2026-08-10
# close fix and the ship fix each closed their own path and left this one
# open. Asserted here rather than in a new file so the pair property is
# stated once, over the full terminal-verb inventory.
# ---------------------------------------------------------------------------


def test_supersede_clears_pickup_ready_and_leaves_status_claimed(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_handoff(
        repo,
        "20260101-supersede.md",
        deliverable_id="dlv-supersede-pair-000000",
        deployment_state="in_flight",
        status="claimed",
        pickup_ready=True,
    )

    result = hat._supersede_continued(
        handoff, "state/handoffs/20260101-successor.md", repo / ".git"
    )

    assert result["exit_code"] == 0, result
    assert _fm_field(handoff, "deployment_state") == "continued"
    assert _fm_field(handoff, "pickup_ready") == "false"
    # status stays claimed: the DR-084 P4 narrow admits only open/claimed, so
    # terminality is never carried there — same contract ship and close hold.
    assert _fm_field(handoff, "status") == "claimed"
    assert (
        _fm_field(handoff, "continued_into") == "state/handoffs/20260101-successor.md"
    )


def test_supersede_idempotent_no_op_only_at_full_target_state(tmp_path):
    """A record already claimed+continued into the SAME successor but with a
    stale pickup_ready:true is not already-converged — mirrors ship's and
    close's own idempotency fixes. Without this, every pre-fix superseded
    baton would short-circuit on the succession edge alone and never pick up
    the clear."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    successor = "state/handoffs/20260101-successor.md"
    handoff = _seed_handoff(
        repo,
        "20260101-supersede-stale.md",
        deliverable_id="dlv-supersede-pair-stale-000000",
        deployment_state="continued",
        status="claimed",
        pickup_ready=True,
    )
    # Seed the succession edge the pre-fix writer would have left behind.
    text = handoff.read_text(encoding="utf-8")
    handoff.write_text(
        text.replace(
            "deployment_state: continued\n",
            f"deployment_state: continued\ncontinued_into: {successor}\n",
        ),
        encoding="utf-8",
    )

    result = hat._supersede_continued(handoff, successor, repo / ".git")

    assert result["exit_code"] == 0, result
    assert _fm_field(handoff, "pickup_ready") == "false", (
        "a stale pickup_ready:true must still be written on a re-supersede"
    )

    # A second call against the now-converged record is a genuine no-op.
    result2 = hat._supersede_continued(handoff, successor, repo / ".git")
    assert result2["exit_code"] == 0, result2
    assert "no-op" in (result2.get("message") or "")


def test_supersede_conflicting_successor_still_refuses(tmp_path):
    """The pickup_ready fix must not weaken the conflict refusal: a different
    continued_into is still a hard abort, not a silent overwrite."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_handoff(
        repo,
        "20260101-supersede-conflict.md",
        deliverable_id="dlv-supersede-pair-conflict-000000",
        deployment_state="continued",
        status="claimed",
        pickup_ready=False,
    )
    text = handoff.read_text(encoding="utf-8")
    handoff.write_text(
        text.replace(
            "deployment_state: continued\n",
            "deployment_state: continued\ncontinued_into: state/handoffs/first.md\n",
        ),
        encoding="utf-8",
    )

    result = hat._supersede_continued(
        handoff, "state/handoffs/second.md", repo / ".git"
    )

    assert result["exit_code"] != 0, result
    assert _fm_field(handoff, "continued_into") == "state/handoffs/first.md"


# ---------------------------------------------------------------------------
# supersede, sibling site (`handoff_transition`'s own supersede verb). There
# are TWO supersede implementations — this one and
# handoff_archive_transition._supersede_continued — both writing the same
# pair. Asserted here so the pair cannot be fixed on one and left open on the
# other, which is exactly how this defect survived three separate fixes.
# ---------------------------------------------------------------------------


def test_transition_supersede_clears_pickup_ready(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_handoff(
        repo,
        "20260101-ht-supersede.md",
        deliverable_id="dlv-ht-supersede-pair-000000",
        deployment_state="in_flight",
        status="claimed",
        pickup_ready=True,
    )

    result = _run(
        {
            "verb": "supersede",
            "handoff_path": str(handoff),
            "continued_into": "state/handoffs/20260101-successor.md",
        },
        repo_root=repo / ".git",
    )

    assert result["exit_code"] == 0, result
    assert _fm_field(handoff, "deployment_state") == "continued"
    assert _fm_field(handoff, "pickup_ready") == "false"
    assert _fm_field(handoff, "status") == "claimed"


def test_transition_supersede_idempotent_no_op_only_at_full_target_state(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    successor = "state/handoffs/20260101-successor.md"
    handoff = _seed_handoff(
        repo,
        "20260101-ht-supersede-stale.md",
        deliverable_id="dlv-ht-supersede-stale-000000",
        deployment_state="continued",
        status="claimed",
        pickup_ready=True,
    )
    text = handoff.read_text(encoding="utf-8")
    handoff.write_text(
        text.replace(
            "deployment_state: continued\n",
            f"deployment_state: continued\ncontinued_into: {successor}\n",
        ),
        encoding="utf-8",
    )

    result = _run(
        {
            "verb": "supersede",
            "handoff_path": str(handoff),
            "continued_into": successor,
        },
        repo_root=repo / ".git",
    )

    assert result["exit_code"] == 0, result
    assert _fm_field(handoff, "pickup_ready") == "false", (
        "a stale pickup_ready:true must still be written on a re-supersede"
    )

    result2 = _run(
        {
            "verb": "supersede",
            "handoff_path": str(handoff),
            "continued_into": successor,
        },
        repo_root=repo / ".git",
    )
    assert result2["exit_code"] == 0, result2
    assert result2["applied"] is False
