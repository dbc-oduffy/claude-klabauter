"""Pins navi-singleton's occurrence-aware poke-check: a ledger entry suppresses only the stall
occurrence it was written for, never a later one."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1]
_ROOT = _BIN.parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from coordinator_core.group_em import atomic_record as ar  # noqa: E402

_spec = importlib.util.spec_from_file_location("navi_singleton_under_test", _BIN / "navi-singleton.py")
navi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(navi)

SID, PEER = "sid-self", "peer-a"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path


def _poked_at(state_dir) -> datetime:
    entry = navi.read_ledger(ar, state_dir)["entries"][navi._ledger_key(SID, PEER)]
    return navi._parse_iso(entry["poked_at"])


def _check(state_dir, start: datetime, age_min: float = 20.0) -> bool:
    return navi.poke_check(
        ar, SID, PEER,
        as_of=_iso(start + timedelta(minutes=age_min)),
        content_age_minutes=age_min,
        directory=state_dir,
    ).record["already_poked"]


def test_unmarked_peer_reads_not_poked(state_dir):
    assert _check(state_dir, datetime.now(timezone.utc)) is False


def test_same_occurrence_reads_poked(state_dir):
    navi.poke_mark(ar, SID, PEER, directory=state_dir)
    assert _check(state_dir, _poked_at(state_dir) - timedelta(minutes=20)) is True


def test_start_inside_tolerance_reads_poked(state_dir):
    navi.poke_mark(ar, SID, PEER, directory=state_dir)
    start = _poked_at(state_dir) + timedelta(seconds=navi.OCCURRENCE_TOLERANCE_SECONDS - 5)
    assert _check(state_dir, start) is True


def test_start_after_tolerance_reads_not_poked(state_dir):
    navi.poke_mark(ar, SID, PEER, directory=state_dir)
    start = _poked_at(state_dir) + timedelta(seconds=navi.OCCURRENCE_TOLERANCE_SECONDS + 5)
    assert _check(state_dir, start) is False


def test_unparseable_poked_at_reads_poked(state_dir):
    ar.write_json_atomic(
        state_dir / "poke-ledger.json",
        {"version": 1, "entries": {navi._ledger_key(SID, PEER): {"poked_at": "garbage"}}},
    )
    assert _check(state_dir, datetime.now(timezone.utc)) is True


def test_unparseable_as_of_raises(state_dir):
    navi.poke_mark(ar, SID, PEER, directory=state_dir)
    with pytest.raises(ValueError):
        navi.poke_check(ar, SID, PEER, as_of="nope", content_age_minutes=1.0, directory=state_dir)


@pytest.mark.parametrize("flags", [[], ["--as-of", "2026-09-30T10:00:00Z"], ["--content-age", "3"]])
def test_cli_refuses_missing_flags(flags):
    with pytest.raises(SystemExit):
        navi.main(["poke-check", "--session-id", SID, "--peer", PEER, *flags])
