"""Tests for coordinator_core.bash_guards.block_subagent_findings_reject.

Covers the EM-only leg closing `review-findings-ledger reject`/`targets` to
a dispatched subagent: the `-m` module form, the direct-script trampoline
form (bare and python3-prefixed), the identity gate (raw `agent_id`
presence, fail CLOSED on unresolvable), the `verify`-is-not-gated carve-out,
and the dispatch-chain wiring.

Pure Python -- no shell spawns, no git repo required.

Spec backlink: coordinator_core/bash_guards/block_subagent_findings_reject.py
Spec backlink: DoE-claude docs/plans/2026-09-26-retire-review-integrator.md, row M3
"""

from __future__ import annotations

from coordinator_core.bash_guards import block_subagent_findings_reject as guard
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


_MODULE_M_REJECT = (
    'python3 -m coordinator_core.ops.review_findings_ledger reject '
    '--sidecar s.md --finding finding-1 --reason "stale"'
)
_MODULE_M_TARGETS = (
    'python3 -m coordinator_core.ops.review_findings_ledger targets --add x.py'
)
_MODULE_M_VERIFY = (
    'python3 -m coordinator_core.ops.review_findings_ledger verify --sidecar s.md'
)
_SCRIPT_REJECT = (
    'python3 /repo/coordinator/bin/review-findings-ledger.py reject '
    '--sidecar s.md --finding finding-1 --reason "stale"'
)
_BARE_SCRIPT_TARGETS = (
    'review-findings-ledger.py targets --add x.py'
)


class TestNonBashOrEmpty:
    def test_non_bash_tool_allows(self):
        assert guard.check({"tool_name": "Edit", "tool_input": {"file_path": "x"}}) is None

    def test_empty_command_allows(self):
        assert guard.check(_payload("", agent_id="a1")) is None

    def test_malformed_tool_input_allows(self):
        assert guard.check({"tool_name": "Bash", "tool_input": "not-a-dict"}) is None


class TestDashMForm:
    def test_em_reject_allows(self):
        """No `agent_id` at all -> main-loop EM -> allowed."""
        assert guard.check(_payload(_MODULE_M_REJECT)) is None

    def test_subagent_reject_denies(self):
        _reason(guard.check(_payload(_MODULE_M_REJECT, agent_id="a1")))

    def test_subagent_targets_denies(self):
        _reason(guard.check(_payload(_MODULE_M_TARGETS, agent_id="a1")))

    def test_present_but_unresolvable_agent_id_denies(self):
        _reason(
            guard.check(
                _payload(_MODULE_M_REJECT, agent_id="unresolvable-nonexistent-id")
            )
        )

    def test_subagent_verify_allows(self):
        """`verify` is not gated -- a reviewer's own self-check."""
        assert guard.check(_payload(_MODULE_M_VERIFY, agent_id="a1")) is None

    def test_lookalike_module_name_does_not_match(self):
        assert guard.check(
            _payload(
                "python3 -m coordinator_core.ops.review_findings_ledger_extra reject x",
                agent_id="a1",
            )
        ) is None


class TestAgentTypeOnlyRegression:
    """`agent_type` alone (no `agent_id`) must NOT be denied -- this guard
    gates on raw `agent_id` presence alone."""

    def test_agent_type_only_payload_not_denied(self):
        assert guard.check(
            _payload(_MODULE_M_REJECT, agent_type="coordinator:em")
        ) is None


class TestDirectScriptForm:
    def test_subagent_script_reject_denies(self):
        _reason(guard.check(_payload(_SCRIPT_REJECT, agent_id="a1")))

    def test_em_script_reject_allows(self):
        assert guard.check(_payload(_SCRIPT_REJECT)) is None

    def test_subagent_bare_script_targets_denies(self):
        _reason(guard.check(_payload(_BARE_SCRIPT_TARGETS, agent_id="a1")))

    def test_subagent_bare_script_verify_allows(self):
        assert guard.check(
            _payload("review-findings-ledger.py verify --sidecar s.md", agent_id="a1")
        ) is None


class TestDispatchWiring:
    def test_guard_registered_in_dispatch_chain(self):
        payload = _payload(_MODULE_M_REJECT, agent_id="a1")
        chain = dispatch._build_guard_chain(
            _MODULE_M_REJECT, "sess1", "/repo", payload, None, None
        )
        names = [entry.name for entry in chain]
        assert "block-subagent-findings-reject" in names

    def test_dispatch_denies_subagent_reject(self):
        import json

        out = dispatch.evaluate_payload_json(
            json.dumps(_payload(_MODULE_M_REJECT, agent_id="a1"))
        )
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
