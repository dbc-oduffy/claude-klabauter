"""Tests for coordinator_core.session.claim_neighbours.

Plan: docs/plans/2026-08-16-trace-a-claim-back-to-its-session.md, chunk C1.

Every fixture is synthetic, built under ``tmp_path`` — no real git repo is
required for the ``claim_index``-lookup side (mirrors
``test_claim_index.py``'s own ``sessions_dir=str(tmp_path)`` convention).
Liveness is monkeypatched directly at ``claim_neighbours.liveness.session_live``
rather than built from real ``meta.json``/process state — this module's own
liveness contract (Layer 1/2/registry precedence) is ``liveness.py``'s test
surface, not this one's; here we only need to pin that a peer's
LIVE/DEAD/raising verdict is respected.
"""

import os

import pytest
import yaml

from coordinator_core.session import claim_index
from coordinator_core.session import claim_neighbours


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def _write_artifact(path, frontmatter: dict, body: str = "\n# body\n"):
    fm_text = yaml.safe_dump(frontmatter, default_flow_style=False, sort_keys=False)
    _write(path, f"---\n{fm_text}---\n{body}")


def _session_touched(base, sid, lines):
    _write(os.path.join(str(base), sid, "touched.txt"), "\n".join(lines) + "\n")


def _touch_line(verb, path, when="2026-08-16T10:00:00.000000Z"):
    return f"{verb} {when} {path}"


def _write_plan_claim(base, sid, slug):
    claim_dir = os.path.join(str(base), "plan-claims", slug)
    os.makedirs(claim_dir, exist_ok=True)
    _write(os.path.join(claim_dir, "session_id"), sid + "\n")


# ---------------------------------------------------------------------------
# AC1 — plan claim with scope: resolves and names a live sister session
# ---------------------------------------------------------------------------


def test_plan_with_scope_resolves_live_neighbour(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    repo = tmp_path / "repo"
    plan_path = repo / "docs" / "plans" / "my-plan.md"
    _write_artifact(
        str(plan_path),
        {"title": "my plan", "scope": ["coordinator_core/session/claims.py"]},
    )

    _session_touched(sessions, "peer-sid", [_touch_line("T", "coordinator_core/session/claims.py")])

    monkeypatch.setattr(
        claim_neighbours.liveness, "session_live", lambda sid, cwd=None: sid == "peer-sid"
    )

    result = claim_neighbours.find_neighbours(
        str(plan_path),
        caller_sid="my-own-sid",
        sessions_dir=str(sessions),
    )

    assert result.status == claim_neighbours.RESOLVED
    assert result.file_set == ["coordinator_core/session/claims.py"]
    assert len(result.neighbours) == 1
    neighbour = result.neighbours[0]
    assert neighbour.session_id == "peer-sid"
    assert neighbour.overlapping_paths == ["coordinator_core/session/claims.py"]


# ---------------------------------------------------------------------------
# AC2 — handoff resolves via the deliverable_id bridge
# ---------------------------------------------------------------------------


def test_handoff_resolves_via_deliverable_bridge(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    repo = tmp_path / "repo"
    plan_path = repo / "docs" / "plans" / "bridge-plan.md"
    _write_artifact(
        str(plan_path),
        {
            "title": "bridge plan",
            "deliverable_id": "dlv-shared-thing-abc123",
            "scope": ["coordinator_core/session/claim_neighbours.py"],
        },
    )
    handoff_path = repo / "state" / "handoffs" / "some-handoff.md"
    _write_artifact(
        str(handoff_path),
        {"title": "a handoff", "deliverable_id": "dlv-shared-thing-abc123"},
    )

    _session_touched(
        sessions, "peer-sid", [_touch_line("T", "coordinator_core/session/claim_neighbours.py")]
    )
    monkeypatch.setattr(
        claim_neighbours.liveness, "session_live", lambda sid, cwd=None: sid == "peer-sid"
    )

    result = claim_neighbours.find_neighbours(
        str(handoff_path),
        caller_sid="my-own-sid",
        cwd=str(repo),
        sessions_dir=str(sessions),
    )

    assert result.status == claim_neighbours.RESOLVED
    assert result.file_set == ["coordinator_core/session/claim_neighbours.py"]
    assert [n.session_id for n in result.neighbours] == ["peer-sid"]


def test_handoff_with_no_matching_plan_is_unresolvable(tmp_path):
    repo = tmp_path / "repo"
    handoff_path = repo / "state" / "handoffs" / "orphan-handoff.md"
    _write_artifact(
        str(handoff_path),
        {"title": "orphan", "deliverable_id": "dlv-nobody-claims-this-999999"},
    )
    # No docs/plans dir at all under repo.

    result = claim_neighbours.find_neighbours(
        str(handoff_path), caller_sid="my-own-sid", cwd=str(repo)
    )

    assert result.status == claim_neighbours.UNRESOLVABLE
    assert result.neighbours == []
    assert result.reason is not None


# ---------------------------------------------------------------------------
# AC2 (explicit) — UNRESOLVABLE is structurally distinct from "resolved,
# zero neighbours" — never inferred from an empty list
# ---------------------------------------------------------------------------


def test_unresolvable_is_not_the_same_as_resolved_empty(tmp_path):
    repo = tmp_path / "repo"
    artifact_path = repo / "docs" / "plans" / "no-scope-no-bridge.md"
    _write_artifact(str(artifact_path), {"title": "carries neither field"})

    unresolvable = claim_neighbours.find_neighbours(
        str(artifact_path), caller_sid="my-own-sid", cwd=str(repo)
    )

    resolved_empty_path = repo / "docs" / "plans" / "explicit-empty-scope.md"
    _write_artifact(str(resolved_empty_path), {"title": "explicit empty scope", "scope": []})
    resolved_empty = claim_neighbours.find_neighbours(
        str(resolved_empty_path), caller_sid="my-own-sid", cwd=str(repo)
    )

    assert unresolvable.status == claim_neighbours.UNRESOLVABLE
    assert resolved_empty.status == claim_neighbours.RESOLVED
    assert unresolvable.status != resolved_empty.status
    # Both happen to carry an empty neighbours list — the discriminator MUST
    # be `.status`, never list emptiness.
    assert unresolvable.neighbours == [] and resolved_empty.neighbours == []


# ---------------------------------------------------------------------------
# AC4 — caller's own session excluded
# ---------------------------------------------------------------------------


def test_callers_own_session_excluded(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    repo = tmp_path / "repo"
    plan_path = repo / "docs" / "plans" / "self-only.md"
    _write_artifact(str(plan_path), {"title": "x", "scope": ["some/file.py"]})

    _session_touched(sessions, "my-own-sid", [_touch_line("T", "some/file.py")])
    monkeypatch.setattr(claim_neighbours.liveness, "session_live", lambda sid, cwd=None: True)

    result = claim_neighbours.find_neighbours(
        str(plan_path), caller_sid="my-own-sid", sessions_dir=str(sessions)
    )

    assert result.status == claim_neighbours.RESOLVED
    assert result.neighbours == []
    assert result.reason is None


# ---------------------------------------------------------------------------
# AC5 — dead claimant excluded
# ---------------------------------------------------------------------------


def test_dead_claimant_excluded(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    repo = tmp_path / "repo"
    plan_path = repo / "docs" / "plans" / "dead-peer.md"
    _write_artifact(str(plan_path), {"title": "x", "scope": ["some/file.py"]})

    _session_touched(sessions, "dead-sid", [_touch_line("T", "some/file.py")])
    monkeypatch.setattr(claim_neighbours.liveness, "session_live", lambda sid, cwd=None: False)

    result = claim_neighbours.find_neighbours(
        str(plan_path), caller_sid="my-own-sid", sessions_dir=str(sessions)
    )

    assert result.status == claim_neighbours.RESOLVED
    assert result.neighbours == []


# ---------------------------------------------------------------------------
# AC4 — a raising claim_index degrades rather than propagates
# ---------------------------------------------------------------------------


def test_raising_claim_index_degrades_not_raises(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    plan_path = repo / "docs" / "plans" / "raises.md"
    _write_artifact(str(plan_path), {"title": "x", "scope": ["some/file.py"]})

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated claim_index failure")

    monkeypatch.setattr(claim_neighbours.claim_index, "lookup", _boom)

    result = claim_neighbours.find_neighbours(
        str(plan_path), caller_sid="my-own-sid", sessions_dir=str(tmp_path / "sessions")
    )

    assert result.status == claim_neighbours.RESOLVED
    assert result.file_set == ["some/file.py"]
    assert result.neighbours == []
    assert result.reason is not None and "claim_index" in result.reason


def test_raising_liveness_check_skips_that_candidate(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    repo = tmp_path / "repo"
    plan_path = repo / "docs" / "plans" / "flaky-liveness.md"
    _write_artifact(str(plan_path), {"title": "x", "scope": ["some/file.py"]})

    _session_touched(sessions, "flaky-sid", [_touch_line("T", "some/file.py")])

    def _raise(sid, cwd=None):
        raise RuntimeError("simulated liveness failure")

    monkeypatch.setattr(claim_neighbours.liveness, "session_live", _raise)

    result = claim_neighbours.find_neighbours(
        str(plan_path), caller_sid="my-own-sid", sessions_dir=str(sessions)
    )

    assert result.status == claim_neighbours.RESOLVED
    assert result.neighbours == []


# ---------------------------------------------------------------------------
# Peer artifact resolution — neighbour carries the peer's own claimed plan
# ---------------------------------------------------------------------------


def test_neighbour_carries_peers_claimed_plan(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    repo = tmp_path / "repo"
    plan_path = repo / "docs" / "plans" / "mine.md"
    _write_artifact(str(plan_path), {"title": "x", "scope": ["some/file.py"]})

    _session_touched(sessions, "peer-sid", [_touch_line("T", "some/file.py")])
    _write_plan_claim(sessions, "peer-sid", "peers-own-plan")
    monkeypatch.setattr(
        claim_neighbours.liveness, "session_live", lambda sid, cwd=None: sid == "peer-sid"
    )

    result = claim_neighbours.find_neighbours(
        str(plan_path), caller_sid="my-own-sid", sessions_dir=str(sessions)
    )

    assert result.neighbours[0].artifact_path == "docs/plans/peers-own-plan.md"


def test_neighbour_with_no_artifact_claim_reports_none(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    repo = tmp_path / "repo"
    plan_path = repo / "docs" / "plans" / "mine2.md"
    _write_artifact(str(plan_path), {"title": "x", "scope": ["some/file.py"]})

    _session_touched(sessions, "peer-sid", [_touch_line("T", "some/file.py")])
    monkeypatch.setattr(
        claim_neighbours.liveness, "session_live", lambda sid, cwd=None: sid == "peer-sid"
    )

    result = claim_neighbours.find_neighbours(
        str(plan_path), caller_sid="my-own-sid", sessions_dir=str(sessions)
    )

    assert result.neighbours[0].artifact_path is None


# ---------------------------------------------------------------------------
# find_neighbours_for_paths — the public bare-path-set seam. C4's CLI
# reaches this same join through here (no more `_sid_to_artifact_map`
# reach-through); these tests pin the seam's OWN contract directly, not
# just observed through either of its two callers.
# ---------------------------------------------------------------------------


def test_paths_seam_returns_live_peer_for_claimed_path(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    _session_touched(sessions, "peer-sid", [_touch_line("T", "some/file.py")])
    monkeypatch.setattr(
        claim_neighbours.liveness, "session_live", lambda sid, cwd=None: sid == "peer-sid"
    )

    result = claim_neighbours.find_neighbours_for_paths(
        ["some/file.py"], caller_sid="my-own-sid", sessions_dir=str(sessions)
    )

    assert [n.session_id for n in result.neighbours] == ["peer-sid"]
    assert result.neighbours[0].overlapping_paths == ["some/file.py"]
    assert result.no_claimant_paths == []
    assert result.unanswerable_paths == []
    assert result.lookup_raised is None
    assert result.abort_cause is None


def test_paths_seam_excludes_callers_own_session(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    _session_touched(sessions, "my-own-sid", [_touch_line("T", "some/file.py")])
    monkeypatch.setattr(claim_neighbours.liveness, "session_live", lambda sid, cwd=None: True)

    result = claim_neighbours.find_neighbours_for_paths(
        ["some/file.py"], caller_sid="my-own-sid", sessions_dir=str(sessions)
    )

    assert result.neighbours == []
    assert result.no_claimant_paths == ["some/file.py"]


def test_paths_seam_excludes_dead_claimant(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    _session_touched(sessions, "dead-sid", [_touch_line("T", "some/file.py")])
    monkeypatch.setattr(claim_neighbours.liveness, "session_live", lambda sid, cwd=None: False)

    result = claim_neighbours.find_neighbours_for_paths(
        ["some/file.py"], caller_sid="my-own-sid", sessions_dir=str(sessions)
    )

    assert result.neighbours == []
    assert result.no_claimant_paths == ["some/file.py"]
    assert result.unanswerable_paths == []


def test_paths_seam_lookup_raised_when_claim_index_raises(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("simulated claim_index failure")

    monkeypatch.setattr(claim_neighbours.claim_index, "lookup", _boom)

    result = claim_neighbours.find_neighbours_for_paths(
        ["some/file.py"], caller_sid="my-own-sid", sessions_dir=str(tmp_path / "sessions")
    )

    assert result.neighbours == []
    assert result.no_claimant_paths == []
    assert result.unanswerable_paths == ["some/file.py"]
    assert result.lookup_raised is not None and "claim_index" in result.lookup_raised
    assert result.abort_cause is None


def test_paths_seam_abort_cause_distinct_from_lookup_raised_on_incomplete_walk(tmp_path):
    """Drives a REAL (not monkeypatched) incomplete-walk-without-raising:
    an empty ``sessions_dir`` and a ``cwd`` outside any git repo both
    resolve to no resolvable base, which ``claim_index.lookup()`` answers
    with ``UNANSWERABLE``/``abort_cause=ABORT_CAUSE_EMPTY_BASE`` rather
    than raising (see claim_index.py's ``_resolve_base``/``lookup``). This
    pins that ``abort_cause`` and ``lookup_raised`` are populated by
    genuinely DIFFERENT code paths, not the same one under two names."""
    result = claim_neighbours.find_neighbours_for_paths(
        ["some/file.py"],
        caller_sid="my-own-sid",
        cwd=str(tmp_path),
        sessions_dir="",
    )

    assert result.neighbours == []
    assert result.no_claimant_paths == []
    assert result.unanswerable_paths == ["some/file.py"]
    assert result.lookup_raised is None
    assert result.abort_cause == claim_neighbours.claim_index.ABORT_CAUSE_EMPTY_BASE


def test_paths_seam_no_claimant_distinct_from_unanswerable(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    _session_touched(sessions, "dead-sid", [_touch_line("T", "answerable/file.py")])
    monkeypatch.setattr(claim_neighbours.liveness, "session_live", lambda sid, cwd=None: False)

    real_lookup = claim_neighbours.claim_index.lookup

    def _partial_unanswerable(paths, sessions_dir=None, cwd=None):
        result = real_lookup(paths, sessions_dir=sessions_dir, cwd=cwd)
        result["unanswerable/file.py"] = [claim_neighbours.claim_index.UNANSWERABLE]
        result.complete = False
        result.abort_cause = claim_neighbours.claim_index.ABORT_CAUSE_CAP_EXCEEDED
        return result

    monkeypatch.setattr(claim_neighbours.claim_index, "lookup", _partial_unanswerable)

    result = claim_neighbours.find_neighbours_for_paths(
        ["answerable/file.py", "unanswerable/file.py"],
        caller_sid="my-own-sid",
        sessions_dir=str(sessions),
    )

    assert result.no_claimant_paths == ["answerable/file.py"]
    assert result.unanswerable_paths == ["unanswerable/file.py"]
    assert result.lookup_raised is None
    assert result.abort_cause == claim_neighbours.claim_index.ABORT_CAUSE_CAP_EXCEEDED
