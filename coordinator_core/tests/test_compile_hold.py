"""push_hold compile hold: refuses control/plugin deltas until cleared or stamped green."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core import push_hold

pytestmark = [pytest.mark.spawns_process]

T = "a" * 40


@pytest.fixture
def repo(tmp_path):
    subprocess.run(
        ["git", "init", "-q", "-b", "work/x"], cwd=str(tmp_path), check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # popup-intentional-last-resort
    )
    return tmp_path


def test_unset_reads_none_and_never_refuses(repo):
    assert push_hold.read_compile_hold(repo) is None
    assert push_hold.read_compile_green_tree(repo) is None
    assert push_hold.ladder_refusal(repo, ["control/plugin/Foo.cpp"], T) is None


def test_set_refuses_plugin_paths_naming_reason(repo):
    assert push_hold.set_compile_hold(repo, reason="r")
    assert push_hold.read_compile_hold(repo) == "r"
    msg = push_hold.ladder_refusal(repo, ["control/plugin/Foo.cpp"], T)
    assert msg and msg.startswith(push_hold.COMPILE_HOLD_REFUSAL) and "(r)" in msg


def test_non_plugin_paths_pass_under_hold(repo):
    push_hold.set_compile_hold(repo, reason="r")
    assert push_hold.ladder_refusal(repo, ["docs/x.md"], T) is None
    assert push_hold.ladder_refusal(repo, ["control/pluginx/a.cpp"], T) is None


def test_clear_and_green_stamp_release(repo):
    push_hold.set_compile_hold(repo, reason="r")
    assert push_hold.clear_compile_hold(repo)
    assert push_hold.ladder_refusal(repo, ["control/plugin/Foo.cpp"], T) is None
    assert push_hold.clear_compile_hold(repo)  # absent key is success
    push_hold.set_compile_hold(repo, reason="r")
    assert push_hold.ladder_refusal(repo, ["control/plugin/Foo.cpp"], T)
    assert push_hold.stamp_green_tree(repo, T.upper())
    assert push_hold.read_compile_green_tree(repo) == T
    assert push_hold.ladder_refusal(repo, ["control/plugin/Foo.cpp"], T) is None
    assert push_hold.ladder_refusal(repo, ["control/plugin/Foo.cpp"], "b" * 40)


def test_stamp_rejects_malformed_sha(repo):
    assert not push_hold.stamp_green_tree(repo, "xyz")
    assert push_hold.read_compile_green_tree(repo) is None


def test_compile_hold_is_not_the_push_freeze(repo):
    push_hold.set_compile_hold(repo, reason="r")
    assert push_hold.read_hold(repo, "work/x") is None
    assert push_hold.list_holds(repo)["repo"] is None
    push_hold.set_hold(repo, None, "freeze")
    assert push_hold.read_compile_hold(repo) == "r"


def test_read_path_spawns_nothing(repo, monkeypatch):
    push_hold.set_compile_hold(repo, reason="r")

    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert push_hold.read_compile_hold(repo) == "r"
    assert push_hold.ladder_refusal(repo, ["control/plugin/a.cpp"], T)
