"""
coordinator_core.ops.memo.tests.test_memo_transition_commit_ownership —
DR-273 regression coverage: memo.transition takes commit ownership of its
own terminal write.

Coverage:
  (1) A successful claim/action/resolve write lands a real git commit, scoped
      to ONLY the memo path (never a bare or broad pathspec).
  (2) A peer's unrelated dirty file in the same shared tree is NOT swept into
      that commit — proves the explicit-pathspec form, not `git add -A`/`.`.
  (3) A GENUINE idempotent no-op (memo already at rest, clean in git — this
      call never wrote anything) does NOT produce a spurious commit.
  (4) A STRANDED write (a prior invocation wrote the verb's terminal state to
      disk but crashed/died before its own follow-up commit landed — tracked,
      dirty) is RESUMED: committed on re-invocation, reported distinguishably
      via "resumed": True, never silently treated as a no-op (Defect 2, C5 of
      docs/plans/2026-08-06-writer-side-commit-ownership-lock-gap.md).
  (5) AC10 red-proof: a dirty-tracked memo whose content does NOT actually
      carry the caller's expected terminal state fails loud, never silently
      committed as a resume of something it never wrote.

Spec backlink: docs/decisions/DR-273-memo-transition-commit-ownership.md
Spec backlink: pln-writer-side-commit-ownership-c-845b25 § C5
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.memo_transition as _memo_mod
from coordinator_core.ops.memo_transition import _action, _claim, _release, _resolve
from coordinator_core.win_portability import no_console_creationflags

# Real git spawn is load-bearing: this file asserts the actual `git commit`
# lands, scoped to only the memo path (never a bare/broad pathspec) — the
# assertion IS git's own commit/status output, not a mock of it. Per-test
# tmp repos stay per-test: each case's dirty/committed state must not leak.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git_init(path: Path) -> None:
    """Initialise a bare-minimum git repo so git rev-parse --show-toplevel works,
    and every subsequent commit in the repo has a working identity."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True, **no_console_creationflags())
    (path / ".gitkeep").touch()
    subprocess.run(["git", "-C", str(path), "add", ".gitkeep"], check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init", "--allow-empty-message"],
        check=True, capture_output=True,
        env={**__import__("os").environ, "GIT_AUTHOR_NAME": "test",
             "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "test",
             "GIT_COMMITTER_EMAIL": "t@t"},
        **no_console_creationflags(),
    )


def _head_sha(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )
    return result.stdout.strip()


def _commit_files(repo: Path, sha: str) -> list[str]:
    """Files touched by `sha`, relative to `repo` -- what the commit actually covers."""
    result = subprocess.run(
        ["git", "-C", str(repo), "diff-tree", "--no-commit-id", "--name-only", "-r", sha],
        check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )
    return [line for line in result.stdout.splitlines() if line]


def _dirty_paths(repo: Path) -> list[str]:
    """Worktree-dirty (unstaged or untracked) paths, relative to `repo`."""
    result = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )
    return [line[3:] for line in result.stdout.splitlines() if line]


def _fm_dict(memo_path: str) -> dict:
    text = Path(memo_path).read_text(encoding="utf-8")
    split = _memo_mod.split_frontmatter(text)
    return yaml.safe_load(split.fm_text) or {}


_OPEN_FIXTURE = """\
---
kind: fyi
status: open
from: sender-session
summary: A test memo.
created: 2026-06-01
---
"""

_IN_PROGRESS_FIXTURE = """\
---
kind: fyi
status: in_progress
picked_up_at: '2026-01-02T10:00:00Z'
picked_up_by: session-test
from: sender-session
summary: A test memo.
created: 2026-06-01
---
"""

_ACTIONED_FIXTURE = """\
---
kind: fyi
status: actioned
picked_up_at: '2026-01-02T10:00:00Z'
picked_up_by: session-test
decision: declined
from: sender-session
summary: A test memo.
created: 2026-06-01
---
"""


def _setup_memo(tmp_path: Path, content: str, *, name: str = "memo.md") -> tuple[Path, str]:
    """Create a git repo + a TRACKED, CLEAN memo under cross-repo/inbox/, committed
    immediately. Return (repo, memo path str).

    Amended (C5, docs/plans/2026-08-06-writer-side-commit-ownership-lock-gap.md):
    the memo is now committed at setup time rather than left untracked. This mirrors
    production reality (a memo is always landed by memo_send.py's own commit before
    any transition verb ever runs on it) and is required by
    `git_native.commit_authored_content`'s own containment guard, which refuses to
    commit a path absent from HEAD (that entrypoint is built for in-place mutation
    of an EXISTING reserved-noun file, never for creating a new one) -- an untracked
    fixture memo would fail every real-write test below with commit_authored_content
    routed in via Defect 1's fix, not just the no-op/resume tests this chunk adds.
    """
    repo = tmp_path / "repo"
    if not repo.exists():
        _git_init(repo)
    inbox = repo / "cross-repo" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    memo = inbox / name
    memo.write_text(content, encoding="utf-8")
    relpath = str(memo.relative_to(repo))
    subprocess.run(["git", "-C", str(repo), "add", relpath], check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", f"seed {relpath}"],
        check=True, capture_output=True,
        env={**__import__("os").environ, "GIT_AUTHOR_NAME": "test",
             "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "test",
             "GIT_COMMITTER_EMAIL": "t@t"},
        **no_console_creationflags(),
    )
    return repo, str(memo)


def _setup_stranded_write(
    tmp_path: Path, pre_content: str, terminal_content: str, *, name: str = "memo.md"
) -> tuple[Path, str]:
    """Simulate a prior invocation that wrote a verb's terminal state to disk and then
    crashed/died BEFORE its own follow-up commit landed.

    ``pre_content`` is committed as the memo's last real commit (what HEAD carries,
    via ``_setup_memo``); ``terminal_content`` is then written straight to the
    worktree WITHOUT any further git add/commit -- a tracked, uncommitted (dirty)
    modification, exactly the shape ``_memo_path_dirty`` is built to detect. Return
    (repo, memo path str).
    """
    repo, memo = _setup_memo(tmp_path, pre_content, name=name)
    Path(memo).write_text(terminal_content, encoding="utf-8")
    return repo, memo


def _setup_clean_terminal_memo(
    tmp_path: Path, terminal_content: str, *, name: str = "memo.md"
) -> tuple[Path, str]:
    """A TRACKED, CLEAN memo already committed at its terminal state -- the genuine
    no-op shape: this call never wrote anything, so there is nothing stranded to
    resume. Return (repo, memo path str)."""
    return _setup_memo(tmp_path, terminal_content, name=name)


# ---------------------------------------------------------------------------
# (1) Successful writes are committed, scoped to the memo path only.
# ---------------------------------------------------------------------------

class TestTerminalWriteIsCommitted:
    def test_claim_commits_only_the_memo_path(self, tmp_path):
        repo, memo = _setup_memo(tmp_path, _OPEN_FIXTURE)
        before_sha = _head_sha(repo)

        result = _claim(memo, "sess-1", "2026-07-26T00:00:00Z")

        assert result["exit_code"] == 0
        assert result["applied"] is True

        after_sha = _head_sha(repo)
        assert after_sha != before_sha, "claim did not create a new commit"

        touched = _commit_files(repo, after_sha)
        assert touched == ["cross-repo/inbox/memo.md"]

        # The write is no longer left dirty in the worktree -- it's committed.
        assert _dirty_paths(repo) == []

    def test_action_commits_only_the_memo_path(self, tmp_path):
        repo, memo = _setup_memo(tmp_path, _IN_PROGRESS_FIXTURE)
        before_sha = _head_sha(repo)

        result = _action(memo, {"decision": "declined"})

        assert result["exit_code"] == 0
        assert result["applied"] is True

        after_sha = _head_sha(repo)
        assert after_sha != before_sha

        touched = _commit_files(repo, after_sha)
        assert touched == ["cross-repo/inbox/memo.md"]
        assert _dirty_paths(repo) == []

    def test_resolve_commits_only_the_memo_path(self, tmp_path):
        repo, memo = _setup_memo(tmp_path, _OPEN_FIXTURE)
        before_sha = _head_sha(repo)

        result = _resolve(
            memo, "sess-1", "2026-07-26T00:00:00Z",
            {"decision": "accepted", "realized_by": "abc1234"},
        )

        assert result["exit_code"] == 0
        assert result["applied"] is True

        after_sha = _head_sha(repo)
        assert after_sha != before_sha

        touched = _commit_files(repo, after_sha)
        assert touched == ["cross-repo/inbox/memo.md"]
        assert _dirty_paths(repo) == []

        fm_dict = _fm_dict(memo)
        assert fm_dict["status"] == "actioned"


# ---------------------------------------------------------------------------
# (2) A peer's unrelated dirty file in the same tree is never swept in.
# ---------------------------------------------------------------------------

class TestPeerDirtyFileNotSwept:
    def test_action_leaves_peer_unrelated_dirty_file_untouched(self, tmp_path):
        repo, memo = _setup_memo(tmp_path, _IN_PROGRESS_FIXTURE)

        # A concurrent peer's own in-flight, unrelated dirty file in the same
        # shared tree -- must survive this op's commit untouched and unstaged.
        peer_file = repo / "cross-repo" / "inbox" / "peer-unrelated.md"
        peer_file.write_text("peer's own in-flight edit\n", encoding="utf-8")

        result = _action(memo, {"decision": "declined"})

        assert result["exit_code"] == 0
        assert result["applied"] is True

        after_sha = _head_sha(repo)
        touched = _commit_files(repo, after_sha)
        assert touched == ["cross-repo/inbox/memo.md"]
        assert "cross-repo/inbox/peer-unrelated.md" not in touched

        # The peer file is still dirty (untracked) -- never staged or committed.
        dirty = _dirty_paths(repo)
        assert "cross-repo/inbox/peer-unrelated.md" in dirty
        assert peer_file.read_text(encoding="utf-8") == "peer's own in-flight edit\n"

    def test_claim_leaves_peer_modified_tracked_file_untouched(self, tmp_path):
        repo, memo = _setup_memo(tmp_path, _OPEN_FIXTURE)

        # A peer's own tracked, already-committed file, dirtied (modified) by
        # the peer concurrently -- must not be absorbed into this op's commit.
        peer_file = repo / "cross-repo" / "inbox" / "peer-tracked.md"
        peer_file.write_text("original\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repo), "add", "cross-repo/inbox/peer-tracked.md"],
            check=True, capture_output=True,
            **no_console_creationflags(),
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "add peer file"],
            check=True, capture_output=True,
            env={**__import__("os").environ, "GIT_AUTHOR_NAME": "test",
                 "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "test",
                 "GIT_COMMITTER_EMAIL": "t@t"},
            **no_console_creationflags(),
        )
        peer_file.write_text("peer's own concurrent modification\n", encoding="utf-8")

        result = _claim(memo, "sess-1", "2026-07-26T00:00:00Z")

        assert result["exit_code"] == 0
        assert result["applied"] is True

        after_sha = _head_sha(repo)
        touched = _commit_files(repo, after_sha)
        assert touched == ["cross-repo/inbox/memo.md"]
        assert "cross-repo/inbox/peer-tracked.md" not in touched

        dirty = _dirty_paths(repo)
        assert "cross-repo/inbox/peer-tracked.md" in dirty
        assert peer_file.read_text(encoding="utf-8") == "peer's own concurrent modification\n"


# ---------------------------------------------------------------------------
# (3) A GENUINE idempotent no-op (never touched by this call, clean in git)
#     writes nothing and commits nothing.
# ---------------------------------------------------------------------------

class TestNoOpDoesNotCommit:
    def test_claim_noop_creates_no_commit(self, tmp_path):
        # Amended (C5, docs/plans/2026-08-06-writer-side-commit-ownership-lock-gap.md):
        # the original fixture left the memo UNTRACKED (never committed), which is
        # ambiguous with a resume candidate only by omission, not by an asserted
        # "clean" precondition -- this now commits the memo at its terminal state
        # first, so the no-commit assertion is against a genuinely CLEAN, tracked
        # file, distinct from the dirty-tracked resume shape covered below.
        repo, memo = _setup_clean_terminal_memo(tmp_path, _IN_PROGRESS_FIXTURE)
        before_sha = _head_sha(repo)

        # Already in_progress with the SAME session, and clean in git -- genuine no-op.
        result = _claim(memo, "session-test", "2026-07-26T00:00:00Z")

        assert result["exit_code"] == 0
        assert result["applied"] is False
        assert "resumed" not in result
        assert _head_sha(repo) == before_sha

    def test_action_already_actioned_noop_creates_no_commit(self, tmp_path):
        # Amended (C5) -- see test_claim_noop_creates_no_commit's comment.
        repo, memo = _setup_clean_terminal_memo(tmp_path, _ACTIONED_FIXTURE)
        before_sha = _head_sha(repo)

        result = _action(memo, {"decision": "declined"})

        assert result["exit_code"] == 0
        assert result["applied"] is False
        assert "resumed" not in result
        assert _head_sha(repo) == before_sha

    # Review: code-reviewer (Finding 4) — release/resolve share the identical
    # no-op shape as claim/action but had no coverage; asserted against real
    # git state (HEAD unmoved), not just the return field.
    def test_release_noop_creates_no_commit(self, tmp_path):
        # Amended (C5) -- see test_claim_noop_creates_no_commit's comment.
        repo, memo = _setup_clean_terminal_memo(tmp_path, _OPEN_FIXTURE)
        before_sha = _head_sha(repo)

        # Already open, and clean in git -- genuine no-op.
        result = _release(memo)

        assert result["exit_code"] == 0
        assert result["applied"] is False
        assert "resumed" not in result
        assert _head_sha(repo) == before_sha

    def test_resolve_noop_creates_no_commit(self, tmp_path):
        # Amended (C5) -- see test_claim_noop_creates_no_commit's comment.
        repo, memo = _setup_clean_terminal_memo(tmp_path, _ACTIONED_FIXTURE)
        before_sha = _head_sha(repo)

        # Already actioned at the same disposition, and clean in git -- genuine no-op.
        result = _resolve(
            memo, "sess-1", "2026-07-26T00:00:00Z",
            {"decision": "declined"},
        )

        assert result["exit_code"] == 0
        assert result["applied"] is False
        assert "resumed" not in result
        assert _head_sha(repo) == before_sha


# ---------------------------------------------------------------------------
# (4) A STRANDED write (a prior invocation wrote the terminal state to disk
#     but crashed/died before its own follow-up commit landed -- tracked,
#     DIRTY) is RESUMED: committed on re-invocation, reported distinguishably
#     via "resumed": True (AC5), never silently treated as a no-op (Defect 2,
#     C5 of docs/plans/2026-08-06-writer-side-commit-ownership-lock-gap.md).
# ---------------------------------------------------------------------------

class TestStrandedWriteIsResumedAndCommitted:
    def test_claim_resumes_stranded_write(self, tmp_path):
        repo, memo = _setup_stranded_write(tmp_path, _OPEN_FIXTURE, _IN_PROGRESS_FIXTURE)
        before_sha = _head_sha(repo)
        assert "cross-repo/inbox/memo.md" in _dirty_paths(repo)

        result = _claim(memo, "session-test", "2026-07-26T00:00:00Z")

        assert result["exit_code"] == 0
        assert result["applied"] is False
        assert result["resumed"] is True
        assert "commit_sha" in result

        after_sha = _head_sha(repo)
        assert after_sha != before_sha
        touched = _commit_files(repo, after_sha)
        assert touched == ["cross-repo/inbox/memo.md"]
        assert _dirty_paths(repo) == []

    def test_action_resumes_stranded_write(self, tmp_path):
        repo, memo = _setup_stranded_write(tmp_path, _IN_PROGRESS_FIXTURE, _ACTIONED_FIXTURE)
        before_sha = _head_sha(repo)
        assert "cross-repo/inbox/memo.md" in _dirty_paths(repo)

        result = _action(memo, {"decision": "declined"})

        assert result["exit_code"] == 0
        assert result["applied"] is False
        assert result["resumed"] is True
        assert "commit_sha" in result

        after_sha = _head_sha(repo)
        assert after_sha != before_sha
        touched = _commit_files(repo, after_sha)
        assert touched == ["cross-repo/inbox/memo.md"]
        assert _dirty_paths(repo) == []

    def test_release_resumes_stranded_write(self, tmp_path):
        repo, memo = _setup_stranded_write(tmp_path, _IN_PROGRESS_FIXTURE, _OPEN_FIXTURE)
        before_sha = _head_sha(repo)
        assert "cross-repo/inbox/memo.md" in _dirty_paths(repo)

        result = _release(memo)

        assert result["exit_code"] == 0
        assert result["applied"] is False
        assert result["resumed"] is True
        assert "commit_sha" in result

        after_sha = _head_sha(repo)
        assert after_sha != before_sha
        touched = _commit_files(repo, after_sha)
        assert touched == ["cross-repo/inbox/memo.md"]
        assert _dirty_paths(repo) == []

    def test_resolve_resumes_stranded_write(self, tmp_path):
        repo, memo = _setup_stranded_write(tmp_path, _OPEN_FIXTURE, _ACTIONED_FIXTURE)
        before_sha = _head_sha(repo)
        assert "cross-repo/inbox/memo.md" in _dirty_paths(repo)

        result = _resolve(
            memo, "sess-1", "2026-07-26T00:00:00Z",
            {"decision": "declined"},
        )

        assert result["exit_code"] == 0
        assert result["applied"] is False
        assert result["resumed"] is True
        assert "commit_sha" in result

        after_sha = _head_sha(repo)
        assert after_sha != before_sha
        touched = _commit_files(repo, after_sha)
        assert touched == ["cross-repo/inbox/memo.md"]
        assert _dirty_paths(repo) == []


# ---------------------------------------------------------------------------
# (5) AC10 red-proof: a dirty-tracked memo whose on-disk content does NOT
#     actually carry the CALLER's expected terminal state must FAIL LOUD,
#     never be silently committed as a "resume" of something it never wrote.
# ---------------------------------------------------------------------------

class TestResumeNeverCommitsUnvalidatedContent:
    def test_action_dirty_memo_with_mismatched_disposition_fails_loud_not_committed(
        self, tmp_path
    ):
        # The memo is dirty-tracked (a real stranded-write SHAPE) and its status
        # IS "actioned" -- but with a DIFFERENT decision than this call requests,
        # so it is not actually AT this call's expected terminal state. The
        # existing already-actioned-mismatch guard (_handle_already_actioned)
        # must still fire loud here -- the dirty-tree resume path must never
        # short-circuit past it and commit whatever happens to be on disk.
        mismatched_actioned = _ACTIONED_FIXTURE.replace("decision: declined", "decision: accepted")
        mismatched_actioned = mismatched_actioned.replace(
            "decision: accepted", "decision: accepted\nrealized_by: abc1234"
        )
        repo, memo = _setup_stranded_write(
            tmp_path, _IN_PROGRESS_FIXTURE, mismatched_actioned
        )
        before_sha = _head_sha(repo)
        assert "cross-repo/inbox/memo.md" in _dirty_paths(repo)

        result = _action(memo, {"decision": "declined"})

        assert result["exit_code"] == 1
        assert result["applied"] is False
        assert "error" in result
        # No commit landed -- the stranded, mismatched write is left exactly as
        # dirty/uncommitted as it was found, never silently swept into a commit.
        assert _head_sha(repo) == before_sha
        assert "cross-repo/inbox/memo.md" in _dirty_paths(repo)
