"""
Tests for coordinator_core.ops.cruft_sweep — failure-path coverage for the
2026-07-22 break-class fixes.

Covers:
  - build_uuid_blocklist: an unreadable handoff .md file is warned about and
    marks the scan incomplete (complete=False), while UUIDs from other
    readable files are still collected.
  - _run_handler (the harness class): apply=True against an incomplete
    blocklist scan raises BlocklistIncompleteError rather than proceeding
    with a silently-narrowed protected set (fail-closed).
  - _delete_path / _delete_file: return True only when the target is
    confirmed gone; False on a failing subprocess/unlink.
  - sweep_orphans (representative sweep_* call site): a failed delete is
    NOT counted as pruned and emits a WARNING; a successful delete IS
    counted, matching prior behavior.
  - sweep_scratch: its own directory-discovery os.walk warns on an
    unwalkable subtree rather than silently dropping it from the scan
    (Finding 1, 2026-07-22 code-reviewer slice1 review), with a
    genuinely-clean companion proving the signal discriminates.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.ops import cruft_sweep


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# build_uuid_blocklist
# ---------------------------------------------------------------------------


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
    """An unreadable handoffs_dir itself (not just a file inside it) must not
    silently look like "zero handoffs, complete scan" — Path.glob() swallows
    PermissionError on an unreadable directory and yields an empty iterator,
    so the pre-fix code returned (set(), True) having scanned nothing.
    """
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


# ---------------------------------------------------------------------------
# _run_handler — fail-closed abort on incomplete blocklist + apply=True
# ---------------------------------------------------------------------------


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
    """Read-only invocations aren't destructive, so an incomplete blocklist
    scan is a warning (already emitted by build_uuid_blocklist), not an
    abort — only apply=True triggers the fail-closed guard."""
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


# ---------------------------------------------------------------------------
# _delete_path / _delete_file — return-value contract
# ---------------------------------------------------------------------------


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
    assert target.exists()  # untouched — the fake subprocess.run never really ran rm


def test_delete_file_returns_false_on_oserror(tmp_path, monkeypatch):
    target = tmp_path / "victim.txt"
    target.write_text("x")

    def _raise(*a, **k):
        raise OSError("simulated unlink failure")

    monkeypatch.setattr(Path, "unlink", _raise)
    assert cruft_sweep._delete_file(target) is False


# ---------------------------------------------------------------------------
# sweep_orphans — representative call site: failed delete not counted
# ---------------------------------------------------------------------------


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
    assert child.exists()  # still there — matches "not actually removed"
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


# ---------------------------------------------------------------------------
# sweep_scratch — its own directory-discovery os.walk must not silently
# swallow an unreadable subtree (Finding 1, 2026-07-22 slice1 review)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="chmod-based unreadable-subtree fixture is POSIX-only")
def test_sweep_scratch_warns_on_unwalkable_subtree(tmp_path, capsys):
    """A subtree os.walk cannot descend into must be named in a stderr WARNING
    rather than silently dropping out of the scan -- the same defect class
    this session's other fixes eliminated elsewhere in this file."""
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
    """Distinguishes the walk-error signal above from an actually-walkable
    tree -- the warning must not fire when nothing was unreadable."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "some-dir").mkdir()

    cruft_sweep.sweep_scratch(
        repo_root, 7, apply=False, json_mode=False, quiet=True,
    )

    assert "WARNING" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# sweep_empty_toplevel_dirs — net-new Phase E (no bash-oracle counterpart).
# Regression coverage for the 2026-07-22..2026-07-28 incident: three
# top-level empty-dir cruft items (a fake-$HOME skeleton and two bare
# prose-word dirs) sat undetected at a repo root for a week.
# ---------------------------------------------------------------------------


def _init_git_repo(repo_root: Path) -> None:
    import subprocess as _subprocess

    _subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    _subprocess.run(
        ["git", "-C", str(repo_root), "config", "user.email", "test@example.com"],
        check=True,
    )
    _subprocess.run(
        ["git", "-C", str(repo_root), "config", "user.name", "Test"], check=True,
    )


def _age_path(path: Path, age_secs: int) -> None:
    old = time.time() - age_secs
    os.utime(path, (old, old))


def _age_subtree(root: Path, age_secs: int) -> None:
    """Set mtime on every directory in `root`'s subtree (and root itself) to
    `age_secs` in the past — sweep_empty_toplevel_dirs gates on the subtree
    MAX mtime, so every level must be aged for an "old" fixture."""
    for dirpath, dirnames, _files in os.walk(root):
        _age_path(Path(dirpath), age_secs)
    _age_path(root, age_secs)


def test_sweep_empty_dirs_nested_empty_skeleton_is_pruned(tmp_path):
    """Regression test for the actual incident: a fake-$HOME skeleton with
    ten levels of nested empty directories and zero files anywhere."""
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
    """The bare-empty case — an arbitrary prose-word dir name, including one
    with literal dots in it (`not...`), must not choke anything."""
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
    # No aging applied — mtime is "now", well under the 24h floor.

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
    assert target.exists()  # dry-run — nothing actually deleted


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


# ---------------------------------------------------------------------------
# Drift-audit D3 — _dir_size_bytes wall-clock budget (ported from the
# coordinator/bin/cruft-sweep trampoline's own hang-bug fix; see
# docs/research/2026-07-28-cruft-sweep-duplicate-port-drift-audit.md).
# ---------------------------------------------------------------------------


def test_dir_size_bytes_respects_budget(tmp_path, monkeypatch):
    """A budget of 0 must bail before the walk visits anything past the
    top-level stat — proves the deadline check is live, not decorative."""
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

    # budget_secs=0 means the deadline (monotonic() + 0) is already in the
    # past by the time the loop checks it — the walk must break immediately.
    cruft_sweep._dir_size_bytes(d, budget_secs=0.0)
    assert len(visited) <= 1, (
        "budget=0 should bail on the first os.walk() iteration, not walk "
        f"the whole tree; visited {len(visited)} levels"
    )


def test_dir_size_bytes_still_sums_normally_under_a_generous_budget(tmp_path):
    """Sanity check that the new budget kwarg doesn't change output for a
    small tree finishing well within its deadline."""
    d = tmp_path / "small"
    d.mkdir()
    (d / "a.txt").write_text("hello")

    size = cruft_sweep._dir_size_bytes(d, budget_secs=5.0)
    assert size >= 0  # KB-rounded; a handful of bytes rounds down to 0, that's fine
    assert size == cruft_sweep._dir_size_bytes(d)  # default budget, same result


# ---------------------------------------------------------------------------
# Drift-audit D4 — sweep_scratch's already-pruned-parent skip must recognize
# BOTH path separators, not just "/" (os.walk yields native/backslash paths
# on Windows; a forward-slash-only check silently never matches there,
# double-counting an already-deleted child as a fresh prune).
# ---------------------------------------------------------------------------


def test_is_pruned_child_recognizes_both_separators():
    pruned = ["C:\\repo\\nonexistent"]
    assert cruft_sweep._is_pruned_child("C:\\repo\\nonexistent", pruned) is True
    assert cruft_sweep._is_pruned_child("C:\\repo\\nonexistent\\sub", pruned) is True
    assert cruft_sweep._is_pruned_child("/repo/nonexistent/sub", ["/repo/nonexistent"]) is True
    assert cruft_sweep._is_pruned_child("/repo/other", ["/repo/nonexistent"]) is False


# ---------------------------------------------------------------------------
# Drift-audit D10 — Phase E / Phase B scratch duplicate-emission wrinkle.
# An empty, untracked, aged top-level dir whose name is ALSO in Phase B's
# auto-prune-name vocabulary (_is_auto_prune_name) must not be reported as
# an independent "auto-prune" finding by Phase E — a JSON/dry-run consumer
# summing "auto-prune" records per class would otherwise double-count one
# physical directory under both "scratch" and "empty-dirs".
# ---------------------------------------------------------------------------


def test_scratch_and_empty_dirs_both_match_same_path_reproduction(tmp_path):
    """Reproduces the overlap: build a fixture where sweep_scratch AND
    sweep_empty_toplevel_dirs both independently classify the same top-level
    'nonexistent/' dir as auto-prune-eligible in dry-run/json mode."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)

    target = repo_root / "nonexistent"
    target.mkdir()
    _age_path(target, 10 * 86400)  # 10 days: past both Phase B's 7d default and Phase E's 24h floor

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

    # Phase E must NOT independently emit an "auto-prune" for the same path —
    # it must relabel it as a duplicate of the scratch-class finding instead.
    empty_dirs_auto_prune = [r for r in empty_dirs_records if r["disposition"] == "auto-prune"]
    assert empty_dirs_auto_prune == [], (
        "Phase E emitted an independent auto-prune record for a path Phase B "
        "already owns — this double-counts one directory as two reclaim items"
    )
    duplicate_records = [r for r in empty_dirs_records if r["disposition"] == "duplicate-of-scratch"]
    assert len(duplicate_records) == 1
    assert duplicate_records[0]["path"] == str(target)


def test_empty_dirs_non_scratch_name_is_not_relabeled(tmp_path):
    """Control case: a name NOT in Phase B's auto-prune vocabulary (an
    arbitrary prose word) must still get Phase E's normal "auto-prune"
    disposition — the relabel is scoped to the actual overlap, not blanket
    suppression of Phase E's own findings."""
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
    """--apply mode is unaffected by the relabel: dispatch order (scratch
    before empty-dirs) already means Phase E finds nothing once Phase B has
    deleted the directory — confirming the brief's claim that apply mode
    never double-counted in the first place."""
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
