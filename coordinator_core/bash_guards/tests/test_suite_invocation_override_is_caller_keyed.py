"""`check_test_suite_invocation`'s own override must be read off the per-call payload.

The guard's kill switch was an ambient `os.environ` read. That was correct while every
evaluation was a fresh child of the caller's shell and is wrong now that the chain runs
inside resident processes (`warm/hook_http.py :: evaluate_cold`, `warm/server.py ::
_run_dispatch`): ambient there fails in BOTH directions, which is why one test is not
enough. A caller's legitimate pre-launch override becomes invisible, so an operator who did
everything the docstring asked still cannot run their suite; and whatever the resident
process happens to have started under disarms the guard for every session on the box, with
nothing logged and nothing to notice.

`dispatch_checks._override` already encodes exactly this (C14c, `32d5224ed`) and falls back
to ambient when the payload carries no `env`, which is what keeps every direct and test
call site unchanged. This file pins that the guard is on that reader rather than beside it.
"""

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
        # The invisible-disarm direction, and the one no earlier test covered: the
        # payload carries an EMPTY env (a truthful "this caller set no overrides",
        # which is what `payload_from_event` builds), so the ambient value below must
        # not be consulted at all.
        monkeypatch.setenv(guard._OVERRIDE_ENV_VAR, "1")

        assert guard.check(_payload({})) is not None

    def test_a_payload_with_no_env_key_still_falls_back_to_ambient(self, monkeypatch):
        # `_override`'s documented compatibility rung, pinned here because the fallback
        # is what every direct/test caller of `check()` relies on -- removing it would
        # be a silent behaviour change for callers that never cross a wire.
        monkeypatch.setenv(guard._OVERRIDE_ENV_VAR, "1")

        assert guard.check(_payload()) is None
