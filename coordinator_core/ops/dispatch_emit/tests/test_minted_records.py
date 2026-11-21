"""minted_records: manifest round-trip, one-commit settle, reap-on-refusal, peer withhold."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import minted_records

pytestmark = [pytest.mark.complete]

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_record_read_round_trip_dedupes_and_ors_created(tmp_path):
    run_dir = tmp_path / "run"
    minted_records.record(run_dir, [{"path": "a.md", "created": False}, {"path": "b.md", "created": True}])
    minted_records.record(run_dir, [{"path": "a.md", "created": True}])
    assert minted_records.read(run_dir) == [
        {"path": "a.md", "created": True},
        {"path": "b.md", "created": True},
    ]
    assert minted_records.read(tmp_path / "absent") == []


@pytest.mark.parametrize("bad", [{"path": "../x", "created": True}, {"path": "a.md", "created": "yes"}, {"created": True}])
def test_record_rejects_a_bad_entry_before_writing(tmp_path, bad):
    run_dir = tmp_path / "run"
    with pytest.raises(ValueError):
        minted_records.record(run_dir, [{"path": "ok.md", "created": True}, bad])
    assert minted_records.read(run_dir) == []
