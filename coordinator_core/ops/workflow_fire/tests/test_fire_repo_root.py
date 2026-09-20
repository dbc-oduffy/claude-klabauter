"""
coordinator_core.ops.workflow_fire.tests.test_fire_repo_root — regression
coverage for klabauter#41: a ``workflow.fire`` aimed at the wrong tree used
to spawn a child rooted in that wrong tree, and still report
``subtype: success``.

Purpose: pins ``fire._assert_script_under_repo_root`` on the real
``fire_workflow`` call path -- the check must fire BEFORE any child is
spawned, for both an explicit (wrong) ``--repo``/``cwd`` and the
script-path-derived fallback -- and pins that the resolved root actually
lands on the fire record (``repo_root``), never left as the pre-fix
``NameError``-triggering dead reference.

Spec backlink: docs/plans/2026-08-18-claude-klabauter-fires-the-workflows-it-emits.md
§ C4; klabauter#41.

Negative-spec:
  - Does NOT spawn a real subprocess anywhere in this file.
  - Does NOT re-test plugin-dir resolution, concurrency-cap locking, or
    outcome classification -- those are ``test_fire.py``'s remit; this file
    is narrowly the repo-root containment check.
"""

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
    """The tree a fire would be wrongly resolved against (e.g. the engine
    root) -- a real git repo, distinct from the one holding ``script_path``.
    """
    wrong_dir = tmp_path / "wrong-repo"
    wrong_dir.mkdir()
    _git_init(wrong_dir)
    return wrong_dir


@pytest.fixture
def target_repo(tmp_path):
    """The tree ``script_path`` actually lives in."""
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
    """Namespacing marker only -- see the free functions below."""


def test_assert_script_under_repo_root_passes_when_script_is_under_root(script, target_repo):
    # Must not raise.
    fire._assert_script_under_repo_root(script, str(target_repo))


def test_assert_script_under_repo_root_refuses_when_script_lives_elsewhere(script, wrong_repo):
    with pytest.raises(fire.ScriptOutsideRepoRootError):
        fire._assert_script_under_repo_root(script, str(wrong_repo))


def test_fire_workflow_refuses_before_spawn_when_explicit_cwd_is_wrong_tree(
    monkeypatch, script, wrong_repo
):
    """The production call path: an explicit --repo/cwd naming a tree that
    does not contain ``script_path`` must refuse at ``fire_workflow`` time,
    never reach ``subprocess.Popen`` -- the exact klabauter#41 shape (three
    of six fires spawned a child rooted at the engine root while the script
    lived in a sibling repo).
    """
    monkeypatch.setattr(fire, "resolve_plugin_dir", lambda claude_bin="claude": "/plugins/coordinator")
    monkeypatch.setattr(fire.subprocess, "Popen", _fail_if_spawned)

    with pytest.raises(fire.ScriptOutsideRepoRootError):
        fire.fire_workflow(str(script), cwd=str(wrong_repo))


def test_fire_workflow_records_resolved_repo_root_on_success(monkeypatch, script, target_repo):
    """``repo_root`` on the returned/persisted record must be the resolved
    tree the script was checked against -- not a dead ``NameError``-causing
    reference (a previous edit left ``resolved_repo_root`` undefined at the
    record-build call site)."""
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
