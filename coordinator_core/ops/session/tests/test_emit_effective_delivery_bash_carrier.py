"""bash_dispatch carrier resolution across its three transports: command script, http, hook-run."""

from __future__ import annotations

import pytest

from coordinator_core.ops.session import emit_effective_delivery as mod

_MATCHER = "Bash|PowerShell"


def _doc(*hooks):
    return {"hooks": {"PreToolUse": [{"matcher": _MATCHER, "hooks": list(hooks)}]}}


_HOOK_RUN = {
    "type": "command",
    "command": 'COORDINATOR_DOOR_STDIN_MODE=hook "${COORDINATOR_SETTINGS_HOME}/bin/hook-run" hooks.preuse_bash_dispatch',
}
_HTTP = {"type": "http", "url": mod._BASH_CARRIER_HTTP_URL}


def test_hook_run_registration_resolves_the_carrier():
    carrier = mod.build_carrier_bash_dispatch({}, _doc(_HOOK_RUN))
    assert carrier["script"] == "hook-run:hooks.preuse_bash_dispatch"
    assert carrier["matcher"] == _MATCHER
    assert any(g["id"] == "guard-subagent-heavy-ue-launch" for g in carrier["guards"])


def test_hook_run_matcher_ignores_a_longer_op_name():
    other = {"type": "command", "command": "hook-run --advisory hooks.preuse_bash_dispatch_extra"}
    assert mod._hook_run_matchers_for_op(_doc(other), "hooks.preuse_bash_dispatch") == set()


def test_two_transports_refuse():
    with pytest.raises(mod.EmitterError, match="more than one transport"):
        mod.build_carrier_bash_dispatch({}, _doc(_HOOK_RUN, _HTTP))


def test_no_registration_refuses():
    with pytest.raises(mod.EmitterError, match="not singular"):
        mod.build_carrier_bash_dispatch({}, {"hooks": {}})
