"""push-hold CLI: --compile set/clear, stamp-green --tree, list shows the compile hold."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process]

T = "b" * 40
_SRC = Path(__file__).resolve().parents[1] / "push-hold.py"


def _main():
    spec = importlib.util.spec_from_file_location("push_hold_cli", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main


@pytest.fixture
def repo(tmp_path):
    subprocess.run(
        ["git", "init", "-q", "-b", "work/x"], cwd=str(tmp_path), check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # popup-intentional-last-resort
    )
    return tmp_path


def _run(args, repo):
    return _main()([*args, "--repo", str(repo)])


def test_set_compile_then_list_then_clear(repo, capsys):
    from coordinator_core import push_hold

    assert _run(["set", "--compile", "--reason", "gate"], repo) == 0
    assert push_hold.read_compile_hold(repo) == "gate"
    capsys.readouterr()
    assert _run(["list"], repo) == 0
    assert "compile: gate" in capsys.readouterr().out
    assert _run(["clear", "--compile"], repo) == 0
    assert push_hold.read_compile_hold(repo) is None
    assert _run(["list"], repo) == 0
    assert "compile: -" in capsys.readouterr().out


def test_stamp_green_recorded_and_listed(repo, capsys):
    from coordinator_core import push_hold

    _run(["set", "--compile", "--reason", "gate"], repo)
    assert _run(["stamp-green", "--tree", T.upper()], repo) == 0
    assert push_hold.read_compile_green_tree(repo) == T
    capsys.readouterr()
    _run(["list"], repo)
    assert f"(green {T})" in capsys.readouterr().out


def test_stamp_green_malformed_tree_fails(repo):
    assert _run(["stamp-green", "--tree", "xyz"], repo) == 1


@pytest.mark.parametrize(
    "args",
    [
        ["set", "--compile", "--branch", "x"],
        ["set", "--compile", "--allow-sha", T],
        ["list", "--compile"],
        ["stamp-green"],
        ["set", "--tree", T],
    ],
)
def test_invalid_combinations_refused(repo, args):
    with pytest.raises(SystemExit) as exc:
        _run(args, repo)
    assert exc.value.code == 2


def test_plain_set_does_not_touch_compile_hold(repo):
    from coordinator_core import push_hold

    assert _run(["set", "--reason", "freeze"], repo) == 0
    assert push_hold.read_compile_hold(repo) is None
