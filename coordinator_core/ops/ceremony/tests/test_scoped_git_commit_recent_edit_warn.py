"""
coordinator_core.ops.ceremony.tests.test_scoped_git_commit_recent_edit_warn

Spec backlink: docs/plans/2026-08-13-claim-release-deadlock-and-the-doctrine-
that-rejects-it.md, chunk C1's deferred AC1d leg (C1d). Pins
`scoped_git_commit._warn_recent_edits` -- the ONLY claim-derived signal that
may exist on the commit path post-C1 -- and its hard constraint: it may WARN
(log), never gate/pause/prompt.

Gate that authorized this: C5 (05075df7d692) returned SUBSTRATE SURVIVES.
Mechanism: state/audits/2026-08-13-edit-recency-spike.md.

Also pins C2 of docs/plans/2026-08-16-authorship-survives-the-sweep.md
(AC3/AC4/AC5): `scoped_git_commit._disclose_peer_claims`, the claim-
PRESENCE-keyed sibling of `_warn_recent_edits` above -- independent of that
function's edit-recency window by design, folded into
`response["peer_claim_disclosure"]` on the same call that commits.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import scoped_git_commit
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

# Declared, not excused -- `_warn_recent_edits` resolves the sessions dir via
# `claim_index.lookup(cwd=...)` -> `core.sessions_dir()`, which spawns a real
# `git rev-parse --git-common-dir` (mirrors the identical note in this
# directory's sibling `test_scoped_git_commit_claim_gate_removed.py`).
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _sessions_dir(repo: Path) -> Path:
    return repo / ".git" / "coordinator-sessions"


def _write_touched(repo: Path, sid: str, lines: list[str]) -> None:
    sdir = _sessions_dir(repo) / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "touched.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )


def _write_meta(repo: Path, sid: str, *, live: bool) -> None:
    sdir = _sessions_dir(repo) / sid
    sdir.mkdir(parents=True, exist_ok=True)
    last_activity = session_core.now_iso() if live else "2020-01-01T00:00:00Z"
    (sdir / "meta.json").write_text(
        '{"pid": 1, "last_activity": "%s"}\n' % last_activity,
        encoding="utf-8",
    )


def _touch_line(verb: str, path: str, ts: str) -> str:
    return "%s %s %s" % (verb, ts, path)


def _write_shape(repo: Path, sid: str, *, deliverable_id: str) -> None:
    sdir = _sessions_dir(repo) / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "session-shape.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": sid,
                "pickup": {"deliverable_id": deliverable_id},
            }
        )
        + "\n",
        encoding="utf-8",
    )


_NOW = datetime(2026, 8, 13, 10, 0, 30, tzinfo=timezone.utc)


def test_edit_just_inside_window_warns(tmp_path, caplog):
    repo = _init_repo(tmp_path)
    # 30s before _NOW -- exactly at the window boundary, still inside it.
    _write_touched(
        repo, "sess-peer", [_touch_line("T", "hot.py", "2026-08-13T10:00:00.000000Z")]
    )
    _write_meta(repo, "sess-peer", live=True)

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.ceremony.scoped_git_commit"):
        scoped_git_commit._warn_recent_edits(
            str(repo), ["hot.py"], "sess-caller", now=_NOW
        )

    assert any("hot.py" in r.message for r in caplog.records), caplog.text
    assert any("sess-peer" in r.message for r in caplog.records), caplog.text


def test_edit_just_outside_window_is_silent(tmp_path, caplog):
    repo = _init_repo(tmp_path)
    # 31s before _NOW -- one second past the window boundary.
    _write_touched(
        repo, "sess-peer", [_touch_line("T", "cold.py", "2026-08-13T09:59:59.000000Z")]
    )
    _write_meta(repo, "sess-peer", live=True)

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.ceremony.scoped_git_commit"):
        scoped_git_commit._warn_recent_edits(
            str(repo), ["cold.py"], "sess-caller", now=_NOW
        )

    assert caplog.records == []


def test_touch_without_edit_inside_window_is_silent(tmp_path, caplog):
    """No `touched.txt` T-event at all -- only a bare filesystem mtime
    change -- produces no warn, however recent that mtime is. `touched.txt`
    T-events are edit-only by construction (state/audits/2026-08-13-edit-
    recency-spike.md finding 1); this pins that `_warn_recent_edits` reads
    ONLY that substrate, never disk mtime."""
    repo = _init_repo(tmp_path)
    untouched = repo / "silent.py"
    untouched.write_text("v1\n", encoding="utf-8")
    # No touched.txt entry recorded for this path at all.

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.ceremony.scoped_git_commit"):
        scoped_git_commit._warn_recent_edits(
            str(repo), ["silent.py"], "sess-caller", now=_NOW
        )

    assert caplog.records == []


def test_recent_edit_by_caller_itself_is_silent(tmp_path, caplog):
    repo = _init_repo(tmp_path)
    _write_touched(
        repo, "sess-caller", [_touch_line("T", "own.py", "2026-08-13T10:00:29.000000Z")]
    )
    _write_meta(repo, "sess-caller", live=True)

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.ceremony.scoped_git_commit"):
        scoped_git_commit._warn_recent_edits(
            str(repo), ["own.py"], "sess-caller", now=_NOW
        )

    assert caplog.records == []


def test_recent_edit_by_dead_peer_is_silent(tmp_path, caplog):
    repo = _init_repo(tmp_path)
    _write_touched(
        repo, "sess-peer", [_touch_line("T", "hot.py", "2026-08-13T10:00:29.000000Z")]
    )
    _write_meta(repo, "sess-peer", live=False)

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.ceremony.scoped_git_commit"):
        scoped_git_commit._warn_recent_edits(
            str(repo), ["hot.py"], "sess-caller", now=_NOW
        )

    assert caplog.records == []


def test_recent_live_peer_edit_never_gates_the_commit(tmp_path, caplog):
    """The hard constraint: a warn-triggering scenario still commits --
    nothing in this file may pause/prompt/gate on what the warn reads.

    Review: coordinator:code-reviewer -- the prior version pinned the
    touch timestamp to 2020-01-01, always outside `_warn_recent_edits`'s
    real `datetime.now(timezone.utc)` comparison window at any wall-clock
    time this suite runs. That made the test pass vacuously: it proved an
    unclaimed/aged commit still lands (already covered by
    `test_scoped_git_commit_claim_gate_removed.py`), never that a commit
    proceeds despite a warn that ACTUALLY fired. `_handler` takes no
    `now=` override, so this pins the edit a few seconds before the real
    wall clock -- inside the 30s window at the instant `_handler` runs --
    and asserts both legs of the conjunction: the warn logged AND the
    commit landed.
    """
    repo = _init_repo(tmp_path)
    f = repo / "hot.py"
    f.write_text("v1\n", encoding="utf-8")
    _git(["add", "hot.py"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    f.write_text("v2\n", encoding="utf-8")

    # A few seconds before the real wall clock -- inside the 30s window
    # at the moment `_handler` below actually reads `datetime.now(...)`.
    recent_ts = (
        datetime.now(timezone.utc) - timedelta(seconds=5)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _write_touched(
        repo, "sess-peer", [_touch_line("T", "hot.py", recent_ts)]
    )
    _write_meta(repo, "sess-peer", live=True)
    _write_meta(repo, "sess-caller", live=True)

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.ceremony.scoped_git_commit"):
        result = scoped_git_commit._handler(
            {
                "worktree_root": str(repo),
                "paths": ["hot.py"],
                "message": "commit despite a warn-shaped scenario",
                "session_id": "sess-caller",
            },
            repo_root=None,
        )

    assert any("hot.py" in r.message for r in caplog.records), caplog.text
    assert any("sess-peer" in r.message for r in caplog.records), caplog.text
    assert result["committed"] is True, result
    assert not result.get("error"), result


def test_peer_claim_disclosure_names_peer_session_and_deliverable_and_lands(tmp_path):
    """AC3/AC4/AC5 core case: a path in the caller's pathspec is claimed by
    a LIVE peer session -- the commit lands AND `response[
    "peer_claim_disclosure"]` names that peer's session id and deliverable
    id. The claim is written far outside `_warn_recent_edits`'s 30s window
    (`2000-01-01`) -- proves the disclosure keys on claim PRESENCE, never
    edit recency, independent of that function's window."""
    repo = _init_repo(tmp_path)
    f = repo / "shared.py"
    f.write_text("v1\n", encoding="utf-8")
    _git(["add", "shared.py"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    f.write_text("v2\n", encoding="utf-8")

    _write_touched(
        repo, "sess-peer", [_touch_line("T", "shared.py", "2000-01-01T00:00:00.000000Z")]
    )
    _write_meta(repo, "sess-peer", live=True)
    _write_meta(repo, "sess-caller", live=True)
    _write_shape(repo, "sess-peer", deliverable_id="dlv-peer-owns-this-abc123")

    result = scoped_git_commit._handler(
        {
            "worktree_root": str(repo),
            "paths": ["shared.py"],
            "message": "commit despite an old-but-live peer claim",
            "session_id": "sess-caller",
        },
        repo_root=None,
    )

    assert result["committed"] is True, result
    assert not result.get("error"), result
    disclosure = result.get("peer_claim_disclosure")
    assert disclosure, result
    assert any(
        e["path"] == "shared.py"
        and e["session_id"] == "sess-peer"
        and e["deliverable_id"] == "dlv-peer-owns-this-abc123"
        for e in disclosure
    ), disclosure


def test_peer_claim_disclosure_swept_hunk_shape(tmp_path):
    """AC5 -- the swept-hunk shape named in the plan: committer session A,
    live claimant session B. The disclosure resolves to B, not A."""
    repo = _init_repo(tmp_path)
    f = repo / "apply_base.py"
    f.write_text("v1\n", encoding="utf-8")
    _git(["add", "apply_base.py"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    f.write_text("v2\n", encoding="utf-8")

    _write_touched(
        repo, "sess-B", [_touch_line("T", "apply_base.py", "2000-01-01T00:00:00.000000Z")]
    )
    _write_meta(repo, "sess-B", live=True)
    _write_meta(repo, "sess-A", live=True)
    _write_shape(repo, "sess-B", deliverable_id="dlv-b-owns-this")

    result = scoped_git_commit._handler(
        {
            "worktree_root": str(repo),
            "paths": ["apply_base.py"],
            "message": "committer A, claimant B",
            "session_id": "sess-A",
        },
        repo_root=None,
    )

    assert result["committed"] is True, result
    disclosure = result.get("peer_claim_disclosure")
    assert disclosure, result
    assert all(e["session_id"] != "sess-A" for e in disclosure), disclosure
    assert any(e["session_id"] == "sess-B" for e in disclosure), disclosure


def test_peer_claim_disclosure_omits_callers_own_claim(tmp_path):
    repo = _init_repo(tmp_path)
    f = repo / "own.py"
    f.write_text("v1\n", encoding="utf-8")
    _git(["add", "own.py"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    f.write_text("v2\n", encoding="utf-8")

    _write_touched(
        repo, "sess-caller", [_touch_line("T", "own.py", "2000-01-01T00:00:00.000000Z")]
    )
    _write_meta(repo, "sess-caller", live=True)

    result = scoped_git_commit._handler(
        {
            "worktree_root": str(repo),
            "paths": ["own.py"],
            "message": "commit over the caller's own claim",
            "session_id": "sess-caller",
        },
        repo_root=None,
    )

    assert result["committed"] is True, result
    assert not result.get("peer_claim_disclosure"), result


def test_peer_claim_disclosure_omits_dead_claimant(tmp_path):
    repo = _init_repo(tmp_path)
    f = repo / "cold.py"
    f.write_text("v1\n", encoding="utf-8")
    _git(["add", "cold.py"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    f.write_text("v2\n", encoding="utf-8")

    _write_touched(
        repo, "sess-dead", [_touch_line("T", "cold.py", "2000-01-01T00:00:00.000000Z")]
    )
    _write_meta(repo, "sess-dead", live=False)
    _write_meta(repo, "sess-caller", live=True)

    result = scoped_git_commit._handler(
        {
            "worktree_root": str(repo),
            "paths": ["cold.py"],
            "message": "commit over a dead peer's claim",
            "session_id": "sess-caller",
        },
        repo_root=None,
    )

    assert result["committed"] is True, result
    assert not result.get("peer_claim_disclosure"), result


def test_peer_claim_disclosure_folds_commit_agent_to_owner_em_session(tmp_path):
    """AC2b (PM ruling 2026-08-16, docs/plans/2026-08-16-authorship-
    survives-the-sweep.md): a dispatched commit-agent's identity is folded
    to its owning EM session before the self-comparison. Without the fold,
    `sid == caller_session_id` never matches (an agent id vs. an EM
    session id), and the EM's own team's claim would be disclosed back to
    it as though a peer held it -- a false positive on exactly the commits
    this mechanism exists to describe correctly."""
    repo = _init_repo(tmp_path)
    f = repo / "team_owned.py"
    f.write_text("v1\n", encoding="utf-8")
    _git(["add", "team_owned.py"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    f.write_text("v2\n", encoding="utf-8")

    # The EM session itself holds the claim.
    _write_touched(
        repo, "sess-em", [_touch_line("T", "team_owned.py", "2000-01-01T00:00:00.000000Z")]
    )
    _write_meta(repo, "sess-em", live=True)

    # The dispatched commit-agent's back-pointer names sess-em as its owner
    # -- the same `.agents/<agent-id>/em-session-id.txt` shape `claim_index`
    # already folds CLAIMANTS through.
    agent_dir = _sessions_dir(repo) / ".agents" / "agent-commit-1"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "em-session-id.txt").write_text("sess-em\n", encoding="utf-8")

    result = scoped_git_commit._handler(
        {
            "worktree_root": str(repo),
            "paths": ["team_owned.py"],
            "message": "commit-agent committing under its own EM's claim",
            "session_id": "agent-commit-1",
        },
        repo_root=None,
    )

    assert result["committed"] is True, result
    assert not result.get("peer_claim_disclosure"), result


def test_fold_caller_to_owner_session_degrades_cleanly_for_plain_em_session(tmp_path):
    """AC2b, second pin: a plain EM session id -- one with no `.agents/<id>/
    em-session-id.txt` back-pointer at all -- folds to ITSELF unchanged.
    An EM's own session id is already the owner; the fold must not require
    an agent back-pointer to exist in order to work."""
    repo = _init_repo(tmp_path)
    folded = scoped_git_commit._fold_caller_to_owner_session(str(repo), "sess-em-plain")
    assert folded == "sess-em-plain", folded


def test_peer_claim_disclosure_absent_for_unclaimed_path(tmp_path):
    repo = _init_repo(tmp_path)
    f = repo / "unclaimed.py"
    f.write_text("v1\n", encoding="utf-8")
    _git(["add", "unclaimed.py"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    f.write_text("v2\n", encoding="utf-8")

    result = scoped_git_commit._handler(
        {
            "worktree_root": str(repo),
            "paths": ["unclaimed.py"],
            "message": "commit over an unclaimed path",
            "session_id": "sess-caller",
        },
        repo_root=None,
    )

    assert result["committed"] is True, result
    assert "peer_claim_disclosure" not in result, result


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
