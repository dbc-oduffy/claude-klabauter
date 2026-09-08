"""This package asserts guard logic against a fleet machine; the pin says so.

One copy of the fixture and its argument lives in
`coordinator_core/testing/fleet_pin.py` — the two conftests that need it were
byte-identical, 52 lines each, 37 of them the same prose twice.
"""

from coordinator_core.testing.fleet_pin import assume_a_fleet_machine  # noqa: F401
