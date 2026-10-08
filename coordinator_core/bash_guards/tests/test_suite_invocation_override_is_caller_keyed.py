
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


class TestCloudBoxAllowsBroadSuiteForEm:
    def _em(self, env):
        return {
            "tool_name": "Bash",
            "session_id": "e641c238-68e3-480a-9e44-3ed73e8c5c94",
            "cwd": "C:/Windows/Temp",
            "tool_input": {"command": _CMD},
            "env": env,
        }

    def test_cloud_em_runs_with_no_grant(self, monkeypatch):
        # Pin the machine rung: the cloud basis needs a cloud machine, and the host running
        # this test usually isn't one.
        from coordinator_core import env_locality
        monkeypatch.setattr(guard, "_tier_u_grant", lambda cwd: (False, None))
        monkeypatch.setattr(env_locality, "machine_rung", lambda *a, **k: env_locality.Locality("cloud", "high", "machine", "test"))
        monkeypatch.setattr(guard, "_mutex_holder", lambda: None)
        # Cloud-box discharges the authority leg only; the suite-mutex wrapper leg still binds.
        payload = self._em({"CLAUDE_CODE_REMOTE": "true"})
        payload["tool_input"]["command"] = "with-suite-mutex -- " + _CMD
        assert guard.check(payload) is None

    def test_local_em_without_grant_is_denied(self, monkeypatch):
        monkeypatch.setattr(guard, "_tier_u_grant", lambda cwd: (False, None))
        assert guard.check(self._em({})) is not None
