"""fleet_live record contract: field set, timed_out row, normalization."""

from coordinator_core.group_em import fleet_live as fl

LAST = {
    "cores": 24,
    "cpu_model": "X",
    "ram_total_mb": 98304,
    "commit_limit_mb": 120000,
    "gpus": [{"index": 0, "name": "G", "vram_total_mib": 24576, "vram_used_mib": 900, "util_pct": 7}],
    "disks": [{"mount": "C:", "total_gb": 1000, "free_gb": 50, "floor_gb": 20}],
    "load5": 3.0,
    "commit_used_mb": 5000,
}


def test_fields_cover_brief_axes_and_commit():
    for f in ("box", "state", "cores", "cpu_model", "ram_total_mb", "commit_limit_mb", "gpus", "disks",
              "load5", "ram_free_mb", "commit_used_mb", "vram_used_mib", "sessions", "gpu_slot_holder",
              "editor_slot_holder", "top_cpu", "top_ram", "maxima_source"):
        assert f in fl.FIELDS
    assert len(set(fl.FIELDS)) == len(fl.FIELDS)
    assert fl.STATES == ("ok", "timed_out", "unreachable", "error")


def test_timed_out_row_carries_maxima_and_nulls_use():
    row = fl.timed_out_row("gipsy", LAST, maxima_source="record:gipsy")
    assert set(row) == set(fl.FIELDS)
    assert row["state"] == "timed_out" and row["box"] == "gipsy"
    assert row["cores"] == 24 and row["ram_total_mb"] == 98304 and row["commit_limit_mb"] == 120000
    assert row["maxima_source"] == "record:gipsy"
    for f in fl.USE_FIELDS:
        assert row[f] is None
    assert row["gpus"][0]["vram_total_mib"] == 24576 and row["gpus"][0]["vram_used_mib"] is None
    assert row["disks"][0]["total_gb"] == 1000 and row["disks"][0]["free_gb"] is None
    assert LAST["gpus"][0]["vram_used_mib"] == 900


def test_timed_out_row_without_last_known_has_null_source():
    for lk in (None, {}, "junk"):
        row = fl.timed_out_row("b", lk)
        assert row["state"] == "timed_out" and row["maxima_source"] is None
        assert all(row[f] is None for f in fl.MAXIMA_FIELDS)


def test_unreachable_state_and_unknown_state_fallback():
    assert fl.timed_out_row("b", LAST, state="unreachable")["state"] == "unreachable"
    assert fl.timed_out_row("b", LAST, state="bogus")["state"] == "timed_out"


def test_normalize_row():
    row = fl.normalize_row("b", {"state": "ok", "cores": 4, "extra": 1})
    assert set(row) == set(fl.FIELDS) and row["cores"] == 4 and row["box"] == "b"
    assert fl.normalize_row("b", {"state": "weird"})["state"] == "ok"
    assert fl.normalize_row("b", None)["state"] == "error"
