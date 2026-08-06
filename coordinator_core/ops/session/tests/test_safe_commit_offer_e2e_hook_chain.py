"""
coordinator_core.ops.session.tests.test_safe_commit_offer_e2e_hook_chain —
the end-to-end integration test for the touched-path-poisoning-normalize-
git-dir workstream: drives the REAL handler chain

    coordinator_core.hooks.track_touched_files._handler (PostToolUse hook)
    -> coordinator_core.session.scope.compute_scope
    -> coordinator_core.ops.session.safe_commit_offer.compute_offer

end to end in a fixture git repo, at the hook's PRODUCTION call shape
(``repo_root`` = the git COMMON dir, ``<repo>/.git`` — ``op_scopes.py``
registers ``"hooks.track_touched_files"`` as ``common_dir``-scoped, so
``ipc.py``'s dispatch always hands this handler ``git_common_dir(request_repo)``,
never the worktree root). No stage of this chain is mocked: the hook's own
``git_common_dir`` -> ``main_worktree_root`` derivation, ``compute_scope``'s
liveness gate (real ``liveness.live_session_ids``, no monkeypatch — see
``coordinator_core.session.tests.test_scope`` F3's own "mock the bridge"
rationale), and ``compute_offer``'s composition are all exercised for real.

Every unit AC in this workstream (C0-C7) can pass while the user-visible
symptom this plan exists to fix (a session's own scope silently narrowed or
widened by a stale/poisoned peer claim) survives — this module is the
criterion the whole plan is judged on. Satisfies AC9(a).

Spec backlink: docs/plans/2026-07-31-touched-path-poisoning-normalize-git-dir.md § C8/AC9

Negative-spec:
    - Do NOT mock/monkeypatch any stage of the chain (hook, compute_scope,
      compute_offer, liveness) — the whole point is to exercise the
      handler's own root derivation and the real liveness gate. A peer
      session is made LIVE the same way ``test_scope.py``'s own
      no-monkeypatch regression does: a real ``core.init()`` call, which
      stamps ``last_activity`` to "now" and satisfies Layer-2 recency
      liveness with no stubbing required.
    - Do NOT assert subset/superset membership on ``my_scope``/``skipped``/
      ``orphans`` — every assertion here is an EXACT set/tuple-set
      comparison. A subset assertion cannot see a silent WIDENING, which is
      the exact failure class (cadc5d87, 29 files) this test guards against.
"""

from __future__ import annotations

import asyncio

import pytest

from coordinator_core.hooks import track_touched_files as ttf
from coordinator_core.ops.session.safe_commit_offer import compute_offer
from coordinator_core.session import core, scope


def _edit_via_hook(session_repo, session_id: str, rel_path: str, content: str) -> None:
    """Drive one file edit through the REAL PostToolUse hook, at the hook's
    production call shape: `repo_root` is the git COMMON dir (`<repo>/.git`),
    exactly what `op_scopes.py`'s `"common_dir"` scope hands the handler in
    production — never the worktree root directly."""
    target = session_repo.root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    params = {
        "session_id": session_id,
        "tool_name": "Edit",
        "file_path": str(target),
    }
    asyncio.run(ttf._handler(params, repo_root=session_repo.common_dir))


def _write_agent_claim(session_repo, agent_id: str, em_session_id: str, *entries: str) -> None:
    """Build a peer dispatched sub-agent's `.agents/<agent_id>/` claim record,
    back-pointed to `em_session_id` via `em-session-id.txt` — the CURRENT
    (post-C2) clean repo-relative dialect, the same shape
    `track_touched_files` itself now writes."""
    agent_dir = session_repo.sessions_dir / ".agents" / agent_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "em-session-id.txt").write_text(em_session_id + "\n", encoding="utf-8")
    (agent_dir / "touched.txt").write_text(
        "".join(e + "\n" for e in entries), encoding="utf-8"
    )


class TestEndToEndHookToComputeScopeToComputeOffer:
    def test_exact_membership_across_full_chain(self, session_repo):
        # --- This session ("mine") edits N=3 files through the REAL hook ---
        _edit_via_hook(session_repo, "mine", "src/a.py", "a")
        _edit_via_hook(session_repo, "mine", "src/b.py", "b")
        _edit_via_hook(session_repo, "mine", "docs/c.md", "c")

        # --- A live PEER session, holding its OWN claim on a file "mine"
        # never touched. Liveness: made live for real via the hook's own
        # session bootstrap (core.init under the hood), which stamps
        # last_activity to "now" -- no monkeypatch. ---
        _edit_via_hook(session_repo, "peer", "peer_own.py", "p")

        # --- A live PEER EM session with a dispatched sub-agent's `.agents/`
        # claim (current, clean repo-relative dialect) on a file "mine"
        # never touched and no top-level session ever recorded. ---
        assert core.init("peer-em", cwd=str(session_repo.root)) is True
        (session_repo.root / "agent").mkdir()
        (session_repo.root / "agent" / "owned.py").write_text("z", encoding="utf-8")
        _write_agent_claim(session_repo, "agent-owner", "peer-em", "agent/owned.py")

        # --- AC0/C2 regression pin: a peer `.agents/` claim, in the CURRENT
        # clean-repo-relative dialect, on a file "mine" ALSO genuinely
        # touched itself (so it is a real touched.txt candidate, not merely
        # an mtime-only one) -- the claim must still SHADOW it: it lands in
        # `skipped` attributed to the owning EM session, and must NOT
        # silently vanish from `other_owner` and widen `my_scope`. This is
        # the exact mechanism behind the cadc5d87 29-file incident. ---
        assert core.init("shadow-em", cwd=str(session_repo.root)) is True
        _edit_via_hook(session_repo, "mine", "shadow/shadowed.py", "s")
        _write_agent_claim(
            session_repo, "agent-shadow", "shadow-em", "shadow/shadowed.py"
        )

        # --- An uncontested dirty orphan: touched by nobody, claimed by
        # nobody, dirty after "mine" started -- must surface as an orphan,
        # never silently owned and never in my_scope. ---
        (session_repo.root / "orphan").mkdir()
        (session_repo.root / "orphan" / "nobody.py").write_text("o", encoding="utf-8")

        result = scope.compute_scope("mine", cwd=str(session_repo.root))

        assert set(result.my_scope) == {"src/a.py", "src/b.py", "docs/c.md"}
        assert set(result.skipped) == {
            ("peer_own.py", "peer"),
            ("agent/owned.py", "peer-em"),
            ("shadow/shadowed.py", "shadow-em"),
        }
        assert set(result.orphans) == {"orphan/nobody.py"}

        # --- Close the loop: compute_offer composes compute_scope, and its
        # safe_paths/excluded must be consistent with the exact sets above --
        # not re-derived independently, the SAME chain end to end. ---
        offer = compute_offer("mine", cwd=str(session_repo.root))
        assert set(offer["safe_paths"]) == set(result.my_scope)

        excluded_by_path = {e["path"]: e["reason"] for e in offer["excluded"]}
        assert excluded_by_path["peer_own.py"] == "owned by session peer"
        assert excluded_by_path["agent/owned.py"] == "owned by session peer-em"
        assert excluded_by_path["shadow/shadowed.py"] == "owned by session shadow-em"
        assert excluded_by_path["orphan/nobody.py"] == "untouched by this session"
        assert set(excluded_by_path) == {
            "peer_own.py",
            "agent/owned.py",
            "shadow/shadowed.py",
            "orphan/nobody.py",
        }
