"""Unit tests for the shared test-run admission verdict: identity, scope and ceiling legs."""

from __future__ import annotations

import subprocess
import types

import pytest

from coordinator_core.bash_guards import _test_run_admission as adm
from coordinator_core.bash_guards import guard_heavy_command_admission as heavy
from coordinator_core.session import touch_record

SESSION = "sess-A"
TOUCHED = "coordinator_core/bash_guards/tests/test_touched.py"
UNTOUCHED = "coordinator_core/bash_guards/tests/test_other.py"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    return tmp_path


@pytest.fixture(autouse=True)
def healthy_box(monkeypatch):
    """A box with RAM above the floor and no hold."""
    monkeypatch.setattr(heavy, "_read_config", lambda: {"heavy_admission.free_ram_floor_mb": "4096"})
    monkeypatch.setattr(
        heavy, "_read_available", lambda: types.SimpleNamespace(trusted=True, avail_mb=32000, source="t")
    )
    monkeypatch.setattr(heavy, "_read_box_hold", lambda: None)


def _touched(monkeypatch, paths, degraded=False):
    projection = touch_record.TouchProjection(claims={p: object() for p in paths}, degraded=degraded)
    monkeypatch.setattr(touch_record, "project_live_claims", lambda *a, **k: projection)


def _payload(agent_type=None, agent_id="agent-1"):
    p = {"session_id": SESSION}
    if agent_id:
        p["agent_id"] = agent_id
    if agent_type:
        p["agent_type"] = agent_type
    return p


def _verdict(repo, payload, *args, is_subagent=True):
    return adm.test_run_verdict(payload, [["python", "-m", "pytest", *args]], str(repo), is_subagent=is_subagent)


# ------------------------------------------------------------------ identity

@pytest.mark.parametrize("agent_type", ["coordinator:executor", None, "coordinator:reviewer"])
@pytest.mark.parametrize("args", [(TOUCHED,), (TOUCHED + "::test_x",), (), ("tests/",)])
def test_non_test_runner_subagent_is_refused_every_shape(repo, monkeypatch, agent_type, args):
    _touched(monkeypatch, [TOUCHED])
    v = _verdict(repo, _payload(agent_type), *args)
    assert not v.allowed
    assert v.leg == "identity"
    assert "test-runner" in v.remedy


def test_workflow_agent_without_agent_type_is_refused(repo, monkeypatch):
    _touched(monkeypatch, [TOUCHED])
    assert _verdict(repo, _payload(None), TOUCHED).leg == "identity"


def test_no_pytest_argv_allows(repo):
    v = adm.test_run_verdict(_payload("coordinator:executor"), [], str(repo), is_subagent=True)
    assert v.allowed and v.leg == ""


# ------------------------------------------------------------------ scope

def test_test_runner_touched_file_and_node_id_allowed(repo, monkeypatch):
    _touched(monkeypatch, [TOUCHED])
    p = _payload(adm.TEST_RUNNER_TYPE)
    assert _verdict(repo, p, TOUCHED).allowed
    assert _verdict(repo, p, TOUCHED + "::test_x", "-q").allowed


def test_test_runner_absolute_path_and_dot_prefix_resolve_to_touched(repo, monkeypatch):
    _touched(monkeypatch, [TOUCHED])
    p = _payload(adm.TEST_RUNNER_TYPE)
    assert _verdict(repo, p, str(repo / TOUCHED)).allowed
    assert _verdict(repo, p, "./" + TOUCHED).allowed


@pytest.mark.parametrize(
    "args",
    [
        (UNTOUCHED,),
        (TOUCHED, UNTOUCHED),
        ("coordinator_core/bash_guards/tests",),
        (),
        ("-k", "foo"),
        ("-m", "slow"),
        (TOUCHED, "-n", "2"),
        (TOUCHED, "-n2"),
        (TOUCHED, "--numprocesses=auto"),
        (TOUCHED, "-n", "auto"),
    ],
)
def test_test_runner_outside_touched_scope_is_refused(repo, monkeypatch, args):
    _touched(monkeypatch, [TOUCHED])
    v = _verdict(repo, _payload(adm.TEST_RUNNER_TYPE), *args)
    assert not v.allowed
    assert v.leg == "scope"


def test_test_runner_n_zero_is_serial_and_allowed(repo, monkeypatch):
    _touched(monkeypatch, [TOUCHED])
    assert _verdict(repo, _payload(adm.TEST_RUNNER_TYPE), TOUCHED, "-n", "0").allowed


def test_touched_non_test_file_does_not_admit_itself(repo, monkeypatch):
    _touched(monkeypatch, ["coordinator_core/foo.py"])
    v = _verdict(repo, _payload(adm.TEST_RUNNER_TYPE), "coordinator_core/foo.py")
    assert v.leg == "scope"


def test_unreadable_touch_record_allows_with_advisory(repo, monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("gone")

    monkeypatch.setattr(touch_record, "project_live_claims", boom)
    v = _verdict(repo, _payload(adm.TEST_RUNNER_TYPE), UNTOUCHED)
    assert v.allowed
    assert "advisory" in capsys.readouterr().err


def test_degraded_record_allows_a_missing_target_with_advisory(repo, monkeypatch, capsys):
    _touched(monkeypatch, [], degraded=True)
    assert _verdict(repo, _payload(adm.TEST_RUNNER_TYPE), UNTOUCHED).allowed
    assert "degraded" in capsys.readouterr().err


def test_degraded_record_still_refuses_a_directory(repo, monkeypatch):
    _touched(monkeypatch, [], degraded=True)
    assert _verdict(repo, _payload(adm.TEST_RUNNER_TYPE), "tests/").leg == "scope"


def test_scope_leg_does_not_apply_to_the_em(repo, monkeypatch):
    _touched(monkeypatch, [])
    v = _verdict(repo, {"session_id": SESSION}, UNTOUCHED, is_subagent=False)
    assert v.allowed


# ------------------------------------------------------------------ touched_test_files

def test_touched_test_files_keeps_only_test_files(repo, monkeypatch):
    _touched(monkeypatch, [TOUCHED, "coordinator_core/foo.py", "x/y_test.py"])
    got = adm.touched_test_files({"session_id": SESSION}, str(repo))
    assert got == frozenset(adm._key(p) for p in (TOUCHED, "x/y_test.py"))


def test_touched_test_files_empty_without_session_or_on_failure(repo, monkeypatch):
    assert adm.touched_test_files({}, str(repo)) == frozenset()
    monkeypatch.setattr(touch_record, "project_live_claims", lambda *a, **k: 1 / 0)
    assert adm.touched_test_files({"session_id": SESSION}, str(repo)) == frozenset()


# ------------------------------------------------------------------ ceiling

def _low_ram(monkeypatch):
    monkeypatch.setattr(
        heavy, "_read_available", lambda: types.SimpleNamespace(trusted=True, avail_mb=1000, source="t")
    )


def test_low_ram_refuses_touched_test_runner_and_em(repo, monkeypatch):
    _touched(monkeypatch, [TOUCHED])
    _low_ram(monkeypatch)
    runner = _verdict(repo, _payload(adm.TEST_RUNNER_TYPE), TOUCHED)
    em = _verdict(repo, {"session_id": SESSION}, TOUCHED, is_subagent=False)
    for v in (runner, em):
        assert not v.allowed and v.leg == "ceiling"
        assert "1000 MB" in v.fact


def test_hold_by_another_session_refuses(repo, monkeypatch):
    hold = types.SimpleNamespace(set_by_session="other", reason="roll")
    monkeypatch.setattr(heavy, "_read_box_hold", lambda: hold)
    v = _verdict(repo, {"session_id": SESSION}, TOUCHED, is_subagent=False)
    assert v.leg == "ceiling" and "other" in v.fact


def test_own_hold_does_not_refuse(repo, monkeypatch):
    hold = types.SimpleNamespace(set_by_session=SESSION, reason="mine")
    monkeypatch.setattr(heavy, "_read_box_hold", lambda: hold)
    assert _verdict(repo, {"session_id": SESSION}, TOUCHED, is_subagent=False).allowed


def test_untrusted_ram_reading_allows_with_advisory(monkeypatch, capsys):
    monkeypatch.setattr(
        heavy, "_read_available", lambda: types.SimpleNamespace(trusted=False, avail_mb=None, source="x")
    )
    assert adm.over_admit_ceiling({"session_id": SESSION}) is None
    assert "advisory" in capsys.readouterr().err


def test_unreadable_hold_allows_with_advisory(monkeypatch, capsys):
    def boom():
        raise OSError

    monkeypatch.setattr(heavy, "_read_box_hold", boom)
    assert adm.over_admit_ceiling({"session_id": SESSION}) is None
    assert "box-hold" in capsys.readouterr().err


def test_ceiling_only_after_identity_and_scope(repo, monkeypatch):
    _touched(monkeypatch, [TOUCHED])
    _low_ram(monkeypatch)
    assert _verdict(repo, _payload("coordinator:executor"), TOUCHED).leg == "identity"
    assert _verdict(repo, _payload(adm.TEST_RUNNER_TYPE), UNTOUCHED).leg == "scope"


# ------------------------------------------------------------------ spawns

def test_verdict_spawns_no_process(repo, monkeypatch):
    _touched(monkeypatch, [TOUCHED])
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append(a))
    _verdict(repo, _payload(adm.TEST_RUNNER_TYPE), TOUCHED)
    _verdict(repo, _payload("coordinator:executor"), TOUCHED)
    assert calls == []
