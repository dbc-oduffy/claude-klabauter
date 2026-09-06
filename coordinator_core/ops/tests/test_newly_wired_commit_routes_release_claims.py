"""
coordinator_core.ops.tests.test_newly_wired_commit_routes_release_claims

Purpose: behavioural coverage for the commit routes wired to
`session/scope.py :: release_committed_claims` on 2026-09-06. Each landed a
commit while the claim over its paths stayed open, because `commit_paths` /
`commit_authored_content` release nothing themselves -- the release is
hand-wired per route, and these routes had no wiring.

WHY THIS MODULE EXISTS BESIDE THE TRIPWIRE.
`ops/ceremony/tests/test_commit_route_release_tripwire.py` is a STATIC
enumerator: it proves a route is classified and, for a "release" row, that
somebody said it should release. It cannot prove the call fires, that it
releases the right paths, or that it leaves a peer alone. A row flipped to
`confirmed: True` over a call that never executes would satisfy it
completely. So the flip is backed here, by driving each route against a real
repo and reading the claim back through `session.claim_index.lookup()` --
the same surface the commit gate reads -- rather than string-matching
`touched.txt`.

Shape mirrors `ops/ceremony/tests/test_commit_v2_claim_release.py`, the
sibling written for the same property on the default committer.

Negative-spec: `execute_plan_assemble.close_out_and_stamp` is the third
wired route and is NOT driven here -- reaching its commit leg needs a full
plan document with a Tasks spine and evidence rows, which is
`execute_plan_assemble/tests/`'s harness, not this module's. Its wiring is
covered statically by the tripwire and by its own suite.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import memo_transition
from coordinator_core.ops.session import safe_commit_offer
from coordinator_core.session import claim_index
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

# Spawns real external `git` processes; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


def _claim_cleared(repo: Path, sid: str, rel_path: str) -> bool:
    result = claim_index.lookup([rel_path], cwd=str(repo))
    return sid not in result.get(rel_path, [])


def _still_claimed(repo: Path, sid: str, rel_path: str) -> bool:
    result = claim_index.lookup([rel_path], cwd=str(repo))
    return sid in result.get(rel_path, [])


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _seed_tracked(repo: Path, rel: str, body: str = "v1\n") -> Path:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body, encoding="utf-8")
    _git(["add", "--", rel], repo)
    _git(["commit", "-q", "-m", "seed " + rel], repo)
    return f


# ---------------------------------------------------------------------------
# ops/session/safe_commit_offer.py :: _commit_group
# ---------------------------------------------------------------------------


def test_commit_group_releases_this_sessions_own_claim(repo):
    """The route that needed this most plainly: the group it commits IS the
    session's own claimed dirty set, so every auto-commit used to leave a
    claim standing over paths it had just written to history."""
    sid = "safe-commit-offer-release-own"
    rel = "state/handoff-tracker.md"
    f = _seed_tracked(repo, rel)
    f.write_text("v2\n", encoding="utf-8")

    session_core.init(sid, cwd=str(repo))
    session_scope.touch(sid, rel, cwd=str(repo))
    assert _still_claimed(repo, sid, rel), "precondition: the claim is held"

    result = asyncio.run(
        safe_commit_offer._commit_group(
            str(repo), {"paths": [rel], "message": "update tracker"}, sid
        )
    )

    assert result["committed"] is True
    assert _claim_cleared(repo, sid, rel)


def test_commit_group_leaves_a_peers_claim_alone(repo):
    """Release is scoped to the sid handed in, never a guess at authorship.
    `release_committed_claims` is structurally incapable of releasing a
    peer's claim; this pins that the route does not defeat that by handing
    it somebody else's id."""
    own_sid = "safe-commit-offer-release-self"
    peer_sid = "safe-commit-offer-release-peer"
    rel = "state/handoff-tracker.md"
    f = _seed_tracked(repo, rel)

    session_core.init(peer_sid, cwd=str(repo))
    session_scope.touch(peer_sid, rel, cwd=str(repo))

    f.write_text("v2\n", encoding="utf-8")
    session_core.init(own_sid, cwd=str(repo))

    result = asyncio.run(
        safe_commit_offer._commit_group(
            str(repo), {"paths": [rel], "message": "update tracker"}, own_sid
        )
    )

    assert result["committed"] is True
    assert _still_claimed(repo, peer_sid, rel)


def test_commit_group_with_no_session_id_lands_and_retains(repo):
    """`session_id` is optional on this route. With none, the commit still
    lands and nothing is released -- skipped explicitly, never guessed from
    the environment."""
    peer_sid = "safe-commit-offer-release-peer-2"
    rel = "state/handoff-tracker.md"
    f = _seed_tracked(repo, rel)

    session_core.init(peer_sid, cwd=str(repo))
    session_scope.touch(peer_sid, rel, cwd=str(repo))
    f.write_text("v2\n", encoding="utf-8")

    result = asyncio.run(
        safe_commit_offer._commit_group(
            str(repo), {"paths": [rel], "message": "update tracker"}, None
        )
    )

    assert result["committed"] is True
    assert _still_claimed(repo, peer_sid, rel)


# ---------------------------------------------------------------------------
# ops/memo_transition.py :: _commit_terminal_write
# ---------------------------------------------------------------------------


def test_memo_terminal_write_releases_under_a_trusted_session_id(repo):
    """Releases when the caller supplied a session id -- the `claim` and
    `resolve` verbs, the only two whose params carry one."""
    sid = "memo-transition-release-own"
    rel = "state/cross-repo/inbox/a-memo.md"
    _seed_tracked(repo, rel, "---\nstatus: open\n---\n")

    session_core.init(sid, cwd=str(repo))
    session_scope.touch(sid, rel, cwd=str(repo))
    assert _still_claimed(repo, sid, rel), "precondition: the claim is held"

    sha, err = memo_transition._commit_terminal_write(
        repo / rel, repo, "resolve", "---\nstatus: resolved\n---\n",
        attributed_session_id=sid,
    )

    assert err is None and sha
    assert _claim_cleared(repo, sid, rel)


def test_memo_terminal_write_releases_nothing_without_a_trusted_id(repo):
    """THE DELIBERATE HALF. With no caller-supplied id the commit still
    lands and NOTHING is released.

    `commit_authored_content` falls back to a blind env-var read for its
    trailer, which state/bug-backlog/2026-08-18-scoped-git-commit-stamps-a-
    foreign-session-id-8d21f0c4e7b9.yaml records stamping a FOREIGN session
    id. A wrong id in a trailer is a mis-attribution; a wrong id in a
    RELEASE would drop a claim that is not ours to drop. So this route
    releases only what it can attribute -- and this test is what stops a
    later edit from "completing" the None case by reaching for that same
    env read.
    """
    peer_sid = "memo-transition-release-peer"
    rel = "state/cross-repo/inbox/a-memo.md"
    _seed_tracked(repo, rel, "---\nstatus: open\n---\n")

    session_core.init(peer_sid, cwd=str(repo))
    session_scope.touch(peer_sid, rel, cwd=str(repo))

    sha, err = memo_transition._commit_terminal_write(
        repo / rel, repo, "action", "---\nstatus: actioned\n---\n",
        attributed_session_id=None,
    )

    assert err is None and sha
    assert _still_claimed(repo, peer_sid, rel)
