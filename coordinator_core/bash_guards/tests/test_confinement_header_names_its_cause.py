"""A leg-3 confinement must not introduce itself as a findings agent.

Purpose: `_is_confined_type`'s third leg (`is_confined_by_roster_absence`)
confines by ABSENCE — it never names a type anywhere. So it fires for agents
that are not findings agents at all, and the default header then told them
they were one and offered them the reviewer's `coordinator-doc-new --type
review-findings` allowlist as their remedy.

That is a wrong diagnosis, not a cosmetic one, and it has a measured cost.
`2026-08-20-example-retrieval-repo-em-executor-confined-under-the-reviewer-allowlist.md`
reported a confined `coordinator:executor`. Two claude-klabauter sessions across
nineteen days went looking for the executor in
`_helpers._CONFINED_FINDINGS_AGENTS` — the one set that cannot contain it —
and both reported finding no resolver. The message had pointed them there.
Reproduced 2026-08-31: the executor is confined by leg 3 alone, on two
shapes, with the roster either unreadable or the dispatch identity
unresolved.

Negative-spec: this file pins WHICH HEADER a denial carries, never WHETHER
it denies. Both leg-3 confinements are deliberate fail-closed behaviour — an
unreadable roster degrades to "cannot confirm this type is legitimate",
never to "assume it is fine" — and whether an unclassifiable input should
pass or refuse is a separate, open, direction-class question. A refusal can
be honest about its cause under either answer, which is why this landed
without waiting on it. The verdict corpus is
`test_block_reviewer_bash_outside_allowlist.py`; the parity corpus is
`test_reviewer_executor_deny_message_parity.py`.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from coordinator_core.bash_guards import _helpers
from coordinator_core.bash_guards import block_reviewer_bash_outside_allowlist as guard

_CMD = "python -m pytest coordinator_core/bash_guards/tests -q"
_AGENT_ID = "deadbeef0123"


def _payload(agent_type: str) -> Dict[str, Any]:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": _CMD},
        "session_id": "sess-header",
        "cwd": None,
        "agent_id": _AGENT_ID,
        "agent_type": agent_type,
    }


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch):
    """The identity seam this guard's own suite wires, so the confined path
    can fire without a real git root or back-pointer chain on disk."""

    def _wire(subagent_type: str) -> None:
        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
        monkeypatch.setattr(
            guard, "_resolve_subagent_identity", lambda raw, session: _AGENT_ID
        )
        monkeypatch.setattr(
            guard,
            "_read_backpointer_subagent_type",
            lambda git_root, agent_id, **kw: subagent_type,
        )

    return _wire


@pytest.fixture
def unreadable_roster(monkeypatch: pytest.MonkeyPatch):
    """`resolve_roster()`'s documented `(None, reason)` failure contract."""
    _helpers._resolve_roster_accessor()
    monkeypatch.setattr(
        _helpers, "resolve_roster", lambda *a, **kw: (None, "test: unreadable roster source")
    )


def _header(payload: Dict[str, Any]) -> str:
    result: Optional[Dict[str, Any]] = guard.check(payload)
    assert result is not None, "fixture must produce a denial for a header to be read"
    return result["hookSpecificOutput"]["permissionDecisionReason"].splitlines()[0]


def test_an_unreadable_roster_says_so(wire, unreadable_roster) -> None:
    """The cause is recoverable and has a real remedy. Naming it is the
    whole fix; the verdict is unchanged."""
    wire("coordinator:executor")
    header = _header(_payload("my-exec-worker"))
    assert header == guard._ROSTER_UNREADABLE_HEADER_LINE
    assert "findings" not in header.lower()


def test_an_unenumerated_identity_says_so(
    wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A healthy roster that simply does not carry this identity is a
    different fact from a roster nobody could read, and points at a
    different remedy.

    The healthy roster is supplied explicitly. A bare test environment has
    no resolvable DoE checkout, so `resolve_roster()` there returns the
    FAILURE shape and this case would otherwise be indistinguishable from
    the one above — which is the distinction under test."""
    _helpers._resolve_roster_accessor()
    monkeypatch.setattr(
        _helpers,
        "resolve_roster",
        lambda *a, **kw: (frozenset({"coordinator:executor", "coordinator:code-reviewer"}), None),
    )
    wire("")
    header = _header(_payload("my-exec-worker"))
    assert header == guard._TYPE_UNENUMERATED_HEADER_LINE
    assert "findings" not in header.lower()


def test_the_real_findings_agent_keeps_its_header(wire) -> None:
    """The narrowing must not empty out the branch it narrows.
    `coordinator:code-reviewer` IS a confined findings agent, is confined by
    leg 2, and its header and remedy were always correct."""
    wire("")
    assert _header(_payload("coordinator:code-reviewer")) == guard._DEFAULT_HEADER_LINE


def test_an_explicit_per_type_header_still_wins(wire, unreadable_roster) -> None:
    """A type carrying its own `_DENY_MESSAGE_STANZA_OVERRIDES` header has an
    identity that DID resolve, so leg 3 is not why it is here and the
    type-specific prose stays. Pinned because the cause check runs on every
    denial, not only the leg-3 ones."""
    wire("coordinator:executor")
    header = _header(_payload("coordinator:executor"))
    assert header == guard._EXECUTOR_HEADER_LINE


class TestConfinementCause:
    """`_confinement_cause` mirrors `_is_confined_type`'s leg ORDER, so the
    cause reported is the one a reader re-deriving the verdict by hand finds
    first."""

    def test_findings_membership_outranks_roster_state(self, unreadable_roster) -> None:
        policy = guard.load_policy(None)
        assert guard._confinement_cause("coordinator:code-reviewer", policy) == "findings"

    def test_unreadable_roster_is_distinguished_from_absence(self, unreadable_roster) -> None:
        policy = guard.load_policy(None)
        assert guard._confinement_cause("some-teammate-name", policy) == "roster-unreadable"

    def test_a_resolver_that_raises_degrades_rather_than_propagating(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Weaker claim, never a crash: this runs inside a denial that is
        already being composed."""
        _helpers._resolve_roster_accessor()
        monkeypatch.setattr(
            _helpers,
            "is_confined_by_roster_absence",
            lambda t: True,
        )

        def _boom(*a, **kw):
            raise RuntimeError("roster resolver exploded")

        monkeypatch.setattr(_helpers, "resolve_roster", _boom)
        monkeypatch.setattr(guard, "is_confined_by_roster_absence", lambda t: True)
        policy = guard.load_policy(None)
        assert guard._confinement_cause("some-teammate-name", policy) == "unenumerated"
