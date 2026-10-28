"""fresh_git_clone borrows objects (--shared) and removes its clone on normal and error exit;
terminal_commit_full_path removes the fixture it builds itself, and only that."""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.git.run as git_run
from coordinator_core.benchmarks import terminal_commit_full_path as drv
from coordinator_core.benchmarks.isolated_clone import fresh_git_clone


class _Result:
    ok = True
    returncode = 0
    stderr = ""


def _patch_git(monkeypatch, seen: list) -> None:
    def _fake(argv, **_kw):
        seen.append(list(argv))
        dest = Path(argv[-1])
        (dest / ".git").mkdir(parents=True)
        (dest / ".git" / "config").write_text("", encoding="utf-8")
        return _Result()

    monkeypatch.setattr(git_run, "run_git", _fake)


def test_clone_uses_shared_never_local(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".git").mkdir()
    seen: list = []
    _patch_git(monkeypatch, seen)
    with fresh_git_clone(tmp_path, prefix="p-", label="t"):
        pass
    assert "--shared" in seen[0]
    assert "--local" not in seen[0]


def test_clone_removed_after_normal_exit(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".git").mkdir()
    _patch_git(monkeypatch, [])
    with fresh_git_clone(tmp_path, prefix="p-", label="t") as dest:
        assert dest.exists()
    assert not dest.exists()
    assert not dest.parent.exists()


def test_clone_removed_after_exception(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".git").mkdir()
    _patch_git(monkeypatch, [])
    seen_dest = []
    with pytest.raises(ValueError):
        with fresh_git_clone(tmp_path, prefix="p-", label="t") as dest:
            seen_dest.append(dest)
            raise ValueError("boom")
    assert not seen_dest[0].exists()
    assert not seen_dest[0].parent.exists()


def test_clone_config_silences_git_daemons(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".git").mkdir()
    _patch_git(monkeypatch, [])
    with fresh_git_clone(tmp_path, prefix="p-", label="t") as dest:
        text = (dest / ".git" / "config").read_text(encoding="utf-8")
    assert "fsmonitor = false" in text
    assert "auto = 0" in text
    assert "auto = false" in text


def test_reap_reports_non_descendant_holder_without_signalling(tmp_path: Path, monkeypatch) -> None:
    import psutil

    from coordinator_core.benchmarks import isolated_clone as ic

    class _Fake:
        pid = 2**22 + 12345
        signalled = False

        def terminate(self):
            _Fake.signalled = True

        kill = terminate

    monkeypatch.setattr(psutil, "process_iter", lambda: [_Fake()])
    monkeypatch.setattr(ic, "_process_is_rooted_under", lambda proc, root: True)
    reaped = ic.reap_processes_under(tmp_path)
    assert list(reaped) == []
    assert reaped.foreign == [_Fake.pid]
    assert _Fake.signalled is False


def test_driver_removes_the_fixture_it_built_on_failure(tmp_path: Path, monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(drv, "declare_benchmark_origin", lambda: None)
    monkeypatch.setattr(drv, "_prepare_close", _boom)
    monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", str(tmp_path / "outbox-unused"))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "x")
    with pytest.raises(RuntimeError):
        drv.main([str(tmp_path)])
    assert list(tmp_path.iterdir()) == []


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_driver_build_only_keeps_its_prebuilt_tree(tmp_path: Path) -> None:
    import subprocess
    import sys

    cmd = [sys.executable, "-m", "coordinator_core.benchmarks.terminal_commit_full_path",
           str(tmp_path), "--kind", "reclose", "--build-only"]
    done = subprocess.run(cmd, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert done.returncode == 0, done.stderr
    assert (tmp_path / drv.PREBUILT_MANIFEST).exists()
    assert any(p.is_dir() for p in tmp_path.iterdir())
