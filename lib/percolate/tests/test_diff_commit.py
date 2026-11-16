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
    land_diff(big, [_w(0, "v2 0\n")], [], "again", commit=True)
    exclude = (big / ".git" / "info" / "exclude").read_text(encoding="utf-8").split()
    assert exclude.count(".coordinator-local/") == 1
    assert _status(big) == ""


def test_all_unchanged_is_noop(big):
    head = _git(big, "rev-parse", "HEAD")
    out = land_diff(big, [_w(200, "v0 200\n")], ["nope.txt"], "m", commit=True)
    assert out.commit_sha is None and out.written == () and _git(big, "rev-parse", "HEAD") == head


def test_deletion_prunes_empty_dirs(tmp_path):
    root = _dest(tmp_path, 3)
    land_diff(root, [], ["bin/run.sh"], "rm", commit=True)
    assert not (root / "bin").exists() and _status(root) == ""


def test_an_eol_crlf_attributed_path_lands_through_the_blob_fallback(tmp_path):
    root = _dest(tmp_path, n=3)
    (root / ".gitattributes").write_bytes(b"*.cmd text eol=crlf\n")
    _git(root, "add", ".gitattributes")
    _git(root, "commit", "-q", "-m", "attrs")
    out = land_diff(root, [DestWrite("bin/run.cmd", b"@echo off\r\n", 0o100644)], [], "crlf", commit=True)
    assert out.commit_sha and out.written == ("bin/run.cmd",)
    assert _status(root) == ""
    assert _git(root, "cat-file", "-p", "HEAD:bin/run.cmd") == "@echo off\n"


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


def _fail_nth_rename_into(monkeypatch, root, n):
    """Make the n-th `os.rename` whose destination sits directly in `root` raise."""
    real = os.rename
    seen = []

    def rename(src, dst, *a, **k):
        if Path(dst).parent == root:
            seen.append(Path(dst).name)
            if len(seen) == n:
                raise PermissionError(32, "in use")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(os, "rename", rename)
    return seen


def _leftovers(root):
    return [p.name for p in root.parent.iterdir() if p.name.startswith(f".{root.name}.")]


def test_failure_mid_swap_rolls_back_clean(tmp_path, monkeypatch):
    root = _dest(tmp_path, 5)
    seen = _fail_nth_rename_into(monkeypatch, root, 3)
    writes = [_w(i, "new\n") for i in range(4)] + [DestWrite("fresh/new.txt", b"x", 0o100644)]
    with pytest.raises(OSError):
        land_diff(root, writes, [], "m", commit=True)
    monkeypatch.undo()
    assert _status(root) == ""
    assert (root / "d0" / "f0.txt").read_bytes() == b"v0 0\n"
    assert not (root / "fresh").exists()
    assert _leftovers(root) == [] and not list(root.rglob(".*.dc-*"))


def test_no_path_bytes_under_the_live_root_change_before_the_swap(tmp_path, monkeypatch):
    root = _dest(tmp_path, 5)
    before = {p: p.read_bytes() for p in root.joinpath("d0").rglob("*") if p.is_file()}
    real = diff_commit._staged.swap_root

    def swap(staged, dest):
        assert {p: p.read_bytes() for p in root.joinpath("d0").rglob("*") if p.is_file()} == before
        assert not (root / "fresh").exists() and staged.parent == root.parent
        return real(staged, dest)

    monkeypatch.setattr(diff_commit._staged, "swap_root", swap)
    land_diff(root, [_w(0, "new\n"), DestWrite("fresh/new.txt", b"x", 0o100644)], [], "m", commit=True)
    assert (root / "d0" / "f0.txt").read_bytes() == b"new\n" and _status(root) == ""


def test_hold_is_entered_after_staging_and_exited_before_the_commit(tmp_path, monkeypatch):
    import contextlib

    root = _dest(tmp_path, 3)
    events = []
    staged_mod = diff_commit._staged
    real_write, real_swap = staged_mod.write_entry, staged_mod.swap_root
    real_commit = diff_commit._gcommit.commit_paths

    @contextlib.contextmanager
    def hold():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    monkeypatch.setattr(staged_mod, "write_entry", lambda *a: (events.append("stage"), real_write(*a))[1])
    monkeypatch.setattr(staged_mod, "swap_root", lambda *a: (events.append("swap"), real_swap(*a))[1])
    monkeypatch.setattr(diff_commit._gcommit, "commit_paths", lambda *a, **k: (events.append("commit"), real_commit(*a, **k))[1])
    land_diff(root, [_w(0, "new\n")], [], "m", commit=True, hold=hold)
    assert events[-4:] == ["enter", "swap", "exit", "commit"] and "stage" not in events[events.index("enter"):]
    assert "stage" in events[: events.index("enter")]


def test_hold_exits_when_the_swap_fails(tmp_path, monkeypatch):
    import contextlib

    root = _dest(tmp_path, 3)
    exits = []

    @contextlib.contextmanager
    def hold():
        try:
            yield
        finally:
            exits.append(1)

    _fail_nth_rename_into(monkeypatch, root, 1)
    with pytest.raises(OSError):
        land_diff(root, [_w(0, "new\n")], [], "m", commit=True, hold=hold)
    monkeypatch.undo()
    assert exits == [1] and _status(root) == ""


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
    staged = diff_commit._staged
    real_write, real_swap = staged.write_entry, staged.swap_root
    real_commit = diff_commit._gcommit.commit_paths

    def write(path, data, mode):
        events.append(("stage", Path(path).name, landing.swap_in_progress(root)))
        real_write(path, data, mode)

    def swap(staged_dir, dest):
        events.append(("swap", None, landing.swap_in_progress(root)))
        return real_swap(staged_dir, dest)

    def commit(*a, **k):
        events.append(("commit", None, landing.swap_in_progress(root)))
        return real_commit(*a, **k)

    monkeypatch.setattr(staged, "write_entry", write)
    monkeypatch.setattr(staged, "swap_root", swap)
    monkeypatch.setattr(diff_commit._gcommit, "commit_paths", commit)
    return events


def test_engine_landing_stamp_last_marker_up_for_swap_and_down_for_commit(tmp_path, monkeypatch):
    from coordinator_core import _engine_landing as landing

    root = _engine(tmp_path)
    sleeps = []
    monkeypatch.setattr(diff_commit.time, "sleep", sleeps.append)
    events = _trace(monkeypatch, root)
    land_diff(root, _engine_round(), [], "m", commit=True)
    assert [e[1] for e in events if e[0] == "stage"] == ["mod_a.py", "mod_b.py", "_engine_stamp"]
    assert not any(e[2] for e in events if e[0] == "stage")
    assert [e for e in events if e[0] == "swap"] == [("swap", None, True)]
    assert [e for e in events if e[0] == "commit"] == [("commit", None, False)]
    assert sleeps == [landing.SWAP_GRACE_S]
    assert not landing.swap_in_progress(root) and not (root / ".git" / landing.MARKER_NAME).exists()
    assert not list(root.rglob(".*.dc-*")) and _leftovers(root) == []


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
    real = os.replace
    seen = []

    def flaky(src, dst):
        if Path(dst).suffix == ".py":
            seen.append(landing.swap_in_progress(root))
        if Path(dst).name == "mod_b.py":
            raise OSError("boom")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(OSError):
        land_diff(root, _engine_round(), [], "m", commit=True)
    monkeypatch.undo()
    assert seen and all(seen) and not landing.swap_in_progress(root) and _status(root) == ""
    assert (root / "coordinator_core" / "_engine_stamp").read_bytes() == b"s0\n"
    assert (root / "mod_a.py").read_bytes() == b"GEN = 1\n"
    assert not list(root.rglob(".*.dc-*")) and _leftovers(root) == []


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
    root = _engine(tmp_path)
    real = os.replace
    fallback = []

    def replace(src, dst):
        if Path(dst).name == "mod_a.py" and not fallback:
            raise PermissionError(32, "in use")
        real(src, dst)

    import coordinator_core.install.door_install as door

    def image(tmp, path):
        fallback.append(path)
        real(tmp, path)

    monkeypatch.setattr(diff_commit.os, "replace", replace)
    monkeypatch.setattr(door, "_replace_possibly_running_image", image)
    land_diff(root, [DestWrite("mod_a.py", b"GEN = 2\n", 0o100644)], [], "m", commit=True)
    assert [p.name for p in fallback] == ["mod_a.py"] and _status(root) == ""


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


def _seed_exec(tmp_path, disk_mode, head_mode="100755"):
    root = _dest(tmp_path, 2)
    tool = root / "bin" / "tool"
    tool.write_bytes(b"#!/bin/sh\n")
    _git(root, "update-index", "--add", "--cacheinfo", head_mode, _git(root, "hash-object", "-w", "bin/tool").strip(), "bin/tool")
    _git(root, "commit", "-q", "-m", "tool")
    tool.chmod(disk_mode)
    return root, tool


@pytest.mark.skipif(os.name == "nt", reason="no disk exec bit on Windows")
def test_unchanged_path_with_drifted_disk_bit_is_promoted_without_a_commit(tmp_path):
    root, tool = _seed_exec(tmp_path, 0o644)
    head = _git(root, "rev-parse", "HEAD")
    out = land_diff(root, [DestWrite("bin/tool", b"#!/bin/sh\n", 0o100755)], [], "m", commit=True)
    assert out.commit_sha is None and out.written == () and out.remoded == ("bin/tool",)
    assert tool.stat().st_mode & 0o111 and _git(root, "rev-parse", "HEAD") == head
    assert _status(root) == ""


@pytest.mark.skipif(os.name == "nt", reason="no disk exec bit on Windows")
def test_unchanged_path_with_exec_disk_bit_is_demoted(tmp_path):
    root, tool = _seed_exec(tmp_path, 0o755, head_mode="100644")
    out = land_diff(root, [DestWrite("bin/tool", b"#!/bin/sh\n", 0o100644)], [], "m", commit=True)
    assert out.remoded == ("bin/tool",) and not tool.stat().st_mode & 0o111
    assert _status(root) == ""


@pytest.mark.skipif(os.name == "nt", reason="no disk exec bit on Windows")
def test_remoded_rides_a_round_that_also_writes(tmp_path):
    root, tool = _seed_exec(tmp_path, 0o644)
    writes = [DestWrite("bin/tool", b"#!/bin/sh\n", 0o100755), _w(0, "new\n")]
    out = land_diff(root, writes, [], "m", commit=True)
    assert out.written == ("d0/f0.txt",) and out.remoded == ("bin/tool",)
    assert tool.stat().st_mode & 0o111 and _status(root) == ""


@pytest.mark.skipif(os.name == "nt", reason="no disk exec bit on Windows")
def test_unchanged_path_with_hand_edited_bytes_is_not_chmodded(tmp_path):
    root, tool = _seed_exec(tmp_path, 0o644)
    tool.write_bytes(b"#!/bin/sh\necho hand\n")
    out = land_diff(root, [DestWrite("bin/tool", b"#!/bin/sh\n", 0o100755)], [], "m", commit=True)
    assert out.remoded == () and not tool.stat().st_mode & 0o111


@pytest.mark.skipif(os.name == "nt", reason="no disk exec bit on Windows")
def test_a_written_path_lands_at_its_declared_mode(tmp_path):
    root = _dest(tmp_path, 2)
    out = land_diff(root, [DestWrite("bin/new", b"#!/bin/sh\n", 0o100755)], [], "m", commit=True)
    assert out.written == ("bin/new",) and out.remoded == ()
    assert (root / "bin" / "new").stat().st_mode & 0o111 and _status(root) == ""


@pytest.mark.skipif(os.name == "nt", reason="no disk exec bit on Windows")
def test_converge_worktree_modes_skips_missing_links_and_reports_only_changes(tmp_path):
    (tmp_path / "a").write_bytes(b"x")
    (tmp_path / "a").chmod(0o644)
    (tmp_path / "b").write_bytes(b"x")
    (tmp_path / "b").chmod(0o755)
    modes = {"a": 0o100755, "b": 0o100755, "gone": 0o100755, "l": 0o120000}
    assert diff_commit.converge_worktree_modes(tmp_path, modes) == ("a",)


def test_converge_worktree_modes_is_a_noop_on_nt(tmp_path, monkeypatch):
    import types

    (tmp_path / "a").write_bytes(b"x")
    before = (tmp_path / "a").stat().st_mode
    monkeypatch.setattr(diff_commit, "os", types.SimpleNamespace(**{**vars(os), "name": "nt"}))
    assert diff_commit.converge_worktree_modes(tmp_path, {"a": 0o100755}) == ()
    monkeypatch.undo()
    assert (tmp_path / "a").stat().st_mode == before


def test_crlf_checkout_of_an_lf_blob_is_clean(tmp_path):
    root = _dest(tmp_path, 5)
    (root / "d1" / "f1.txt").write_bytes(b"v0 1\r\n")
    out = land_diff(root, [_w(1, "new\n")], [], "m", commit=True)
    assert out.written == ("d1/f1.txt",) and out.commit_sha
    assert _git(root, "show", "HEAD:d1/f1.txt") == "new\n"
