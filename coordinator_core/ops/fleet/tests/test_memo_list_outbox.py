"""
Tests for coordinator_core.ops.fleet.memo_list_outbox — memo.list_outbox
COMPUTE_ONLY UDS op.

Test surface (docs/plans/2026-07-21-memo-tool-rebuild-full-ownership.md):
  - setup-error envelope on bad params (missing/wrong-typed dry_run, dry_run:false)
  - drafts written by memo.draft are enumerated with correct fields
  - empty/absent outbox -> empty candidate list, no error
  - missing repo_root -> setup-error envelope
  - store-less/no-write invariant (mirrors memo_list.py's TestNoMemoIndex)
  - deterministic sort order (by filename)
  - malformed draft file degrades gracefully (still listed, fields None, note set)

Harness: asyncio.run() in sync test functions — no pytest-asyncio dependency.
Pattern: mirrors test_memo_draft.py's real-git-repo sender fixture.

Spec backlink: docs/plans/2026-07-21-memo-tool-rebuild-full-ownership.md
"""

from __future__ import annotations

import asyncio
import subprocess
import types
from pathlib import Path

from coordinator_core.ops.fleet.memo_draft import _memo_draft
from coordinator_core.ops.fleet.memo_list_outbox import (
    _MODE,
    _memo_list_outbox,
    _validate_list_outbox_params,
)


def _run(coro):
    """Run async coroutine synchronously — no pytest-asyncio dependency."""
    return asyncio.run(coro)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + list(args), cwd=str(repo), capture_output=True, check=check,
    )


def _make_sender_git_repo(tmp_path: Path) -> Path:
    """Minimal git repo to serve as the caller repo (common_dir -> worktree)."""
    root = tmp_path / "sender-repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@claude-klabauter.test")
    _git(root, "config", "user.name", "ClaudeKlabauterTest")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", ".gitkeep")
    _git(root, "commit", "-m", "init")
    return root


def _base_draft_params(**overrides) -> dict:
    params = {
        "dry_run": False,
        "topic": "some-topic",
        "to": "example-retrieval-repo-em",
        "title": "A draft memo",
    }
    params.update(overrides)
    return params


def _snapshot(tmp_path: Path) -> set:
    """Return the set of all paths (files + dirs) under tmp_path, for a
    before/after no-write comparison."""
    return {str(p) for p in tmp_path.rglob("*")}


# ===========================================================================
# 1. setup-error envelope on bad params
# ===========================================================================

class TestSetupErrorEnvelope:
    def test_dry_run_missing(self):
        result = _validate_list_outbox_params({})
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_dry_run_not_bool(self):
        result = _validate_list_outbox_params({"dry_run": "yes"})
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_dry_run_false_rejected(self):
        """memo.list_outbox has no act mode — dry_run:false is a setup error."""
        result = _validate_list_outbox_params({"dry_run": False})
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_handler_dry_run_false_returns_setup_error(self, tmp_path):
        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        result = _run(_memo_list_outbox({"dry_run": False}, repo_root=common_dir))
        assert result["exit_code"] == 1
        assert result["dry_run"] is False

    def test_missing_repo_root_is_setup_error(self):
        result = _run(_memo_list_outbox({"dry_run": True}, repo_root=None))
        assert result["exit_code"] == 1


# ===========================================================================
# 2. Empty/absent outbox
# ===========================================================================

class TestEmptyOutbox:
    def test_no_outbox_dir_yields_empty_candidates(self, tmp_path):
        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"

        result = _run(_memo_list_outbox({"dry_run": True}, repo_root=common_dir))

        assert result["exit_code"] == 0
        assert result["mode"] == _MODE
        assert result["dry_run"] is True
        assert result["candidates"] == []
        assert result["acted"] == []
        assert result["skipped"] == []
        assert result["failed"] == []

    def test_empty_outbox_dir_yields_empty_candidates(self, tmp_path):
        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        (sender / "state" / "memo-outbox").mkdir(parents=True)

        result = _run(_memo_list_outbox({"dry_run": True}, repo_root=common_dir))

        assert result["exit_code"] == 0
        assert result["candidates"] == []


# ===========================================================================
# 3. Drafts written by memo.draft are enumerated with correct fields
# ===========================================================================

class TestEnumeratesDrafts:
    def test_single_draft_enumerated_with_fields(self, tmp_path):
        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"

        draft_result = _run(_memo_draft(
            _base_draft_params(kind="ask", summary="a summary"), repo_root=common_dir,
        ))
        assert draft_result["exit_code"] == 0

        result = _run(_memo_list_outbox({"dry_run": True}, repo_root=common_dir))

        assert result["exit_code"] == 0
        assert len(result["candidates"]) == 1
        candidate = result["candidates"][0]
        assert candidate["filename"] == "some-topic.md"
        assert candidate["topic"] == "some-topic"
        assert candidate["to"] == '"example-retrieval-repo-em"'
        assert candidate["status"] == "draft"
        assert candidate["summary"] == '"a summary"'
        assert candidate["kind"] == '"ask"'
        assert candidate["title"] == '"A draft memo"'
        assert candidate["from"] == '"claude-klabauter-engine"'
        assert candidate["note"] is None

    def test_multiple_drafts_enumerated_sorted_by_filename(self, tmp_path):
        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"

        for topic in ("zeta-topic", "alpha-topic", "mid-topic"):
            draft_result = _run(_memo_draft(
                _base_draft_params(topic=topic), repo_root=common_dir,
            ))
            assert draft_result["exit_code"] == 0

        result = _run(_memo_list_outbox({"dry_run": True}, repo_root=common_dir))

        assert result["exit_code"] == 0
        filenames = [c["filename"] for c in result["candidates"]]
        assert filenames == sorted(filenames)
        assert filenames == ["alpha-topic.md", "mid-topic.md", "zeta-topic.md"]

    def test_malformed_draft_still_listed_with_note(self, tmp_path):
        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        outbox = sender / "state" / "memo-outbox"
        outbox.mkdir(parents=True)
        (outbox / "broken.md").write_text("not valid frontmatter at all", encoding="utf-8")

        result = _run(_memo_list_outbox({"dry_run": True}, repo_root=common_dir))

        assert result["exit_code"] == 0
        assert len(result["candidates"]) == 1
        candidate = result["candidates"][0]
        assert candidate["filename"] == "broken.md"
        assert candidate["topic"] == "broken"
        assert candidate["note"] is not None
        assert candidate["title"] is None
        assert candidate["status"] is None


# ===========================================================================
# 4. No-write proof
# ===========================================================================

class TestNoWriteProof:
    def test_enumeration_leaves_filesystem_unchanged(self, tmp_path):
        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"

        draft_result = _run(_memo_draft(_base_draft_params(), repo_root=common_dir))
        assert draft_result["exit_code"] == 0

        before = _snapshot(tmp_path)
        _run(_memo_list_outbox({"dry_run": True}, repo_root=common_dir))
        after = _snapshot(tmp_path)

        assert after == before, (
            "memo.list_outbox must not write/create anything on disk. "
            f"New paths: {after - before}"
        )


# ===========================================================================
# 5. Store-less-ness architecture test (mirrors memo_list.py's TestNoMemoIndex)
# ===========================================================================

class TestNoMemoIndex:
    def test_no_memo_index(self):
        import coordinator_core.ops.fleet.memo_list_outbox as memo_list_outbox_mod

        module_globals = {
            name: val
            for name, val in vars(memo_list_outbox_mod).items()
            if not name.startswith("__")
        }
        mutable_collections = {
            name: val
            for name, val in module_globals.items()
            if isinstance(val, (dict, list, set))
            and not isinstance(val, types.ModuleType)
        }

        assert mutable_collections == {}, (
            f"memo_list_outbox module MUST NOT contain module-level mutable collections "
            f"(dict/list/set) — Q-d store-less-ness invariant. "
            f"Found violating names: {sorted(mutable_collections.keys())}"
        )

    def test_handler_calls_do_not_mutate_module_state(self, tmp_path):
        import coordinator_core.ops.fleet.memo_list_outbox as memo_list_outbox_mod

        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        draft_result = _run(_memo_draft(_base_draft_params(), repo_root=common_dir))
        assert draft_result["exit_code"] == 0

        def _mutable_module_names():
            return frozenset(
                name
                for name, val in vars(memo_list_outbox_mod).items()
                if isinstance(val, (dict, list, set)) and not name.startswith("__")
            )

        names_before = _mutable_module_names()

        _run(_memo_list_outbox({"dry_run": True}, repo_root=common_dir))
        _run(_memo_list_outbox({"dry_run": True}, repo_root=common_dir))

        names_after = _mutable_module_names()

        assert names_after == names_before, (
            f"memo_list_outbox module must not accumulate new module-level mutable state "
            f"across handler calls (Q-d store-less-ness invariant). "
            f"Names that appeared after handler calls: {sorted(names_after - names_before)}"
        )
