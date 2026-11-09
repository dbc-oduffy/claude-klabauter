import json
from datetime import datetime, timedelta, timezone

import pytest

from coordinator_core.ops.dispatch_emit import run_scratch as rs

RUN = "run-abc"
NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)


def test_record_paths_appends_deduped_normalised(tmp_path):
    rs.record_paths(tmp_path, RUN, ["scratch\\land-x", "scratch/probe-y/"])
    rs.record_paths(tmp_path, RUN, ["scratch/land-x"])
    ledger = rs.read_ledger(tmp_path, RUN)
    assert ledger["paths"] == ["scratch/land-x", "scratch/probe-y"]
    assert rs.ledger_path(tmp_path, RUN).name == rs.LEDGER_FILE
    assert str(tmp_path).replace("\\", "/") not in json.dumps(ledger)


@pytest.mark.parametrize(
    "bad",
    [
        "/abs/scratch/x",
        "C:/scratch/x",  # abs-path-ok: refusal fixture
        "scratch/../x",
        "x/y",
        "scratch",
        "scratch/",
        "scratch/.deleted/a",
        "scratch/scratch-hold/a",
        "scratch/stage-readers/a",
        "",
    ],
)
def test_record_paths_refuses_and_writes_nothing(tmp_path, bad):
    with pytest.raises(ValueError):
        rs.record_paths(tmp_path, RUN, ["scratch/ok", bad])
    assert rs.read_ledger(tmp_path, RUN)["paths"] == []


SHARE = ".coordinator-local/subagent-share"


@pytest.mark.parametrize("name", ["p-1.review-prep.md", "p-1.review-prep.lock"])
def test_review_prep_sidecar_and_lock_admitted(tmp_path, name):
    rel = f"{SHARE}/sess-1/{name}"
    assert rs.record_paths(tmp_path, RUN, [rel]) == [rel]
    assert rs.read_ledger(tmp_path, RUN)["paths"] == [rel]


@pytest.mark.parametrize(
    "bad",
    [
        ".coordinator-local/state/x.review-prep.md",
        f"{SHARE}/sess-1/notes.md",
        f"{SHARE}/sess-1/p.review-prep-ptr.json",
        f"{SHARE}/sess-1/.review-prep.md",
        f"{SHARE}/p.review-prep.md",
        f"{SHARE}/a/b/p.review-prep.md",
        f"{SHARE}/../x/p.review-prep.md",
        ".coordinator-local/p.review-prep.md",
    ],
)
def test_other_coordinator_local_paths_still_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        rs.record_paths(tmp_path, RUN, [bad])
    assert rs.read_ledger(tmp_path, RUN)["paths"] == []


def test_read_ledger_empty_when_absent(tmp_path):
    assert rs.read_ledger(tmp_path, RUN) == {"paths": [], "keeps": []}


def test_declare_keep_records_ledger_and_index(tmp_path):
    entry = rs.declare_keep(tmp_path, RUN, "scratch/probe-keep", "repro for bug", 2, now=NOW)
    assert entry["kept_at"] == "2026-11-09T12:00:00Z"
    ledger = rs.read_ledger(tmp_path, RUN)
    assert ledger["keeps"] == [entry]
    assert "scratch/probe-keep" in ledger["paths"]
    index = rs.read_keep_index(tmp_path)
    assert index["scratch/probe-keep"] == {
        "run_id": RUN,
        "purpose": "repro for bug",
        "expires_at": "2026-11-09T14:00:00Z",
    }


def test_declare_keep_upserts(tmp_path):
    rs.declare_keep(tmp_path, RUN, "scratch/k", "one", 1, now=NOW)
    rs.declare_keep(tmp_path, RUN, "scratch/k", "two", 3, now=NOW)
    assert [k["purpose"] for k in rs.read_ledger(tmp_path, RUN)["keeps"]] == ["two"]
    assert rs.read_keep_index(tmp_path)["scratch/k"]["expires_at"] == "2026-11-09T15:00:00Z"


@pytest.mark.parametrize(
    "purpose,ttl",
    [("", 1), ("   ", 1), (None, 1), ("p", 0), ("p", -1), ("p", True), ("p", None), ("p", rs.MAX_KEEP_TTL_HOURS + 1)],
)
def test_declare_keep_refusals_write_nothing(tmp_path, purpose, ttl):
    with pytest.raises(ValueError):
        rs.declare_keep(tmp_path, RUN, "scratch/k", purpose, ttl, now=NOW)
    assert rs.read_ledger(tmp_path, RUN) == {"paths": [], "keeps": []}
    assert rs.read_keep_index(tmp_path) == {}


def test_declare_keep_accepts_cap_and_refuses_bad_path(tmp_path):
    rs.declare_keep(tmp_path, RUN, "scratch/k", "p", rs.MAX_KEEP_TTL_HOURS, now=NOW)
    with pytest.raises(ValueError):
        rs.declare_keep(tmp_path, RUN, "scratch/stage-readers/k", "p", 1, now=NOW)


def test_keep_index_expired_boundary(tmp_path):
    rs.declare_keep(tmp_path, RUN, "scratch/short", "p", 1, now=NOW)
    rs.declare_keep(tmp_path, "run-two", "scratch/long", "p", 10, now=NOW)
    assert rs.keep_index_expired(tmp_path, NOW + timedelta(minutes=59)) == []
    assert rs.keep_index_expired(tmp_path, NOW + timedelta(hours=1)) == ["scratch/short"]
    assert rs.keep_index_expired(tmp_path, NOW + timedelta(hours=11)) == ["scratch/long", "scratch/short"]


def test_keep_index_expired_empty_and_corrupt(tmp_path):
    assert rs.keep_index_expired(tmp_path, NOW) == []
    (tmp_path / "scratch").mkdir()
    (tmp_path / rs.KEEP_INDEX_REL).write_text("not json", encoding="utf-8")
    assert rs.keep_index_expired(tmp_path, NOW) == []


def test_unsafe_run_id_refused(tmp_path):
    with pytest.raises(ValueError):
        rs.record_paths(tmp_path, "../evil", ["scratch/x"])
