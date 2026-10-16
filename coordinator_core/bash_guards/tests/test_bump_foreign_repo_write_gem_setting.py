"""Tests for bump_foreign_repo_write's Group EM setting branch
(`coordinator.feature.gem_cross_repo_commit`): scoped allow when on or unset,
uncleared refusal when off, ordinary bump for every non-Group-EM session, and
no added process spawns. Standing is driven by monkeypatching
`nomination.identify`; nothing is written outside tmp_path.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.bash_guards import bump_foreign_repo_write as guard
from coordinator_core.bash_guards._write_bump_marker import marker_basename, resolve_gitdir
from coordinator_core.bash_guards._write_bump_message import render_gem_refusal_message
from coordinator_core.bash_guards.tests.test_bump_foreign_repo_write import (
    _posix,
    _reset_git_root_cache,  # noqa: F401 -- autouse fixture reused
    _set_anchor,
    repos,  # noqa: F401 -- reused fixture
)
from coordinator_core.bash_guards.tests.test_bump_outside_repo_write import (
    _clean_bump_env,  # noqa: F401 -- reused fixture
)
from coordinator_core.group_em import nomination

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

KEY_ENV = "MACHINE_LOCAL_COORDINATOR_FEATURE_GEM_CROSS_REPO_COMMIT"
GEM_SID = "sess-gem-1"


def _live(monkeypatch, holder_sid: str = GEM_SID, state: str = "live") -> None:
    monkeypatch.setattr(
        nomination, "identify", lambda root=None: {"state": state, "holder": {"session_id": holder_sid}}
    )


def _setting(monkeypatch, value) -> None:
    if value is None:
        monkeypatch.delenv(KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(KEY_ENV, value)


def _scoped(repos) -> str:
    return "git -C %s commit -m x -- f.txt" % _posix(repos["foreign"])


def _run(repos, cmd, sid=GEM_SID, payload=None):
    return guard.check_bump_foreign_repo_write(cmd, sid, str(repos["anchor"]), payload or {})


@pytest.mark.parametrize("value", [None, "on"])
def test_scoped_commit_allowed_when_on_or_unset(repos, monkeypatch, value):
    _set_anchor(monkeypatch, repos, GEM_SID)
    _live(monkeypatch)
    _setting(monkeypatch, value)
    assert _run(repos, _scoped(repos)) is None
    gitdir = resolve_gitdir(str(repos["foreign"]))
    assert not (gitdir / marker_basename(GEM_SID)).exists()


@pytest.mark.parametrize("sub", ["push", "add -A", "commit -a -m x"])
def test_unscoped_write_keeps_ordinary_clearable_bump_when_on(repos, monkeypatch, sub):
    _set_anchor(monkeypatch, repos, GEM_SID)
    _live(monkeypatch)
    _setting(monkeypatch, "on")
    cmd = "git -C %s %s" % (_posix(repos["foreign"]), sub)
    assert _run(repos, cmd) is not None
    (resolve_gitdir(str(repos["foreign"])) / marker_basename(GEM_SID)).touch()
    assert _run(repos, cmd) is None


def test_off_refuses_with_gem_message_and_marker_does_not_clear(repos, monkeypatch):
    _set_anchor(monkeypatch, repos, GEM_SID)
    _live(monkeypatch)
    _setting(monkeypatch, "off")
    result = _run(repos, _scoped(repos))
    assert result is not None
    reason = str(result)
    label = guard._show_toplevel(str(repos["foreign"])) or str(repos["foreign"])
    assert render_gem_refusal_message(label).split("\n")[0][:40] in reason
    (resolve_gitdir(str(repos["foreign"])) / marker_basename(GEM_SID)).touch()
    assert _run(repos, _scoped(repos)) is not None


def test_off_returns_none_when_environment_stands_the_bump_down(repos, monkeypatch):
    _set_anchor(monkeypatch, repos, GEM_SID)
    _live(monkeypatch)
    _setting(monkeypatch, "off")
    monkeypatch.delenv("COORDINATOR_CAP_FLEET_PRESENT", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "remote")
    assert _run(repos, _scoped(repos)) is None


def _subagent(repos, monkeypatch):
    d = repos["anchor"] / ".git" / "coordinator-sessions" / ".agents" / "agent-1"
    d.mkdir(parents=True)
    (d / "em-session-id.txt").write_text(GEM_SID + "\n", encoding="utf-8")
    return {"agent_id": "agent-1"}


def test_subagent_inherits_group_em_disposition(repos, monkeypatch):
    _set_anchor(monkeypatch, repos, "sess-sub-1")
    payload = _subagent(repos, monkeypatch)
    _live(monkeypatch)
    _setting(monkeypatch, "on")
    assert _run(repos, _scoped(repos), sid="sess-sub-1", payload=payload) is None
    _setting(monkeypatch, "off")
    assert _run(repos, _scoped(repos), sid="sess-sub-1", payload=payload) is not None


@pytest.mark.parametrize("value", ["on", "off"])
def test_non_group_em_gets_ordinary_clearable_bump(repos, monkeypatch, value):
    _set_anchor(monkeypatch, repos, "sess-plain")
    _live(monkeypatch, holder_sid="someone-else")
    _setting(monkeypatch, value)
    assert _run(repos, _scoped(repos), sid="sess-plain") is not None
    (resolve_gitdir(str(repos["foreign"])) / marker_basename("sess-plain")).touch()
    assert _run(repos, _scoped(repos), sid="sess-plain") is None


def test_plain_sink_is_ordinary_bump_even_for_live_group_em(repos, monkeypatch):
    _set_anchor(monkeypatch, repos, GEM_SID)
    _live(monkeypatch)
    _setting(monkeypatch, "on")
    cmd = "echo hi > %s/out.txt" % _posix(repos["foreign"])
    assert _run(repos, cmd) is not None


def test_standing_read_failure_fails_open_to_ordinary_bump(repos, monkeypatch):
    _set_anchor(monkeypatch, repos, GEM_SID)

    def boom(root=None):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(nomination, "identify", boom)
    _setting(monkeypatch, "off")
    assert _run(repos, _scoped(repos)) is not None
    (resolve_gitdir(str(repos["foreign"])) / marker_basename(GEM_SID)).touch()
    assert _run(repos, _scoped(repos)) is None


def _count_spawns(monkeypatch):
    counts = {"n": 0}
    real_popen = subprocess.Popen

    class CountingPopen(real_popen):
        def __init__(self, *a, **k):
            counts["n"] += 1
            super().__init__(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", CountingPopen)
    return counts


def _spawns(repos, monkeypatch, sid, cmd) -> int:
    _sandbox_reset()
    counts = _count_spawns(monkeypatch)
    _run(repos, cmd, sid=sid)
    return counts["n"]


def _sandbox_reset() -> None:
    from coordinator_core.subagent_sandbox import engine

    engine.reset_resolve_git_root_cache()


def test_group_em_paths_add_no_spawns_over_an_ordinary_bump(repos, monkeypatch):
    _set_anchor(monkeypatch, repos, GEM_SID)
    cmd = _scoped(repos)
    _live(monkeypatch, holder_sid="someone-else")
    _setting(monkeypatch, "on")
    baseline = _spawns(repos, monkeypatch, GEM_SID, cmd)
    _live(monkeypatch)
    allow = _spawns(repos, monkeypatch, GEM_SID, cmd)
    _setting(monkeypatch, "off")
    refuse = _spawns(repos, monkeypatch, GEM_SID, cmd)
    assert allow <= baseline
    assert refuse <= baseline
