"""test_workday_start_reconcile_sweep.py — regression suite for
workday-start-reconcile-sweep.py's bounded-scan/frontmatter-gate/dispatch
loop (the naked-Python port of /workday-start Step 1.86's retired bash
fence — example-doctrine-repo coordinator/commands/workday-start.md § Step 1.86).

Covers: bounded today/yesterday date-window filtering, pending-release-only
gating, the authored_by null/empty unscopable-entry warning, dispatch to
(a stubbed) reconcile-completion-commits.py with delta-parsing, and the
non-zero-helper-exit fallthrough. All cases exercise `run_sweep` directly
against a tmp_path fixture tree — no real git repo or real reconcile helper
is needed; `--reconcile-script` points at a small fixture script.

Spec backlink: coordinator/bin/workday-start-reconcile-sweep.py
"""
from __future__ import annotations

import importlib.util
import io
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"], cwd=os.path.dirname(os.path.abspath(__file__)),
    capture_output=True, text=True, check=True,
).stdout.strip()
_TARGET = os.path.join(_REPO_ROOT, "coordinator", "bin", "workday-start-reconcile-sweep.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("workday_start_reconcile_sweep", _TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


def _write_entry(path: Path, *, created: str, status: str, authored_by: str | None) -> None:
    lines = ["---", f"created: {created}", f"status: {status}"]
    if authored_by is not None:
        lines.append(f"authored_by: {authored_by}")
    lines.append("---")
    lines.append("body")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fixture_reconcile_script(tmp_path: Path, *, rc: int = 0, delta: int = 0) -> Path:
    """A tiny stand-in for reconcile-completion-commits.py: prints
    `delta=<N>` to stdout and exits with the given rc, ignoring its args.
    """
    script = tmp_path / "fake-reconcile.py"
    script.write_text(
        "import sys\n"
        f"print('delta={delta}')\n"
        f"sys.exit({rc})\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _run(mod, archive_root: Path, reconcile_script: Path, today="2026-07-23", yesterday="2026-07-22"):
    out = io.StringIO()
    err = io.StringIO()
    rc = mod.run_sweep(str(archive_root), today, yesterday, str(reconcile_script), out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


def test_skips_entries_outside_today_yesterday_window(mod, tmp_path):
    archive_root = tmp_path / "archive" / "completed"
    month_dir = archive_root / "2026-07"
    month_dir.mkdir(parents=True)
    entry = month_dir / "old-entry.md"
    _write_entry(entry, created="2026-07-01", status="pending-release", authored_by="sess-123")

    reconcile_script = _fixture_reconcile_script(tmp_path, delta=5)
    rc, out, err = _run(mod, archive_root, reconcile_script)

    assert rc == 0
    assert out == ""
    assert err == ""


def test_skips_entries_not_pending_release(mod, tmp_path):
    archive_root = tmp_path / "archive" / "completed"
    month_dir = archive_root / "2026-07"
    month_dir.mkdir(parents=True)
    entry = month_dir / "shipped-entry.md"
    _write_entry(entry, created="2026-07-23", status="shipped", authored_by="sess-123")

    reconcile_script = _fixture_reconcile_script(tmp_path, delta=5)
    rc, out, err = _run(mod, archive_root, reconcile_script)

    assert rc == 0
    assert out == ""


def test_unscopable_entry_missing_authored_by_warns(mod, tmp_path):
    archive_root = tmp_path / "archive" / "completed"
    month_dir = archive_root / "2026-07"
    month_dir.mkdir(parents=True)
    entry = month_dir / "no-author.md"
    _write_entry(entry, created="2026-07-23", status="pending-release", authored_by=None)

    reconcile_script = _fixture_reconcile_script(tmp_path, delta=5)
    rc, out, err = _run(mod, archive_root, reconcile_script)

    assert rc == 0
    assert "unscopable (no authored_by)" in out
    assert "no-author.md" in out


def test_unscopable_entry_literal_null_authored_by_warns(mod, tmp_path):
    archive_root = tmp_path / "archive" / "completed"
    month_dir = archive_root / "2026-07"
    month_dir.mkdir(parents=True)
    entry = month_dir / "null-author.md"
    _write_entry(entry, created="2026-07-22", status="pending-release", authored_by="null")

    reconcile_script = _fixture_reconcile_script(tmp_path, delta=5)
    rc, out, err = _run(mod, archive_root, reconcile_script)

    assert rc == 0
    assert "unscopable (no authored_by)" in out


def test_nonzero_delta_surfaces_reconcile_warning(mod, tmp_path):
    archive_root = tmp_path / "archive" / "completed"
    month_dir = archive_root / "2026-07"
    month_dir.mkdir(parents=True)
    entry = month_dir / "gap-entry.md"
    _write_entry(entry, created="2026-07-23", status="pending-release", authored_by="sess-abc123")

    reconcile_script = _fixture_reconcile_script(tmp_path, delta=3)
    rc, out, err = _run(mod, archive_root, reconcile_script)

    assert rc == 0
    assert "gap-entry.md has 3 session commit(s) not accounted" in out


def test_zero_delta_is_silent(mod, tmp_path):
    archive_root = tmp_path / "archive" / "completed"
    month_dir = archive_root / "2026-07"
    month_dir.mkdir(parents=True)
    entry = month_dir / "clean-entry.md"
    _write_entry(entry, created="2026-07-23", status="pending-release", authored_by="sess-abc123")

    reconcile_script = _fixture_reconcile_script(tmp_path, delta=0)
    rc, out, err = _run(mod, archive_root, reconcile_script)

    assert rc == 0
    assert out == ""


def test_reconcile_helper_nonzero_exit_reports_and_continues(mod, tmp_path):
    archive_root = tmp_path / "archive" / "completed"
    month_dir = archive_root / "2026-07"
    month_dir.mkdir(parents=True)
    entry = month_dir / "helper-fail.md"
    _write_entry(entry, created="2026-07-23", status="pending-release", authored_by="sess-abc123")

    reconcile_script = _fixture_reconcile_script(tmp_path, rc=2, delta=0)
    rc, out, err = _run(mod, archive_root, reconcile_script)

    assert rc == 0
    assert "reconcile helper failed" in err
    assert "rc=2" in err


def test_authored_by_strips_inline_comment_and_quotes(mod):
    content = '---\nauthored_by: "sess-xyz"  # note\n---\n'
    assert mod._authored_by_field(content) == "sess-xyz"


def test_default_reconcile_script_resolves_sibling_path(mod):
    resolved = mod._default_reconcile_script()
    assert os.path.basename(resolved) == "reconcile-completion-commits.py"
    assert os.path.dirname(resolved) == os.path.dirname(_TARGET)


def test_main_uses_real_dates_and_archive_root_defaults(mod, tmp_path, monkeypatch):
    # Smoke test for main()'s wiring: cwd-relative archive/completed, no
    # entries present -> silent, exit 0.
    monkeypatch.chdir(tmp_path)
    rc = mod.main(["workday-start-reconcile-sweep.py"])
    assert rc == 0
