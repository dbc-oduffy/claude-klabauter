"""Tests for coordinator_core.bash_guards.block_subagent_guard_grant.

Covers the ACQUISITION-side leg denying a dispatched subagent from minting
its own EM guard-grant record: the `-m` module form (`python3 -m
coordinator_core.session.em_guard_grant grant ...`), the `-c` inline-import
form, the identity gate (raw `agent_id` presence, fail CLOSED on
unresolvable), the `grant`-only subcommand gating (`read`/`check`
allowed), the `--agent` EM-session regression pin (`agent_type`-only is
NOT denied), and the dispatch-chain wiring.

Pure Python -- no shell spawns, no git repo required.

Spec backlink: coordinator_core/bash_guards/block_subagent_guard_grant.py
Spec backlink: pln-an-em-exercisable-in-band-gran-6bfb4a, chunk C3
"""

from __future__ import annotations

import json

from coordinator_core.bash_guards import block_subagent_guard_grant as guard
from coordinator_core.bash_guards import dispatch


def _payload(command, agent_id=None, agent_type=None):
    p = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": "/repo",
    }
    if agent_id is not None:
        p["agent_id"] = agent_id
    if agent_type is not None:
        p["agent_type"] = agent_type
    return p


def _reason(out):
    assert out is not None, "expected a deny envelope, got allow"
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    return hso["permissionDecisionReason"]


_MODULE_M_GRANT = (
    'python3 -m coordinator_core.session.em_guard_grant grant '
    'bump-foreign-repo-write "reason"'
)
_DASH_C_GRANT = (
    "python3 -c \"from coordinator_core.session.em_guard_grant import "
    "write_em_guard_grant as w; w('bump-foreign-repo-write', 'reason')\""
)
_DASH_C_READ = (
    "python3 -c \"from coordinator_core.session.em_guard_grant import "
    "read_em_guard_grant as r; r()\""
)


class TestNonBashOrEmpty:
    def test_non_bash_tool_allows(self):
        assert guard.check({"tool_name": "Edit", "tool_input": {"file_path": "x"}}) is None

    def test_empty_command_allows(self):
        assert guard.check(_payload("", agent_id="a1")) is None

    def test_malformed_tool_input_allows(self):
        assert guard.check({"tool_name": "Bash", "tool_input": "not-a-dict"}) is None


class TestDashMForm:
    def test_em_grant_allows(self):
        """No `agent_id` at all -> main-loop EM -> allowed."""
        assert guard.check(_payload(_MODULE_M_GRANT)) is None

    def test_subagent_grant_denies(self):
        """Subagent (`agent_id` present) invoking `grant` -> DENY."""
        _reason(guard.check(_payload(_MODULE_M_GRANT, agent_id="a1")))

    def test_present_but_unresolvable_agent_id_denies(self):
        """Fails CLOSED on a present-but-unresolvable `agent_id` (AC-3)."""
        _reason(
            guard.check(
                _payload(_MODULE_M_GRANT, agent_id="unresolvable-nonexistent-id")
            )
        )

    def test_subagent_read_allows(self):
        """`read` is not gated."""
        assert guard.check(
            _payload(
                "python3 -m coordinator_core.session.em_guard_grant read",
                agent_id="a1",
            )
        ) is None

    def test_subagent_check_allows(self):
        """`check` is not gated."""
        assert guard.check(
            _payload(
                "python3 -m coordinator_core.session.em_guard_grant check",
                agent_id="a1",
            )
        ) is None

    def test_lookalike_module_name_does_not_match(self):
        assert guard.check(
            _payload(
                "python3 -m coordinator_core.session.em_guard_grant_extra grant x y",
                agent_id="a1",
            )
        ) is None
        assert guard.check(
            _payload(
                "python3 -m coordinator_core.session.em_guard_grantx grant x y",
                agent_id="a1",
            )
        ) is None


class TestAgentTypeOnlyRegression:
    """LOAD-BEARING: the `--agent` EM-session regression the sibling module
    already pins. `agent_type` alone (no `agent_id`) must NOT be denied --
    this guard gates on raw `agent_id` presence alone, never the broader
    `resolve_effective_types` OR-resolved triple."""

    def test_agent_type_only_payload_not_denied(self):
        assert guard.check(
            _payload(_MODULE_M_GRANT, agent_type="coordinator:em")
        ) is None


class TestDashCForm:
    def test_subagent_dash_c_grant_denies(self):
        _reason(guard.check(_payload(_DASH_C_GRANT, agent_id="a1")))

    def test_em_dash_c_grant_allows(self):
        assert guard.check(_payload(_DASH_C_GRANT)) is None

    def test_subagent_dash_c_read_allows(self):
        """A `-c` payload referencing only a read/check-shaped name mirrors
        the `-m` form's grant-only gating."""
        assert guard.check(_payload(_DASH_C_READ, agent_id="a1")) is None


class TestDispatchWiring:
    """The one thing dispatch registration delivers that every other case
    in this file would pass identically against a completely unregistered
    guard: reachability through the dispatcher entrypoint, not merely
    callable in isolation."""

    def test_registered_in_confinement_deny_run(self):
        chain = dispatch._build_guard_chain(
            cmd="echo bash-guard-grant-probe",
            session_id="probe-session",
            cwd="/tmp",
            payload={"tool_name": "Bash", "tool_input": {"command": "echo x"}},
            policy_file=None,
            host_is_windows=None,
        )
        entry = next(
            (e for e in chain if e.name == "block-subagent-guard-grant"), None
        )
        assert entry is not None, (
            "block-subagent-guard-grant missing from dispatch._build_guard_chain"
        )
        assert entry.band is dispatch.GuardBand.CONFINEMENT_DENY
        assert entry.fail_closed is True

    def test_reachable_through_dispatcher_entrypoint(self):
        payload = _payload(_MODULE_M_GRANT, agent_id="a1")
        out = dispatch.evaluate_payload_json(json.dumps(payload))
        assert out is not None, (
            "dispatcher entrypoint allowed a guard-grant-acquisition "
            "invocation that check() alone denies -- leg not reachable "
            "through the dispatch chain"
        )
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"


class TestScopeEqualsEnforcement:
    """Assert the leg denies EXACTLY the guard-grant-acquisition
    invocations its own docstring claims to deny -- no wider, no
    narrower. The docstring's RECOGNIZED SHAPES section claims exactly two
    positive shapes and explicitly disclaims exhaustiveness
    (HEURISTIC-NOT-EXHAUSTIVE) -- this test does NOT assert coverage of an
    indirection route the docstring itself says is not caught."""

    def test_denies_exactly_the_two_documented_shapes(self):
        _reason(guard.check(_payload(_MODULE_M_GRANT, agent_id="a1")))
        _reason(guard.check(_payload(_DASH_C_GRANT, agent_id="a1")))

    def test_does_not_deny_a_command_merely_mentioning_the_module_name(self):
        assert guard.check(
            _payload(
                "echo coordinator_core.session.em_guard_grant",
                agent_id="a1",
            )
        ) is None

    def test_does_not_deny_dash_c_mentioning_module_without_write_func(self):
        assert guard.check(_payload(_DASH_C_READ, agent_id="a1")) is None

    def test_does_not_deny_dash_c_mentioning_lookalike_func_name(self):
        """Word-boundary match, not substring containment."""
        payload = (
            "python3 -c \"from coordinator_core.session.em_guard_grant import "
            "_write_em_guard_grant_helper as w; w()\""
        )
        assert guard.check(_payload(payload, agent_id="a1")) is None

    def test_does_not_deny_non_python_command(self):
        assert guard.check(
            _payload(
                "rg 'em_guard_grant' coordinator_core/",
                agent_id="a1",
            )
        ) is None
