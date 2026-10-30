"""Per-guard deny/allow fixtures for the fleet-mode independence harness.

`FIXTURES` (in each `*_cases` module) maps a guard's dotted module name to a
`GuardFixtures`: `deny` cases that must make `module.check(payload)` deny, and
`allow` cases that must not. A `Case` builds on-disk state only under the given
`tmp_path`, sets env only through the given `monkeypatch`, and returns the
payload handed to `check`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, TypedDict

import pytest

Case = Callable[[Path, pytest.MonkeyPatch], dict]


class GuardFixtures(TypedDict):
    deny: list[Case]
    allow: list[Case]


def is_deny(verdict: Any) -> bool:
    """True for the two returned shapes that deny: a `permissionDecision: "deny"`
    (or Stop `decision: "block"`) envelope dict, and an `AlternativeRecord` whose
    `verdict_kind` is `"deny"`. `None`, advisory and rewrite records, and any
    other value are not a deny. Total over any input.
    """
    from coordinator_core.guard_messages.alternative_record import AlternativeRecord
    from coordinator_core.machine_profile import is_deny_envelope

    if isinstance(verdict, AlternativeRecord):
        return verdict.verdict_kind == "deny"
    return is_deny_envelope(verdict)


# The case modules import `GuardFixtures` from this package, so they load after it is defined.
from coordinator_core.tests.fleet_mode_independence_fixtures import (  # noqa: E402
    bash_guard_cases,
    write_guard_cases,
)

ALL_FIXTURES: dict[str, GuardFixtures] = {
    **write_guard_cases.FIXTURES,
    **bash_guard_cases.FIXTURES,
}
