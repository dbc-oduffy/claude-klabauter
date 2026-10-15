"""run_git_stream: a bounded streaming git spawn sharing run_git's console flags and accounting."""
from __future__ import annotations

import subprocess

from coordinator_core.git import run as run_mod
from coordinator_core.git.run import run_git_stream


def test_streams_stdout_incrementally():
    with run_git_stream(["--version"]) as proc:
        first = proc.stdout.read(1)
        rest = proc.stdout.read()
        assert proc.wait() == 0
    assert (first + rest).startswith(b"git version")


def test_console_flags_match_run_git(monkeypatch):
    seen = {}
    real = subprocess.Popen

    def spy(argv, **kw):
        seen.update(kw)
        return real(argv, **kw)

    monkeypatch.setattr(subprocess, "Popen", spy)
    args = ["--version"]
    with run_git_stream(args) as proc:
        proc.stdout.read()
    expected = run_mod._console_flags(args)
    assert seen.get("creationflags", 0) & expected.get("creationflags", 0) == expected.get("creationflags", 0)


def test_child_is_killed_and_reaped_when_body_raises():
    holder = {}
    try:
        with run_git_stream(["--version"]) as proc:
            holder["p"] = proc
            raise RuntimeError("stop")
    except RuntimeError:
        pass
    assert holder["p"].poll() is not None
    assert holder["p"].stdout.closed


def test_missing_cwd_raises_oserror(tmp_path):
    import pytest

    with pytest.raises(OSError):
        with run_git_stream(["--version"], cwd=str(tmp_path / "nope")):
            pass
