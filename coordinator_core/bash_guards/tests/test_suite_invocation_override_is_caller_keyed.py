
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


class TestSubagentIdentityFailsClosed:
    _FAST = _CMD

    def _sub(self, **extra):
        p = _payload({guard._OVERRIDE_ENV_VAR: "1"})
        p["tool_input"]["command"] = self._FAST
        p.update(extra)
        return p

    def test_override_never_reaches_a_subagent(self):
        assert guard.check(self._sub(agent_id="a0123456789abcdef")) is not None

    def test_agent_type_alone_is_a_subagent(self):
        assert guard.check(self._sub(agent_type="coordinator:executor")) is not None

    def test_subagent_transcript_path_alone_is_a_subagent(self):
        tp = "/h/projects/p/sess/subagents/agent-a1.jsonl"
        assert guard.check(self._sub(transcript_path=tp)) is not None

    def test_non_string_agent_id_is_not_absent(self):
        assert guard.check(self._sub(agent_id=7)) is not None

    def test_main_transcript_path_is_not_a_subagent(self):
        p = self._sub(transcript_path="/h/projects/p/sess.jsonl")
        assert guard.check(p) is None

    def test_subagent_deny_offers_no_override(self):
        r = guard.check(self._sub(agent_id="a1", env={}))
        text = r["hookSpecificOutput"]["permissionDecisionReason"]
        assert guard._OVERRIDE_ENV_VAR not in text


class TestCloudBoxDeniesBroadSuiteForEm:
    def _em(self, env):
        return {
            "tool_name": "Bash",
            "session_id": "e641c238-68e3-480a-9e44-3ed73e8c5c94",
            "cwd": "C:/Windows/Temp",
            "tool_input": {"command": _CMD},
            "env": env,
        }

    def test_cloud_denies_even_with_a_live_grant(self, monkeypatch):
        monkeypatch.setattr(guard, "_tier_u_grant", lambda cwd: (True, None))
        r = guard.check(self._em({"CLAUDE_CODE_REMOTE": "true"}))
        assert "cloud box" in r["hookSpecificOutput"]["permissionDecisionReason"]

    def test_local_with_grant_is_not_cloud_denied(self, monkeypatch):
        monkeypatch.setattr(guard, "_tier_u_grant", lambda cwd: (True, None))
        r = guard.check(self._em({}))
        assert r is None or "cloud box" not in r["hookSpecificOutput"]["permissionDecisionReason"]
