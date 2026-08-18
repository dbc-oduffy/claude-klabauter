"""
coordinator_core.ops.tests.test_handoff_close_origin_stub

Tests for `handoff.close_origin_stub`'s guard-decline reporting fidelity —
`_try_close`'s skip payload when the live-children guard declines. No git
fixture is used: the guard call (`_live_children_guard`) is monkeypatched
directly so these tests never spawn a subprocess (mirrors this test
package's `test_handoff_children.py` idiom of tmp_path + monkeypatch over a
real git repo, where the module under test does not itself require git
plumbing to exercise).

Coverage:
  (a) exit_code 0 (has live children) -> reason "guard-declined-live-children",
      `blocking_children` rendered worktree-relative (not the guard's
      absolute paths), `guard_error` None.
  (b) exit_code 2 (indeterminate/fail-closed) -> reason
      "guard-declined-indeterminate", `guard_error` carried through.
  (c) A complete, stub-matching delivery proof closes the stub WITHOUT
      consulting the live-children guard at all (`close_basis` ==
      "delivery-proof"); a guard-fallback close (no/incomplete/mismatched
      proof) still closes when the guard reads safe, with `close_basis` ==
      "guard".
  (d) A proof for a MISMATCHED `deliverable_id` never closes on the proof --
      falls back to the guard.
  (e) An incomplete proof (non-"joined" `join_provenance`, or non-empty
      `missing_chunk_ids`) never closes on the proof -- falls back to the
      guard.

Spec backlink: this chunk's dispatch brief (reporting-fidelity fix for
`_try_close`'s guard-decline collapse, 2026-08-13; delivery-proof origin-stub
close, 2026-08-13).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import coordinator_core.ops.handoff_close_origin_stub as m


def _run(coro):
    return asyncio.run(coro)


def _fake_git_common_dir(worktree: Path, monkeypatch) -> None:
    """Let the close-path tests (which reach `_ship`/`_stamp_handler`, which
    resolve a lock directory via `coordinator_core.lifecycle.git_common_dir`
    and re-derive the worktree root via `main_worktree_root`) run without a
    real git repository or subprocess spawn -- monkeypatches the lru_cache'd
    resolver directly, mirroring this module's own no-git-fixture idiom for
    every other test in this file. A bare `.git` marker directory (never
    actually used as a git repo) lets `main_worktree_root(worktree)` accept
    `worktree` itself as an already-resolved worktree root, per that
    function's own "ERGONOMIC WIDENING" arm."""
    import coordinator_core.lifecycle as lifecycle

    (worktree / ".git").mkdir(parents=True, exist_ok=True)
    lock_root = worktree / ".fake-git-common"
    lock_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(lifecycle, "git_common_dir", lambda repo_root: lock_root)


def _seed_stub(worktree: Path, deliverable_id: str | None = None) -> Path:
    """Seed a schema-valid `kind: roadmap-baton` origin stub (`_is_baton_kind`
    admits it; `roadmap_id`/`stub_id` are only schema-legal on this kind,
    unlike `spinoff`) -- full-record shape only matters for the tests that
    reach `_ship` (schema-validated); the guard-decline tests above never
    reach validation and would pass with a minimal record too."""
    stub = worktree / "state" / "handoffs" / "origin-stub.md"
    stub.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        "kind: roadmap-baton\n"
        "title: Origin stub\n"
        "created: 2026-08-13\n"
        "branch: main\n"
        "status: open\n"
        "predecessor: none\n"
        "category: infra\n"
        "summary: origin stub fixture for close-origin-stub tests\n"
        "roadmap_id: r1\n"
        "stub_id: s1\n"
        "wave: 1\n"
        "blocks: []\n"
        "blocked_by: []\n"
        "deployment_state: ready_to_fire\n"
    )
    if deliverable_id is not None:
        fm += f"deliverable_id: {deliverable_id}\n"
    stub.write_text(f"---\n{fm}---\n\nBody.\n", encoding="utf-8")
    return stub


def _complete_proof(deliverable_id: str = "dlv-d1") -> dict:
    return {
        "deliverable_id": deliverable_id,
        "join_provenance": "joined",
        "missing_chunk_ids": [],
        "status": "implemented",
        # Finding 0 (staff-eng review 2026-08-13): a proof is only complete
        # when the plan's spine actually had at least one commit-required
        # row for the join to have run against.
        "commit_required_chunk_count": 1,
    }


def test_try_close_reports_live_children_distinct_reason_and_relative_paths(
    tmp_path, monkeypatch
):
    worktree = tmp_path
    stub_path = _seed_stub(worktree)
    blocker_abs = str(worktree / "state" / "handoffs" / "blocker.md")

    async def _fake_guard(params, repo_root):
        return {"exit_code": 0, "referenced": True, "children": [blocker_abs]}

    monkeypatch.setattr(m, "_live_children_guard", _fake_guard)

    closed, skipped = _run(
        m._try_close(
            stub_path, worktree, worktree, "r1", "s1", "direct", "", []
        )
    )

    assert closed is None
    assert skipped["reason"] == "guard-declined-live-children"
    assert skipped["blocking_children"] == ["state/handoffs/blocker.md"]
    assert skipped["guard_error"] is None


def test_try_close_reports_indeterminate_distinct_reason_and_guard_error(
    tmp_path, monkeypatch
):
    worktree = tmp_path
    stub_path = _seed_stub(worktree)

    async def _fake_guard(params, repo_root):
        return {"exit_code": 2, "error": "unscannable subtree: boom"}

    monkeypatch.setattr(m, "_live_children_guard", _fake_guard)

    closed, skipped = _run(
        m._try_close(
            stub_path, worktree, worktree, "r1", "s1", "direct", "", []
        )
    )

    assert closed is None
    assert skipped["reason"] == "guard-declined-indeterminate"
    assert skipped["blocking_children"] == []
    assert skipped["guard_error"] == "unscannable subtree: boom"


def test_is_complete_delivery_proof_accepts_only_the_full_condition_set():
    assert m._is_complete_delivery_proof(_complete_proof()) is True
    assert m._is_complete_delivery_proof(None) is False
    assert m._is_complete_delivery_proof({}) is False
    assert (
        m._is_complete_delivery_proof({**_complete_proof(), "join_provenance": "key_mismatch"})
        is False
    )
    assert (
        m._is_complete_delivery_proof({**_complete_proof(), "missing_chunk_ids": ["c1"]})
        is False
    )
    assert (
        m._is_complete_delivery_proof({**_complete_proof(), "status": "landed"})
        is False
    )
    assert (
        m._is_complete_delivery_proof({**_complete_proof(), "deliverable_id": ""})
        is False
    )


def test_try_close_complete_matching_proof_closes_without_consulting_guard(
    tmp_path, monkeypatch
):
    worktree = tmp_path
    stub_path = _seed_stub(worktree, deliverable_id="dlv-d1")
    _fake_git_common_dir(worktree, monkeypatch)

    async def _guard_must_not_be_called(params, repo_root):
        raise AssertionError("live-children guard must not be consulted under a complete, matching delivery proof")

    monkeypatch.setattr(m, "_live_children_guard", _guard_must_not_be_called)

    closed, skipped = _run(
        m._try_close(
            stub_path,
            worktree,
            worktree,
            "r1",
            "s1",
            "direct",
            "deadbeef1234",
            [],
            _complete_proof("dlv-d1"),
        )
    )

    assert skipped is None
    assert closed["close_basis"] == m.CLOSE_BASIS_DELIVERY_PROOF
    assert closed["stub_path"] == "state/handoffs/origin-stub.md"


def test_try_close_proof_deliverable_id_whitespace_padding_still_matches(
    tmp_path, monkeypatch
):
    """A proof's `deliverable_id` carrying leading/trailing whitespace still
    matches a clean stub `deliverable_id` -- pins the strip normalization at
    the "Review: staff-eng Finding 3" comment block (`_try_close`), which
    compares an already-stripped `_read_deliverable_id` return against a
    proof value that must be stripped too. Every other proof-matching test in
    this file uses already-clean `deliverable_id` values, so a future
    refactor that dropped that strip would pass the rest of the suite
    silently."""
    worktree = tmp_path
    stub_path = _seed_stub(worktree, deliverable_id="dlv-d1")
    _fake_git_common_dir(worktree, monkeypatch)

    async def _guard_must_not_be_called(params, repo_root):
        raise AssertionError("live-children guard must not be consulted under a complete, matching delivery proof")

    monkeypatch.setattr(m, "_live_children_guard", _guard_must_not_be_called)

    closed, skipped = _run(
        m._try_close(
            stub_path,
            worktree,
            worktree,
            "r1",
            "s1",
            "direct",
            "deadbeef1234",
            [],
            _complete_proof("  dlv-d1  "),
        )
    )

    assert skipped is None
    assert closed["close_basis"] == m.CLOSE_BASIS_DELIVERY_PROOF
    assert closed["stub_path"] == "state/handoffs/origin-stub.md"


def test_try_close_guard_fallback_close_reports_guard_basis(tmp_path, monkeypatch):
    worktree = tmp_path
    stub_path = _seed_stub(worktree)
    _fake_git_common_dir(worktree, monkeypatch)

    async def _fake_guard(params, repo_root):
        return {"exit_code": 1, "referenced": False, "children": []}

    monkeypatch.setattr(m, "_live_children_guard", _fake_guard)

    closed, skipped = _run(
        m._try_close(
            stub_path, worktree, worktree, "r1", "s1", "direct", "deadbeef1234", []
        )
    )

    assert skipped is None
    assert closed["close_basis"] == m.CLOSE_BASIS_GUARD


def test_try_close_proof_for_mismatched_deliverable_id_falls_back_to_guard(
    tmp_path, monkeypatch
):
    worktree = tmp_path
    stub_path = _seed_stub(worktree, deliverable_id="d1")

    async def _fake_guard(params, repo_root):
        return {"exit_code": 0, "referenced": True, "children": [str(worktree / "x.md")]}

    monkeypatch.setattr(m, "_live_children_guard", _fake_guard)

    closed, skipped = _run(
        m._try_close(
            stub_path, worktree, worktree, "r1", "s1", "direct", "", [], _complete_proof("d-other")
        )
    )

    assert closed is None
    assert skipped["reason"] == "guard-declined-live-children"


def test_try_close_incomplete_proof_falls_back_to_guard(tmp_path, monkeypatch):
    worktree = tmp_path
    stub_path = _seed_stub(worktree, deliverable_id="d1")

    async def _fake_guard(params, repo_root):
        return {"exit_code": 0, "referenced": True, "children": [str(worktree / "x.md")]}

    monkeypatch.setattr(m, "_live_children_guard", _fake_guard)

    incomplete = {**_complete_proof("d1"), "join_provenance": "key_mismatch"}
    closed, skipped = _run(
        m._try_close(
            stub_path, worktree, worktree, "r1", "s1", "direct", "", [], incomplete
        )
    )

    assert closed is None
    assert skipped["reason"] == "guard-declined-live-children"

    incomplete2 = {**_complete_proof("d1"), "missing_chunk_ids": ["c9"]}
    closed2, skipped2 = _run(
        m._try_close(
            stub_path, worktree, worktree, "r1", "s1", "direct", "", [], incomplete2
        )
    )

    assert closed2 is None
    assert skipped2["reason"] == "guard-declined-live-children"


def test_try_close_absent_proof_preserves_today_behaviour(tmp_path, monkeypatch):
    worktree = tmp_path
    stub_path = _seed_stub(worktree, deliverable_id="dlv-d1")
    _fake_git_common_dir(worktree, monkeypatch)

    async def _fake_guard(params, repo_root):
        return {"exit_code": 1, "referenced": False, "children": []}

    monkeypatch.setattr(m, "_live_children_guard", _fake_guard)

    closed, skipped = _run(
        m._try_close(
            stub_path, worktree, worktree, "r1", "s1", "direct", "deadbeef1234", [], None
        )
    )

    assert skipped is None
    assert closed["close_basis"] == m.CLOSE_BASIS_GUARD


# ---------------------------------------------------------------------------
# Review: staff-eng Finding 0 (2026-08-13, critical) — degenerate proof
# ---------------------------------------------------------------------------


def test_is_complete_delivery_proof_rejects_zero_commit_required_count():
    """A proof with `commit_required_chunk_count: 0` must NOT be complete —
    this is the exact shape `close_out_and_stamp` now builds for a plan whose
    spine has zero commit-required rows, the degenerate `_determine_shipped`
    branch (`if not chunk_ids:`) Finding 0 identified."""
    proof = {**_complete_proof(), "commit_required_chunk_count": 0}
    assert m._is_complete_delivery_proof(proof) is False


def test_is_complete_delivery_proof_rejects_missing_or_wrong_typed_count():
    assert (
        m._is_complete_delivery_proof(
            {k: v for k, v in _complete_proof().items() if k != "commit_required_chunk_count"}
        )
        is False
    )
    assert (
        m._is_complete_delivery_proof({**_complete_proof(), "commit_required_chunk_count": None})
        is False
    )
    assert (
        m._is_complete_delivery_proof({**_complete_proof(), "commit_required_chunk_count": "1"})
        is False
    )
    assert (
        m._is_complete_delivery_proof({**_complete_proof(), "commit_required_chunk_count": True})
        is False
    )


def test_try_close_zero_commit_required_proof_falls_back_to_guard(tmp_path, monkeypatch):
    worktree = tmp_path
    stub_path = _seed_stub(worktree, deliverable_id="dlv-d1")

    async def _fake_guard(params, repo_root):
        return {"exit_code": 0, "referenced": True, "children": [str(worktree / "x.md")]}

    monkeypatch.setattr(m, "_live_children_guard", _fake_guard)

    zero_count_proof = {**_complete_proof("dlv-d1"), "commit_required_chunk_count": 0}
    closed, skipped = _run(
        m._try_close(
            stub_path, worktree, worktree, "r1", "s1", "direct", "", [], zero_count_proof
        )
    )

    assert closed is None
    assert skipped["reason"] == "guard-declined-live-children"


# ---------------------------------------------------------------------------
# Review: staff-eng Finding 2 (test gaps)
# ---------------------------------------------------------------------------


def test_is_complete_delivery_proof_missing_key_vs_empty_list_for_missing_chunk_ids():
    """`missing_chunk_ids` absent is NOT the same claim as an explicitly
    empty list — an absent key must be treated as incomplete."""
    proof = {k: v for k, v in _complete_proof().items() if k != "missing_chunk_ids"}
    assert m._is_complete_delivery_proof(proof) is False


def test_try_close_stub_without_deliverable_id_falls_back_to_guard(tmp_path, monkeypatch):
    """A stub carrying NO `deliverable_id` at all, presented with an
    otherwise-complete proof, must fall back to the live-children guard —
    never close on the proof. This is the airtightness of the
    `stub_deliverable_id is not None` arm."""
    worktree = tmp_path
    stub_path = _seed_stub(worktree, deliverable_id=None)
    _fake_git_common_dir(worktree, monkeypatch)

    guard_called = False

    async def _fake_guard(params, repo_root):
        nonlocal guard_called
        guard_called = True
        return {"exit_code": 1, "referenced": False, "children": []}

    monkeypatch.setattr(m, "_live_children_guard", _fake_guard)

    closed, skipped = _run(
        m._try_close(
            stub_path,
            worktree,
            worktree,
            "r1",
            "s1",
            "direct",
            "deadbeef1234",
            [],
            _complete_proof("dlv-d1"),
        )
    )

    assert guard_called is True
    assert skipped is None
    assert closed["close_basis"] == m.CLOSE_BASIS_GUARD


def test_handler_coerces_non_dict_delivery_proof_safely(tmp_path, monkeypatch):
    """A non-dict `delivery_proof` at the handler boundary (string/list/int)
    must be coerced to None, not raise."""
    worktree = tmp_path
    (worktree / ".git").mkdir(parents=True, exist_ok=True)
    handoffs_dir = worktree / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)

    for bad_proof in ("not-a-dict", ["a", "list"], 42):
        result = _run(
            m._handler(
                {"handoff_path": "state/handoffs/nonexistent.md", "delivery_proof": bad_proof},
                worktree,
            )
        )
        assert result["exit_code"] == 1  # unresolvable handoff_path — reached without raising


def test_handler_multi_stub_fan_out_reruns_proof_check_per_stub(tmp_path, monkeypatch):
    """Multi-stub fan-out: one call resolving several (roadmap_id, stub_id)
    pairs via a plan's `closes_stubs` list must re-check the delivery proof
    PER STUB — only the stub whose own `deliverable_id` matches the proof
    closes on the proof; the other falls back to the (safe-reading) guard."""
    worktree = tmp_path
    _fake_git_common_dir(worktree, monkeypatch)

    stub_a = worktree / "state" / "handoffs" / "stub-a.md"
    stub_a.parent.mkdir(parents=True, exist_ok=True)
    stub_a.write_text(
        "---\n"
        "kind: roadmap-baton\n"
        "title: Stub A\n"
        "created: 2026-08-13\n"
        "branch: main\n"
        "status: open\n"
        "predecessor: none\n"
        "category: infra\n"
        "summary: fan-out fixture A\n"
        "roadmap_id: rA\n"
        "stub_id: sA\n"
        "wave: 1\n"
        "blocks: []\n"
        "blocked_by: []\n"
        "deployment_state: ready_to_fire\n"
        "deliverable_id: dlv-a\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )
    stub_b = worktree / "state" / "handoffs" / "stub-b.md"
    stub_b.write_text(
        "---\n"
        "kind: roadmap-baton\n"
        "title: Stub B\n"
        "created: 2026-08-13\n"
        "branch: main\n"
        "status: open\n"
        "predecessor: none\n"
        "category: infra\n"
        "summary: fan-out fixture B\n"
        "roadmap_id: rB\n"
        "stub_id: sB\n"
        "wave: 1\n"
        "blocks: []\n"
        "blocked_by: []\n"
        "deployment_state: ready_to_fire\n"
        "deliverable_id: dlv-b\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )

    plans_dir = worktree / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    plan_path = plans_dir / "fan-out-plan.md"
    plan_path.write_text(
        "---\n"
        "closes_stubs:\n"
        "  - roadmap_id: rA\n"
        "    stub_id: sA\n"
        "  - roadmap_id: rB\n"
        "    stub_id: sB\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )

    guard_calls = []

    async def _fake_guard(params, repo_root):
        guard_calls.append(params.get("candidate"))
        return {"exit_code": 1, "referenced": False, "children": []}

    monkeypatch.setattr(m, "_live_children_guard", _fake_guard)

    result = _run(
        m._handler(
            {
                "plan_path": "docs/plans/fan-out-plan.md",
                "sha": "deadbeef1234",
                "delivery_proof": _complete_proof("dlv-a"),
            },
            worktree,
        )
    )

    assert result["exit_code"] == 0
    closed_by_basis = {c["stub_id"]: c["close_basis"] for c in result["closed"]}
    assert closed_by_basis.get("sA") == m.CLOSE_BASIS_DELIVERY_PROOF
    assert closed_by_basis.get("sB") == m.CLOSE_BASIS_GUARD
    # Guard must have been consulted for stub B (proof mismatch) but not
    # need to have been for stub A (proof applies) — asserted via the closed
    # basis above; guard_calls confirms it was reached at all for B.
    assert any("stub-b.md" in (c or "") for c in guard_calls)


# ---------------------------------------------------------------------------
# predecessor_handoff path-keyed leg (C2)
# ---------------------------------------------------------------------------


def _seed_non_roadmap_spinoff_stub(
    worktree: Path, name: str = "origin-stub.md", *, deployment_state: str = "ready_to_fire"
) -> Path:
    """Seed a NON-ROADMAP `kind: spinoff` origin stub — schema-legal without
    `roadmap_id`/`stub_id` (those are legal only on `kind: spinoff-roadmap`/
    `roadmap-baton`), the exact population `predecessor_handoff` exists to
    reach: `_is_baton_kind` admits `kind: spinoff`, but the pair-keyed
    `_scan_matches`/`_try_close` pipeline can never match it (no pair to
    match on)."""
    stub = worktree / "state" / "handoffs" / name
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(
        "---\n"
        "kind: spinoff\n"
        "title: Non-roadmap origin stub\n"
        "created: 2026-08-13\n"
        "branch: main\n"
        "status: open\n"
        "predecessor: none\n"
        "category: infra\n"
        "summary: non-roadmap spinoff origin stub fixture\n"
        f"deployment_state: {deployment_state}\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )
    return stub


def _seed_session_handoff(worktree: Path, name: str = "not-a-baton.md") -> Path:
    """Seed a `kind: session-handoff` record — NOT baton-kind
    (`_is_baton_kind` refuses it), the fixture for "predecessor_handoff names
    a file that is not baton-kind" (never closed)."""
    stub = worktree / "state" / "handoffs" / name
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(
        "---\n"
        "kind: session-handoff\n"
        "title: Not a baton\n"
        "created: 2026-08-13\n"
        "branch: main\n"
        "status: open\n"
        "predecessor: none\n"
        "category: infra\n"
        "summary: session-handoff fixture, refused by _is_baton_kind\n"
        "deployment_state: ready_to_fire\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )
    return stub


def _seed_plan(
    worktree: Path,
    name: str = "plan.md",
    *,
    predecessor_handoff: str | None = None,
    roadmap_id: str | None = None,
    stub_id: str | None = None,
) -> Path:
    plans_dir = worktree / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    plan = plans_dir / name
    fm = "status: draft\n"
    if predecessor_handoff is not None:
        fm += f"predecessor_handoff: {predecessor_handoff}\n"
    if roadmap_id is not None:
        fm += f"roadmap_id: {roadmap_id}\n"
    if stub_id is not None:
        fm += f"stub_id: {stub_id}\n"
    plan.write_text(f"---\n{fm}---\n\nBody.\n", encoding="utf-8")
    return plan


def test_predecessor_handoff_closes_non_roadmap_spinoff_stub(tmp_path, monkeypatch):
    """(1) A plan's `predecessor_handoff` naming a `ready_to_fire` non-roadmap
    `kind: spinoff` stub with NO roadmap_id/stub_id closes it, with
    `stubs_resolved == 1`."""
    worktree = tmp_path
    _fake_git_common_dir(worktree, monkeypatch)
    _seed_non_roadmap_spinoff_stub(worktree)
    plan = _seed_plan(
        worktree, predecessor_handoff="state/handoffs/origin-stub.md"
    )

    async def _fake_guard(params, repo_root):
        return {"exit_code": 1, "referenced": False, "children": []}

    monkeypatch.setattr(m, "_live_children_guard", _fake_guard)

    result = _run(
        m._handler(
            {"plan_path": "docs/plans/plan.md", "sha": "deadbeef1234"}, worktree
        )
    )

    assert result["exit_code"] == 0
    assert result["stubs_resolved"] == 1
    assert len(result["closed"]) == 1
    closed_entry = result["closed"][0]
    assert closed_entry["stub_path"] == "state/handoffs/origin-stub.md"
    assert closed_entry["join_source"] == "predecessor_handoff"
    assert closed_entry["roadmap_id"] is None
    assert closed_entry["stub_id"] is None


def test_predecessor_handoff_shipped_stub_excluded_not_no_candidates(tmp_path, monkeypatch):
    """(2) A `predecessor_handoff`-named stub already `deployment_state:
    shipped` is NOT closed, is reported as state-excluded, and the call is
    NOT `no_candidates`."""
    worktree = tmp_path
    (worktree / ".git").mkdir(parents=True, exist_ok=True)
    _seed_non_roadmap_spinoff_stub(worktree, deployment_state="shipped")
    _seed_plan(worktree, predecessor_handoff="state/handoffs/origin-stub.md")

    result = _run(
        m._handler({"plan_path": "docs/plans/plan.md"}, worktree)
    )

    assert result["exit_code"] == 0
    assert result.get("no_candidates") is not True
    assert result["closed"] == []
    assert len(result["skipped"]) == 1
    skip_entry = result["skipped"][0]
    assert skip_entry["reason"] == "no-match-filtered-deployment-state"
    excluded = skip_entry["excluded"]
    assert len(excluded) == 1
    assert excluded[0]["stub_path"] == "state/handoffs/origin-stub.md"
    assert excluded[0]["deployment_state"] == "shipped"
    assert excluded[0]["exclusion_reason"] == "state-not-eligible"


def test_predecessor_handoff_dedupes_against_pair_leg(tmp_path, monkeypatch):
    """(3) A stub reachable by BOTH a (roadmap_id, stub_id) pair (the plan's
    own direct frontmatter) AND `predecessor_handoff` (naming the SAME stub)
    closes exactly once."""
    worktree = tmp_path
    _fake_git_common_dir(worktree, monkeypatch)
    stub = _seed_stub(worktree)  # kind: roadmap-baton, roadmap_id: r1, stub_id: s1
    assert stub.name == "origin-stub.md"
    _seed_plan(
        worktree,
        predecessor_handoff="state/handoffs/origin-stub.md",
        roadmap_id="r1",
        stub_id="s1",
    )

    close_calls = []

    async def _fake_guard(params, repo_root):
        close_calls.append(params.get("candidate"))
        return {"exit_code": 1, "referenced": False, "children": []}

    monkeypatch.setattr(m, "_live_children_guard", _fake_guard)

    result = _run(
        m._handler(
            {"plan_path": "docs/plans/plan.md", "sha": "deadbeef1234"}, worktree
        )
    )

    assert result["exit_code"] == 0
    assert result["pairs_resolved"] == 1
    assert result["stubs_resolved"] == 1
    assert len(result["closed"]) == 1
    assert result["closed"][0]["stub_path"] == "state/handoffs/origin-stub.md"
    # Closed exactly once — the live-children guard (called once per actual
    # close attempt) must have been consulted exactly once, not twice.
    assert len(close_calls) == 1


def test_predecessor_handoff_absent_is_byte_identical_to_today(tmp_path, monkeypatch):
    """(4) `predecessor_handoff` absent -> byte-identical behaviour to today,
    including `no_candidates: true` on a plan with no linkage at all
    (regression guard on the quiet path)."""
    worktree = tmp_path
    (worktree / ".git").mkdir(parents=True, exist_ok=True)
    _seed_plan(worktree)  # no predecessor_handoff, no roadmap_id/stub_id

    result = _run(
        m._handler({"plan_path": "docs/plans/plan.md"}, worktree)
    )

    assert result["exit_code"] == 0
    assert result["no_candidates"] is True
    assert result["closed"] == []
    assert result["skipped"] == []
    assert result["pairs_resolved"] == 0


def test_predecessor_handoff_nonexistent_path_does_not_crash(tmp_path, monkeypatch):
    """(5) `predecessor_handoff` naming a non-existent path does not crash;
    behaves as no linkage from that leg."""
    worktree = tmp_path
    (worktree / ".git").mkdir(parents=True, exist_ok=True)
    _seed_plan(
        worktree, predecessor_handoff="state/handoffs/does-not-exist.md"
    )

    result = _run(
        m._handler({"plan_path": "docs/plans/plan.md"}, worktree)
    )

    assert result["exit_code"] == 0
    assert result["no_candidates"] is True
    assert result["closed"] == []
    assert result["skipped"] == []


def test_predecessor_handoff_non_baton_kind_refused(tmp_path, monkeypatch):
    """(6) `predecessor_handoff` naming a file that is NOT baton-kind (e.g. a
    `session-handoff`) is refused, not closed."""
    worktree = tmp_path
    (worktree / ".git").mkdir(parents=True, exist_ok=True)
    _seed_session_handoff(worktree)
    _seed_plan(
        worktree, predecessor_handoff="state/handoffs/not-a-baton.md"
    )

    result = _run(
        m._handler({"plan_path": "docs/plans/plan.md"}, worktree)
    )

    assert result["exit_code"] == 0
    assert result["no_candidates"] is True
    assert result["closed"] == []
    assert result["skipped"] == []
