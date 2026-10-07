"""Pins the read-only purge planner against the shared two-root fixture."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest

from coordinator_core.install.junction import is_junction
from coordinator_core.ops.fleet import scratch_hygiene_plan as plan
from coordinator_core.ops.fleet.tests._scratch_hygiene_fixture import build_two_root_fixture


def _backdate(path: Path) -> None:
    """Backdate ``path`` and its subtree, never touching a link (utime would follow it on Windows)."""
    stamp = time.time() - 10 * 24 * 3600
    stack = [path]
    while stack:
        p = stack.pop()
        if os.path.islink(p) or is_junction(p):
            continue
        os.utime(p, (stamp, stamp))
        if os.path.isdir(p):
            stack.extend(p / n for n in os.listdir(p))


@pytest.fixture
def fx(tmp_path, monkeypatch):
    f = build_two_root_fixture(tmp_path, monkeypatch)
    # The shared fixture's age() swallows NotImplementedError from utime(follow_symlinks=False),
    # which Windows raises, leaving every entry young; re-age here.
    for p, action in f.expected.items():
        if action != "skipped-young" and not (os.path.islink(p) or is_junction(p)):
            _backdate(p)
    _backdate(f.temp_root / "pytest")
    yield f
    f.close()


def _plan(fx, **kw):
    return plan.plan_purge(
        fx.repo_root, registry_dir=fx.registry_dir, verify_copy=fx.verify_copy, **kw
    )


def _by_path(fx, records):
    return {os.path.normcase(str(fx.repo_root / r["path"])): r for r in records}


def _snapshot(*roots: Path):
    out = {}
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            for n in dirnames + filenames:
                p = os.path.join(dirpath, n)
                st = os.lstat(p)
                digest = ""
                if os.path.isfile(p) and not os.path.islink(p):
                    digest = hashlib.sha256(Path(p).read_bytes()).hexdigest()
                out[p] = (st.st_mtime_ns, st.st_size, digest)
    return out


def test_every_entry_classified_as_expected(fx):
    got = _by_path(fx, _plan(fx))
    assert set(got) == {os.path.normcase(str(p)) for p in fx.expected}
    for path, action in fx.expected.items():
        rec = got[os.path.normcase(str(path))]
        assert rec["action"] == action, (path, rec)
        want = fx.expected_reason.get(path)
        if want:
            assert rec["reason"] == want


def test_would_delete_set_and_skips_present(fx):
    actions = {r["action"] for r in _plan(fx)}
    assert {"would-delete", "skipped-link", "skipped-live", "skipped-young", "skipped-unverified"} <= actions


def test_pytest_dir_is_not_an_entry_but_its_runs_are(fx):
    paths = [r["path"] for r in _plan(fx)]
    assert not any(p.endswith("/pytest") for p in paths)
    assert any(p.endswith("pytest/run-old") for p in paths)


def test_planning_deletes_nothing(fx):
    before = _snapshot(fx.scratch, fx.temp_root, fx.outside, fx.hold)
    _plan(fx)
    assert _snapshot(fx.scratch, fx.temp_root, fx.outside, fx.hold) == before


def test_planning_process_time_under_half_second(fx):
    start = time.process_time()
    _plan(fx)
    assert time.process_time() - start < 0.5


def test_missing_roots_are_not_an_error(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))
    assert plan.plan_purge(tmp_path / "bare-repo", registry_dir=tmp_path / "none") == []


def test_young_window_is_configurable(fx):
    old = fx.scratch / "old-plain"
    rec = plan.classify_entry(old, repo_root=fx.repo_root, registry_dir=fx.registry_dir, quiescence_hours=24 * 30)
    assert rec["action"] == "skipped-young"
    rec = plan.classify_entry(old, repo_root=fx.repo_root, registry_dir=fx.registry_dir)
    assert rec["action"] == "would-delete"
    assert rec["files"] == 2 and rec["bytes"] == 6 and rec["age_days"] >= 9


def test_classify_entry_rechecks_current_disk_state(fx):
    old = fx.scratch / "old-plain"
    assert plan.classify_entry(old, repo_root=fx.repo_root, registry_dir=fx.registry_dir)["action"] == "would-delete"
    (old / "late.txt").write_bytes(b"new")
    assert plan.classify_entry(old, repo_root=fx.repo_root, registry_dir=fx.registry_dir)["action"] == "skipped-young"


def test_matching_copy_is_not_unverified(fx):
    src = fx.scratch / "copy-source"
    (Path(fx.copy_dest) / "two.bin").write_bytes(b"abcd")
    rec = plan.classify_entry(
        src, repo_root=fx.repo_root, registry_dir=fx.registry_dir, verify_copy=fx.verify_copy
    )
    assert rec["action"] == "would-delete"


def test_top_level_file_entry(fx):
    f = fx.scratch / "loose.log"
    f.write_bytes(b"12345")
    _backdate(f)
    rec = plan.classify_entry(f, repo_root=fx.repo_root, registry_dir=fx.registry_dir)
    assert (rec["action"], rec["files"], rec["bytes"]) == ("would-delete", 1, 5)


def test_nested_link_is_not_traversed(fx):
    rec = plan.classify_entry(
        fx.scratch / "old-with-nested-link", repo_root=fx.repo_root, registry_dir=fx.registry_dir
    )
    assert rec["action"] == "would-delete" and rec["files"] == 1


def test_no_subprocess_spawned(fx, monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise AssertionError("planner spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    _plan(fx)
