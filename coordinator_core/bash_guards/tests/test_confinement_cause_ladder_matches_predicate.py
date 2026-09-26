"""`_confinement_cause` and `_is_confined_type` walk one ladder twice.

Purpose: the two functions encode the same three-leg confinement ladder in
the same order — leg 1 a `bash_policy:` key, leg 2 `_CONFINED_FINDINGS_AGENTS`
membership, leg 3 `is_confined_by_roster_absence` — one returning the verdict
and one returning which leg produced it. A fourth leg added to the predicate
alone would leave every denial reporting a cause that is no longer true, and
nothing in the module catches that.

Kira (2026-08-31) proposed closing the desync by collapsing the two into
`_is_confined_type = bool(_confinement_cause(...))`. That was rejected on a
measurement the review could not run: `_confinement_cause`'s leg-3 arm
resolves the roster a SECOND time, after `is_confined_by_roster_absence` has
already resolved it to return True. `_helpers._resolve_roster_accessor`
caches the import, not the result, and `resolve_roster` is uncached — so the
second call is a second full walk of DoE's policy YAML, `coordinator/agents/
*.md`, and the plugin discovery tree. `_is_confined_type` runs four times per
dispatch across two identity legs; routing it through the cause function
would take every roster-absence-confined dispatch from four walks to eight,
against a 500ms per-process budget.

So the duplication stays and this file is what makes it safe: the ladders are
pinned in agreement leg by leg, including the case neither confines. A fourth
leg added to one and not the other fails here.

Negative-spec: this pins AGREEMENT between two functions, never the verdict
either one should reach — the verdict corpus is
`test_block_reviewer_bash_outside_allowlist.py`, and which header a denial
carries is `test_confinement_header_names_its_cause.py`. It also does not pin
the roster-unreadable/unenumerated SPLIT, which exists only on the cause side
and has no predicate half to agree with; that split is pinned in the header
file. Adding a `bool()` assertion here about a leg the predicate cannot see
would be pinning the collapse this file exists to say was rejected.
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

import pytest

from coordinator_core.bash_guards import _helpers
from coordinator_core.bash_guards import block_reviewer_bash_outside_allowlist as guard


class _Policy:

    def __init__(self, bash_policy: Any) -> None:
        self.bash_policy = bash_policy


@pytest.fixture
def roster(monkeypatch: pytest.MonkeyPatch):

    def _install(members: Tuple[str, ...]) -> None:
        _helpers._resolve_roster_accessor()
        monkeypatch.setattr(
            _helpers, "resolve_roster", lambda *a, **kw: (frozenset(members), None)
        )

    return _install


_LADDER_CASES: List[Tuple[str, str, Any, Tuple[str, ...], str]] = [
    (
        "leg-1 policy key",
        "coordinator:some-worker",
        {"coordinator:some-worker": {"allow": []}},
        ("coordinator:some-worker",),
        "policy",
    ),
    (
        "leg-2 findings-agent membership",
        "coordinator:code-reviewer",
        None,
        ("coordinator:code-reviewer",),
        "findings",
    ),
    (
        "leg-3 absent from a healthy roster",
        "invented-type-nobody-enumerated",
        None,
        ("coordinator:enricher",),
        "unenumerated",
    ),
    (
        "no leg fires: enumerated and deliberately unconfined",
        "coordinator:enricher",
        None,
        ("coordinator:enricher",),
        "",
    ),
    (
        "no leg fires: empty type contributes nothing in either direction",
        "",
        None,
        ("coordinator:enricher",),
        "",
    ),
]


@pytest.mark.parametrize(
    "label,effective_type,bash_policy,members,expected_cause",
    _LADDER_CASES,
    ids=[c[0] for c in _LADDER_CASES],
)
def test_cause_agrees_with_predicate_on_every_leg(
    roster,
    label: str,
    effective_type: str,
    bash_policy: Any,
    members: Tuple[str, ...],
    expected_cause: str,
) -> None:
    roster(members)
    policy = _Policy(bash_policy)

    confined = guard._is_confined_type(effective_type, policy)
    cause = guard._confinement_cause(effective_type, policy)

    assert cause == expected_cause, label
    assert bool(cause) is confined, (
        "%s: the ladders disagree — predicate=%r, cause=%r. A leg was added "
        "to one and not the other." % (label, confined, cause)
    )


def test_a_bash_policy_that_is_not_a_dict_falls_through_identically(roster) -> None:
    roster(("coordinator:enricher",))
    policy = _Policy("not-a-dict")

    assert guard._is_confined_type("coordinator:enricher", policy) is False
    assert guard._confinement_cause("coordinator:enricher", policy) == ""


def test_leg_order_is_the_same_on_both_sides(roster) -> None:
    roster(("coordinator:code-reviewer",))
    policy = _Policy({"coordinator:code-reviewer": {"allow": []}})

    assert guard._is_confined_type("coordinator:code-reviewer", policy) is True
    assert guard._confinement_cause("coordinator:code-reviewer", policy) == "policy"


def test_the_cause_never_raises_when_the_resolver_throws(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _helpers._resolve_roster_accessor()

    calls: List[int] = []

    def _boom(*_a: Any, **_kw: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            return (frozenset(("coordinator:enricher",)), None)
        raise OSError("test: roster source vanished between resolves")

    monkeypatch.setattr(_helpers, "resolve_roster", _boom)

    cause: Optional[str] = guard._confinement_cause(
        "invented-type-nobody-enumerated", _Policy(None)
    )
    assert cause == "unenumerated"
    assert len(calls) == 2, (
        "the second resolve is the cost this file's module docstring cites as "
        "the reason the two ladders are not collapsed — if it is gone, the "
        "rejection rationale is stale and should be re-read"
    )
