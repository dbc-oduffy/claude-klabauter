
from __future__ import annotations

from coordinator_core.bash_guards import check_test_suite_invocation as guard

_CMD = "python3 -m pytest"


def _payload(env: dict | None = None) -> dict:
    return {
        "tool_name": "Bash",
        "session_id": "e641c238-68e3-480a-9e44-3ed73e8c5c94",
        "cwd": "C:/Windows/Temp",
        "tool_input": {"command": _CMD},
        **({"env": env} if env is not None else {}),
    }


class TestOverrideIsCallerKeyed:
    def test_the_callers_override_disarms_the_guard(self, monkeypatch):
        monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)

        assert guard.check(_payload({guard._OVERRIDE_ENV_VAR: "1"})) is None

    def test_the_host_processs_own_override_does_not_reach_a_caller_who_set_none(
        self, monkeypatch
    ):
        monkeypatch.setenv(guard._OVERRIDE_ENV_VAR, "1")

        assert guard.check(_payload({})) is not None

    def test_a_payload_with_no_env_key_still_falls_back_to_ambient(self, monkeypatch):
        monkeypatch.setenv(guard._OVERRIDE_ENV_VAR, "1")

        assert guard.check(_payload()) is None
