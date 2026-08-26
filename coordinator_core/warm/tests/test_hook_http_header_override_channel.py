"""The caller's overrides ride REGISTRATION HEADERS, and a vetoed channel is loud.

WHY THIS FILE EXISTS. `payload_from_event` reads `event["env"]`; the harness never writes
one. Measured n=2 on harness 2.1.246 with two positive controls: the POST body carries
`cwd, effort, hook_event_name, permission_mode, prompt_id, session_id, tool_input,
tool_name, tool_use_id, transcript_path` and no `env` under any spelling, while an
allowlisted var interpolated into a header arrives as the CALLER's value. So the override
boundary AC10 governs has exactly one live channel and it is headers.

The load-bearing case is the third one below, and it is a SAFETY test rather than a
plumbing test: an `httpHookAllowedEnvVars` setting can veto a name the registration
allowed, and a vetoed interpolation arrives EMPTY -- indistinguishable, to the guard, from
a caller who set no override. That ambiguity resolves in the PERMISSIVE direction, so a
disarmed channel must refuse to report a verdict rather than report a clean one.
"""

from coordinator_core.warm import hook_http

CHANNEL = hook_http.OVERRIDE_CHANNEL_HEADER
CANARY = hook_http.OVERRIDE_CANARY_HEADER


def _declared(**extra):
    headers = {CHANNEL: "declared", CANARY: "canary-value"}
    headers.update(extra)
    return headers


def test_undeclared_channel_is_not_a_fault_and_yields_no_overrides():
    """A legacy registration declares no channel; that is silence, not disarmament.

    Returning a `disarm_reason` here would fail every registration that predates this
    channel closed, which is the opposite of the safety property -- the guard would stop
    running for everyone rather than for the one case that is actually broken.
    """
    env, disarm = hook_http.env_from_headers({"X-Other": "x"})
    assert env == {}
    assert disarm is None


def test_declared_channel_forwards_the_callers_override():
    env, disarm = hook_http.env_from_headers(
        _declared(**{"X-Coordinator-Env-COORDINATOR_OVERRIDE_BASH": "caller-value"})
    )
    assert disarm is None
    assert env == {"COORDINATOR_OVERRIDE_BASH": "caller-value"}


def test_declared_channel_with_empty_canary_disarms_loudly():
    """The veto case. Channel declared, canary interpolated empty -> refuse, do not allow.

    Mutation check: returning `(env, None)` here makes this test fail, which is the point --
    an empty canary means the setting ate every override header too, so an `env` built from
    what survived is a confident answer to a question that was never asked.
    """
    env, disarm = hook_http.env_from_headers(
        {
            CHANNEL: "declared",
            CANARY: "",
            "X-Coordinator-Env-COORDINATOR_OVERRIDE_BASH": "",
        }
    )
    assert env == {}
    assert disarm is not None
    assert hook_http.OVERRIDE_CANARY_ENV in disarm


def test_a_whitespace_canary_is_treated_as_empty():
    _, disarm = hook_http.env_from_headers({CHANNEL: "declared", CANARY: "   "})
    assert disarm is not None


def test_only_forwardable_prefixes_survive_the_header_channel():
    """The header channel is not a wider door than the body one it replaces.

    `forwardable_env`'s prefix allowlist is what stops arbitrary session secrets reaching
    guard code; routing around it by renaming a header would be a privilege escalation
    dressed as plumbing.
    """
    env, disarm = hook_http.env_from_headers(
        _declared(
            **{
                "X-Coordinator-Env-COORDINATOR_OVERRIDE_OK": "yes",
                "X-Coordinator-Env-AWS_SECRET_ACCESS_KEY": "leaked",
                "X-Coordinator-Env-PATH": "/usr/bin",
            }
        )
    )
    assert disarm is None
    assert env == {"COORDINATOR_OVERRIDE_OK": "yes"}


def test_the_channel_and_canary_headers_are_not_themselves_forwarded():
    env, _ = hook_http.env_from_headers(_declared())
    assert env == {}


def test_header_names_are_matched_case_insensitively():
    """HTTP header casing is not preserved end to end; env var names are uppercase."""
    env, disarm = hook_http.env_from_headers(
        {
            CHANNEL.lower(): "declared",
            CANARY.lower(): "canary-value",
            "x-coordinator-env-coordinator_override_bash": "caller-value",
        }
    )
    assert disarm is None
    assert env == {"COORDINATOR_OVERRIDE_BASH": "caller-value"}


def test_an_empty_override_header_is_dropped_rather_than_forwarded_as_empty():
    """An empty value is not an override; forwarding `""` would let it read as one set."""
    env, disarm = hook_http.env_from_headers(
        _declared(**{"X-Coordinator-Env-COORDINATOR_OVERRIDE_BASH": ""})
    )
    assert disarm is None
    assert env == {}


def test_the_disarm_response_says_the_guard_did_not_run():
    """The refusal must reach the MODEL, not only the operator.

    `additionalContext` is nested-only over this transport (measured); a top-level copy
    goes nowhere behind a 200, which is how this plan's anti-scope was void over the wire
    once already.
    """
    _, disarm = hook_http.env_from_headers({CHANNEL: "declared", CANARY: ""})
    body = hook_http.unreachable_response("PreToolUse", disarm)
    assert "did not run" in body["systemMessage"]
    assert "additionalContext" not in body
    assert "additionalContext" in body["hookSpecificOutput"]
    assert body["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
