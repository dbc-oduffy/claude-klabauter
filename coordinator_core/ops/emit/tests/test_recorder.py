"""Unit tests for coordinator_core.ops.emit.recorder (backlog.record MUTATING op).

Post-2026-07-07 per-repo-emission-cutover (Option A): single-repo semantics — one row per
emission (the emitting repo R only); no fleet walk; no sibling rows.

Covers:
  - _count_non_archived_yaml: archive-exclusion correctness, including nested archive/ paths
  - _machine_slug (via _slug.machine_slug): bash-parity cases
  - record(): single-repo semantics — exactly one row (R/state only), no sibling rows,
               append-not-overwrite, row shape, shard filename convention
  - Structural assertions: _parse_working_repos and _resolve_sibling_path must NOT exist
    in recorder (removed under Option A — C4b; not merely unexercised).

Spec backlink: docs/plans/2026-07-07-per-repo-emission-cutover.md § C4b
"""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

import coordinator_core.ops.emit.recorder as _recorder_module
from coordinator_core.ops.emit._slug import machine_slug
from coordinator_core.ops.emit.recorder import (
    _count_non_archived_yaml,
    record,
)
from coordinator_core.ops.emit.context import EmitContext


def _run(result):
    """Run an async coroutine synchronously, or pass a plain result through
    unchanged (2026-08-07: `_backlog_record` is now plain `def`)."""
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


# ---------------------------------------------------------------------------
# Structural assertions — removed helpers must NOT exist
# ---------------------------------------------------------------------------

class TestRemovedHelpers:
    """_parse_working_repos and _resolve_sibling_path must be absent from recorder (Option A)."""

    def test_parse_working_repos_does_not_exist(self) -> None:
        """_parse_working_repos must NOT be defined in recorder (fleet-walk helper, removed)."""
        assert not hasattr(_recorder_module, "_parse_working_repos"), (
            "_parse_working_repos still exists in recorder — it must be removed (Option A drop)"
        )

    def test_resolve_sibling_path_does_not_exist(self) -> None:
        """_resolve_sibling_path must NOT be defined in recorder (fleet-walk helper, removed)."""
        assert not hasattr(_recorder_module, "_resolve_sibling_path"), (
            "_resolve_sibling_path still exists in recorder — it must be removed (Option A drop)"
        )

    def test_yaml_not_imported(self) -> None:
        """The 'yaml' module must NOT be imported at the recorder module level (dead import)."""
        # Check that 'yaml' is not a module-level attribute of recorder (direct import would
        # create an attribute named 'yaml' on the module).
        assert not isinstance(getattr(_recorder_module, "yaml", None), types.ModuleType), (
            "'yaml' is still imported in recorder — it must be removed (Option A drop)"
        )

    def test_subprocess_not_imported(self) -> None:
        """The 'subprocess' module must NOT be imported at the recorder module level (dead import)."""
        assert not isinstance(getattr(_recorder_module, "subprocess", None), types.ModuleType), (
            "'subprocess' is still imported in recorder — it must be removed (Option A drop)"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(
    tmp_path: Path,
    *,
    hostname: str = "test-host",
    repo_name: str = "test-org/test-repo",
) -> EmitContext:
    """Build a minimal EmitContext pointing at tmp_path as both repo_root and central_state_root."""
    return EmitContext(
        repo_root=tmp_path,
        coordinator_root=tmp_path,
        central_state_root=tmp_path,
        git_branch="main",
        git_sha="abc1234567890123456789012345678901234567",
        git_sha_short="abc12345",
        observed_at="2026-07-07T00:00:00Z",
        hostname=hostname,
        repo_name=repo_name,
    )


# ---------------------------------------------------------------------------
# _count_non_archived_yaml: archive-exclusion correctness
# ---------------------------------------------------------------------------

class TestCountNonArchivedYaml:
    """_count_non_archived_yaml must count non-archived *.yaml and exclude archive/ paths."""

    def test_absent_subdir_returns_zero(self, tmp_path: Path) -> None:
        """When the subdir does not exist, returns 0 (graceful-absent)."""
        result = _count_non_archived_yaml(tmp_path, "bug-backlog")
        assert result == 0

    def test_counts_yaml_files(self, tmp_path: Path) -> None:
        """Plain .yaml files under subdir are counted."""
        subdir = tmp_path / "bug-backlog"
        subdir.mkdir()
        (subdir / "entry-a.yaml").write_text("id: a\n")
        (subdir / "entry-b.yaml").write_text("id: b\n")
        assert _count_non_archived_yaml(tmp_path, "bug-backlog") == 2

    def test_excludes_direct_archive_subdir(self, tmp_path: Path) -> None:
        """Files under a direct archive/ subdirectory are excluded."""
        subdir = tmp_path / "bug-backlog"
        subdir.mkdir()
        (subdir / "active.yaml").write_text("id: active\n")
        archive = subdir / "archive"
        archive.mkdir()
        (archive / "closed.yaml").write_text("id: closed\n")
        assert _count_non_archived_yaml(tmp_path, "bug-backlog") == 1

    def test_excludes_nested_archive_subdir(self, tmp_path: Path) -> None:
        """Files nested under archive/YYYY-MM/ are excluded (closure = git mv to archive/)."""
        subdir = tmp_path / "bug-backlog"
        subdir.mkdir()
        (subdir / "open.yaml").write_text("id: open\n")
        nested_archive = subdir / "archive" / "2026-06"
        nested_archive.mkdir(parents=True)
        (nested_archive / "old-entry.yaml").write_text("id: old\n")
        assert _count_non_archived_yaml(tmp_path, "bug-backlog") == 1

    def test_non_yaml_files_not_counted(self, tmp_path: Path) -> None:
        """Non-.yaml files are not counted."""
        subdir = tmp_path / "lessons"
        subdir.mkdir()
        (subdir / "note.txt").write_text("not yaml\n")
        (subdir / "lesson.yaml").write_text("id: lesson\n")
        assert _count_non_archived_yaml(tmp_path, "lessons") == 1

    def test_empty_subdir_returns_zero(self, tmp_path: Path) -> None:
        """An empty subdir returns 0."""
        subdir = tmp_path / "improvement-queue"
        subdir.mkdir()
        assert _count_non_archived_yaml(tmp_path, "improvement-queue") == 0

    def test_archive_only_subdir_returns_zero(self, tmp_path: Path) -> None:
        """A subdir with only archived files returns 0."""
        subdir = tmp_path / "bug-backlog"
        subdir.mkdir()
        archive = subdir / "archive" / "2026-05"
        archive.mkdir(parents=True)
        (archive / "a.yaml").write_text("id: a\n")
        (archive / "b.yaml").write_text("id: b\n")
        assert _count_non_archived_yaml(tmp_path, "bug-backlog") == 0


# ---------------------------------------------------------------------------
# machine_slug (from _slug.py): bash-parity cases
# ---------------------------------------------------------------------------

class TestMachineSlug:
    """machine_slug must mirror append-goal-event.sh's slug convention exactly."""

    def test_plain_hostname(self) -> None:
        """Plain alphanumeric hostname passes through unchanged."""
        assert machine_slug("myhost") == "myhost"

    def test_hostname_with_dots(self) -> None:
        """Dots are collapsed to a single dash (host.local → host-local)."""
        assert machine_slug("host.local") == "host-local"

    def test_hostname_with_multiple_dots(self) -> None:
        """Multiple dots in a row collapse to one dash."""
        assert machine_slug("a.b.c") == "a-b-c"

    def test_hostname_with_leading_digit(self) -> None:
        """Hostnames starting with a digit are valid slug characters."""
        assert machine_slug("1myhost") == "1myhost"

    def test_empty_string_returns_unknown(self) -> None:
        """Empty string input returns 'unknown'."""
        assert machine_slug("") == "unknown"

    def test_whitespace_only_returns_unknown(self) -> None:
        """Whitespace-only input returns 'unknown'."""
        assert machine_slug("   ") == "unknown"

    def test_uppercase_lowercased(self) -> None:
        """Uppercase characters are lowercased."""
        assert machine_slug("MyHost") == "myhost"

    def test_mixed_special_chars_collapsed(self) -> None:
        """Runs of non-[a-z0-9] characters collapse to a single dash."""
        assert machine_slug("host--name") == "host-name"

    def test_leading_trailing_dashes_stripped(self) -> None:
        """Leading and trailing dashes are stripped."""
        assert machine_slug(".host.") == "host"

    def test_none_uses_system_hostname(self) -> None:
        """None triggers socket.gethostname(); result is a non-empty string."""
        slug = machine_slug(None)
        assert isinstance(slug, str)
        assert slug  # non-empty
        assert slug == slug.lower()  # lowercased


# ---------------------------------------------------------------------------
# record(): single-repo semantics — no sibling rows, append-not-overwrite, shape
# ---------------------------------------------------------------------------

class TestRecord:
    """record() — single-repo end-to-end tests (Option A: no fleet walk, no sibling rows)."""

    def _record_with_ctx(self, ctx: EmitContext) -> dict:
        """Call record() with a pre-built EmitContext (bypasses resolve_context())."""
        return _recorder_module.record(ctx)

    def test_shard_created_on_first_call(self, tmp_path: Path) -> None:
        """record() creates the shard file on first call."""
        ctx = _make_ctx(tmp_path)
        result = self._record_with_ctx(ctx)
        shard = Path(result["shard"])
        assert shard.exists(), "shard file must be created by record()"
        assert shard.stat().st_size > 0

    def test_shard_filename_matches_slug_convention(self, tmp_path: Path) -> None:
        """Shard filename must be backlog-snapshots.<machine>.jsonl (D5 convention)."""
        ctx = _make_ctx(tmp_path, hostname="my.host.local")
        result = self._record_with_ctx(ctx)
        shard_name = Path(result["shard"]).name
        expected_machine = machine_slug("my.host.local")
        assert shard_name == f"backlog-snapshots.{expected_machine}.jsonl", (
            f"shard filename {shard_name!r} does not match convention "
            f"backlog-snapshots.{expected_machine}.jsonl"
        )

    def test_single_row_per_call(self, tmp_path: Path) -> None:
        """record() produces exactly one row per call (single-repo, no fleet walk)."""
        ctx = _make_ctx(tmp_path)
        result = self._record_with_ctx(ctx)
        assert len(result["rows"]) == 1, (
            f"Expected exactly 1 row (single-repo semantics); got {len(result['rows'])}"
        )

    def test_no_sibling_rows(self, tmp_path: Path) -> None:
        """No sibling rows appear even when a working-repos.yaml is present (Option A)."""
        # Place a working-repos.yaml with entries — they must NOT produce sibling rows.
        yaml_path = tmp_path / "working-repos.yaml"
        yaml_path.write_text(
            "repos:\n  - github: org/sibling-a\n  - github: org/sibling-b\n",
            encoding="utf-8",
        )
        ctx = _make_ctx(tmp_path)
        result = self._record_with_ctx(ctx)
        repos_in_rows = [r["repo"] for r in result["rows"]]
        assert "org/sibling-a" not in repos_in_rows, (
            "sibling repo appeared in rows — fleet walk must be removed (Option A)"
        )
        assert "org/sibling-b" not in repos_in_rows, (
            "sibling repo appeared in rows — fleet walk must be removed (Option A)"
        )

    def test_skipped_field_absent_from_result(self, tmp_path: Path) -> None:
        """record() must NOT return a 'skipped' field (vestigial fleet-walk field removed).

        Review: code-reviewer (S3-F4) — `skipped: []` was a permanent empty field implying
        a code path that no longer exists. Field removed; this test proves its absence.
        """
        ctx = _make_ctx(tmp_path)
        result = self._record_with_ctx(ctx)
        assert "skipped" not in result, (
            f"record() must not include 'skipped' field (removed in single-repo mode); "
            f"got keys: {list(result.keys())}"
        )

    def test_rows_appended_not_overwritten_on_second_call(self, tmp_path: Path) -> None:
        """Two successive record() calls produce two rows (append, not overwrite)."""
        ctx = _make_ctx(tmp_path)
        self._record_with_ctx(ctx)
        self._record_with_ctx(ctx)
        shard = tmp_path / f"backlog-snapshots.{machine_slug(ctx.hostname)}.jsonl"
        lines = [ln for ln in shard.read_text().splitlines() if ln.strip()]
        assert len(lines) == 2, (
            f"Expected exactly 2 JSONL rows after two single-repo calls; got {len(lines)}"
        )

    def test_row_contains_required_keys(self, tmp_path: Path) -> None:
        """Each row in the shard must have repo, date, bug, improvement, lessons keys."""
        ctx = _make_ctx(tmp_path)
        self._record_with_ctx(ctx)
        shard = tmp_path / f"backlog-snapshots.{machine_slug(ctx.hostname)}.jsonl"
        row = json.loads(shard.read_text().splitlines()[0])
        for key in ("repo", "date", "bug", "improvement", "lessons"):
            assert key in row, f"shard row missing required key {key!r}"

    def test_row_repo_matches_emitting_repo(self, tmp_path: Path) -> None:
        """The row's repo field matches the emitting repo's slug (per-repo attribution)."""
        ctx = _make_ctx(tmp_path, repo_name="my-org/my-repo")
        self._record_with_ctx(ctx)
        shard = tmp_path / f"backlog-snapshots.{machine_slug(ctx.hostname)}.jsonl"
        row = json.loads(shard.read_text().splitlines()[0])
        assert row["repo"] == "my-org/my-repo", (
            f"Row repo {row['repo']!r} must equal emitting repo slug 'my-org/my-repo'"
        )

    def test_result_summary_keys(self, tmp_path: Path) -> None:
        """record() returns a dict with the expected summary keys.

        Review: code-reviewer (S3-F4) — 'skipped' removed from expected keys;
        the field was a vestigial fleet-walk artifact that no longer belongs in
        single-repo mode.
        """
        ctx = _make_ctx(tmp_path)
        result = self._record_with_ctx(ctx)
        for key in ("shard", "machine", "date", "rows"):
            assert key in result, f"record() summary missing key {key!r}"

    def test_counts_only_r_state_backlog(self, tmp_path: Path) -> None:
        """record() counts backlog under central_state_root (= R/state), not any sibling."""
        # Place some backlog entries under the state root.
        bug_dir = tmp_path / "bug-backlog"
        bug_dir.mkdir()
        (bug_dir / "bug-1.yaml").write_text("id: bug-1\n")
        (bug_dir / "bug-2.yaml").write_text("id: bug-2\n")

        ctx = _make_ctx(tmp_path)
        result = self._record_with_ctx(ctx)
        assert len(result["rows"]) == 1
        row = result["rows"][0]
        assert row["bug"] == 2, (
            f"Expected bug count 2 from R/state; got {row['bug']}"
        )


# ---------------------------------------------------------------------------
# _backlog_record IPC handler: AC5 fail-loud + wiring tests
# Review: code-reviewer (S3-F3 / S4-F2) — mirrors TestArtifactEmitContextDerivation /
# TestGoalAppendExplicitOverrides in test_c4a_handler_wiring.py; proves the handler's
# fail-loud guard and that resolve_context is called with the DERIVED main_worktree_root,
# not the raw repo_root (common_dir). Plan C4b: "internal resolve_context() fallback NOT reached".
# ---------------------------------------------------------------------------

def _fake_ctx(tmp_path: Path, repo_name: str = "test-org/test-repo") -> MagicMock:
    """Build a minimal EmitContext-shaped mock for handler-wiring tests."""
    ctx = MagicMock(spec=EmitContext)
    ctx.repo_root = tmp_path
    ctx.central_state_root = tmp_path
    ctx.repo_name = repo_name
    ctx.hostname = "test-host"
    return ctx


class TestBacklogRecordHandler:
    """_backlog_record IPC handler: fail-loud on None repo_root + derive-and-delegate wiring."""

    def test_none_repo_root_raises_value_error(self) -> None:
        """Handler must raise ValueError when repo_root is None (AC5 fail-loud).

        Without the guard, None.parent → AttributeError → INTERNAL_ERROR (-32603), not
        INVALID_PARAMS (-32602). Wrong error code, wrong message, wrong signal.
        """
        from coordinator_core.ops.emit.recorder import _backlog_record

        with pytest.raises(ValueError, match="_origin_worktree"):
            _run(_backlog_record({}, repo_root=None))

    def test_none_repo_root_error_mentions_no_fallback(self) -> None:
        """Error message must mention no silent fallback to meta-repo (AC5)."""
        from coordinator_core.ops.emit.recorder import _backlog_record

        with pytest.raises(ValueError, match="No silent fallback"):
            _run(_backlog_record({}, repo_root=None))

    def test_resolve_context_called_with_main_worktree_root(self, tmp_path: Path) -> None:
        """Handler calls resolve_context(main_worktree_root(repo_root)), not bare repo_root.

        Verifies the wiring chain: common_dir → main_worktree_root → resolve_context → record.
        The param-less internal resolve_context() fallback (which would root at ~/.claude) is
        NOT reached when the handler is correctly wired.
        """
        from coordinator_core.ops.emit.recorder import _backlog_record

        # Simulate common_dir = main_worktree/.git (standard layout)
        main_worktree = tmp_path / "main_worktree"
        common_dir = main_worktree / ".git"
        # main_worktree_root(common_dir) returns common_dir.parent = main_worktree

        fake_ctx = _fake_ctx(main_worktree)
        fake_record_result = {
            "shard": str(tmp_path / "shard.jsonl"),
            "machine": "test-host",
            "date": "2026-07-07",
            "rows": [{"repo": "test-org/test-repo", "date": "2026-07-07", "bug": 0, "improvement": 0, "lessons": 0}],
        }

        with patch(
            "coordinator_core.ops.emit.recorder.resolve_context",
            return_value=fake_ctx,
        ) as mock_resolve, patch(
            "coordinator_core.ops.emit.recorder.record",
            return_value=fake_record_result,
        ) as mock_record:
            result = _run(_backlog_record({}, repo_root=common_dir))

        # resolve_context must be called with the DERIVED main_worktree (common_dir.parent),
        # not the raw common_dir — this is the load-bearing linked-worktree-safety derivation.
        mock_resolve.assert_called_once_with(main_worktree)
        mock_record.assert_called_once_with(fake_ctx)
        assert result == fake_record_result

    def test_internal_paramless_resolve_context_not_reached(self, tmp_path: Path) -> None:
        """The param-less internal resolve_context() inside record() is NOT reached.

        When repo_root is non-None, the handler builds ctx externally via resolve_context(worktree)
        and passes it to record(ctx), bypassing the internal record(ctx=None) → resolve_context()
        fallback that would root at ~/.claude.
        """
        from coordinator_core.ops.emit.recorder import _backlog_record

        common_dir = tmp_path / ".git"
        fake_ctx = _fake_ctx(tmp_path)
        call_count: list[int] = [0]

        def _counting_resolve(repo_root=None):
            call_count[0] += 1
            return fake_ctx

        with patch(
            "coordinator_core.ops.emit.recorder.resolve_context",
            side_effect=_counting_resolve,
        ), patch(
            "coordinator_core.ops.emit.recorder.record",
            return_value={"shard": "x", "machine": "m", "date": "d", "rows": []},
        ):
            _run(_backlog_record({}, repo_root=common_dir))

        # Handler calls resolve_context exactly once (the explicit external call).
        # If record(ctx=None) were reached, it would call resolve_context() a second time.
        assert call_count[0] == 1, (
            f"resolve_context must be called exactly once (handler-side); "
            f"got {call_count[0]} calls — internal param-less fallback may have fired"
        )
