"""fleet_live record contract: field set, timed_out row, normalization."""

import json
import os

import pytest

from coordinator_core.group_em import box_registry
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
    assert fl.STATES == ("ok", "timed_out", "unreachable", "error", "op_missing", "busy")
    for f in ("alias", "os", "cpu_pct", "gpu_util_pct", "jobs", "sampled_at", "last_seen"):
        assert f in fl.FIELDS


def test_box_alias_derivation():
    assert fl.box_alias({"alias": " Gipsy "}) == "gipsy"
    ssh = lambda h: {"transport": {"kind": "ssh", "host": h}}  # noqa: E731
    assert fl.box_alias(ssh("Mini.local")) == "mini"
    assert fl.box_alias(ssh("user@Machine-b.lan")) == "machine-b"
    assert fl.box_alias(ssh("10.0.0.5"), local_hostname="MACHINE-A.corp") == "machine-a"
    assert fl.box_alias({"box_id": "x"}, local_hostname="Machine-a") == "machine-a"
    assert fl.box_alias({}, local_hostname="") == "local"


def test_timed_out_row_carries_identity_and_last_seen():
    lk = dict(LAST, alias="gipsy", os="windows", last_seen="2026-11-22T00:00:00Z")
    row = fl.timed_out_row("b", lk, state="op_missing")
    assert row["state"] == "op_missing"
    assert row["alias"] == "gipsy" and row["os"] == "windows" and row["last_seen"] == "2026-11-22T00:00:00Z"
    for f in fl.USE_FIELDS + ("sampled_at",):
        assert row[f] is None


def test_last_known_round_trip_and_unreadable(tmp_path):
    path = tmp_path / "group-em" / "fleet-capacity-cache.json"
    assert fl.read_last_known(path) == {}
    ok = fl.normalize_row("b", dict(LAST, state="ok", alias="b", os="linux", sampled_at="T1", cpu_pct=9))
    assert ok["last_seen"] == "T1"
    rec = fl.maxima_record(ok)
    assert rec["gpus"][0]["vram_used_mib"] is None and "cpu_pct" not in rec and rec["last_seen"] == "T1"
    fl.write_last_known({"b": rec}, path)
    assert fl.read_last_known(path) == {"b": rec}
    path.write_text("{not json", "utf-8")
    assert fl.read_last_known(path) == {}
    path.write_text("[1]", "utf-8")
    assert fl.read_last_known(path) == {}
    path.write_text('{"a": 1, "b": {"cores": 2}}', "utf-8")
    assert fl.read_last_known(path) == {"b": {"cores": 2}}


def test_last_known_path_under_settings_home(tmp_path):
    p = fl.last_known_path(tmp_path)
    assert p == tmp_path / "group-em" / "fleet-capacity-cache.json"


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


def _fresh(box, **over):
    return dict(LAST, cores=8, alias=box, os="linux", cpu_pct=10, ram_free_mb=2048, vram_used_mib=900,
                gpu_util_pct=7, sessions=2, jobs=[{"a": 1}], sampled_at=1000.0, **over)


def _recs(*ids):
    return [{"box_id": i, "transport": {"kind": "ssh", "host": f"{i}.lan"}} for i in ids]


def _gather(invoke, cache, ids=("far",), **kw):
    return fl.gather(_recs("me", *ids), timeout=kw.pop("timeout", 1.0), invoke=invoke, cache_path=cache,
                     local_sample=lambda: _fresh("me"), local_id="me", now=1000.0, **kw)


def _by(rows):
    return {r["box"]: r for r in rows}


def test_gather_ok_writes_cache_and_local_is_in_process(tmp_path):
    cache = tmp_path / "c.json"
    calls = []

    def invoke(box, op, params, timeout):
        calls.append((box["box_id"], op, params))
        return {"ok": True, "reply": {"row": _fresh("far")}}

    rows = _by(_gather(invoke, cache))
    assert calls == [("far", "groupem.capacity_local", {})]
    assert rows["me"]["state"] == rows["far"]["state"] == "ok"
    assert set(fl.read_last_known(cache)) == {"me", "far"}


def test_gather_timeout_returns_fast_and_keeps_last_known(tmp_path):
    import time

    cache = tmp_path / "c.json"
    fl.write_last_known({"far": fl.maxima_record(fl.normalize_row("far", _fresh("far", state="ok")))}, cache)

    def invoke(box, op, params, timeout):
        time.sleep(3)
        return {"ok": True, "reply": {"row": _fresh("far")}}

    t0 = time.monotonic()
    rows = _by(_gather(invoke, cache, timeout=0.4))
    assert time.monotonic() - t0 < 2.5  # under the 3s hang: proves the call did not wait for it
    far = rows["far"]
    assert far["state"] == "timed_out" and far["cores"] == 8 and far["last_seen"] == 1000.0
    assert far["cpu_pct"] is None and rows["me"]["state"] == "ok"


def test_gather_failure_reason_mapping(tmp_path):
    cases = [
        ({"ok": False, "state": "unknown", "reason": "ssh_timeout"}, "timed_out"),
        ({"ok": False, "state": "unknown", "reason": "remote_error"}, "op_missing"),
        ({"ok": False, "state": "unknown", "reason": "ssh_exit_1",
          "stderr": "Method not found: 'groupem.capacity_local'"}, "op_missing"),
        ({"ok": False, "state": "unknown", "reason": "box_busy"}, "busy"),
        ({"ok": False, "state": "unknown", "reason": "rate_limited"}, "busy"),
        ({"ok": False, "state": "unknown", "reason": "box_lock_unavailable"}, "busy"),
        ({"ok": False, "state": "unknown", "reason": "ssh_exit_255"}, "unreachable"),
        ({"ok": True, "reply": {"error": "boom"}}, "error"),
    ]
    for reply, state in cases:
        row =_by(_gather(lambda b, o, p, timeout, r=reply: r, tmp_path / "c.json"))["far"]
        assert row["state"] == state, reply
        assert row["maxima_source"] is None and row["reason"]


def test_gather_ok_then_unreachable_carries_maxima_and_flags_power_on(tmp_path):
    cache = tmp_path / "c.json"
    _gather(lambda b, o, p, timeout: {"ok": True, "reply": {"row": _fresh("far")}}, cache)
    down = {"ok": False, "state": "unknown", "reason": "ssh_exit_255"}
    rows = _gather(lambda b, o, p, timeout: down, cache)
    far = _by(rows)["far"]
    assert far["state"] == "unreachable" and far["cores"] == 8 and far["last_seen"] == 1000.0
    assert far["maxima_source"] == "last_known"
    assert fl.read_last_known(cache)["far"]["cores"] == 8
    items = fl.power_on_items(rows)
    assert len(items) == 1 and "far" in items[0] and "power it on?" in items[0]


def test_busy_and_op_missing_get_no_power_on_item(tmp_path):
    for reason in ("box_busy", "remote_error"):
        rows = _gather(lambda b, o, p, timeout, r=reason: {"ok": False, "reason": r}, tmp_path / "c.json")
        assert fl.power_on_items(rows) == []


def test_render_lines_and_never_drops_a_box(tmp_path):
    cache = tmp_path / "c.json"
    _gather(lambda b, o, p, timeout: {"ok": True, "reply": {"row": _fresh("far")}}, cache)
    rows = _gather(lambda b, o, p, timeout: {"ok": False, "reason": "ssh_exit_255"}, cache)
    text = fl.render(rows, now=1060.0)
    lines = text.splitlines()
    assert lines[0] == "FLEET CAPACITY"
    body = [ln for ln in lines[1:] if not ln.startswith("power it on?")]
    assert len(body) == 2
    far = next(ln for ln in body if ln.startswith("far"))
    assert "unknown (ssh_exit_255)" in far and "UNREACHABLE" in far and "MAX 8c" in far
    assert "last seen 1m ago" in far
    me = next(ln for ln in body if ln.startswith("me"))
    assert "NOW cpu 10%" in me and "sess 2" in me and "jobs 1" in me and me.endswith("1m ago")
    assert sum(ln.startswith("power it on?") for ln in lines) == 1


def test_normalize_row():
    row = fl.normalize_row("b", {"state": "ok", "cores": 4, "extra": 1})
    assert set(row) == set(fl.FIELDS) and row["cores"] == 4 and row["box"] == "b"
    assert fl.normalize_row("b", {"state": "weird"})["state"] == "ok"
    assert fl.normalize_row("b", None)["state"] == "error"


def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    box_registry.ensure_identity(home)
    return home


def _reg(home, box_id, root, host, at):
    box_registry.register(root, {}, directory=home / "state" / "group-em", box_id=box_id,
                          transport={"kind": "ssh", "host": host})
    path = home / "state" / "group-em" / "boxes" / f"{box_id}.json"
    rec = json.loads(path.read_text("utf-8"))
    rec["registered_at"] = at
    path.write_text(json.dumps(rec), "utf-8")


@pytest.mark.skipif(os.name != "nt", reason="a POSIX root is absolute-and-resolvable only off Windows; the collision needs a drive")
def test_far_posix_root_is_not_taken_for_this_box(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    local = box_registry.local_box_id(home)
    _reg(home, "machine-a", str(home), "u@machine-a.local", 5)
    _reg(home, "Mac-mini", "/" + "/".join(home.resolve().parts[1:]), "u@Mac-mini.local", 4)
    boxes = fl.registered_boxes(home)
    assert sorted(b["box_id"] for b in boxes) == sorted([local, "Mac-mini"])
    assert [b["registered_as"] for b in boxes if b["box_id"] == local] == ["machine-a"]


def test_alias_rows_of_one_host_collapse_to_the_freshest(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    _reg(home, "gipsy", str(tmp_path / "a"), "u@gipsy.lan", 1)
    _reg(home, "gipsy-windows", str(tmp_path / "b"), "u@gipsy.lan", 9)
    far = [b for b in fl.registered_boxes(home) if b["box_id"] != box_registry.local_box_id(home)]
    assert [b["box_id"] for b in far] == ["gipsy-windows"] and far[0]["aliases"] == ["gipsy"]


def test_gather_on_the_raw_registry_samples_the_local_box_once(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    local = box_registry.local_box_id(home)
    _reg(home, "machine-a", str(home), "u@machine-a.local", 5)
    samples = []
    rows = fl.gather(timeout=1.0, invoke=lambda *a, **k: {"ok": False}, cache_path=tmp_path / "c" / "c.json",
                     local_sample=lambda: samples.append(1) or _fresh(local), now=1000.0)
    assert [r["box"] for r in rows] == [local] and len(samples) == 1


def _specs(home, **data):
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "box-specs.json").write_text(json.dumps(data), "utf-8")


def test_known_boxes_adds_each_box_specs_peer_once_and_folds_the_local_box(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    local = box_registry.local_box_id(home)
    _reg(home, "machine-a", str(home), "u@machine-a.local", 5)
    _reg(home, "Mac-mini", "/far/root", "u@Mac-mini.local", 4)
    _specs(home, peers=["Machine-b", "mac-mini.local", "gipsy-windows", "Machine-a", "Machine-b"],
           boxes={"gipsy": {}})
    boxes = fl.known_boxes(home)
    ids = [b["box_id"] for b in boxes]
    assert sorted(ids) == sorted([local, "Mac-mini", "Machine-b", "gipsy-windows"])
    assert len(set(ids)) == len(ids)
    assert {b["box_id"]: b.get("roster_only") for b in boxes if b.get("roster_only")} == {
        "Machine-b": True, "gipsy-windows": True}


def test_box_specs_roster_falls_back_to_boxes_keys_and_missing_file_is_empty(tmp_path):
    assert fl.box_specs_roster(tmp_path) == []
    _specs(tmp_path, boxes={"a": {}, "b": {}})
    assert fl.box_specs_roster(tmp_path) == ["a", "b"]
    (tmp_path / "state" / "box-specs.json").write_text("{", "utf-8")
    assert fl.box_specs_roster(tmp_path) == []


def test_a_roster_box_with_no_route_is_an_unreachable_row_not_a_dropped_one(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    local = box_registry.local_box_id(home)
    _specs(home, peers=["Machine-b"])
    calls = []
    rows = fl.gather(fl.known_boxes(home), timeout=1.0, invoke=lambda *a, **k: calls.append(a) or {"ok": True},
                     cache_path=tmp_path / "c" / "c.json", local_sample=lambda: _fresh(local), local_id=local,
                     now=1000.0)
    by = _by(rows)
    assert sorted(by) == sorted([local, "Machine-b"]) and calls == []
    assert by["Machine-b"]["state"] == "unreachable" and by["Machine-b"]["alias"] == "machine-b"
    assert by[local]["state"] == "ok"
    assert fl.power_on_items(rows) == ["power it on? Machine-b is UNREACHABLE (last seen never)"]
