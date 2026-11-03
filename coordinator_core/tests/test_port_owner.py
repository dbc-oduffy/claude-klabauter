"""port_owner: pid 0 / NONE / missing is owner-unknown and raises; a real pid resolves."""

from __future__ import annotations

import pytest

from coordinator_core.port_owner import (
    OwnerUnknown,
    owner_from_child_record,
    owner_from_pidfile,
    parse_owner_pid,
)


@pytest.mark.parametrize("raw", [0, "0", "NONE", "none", "", "  ", None, -3, "abc", b"0", True])
def test_unknown_owner_raises(raw):
    with pytest.raises(OwnerUnknown):
        parse_owner_pid(raw)


@pytest.mark.parametrize("raw,expected", [(1234, 1234), ("42\n", 42), (b"7", 7)])
def test_real_pid_resolves(raw, expected):
    assert parse_owner_pid(raw) == expected


def test_pidfile_resolves_and_fails_loud(tmp_path):
    good = tmp_path / "a.pid"
    good.write_text("555\n", encoding="utf-8")
    assert owner_from_pidfile(good) == 555
    zero = tmp_path / "z.pid"
    zero.write_text("0", encoding="utf-8")
    with pytest.raises(OwnerUnknown):
        owner_from_pidfile(zero)
    with pytest.raises(OwnerUnknown):
        owner_from_pidfile(tmp_path / "missing.pid")


def test_child_record():
    assert owner_from_child_record({"pid": 9}) == 9
    for bad in (None, {}, {"pid": 0}, {"pid": "NONE"}):
        with pytest.raises(OwnerUnknown):
            owner_from_child_record(bad)
