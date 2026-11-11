import json

from coordinator_core.group_em import remote_roster as rr

NOW = 1000.0


def _write(tmp_path, box_id, **over):
    rec = {
        "box_id": box_id,
        "fetched_at": NOW - 5,
        "ok": True,
        "sessions": [
            {"session_id": "s1", "name": "n", "cwd": "c", "status": "idle", "address": "a"}
        ],
    }
    rec.update(over)
    p = rr.snapshot_path(tmp_path, box_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec), encoding="utf-8")
    return p


def _read(tmp_path, **kw):
    return rr.read_snapshot(tmp_path, now=NOW, local_id="box-me", **kw)


def test_fresh_snapshot_reachable(tmp_path):
    _write(tmp_path, "box-a")
    out = _read(tmp_path)
    assert out["box-a"]["reachable"] is True
    assert out["box-a"]["sessions"][0]["session_id"] == "s1"


def test_stale_snapshot_unreachable(tmp_path):
    _write(tmp_path, "box-a", fetched_at=NOW - rr.SNAPSHOT_TTL_S - 1)
    assert _read(tmp_path)["box-a"] == {"reachable": False, "sessions": []}


def test_ok_false_and_unreadable_unreachable(tmp_path):
    _write(tmp_path, "box-a", ok=False)
    p = _write(tmp_path, "box-b")
    p.write_text("{not json", encoding="utf-8")
    out = _read(tmp_path)
    assert out["box-a"]["reachable"] is False
    assert out["box-b"]["reachable"] is False


def test_local_box_skipped_and_missing_dir_empty(tmp_path):
    assert _read(tmp_path) == {}
    _write(tmp_path, "box-me")
    assert _read(tmp_path) == {}


def test_read_snapshot_never_spawns(tmp_path, monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    _write(tmp_path, "box-a")
    assert _read(tmp_path)["box-a"]["reachable"] is True


# ---- refresh ----

import subprocess  # noqa: E402

from coordinator_core.group_em import box_registry  # noqa: E402
from coordinator_core.group_em import box_transport  # noqa: E402
import pytest  # noqa: E402


_SSH_BOXES = [
    {"box_id": bid, "transport": {"kind": "ssh", "host": "h", "invoke": "/x/coordinator-invoke"}}
    for bid in ("box-s", "box-ssh")
]


@pytest.fixture(autouse=True)
def _ssh_registration(monkeypatch, tmp_path):
    """box_transport admits only boxes found in the box registry, and takes locks and rate tokens."""
    monkeypatch.setattr(box_registry, "lock_dir", lambda home=None: tmp_path / "box-locks")
    real_read = box_registry.read_boxes

    def read_boxes(directory=None):
        # Only box_transport's registration check passes no directory; every other caller keeps the real read.
        return real_read(directory) if directory is not None else [dict(b) for b in _SSH_BOXES]

    monkeypatch.setattr(box_registry, "read_boxes", read_boxes)
    box_transport._reset_limits()
    yield
    box_transport._reset_limits()


def _primary(tmp_path):
    d = tmp_path / "primary" / "state" / "group-em"
    d.mkdir(parents=True)
    return d


def _row(sid, **over):
    r = {"session_id": sid, "name": "n-" + sid, "cwd": "/c", "status": "idle", "address": "a", "pid": 9}
    r.update(over)
    return r


def _fs_box(tmp_path, directory, box_id="box-a", published=True, fetched_at=NOW - 5):
    root = tmp_path / box_id
    ident = root / "state" / "group-em" / "box.json"
    ident.parent.mkdir(parents=True, exist_ok=True)
    ident.write_text(json.dumps({"box_id": box_id}), encoding="utf-8")
    box_registry.register(root, {}, directory)
    if published:
        p = rr.snapshot_path(root / "state" / "group-em", box_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"box_id": box_id, "fetched_at": fetched_at, "ok": True, "sessions": [_row("s1")]}),
            encoding="utf-8",
        )
    return next(b for b in box_registry.read_boxes(directory) if b["box_id"] == box_id)


def test_refresh_fs_box_copies_published_roster(tmp_path):
    d = _primary(tmp_path)
    box = _fs_box(tmp_path, d)
    assert rr.refresh_box(d, box, now=NOW) is True
    out = rr.read_snapshot(d, now=NOW, local_id="box-me")
    assert out["box-a"]["reachable"] is True
    assert out["box-a"]["sessions"] == [
        {"session_id": "s1", "name": "n-s1", "cwd": "/c", "status": "idle", "address": "a"}
    ]


def test_fs_box_without_fresh_publish_is_unreachable_and_keeps_prior(tmp_path):
    d = _primary(tmp_path)
    box = _fs_box(tmp_path, d)
    assert rr.refresh_box(d, box, now=NOW)
    stale = _fs_box(tmp_path, d, fetched_at=NOW - 1000)
    assert rr.refresh_box(d, stale, now=NOW + 1) is False
    raw = json.loads(rr.snapshot_path(d, "box-a").read_text(encoding="utf-8"))
    assert raw["ok"] is False
    assert [s["session_id"] for s in raw["sessions"]] == ["s1"]
    assert rr.read_snapshot(d, now=NOW + 1, local_id="box-me")["box-a"]["reachable"] is False


def test_ssh_box_through_injected_runner(tmp_path):
    d = _primary(tmp_path)
    box = {"box_id": "box-s", "transport": {"kind": "ssh", "host": "h", "invoke": "/x/coordinator-invoke"}}
    calls = []

    def runner(argv, timeout):
        calls.append(argv)
        body = {"rows": [_row("r1"), _row("r2")]}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr="")

    assert rr.refresh_box(d, box, now=NOW, runner=runner) is True
    assert len(calls) == 1
    out = rr.read_snapshot(d, now=NOW, local_id="box-me")
    assert [s["session_id"] for s in out["box-s"]["sessions"]] == ["r1", "r2"]
    assert "pid" not in out["box-s"]["sessions"][0]


def test_failing_ssh_box_writes_ok_false_without_raising(tmp_path):
    d = _primary(tmp_path)
    box = {"box_id": "box-s", "transport": {"kind": "ssh", "host": "h", "invoke": "/x/coordinator-invoke"}}

    def good(argv, timeout):
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"rows": [_row("r1")]}), stderr="")

    def bad(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    assert rr.refresh_box(d, box, now=NOW, runner=good)
    assert rr.refresh_box(d, box, now=NOW + 1, runner=bad) is False
    raw = json.loads(rr.snapshot_path(d, "box-s").read_text(encoding="utf-8"))
    assert raw["ok"] is False
    assert [s["session_id"] for s in raw["sessions"]] == ["r1"]
    assert rr.read_snapshot(d, now=NOW + 1, local_id="box-me")["box-s"]["reachable"] is False


def test_refresh_box_dispatches_through_select(tmp_path, monkeypatch):
    from coordinator_core.group_em import node_transport

    d = _primary(tmp_path)
    seen = []

    class T:
        kind = "other"

        def list_sessions(self, box, run=None):
            return {"ok": True, "box_id": box["box_id"], "sessions": [_row("t1")]}

    def fake_select(box, directory=None, runner=None):
        seen.append((box["box_id"], directory))
        return T()

    monkeypatch.setattr(node_transport, "select", fake_select)
    assert rr.refresh_box(d, {"box_id": "box-t"}, now=NOW) is True
    assert seen == [("box-t", d)]
    assert [s["session_id"] for s in rr.read_snapshot(d, now=NOW, local_id="x")["box-t"]["sessions"]] == ["t1"]


def test_unknown_transport_kind_writes_ok_false(tmp_path):
    d = _primary(tmp_path)
    box = {"box_id": "box-u", "transport": {"kind": "carrier-pigeon"}}
    assert rr.refresh_box(d, box, now=NOW) is False
    assert json.loads(rr.snapshot_path(d, "box-u").read_text(encoding="utf-8"))["ok"] is False


def test_refresh_on_node_without_primary_role_fetches_every_peer(tmp_path):
    # A roaming node: its own registry holds an fs peer and an ssh peer; neither is a "primary".
    d = tmp_path / "roamer" / "state" / "group-em"
    fs = _fs_box(tmp_path, d, box_id="box-home")
    box_registry.register(
        tmp_path / "unreachable",
        {},
        d,
        box_id="box-ssh",
        transport={"kind": "ssh", "host": "h", "invoke": "/x/coordinator-invoke"},
    )

    def runner(argv, timeout):
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"rows": [_row("r1")]}), stderr="")

    res = rr.refresh(d, now=NOW, runner=runner, local_sessions=[_row("m1")], local_id="box-roamer")
    assert res == {"box-home": True, "box-ssh": True, "box-roamer": True}
    assert fs["box_id"] == "box-home"
    assert set(rr.read_snapshot(d, now=NOW, local_id="box-roamer")) == {"box-home", "box-ssh"}


def test_refresh_box_never_raises_on_malformed_box(tmp_path):
    assert rr.refresh_box(_primary(tmp_path), {}, now=NOW) is False


def test_publish_local_writes_fs_source(tmp_path):
    d = _primary(tmp_path)
    assert rr.publish_local(d, [_row("m1")], now=NOW, local_id="box-me") is True
    raw = json.loads(rr.snapshot_path(d, "box-me").read_text(encoding="utf-8"))
    assert raw["ok"] is True and raw["sessions"][0]["session_id"] == "m1"
    assert rr.read_snapshot(d, now=NOW, local_id="box-me") == {}


def test_publish_local_skips_unidentified_box(tmp_path):
    assert rr.publish_local(_primary(tmp_path), [_row("m1")], now=NOW, local_id="local") is False


def test_refresh_due_and_refresh_run_once_per_interval(tmp_path):
    d = _primary(tmp_path)
    box = _fs_box(tmp_path, d)
    assert rr.refresh_due(d, now=NOW, local_id="box-me") is True
    res = rr.refresh(d, now=NOW, local_sessions=[_row("m1")], local_id="box-me")
    assert res == {"box-a": True, "box-me": True}
    assert rr.refresh_due(d, now=NOW + 1, local_id="box-me") is False
    assert rr.refresh(d, now=NOW + 1, local_id="box-me") == {}
    assert rr.refresh_due(d, now=NOW + rr.REFRESH_INTERVAL_S + 1, local_id="box-me") is True
    assert box["box_id"] == "box-a"
