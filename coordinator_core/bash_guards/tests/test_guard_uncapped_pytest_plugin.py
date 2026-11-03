"""guard-uncapped-pytest-plugin: both sides of the decision, through the production entry."""

from __future__ import annotations

import json
import tempfile

import pytest

from coordinator_core.bash_guards import dispatch, guard_uncapped_pytest_plugin as guard

NAME = "guard-uncapped-pytest-plugin"


def _payload(cmd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "s-uncapped", "cwd": "."}


def _reason(cmd: str) -> str:
    out = dispatch.evaluate_payload_json(json.dumps(_payload(cmd))) or {}
    hso = out.get("hookSpecificOutput", {})
    return str(hso.get("permissionDecisionReason", "")) if hso.get("permissionDecision") == "deny" else ""


@pytest.fixture(autouse=True)
def _isolated_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))


@pytest.mark.parametrize(
    "cmd",
    [
        "python -m pytest -p myplugin tests/",
        "python3 -m pytest -pmyplugin tests/",
        "pytest -p=myplugin tests/",
        "py.test -q -p some.adhoc.mod tests/",
        "cd x && python.exe -u -m pytest -p adhoc tests/",
        "env FOO=1 pytest -p adhoc tests/",
        "python -X dev -W ignore -m pytest -p adhoc tests/",
    ],
)
def test_adhoc_plugin_run_is_denied_by_the_guard(cmd):
    rec = guard.check(_payload(cmd))
    assert rec is not None and rec.guard_name == NAME


def test_adhoc_plugin_run_is_denied_box_degrading_through_the_dispatcher():
    reason = _reason("python -m pytest -p myplugin tests/")
    assert f"`{NAME}`" in reason
    assert "scoped_pytest" in reason


@pytest.mark.parametrize(
    "cmd",
    [
        "pytest -p no:cacheprovider tests/",
        "python -m pytest -p no:cacheprovider -q tests/",
        "pytest -p xdist -n 2 tests/",
        "pytest -p coordinator_core.testing.nested_session_guard tests/",
        "python -m coordinator_core.testing.scoped_pytest --allow tests -- -p myplugin tests/",
        "pytest tests/ -q",
        "echo pytest -p myplugin",
        "grep -p myplugin pytest.ini",
        "pytest -- -p myplugin",
        "",
    ],
)
def test_capped_or_unrelated_command_is_admitted(cmd):
    assert guard.check(_payload(cmd)) is None


def test_scoped_launcher_is_not_denied_by_this_guard_through_the_dispatcher():
    cmd = "python -m coordinator_core.testing.scoped_pytest --allow tests -- -p myplugin tests/"
    assert f"`{NAME}`" not in _reason(cmd)


def test_non_command_tool_is_ignored():
    p = _payload("pytest -p myplugin")
    p["tool_name"] = "Read"
    assert guard.check(p) is None

