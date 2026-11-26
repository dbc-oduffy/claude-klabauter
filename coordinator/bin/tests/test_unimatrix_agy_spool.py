"""agy_spool: spool_append line shape, bound and lock; write_receipt/read_receipt round trip and bound."""
import importlib.util
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
CONV = "conv-abc123"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sp():
    return _load("agy_spool_under_test", BIN / "_unimatrix" / "agy_spool.py")


def test_spool_append_writes_one_line_and_returns_id(sp, tmp_path):
    msg_id = sp.spool_append(tmp_path, CONV, "hello", "sender-1")
    lines = (tmp_path / f"{CONV}.queue.jsonl").read_text().splitlines()
    assert [json.loads(x) for x in lines] == [{"id": msg_id, "text": "hello", "from": "sender-1"}]
    assert len(msg_id) == 32 and int(msg_id, 16) >= 0


def test_spool_append_keeps_order_and_unique_ids(sp, tmp_path):
    ids = [sp.spool_append(tmp_path, CONV, f"m{i}", "s") for i in range(3)]
    rows = [json.loads(x) for x in (tmp_path / f"{CONV}.queue.jsonl").read_text().splitlines()]
    assert [r["id"] for r in rows] == ids and len(set(ids)) == 3
    assert [r["text"] for r in rows] == ["m0", "m1", "m2"]


def test_spool_append_refuses_when_full_and_writes_nothing(sp, tmp_path):
    cap = sp._leg().SPOOL_MAX_BYTES
    spool = tmp_path / f"{CONV}.queue.jsonl"
    spool.write_bytes(b"x" * cap)
    with pytest.raises(OSError, match="spool full"):
        sp.spool_append(tmp_path, CONV, "late", "s")
    assert spool.stat().st_size == cap


def test_spool_append_just_under_cap_still_accepted(sp, tmp_path):
    spool = tmp_path / f"{CONV}.queue.jsonl"
    spool.write_bytes(b"x" * (sp._leg().SPOOL_MAX_BYTES - 4096))
    assert sp.spool_append(tmp_path, CONV, "fits", "s")


@pytest.mark.parametrize("bad", ["../evil", "a/b", "a\\b", "", "..", "x" * 129])
def test_unsafe_conv_is_refused(sp, tmp_path, bad):
    with pytest.raises(ValueError):
        sp.spool_append(tmp_path, bad, "t", "s")
    with pytest.raises(ValueError):
        sp.write_receipt(tmp_path, bad, "id", None)


def test_concurrent_appends_lose_nothing(sp, tmp_path):
    ids, errs = [], []

    def worker(n):
        try:
            ids.append(sp.spool_append(tmp_path, CONV, f"t{n}", "s"))
        except Exception as exc:  # noqa: BLE001
            errs.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = [json.loads(x) for x in (tmp_path / f"{CONV}.queue.jsonl").read_text().splitlines()]
    assert not errs and sorted(r["id"] for r in rows) == sorted(ids) and len(rows) == 12


def test_receipt_round_trip_and_absent(sp, tmp_path):
    assert sp.read_receipt(tmp_path, "nope") is None
    sp.write_receipt(tmp_path, CONV, "id1", "2026-11-26T14:00:00Z")
    assert sp.read_receipt(tmp_path, "id1") == {"id": "id1", "read_at": "2026-11-26T14:00:00Z"}
    assert sp.read_receipt(tmp_path, "id2") is None


def test_receipt_found_across_conversations(sp, tmp_path):
    sp.write_receipt(tmp_path, "conv-a", "ida")
    sp.write_receipt(tmp_path, "conv-b", "idb")
    assert sp.read_receipt(tmp_path, "idb")["id"] == "idb"
    assert sp.read_receipt(tmp_path, "ida")["id"] == "ida"


def test_receipt_default_stamp_is_iso_utc_now(sp, tmp_path):
    sp.write_receipt(tmp_path, CONV, "id1")
    stamp = sp.read_receipt(tmp_path, "id1")["read_at"]
    assert stamp.endswith("Z")
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 60


def test_receipt_datetime_is_normalised_to_utc(sp, tmp_path):
    aware = datetime(2026, 11, 26, 16, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    sp.write_receipt(tmp_path, CONV, "id1", aware)
    assert sp.read_receipt(tmp_path, "id1")["read_at"] == "2026-11-26T14:00:00Z"


def test_write_receipt_drops_rows_older_than_ttl_and_keeps_fresh(sp, tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(seconds=sp.RECEIPT_TTL_SECS + 600)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = (datetime.now(timezone.utc) - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sp.write_receipt(tmp_path, CONV, "old", old)
    sp.write_receipt(tmp_path, CONV, "fresh", fresh)
    sp.write_receipt(tmp_path, CONV, "new")
    ids = [json.loads(x)["id"] for x in (tmp_path / f"{CONV}.receipts.jsonl").read_text().splitlines()]
    assert ids == ["fresh", "new"]
    assert sp.read_receipt(tmp_path, "old") is None


def test_receipt_ttl_matches_hook_rung_ttl(sp):
    hook = _load("agy_hook_for_ttl", BIN / "unimatrix-agy-hook-pretool.py")
    assert sp.RECEIPT_TTL_SECS == hook.RUNG_TTL_SECS


def test_read_receipt_skips_garbled_lines(sp, tmp_path):
    (tmp_path / f"{CONV}.receipts.jsonl").write_text('not json\n{"id": "ok", "read_at": "2026-11-26T14:00:00Z"}\n')
    assert sp.read_receipt(tmp_path, "ok")["id"] == "ok"


def test_ensure_pending_record_writes_pinned_shape(sp, tmp_path):
    assert sp.ensure_pending_record(tmp_path, CONV, name="agy-session-x", cwd="/w") is True
    assert json.loads((tmp_path / f"{CONV}.json").read_text()) == {
        "conversation": CONV, "mode": "pending", "spooled": True, "name": "agy-session-x", "cwd": "/w"}
    assert list(tmp_path.glob("*.tmp.*")) == []


def test_ensure_pending_record_omits_absent_name_and_cwd(sp, tmp_path):
    sp.ensure_pending_record(tmp_path, CONV)
    assert json.loads((tmp_path / f"{CONV}.json").read_text()) == {
        "conversation": CONV, "mode": "pending", "spooled": True}


@pytest.mark.parametrize("existing", [{"mode": "live", "pid": 7}, {"mode": "pending", "pid": 9}])
def test_ensure_pending_record_never_overwrites(sp, tmp_path, existing):
    (tmp_path / f"{CONV}.json").write_text(json.dumps(existing))
    assert sp.ensure_pending_record(tmp_path, CONV, name="n") is False
    assert json.loads((tmp_path / f"{CONV}.json").read_text()) == existing


def test_ensure_pending_record_refuses_unsafe_conv(sp, tmp_path):
    with pytest.raises(ValueError):
        sp.ensure_pending_record(tmp_path, "../x")


def _adapter():
    import sys
    if str(BIN) not in sys.path:
        sys.path.insert(0, str(BIN))
    from _unimatrix.adapters import agy
    return agy


def test_spool_no_leg_writes_pending_record_and_spools(tmp_path):
    import asyncio
    agy = _adapter()
    (tmp_path / f"{CONV}.log").write_text("x")
    out = asyncio.run(agy.spool_no_leg(tmp_path / "nope.jsonl", "hi", CONV, tmp_path, "peer"))
    assert out["spooled"] is True
    assert json.loads((tmp_path / f"{CONV}.json").read_text())["mode"] == "pending"
    assert (tmp_path / f"{CONV}.queue.jsonl").exists()


def test_spool_no_leg_keeps_existing_live_record(tmp_path):
    import asyncio
    agy = _adapter()
    (tmp_path / f"{CONV}.log").write_text("x")
    (tmp_path / f"{CONV}.json").write_text(json.dumps({"mode": "live"}))
    asyncio.run(agy.spool_no_leg(tmp_path / "nope.jsonl", "hi", CONV, tmp_path))
    assert json.loads((tmp_path / f"{CONV}.json").read_text()) == {"mode": "live"}


def test_spool_no_leg_unknown_conversation_writes_no_record(tmp_path):
    import asyncio
    agy = _adapter()
    assert asyncio.run(agy.spool_no_leg(tmp_path / "nope.jsonl", "hi", CONV, tmp_path)) == agy.NO_LEG
    assert not (tmp_path / f"{CONV}.json").exists()
