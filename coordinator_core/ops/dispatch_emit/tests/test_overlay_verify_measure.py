"""overlay_verify_measure records child process time and spawn count per verify leg (the bounded figures),
beside wall_ms and files_written recorded only to tell IO wait apart, and `verify` enters the leg hook around
exactly its clone (cold mirror only), checkout, pytest and base-rerun legs."""

from __future__ import annotations


import pytest

from coordinator_core.ops.dispatch_emit import overlay_verify_measure as ovm
from pathlib import Path

pytestmark = [pytest.mark.complete]

_PASSING = "t/test_ok.py"
_FAILING = "t/test_bad.py"


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


def test_meter_with_no_child_records_zero_spawns_and_the_files_a_leg_reports() -> None:
    with ovm.LegMeter() as meter:
        with meter("idle"):
            pass
        with meter("write") as extra:
            extra["files_written"] = 7
    assert meter.records[0]["spawns"] == 0
    assert meter.records[1]["files_written"] == 7
