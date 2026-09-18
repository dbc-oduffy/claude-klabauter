"""This package asserts guard logic against a fleet machine; the pin says so.

Needed from the moment `bump_out_of_repo_tool_write` gained the shared
environment stand-down (`bash_guards._write_bump_stand_down`): without the
pin, every test here that asserts a DENY passes or fails according to the
venue the suite happens to run in — green on a fleet workstation, red on a
managed remote container where the guard now honestly declines. An assertion
whose outcome depends on the host is not an assertion.

One copy of the fixture and its argument lives in
`coordinator_core/testing/fleet_pin.py`; read that module before widening or
narrowing this. The stand-down's own suite
(`test_bump_out_of_repo_tool_write_stand_down.py`) overrides this pin in its
own function-scoped fixture, which runs after this autouse one.
"""

from coordinator_core.testing.fleet_pin import assume_a_fleet_machine  # noqa: F401
