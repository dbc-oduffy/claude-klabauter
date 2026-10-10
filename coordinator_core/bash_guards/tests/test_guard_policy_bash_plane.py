"""Bash chain resolves every guard deny at the guard-level policy point."""

from __future__ import annotations

import json

import pytest

from coordinator_core import machine_profile
from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards._advisory_value import AdvisoryValue
from coordinator_core.bash_guards.dispatch import GuardBand, GuardEntry

_FIND = "find /mnt/c -maxdepth 4 -name '*.py'"
_CLEAN = "git clean -fdx"
_PYTEST = "python -m pytest -q"


@pytest.fixture(autouse=True)
def _no_flags(monkeypatch, tmp_path):
    import os

    for key in list(os.environ):
        if key.startswith("MACHINE_LOCAL_COORDINATOR_"):
            monkeypatch.delenv(key)
    reg = tmp_path / "reg"
    reg.mkdir()
    (reg / "registry.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    machine_profile.reset_cache()
    yield
    machine_profile.reset_cache()


@pytest.fixture
def loadbearing_repo(tmp_path):
    """A real repo whose only untracked file is load-bearing: the floor guard's
    `git clean -nd` oracle must run in a repo that has something to lose, or it
    fails open (a non-repo cwd) and the deny never fires. The `.git` skeleton
    is written by hand: a test-side `git init` would put this file under the
    spawn ratchet and off the fast tier."""
    repo = tmp_path / "repo"
    (repo / "state").mkdir(parents=True)
    git_dir = repo / ".git"
    (git_dir / "objects").mkdir(parents=True)
    (git_dir / "refs").mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (repo / "state" / "untracked.md").write_text("x", encoding="utf-8")
    return str(repo)


def _run(cmd, session="sess-policy", cwd="/tmp"):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "session_id": session,
        "cwd": cwd,
    }
    return dispatch.evaluate_payload_json(json.dumps(payload))


def _decision(out):
    return ((out or {}).get("hookSpecificOutput") or {}).get("permissionDecision")


def test_no_flags_runaway_find_warns():
    out = _run(_FIND)
    assert _decision(out) != "deny"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "runaway-find" in ctx
    assert machine_profile.LEVEL_VERB.rsplit(" ", 1)[0] in ctx or "guard_level" in ctx


def test_global_strict_denies(monkeypatch):
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "strict")
    assert _decision(_run(_FIND)) == "deny"


def test_per_guard_strict_hardens_one_guard_alone(monkeypatch):
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "warn")
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL_RUNAWAY-FIND", "strict")
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL_CHECK-TEST-SUITE-INVOCATION", "warn")
    assert _decision(_run(_FIND)) == "deny"
    assert _decision(_run(_PYTEST, session="sess-policy-2")) != "deny"


def test_suite_guard_defaults_strict_under_a_global_warn(monkeypatch):
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "warn")
    assert _decision(_run(_PYTEST, session="sess-policy-3")) == "deny"


def test_floor_guard_denies_with_no_flags(loadbearing_repo):
    assert _decision(_run(_CLEAN, cwd=loadbearing_repo)) == "deny"


def test_demoted_deny_does_not_shadow_a_later_floor_deny(loadbearing_repo):
    out = _run("%s; %s" % (_FIND, _CLEAN), cwd=loadbearing_repo)
    assert _decision(out) == "deny"


def test_held_demoted_envelope_returned_when_no_later_deny():
    out = _run(_FIND)
    assert _decision(out) == "allow"


def test_collect_mode_gathers_demoted_advisory():
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": _FIND},
        "session_id": "sess-collect",
        "cwd": "/tmp",
    }
    out = dispatch.evaluate_payload_json(json.dumps(payload), collect_advisories=True)
    assert out and all(_decision(e) != "deny" for e in out)


def test_crashed_floor_guard_still_denies(monkeypatch):
    def boom():
        raise RuntimeError("x")

    chain = [GuardEntry("destructive-git-clean", boom, True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED)]
    out = dispatch._apply_guard_level("destructive-git-clean", dispatch._crash_deny("destructive-git-clean", RuntimeError("x")))
    assert _decision(out) == "deny"
    out = dispatch._apply_guard_level("runaway-find", dispatch._crash_deny("runaway-find", RuntimeError("x")))
    assert _decision(out) != "deny"
    assert chain
