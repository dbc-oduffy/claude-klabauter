
from __future__ import annotations

import subprocess

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.ops.workflow_fire import fire


def _git_init(repo_dir):
    cnw = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(["git", "init", "-q", str(repo_dir)], check=True, creationflags=cnw)
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.email", "test@example.com"],
        check=True,
        creationflags=cnw,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.name", "test"],
        check=True,
        creationflags=cnw,
    )


@pytest.fixture
def wrong_repo(tmp_path):
    wrong_dir = tmp_path / "wrong-repo"
    wrong_dir.mkdir()
    _git_init(wrong_dir)
    return wrong_dir


@pytest.fixture
def target_repo(tmp_path):
    target_dir = tmp_path / "target-repo"
    target_dir.mkdir()
    _git_init(target_dir)
    return target_dir


@pytest.fixture
def script(target_repo):
    p = target_repo / "workflow.mjs"
    p.write_text("// emitted workflow\n", encoding="utf-8")
    return p


def _fail_if_spawned(*args, **kwargs):
    raise AssertionError(
        "fire_workflow spawned a child before the repo-root containment "
        "check ran -- the refusal must happen at fire time, before Popen"
    )


class _ScriptOutsideRepoRootError_direct_unit_check:
    pass


def test_assert_script_under_repo_root_passes_when_script_is_under_root(script, target_repo):
    fire._assert_script_under_repo_root(script, str(target_repo))


def test_assert_script_under_repo_root_refuses_when_script_lives_elsewhere(script, wrong_repo):
    with pytest.raises(fire.ScriptOutsideRepoRootError):
        fire._assert_script_under_repo_root(script, str(wrong_repo))


def test_fire_workflow_refuses_before_spawn_when_explicit_cwd_is_wrong_tree(
    monkeypatch, script, wrong_repo
):
    monkeypatch.setattr(fire, "resolve_plugin_dir", lambda claude_bin="claude": "/plugins/coordinator")
    monkeypatch.setattr(fire.subprocess, "Popen", _fail_if_spawned)

    with pytest.raises(fire.ScriptOutsideRepoRootError):
        fire.fire_workflow(str(script), cwd=str(wrong_repo))


def test_fire_workflow_records_resolved_repo_root_on_success(monkeypatch, script, target_repo):
    monkeypatch.setattr(fire, "resolve_plugin_dir", lambda claude_bin="claude": "/plugins/coordinator")

    class _FakePopen:
        def __init__(self, *a, **k):
            self.pid = 4242

        def poll(self):
            return None

    monkeypatch.setattr(fire.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(fire.time, "sleep", lambda *_: None)

    record = fire.fire_workflow(str(script), cwd=str(target_repo))

    import os

    assert os.path.realpath(record["repo_root"]) == os.path.realpath(str(target_repo))
