"""Leaf: mode names and value sets shared by consumers that never resolve a mode.

Imports nothing, so a module that only needs a constant (the warm door's
forwarding set, exit-criterion validation) does not reach the fleet record
through ``mode_resolution``.
"""

from typing import FrozenSet

COORDINATOR_JOB_MODE = "COORDINATOR_JOB_MODE"

JOB_MODE_VALUES: FrozenSet[str] = frozenset({"blitz", "cron", "interactive"})

INTERACTION_MODES = ("hands-on", "pm", "ceo")
