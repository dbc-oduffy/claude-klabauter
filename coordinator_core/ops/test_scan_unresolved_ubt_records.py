"""Characterization tests for coordinator_core.ops.scan_unresolved_ubt_records.

Port source: ``find state/review-trail -name *.ubt-compile.pending.json``, sibling
``.resolved.json`` absence check (docs/plans/2026-07-22-coordinator-ops-buildout-
from-fence-inventory.md § Wave 2).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.scan_unresolved_ubt_records import (
    _scan_unresolved_ubt_handler,
    main,
    scan_unresolved_ubt_records,
)

# The subprocess test below spawns a fresh interpreter that imports
# coordinator_core. That child inherits cwd but NOT pytest's rootdir sys.path
# insertion, so it can only resolve the package when cwd is (or is under) the
# repo root -- from any other cwd it dies with ModuleNotFoundError before it
# can write anything to stdout. Pinning cwd to the repo root derived from this
# file's own path makes the subprocess resolvable regardless of the invoking
# shell's cwd.
_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Circular-import regression guard — pickup_assemble-before-ops import order
# ---------------------------------------------------------------------------


def test_op_registers_when_pickup_assemble_imported_before_ops() -> None:
    """Regression guard: importing ``coordinator_core.pickup_assemble`` ALONE
    (never touching ``coordinator_core.ops.scan_unresolved_ubt_records``
    explicitly) must still leave this op registered and un-poisoned.

    Prior to the leaf-level import deferral fix, this module imported
    ``resolve_repo_root`` from ``coordinator_core.pickup_assemble`` at module
    scope. ``pickup_assemble`` transitively imports ``coordinator_core.ops``,
    whose eager-load loop then re-imports this module while
    ``pickup_assemble`` is still initializing, raising an ImportError that the
    ops eager-loader records in ``coordinator_core.ops._POISONED_MODULES``
    (dispatch of the op re-raises that ImportError rather than "unknown op").

    Deliberately does NOT also ``import
    coordinator_core.ops.scan_unresolved_ubt_records`` — doing so would let a
    broken module-scope import pass anyway, because by that second explicit
    import ``pickup_assemble`` has already finished initializing and the
    re-import would silently succeed, masking the exact regression this test
    exists to catch. The first line — ``import
    coordinator_core.pickup_assemble`` alone — must be sufficient for the op
    to end up registered.

    Runs in a fresh subprocess so there is no prior test's ``sys.modules``
    import-order pollution masking the regression.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import coordinator_core.pickup_assemble\n"
                "from coordinator_core.ipc import _REGISTRY\n"
                "from coordinator_core.ops import _POISONED_MODULES\n"
                "assert 'review_trail.scan_unresolved_ubt' in _REGISTRY, "
                "'op missing from registry after pickup_assemble-first import order'\n"
                "assert not any('scan_unresolved_ubt_records' in k for k in _POISONED_MODULES), "
                "f'module poisoned: {_POISONED_MODULES}'\n"
            ),
        ],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        cwd=_REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# scan_unresolved_ubt_records
# ---------------------------------------------------------------------------


def test_absent_review_trail_dir_returns_empty(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    assert scan_unresolved_ubt_records(worktree) == []


def test_pending_marker_without_resolved_sibling_is_unresolved(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    trail_dir = worktree / "state" / "review-trail"
    trail_dir.mkdir(parents=True)
    pending = trail_dir / "2026-07-22-abc.ubt-compile.pending.json"
    pending.write_text("{}", encoding="utf-8")

    result = scan_unresolved_ubt_records(worktree)

    assert result == [str(pending)]


def test_pending_marker_with_resolved_sibling_is_excluded(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    trail_dir = worktree / "state" / "review-trail"
    trail_dir.mkdir(parents=True)
    pending = trail_dir / "2026-07-22-abc.ubt-compile.pending.json"
    pending.write_text("{}", encoding="utf-8")
    resolved = trail_dir / "2026-07-22-abc.resolved.json"
    resolved.write_text("{}", encoding="utf-8")

    assert scan_unresolved_ubt_records(worktree) == []


def test_scan_is_recursive_across_subdirectories(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    nested_dir = worktree / "state" / "review-trail" / "nested"
    nested_dir.mkdir(parents=True)
    pending = nested_dir / "2026-07-22-def.ubt-compile.pending.json"
    pending.write_text("{}", encoding="utf-8")

    assert scan_unresolved_ubt_records(worktree) == [str(pending)]


def test_result_is_sorted(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    trail_dir = worktree / "state" / "review-trail"
    trail_dir.mkdir(parents=True)
    marker_b = trail_dir / "2026-07-22-b.ubt-compile.pending.json"
    marker_a = trail_dir / "2026-07-22-a.ubt-compile.pending.json"
    marker_b.write_text("{}", encoding="utf-8")
    marker_a.write_text("{}", encoding="utf-8")

    result = scan_unresolved_ubt_records(worktree)

    assert result == sorted(result)
    assert result == [str(marker_a), str(marker_b)]


def test_double_invocation_is_idempotent(tmp_path: Path) -> None:
    """AC7: a second invocation against unchanged disk state returns the same list."""
    worktree = tmp_path / "repo"
    trail_dir = worktree / "state" / "review-trail"
    trail_dir.mkdir(parents=True)
    pending = trail_dir / "2026-07-22-abc.ubt-compile.pending.json"
    pending.write_text("{}", encoding="utf-8")

    first = scan_unresolved_ubt_records(worktree)
    second = scan_unresolved_ubt_records(worktree)

    assert first == second == [str(pending)]


def test_non_matching_file_is_ignored(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    trail_dir = worktree / "state" / "review-trail"
    trail_dir.mkdir(parents=True)
    (trail_dir / "2026-07-22-abc.json").write_text("{}", encoding="utf-8")

    assert scan_unresolved_ubt_records(worktree) == []


# ---------------------------------------------------------------------------
# _scan_unresolved_ubt_handler — JSON-RPC op entrypoint
# ---------------------------------------------------------------------------


def test_handler_returns_unresolved_under_repo_root(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    trail_dir = worktree / "state" / "review-trail"
    trail_dir.mkdir(parents=True)
    pending = trail_dir / "2026-07-22-abc.ubt-compile.pending.json"
    pending.write_text("{}", encoding="utf-8")
    git_common_dir = worktree / ".git"
    git_common_dir.mkdir()

    result = _scan_unresolved_ubt_handler({}, repo_root=git_common_dir)

    assert result == {"unresolved": [str(pending)]}


# ---------------------------------------------------------------------------
# main() — CLI entrypoint, non-blocking contract (d-run-ubt-pending-check)
# ---------------------------------------------------------------------------


def test_main_exits_zero_when_unresolved_markers_are_found(monkeypatch, tmp_path: Path, capsys) -> None:
    """Pins the non-blocking contract: an unresolved UBT-compile marker is a
    DEFERRED RECORD (resolution happens at /workday-complete Step 0c per the
    pre-conversion SKILL body), never a `d-run-ubt-pending-check` failure.
    Regression guard against re-introducing the blocking read this test
    exists to prevent — see `main()`'s own docstring."""
    worktree = tmp_path / "repo"
    trail_dir = worktree / "state" / "review-trail"
    trail_dir.mkdir(parents=True)
    pending = trail_dir / "2026-07-22-abc.ubt-compile.pending.json"
    pending.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        "coordinator_core.pickup_assemble.resolve_repo_root",
        lambda: worktree,
    )

    exit_code = main(["--mode", "pending", "--since", "deadbeef"])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == str(pending)


def test_main_exits_zero_when_no_unresolved_markers(monkeypatch, tmp_path: Path, capsys) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()

    monkeypatch.setattr(
        "coordinator_core.pickup_assemble.resolve_repo_root",
        lambda: worktree,
    )

    exit_code = main(["--mode", "pending", "--since", "deadbeef"])

    assert exit_code == 0
    assert capsys.readouterr().out == ""


def test_main_usage_error_on_bad_mode(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "coordinator_core.pickup_assemble.resolve_repo_root",
        lambda: tmp_path,
    )
    assert main(["--mode", "resolved", "--since", "deadbeef"]) == 2


def test_main_usage_error_on_unrecognized_argument() -> None:
    assert main(["--bogus"]) == 2


@pytest.mark.parametrize("argv", [["--mode"], ["--mode", "pending", "--since"]])
def test_main_usage_error_on_missing_flag_value(argv: list[str]) -> None:
    assert main(argv) == 2


def test_handler_with_no_repo_root_returns_empty(tmp_path: Path) -> None:
    result = _scan_unresolved_ubt_handler({}, repo_root=None)
    assert result == {"unresolved": []}
