"""
coordinator_core.ops.ceremony.tests.test_scoped_git_commit_dirty_gate

C5 (docs/plans/2026-08-11-claim-release-and-the-gate-that-cannot-clear.md):
`_check_claim_conflicts` refuses a path only on the CONJUNCTION "a live
peer claims it AND it is currently dirty" -- never on the mere existence of
a live peer's claim. This is the change that closes the rescue-commit
class: a session that died mid-work leaves a claim
`release_committed_claims` is structurally incapable of retiring for a
different rescuing sid (see `session/tests/test_scope.py`), so once a peer
rescues and commits that work the path is clean and must be committable
again despite the stale claim -- see `scoped_git_commit._check_claim_
conflicts`'s own docstring for the full argument.

Fixture shape borrowed from `test_scoped_git_commit_ownership.py` (peer
sessions synthesized by writing `.git/coordinator-sessions/<sid>/` files
directly, never a spawned peer process) -- this file only adds the
dirty/clean axis that file's fixtures never varied (every case there
dirties the contested path first).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import scoped_git_commit
from coordinator_core.session import claim_index
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

# Declared, not excused -- see test_scoped_git_commit_ownership.py's own
# identical note: this file spawns a real `git` process because the
# property under test is porcelain's own behaviour.
pytestmark = [pytest.mark.spawns_process]


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
    last_activity = (
        session_core.now_iso() if live else "2020-01-01T00:00:00Z"
    )
    (sdir / "meta.json").write_text(
        '{"pid": 1, "last_activity": "%s"}\n' % last_activity,
        encoding="utf-8",
    )


def _touch_line(verb: str, path: str) -> str:
    return "%s 2026-08-08T10:00:00.000000Z %s" % (verb, path)


def _call(params: dict) -> dict:
    return scoped_git_commit._handler(params, repo_root=None)


# ---------------------------------------------------------------------------
# A7: the rescue-commit case -- the whole reason C5 exists
# ---------------------------------------------------------------------------


def test_rescue_commit_proceeds_when_claimant_live_but_path_clean(tmp_path):
    """A LIVE peer claims the path; the path itself carries NOTHING
    uncommitted (a rescuing session already landed the work under its own
    sid) -- the commit must PROCEED. This is the case placement (c) could
    never see (its map is degenerate exactly here -- see this module's own
    docstring) and the case `7f95320d4`'s liveness-gated release cannot
    reach either (it refuses on any live claimant, full stop)."""
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "rescued.txt", "v1\n")
    # The claimant's crashed-session claim is still on disk and still LIVE
    # by this fixture's liveness signal, but nothing is left dirty under
    # the path -- a rescuer already committed it.
    _dirty_file(repo, "rescued.txt", "v2\n")
    _git(["add", "rescued.txt"], repo)
    _git(["commit", "-q", "-m", "rescuer lands the crashed session's work"], repo)

    _write_touched(repo, "sess-crashed", [_touch_line("T", "rescued.txt")])
    _write_meta(repo, "sess-crashed", live=True)
    _write_meta(repo, "sess-caller", live=True)

    result = _call({
        "worktree_root": str(repo),
        "paths": ["rescued.txt"],
        "message": "second, unrelated commit over the now-clean path",
        "session_id": "sess-caller",
    })

    # The claim gate must not be what stops this call: "rescued.txt" is
    # already committed by the rescuer, so a re-invocation over the same
    # (now-clean) path is the benign already-committed no-op (AC7,
    # `_classify_uncommitted`'s idempotency case) -- NOT a claim-conflict
    # `error`. Pre-C5 the mere existence of `sess-crashed`'s live claim
    # would have refused this outright with a claim-conflict `error`.
    assert "error" not in result
    assert result.get("reason") == "empty-commit-set"
    assert result["commit_failed"] is False


def test_rescue_commit_case_alongside_a_genuinely_separate_clean_path(tmp_path):
    """Same rescue-commit scenario as above, but the caller's pathspec
    ALSO includes a second, genuinely dirty, unclaimed path -- proves the
    gate lets a real commit land, not merely produces a benign no-op,
    while the previously-claimed-and-now-clean path rides along without
    being refused."""
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "rescued.txt", "v1\n")
    _dirty_file(repo, "rescued.txt", "v2\n")
    _git(["add", "rescued.txt"], repo)
    _git(["commit", "-q", "-m", "rescuer lands the crashed session's work"], repo)
    _seed_and_commit_file(repo, "unrelated.txt", "v1\n")
    _dirty_file(repo, "unrelated.txt", "v2\n")

    _write_touched(repo, "sess-crashed", [_touch_line("T", "rescued.txt")])
    _write_meta(repo, "sess-crashed", live=True)
    _write_meta(repo, "sess-caller", live=True)

    result = _call({
        "worktree_root": str(repo),
        "paths": ["rescued.txt", "unrelated.txt"],
        "message": "a real commit, riding alongside the now-clean rescued path",
        "session_id": "sess-caller",
    })

    assert result["committed"] is True
    assert "error" not in result


# ---------------------------------------------------------------------------
# Genuine conflict: live peer claims it, AND it is actually dirty
# ---------------------------------------------------------------------------


def test_refuses_when_claimant_live_and_path_dirty(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "contested.txt", "v1\n")
    _dirty_file(repo, "contested.txt", "v2\n")  # left dirty, no rescue commit

    _write_touched(repo, "sess-peer", [_touch_line("T", "contested.txt")])
    _write_meta(repo, "sess-peer", live=True)
    _write_meta(repo, "sess-caller", live=True)

    result = _call({
        "worktree_root": str(repo),
        "paths": ["contested.txt"],
        "message": "steal a live peer's still-dirty file",
        "session_id": "sess-caller",
    })

    assert result["committed"] is False
    assert "contested.txt" in result["error"]
    assert "sess-peer" in result["error"]


# ---------------------------------------------------------------------------
# Unanswerable-path: fails closed PER PATH, sibling proceeds
# ---------------------------------------------------------------------------


def test_dirty_status_unanswerable_fails_closed_for_that_path_only(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "contested.txt", "v1\n")
    _seed_and_commit_file(repo, "free.txt", "v1\n")
    _dirty_file(repo, "contested.txt", "v2\n")
    _dirty_file(repo, "free.txt", "v2\n")

    _write_touched(repo, "sess-peer", [_touch_line("T", "contested.txt")])
    _write_meta(repo, "sess-peer", live=True)
    _write_meta(repo, "sess-caller", live=True)

    # Force the C5 dirtiness probe itself (never claim_index.lookup, which
    # this test leaves untouched) to fail closed -- simulates git declining
    # to answer the pathspec-scoped status call.
    real_git = scoped_git_commit.git_native._git

    def _failing_status(args, *, cwd, **kwargs):
        if "status" in args:
            from coordinator_core.ops.ceremony.git_native import GitResult

            return GitResult(returncode=1, stdout="", stderr="simulated failure")
        return real_git(args, cwd=cwd, **kwargs)

    monkeypatch.setattr(scoped_git_commit.git_native, "_git", _failing_status)

    result = _call({
        "worktree_root": str(repo),
        "paths": ["contested.txt", "free.txt"],
        "message": "dirtiness probe cannot answer",
        "session_id": "sess-caller",
    })

    assert result["committed"] is False
    assert "contested.txt" in result["error"]
    assert "free.txt" not in result["error"]


# ---------------------------------------------------------------------------
# Touched-then-reverted-never-committed: the claim outlives a clean commit,
# and must -- nothing here retires it
# ---------------------------------------------------------------------------


def test_touched_then_reverted_never_committed_survives_a_clean_commit(tmp_path):
    """A session touches a file, reverts the edit byte-for-byte, and never
    commits. The touch log recorded the touch; nothing recorded an
    un-touch, and the session stays LIVE (no dead-holder sweep will ever
    reach it) -- reported against a real live session on
    `coordinator/bin/session-claim-cli`, an observed state, not a
    hypothetical.

    The path itself is CLEAN (the revert leaves nothing for `git status` to
    report), so C5's conjunction ("live claimant AND dirty") fails and an
    unrelated commit over that path must PROCEED -- same shape as the
    rescue-commit cases above. What makes this case distinct enough to earn
    its own test: unlike every rescue-commit case, NOBODY ever committed
    over the touched path, so there is no artifact-release event of any
    kind that could have retired `sess-touched`'s claim. If anything in
    this call path ever cleared claim records on a clean-but-uncommitted
    path, this is the case that would catch it -- so this test asserts
    BOTH halves: the commit proceeds, AND the claim record is still there
    afterward. Asserting only the first half would also pass if something
    had wrongly cleared the record, which is a different and unintended
    behaviour."""
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "touched.txt", "v1\n")
    _seed_and_commit_file(repo, "unrelated.txt", "v1\n")

    # sess-touched touches touched.txt, edits it, then reverts the edit
    # byte-for-byte before ever committing -- the touch log only ever
    # records the touch, never an un-touch.
    (repo / "touched.txt").write_text("v2 -- mid-edit\n", encoding="utf-8")
    (repo / "touched.txt").write_text("v1\n", encoding="utf-8")  # reverted

    _dirty_file(repo, "unrelated.txt", "v2\n")  # the real, unrelated commit

    _write_touched(repo, "sess-touched", [_touch_line("T", "touched.txt")])
    _write_meta(repo, "sess-touched", live=True)
    _write_meta(repo, "sess-caller", live=True)

    result = _call({
        "worktree_root": str(repo),
        "paths": ["touched.txt", "unrelated.txt"],
        "message": "unrelated commit over a reverted-but-still-claimed path",
        "session_id": "sess-caller",
    })

    assert result["committed"] is True
    assert "error" not in result

    claimants = claim_index.lookup(["touched.txt"], cwd=str(repo)).get(
        "touched.txt", []
    )
    assert "sess-touched" in claimants, (
        "the touch claim on 'touched.txt' must survive a clean, unrelated "
        "commit -- nothing in this call path is entitled to clear it, "
        "since nobody ever committed over the touched path"
    )


# ---------------------------------------------------------------------------
# EOL-phantom: stat-stale porcelain dirt with no real diff is CLEAN
# ---------------------------------------------------------------------------


def test_phantom_dirty_path_with_live_claimant_proceeds(tmp_path):
    """A tracked-unstaged porcelain line with no real `git diff` content
    (the Git-for-Windows stat-staleness artifact `dirty_tree_gate` already
    filters) must be treated as CLEAN by C5's own phantom filter, even
    though a live peer claims the path."""
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "phantom.txt", "v1\n")

    # Synthesize a phantom: touch mtime/content-neutral rewrite that git
    # still reports as tracked-unstaged (X==' ', Y=='M') but for which
    # `git diff --name-only` reports nothing, by writing back the EXACT
    # committed bytes through a path that still flips the stat cache --
    # writing identical content is deterministic and portable, unlike
    # relying on OS-level mtime granularity tricks.
    (repo / "phantom.txt").write_text("v1\n", encoding="utf-8")

    probe = subprocess.run(
        ["git", "status", "--porcelain", "--", "phantom.txt"],
        cwd=str(repo), capture_output=True, text=True,
        **no_console_creationflags(),
    )
    diff_probe = subprocess.run(
        ["git", "diff", "--name-only", "--", "phantom.txt"],
        cwd=str(repo), capture_output=True, text=True,
        **no_console_creationflags(),
    )
    if not probe.stdout.strip() or diff_probe.stdout.strip():
        pytest.skip(
            "this git/OS combination does not reproduce the stat-staleness "
            "phantom -- nothing to assert the filter against"
        )

    _write_touched(repo, "sess-peer", [_touch_line("T", "phantom.txt")])
    _write_meta(repo, "sess-peer", live=True)
    _write_meta(repo, "sess-caller", live=True)

    result = _call({
        "worktree_root": str(repo),
        "paths": ["phantom.txt"],
        "message": "phantom-dirty path with a live claimant",
        "session_id": "sess-caller",
    })

    assert result["committed"] is True
    assert "error" not in result
