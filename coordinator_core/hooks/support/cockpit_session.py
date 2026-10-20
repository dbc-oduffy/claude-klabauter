"""Single decider for "this Claude session was launched by Cockpit".

Cockpit sets COORDINATOR_COCKPIT=1 in the env of every Claude process it launches; hooks
inherit it. Read per call: a test swaps os.environ in-process, so an import-time cache is wrong.

Ported from coordinator-content-repo `coordinator/hooks/scripts/_cockpit_session.py`.
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

SIGNAL_ENV = "COORDINATOR_COCKPIT"
_TRUTHY = frozenset({"1", "true"})


def under_cockpit(environ: Optional[Mapping[str, str]] = None) -> bool:
    """True iff the env (default os.environ) carries SIGNAL_ENV as "1" or "true"
    (case-insensitive, surrounding whitespace ignored)."""
    env = os.environ if environ is None else environ
    return env.get(SIGNAL_ENV, "").strip().lower() in _TRUTHY
