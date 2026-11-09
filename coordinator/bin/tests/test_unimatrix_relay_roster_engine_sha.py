"""Roster lines carry the box's engine identity; the near-box mirror keeps it."""
import importlib.util
import json
from pathlib import Path

BIN = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, BIN / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_box(tmp_path, monkeypatch, stamp="abc123", freshness=None):
    root = tmp_path / "engine"
    (root / "coordinator_core").mkdir(parents=True)
    if stamp is not None:
        (root / "coordinator_core" / "_engine_stamp").write_text(stamp + "\n")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / ".git" / "refs" / "heads").mkdir(parents=True)
    (root / ".git" / "refs" / "heads" / "main").write_text("f" * 40 + "\n")
    home = tmp_path / "home"
    if freshness is not None:
        (home / "coordinator").mkdir(parents=True)
        (home / "coordinator" / "klabauter-freshness.json").write_text(json.dumps(freshness))
    monkeypatch.setenv("COORDINATOR_ENGINE_SOURCE_ROOT", str(root))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))


def run_roster(relay, tmp_path, monkeypatch, capsys):
    sess = tmp_path / "sess"
    sess.mkdir()
    (sess / "k").write_text("body")
    monkeypatch.setattr(relay, "SESS", str(sess))
    monkeypatch.setattr(relay, "entries", lambda: [(1, {"pid": 1, "messagingSocketPath": "/s"}, "k")])
    relay.roster()
    return [json.loads(ln) for ln in capsys.readouterr().out.splitlines()]


def test_every_line_has_engine_sha_and_checkout_sha(tmp_path, monkeypatch, capsys):
    make_box(tmp_path, monkeypatch)
    rows = run_roster(load("relay_a", "unimatrix-relay.py"), tmp_path, monkeypatch, capsys)
    assert rows[0]["engine_sha"] == "abc123"
    assert rows[0]["engine_checkout_sha"] == "f" * 40
    assert "engine_freshness" not in rows[0]


def test_freshness_present_only_when_record_exists(tmp_path, monkeypatch, capsys):
    rec = {"hostname": "h", "local_sha": "a", "origin_sha": "b", "state": "dirty"}
    make_box(tmp_path, monkeypatch, freshness=rec)
    rows = run_roster(load("relay_b", "unimatrix-relay.py"), tmp_path, monkeypatch, capsys)
    assert rows[0]["engine_freshness"] == rec


def test_unstamped_box_still_reports_key(tmp_path, monkeypatch, capsys):
    make_box(tmp_path, monkeypatch, stamp=None)
    rows = run_roster(load("relay_c", "unimatrix-relay.py"), tmp_path, monkeypatch, capsys)
    assert "engine_sha" in rows[0] and rows[0]["engine_sha"] is None


def _entries_in(relay, tmp_path, monkeypatch, files):
    sess = tmp_path / "sess"
    sess.mkdir()
    for pid, d in files.items():
        (sess / f"{pid}.json").write_text(json.dumps(d))
        (sess / f"{pid}.h.key").write_text("k")
    monkeypatch.setattr(relay, "SESS", str(sess))
    return sorted(p for p, _, _ in relay.entries())


def test_entries_drops_a_recycled_pid_and_a_mirror_without_a_live_supervisor(tmp_path, monkeypatch):
    import os
    relay = load("relay_e", "unimatrix-relay.py")
    lv = relay._liveness()
    me = os.getpid()
    start = lv.proc_start(me)
    far = {"macSocketPath": "/far/1.sock"}
    files = {
        me: {"pid": me, "procStart": start},
        me + 1: {"pid": me + 1, "procStart": "any"},
    }
    monkeypatch.setattr(lv, "pid_alive", lambda p: p in (me, me + 1, me + 2, me + 3, me + 4))
    real = lv.proc_start
    monkeypatch.setattr(lv, "proc_start", lambda p, platform=None: real(me) if p == me else f"now-{p}")
    files.update({
        me + 2: {"pid": me + 2, "procStart": f"now-{me + 2}", **far, "unimatrixRosterPid": me,
                 "unimatrixRosterProcStart": start},
        me + 3: {"pid": me + 3, "procStart": f"now-{me + 3}", **far, "unimatrixRosterPid": me,
                 "unimatrixRosterProcStart": "stale"},
        me + 4: {"pid": me + 4, "procStart": f"now-{me + 4}", **far},
    })
    assert _entries_in(relay, tmp_path, monkeypatch, files) == [me, me + 2]


def test_entries_drops_a_codex_face_whose_recorded_holder_is_dead(tmp_path, monkeypatch):
    import os
    relay = load("relay_h", "unimatrix-relay.py")
    lv = relay._liveness()
    me = os.getpid()
    home = tmp_path / "home"
    (home / "unimatrix").mkdir(parents=True)
    peers = [
        {"name": "c-dead", "vendor": "codex", "target": "t-dead", "codexPid": me + 100},
        {"name": "c-live", "vendor": "codex", "target": "t-live", "codexPid": me},
        {"name": "c-none", "vendor": "codex", "target": "t-none"},
    ]
    (home / "unimatrix" / "peers.json").write_text(json.dumps({"peers": peers}))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setattr(lv, "pid_alive", lambda p: p in (me, me + 1, me + 2, me + 3, me + 4))
    face = lambda p, t: {"pid": p, "vendor": "codex", "target": t}
    files = {me + 1: face(me + 1, "t-dead"), me + 2: face(me + 2, "t-live"),
             me + 3: face(me + 3, "t-none"), me + 4: {"pid": me + 4, "vendor": "claude", "target": "t-dead"}}
    assert _entries_in(relay, tmp_path, monkeypatch, files) == [me + 2, me + 3, me + 4]


def test_codex_export_refused_for_non_root_or_unaddressable_threads_only():
    import types
    relay = load("relay_x", "unimatrix-relay.py")
    cache = {
        "router": types.SimpleNamespace(codex_is_sendable_root=lambda m: bool(m.get("root"))),
        "codex": types.SimpleNamespace(thread_addressable=lambda t, home, index: t == "a"),
        "home": None, "metadata": {"a": {"root": 1}, "b": {"root": 1}, "c": {}}, "index": {},
    }
    refused = lambda d: relay.codex_export_refused(d, cache)
    assert refused({"vendor": "codex", "target": "a"}) is False
    assert refused({"vendor": "codex", "target": "b"}) is True  # root, but no addressable rollout
    assert refused({"vendor": "codex", "target": "c"}) is True  # not a sendable root
    assert refused({"vendor": "claude", "target": "c"}) is False
    assert refused({"vendor": "codex", "target": "c", "unimatrixMirror": True}) is False


def test_mirror_helper_passes_far_fields_and_nothing_else():
    roster = load("roster_a", "unimatrix-roster.py")
    row = {"entry": {"pid": 1}, "key": "k", "engine_sha": "s", "engine_checkout_sha": "c",
           "engine_freshness": {"state": "dirty"}}
    assert roster.engine_fields(row) == {
        "engine_sha": "s", "engine_checkout_sha": "c", "engine_freshness": {"state": "dirty"}}
    assert roster.engine_fields({"entry": {}, "key": "k"}) == {}
