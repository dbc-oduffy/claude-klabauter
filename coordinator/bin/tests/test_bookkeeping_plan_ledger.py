"""bookkeeping-plan-ledger.py lists terminal-status plans per window, ranked by sizing tshirt.

Every test drives the script as a subprocess against a tmp_path git repo, the production
entry point; dates are pinned through GIT_COMMITTER_DATE so no test depends on the clock
except the two default-argument tests, which commit "now".
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "bookkeeping-plan-ledger.py"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LONG_AGO = "2026-01-05T10:00:00+00:00"


def test_terminal_set_covers_the_engine_terminal_statuses() -> None:
    spec = importlib.util.spec_from_file_location("bookkeeping_plan_ledger", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from coordinator_core.lifecycle_constants import PLAN_TERMINAL_STATUS

    assert PLAN_TERMINAL_STATUS <= mod.TERMINAL_STATUSES
    assert {"implemented", "landed"} <= mod.TERMINAL_STATUSES
