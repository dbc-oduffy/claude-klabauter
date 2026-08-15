"""
coordinator_core.ops.ceremony.tests.test_scoped_git_commit_claim_gate_removed

Spec backlink: docs/plans/2026-08-13-claim-release-deadlock-and-the-doctrine-
that-rejects-it.md, C1, AC2.

Regression tests pinning post-C1 behaviour: `scoped_git_commit._handler` no
longer refuses a commit on a path-touch claim signal at all -- the
`_check_claim_conflicts` gate this file's now-deleted siblings
(`test_scoped_git_commit_ownership.py`, `test_scoped_git_commit_dirty_gate.py`,
`test_commit_gate_budget.py`, `test_claim_cli_remedy_invocations.py`) pinned
was removed outright, not narrowed. See this module's own docstring
("Sink-side ownership enforcement") for the removal rationale.

AC2 asks for both observed incidents reproduced as tests that failed before
the change and pass after. The deletion already landed in this shared tree
before this test file was authored (commit `b2d2828c8`), so there is no
live checkout at which either scenario can be RUN and observed to fail --
instead, each test below is a REGRESSION pin of the post-removal behaviour
(commit proceeds), with its docstring citing the deleted `_check_claim_
conflicts` (read via `git show b2d2828c8^:coordinator_core/ops/ceremony/
scoped_git_commit.py`) to establish that the identical scenario, run against
that prior code, would have hit its refusal path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import scoped_git_commit
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

# Declared, not excused -- this file spawns a real `git` process because the
# property under test is porcelain's own behaviour (mirrors the identical
# note in the now-deleted `test_scoped_git_commit_dirty_gate.py`).
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


def _seed_and_commit_file(repo: Path, rel_path: str, content: str) -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _git(["add", rel_path], repo)
    _git(["commit", "-q", "-m", "seed"], repo)


def _dirty_file(repo: Path, rel_path: str, content: str) -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


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


def _call(params: dict) -> dict:
    return scoped_git_commit._handler(params, repo_root=None)


def test_dirty_path_live_peer_claimant_non_claimant_committer_proceeds(tmp_path):
    """Incident 1's shape: a dirty path is claimed (touched) by a LIVE peer
    session, and a DIFFERENT (non-claimant) session commits it. Post-C1 this
    PROCEEDS -- no claim-derived signal remains in the commit path at all.

    Pre-C1, this scenario would have hit `_check_claim_conflicts`'s refusal
    path (`git show b2d2828c8^:coordinator_core/ops/ceremony/scoped_git_
    commit.py`, `_check_claim_conflicts` lines ~666-722): `sess-peer` is an
    "other" claimant, live (`session_liveness.session_live` reads its fresh
    `meta.json`), and `contested.txt` resolves `_DIRTY_STATUS_DIRTY` since it
    was left dirty with no rescue commit -- landing in the `conflicted.append`
    branch (not the diverged carve-out, since nothing is staged), which
    `_handler` turned into a `committed: False` refusal naming both the path
    and the peer session id.
    """
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "contested.txt", "v1\n")
    _dirty_file(repo, "contested.txt", "v2\n")  # left dirty, no rescue commit

    _write_touched(
        repo, "sess-peer", [_touch_line("T", "contested.txt", "2026-08-13T10:00:00.000000Z")]
    )
    _write_meta(repo, "sess-peer", live=True)
    _write_meta(repo, "sess-caller", live=True)

    result = _call({
        "worktree_root": str(repo),
        "paths": ["contested.txt"],
        "message": "commit over a live peer's touched-but-dirty file",
        "session_id": "sess-caller",
    })

    assert result["committed"] is True, result
    assert not result.get("error"), result


def test_stale_touch_no_edit_within_30s_still_proceeds(tmp_path):
    """Incident-2-shaped duration defect: a claim whose recorded TOUCH event
    is old (no `T`-event newer than ~30s -- the plan's AC1d recency-of-EDIT
    window) still refuses under the old gate, because `_check_claim_
    conflicts` never read touch recency at all -- only claim existence,
    liveness, dirtiness, and (C1) divergence. Post-C1 this PROCEEDS.

    Pre-C1: identical to the sibling test above at `_check_claim_conflicts`
    (`git show b2d2828c8^:...`, lines ~666-722) -- the touch timestamp
    recorded in `touched.txt` (here backdated well past 30s) is never read
    by that function; only `others`/liveness/dirty-status/divergence are.
    So an old, stale touch record refused a commit exactly as a fresh one
    did -- the buildable form of the duration defect DR-258 makes otherwise
    unproducible (a genuinely read-only claim cannot arise from any live
    write path).
    """
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "contested.txt", "v1\n")
    _dirty_file(repo, "contested.txt", "v2\n")

    # Touch recorded long before the ~30s recency window -- "stale" per the
    # plan's AC1d framing, not stale in the claim-index staleness-release
    # sense (that requires the SESSION to be dead; here it stays live).
    _write_touched(
        repo, "sess-peer", [_touch_line("T", "contested.txt", "2026-08-13T00:00:00.000000Z")]
    )
    _write_meta(repo, "sess-peer", live=True)
    _write_meta(repo, "sess-caller", live=True)

    result = _call({
        "worktree_root": str(repo),
        "paths": ["contested.txt"],
        "message": "commit over a stale-touch live peer claim",
        "session_id": "sess-caller",
    })

    assert result["committed"] is True, result
