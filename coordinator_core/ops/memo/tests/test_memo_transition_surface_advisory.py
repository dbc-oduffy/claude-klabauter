"""
coordinator_core.ops.memo.tests.test_memo_transition_surface_advisory —
P080-C2: `memo.transition` wiring of the additive `surface_advisory` reply key.

Spec: docs/plans/2026-09-11-realized-by-vs-declared-surface-reconcile.md (P080-C2, AC4/AC5/AC11)

Coverage:
  (1) Wiring — a `resolve` call that stamps `realized_by` against a matching declared
      surface returns `surface_advisory: {"verdict": "ok", ...}` additively, with the
      rest of the envelope (exit_code/applied/message/commit_sha) unchanged; removing
      the key yields the pre-C2 reply shape.
  (2) The resumed stranded-write branch (`_resume_probe_and_commit`) carries the same
      additive key under the same rule.
  (3) The `commit_error` branch (`_commit_terminal_write` fails its follow-up commit)
      still carries the key — AC4's clause on this.
  (4) A `correct_realization` call that supplies only `decision_note` (no `realized_by`)
      writes no SHA and carries no advisory.
  (5) AC11 — both `_apply_realization_correction` clause texts: the moved-SHA
      "realized_by superseded — was <sha>" wording (byte-for-byte, unchanged) and the
      unchanged-SHA "decision_note corrected" wording.

test_memo_transition_unit.py is unedited and stays green (not re-run here).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import coordinator_core.ops.memo_transition as _memo_mod
from coordinator_core.ops.ceremony.git_native import GitResult
from coordinator_core.ops.memo_transition import _action, _resolve
from coordinator_core.win_portability import no_console_creationflags

# Real git spawns are load-bearing: the advisory's own touched-path read
# (surface_advisory._touched_paths) judges a REAL commit's real diff — no mock
# stands in for git's own `git log` output.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_ENV = {
    **__import__("os").environ,
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t",
}


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, check=True, capture_output=True, text=True, env=_ENV,
        cwd=str(cwd) if cwd else None, **no_console_creationflags(),
    )


def _git_init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", str(path)])
    (path / ".gitkeep").touch()
    _run(["git", "-C", str(path), "add", ".gitkeep"])
    _run(["git", "-C", str(path), "commit", "-m", "init", "--allow-empty-message"])


def _head_sha(repo: Path) -> str:
    return _run(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()


def _fm_dict(memo_path) -> dict:
    text = Path(memo_path).read_text(encoding="utf-8")
    split = _memo_mod.split_frontmatter(text)
    return yaml.safe_load(split.fm_text) or {}


def _setup_memo(tmp_path: Path, content: str, *, name: str = "memo.md") -> tuple[Path, str]:
    """Real git repo + a TRACKED, CLEAN memo under cross-repo/inbox/, committed."""
    repo = tmp_path / "repo"
    if not repo.exists():
        _git_init(repo)
    inbox = repo / "cross-repo" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    memo = inbox / name
    memo.write_text(content, encoding="utf-8")
    relpath = str(memo.relative_to(repo))
    _run(["git", "-C", str(repo), "add", relpath])
    _run(["git", "-C", str(repo), "commit", "-m", f"seed {relpath}"])
    return repo, str(memo)


def _commit_matching_surface(repo: Path, declared_path: str) -> str:
    """Land a real commit in `repo` that touches `declared_path` — a commit
    `surface_advisory` should verdict "ok" against. Content is unique per call
    (a monotonic counter) so a second commit to the same path is never a no-op."""
    target = repo / declared_path
    target.parent.mkdir(parents=True, exist_ok=True)
    counter_path = repo / ".commit_counter"
    n = int(counter_path.read_text()) + 1 if counter_path.exists() else 1
    counter_path.write_text(str(n))
    target.write_text(f"payload {n}\n", encoding="utf-8")
    _run(["git", "-C", str(repo), "add", "--", declared_path, ".commit_counter"])
    _run(["git", "-C", str(repo), "commit", "-m", f"touch {declared_path} #{n}"])
    return _head_sha(repo)


_OPEN_FIXTURE_TEMPLATE = """\
---
kind: fyi
status: open
from: sender-session
surface: {surface}
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

_ACTIONED_FIXTURE_TEMPLATE = """\
---
kind: fyi
status: actioned
picked_up_at: '2026-01-02T10:00:00Z'
picked_up_by: session-test
decision: accepted
realized_by: {realized_by}
surface: {surface}
from: sender-session
summary: A test memo.
created: 2026-06-01
---
"""


# ---------------------------------------------------------------------------
# (1) Wiring — resolve stamps a matching-surface SHA, key attaches additively.
# ---------------------------------------------------------------------------

class TestWiring:
    def test_resolve_attaches_surface_advisory_ok(self, tmp_path):
        repo = tmp_path / "repo"
        _git_init(repo)
        sha = _commit_matching_surface(repo, "src/foo.py")

        repo2, memo = _setup_memo(
            tmp_path, _OPEN_FIXTURE_TEMPLATE.format(surface="src/foo.py"),
        )
        assert repo2 == repo

        result = _resolve(
            memo, "sess-1", "2026-07-26T00:00:00Z",
            {"decision": "accepted", "realized_by": sha},
        )

        assert result["exit_code"] == 0
        assert result["applied"] is True
        assert "surface_advisory" in result
        advisory = result["surface_advisory"]
        assert advisory["verdict"] == "ok"
        assert advisory["sha"] == sha.lower()
        assert advisory["declared"] == ["src/foo.py"]

        # Envelope otherwise unchanged — removing the key yields the pre-C2 shape.
        stripped = dict(result)
        del stripped["surface_advisory"]
        assert set(stripped.keys()) == {"exit_code", "applied", "message", "commit_sha"}

    def test_action_non_sha_realized_by_no_second_sha_check(self, tmp_path):
        """A valid-shape but non-SHA `realized_by` (the "path containing /" form
        the cross-field validator itself accepts) never reaches a verdict —
        `surface_advisory` (not this op) is the only place the shape check runs,
        so the key is simply absent, never a second, redundant rejection."""
        repo, memo = _setup_memo(tmp_path, _IN_PROGRESS_FIXTURE)

        result = _action(
            memo, {"decision": "accepted", "realized_by": "docs/plans/2026-06-23-foo.md"},
        )

        assert result["exit_code"] == 0
        assert "surface_advisory" not in result


# ---------------------------------------------------------------------------
# (2) Resumed stranded-write branch carries the advisory under the same rule.
# ---------------------------------------------------------------------------

class TestResumeBranch:
    def test_resumed_resolve_carries_surface_advisory(self, tmp_path):
        repo = tmp_path / "repo"
        _git_init(repo)
        sha = _commit_matching_surface(repo, "src/foo.py")

        pre = _OPEN_FIXTURE_TEMPLATE.format(surface="src/foo.py")
        terminal = _ACTIONED_FIXTURE_TEMPLATE.format(realized_by=sha, surface="src/foo.py")

        repo2, memo = _setup_memo(tmp_path, pre)
        assert repo2 == repo
        # Simulate a prior crashed invocation: write the terminal state to the
        # worktree WITHOUT committing — a tracked, dirty modification.
        Path(memo).write_text(terminal, encoding="utf-8")

        result = _resolve(
            memo, "sess-1", "2026-07-26T00:00:00Z",
            {"decision": "accepted", "realized_by": sha},
        )

        assert result["exit_code"] == 0
        assert result.get("resumed") is True
        assert "surface_advisory" in result
        assert result["surface_advisory"]["verdict"] == "ok"


# ---------------------------------------------------------------------------
# (3) commit_error branch still carries the advisory (AC4).
# ---------------------------------------------------------------------------

class TestCommitErrorBranch:
    def test_commit_failure_still_carries_advisory(self, tmp_path):
        repo = tmp_path / "repo"
        _git_init(repo)
        sha = _commit_matching_surface(repo, "src/foo.py")

        repo2, memo = _setup_memo(
            tmp_path, _OPEN_FIXTURE_TEMPLATE.format(surface="src/foo.py"),
        )
        assert repo2 == repo

        failing = GitResult(
            returncode=1, stdout="", stderr="simulated commit failure",
            worktree_excluded=(), cas_ref_relpath="HEAD",
        )
        with patch.object(_memo_mod.git_native, "commit_authored_content", return_value=failing):
            result = _resolve(
                memo, "sess-1", "2026-07-26T00:00:00Z",
                {"decision": "accepted", "realized_by": sha},
            )

        assert result["exit_code"] == 1
        assert "error" in result
        assert "surface_advisory" in result
        assert result["surface_advisory"]["verdict"] == "ok"


# ---------------------------------------------------------------------------
# (4) decision_note-only correction writes no SHA, carries no advisory.
# ---------------------------------------------------------------------------

_ACTIONED_DECLINED_FIXTURE = """\
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


class TestDecisionNoteOnlyCorrection:
    def test_decision_note_only_correction_no_advisory(self, tmp_path):
        """A `correct_realization` call whose params carry `decision_note` but
        no `realized_by` at all (only possible when `decision` is unchanged and
        `declined` — the one disposition that never requires `realized_by`,
        AC4/AC11's "decision_note-only correction") writes no SHA and carries
        no advisory key."""
        repo, memo = _setup_memo(tmp_path, _ACTIONED_DECLINED_FIXTURE)

        result = _action(
            memo,
            {
                "decision": "declined",
                "correct_realization": True,
                "decision_note": "clarifying the record",
            },
        )

        assert result["exit_code"] == 0
        assert "surface_advisory" not in result

        fm = _fm_dict(memo)
        assert "realized_by" not in fm
        assert "clarifying the record" in fm["decision_note"]


# ---------------------------------------------------------------------------
# (5) AC11 — both correction-clause texts.
# ---------------------------------------------------------------------------

class TestCorrectionClauseTexts:
    def test_unchanged_sha_gets_decision_note_corrected_clause(self, tmp_path):
        repo = tmp_path / "repo"
        _git_init(repo)
        sha = _commit_matching_surface(repo, "src/foo.py")

        actioned = _ACTIONED_FIXTURE_TEMPLATE.format(realized_by=sha, surface="src/foo.py")
        repo2, memo = _setup_memo(tmp_path, actioned)
        assert repo2 == repo

        result = _action(
            memo,
            {
                "decision": "accepted", "correct_realization": True, "realized_by": sha,
                "decision_note": "straightening out the record",
            },
        )

        assert result["exit_code"] == 0
        fm = _fm_dict(memo)
        assert "[correction " in fm["decision_note"]
        assert "decision_note corrected]" in fm["decision_note"]
        assert "superseded" not in fm["decision_note"]

    def test_moved_sha_gets_superseded_clause_byte_for_byte(self, tmp_path):
        repo = tmp_path / "repo"
        _git_init(repo)
        old_sha = _commit_matching_surface(repo, "src/foo.py")
        new_sha = _commit_matching_surface(repo, "src/foo.py")
        assert old_sha != new_sha

        actioned = _ACTIONED_FIXTURE_TEMPLATE.format(realized_by=old_sha, surface="src/foo.py")
        repo2, memo = _setup_memo(tmp_path, actioned)
        assert repo2 == repo

        result = _action(
            memo,
            {"decision": "accepted", "correct_realization": True, "realized_by": new_sha},
        )

        assert result["exit_code"] == 0
        fm = _fm_dict(memo)
        assert f"realized_by superseded — was {old_sha}]" in fm["decision_note"]
        assert "decision_note corrected" not in fm["decision_note"]
