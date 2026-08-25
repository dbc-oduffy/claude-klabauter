"""
coordinator_core.ops.test_workday_stitch_sidecar_summary

Tests for the "workday.stitch_sidecar_into_summary" op.

Import guard: coordinator_core.ops.workday_stitch_sidecar_summary MUST be
imported at module load time so @register_op(...) fires and populates
_REGISTRY.

Coverage:
  (a) registry assertion — op name present in _REGISTRY after import
  (b) fresh append — sidecar content appended to summary, sidecar deleted
  (c) fresh append — summary did not previously exist (missing_ok path)
  (d) double-invocation (AC7) — second call after full success is a safe
      no-op (sidecar already gone)
  (e) crash-window rerun (DEC-7 mechanized proof) — simulate a crash between
      append and delete by invoking the append-only mutate path directly,
      then re-run the full handler and assert: no duplicate content, sidecar
      cleaned up
  (f) marker not found in sidecar content — refuses to append, exit_code 1
  (g) sidecar not found at all — no-op, exit_code 0
  (h) missing required params — exit_code 1
  (i) path containment — summary_path / sidecar_path escaping
      state/week-changelog/ is rejected
  (j) LockTimeout surfaces as exit_code 1

Spec backlink: coordinator_core/ops/workday_stitch_sidecar_summary.py
Port source: commands/workday-complete.md:644 (cat >> summary && rm sidecar)
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.workday_stitch_sidecar_summary  # noqa: F401

from coordinator_core.ipc import _REGISTRY
from coordinator_core.locked_write import LockTimeout
from coordinator_core.ops.workday_stitch_sidecar_summary import _handler

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_OP_NAME = "workday.stitch_sidecar_into_summary"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.workday_stitch_sidecar_summary @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo and return its root (main worktree root).

    locked_rmw's lock-sidecar directory derivation shells to
    ``git rev-parse --path-format=absolute --git-common-dir``, so the fixture
    needs a REAL git repo, not just a directory named ``.git``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "stitch-test@makima.test")
    _git("config", "user.name", "Stitch Test")
    _git("config", "commit.gpgsign", "false")

    (repo / "state" / "week-changelog").mkdir(parents=True, exist_ok=True)
    (repo / "state" / "week-changelog" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return repo


def _call(repo: Path, summary_path: str, sidecar_path: str, marker: str) -> dict:
    return _run(
        _handler(
            {
                "summary_path": summary_path,
                "sidecar_path": sidecar_path,
                "marker": marker,
            },
            repo_root=repo / ".git",
        )
    )


# ---------------------------------------------------------------------------
# (a) Registry assertion
# ---------------------------------------------------------------------------


def test_op_registered():
    assert _OP_NAME in _REGISTRY


# ---------------------------------------------------------------------------
# (b) Fresh append
# ---------------------------------------------------------------------------


def test_fresh_append_stitches_and_removes_sidecar(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    summary = repo / "state" / "week-changelog" / "2026-07-22-summary.md"
    sidecar = repo / "state" / "week-changelog" / "2026-07-22-sidecar.md"
    summary.write_text("# Daily summary\n\nExisting content.\n", encoding="utf-8")
    sidecar.write_text(
        "## Observer notes <!-- marker:2026-07-22 -->\nObserved things.\n",
        encoding="utf-8",
    )

    result = _call(
        repo,
        "state/week-changelog/2026-07-22-summary.md",
        "state/week-changelog/2026-07-22-sidecar.md",
        "<!-- marker:2026-07-22 -->",
    )

    assert result["exit_code"] == 0
    assert result["stitched"] is True
    assert result["sidecar_removed"] is True
    final_text = summary.read_text(encoding="utf-8")
    assert "Existing content." in final_text
    assert "Observed things." in final_text
    assert not sidecar.exists()


# ---------------------------------------------------------------------------
# (c) Summary does not yet exist (missing_ok path)
# ---------------------------------------------------------------------------


def test_summary_created_when_absent(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    sidecar = repo / "state" / "week-changelog" / "2026-07-22-sidecar.md"
    sidecar.write_text("Sidecar body <<marker>>.\n", encoding="utf-8")

    result = _call(
        repo,
        "state/week-changelog/2026-07-22-summary.md",
        "state/week-changelog/2026-07-22-sidecar.md",
        "<<marker>>",
    )

    assert result["exit_code"] == 0
    assert result["stitched"] is True
    assert result["sidecar_removed"] is True
    summary = repo / "state" / "week-changelog" / "2026-07-22-summary.md"
    assert "Sidecar body <<marker>>." in summary.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (d) Double-invocation (AC7): second call after full success is a no-op
# ---------------------------------------------------------------------------


def test_double_invocation_after_success_is_safe_noop(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    summary = repo / "state" / "week-changelog" / "2026-07-22-summary.md"
    sidecar = repo / "state" / "week-changelog" / "2026-07-22-sidecar.md"
    summary.write_text("# Daily summary\n", encoding="utf-8")
    sidecar.write_text("Body <<m1>>.\n", encoding="utf-8")

    args = (
        "state/week-changelog/2026-07-22-summary.md",
        "state/week-changelog/2026-07-22-sidecar.md",
        "<<m1>>",
    )

    first = _call(repo, *args)
    assert first["exit_code"] == 0
    assert first["stitched"] is True
    assert first["sidecar_removed"] is True

    text_after_first = summary.read_text(encoding="utf-8")

    second = _call(repo, *args)
    assert second["exit_code"] == 0
    assert second["stitched"] is False
    assert second["sidecar_removed"] is False
    assert summary.read_text(encoding="utf-8") == text_after_first
    assert text_after_first.count("Body <<m1>>.") == 1


# ---------------------------------------------------------------------------
# (e) Crash-window rerun (DEC-7 mechanized proof): a crash between the
# append and the delete does NOT duplicate the append on rerun, and the
# rerun completes the cleanup.
# ---------------------------------------------------------------------------


def test_crash_between_append_and_delete_does_not_duplicate_on_rerun(
    tmp_path: Path,
) -> None:
    repo = _make_git_repo(tmp_path)
    summary = repo / "state" / "week-changelog" / "2026-07-22-summary.md"
    sidecar = repo / "state" / "week-changelog" / "2026-07-22-sidecar.md"
    summary.write_text("# Daily summary\n", encoding="utf-8")
    sidecar.write_text("Body <<crashmark>>.\n", encoding="utf-8")

    args = (
        "state/week-changelog/2026-07-22-summary.md",
        "state/week-changelog/2026-07-22-sidecar.md",
        "<<crashmark>>",
    )

    # Simulate "crash between append and delete": patch Path.unlink so the
    # delete step raises mid-call, after the append has already landed on
    # disk. The sidecar file is left behind, exactly as a real crash would
    # leave it.
    class _SimulatedCrash(Exception):
        pass

    with patch(
        "coordinator_core.ops.workday_stitch_sidecar_summary.Path.unlink",
        side_effect=_SimulatedCrash("simulated crash before delete"),
    ):
        with pytest.raises(_SimulatedCrash):
            _call(repo, *args)

    # Append landed; sidecar is still present (crash happened before delete).
    text_after_crash = summary.read_text(encoding="utf-8")
    assert "Body <<crashmark>>." in text_after_crash
    assert sidecar.exists()

    # Rerun the full handler (no patching this time) — must NOT duplicate
    # the append, and must complete the deferred delete.
    result = _call(repo, *args)

    assert result["exit_code"] == 0
    assert result["stitched"] is True
    assert result["sidecar_removed"] is True
    final_text = summary.read_text(encoding="utf-8")
    assert final_text == text_after_crash, "rerun must be a zero-write no-op on the summary"
    assert final_text.count("Body <<crashmark>>.") == 1
    assert not sidecar.exists()


# ---------------------------------------------------------------------------
# (f) Marker not found in sidecar content — refuses to append
# ---------------------------------------------------------------------------


def test_marker_not_in_sidecar_content_refuses_to_append(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    summary = repo / "state" / "week-changelog" / "2026-07-22-summary.md"
    sidecar = repo / "state" / "week-changelog" / "2026-07-22-sidecar.md"
    summary.write_text("# Daily summary\n", encoding="utf-8")
    sidecar.write_text("Body with no marker text.\n", encoding="utf-8")

    result = _call(
        repo,
        "state/week-changelog/2026-07-22-summary.md",
        "state/week-changelog/2026-07-22-sidecar.md",
        "<<absent-marker>>",
    )

    assert result["exit_code"] == 1
    assert "marker" in result["error"].lower()
    # Nothing was touched.
    assert summary.read_text(encoding="utf-8") == "# Daily summary\n"
    assert sidecar.exists()


# ---------------------------------------------------------------------------
# (g) Sidecar not found at all — no-op
# ---------------------------------------------------------------------------


def test_sidecar_not_found_is_noop(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    summary = repo / "state" / "week-changelog" / "2026-07-22-summary.md"
    summary.write_text("# Daily summary\n", encoding="utf-8")

    result = _call(
        repo,
        "state/week-changelog/2026-07-22-summary.md",
        "state/week-changelog/2026-07-22-nonexistent-sidecar.md",
        "<<whatever>>",
    )

    assert result["exit_code"] == 0
    assert result["stitched"] is False
    assert result["sidecar_removed"] is False
    assert summary.read_text(encoding="utf-8") == "# Daily summary\n"


# ---------------------------------------------------------------------------
# (h) Missing required params
# ---------------------------------------------------------------------------


def test_missing_summary_path_param(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    result = _run(
        _handler(
            {"sidecar_path": "x", "marker": "y"},
            repo_root=repo / ".git",
        )
    )
    assert result["exit_code"] == 1
    assert "summary_path" in result["error"]


def test_missing_sidecar_path_param(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    result = _run(
        _handler(
            {"summary_path": "x", "marker": "y"},
            repo_root=repo / ".git",
        )
    )
    assert result["exit_code"] == 1
    assert "sidecar_path" in result["error"]


def test_missing_marker_param(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    result = _run(
        _handler(
            {"summary_path": "x", "sidecar_path": "y"},
            repo_root=repo / ".git",
        )
    )
    assert result["exit_code"] == 1
    assert "marker" in result["error"]


def test_missing_repo_root(tmp_path: Path) -> None:
    result = _run(
        _handler(
            {"summary_path": "x", "sidecar_path": "y", "marker": "z"},
            repo_root=None,
        )
    )
    assert result["exit_code"] == 1
    assert "repo_root" in result["error"]


# ---------------------------------------------------------------------------
# (i) Path containment
# ---------------------------------------------------------------------------


def test_summary_path_escaping_allowed_root_is_rejected(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    (repo / "state" / "handoffs").mkdir(parents=True, exist_ok=True)

    result = _call(
        repo,
        "../outside-summary.md",
        "state/week-changelog/sidecar.md",
        "marker",
    )

    assert result["exit_code"] == 1
    assert "escapes" in result["error"]


def test_sidecar_path_escaping_allowed_root_is_rejected(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)

    result = _call(
        repo,
        "state/week-changelog/summary.md",
        "../outside-sidecar.md",
        "marker",
    )

    assert result["exit_code"] == 1
    assert "escapes" in result["error"]


# ---------------------------------------------------------------------------
# (j) LockTimeout surfaces as exit_code 1
# ---------------------------------------------------------------------------


def test_lock_timeout_surfaces_as_exit_code_1(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    sidecar = repo / "state" / "week-changelog" / "sidecar.md"
    sidecar.write_text("Body <<m>>.\n", encoding="utf-8")

    with patch(
        "coordinator_core.ops.workday_stitch_sidecar_summary.locked_rmw",
        side_effect=LockTimeout("simulated timeout"),
    ):
        result = _call(
            repo,
            "state/week-changelog/summary.md",
            "state/week-changelog/sidecar.md",
            "<<m>>",
        )

    assert result["exit_code"] == 1
    assert "lock timeout" in result["error"].lower()
    # Sidecar must not have been deleted if the write step never completed.
    assert sidecar.exists()
