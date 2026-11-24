"""coordinator/bin/tests/test_publish_git_archive_deadline_and_reap.py —
pins `publish.py`'s bounded `git archive` (`GitArchiveDeadlineError`) and the
start-of-run reaper for dead-pid `claude-klabauter-publish-archive-*` /
`claude-klabauter-publish-materialize-*` temps.

Run: python -m pytest coordinator/bin/tests/test_publish_git_archive_deadline_and_reap.py -q
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_git_archive_deadline_and_reap_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _dead_pid() -> int:
    """A pid that names no running process: a child that has already exited and been reaped."""
    child = subprocess.Popen([sys.executable, "-c", "pass"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    child.wait()
    return child.pid


@pytest.fixture()
def managed(tmp_path, monkeypatch):
    root = tmp_path / "managed"
    root.mkdir()
    from coordinator_core import temp_layout

    monkeypatch.setattr(temp_layout, "managed_tempdir", lambda *a, **k: root)
    return root


def _make_foreign_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "c1@claude-klabauter.test"],
                 ["config", "user.name", "C1"], ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    (repo / "f.txt").write_bytes(b"x\n")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "i"], check=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return repo, publish._git_rev_parse(repo, "HEAD")


@pytest.fixture(autouse=True)
def _clear_caches():
    publish._REQUIRED_PATHSPEC_CACHE.clear()
    yield
    publish._REQUIRED_PATHSPEC_CACHE.clear()


def test_deadline_is_well_below_the_publish_leg_timeout():
    text = (_BIN_DIR / "percolate-round.py").read_text(encoding="utf-8")
    leg = float(re.search(r"^_PUBLISH_LEG_TIMEOUT_SECS = ([\d.]+)", text, re.M).group(1))
    assert publish._GIT_ARCHIVE_DEADLINE_SECS <= leg / 10


def test_archive_past_the_deadline_raises_typed_error_and_leaves_no_temps(tmp_path, monkeypatch, managed):
    repo, sha = _make_foreign_repo(tmp_path)
    monkeypatch.setattr(publish, "_required_pathspec_for_toplevel", lambda *a, **k: ())
    real_run = subprocess.run

    def slow_run(cmd, **kwargs):
        # Stand-in for a wedged `git archive`: partial bytes written, then the deadline kills it.
        out = Path(cmd[cmd.index("-o") + 1])
        out.write_bytes(b"x" * 123)
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(publish.subprocess, "run", slow_run)
    with pytest.raises(publish.GitArchiveDeadlineError) as info:
        publish._extract_git_archive(repo, sha)
    monkeypatch.setattr(publish.subprocess, "run", real_run)

    message = str(info.value)
    assert str(repo) in message and sha in message
    assert "123 bytes" in message
    assert re.search(r"after \d+(\.\d+)?s", message)
    assert isinstance(info.value, publish.GitMaterializeError)
    assert list(managed.iterdir()) == []


def test_archive_passes_the_deadline_to_git(tmp_path, monkeypatch, managed):
    repo, sha = _make_foreign_repo(tmp_path)
    monkeypatch.setattr(publish, "_required_pathspec_for_toplevel", lambda *a, **k: ())
    seen = {}
    real_run = subprocess.run

    def spy_run(cmd, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(publish.subprocess, "run", spy_run)
    shadow = publish._extract_git_archive(repo, sha)
    assert seen["timeout"] == publish._GIT_ARCHIVE_DEADLINE_SECS
    assert (shadow / "f.txt").is_file()
    assert re.match(rf"claude-klabauter-publish-materialize-{os.getpid()}-", shadow.name)


def test_reap_removes_dead_pid_temps_keeps_live_and_young_legacy(managed, monkeypatch):
    dead = _dead_pid()
    gone_dir = managed / f"claude-klabauter-publish-materialize-{dead}-abc"
    gone_dir.mkdir()
    (gone_dir / "f").write_bytes(b"1")
    gone_tar = managed / f"claude-klabauter-publish-archive-{dead}-abc.tar"
    gone_tar.write_bytes(b"1")
    live_dir = managed / f"claude-klabauter-publish-materialize-{os.getpid()}-abc"
    live_dir.mkdir()
    live_tar = managed / f"claude-klabauter-publish-archive-{os.getpid()}-abc.tar"
    live_tar.write_bytes(b"1")
    young_legacy = managed / "claude-klabauter-publish-archive-wg620402.tar"
    young_legacy.write_bytes(b"1")
    old_legacy = managed / "claude-klabauter-publish-materialize-wg620403"
    old_legacy.mkdir()
    old = time.time() - 2 * 24 * 3600
    os.utime(old_legacy, (old, old))
    other = managed / "claude-klabauter-publish-inject-zzz"
    other.mkdir()

    publish._reap_dead_publish_temps()

    assert not gone_dir.exists() and not gone_tar.exists()
    assert not old_legacy.exists()
    assert live_dir.exists() and live_tar.exists()
    assert young_legacy.exists()
    assert other.exists()


def test_reap_skips_a_tar_it_cannot_unlink_and_never_raises(managed, monkeypatch):
    dead = _dead_pid()
    held = managed / f"claude-klabauter-publish-archive-{dead}-held.tar"
    held.write_bytes(b"1")
    free = managed / f"claude-klabauter-publish-archive-{dead}-free.tar"
    free.write_bytes(b"1")
    real_unlink = Path.unlink

    def guarded_unlink(self, *a, **k):
        if self.name == held.name:
            raise PermissionError("held open")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", guarded_unlink)
    publish._reap_dead_publish_temps()
    monkeypatch.setattr(Path, "unlink", real_unlink)

    assert held.exists()
    assert not free.exists()


def test_reap_spawns_nothing(managed, monkeypatch):
    dead = _dead_pid()
    (managed / f"claude-klabauter-publish-archive-{dead}-x.tar").write_bytes(b"1")

    def boom(*a, **k):
        raise AssertionError("reaper spawned a subprocess")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    publish._reap_dead_publish_temps()
    assert list(managed.iterdir()) == []


def test_main_reaps_before_the_round_body(monkeypatch):
    order = []
    monkeypatch.setattr(publish, "lower_process_priority_self", lambda: None)
    monkeypatch.setattr(publish, "_reap_dead_publish_temps", lambda: order.append("reap"))
    monkeypatch.setattr(publish, "_main", lambda argv=None: order.append("main") or 0)
    assert publish.main([]) == 0
    assert order == ["reap", "main"]


def test_extraction_past_the_deadline_raises_typed_error_and_leaves_no_temps(tmp_path, monkeypatch, managed):
    repo, sha = _make_foreign_repo(tmp_path)
    monkeypatch.setattr(publish, "_required_pathspec_for_toplevel", lambda *a, **k: ())
    monkeypatch.setattr(publish, "_GIT_EXTRACT_DEADLINE_SECS", -1.0)
    with pytest.raises(publish.GitArchiveDeadlineError) as info:
        publish._extract_git_archive(repo, sha)
    assert "extracting" in str(info.value) and sha in str(info.value)
    assert list(managed.iterdir()) == []


def test_extraction_deadline_names_members_and_bytes_done(tmp_path, monkeypatch):
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as out:
        for name in ("a", "b", "c"):
            info = tarfile.TarInfo(name)
            info.size = 5
            out.addfile(info, io.BytesIO(b"12345"))
    buf.seek(0)
    clock = iter([0.0, 99.0])
    monkeypatch.setattr(publish.time, "perf_counter", lambda: next(clock))
    monkeypatch.setattr(publish, "_GIT_EXTRACT_DEADLINE_SECS", 50.0)
    with tarfile.open(fileobj=buf) as archive:
        gen = publish._members_within_deadline(archive, 0.0, tmp_path, "abc")
        next(gen)
        with pytest.raises(publish.GitArchiveDeadlineError, match=r"1 members / 5 bytes extracted"):
            next(gen)


def test_reap_logs_an_advisory_when_its_imports_fail(monkeypatch, capsys):
    import builtins

    real_import = builtins.__import__

    def failing(name, *a, **k):
        if name.startswith("coordinator_core.temp_layout"):
            raise ImportError("simulated")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", failing)
    publish._reap_dead_publish_temps()
    monkeypatch.setattr(builtins, "__import__", real_import)
    assert "Advisory: publish temp reaper skipped" in capsys.readouterr().err


def test_extraction_within_the_deadline_unpacks(tmp_path, managed, monkeypatch):
    repo, sha = _make_foreign_repo(tmp_path)
    monkeypatch.setattr(publish, "_required_pathspec_for_toplevel", lambda *a, **k: ())
    assert (publish._extract_git_archive(repo, sha) / "f.txt").is_file()


def test_extraction_deadline_is_well_below_the_publish_leg_timeout():
    text = (_BIN_DIR / "percolate-round.py").read_text(encoding="utf-8")
    leg = float(re.search(r"^_PUBLISH_LEG_TIMEOUT_SECS = ([\d.]+)", text, re.M).group(1))
    assert publish._GIT_EXTRACT_DEADLINE_SECS <= leg / 4
