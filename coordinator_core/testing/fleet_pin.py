"""Pin the environment a guard suite was written against.

`coordinator_core.environment` lets a guard stand down where its premise is
false, and that fires on a managed remote container. Guard suites assert guard
LOGIC — given this command, this anchor, this target, does the rule fire and
what does it say — which is a different layer from "does the rule apply on this
host". Run unpinned on a cloud box, 51 of them failed: not because the guards
broke, but because the environment answered honestly and the suites never said
which environment they meant. An assertion whose outcome depends on the venue is
not an assertion.

Import the fixture into a package `conftest.py`:

    from coordinator_core.testing.fleet_pin import assume_a_fleet_machine  # noqa: F401

A test wanting the other environment deletes BOTH capability vars in its own
function-scoped fixture (which runs after this autouse one) and sets the venue
it means — both, because `peer_ems_reachable` is derived from `fleet_present`
and clearing one infers the other straight back into existence.

Do NOT widen this to a global conftest: it would silence the capability layer
inside its own tests, where the next regression in it would then be invisible.
Stand-down behaviour has its own suites, which override this pin:
`bash_guards/tests/test_bump_foreign_repo_write_stand_down.py`,
`ops/fleet/tests/test_memo_send_no_reader_gate.py`, and
`coordinator_core/tests/test_environment_capabilities.py`.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def assume_a_fleet_machine(monkeypatch):
    monkeypatch.setenv("COORDINATOR_CAP_FLEET_PRESENT", "1")
    monkeypatch.setenv("COORDINATOR_CAP_PEER_EMS_REACHABLE", "1")
    yield
