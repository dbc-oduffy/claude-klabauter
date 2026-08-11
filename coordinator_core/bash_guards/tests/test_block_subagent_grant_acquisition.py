"""Tests for coordinator_core.bash_guards.block_subagent_grant_acquisition.

Covers the ACQUISITION-side leg closing the hole where a dispatched
subagent mints its own `granted_by: "pm"` CLAUDE.md write-grant record: the
`-m` module form (`python3 -m coordinator_core.session.claude_md_grant
grant ...`), the widened `-c` inline-import form (AC11), the identity gate
(raw `agent_id` presence, fail CLOSED on unresolvable -- AC1/AC2/AC3), the
`grant`-only subcommand gating (`read`/`check` allowed -- AC4), the `--agent`
EM-session regression pin (AC5, `agent_type`-only is NOT denied), the
dispatch-chain wiring (C2), and a scope-equals-enforcement check against the
module's own docstring (AC10, DR-104).

Pure Python -- no shell spawns, no git repo required.

Spec backlink: coordinator_core/bash_guards/block_subagent_grant_acquisition.py
Spec backlink: docs/plans/2026-08-08-discriminate-the-caller-on-the-write-grant.md, chunk C3
"""

from __future__ import annotations

import json

from coordinator_core.bash_guards import block_subagent_grant_acquisition as guard
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
    'python3 -m coordinator_core.session.claude_md_grant grant pm "note"'
)
_DASH_C_GRANT = (
    "python3 -c \"from coordinator_core.session.claude_md_grant import "
    "write_claude_md_write_grant as w; w('pm', '...')\""
)
_DASH_C_READ = (
    "python3 -c \"from coordinator_core.session.claude_md_grant import "
    "read_claude_md_write_grant as r; r()\""
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
        """AC2: no `agent_id` at all -> main-loop EM -> allowed."""
        assert guard.check(_payload(_MODULE_M_GRANT)) is None

    def test_subagent_grant_denies(self):
        """AC1: subagent (`agent_id` present) invoking `grant` -> DENY."""
        _reason(guard.check(_payload(_MODULE_M_GRANT, agent_id="a1")))

    def test_present_but_unresolvable_agent_id_denies(self):
        """AC3: fails CLOSED on a present-but-unresolvable `agent_id`."""
        _reason(
            guard.check(
                _payload(_MODULE_M_GRANT, agent_id="unresolvable-nonexistent-id")
            )
        )

    def test_subagent_read_allows(self):
        """AC4: `read` is not gated."""
        assert guard.check(
            _payload(
                "python3 -m coordinator_core.session.claude_md_grant read",
                agent_id="a1",
            )
        ) is None

    def test_subagent_check_allows(self):
        """AC4: `check` is not gated."""
        assert guard.check(
            _payload(
                "python3 -m coordinator_core.session.claude_md_grant check",
                agent_id="a1",
            )
        ) is None

    def test_lookalike_module_name_does_not_match(self):
        assert guard.check(
            _payload(
                "python3 -m coordinator_core.session.claude_md_grant_extra grant pm x",
                agent_id="a1",
            )
        ) is None
        assert guard.check(
            _payload(
                "python3 -m coordinator_core.session.claude_md_grantx grant pm x",
                agent_id="a1",
            )
        ) is None


class TestAgentTypeOnlyRegression:
    """AC5, LOAD-BEARING: the `--agent` EM-session regression the spike
    surfaced. `agent_type` alone (no `agent_id`) must NOT be denied -- this
    guard gates on raw `agent_id` presence alone, never the broader
    `resolve_effective_types` OR-resolved triple."""

    def test_agent_type_only_payload_not_denied(self):
        assert guard.check(
            _payload(_MODULE_M_GRANT, agent_type="coordinator:em")
        ) is None


class TestDashCForm:
    """AC11: widened inline-import `-c` shape."""

    def test_subagent_dash_c_grant_denies(self):
        _reason(guard.check(_payload(_DASH_C_GRANT, agent_id="a1")))

    def test_em_dash_c_grant_allows(self):
        assert guard.check(_payload(_DASH_C_GRANT)) is None

    def test_subagent_dash_c_read_allows(self):
        """A `-c` payload referencing only a read/check-shaped name mirrors
        the `-m` form's grant-only gating."""
        assert guard.check(_payload(_DASH_C_READ, agent_id="a1")) is None


class TestDispatchWiring:
    """C2: the leg is registered in `dispatch.py`'s CONFINEMENT_DENY run and
    is reachable through the dispatcher entrypoint, not merely callable in
    isolation -- this is the one thing C2 delivers that every other case in
    this file would pass identically against a completely unregistered
    guard."""

    def test_registered_in_confinement_deny_run(self):
        chain = dispatch._build_guard_chain(
            cmd="echo bash-guard-grant-acquisition-probe",
            session_id="probe-session",
            cwd="/tmp",
            payload={"tool_name": "Bash", "tool_input": {"command": "echo x"}},
            policy_file=None,
            host_is_windows=None,
        )
        entry = next(
            (e for e in chain if e.name == "block-subagent-grant-acquisition"), None
        )
        assert entry is not None, (
            "block-subagent-grant-acquisition missing from dispatch._build_guard_chain"
        )
        assert entry.band is dispatch.GuardBand.CONFINEMENT_DENY
        assert entry.fail_closed is True

    def test_reachable_through_dispatcher_entrypoint(self):
        payload = _payload(_MODULE_M_GRANT, agent_id="a1")
        out = dispatch.evaluate_payload_json(json.dumps(payload))
        assert out is not None, (
            "dispatcher entrypoint allowed a grant-acquisition invocation "
            "that check() alone denies -- leg not reachable through the "
            "dispatch chain"
        )
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"


class TestScopeEqualsEnforcement:
    """AC10, DR-104: assert the leg denies EXACTLY the grant-acquisition
    invocations its own docstring claims to deny -- no wider, no narrower.
    Named precedent (DR-104, cited in the module docstring's HEURISTIC-NOT-
    EXHAUSTIVE section): `check_blanket_git_add`'s documented scope gap,
    where doctrine claimed a wider deny scope than the code enforced. This
    case exists so this leg does not repeat that gap.

    The docstring's RECOGNIZED SHAPES section claims exactly two positive
    shapes (`-m ... grant`, and `-c` referencing both the module and
    `write_claude_md_write_grant`) and explicitly disclaims exhaustiveness
    (HEURISTIC-NOT-EXHAUSTIVE) -- so this test does NOT assert coverage of
    an indirection route (script-to-disk-then-exec, alternate interpreter,
    etc.) the docstring itself says is not caught."""

    def test_denies_exactly_the_two_documented_shapes(self):
        # Shape 1: `-m` + module + `grant` subcommand.
        _reason(guard.check(_payload(_MODULE_M_GRANT, agent_id="a1")))
        # Shape 2: `-c` referencing both the module and the write function.
        _reason(guard.check(_payload(_DASH_C_GRANT, agent_id="a1")))

    def test_does_not_deny_a_command_merely_mentioning_the_module_name(self):
        """Not wider than documented: a command that references the module
        path in prose/echo, without either recognized invocation shape, is
        not classified."""
        assert guard.check(
            _payload(
                "echo coordinator_core.session.claude_md_grant",
                agent_id="a1",
            )
        ) is None

    def test_does_not_deny_dash_c_mentioning_module_without_write_func(self):
        """Not wider than documented: a `-c` payload referencing the module
        but not `write_claude_md_write_grant` is not grant-shaped."""
        assert guard.check(_payload(_DASH_C_READ, agent_id="a1")) is None

    def test_does_not_deny_dash_c_mentioning_lookalike_func_name(self):
        """Not wider than documented: a `-c` payload referencing an
        identifier that merely CONTAINS `write_claude_md_write_grant` as a
        substring (e.g. a hypothetical helper name) must not classify as
        grant-shaped -- word-boundary match, not substring containment."""
        payload = (
            "python3 -c \"from coordinator_core.session.claude_md_grant import "
            "_write_claude_md_write_grant_helper as w; w()\""
        )
        assert guard.check(_payload(payload, agent_id="a1")) is None

    def test_does_not_deny_non_python_command(self):
        """Not wider than documented: the docstring's RECOGNIZED SHAPES are
        both python-interpreter-headed; a non-python command is out of
        scope regardless of content."""
        assert guard.check(
            _payload(
                "rg 'claude_md_grant' coordinator_core/",
                agent_id="a1",
            )
        ) is None
