"""Reverify isolated export with a measured-rev overlay: the run's untracked new files ride into the export."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import retest_targeted as rt

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

NEW = "pkg/tests/test_new.py"
TRACKED = "pkg/own.py"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    (root / "pkg").mkdir()
    (root / TRACKED).write_bytes(b"own at overlay\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "overlay")
    overlay = _git(root, "rev-parse", "HEAD")
    (root / TRACKED).write_bytes(b"own plus PEER hunk in worktree\n")
    (root / NEW).parent.mkdir(parents=True)
    (root / NEW).write_bytes(b"def test_new(): pass\n")
    return root, overlay


def test_helper_lists_only_write_set_files_absent_at_overlay(repo):
    root, overlay = repo
    got = rt._untracked_at_overlay(root, [TRACKED, NEW, "pkg/never_written.py"], overlay)
    assert got == [NEW]


def test_isolated_body_export_carries_untracked_and_keeps_overlay_blob(repo, monkeypatch):
    root, overlay = repo
    captured: dict = {}

    def fake_pass(run, export_root, tests, log, deadline, marker):
        if "export" not in captured:
            captured["export"] = Path(export_root)
            captured["new"] = (Path(export_root) / NEW).read_bytes() if (Path(export_root) / NEW).exists() else None
            captured["own"] = (Path(export_root) / TRACKED).read_bytes()
        return "pass", "", [0], True

    monkeypatch.setattr(rt, "_resolve_run_base", lambda *a, **k: (None, None))
    monkeypatch.setattr(rt, "_landing_paths", lambda *a, **k: ([TRACKED, NEW], "manifest"))
    monkeypatch.setattr(rt, "_overlay_sha", lambda *a, **k: overlay)
    monkeypatch.setattr(rt, "_run_pass_retrying", fake_pass)
    monkeypatch.setattr(rt, "_reverify_failing_ids", lambda params: [])
    seen: dict = {}

    def fake_write_dual(root_, path, log_path, export_log, run_id, tests, dropped, drop_note, meta, *rest):
        seen["meta"] = meta
        return 0

    monkeypatch.setattr(rt, "_write_dual", fake_write_dual)
    path = root / "scratch" / "rec" / "r.json"
    rc = rt._isolated_body(root, path, path.with_suffix(".log"), "run-x", {}, ["t"], [], "", None, None, None)
    assert rc == 0
    assert captured["new"] == b"def test_new(): pass\n"
    assert captured["own"] == b"own at overlay\n"
    assert seen["meta"]["untracked_overlaid"] == [NEW]
    assert seen["meta"]["overlay_sha"] == overlay
