from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from percolate import diff_commit  # noqa: E402
from percolate.diff_commit import DestDirtyError, DestWrite, land_diff  # noqa: E402

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True, **_NO_CONSOLE
    ).stdout


def _dest(tmp_path, n=3000):
    root = tmp_path / "dest"
    for i in range(n):
        p = root / f"d{i % 30}" / f"f{i}.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(f"v0 {i}\n".encode())
    (root / "bin").mkdir(exist_ok=True)
    (root / "bin" / "run.sh").write_bytes(b"#!/bin/sh\n")
    _git(root, "init", "-q", "-b", "work/z")
    _git(root, "config", "user.email", "t@local")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    return _dest(tmp_path_factory.mktemp("big"))


def _w(i, text):
    return DestWrite(f"d{i % 30}/f{i}.txt", text.encode(), 0o100644)


def _status(root):
    return _git(root, "status", "--porcelain")


def test_fifty_writes_commit_exactly_the_differing_paths(big):
    writes = [_w(i, f"v1 {i}\n") for i in range(50)]
    writes.append(_w(100, "v0 100\n"))  # unchanged
    out = land_diff(big, writes, [], "diff", commit=True)
    assert len(out.written) == 50 and out.unchanged == (_w(100, "").rel,)
    changed = _git(big, "diff", "--name-only", "HEAD~1", "HEAD").split()
    assert sorted(changed) == sorted(w.rel for w in writes[:50])
    assert _status(big) == ""


def test_all_unchanged_is_noop(big):
    head = _git(big, "rev-parse", "HEAD")
    out = land_diff(big, [_w(200, "v0 200\n")], ["nope.txt"], "m", commit=True)
    assert out.commit_sha is None and out.written == () and _git(big, "rev-parse", "HEAD") == head


def test_deletion_prunes_empty_dirs(tmp_path):
    root = _dest(tmp_path, 3)
    land_diff(root, [], ["bin/run.sh"], "rm", commit=True)
    assert not (root / "bin").exists() and _status(root) == ""


def test_dirty_path_refuses_without_writing(tmp_path):
    root = _dest(tmp_path, 5)
    (root / "d1" / "f1.txt").write_bytes(b"local edit\n")
    with pytest.raises(DestDirtyError):
        land_diff(root, [_w(0, "new\n"), _w(1, "new\n")], [], "m", commit=True)
    assert (root / "d0" / "f0.txt").read_bytes() == b"v0 0\n"
    assert (root / "d1" / "f1.txt").read_bytes() == b"local edit\n"


def test_a_derived_path_overwrites_its_own_stale_worktree_copy(tmp_path):
    root = _dest(tmp_path, 5)
    rel = _w(1, "").rel
    (root / rel).write_bytes(b"a prior round's uncommitted output\n")
    out = land_diff(root, [_w(1, "new\n")], [], "m", commit=True, derived=frozenset({rel}))
    assert out.commit_sha is not None and (root / rel).read_bytes() == b"new\n" and _status(root) == ""


def test_failure_mid_write_restores_clean(tmp_path, monkeypatch):
    root = _dest(tmp_path, 5)
    real = diff_commit._swap
    calls = []

    def flaky(tmp, path):
        calls.append(path)
        if len(calls) == 3:
            raise OSError("boom")
        real(tmp, path)

    monkeypatch.setattr(diff_commit, "_swap", flaky)
    writes = [_w(i, "new\n") for i in range(4)] + [DestWrite("fresh/new.txt", b"x", 0o100644)]
    with pytest.raises(OSError):
        land_diff(root, writes, [], "m", commit=True)
    monkeypatch.setattr(diff_commit, "_swap", real)
    assert _status(root) == ""
    assert not [p for p in root.rglob(".*.dc-*")]


def _engine(tmp_path):
    root = tmp_path / "engine"
    (root / "coordinator_core").mkdir(parents=True)
    (root / "coordinator_core" / "_engine_stamp").write_bytes(b"s0\n")
    (root / "mod_a.py").write_bytes(b"GEN = 1\n")
    (root / "mod_b.py").write_bytes(b"GEN = 1\n")
    _git(root, "init", "-q", "-b", "work/z")
    _git(root, "config", "user.email", "t@local")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _engine_round():
    return [
        DestWrite("mod_a.py", b"GEN = 2\n", 0o100644),
        DestWrite("coordinator_core/_engine_stamp", b"s1\n", 0o100644),
        DestWrite("mod_b.py", b"GEN = 2\n", 0o100644),
    ]


def _trace(monkeypatch, root):
    from coordinator_core import _engine_landing as landing

    events = []
    real_swap, real_commit = diff_commit._swap, diff_commit._gcommit.commit_paths

    def swap(tmp, path):
        events.append(("swap", path.name, landing.swap_in_progress(root)))
        real_swap(tmp, path)

    def commit(*a, **k):
        events.append(("commit", None, landing.swap_in_progress(root)))
        return real_commit(*a, **k)

    monkeypatch.setattr(diff_commit, "_swap", swap)
    monkeypatch.setattr(diff_commit._gcommit, "commit_paths", commit)
    return events


def test_engine_landing_stamp_last_marker_up_for_swap_and_down_for_commit(tmp_path, monkeypatch):
    from coordinator_core import _engine_landing as landing

    root = _engine(tmp_path)
    sleeps = []
    monkeypatch.setattr(diff_commit.time, "sleep", sleeps.append)
    events = _trace(monkeypatch, root)
    land_diff(root, _engine_round(), [], "m", commit=True)
    assert [e[1] for e in events if e[0] == "swap"] == ["mod_a.py", "mod_b.py", "_engine_stamp"]
    assert all(e[2] for e in events if e[0] == "swap")
    assert [e for e in events if e[0] == "commit"] == [("commit", None, False)]
    assert sleeps == [landing.SWAP_GRACE_S]
    assert not landing.swap_in_progress(root) and not (root / ".git" / landing.MARKER_NAME).exists()
    assert not list(root.rglob(".*.dc-*"))


def test_engine_landing_marker_absent_after_post_write_failure_and_restore_ran_under_it(tmp_path, monkeypatch):
    from coordinator_core import _engine_landing as landing

    root = _engine(tmp_path)
    monkeypatch.setattr(diff_commit.time, "sleep", lambda s: None)
    seen = []
    real_restore = diff_commit._restore

    def restore(*a, **k):
        seen.append(landing.swap_in_progress(root))
        real_restore(*a, **k)

    def lose(*a, **k):
        raise diff_commit._gcommit.CommitRefused("lost CAS race")

    monkeypatch.setattr(diff_commit, "_restore", restore)
    monkeypatch.setattr(diff_commit._gcommit, "commit_paths", lose)
    with pytest.raises(diff_commit._gcommit.CommitRefused):
        land_diff(root, _engine_round(), [], "m", commit=True)
    assert seen == [True]
    assert not landing.swap_in_progress(root) and _status(root) == ""


def test_engine_landing_swap_failure_restores_under_marker_and_clears_it(tmp_path, monkeypatch):
    from coordinator_core import _engine_landing as landing

    root = _engine(tmp_path)
    monkeypatch.setattr(diff_commit.time, "sleep", lambda s: None)
    real = diff_commit._swap
    seen = []

    def flaky(tmp, path):
        if path.name == "mod_b.py":
            raise OSError("boom")
        real(tmp, path)

    real_restore = diff_commit._restore

    def restore(*a, **k):
        seen.append(landing.swap_in_progress(root))
        real_restore(*a, **k)

    monkeypatch.setattr(diff_commit, "_swap", flaky)
    monkeypatch.setattr(diff_commit, "_restore", restore)
    with pytest.raises(OSError):
        land_diff(root, _engine_round(), [], "m", commit=True)
    assert seen == [True] and not landing.swap_in_progress(root) and _status(root) == ""
    assert not list(root.rglob(".*.dc-*"))


def test_non_engine_destination_raises_no_marker_and_no_grace(tmp_path, monkeypatch):
    root = _dest(tmp_path, 3)
    sleeps = []
    monkeypatch.setattr(diff_commit.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        diff_commit._landing, "begin_swap", lambda *a, **k: pytest.fail("marker raised for a non-engine root")
    )
    land_diff(root, [_w(0, "new\n")], [], "m", commit=True)
    assert sleeps == []


def test_engine_root_without_git_dir_reports_unguarded(tmp_path, monkeypatch, capsys):
    root = _engine(tmp_path)
    monkeypatch.setattr(diff_commit._landing, "marker_path", lambda r: None)
    monkeypatch.setattr(diff_commit.time, "sleep", lambda s: pytest.fail("grace slept with no marker"))
    land_diff(root, _engine_round(), [], "m", commit=False)
    assert "unguarded" in capsys.readouterr().err


def test_winerror_32_fallback_is_still_taken_on_swap(tmp_path, monkeypatch):
    root = _dest(tmp_path, 3)
    real = os.replace
    fallback = []

    def replace(src, dst):
        if Path(dst).name == "f0.txt" and not fallback:
            raise PermissionError(32, "in use")
        real(src, dst)

    import coordinator_core.install.door_install as door

    def image(tmp, path):
        fallback.append(path)
        real(tmp, path)

    monkeypatch.setattr(diff_commit.os, "replace", replace)
    monkeypatch.setattr(door, "_replace_possibly_running_image", image)
    land_diff(root, [_w(0, "new\n")], [], "m", commit=True)
    assert [p.name for p in fallback] == ["f0.txt"] and _status(root) == ""


def test_lost_cas_race_restores_and_raises(tmp_path, monkeypatch):
    root = _dest(tmp_path, 5)
    head = _git(root, "rev-parse", "HEAD")

    def lose(*a, **k):
        raise diff_commit._gcommit.CommitRefused("lost CAS race")

    monkeypatch.setattr(diff_commit._gcommit, "commit_paths", lose)
    with pytest.raises(diff_commit._gcommit.CommitRefused):
        land_diff(root, [_w(0, "new\n"), DestWrite("fresh/new.txt", b"x", 0o100644)], ["d1/f1.txt"], "m", commit=True)
    assert _status(root) == "" and _git(root, "rev-parse", "HEAD") == head
    assert not (root / "fresh").exists()


def test_exec_mode_lands_as_100755_on_nt(tmp_path, monkeypatch):
    root = _dest(tmp_path, 2)
    # Patch only the two modules' view of `os`: a process-wide `os.name = "nt"`
    # makes pathlib build WindowsPath, which POSIX refuses to instantiate.
    import types

    from coordinator_core.git import commit as git_commit

    for module in (diff_commit, git_commit):
        monkeypatch.setattr(module, "os", types.SimpleNamespace(**{**vars(os), "name": "nt"}))
    land_diff(root, [DestWrite("bin/tool.sh", b"#!/bin/sh\n", 0o100755)], [], "m", commit=True)
    monkeypatch.undo()
    assert _git(root, "ls-tree", "HEAD", "bin/tool.sh").split()[0] == "100755"


def test_commit_false_leaves_writes_uncommitted(tmp_path):
    root = _dest(tmp_path, 3)
    head = _git(root, "rev-parse", "HEAD")
    out = land_diff(root, [_w(0, "new\n")], [], "m", commit=False)
    assert out.commit_sha is None and out.written == ("d0/f0.txt",)
    assert _git(root, "rev-parse", "HEAD") == head
    assert _status(root).strip() == "M d0/f0.txt"


def test_crlf_checkout_of_an_lf_blob_is_clean(tmp_path):
    root = _dest(tmp_path, 5)
    (root / "d1" / "f1.txt").write_bytes(b"v0 1\r\n")
    out = land_diff(root, [_w(1, "new\n")], [], "m", commit=True)
    assert out.written == ("d1/f1.txt",) and out.commit_sha
    assert _git(root, "show", "HEAD:d1/f1.txt") == "new\n"
