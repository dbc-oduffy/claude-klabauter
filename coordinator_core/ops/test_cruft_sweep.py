
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.ops import cruft_sweep
from coordinator_core.win_portability import no_console_passthrough_kwargs

# (`import subprocess as _subprocess`) -- SPAWN-RATCHET Rule 2 declaration,
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="chmod-based unreadable-file fixture is POSIX-only")
def test_build_uuid_blocklist_unreadable_file_marks_incomplete(tmp_path, capsys):
    handoffs_dir = tmp_path / "handoffs"
    handoffs_dir.mkdir()

    _write(
        handoffs_dir / "readable.md",
        "---\npredecessor: 11111111-1111-1111-1111-111111111111\n---\n",
    )
    blocked = handoffs_dir / "blocked.md"
    _write(blocked, "predecessor: 22222222-2222-2222-2222-222222222222\n")
    os.chmod(blocked, 0o000)

    try:
        blocklist, complete = cruft_sweep.build_uuid_blocklist(handoffs_dir)
    finally:
        os.chmod(blocked, 0o644)

    assert complete is False
    assert "11111111-1111-1111-1111-111111111111" in blocklist
    assert "22222222-2222-2222-2222-222222222222" not in blocklist
    assert "unreadable handoff file" in capsys.readouterr().err


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_build_uuid_blocklist_unreadable_handoffs_dir_marks_incomplete(tmp_path, capsys):
    handoffs_dir = tmp_path / "handoffs"
    handoffs_dir.mkdir()
    _write(
        handoffs_dir / "readable.md",
        "---\npredecessor: 44444444-4444-4444-4444-444444444444\n---\n",
    )

    original_mode = handoffs_dir.stat().st_mode
    os.chmod(handoffs_dir, 0o000)
    try:
        blocklist, complete = cruft_sweep.build_uuid_blocklist(handoffs_dir)
    finally:
        os.chmod(handoffs_dir, original_mode)

    assert complete is False
    assert blocklist == set()
    assert str(handoffs_dir) in capsys.readouterr().err


def test_build_uuid_blocklist_all_readable_is_complete(tmp_path):
    handoffs_dir = tmp_path / "handoffs"
    handoffs_dir.mkdir()
    _write(
        handoffs_dir / "a.md",
        "---\npredecessor: 33333333-3333-3333-3333-333333333333\n---\n",
    )
    blocklist, complete = cruft_sweep.build_uuid_blocklist(handoffs_dir)
    assert complete is True
    assert "33333333-3333-3333-3333-333333333333" in blocklist


@pytest.mark.skipif(os.name == "nt", reason="chmod-based unreadable-file fixture is POSIX-only")
def test_run_handler_aborts_apply_on_incomplete_blocklist(tmp_path):
    handoffs_dir = tmp_path / "handoffs"
    handoffs_dir.mkdir()
    blocked = handoffs_dir / "blocked.md"
    _write(blocked, "predecessor: 44444444-4444-4444-4444-444444444444\n")
    os.chmod(blocked, 0o000)

    params = {
        "class_": "harness",
        "apply": True,
        "handoffs_dir": str(handoffs_dir),
        "projects_root": str(tmp_path / "projects"),
        "file_history_root": str(tmp_path / "file-history"),
        "lock_dir": str(tmp_path / "lock.d"),
    }
    try:
        with pytest.raises(cruft_sweep.BlocklistIncompleteError):
            asyncio.run(cruft_sweep._run_handler(params))
    finally:
        os.chmod(blocked, 0o644)


@pytest.mark.skipif(os.name == "nt", reason="chmod-based unreadable-file fixture is POSIX-only")
def test_run_handler_dry_run_does_not_abort_on_incomplete_blocklist(tmp_path):
    handoffs_dir = tmp_path / "handoffs"
    handoffs_dir.mkdir()
    blocked = handoffs_dir / "blocked.md"
    _write(blocked, "predecessor: 55555555-5555-5555-5555-555555555555\n")
    os.chmod(blocked, 0o000)

    params = {
        "class_": "harness",
        "apply": False,
        "handoffs_dir": str(handoffs_dir),
        "projects_root": str(tmp_path / "projects"),
        "file_history_root": str(tmp_path / "file-history"),
        "lock_dir": str(tmp_path / "lock.d"),
    }
    try:
        result = asyncio.run(cruft_sweep._run_handler(params))
    finally:
        os.chmod(blocked, 0o644)
    assert result["totals"]["harness"] == {"bytes": 0, "items": 0}


def test_delete_path_returns_true_and_removes_dir(tmp_path):
    target = tmp_path / "victim"
    target.mkdir()
    (target / "f.txt").write_text("x")
    assert cruft_sweep._delete_path(target) is True
    assert not target.exists()


def test_delete_path_returns_false_on_failing_subprocess(tmp_path, monkeypatch):
    target = tmp_path / "victim"
    target.mkdir()

    class _FakeCompleted:
        returncode = 1

    monkeypatch.setattr(
        cruft_sweep.subprocess, "run", lambda *a, **k: _FakeCompleted()
    )
    assert cruft_sweep._delete_path(target) is False
    assert target.exists()


def test_delete_file_returns_false_on_oserror(tmp_path, monkeypatch):
    target = tmp_path / "victim.txt"
    target.write_text("x")

    def _raise(*a, **k):
        raise OSError("simulated unlink failure")

    monkeypatch.setattr(Path, "unlink", _raise)
    assert cruft_sweep._delete_file(target) is False


def _make_orphan_fixture(parent_root: Path) -> Path:
    child = parent_root / "tmp"
    (child / "vector" / "store").mkdir(parents=True)
    (child / "vector" / "store" / "chroma.sqlite3").write_text("fake sqlite")
    return child


def test_sweep_orphans_failed_delete_not_counted_as_pruned(tmp_path, monkeypatch, capsys):
    parent_root = tmp_path / "parent"
    parent_root.mkdir()
    child = _make_orphan_fixture(parent_root)

    monkeypatch.setattr(cruft_sweep, "_delete_path", lambda target: False)

    total_bytes, total_items = cruft_sweep.sweep_orphans(
        [parent_root], [], apply=True, json_mode=False, quiet=False,
    )
    assert total_items == 0
    assert total_bytes == 0
    assert child.exists()
    assert "WARNING: delete failed, not counted as pruned" in capsys.readouterr().err


def test_sweep_orphans_successful_delete_counted_as_pruned(tmp_path):
    parent_root = tmp_path / "parent"
    parent_root.mkdir()
    child = _make_orphan_fixture(parent_root)

    total_bytes, total_items = cruft_sweep.sweep_orphans(
        [parent_root], [], apply=True, json_mode=False, quiet=True,
    )
    assert total_items == 1
    assert total_bytes > 0
    assert not child.exists()


@pytest.mark.skipif(os.name == "nt", reason="chmod-based unreadable-subtree fixture is POSIX-only")
def test_sweep_scratch_warns_on_unwalkable_subtree(tmp_path, capsys):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    locked = repo_root / "locked"
    locked.mkdir()
    (locked / "tmp-cc").mkdir()

    original_mode = locked.stat().st_mode
    os.chmod(locked, 0o000)
    try:
        cruft_sweep.sweep_scratch(
            repo_root, 7, apply=False, json_mode=False, quiet=True,
        )
    finally:
        os.chmod(locked, original_mode)

    captured = capsys.readouterr()
    assert str(locked) in captured.err
    assert "WARNING" in captured.err


def test_sweep_scratch_no_warning_on_genuinely_clean_tree(tmp_path, capsys):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "some-dir").mkdir()

    cruft_sweep.sweep_scratch(
        repo_root, 7, apply=False, json_mode=False, quiet=True,
    )

    assert "WARNING" not in capsys.readouterr().err


def test_sweep_scratch_apply_batches_multiple_auto_prune_dirs(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)

    first = repo_root / "tmp-cc"
    second = repo_root / "fake"
    first.mkdir()
    second.mkdir()
    _age_path(first, 2 * 86400)
    _age_path(second, 2 * 86400)

    total_bytes, total_items = cruft_sweep.sweep_scratch(
        repo_root, 1, apply=True, json_mode=False, quiet=True,
    )

    assert total_items == 2
    assert not first.exists()
    assert not second.exists()


def _init_git_repo(repo_root: Path) -> None:
    import subprocess as _subprocess

    _subprocess.run(["git", "init", "-q", str(repo_root)], check=True, **no_console_passthrough_kwargs())
    _subprocess.run(
        ["git", "-C", str(repo_root), "config", "user.email", "test@example.com"],
        check=True, **no_console_passthrough_kwargs(),
    )
    _subprocess.run(
        ["git", "-C", str(repo_root), "config", "user.name", "Test"], check=True, **no_console_passthrough_kwargs(),
    )


def _age_path(path: Path, age_secs: int) -> None:
    old = time.time() - age_secs
    os.utime(path, (old, old))


def _age_subtree(root: Path, age_secs: int) -> None:
    for dirpath, dirnames, _files in os.walk(root):
        _age_path(Path(dirpath), age_secs)
    _age_path(root, age_secs)


def test_sweep_empty_dirs_nested_empty_skeleton_is_pruned(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)

    skeleton = (
        repo_root / "nonexistent" / "Library" / "Caches" / "com.apple.python"
        / "Applications" / "Xcode.app" / "Contents" / "Developer" / "Library"
        / "Frameworks" / "Python3.framework" / "Versions" / "3.9" / "lib"
        / "python3.9" / "encodings"
    )
    skeleton.mkdir(parents=True)
    (repo_root / "nonexistent" / "Library" / "Caches" / "com.apple.python"
     / "Applications" / "Xcode.app" / "Contents" / "Developer" / "Library"
     / "Frameworks" / "Python3.framework" / "Versions" / "3.9" / "lib"
     / "python3.9" / "importlib").mkdir()
    (repo_root / "nonexistent" / "Library" / "Caches" / "com.apple.python"
     / "Applications" / "Xcode.app" / "Contents" / "Developer" / "Library"
     / "Frameworks" / "Python3.framework" / "Versions" / "3.9" / "lib"
     / "python3.9" / "collections").mkdir()

    _age_subtree(repo_root / "nonexistent", 2 * 86400)

    total_bytes, total_items = cruft_sweep.sweep_empty_toplevel_dirs(
        repo_root, apply=True, json_mode=False, quiet=True,
    )

    assert total_items == 1
    assert not (repo_root / "nonexistent").exists()


def test_sweep_empty_dirs_bare_empty_dir_with_dots_is_pruned(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)

    dotted = repo_root / "not..."
    dotted.mkdir()
    plain = repo_root / "probably"
    plain.mkdir()
    _age_path(dotted, 2 * 86400)
    _age_path(plain, 2 * 86400)

    total_bytes, total_items = cruft_sweep.sweep_empty_toplevel_dirs(
        repo_root, apply=True, json_mode=False, quiet=True,
    )

    assert total_items == 2
    assert not dotted.exists()
    assert not plain.exists()


def test_sweep_empty_dirs_git_ignored_is_skipped(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)
    (repo_root / ".gitignore").write_text("ignored-empty/\n", encoding="utf-8")

    target = repo_root / "ignored-empty"
    target.mkdir()
    _age_path(target, 2 * 86400)

    total_bytes, total_items = cruft_sweep.sweep_empty_toplevel_dirs(
        repo_root, apply=True, json_mode=False, quiet=True,
    )

    assert total_items == 0
    assert target.exists()


def test_sweep_empty_dirs_dir_with_one_deep_file_not_swept(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)

    target = repo_root / "almost-empty"
    deep = target / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "keepme.txt").write_text("x", encoding="utf-8")
    _age_subtree(target, 2 * 86400)

    total_bytes, total_items = cruft_sweep.sweep_empty_toplevel_dirs(
        repo_root, apply=True, json_mode=False, quiet=True,
    )

    assert total_items == 0
    assert target.exists()


def test_sweep_empty_dirs_recent_mtime_not_swept(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)

    target = repo_root / "just-made"
    target.mkdir()

    total_bytes, total_items = cruft_sweep.sweep_empty_toplevel_dirs(
        repo_root, apply=True, json_mode=False, quiet=True,
    )

    assert total_items == 0
    assert target.exists()


def test_sweep_empty_dirs_whitelisted_name_is_skipped(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)

    target = repo_root / "keep-me-please"
    target.mkdir()
    _age_path(target, 2 * 86400)

    total_bytes, total_items = cruft_sweep.sweep_empty_toplevel_dirs(
        repo_root, ["keep-me-please"], apply=True, json_mode=False, quiet=True,
    )

    assert total_items == 0
    assert target.exists()


def test_sweep_empty_dirs_hard_excluded_name_is_skipped(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)

    target = repo_root / "state"
    target.mkdir()
    _age_path(target, 2 * 86400)

    total_bytes, total_items = cruft_sweep.sweep_empty_toplevel_dirs(
        repo_root, apply=True, json_mode=False, quiet=True,
    )

    assert total_items == 0
    assert target.exists()


def test_sweep_empty_dirs_dry_run_deletes_nothing_but_reports(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)

    target = repo_root / "stray-junk"
    target.mkdir()
    _age_path(target, 2 * 86400)

    total_bytes, total_items = cruft_sweep.sweep_empty_toplevel_dirs(
        repo_root, apply=False, json_mode=False, quiet=True,
    )

    assert total_items == 1
    assert target.exists()


def test_sweep_empty_dirs_outside_git_work_tree_skips_no_deletions(tmp_path, capsys):
    repo_root = tmp_path / "not-a-repo"
    repo_root.mkdir()

    target = repo_root / "stray-junk"
    target.mkdir()
    _age_path(target, 2 * 86400)

    total_bytes, total_items = cruft_sweep.sweep_empty_toplevel_dirs(
        repo_root, apply=True, json_mode=False, quiet=False,
    )

    assert total_items == 0
    assert total_bytes == 0
    assert target.exists()
    assert "not inside a git work tree" in capsys.readouterr().err


def test_dir_size_bytes_respects_budget(tmp_path, monkeypatch):
    d = tmp_path / "big"
    d.mkdir()
    for i in range(50):
        (d / f"file-{i}.txt").write_text("x" * 100)

    visited = []
    real_walk = os.walk

    def _spy_walk(*args, **kwargs):
        for item in real_walk(*args, **kwargs):
            visited.append(item)
            yield item

    monkeypatch.setattr(cruft_sweep.os, "walk", _spy_walk)

    cruft_sweep._dir_size_bytes(d, budget_secs=0.0)
    assert len(visited) <= 1, (
        "budget=0 should bail on the first os.walk() iteration, not walk "
        f"the whole tree; visited {len(visited)} levels"
    )


def test_dir_size_bytes_still_sums_normally_under_a_generous_budget(tmp_path):
    d = tmp_path / "small"
    d.mkdir()
    (d / "a.txt").write_text("hello")

    size = cruft_sweep._dir_size_bytes(d, budget_secs=5.0)
    assert size >= 0
    assert size == cruft_sweep._dir_size_bytes(d)


def test_dir_size_bytes_no_st_blocks_platform_falls_back_to_st_size(tmp_path, monkeypatch):
    d = tmp_path / "winlike"
    d.mkdir()
    expected_size = 0
    for i in range(5):
        content = ("x" * (100 + i * 37)).encode("utf-8")
        (d / f"file-{i}.txt").write_bytes(content)
        expected_size += len(content)

    monkeypatch.setattr(cruft_sweep.os, "stat_result", object)

    size = cruft_sweep._dir_size_bytes(d)
    assert size == expected_size
    assert size > 0


def test_is_pruned_child_recognizes_both_separators():
    pruned = ["C:\\repo\\nonexistent"]
    assert cruft_sweep._is_pruned_child("C:\\repo\\nonexistent", pruned) is True
    assert cruft_sweep._is_pruned_child("C:\\repo\\nonexistent\\sub", pruned) is True
    assert cruft_sweep._is_pruned_child("/repo/nonexistent/sub", ["/repo/nonexistent"]) is True
    assert cruft_sweep._is_pruned_child("/repo/other", ["/repo/nonexistent"]) is False


def test_scratch_and_empty_dirs_both_match_same_path_reproduction(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)

    target = repo_root / "nonexistent"
    target.mkdir()
    _age_path(target, 10 * 86400)

    scratch_records = []
    cruft_sweep.sweep_scratch(
        repo_root, 7, apply=False, json_mode=True, quiet=True,
        emit_fn=scratch_records.append,
    )
    empty_dirs_records = []
    cruft_sweep.sweep_empty_toplevel_dirs(
        repo_root, apply=False, json_mode=True, quiet=True,
        emit_fn=empty_dirs_records.append,
    )

    scratch_auto_prune = [r for r in scratch_records if r["disposition"] == "auto-prune"]
    assert len(scratch_auto_prune) == 1
    assert scratch_auto_prune[0]["path"] == str(target)

    empty_dirs_auto_prune = [r for r in empty_dirs_records if r["disposition"] == "auto-prune"]
    assert empty_dirs_auto_prune == [], (
        "Phase E emitted an independent auto-prune record for a path Phase B "
        "already owns — this double-counts one directory as two reclaim items"
    )
    duplicate_records = [r for r in empty_dirs_records if r["disposition"] == "duplicate-of-scratch"]
    assert len(duplicate_records) == 1
    assert duplicate_records[0]["path"] == str(target)


def test_empty_dirs_non_scratch_name_is_not_relabeled(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)

    target = repo_root / "some-stray-prose-word"
    target.mkdir()
    _age_path(target, 2 * 86400)

    records = []
    cruft_sweep.sweep_empty_toplevel_dirs(
        repo_root, apply=False, json_mode=True, quiet=True,
        emit_fn=records.append,
    )

    auto_prune = [r for r in records if r["disposition"] == "auto-prune"]
    assert len(auto_prune) == 1
    assert auto_prune[0]["path"] == str(target)
    assert not any(r["disposition"] == "duplicate-of-scratch" for r in records)


def test_apply_mode_has_no_double_delete_for_overlapping_name(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)

    target = repo_root / "nonexistent"
    target.mkdir()
    _age_path(target, 10 * 86400)

    scratch_bytes, scratch_items = cruft_sweep.sweep_scratch(
        repo_root, 7, apply=True, json_mode=False, quiet=True,
    )
    assert scratch_items == 1
    assert not target.exists()

    empty_bytes, empty_items = cruft_sweep.sweep_empty_toplevel_dirs(
        repo_root, apply=True, json_mode=False, quiet=True,
    )
    assert empty_items == 0


_SP_SID_DEAD_OLD = "33333333-3333-3333-3333-333333333333"
_SP_SID_SELF = "44444444-4444-4444-4444-444444444444"


def _build_scratchpad_fixture(tmp_path, project_slug="X--claude-klabauter"):
    claude_root = tmp_path / "claude" / project_slug
    claude_root.mkdir(parents=True)

    for sid in (_SP_SID_DEAD_OLD, _SP_SID_SELF):
        sdir = claude_root / sid
        sdir.mkdir()
        scratch = sdir / "scratchpad"
        scratch.mkdir()
        f = scratch / "a.txt"
        f.write_text("x" * 100, encoding="utf-8")
        if sid == _SP_SID_DEAD_OLD:
            old_stamp = time.time() - 10 * 86400
            os.utime(f, (old_stamp, old_stamp))

    return tmp_path


@pytest.fixture
def _patch_scratchpad_liveness(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.ops.scratchpad_sweep._session_liveness.session_live",
        lambda sid, cwd=None: False,
    )


def _sweep_scratchpad_kwargs(tmp_path, **overrides):
    kwargs = dict(
        temp_root=str(tmp_path),
        self_session_id=_SP_SID_SELF,
        slug_to_root_map={"X--claude-klabauter": "X:/claude-klabauter"},
    )
    kwargs.update(overrides)
    return kwargs


def test_run_all_phases_scratchpad_class_dispatches_phase(tmp_path, monkeypatch, _patch_scratchpad_liveness):
    _build_scratchpad_fixture(tmp_path)
    called = {}

    def _fake_sweep(*, apply, json_mode, quiet, log_path=None, emit_fn=None, **kwargs):
        called["hit"] = True
        called["apply"] = apply
        return 0, 0

    monkeypatch.setattr(cruft_sweep, "sweep_harness_scratchpads", _fake_sweep)
    totals = cruft_sweep._run_all_phases(
        "scratchpad", False, False, True, 14, 7,
        Path("/nonexistent-projects"), Path("/nonexistent-fh"), None, None,
        None, [], [], None, None,
    )
    assert called.get("hit") is True
    assert "scratchpad" in totals


def test_run_all_phases_harness_and_scratch_classes_do_not_dispatch_scratchpad(monkeypatch):
    called = {"hit": False}

    def _fake_sweep(*args, **kwargs):
        called["hit"] = True
        return 0, 0

    monkeypatch.setattr(cruft_sweep, "sweep_harness_scratchpads", _fake_sweep)

    cruft_sweep._run_all_phases(
        "harness", False, False, True, 14, 7,
        Path("/nonexistent-projects"), Path("/nonexistent-fh"), None, None,
        None, [], [], None, None,
    )
    assert called["hit"] is False

    cruft_sweep._run_all_phases(
        "scratch", False, False, True, 14, 7,
        Path("/nonexistent-projects"), Path("/nonexistent-fh"), None, None,
        Path("/nonexistent-repo-root"), [], [], None, None,
    )
    assert called["hit"] is False


def test_run_all_phases_all_class_includes_scratchpad_in_grand_total(tmp_path, monkeypatch, _patch_scratchpad_liveness):
    _build_scratchpad_fixture(tmp_path)

    def _fake_sweep(*, apply, json_mode, quiet, log_path=None, emit_fn=None, **kwargs):
        return 12345, 1

    monkeypatch.setattr(cruft_sweep, "sweep_harness_scratchpads", _fake_sweep)

    totals = cruft_sweep._run_all_phases(
        "all", False, False, True, 14, 7,
        Path("/nonexistent-projects"), Path("/nonexistent-fh"), None, None,
        Path(str(tmp_path / "not-a-repo")), [], [], None, None,
    )
    assert totals["scratchpad"] == {"bytes": 12345, "items": 1}
    assert totals["_grand_total_bytes"] >= 12345
    assert totals["_grand_total_items"] >= 1


def test_sweep_harness_scratchpads_dry_run_deletes_nothing(tmp_path, _patch_scratchpad_liveness):
    _build_scratchpad_fixture(tmp_path)
    old_scratch = tmp_path / "claude" / "X--claude-klabauter" / _SP_SID_DEAD_OLD / "scratchpad"
    assert old_scratch.is_dir()

    total_bytes, total_items = cruft_sweep.sweep_harness_scratchpads(
        apply=False, json_mode=False, quiet=True,
        **_sweep_scratchpad_kwargs(tmp_path),
    )

    assert old_scratch.is_dir(), "dry-run (apply=False) must never delete"
    assert total_items == 1
    assert total_bytes == 100


def test_sweep_harness_scratchpads_apply_deletes_real_dead_scratchpad(tmp_path, _patch_scratchpad_liveness):
    _build_scratchpad_fixture(tmp_path)
    old_scratch = tmp_path / "claude" / "X--claude-klabauter" / _SP_SID_DEAD_OLD / "scratchpad"
    self_scratch = tmp_path / "claude" / "X--claude-klabauter" / _SP_SID_SELF / "scratchpad"
    assert old_scratch.is_dir()
    assert self_scratch.is_dir()

    total_bytes, total_items = cruft_sweep.sweep_harness_scratchpads(
        apply=True, json_mode=False, quiet=True,
        **_sweep_scratchpad_kwargs(tmp_path),
    )

    assert not old_scratch.is_dir(), "apply=True must actually delete the reclaimable scratchpad"
    assert self_scratch.is_dir(), "the invoking session's own scratchpad must never be touched"
    assert total_items == 1
    assert total_bytes == 100


def test_sweep_harness_scratchpads_apply_maps_to_reclaim(tmp_path, monkeypatch, _patch_scratchpad_liveness):
    captured = {}

    def _fake_sweep_scratchpads(**kwargs):
        captured.update(kwargs)
        return {
            "entries": [], "counts": {}, "ttl_days": kwargs.get("ttl_days", 7),
            "bytes_reclaimable": 0, "bytes_reclaimed": 0,
        }

    monkeypatch.setattr(
        "coordinator_core.ops.scratchpad_sweep.sweep_scratchpads", _fake_sweep_scratchpads
    )

    cruft_sweep.sweep_harness_scratchpads(
        apply=True, json_mode=False, quiet=True,
        **_sweep_scratchpad_kwargs(tmp_path),
    )

    assert captured.get("reclaim") is True


def test_sweep_harness_scratchpads_dry_run_maps_apply_false_to_reclaim_false(tmp_path, monkeypatch, _patch_scratchpad_liveness):
    captured = {}

    def _fake_sweep_scratchpads(**kwargs):
        captured.update(kwargs)
        return {
            "entries": [], "counts": {}, "ttl_days": kwargs.get("ttl_days", 7),
            "bytes_reclaimable": 0, "bytes_reclaimed": 0,
        }

    monkeypatch.setattr(
        "coordinator_core.ops.scratchpad_sweep.sweep_scratchpads", _fake_sweep_scratchpads
    )

    cruft_sweep.sweep_harness_scratchpads(
        apply=False, json_mode=False, quiet=True,
        **_sweep_scratchpad_kwargs(tmp_path),
    )

    assert captured.get("reclaim") is False


class _FakeCompletedProcess:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = b""
        self.stderr = b""


def test_sweep_toolchain_caches_invokes_resolved_full_path_not_bare_name(tmp_path, monkeypatch):
    cmd_shaped_path = str(tmp_path / "npm.CMD")

    def _fake_which(name):
        if name == "npm":
            return cmd_shaped_path
        return None

    captured_argv = {}

    def _fake_run(argv, **kwargs):
        captured_argv["argv"] = argv
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(cruft_sweep.shutil, "which", _fake_which)
    monkeypatch.setattr(cruft_sweep.subprocess, "run", _fake_run)

    total_bytes, total_items = cruft_sweep.sweep_toolchain_caches(
        apply=True, json_mode=False, quiet=True,
    )

    # Only npm resolves in this fixture; every other tool is UNAVAILABLE.
    assert total_items == 1
    assert total_bytes == 0
    assert captured_argv["argv"][0] == cmd_shaped_path
    assert captured_argv["argv"][0] != "npm"
    assert Path(captured_argv["argv"][0]).name == "npm.CMD"


def test_sweep_toolchain_caches_absent_tool_is_unavailable_not_a_phase_failure(monkeypatch):
    """AC4: a tool absent from PATH yields UNAVAILABLE for that row and
    never fails the phase."""
    monkeypatch.setattr(cruft_sweep.shutil, "which", lambda name: None)
    run_calls = []
    monkeypatch.setattr(
        cruft_sweep.subprocess, "run",
        lambda *a, **k: run_calls.append((a, k)) or _FakeCompletedProcess(),
    )

    records = []
    total_bytes, total_items = cruft_sweep.sweep_toolchain_caches(
        apply=True, json_mode=True, quiet=True, emit_fn=records.append,
    )

    assert total_items == 0
    assert run_calls == []
    assert len(records) == len(cruft_sweep._TOOLCHAIN_CACHE_TOOLS)
    assert all(r["disposition"] == "unavailable" for r in records)
    assert all("UNAVAILABLE" in r["evidence"] for r in records)


def test_sweep_toolchain_caches_one_tool_erroring_does_not_abort_others(monkeypatch):
    def _fake_which(name):
        return f"/resolved/{name}"

    def _fake_run(argv, **kwargs):
        if "uv" in argv[0]:
            raise OSError("simulated spawn failure")
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(cruft_sweep.shutil, "which", _fake_which)
    monkeypatch.setattr(cruft_sweep.subprocess, "run", _fake_run)

    records = []
    total_bytes, total_items = cruft_sweep.sweep_toolchain_caches(
        apply=True, json_mode=True, quiet=True, emit_fn=records.append,
    )

    assert total_items == len(cruft_sweep._TOOLCHAIN_CACHE_TOOLS) - 1
    uv_records = [r for r in records if r["name"] == "uv"]
    assert len(uv_records) == 1
    assert uv_records[0]["disposition"] == "prune-failed"
    other_records = [r for r in records if r["name"] != "uv"]
    assert all(r["disposition"] == "auto-prune" for r in other_records)


def test_sweep_toolchain_caches_dry_run_issues_no_mutating_call(monkeypatch):
    monkeypatch.setattr(cruft_sweep.shutil, "which", lambda name: f"/resolved/{name}")
    run_calls = []
    monkeypatch.setattr(
        cruft_sweep.subprocess, "run",
        lambda *a, **k: run_calls.append((a, k)) or _FakeCompletedProcess(),
    )

    records = []
    total_bytes, total_items = cruft_sweep.sweep_toolchain_caches(
        apply=False, json_mode=True, quiet=True, emit_fn=records.append,
    )

    assert run_calls == []
    assert total_items == 0
    assert total_bytes == 0
    assert len(records) == len(cruft_sweep._TOOLCHAIN_CACHE_TOOLS)
    assert all(r["disposition"] == "skip" for r in records)
    assert all("no dry-run available" in r["evidence"] for r in records)


def test_sweep_toolchain_caches_dry_run_argv_invoked_but_never_mutates(tmp_path, monkeypatch):
    """AC5, dry_run_argv wiring: a row WITH a native `dry_run_argv` must
    still issue no MUTATING call under apply=False -- the dry-run argv is
    invoked instead of `prune_argv`, and `prune_argv` is never touched."""
    fake_table = (
        {
            "name": "uv", "executable": "uv",
            "prune_argv": ("cache", "prune"),
            "dry_run_argv": ("cache", "prune", "--dry-run"),
            "wholesale": False,
        },
    )
    monkeypatch.setattr(cruft_sweep, "_TOOLCHAIN_CACHE_TOOLS", fake_table)
    monkeypatch.setattr(cruft_sweep.shutil, "which", lambda name: f"/resolved/{name}")

    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(cruft_sweep.subprocess, "run", _fake_run)

    records = []
    total_bytes, total_items = cruft_sweep.sweep_toolchain_caches(
        apply=False, json_mode=True, quiet=True, emit_fn=records.append,
    )

    assert len(calls) == 1
    assert calls[0] == ["/resolved/uv", "cache", "prune", "--dry-run"]
    assert calls[0] != ["/resolved/uv", "cache", "prune"]
    assert total_items == 0
    assert total_bytes == 0
    assert len(records) == 1
    assert records[0]["disposition"] == "dry-run"
    assert "dry-run, no mutation" in records[0]["evidence"]
    assert "wholesale=False" in records[0]["evidence"]


def test_run_all_phases_toolchain_caches_class_dispatches_phase(monkeypatch):
    called = {}

    def _fake_sweep(*, apply, json_mode, quiet, log_path=None, emit_fn=None):
        called["hit"] = True
        called["apply"] = apply
        return 0, 0

    monkeypatch.setattr(cruft_sweep, "sweep_toolchain_caches", _fake_sweep)
    totals = cruft_sweep._run_all_phases(
        "toolchain-caches", False, False, True, 14, 7,
        Path("/nonexistent-projects"), Path("/nonexistent-fh"), None, None,
        None, [], [], None, None,
    )
    assert called.get("hit") is True
    assert "toolchain_caches" in totals


def test_run_all_phases_other_classes_do_not_dispatch_toolchain_caches(monkeypatch):
    called = {"hit": False}

    def _fake_sweep(*args, **kwargs):
        called["hit"] = True
        return 0, 0

    monkeypatch.setattr(cruft_sweep, "sweep_toolchain_caches", _fake_sweep)

    cruft_sweep._run_all_phases(
        "harness", False, False, True, 14, 7,
        Path("/nonexistent-projects"), Path("/nonexistent-fh"), None, None,
        None, [], [], None, None,
    )
    assert called["hit"] is False


def test_run_all_phases_all_class_includes_toolchain_caches_in_grand_total(monkeypatch):
    def _fake_sweep(*, apply, json_mode, quiet, log_path=None, emit_fn=None):
        return 0, 3

    monkeypatch.setattr(cruft_sweep, "sweep_toolchain_caches", _fake_sweep)

    totals = cruft_sweep._run_all_phases(
        "all", False, False, True, 14, 7,
        Path("/nonexistent-projects"), Path("/nonexistent-fh"), None, None,
        Path("/nonexistent-repo-root"), [], [], None, None,
    )
    assert totals["toolchain_caches"] == {"bytes": 0, "items": 3}
    assert totals["_grand_total_items"] >= 3


def test_toolchain_cache_tools_table_is_data_shape(tmp_path):
    names = [row["name"] for row in cruft_sweep._TOOLCHAIN_CACHE_TOOLS]
    assert "huggingface" in names
    assert "playwright" not in names
    for row in cruft_sweep._TOOLCHAIN_CACHE_TOOLS:
        assert set(row.keys()) == {"name", "executable", "prune_argv", "dry_run_argv", "wholesale"}
    pip_row = next(r for r in cruft_sweep._TOOLCHAIN_CACHE_TOOLS if r["name"] == "pip")
    assert pip_row["wholesale"] is True
    non_pip_rows = [r for r in cruft_sweep._TOOLCHAIN_CACHE_TOOLS if r["name"] != "pip"]
    assert all(r["wholesale"] is False for r in non_pip_rows)
    npm_row = next(r for r in cruft_sweep._TOOLCHAIN_CACHE_TOOLS if r["name"] == "npm")
    assert npm_row["prune_argv"] == ("cache", "verify")
    hf_row = next(r for r in cruft_sweep._TOOLCHAIN_CACHE_TOOLS if r["name"] == "huggingface")
    assert hf_row["executable"] == "hf"
    assert hf_row["executable"] != "huggingface-cli"


# ---------------------------------------------------------------------------
# BCHST-C2 — sub-tier retention: memory/ survival, blocklist shielding at
# sub-tier and cap, mtime floor, tool-results cap-only, mtime invariance,
# oldest-first budget.
#
# Spec: docs/plans/2026-09-26-bound-the-claude-home-sub-tier-retention.md (C2)
#
# T1's leg (i) is HEAD-compatible (no new sweep_harness kwargs) and must pass
# today. T1's legs (ii)/(iii), and T2-T6b, exercise `size_cap_bytes` /
# `size_cap_mtime_floor_secs` kwargs that C3 adds to `sweep_harness` — they
# fail with TypeError until C3 lands, by design (this row authors the tests
# the engine chunk must satisfy).
# ---------------------------------------------------------------------------

_HUGE_CAP_BYTES = 10 * 1024**4  # never reached by any fixture here


def _mk_session(repo_dir: Path, uuid: str, *, transcript: bool = True) -> Path:
    """Create projects/<repo>/<uuid>/ (and, by default, its sibling
    <uuid>.jsonl transcript). Returns the session dir."""
    session_dir = repo_dir / uuid
    session_dir.mkdir(parents=True)
    if transcript:
        _write(repo_dir / f"{uuid}.jsonl", "transcript\n")
    return session_dir


def _mk_subtier_file(
    session_dir: Path, artifact_class: str, name: str, *, content: str = "x", age_secs: int = None,
) -> Path:
    """Create session_dir/<artifact_class>/<name> (e.g. subagents/agent-1.jsonl
    or tool-results/blob.json), optionally aged."""
    d = session_dir / artifact_class
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    _write(f, content)
    if age_secs is not None:
        _age_path(f, age_secs)
    return f


def _mk_memory(repo_dir: Path, *, age_secs: int = None) -> tuple[Path, Path]:
    """Create projects/<repo>/memory/MEMORY.md plus a nested file, both aged
    if requested. Returns (top_file, nested_file)."""
    memory_dir = repo_dir / "memory"
    top = memory_dir / "MEMORY.md"
    nested = memory_dir / "nested" / "note.md"
    _write(top, "keep me")
    _write(nested, "keep me too")
    if age_secs is not None:
        _age_path(top, age_secs)
        _age_path(nested, age_secs)
    return top, nested


_UUID_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_UUID_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_UUID_C = "cccccccc-cccc-cccc-cccc-cccccccccccc"


# T1 is split into one test function per leg (Review finding 1): leg (i) is
# HEAD-compatible and must pass today, standalone, without ever touching the
# `size_cap_*` kwargs C3 has not yet added — combining all three legs into
# one function would make leg (i) untestable in isolation, since a shared
# function body raises TypeError on leg (ii)/(iii) before pytest can report
# leg (i) as its own pass/fail.


def test_memory_dir_survives_head_leg(tmp_path):
    """T1 (i): days=0, no new kwargs. HEAD-compatible -- must pass before C3
    lands (whole-dir/jsonl pass only)."""
    projects_root = tmp_path / "projects"
    repo_dir = projects_root / "repo"
    repo_dir.mkdir(parents=True)
    top, nested = _mk_memory(repo_dir, age_secs=400 * 86400)

    records = []
    cruft_sweep.sweep_harness(
        projects_root, tmp_path / "file-history", 0, set(),
        apply=True, json_mode=True, quiet=True, emit_fn=records.append,
    )
    assert top.exists() and top.read_text() == "keep me"
    assert nested.exists() and nested.read_text() == "keep me too"
    assert not any("/memory/" in r["path"].replace("\\", "/") for r in records)


def test_memory_dir_survives_subtier_pass(tmp_path):
    """T1 (ii): whole-dir pass inert (days huge), sub-tier active, cap huge.
    Runs only after C3 lands `size_cap_bytes` / `size_cap_mtime_floor_secs`."""
    projects_root = tmp_path / "projects"
    repo_dir = projects_root / "repo"
    repo_dir.mkdir(parents=True)
    top, nested = _mk_memory(repo_dir, age_secs=400 * 86400)

    records = []
    cruft_sweep.sweep_harness(
        projects_root, tmp_path / "file-history", 10000, set(),
        apply=True, json_mode=True, quiet=True, emit_fn=records.append,
        size_cap_bytes=_HUGE_CAP_BYTES, size_cap_mtime_floor_secs=0,
    )
    assert top.exists() and nested.exists()
    assert not any("/memory/" in r["path"].replace("\\", "/") for r in records)


def test_memory_dir_survives_size_cap_pass(tmp_path):
    """T1 (iii): cap=0, floor=0 -- maximal eviction pressure, memory/ still
    exempt (it is never a candidate: the UUID gate never descends into it).
    Runs only after C3 lands the cap kwargs."""
    projects_root = tmp_path / "projects"
    repo_dir = projects_root / "repo"
    repo_dir.mkdir(parents=True)
    top, nested = _mk_memory(repo_dir, age_secs=400 * 86400)

    records = []
    cruft_sweep.sweep_harness(
        projects_root, tmp_path / "file-history", 10000, set(),
        apply=True, json_mode=True, quiet=True, emit_fn=records.append,
        size_cap_bytes=0, size_cap_mtime_floor_secs=0,
    )
    assert top.exists() and nested.exists()
    assert not any("/memory/" in r["path"].replace("\\", "/") for r in records)


def test_subtier_age_never_enters_blocklisted_session(tmp_path):
    projects_root = tmp_path / "projects"
    repo_dir = projects_root / "repo"
    repo_dir.mkdir(parents=True)

    blocked_session = _mk_session(repo_dir, _UUID_A)
    blocked_file = _mk_subtier_file(
        blocked_session, "subagents", "agent-1.jsonl", age_secs=30 * 86400,
    )
    live_session = _mk_session(repo_dir, _UUID_B)
    live_file = _mk_subtier_file(
        live_session, "subagents", "agent-1.jsonl", age_secs=30 * 86400,
    )

    records = []
    cruft_sweep.sweep_harness(
        projects_root, tmp_path / "file-history", 10000, {_UUID_A},
        apply=True, json_mode=True, quiet=True, emit_fn=records.append,
        size_cap_bytes=_HUGE_CAP_BYTES, size_cap_mtime_floor_secs=0,
    )

    assert blocked_file.exists(), "blocklisted session's subagents file must survive the sub-tier pass"
    assert not any(str(blocked_session) in r["path"] for r in records), (
        "no record may be emitted for any file under a blocklisted session"
    )
    assert not live_file.exists(), "the non-blocklisted twin must be pruned by the 3-day subagents window"


def test_size_cap_honours_blocklist_memory_and_floor(tmp_path):
    projects_root = tmp_path / "projects"
    repo_dir = projects_root / "repo"
    repo_dir.mkdir(parents=True)

    top, nested = _mk_memory(repo_dir, age_secs=400 * 86400)

    blocked_session = _mk_session(repo_dir, _UUID_A)
    blocked_file = _mk_subtier_file(
        blocked_session, "tool-results", "blob.json", content="x" * 5000, age_secs=30 * 86400,
    )

    floor_secs = 86400
    young_session = _mk_session(repo_dir, _UUID_B)
    young_file = _mk_subtier_file(
        young_session, "tool-results", "blob.json", content="x" * 5000, age_secs=100,
    )

    old_session = _mk_session(repo_dir, _UUID_C)
    old_file = _mk_subtier_file(
        old_session, "tool-results", "blob.json", content="x" * 5000, age_secs=30 * 86400,
    )

    # Cap set below the total of (young + old) eligible bytes, so the floor
    # keeps the pass from reaching the cap: only `old_file` is eligible.
    cap_bytes = 1000

    records = []
    cruft_sweep.sweep_harness(
        projects_root, tmp_path / "file-history", 10000, {_UUID_A},
        apply=True, json_mode=True, quiet=True, emit_fn=records.append,
        size_cap_bytes=cap_bytes, size_cap_mtime_floor_secs=floor_secs,
    )

    assert top.exists() and nested.exists(), "memory/ must survive the cap pass"
    assert blocked_file.exists(), "blocklisted session's files must survive the cap pass"
    assert young_file.exists(), "a file younger than the mtime floor is never removed by cap pressure"
    assert not old_file.exists(), "the old, non-blocklisted, non-floor-protected file is evicted"

    floor_skip_records = [
        r for r in records
        if r["disposition"] == "skip" and r["evidence"] == "size-cap mtime floor"
    ]
    # The fixture's session transcripts are also younger than the floor, so they
    # are floor-skipped too; the subject here is the young tool-results blob.
    assert str(young_file) in {r["path"] for r in floor_skip_records}
    assert str(old_file) not in {r["path"] for r in floor_skip_records}

    residue_records = [r for r in records if r["name"] == "size-cap-residue"]
    assert len(residue_records) == 1
    residue = residue_records[0]
    assert residue["evidence"] == "size-cap residue protected-by-floor"
    assert residue["disposition"] == "skip"
    assert residue["path"] == str(projects_root)
    # post-eviction total (UUID-gated, non-blocklisted, memory/ excluded) is
    # every floor-protected survivor: young_file plus the fixture's session
    # transcripts outside the blocklist.
    expected_total_after_eviction = sum(
        r["size_bytes"] for r in floor_skip_records
    )
    assert residue["size_bytes"] == expected_total_after_eviction - cap_bytes

    # Sibling run where the cap is reachable: no residue row.
    other_root = tmp_path / "projects-reachable"
    other_repo = other_root / "repo"
    other_repo.mkdir(parents=True)
    reachable_session = _mk_session(other_repo, _UUID_C)
    _mk_subtier_file(
        reachable_session, "tool-results", "blob.json", content="x" * 5000, age_secs=30 * 86400,
    )
    reachable_records = []
    cruft_sweep.sweep_harness(
        other_root, tmp_path / "file-history-2", 10000, set(),
        apply=True, json_mode=True, quiet=True, emit_fn=reachable_records.append,
        size_cap_bytes=_HUGE_CAP_BYTES, size_cap_mtime_floor_secs=0,
    )
    assert not any(r["name"] == "size-cap-residue" for r in reachable_records)


def test_tool_results_never_age_pruned_but_cap_eligible(tmp_path):
    projects_root = tmp_path / "projects"
    repo_dir = projects_root / "repo"
    repo_dir.mkdir(parents=True)

    session = _mk_session(repo_dir, _UUID_A)
    tr_file = _mk_subtier_file(
        session, "tool-results", "blob.json", content="x" * 5000, age_secs=30 * 86400,
    )

    # Huge cap: the age pass never touches tool-results (no window for it).
    cruft_sweep.sweep_harness(
        projects_root, tmp_path / "file-history", 10000, set(),
        apply=True, json_mode=False, quiet=True,
        size_cap_bytes=_HUGE_CAP_BYTES, size_cap_mtime_floor_secs=0,
    )
    assert tr_file.exists(), "tool-results/ must never be pruned by an age window"

    # Tight cap, floor=0: the same file is now cap-eligible.
    cruft_sweep.sweep_harness(
        projects_root, tmp_path / "file-history", 10000, set(),
        apply=True, json_mode=False, quiet=True,
        size_cap_bytes=0, size_cap_mtime_floor_secs=0,
    )
    assert not tr_file.exists(), "tool-results/ is cap-eligible once the cap is tight"


def test_new_passes_never_change_uuid_dir_mtime(tmp_path):
    projects_root = tmp_path / "projects"
    repo_dir = projects_root / "repo"
    repo_dir.mkdir(parents=True)

    session = _mk_session(repo_dir, _UUID_A)
    _mk_subtier_file(session, "subagents", "agent-1.jsonl", age_secs=30 * 86400)
    _mk_subtier_file(session, "tool-results", "blob.json", content="x" * 5000, age_secs=30 * 86400)
    depth1_file = session / "ccr-tip.json"
    _write(depth1_file, "{}")
    _age_path(depth1_file, 30 * 86400)
    _age_path(session, 30 * 86400)

    before_mtime = session.stat().st_mtime

    cruft_sweep.sweep_harness(
        projects_root, tmp_path / "file-history", 10000, set(),
        apply=True, json_mode=False, quiet=True,
        size_cap_bytes=0, size_cap_mtime_floor_secs=0,
    )

    after_mtime = session.stat().st_mtime
    assert after_mtime == before_mtime, "evicting files inside <uuid>/ must never touch its own mtime"
    assert depth1_file.exists(), "depth-1 files directly inside <uuid>/ are never candidates"


def test_size_cap_dry_run_deletes_nothing_and_matches_apply_set(tmp_path):
    def _build(root: Path) -> None:
        repo_dir = root / "repo"
        repo_dir.mkdir(parents=True)
        for uuid in (_UUID_A, _UUID_B, _UUID_C):
            session = _mk_session(repo_dir, uuid)
            _mk_subtier_file(
                session, "tool-results", "blob.json", content="x" * 5000, age_secs=10 * 86400,
            )

    dry_root = tmp_path / "dry"
    apply_root = tmp_path / "apply"
    _build(dry_root)
    _build(apply_root)

    dry_records = []
    cruft_sweep.sweep_harness(
        dry_root, tmp_path / "fh-dry", 10000, set(),
        apply=False, json_mode=True, quiet=True, emit_fn=dry_records.append,
        size_cap_bytes=1000, size_cap_mtime_floor_secs=0,
    )
    apply_records = []
    cruft_sweep.sweep_harness(
        apply_root, tmp_path / "fh-apply", 10000, set(),
        apply=True, json_mode=True, quiet=True, emit_fn=apply_records.append,
        size_cap_bytes=1000, size_cap_mtime_floor_secs=0,
    )

    def _evicted_paths(recs):
        return {
            r["path"] for r in recs
            if r["evidence"].startswith("size-cap") and r["disposition"] == "auto-prune"
        }

    dry_evicted = _evicted_paths(dry_records)
    apply_evicted = {
        p.replace(str(apply_root), str(dry_root)) for p in _evicted_paths(apply_records)
    }
    assert dry_evicted == apply_evicted
    assert dry_evicted, "fixture is over the cap, so the eviction set must be non-empty"

    for uuid in (_UUID_A, _UUID_B, _UUID_C):
        f = dry_root / "repo" / uuid / "tool-results" / "blob.json"
        assert f.exists(), "dry-run must delete nothing"


def test_size_cap_dry_run_matches_apply_when_whole_dir_pass_has_candidates(tmp_path):
    def _build(root: Path) -> Path:
        repo_dir = root / "repo"
        repo_dir.mkdir(parents=True)
        # whole-dir-pass-eligible (>14-day) session -- its own bytes are the
        # whole-dir pass's candidate, and must be excluded from the cap
        # total the same way in dry-run and apply.
        stale_session = _mk_session(repo_dir, _UUID_A)
        _mk_subtier_file(
            stale_session, "tool-results", "blob.json", content="x" * 5000, age_secs=20 * 86400,
        )
        _age_path(stale_session, 20 * 86400)

        # cap-pass-eligible session, younger than the whole-dir threshold.
        live_session = _mk_session(repo_dir, _UUID_B)
        _mk_subtier_file(
            live_session, "tool-results", "blob.json", content="x" * 5000, age_secs=10 * 86400,
        )
        return root

    dry_root = tmp_path / "dry"
    apply_root = tmp_path / "apply"
    _build(dry_root)
    _build(apply_root)

    dry_records = []
    cruft_sweep.sweep_harness(
        dry_root, tmp_path / "fh-dry", 14, set(),
        apply=False, json_mode=True, quiet=True, emit_fn=dry_records.append,
        size_cap_bytes=1000, size_cap_mtime_floor_secs=0,
    )
    apply_records = []
    cruft_sweep.sweep_harness(
        apply_root, tmp_path / "fh-apply", 14, set(),
        apply=True, json_mode=True, quiet=True, emit_fn=apply_records.append,
        size_cap_bytes=1000, size_cap_mtime_floor_secs=0,
    )

    def _cap_evicted_paths(recs):
        return {
            r["path"] for r in recs
            if r["evidence"].startswith("size-cap") and r["disposition"] == "auto-prune"
        }

    dry_cap_evicted = _cap_evicted_paths(dry_records)
    apply_cap_evicted = {
        p.replace(str(apply_root), str(dry_root)) for p in _cap_evicted_paths(apply_records)
    }
    assert dry_cap_evicted == apply_cap_evicted, (
        "dry-run must exclude the whole-dir pass's own candidate bytes from the cap total "
        "the same way apply does, or the two eviction sets diverge"
    )

    for uuid in (_UUID_A, _UUID_B):
        f = dry_root / "repo" / uuid / "tool-results" / "blob.json"
        assert f.exists(), "dry-run must delete nothing, including the whole-dir pass's own candidates"
