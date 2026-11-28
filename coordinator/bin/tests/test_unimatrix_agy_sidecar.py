"""The declared agy sidecar brings a conversation's leg up live from its env, never persisting the token."""
import importlib.util
import json
import os
import socket
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
CONV = "0cf351a3-6538-4a9b-814c-52b1b75a596c"
TOKEN = "tok-" + "x" * 20


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sidecar = load("unimatrix-agy-sidecar")
launch = load("unimatrix-agy-hook-launch")
sess = load("unimatrix-agy-hook-sessionstart")


class FakeProc:
    pid = os.getpid()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Sidecar env as agy hands it: LS address (a real listener) and token."""
    ls = socket.socket()
    ls.bind(("127.0.0.1", 0))
    ls.listen(4)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.setenv("ANTIGRAVITY_LS_ADDRESS", f"127.0.0.1:{ls.getsockname()[1]}")
    monkeypatch.setenv("ANTIGRAVITY_CSRF_TOKEN", TOKEN)
    monkeypatch.delenv("ANTIGRAVITY_CONVERSATION_ID", raising=False)
    spawned = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda cmd, **kw: spawned.append((cmd, kw["env"])) or FakeProc())
    monkeypatch.setattr(launch.os, "kill", lambda pid, sig: None)
    yield spawned
    ls.close()


def make_hub(tmp_path, agy_pid=4242):
    roster = launch.load_sibling("unimatrix-roster")
    return sidecar.Hub(launch, roster, agy_pid, "n0nce"), roster


def test_main_without_credentials_starts_nothing_and_writes_no_record(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    assert sidecar.main({}) == 0
    assert not (tmp_path / "unimatrix" / "agy-sidecars").exists()
    assert not (tmp_path / "unimatrix" / "agy-legs").exists()


def test_main_without_credentials_appends_one_trace_line_naming_the_absent_variables_and_the_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    assert sidecar.main({"ANTIGRAVITY_CSRF_TOKEN": TOKEN}) == 0
    lines = (tmp_path / "unimatrix" / "agy-sidecars.log").read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["has_ls_address"] is False and row["has_csrf_token"] is True
    assert row["exit_reason"] == "no-credentials" and row["pid"] == os.getpid()
    assert {"ppid", "agy_pid"} <= row.keys() and TOKEN not in lines[0]


def test_trace_log_is_capped_to_its_newest_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecar, "TRACE_MAX_BYTES", 1000)
    monkeypatch.setattr(sidecar, "TRACE_KEEP", 3)
    for i in range(40):
        sidecar.trace_launch(tmp_path, i, {}, "no-credentials")
    rows = [json.loads(x) for x in (tmp_path / "unimatrix" / "agy-sidecars.log").read_text().splitlines()]
    pids = [r["agy_pid"] for r in rows]
    log = tmp_path / "unimatrix" / "agy-sidecars.log"
    assert 3 <= len(rows) < 40 and pids == list(range(40 - len(pids), 40))  # trimmed, and what is left is the newest, contiguous
    assert log.stat().st_size <= 1000 + len(log.read_text().splitlines()[-1]) + 1  # never grows past the cap plus one line


def test_ls_credentials_reads_env_and_needs_both():
    assert launch.ls_credentials({"ANTIGRAVITY_LS_ADDRESS": "h:1"}) is None
    assert launch.ls_credentials({"ANTIGRAVITY_CSRF_TOKEN": "t"}) is None
    assert launch.ls_credentials({"ANTIGRAVITY_LS_ADDRESS": "h:1", "ANTIGRAVITY_CSRF_TOKEN": "t"}) == ("h:1", "t")


def test_a_started_leg_is_live_with_the_sidecar_env_address_and_the_token_only_in_the_leg_env(tmp_path, env):
    hub, _ = make_hub(tmp_path)
    assert hub.answer(json.dumps({"nonce": "n0nce", "conversationId": CONV, "cwd": str(tmp_path)})) == {"ok": True}
    rec_path = tmp_path / "unimatrix" / "agy-legs" / f"{CONV}.json"
    rec = json.loads(rec_path.read_text())
    assert rec["mode"] == "live" and rec["ls_address"] == os.environ["ANTIGRAVITY_LS_ADDRESS"]
    assert rec["agy_pid"] == 4242 and rec["cwd"] == str(tmp_path)
    cmd, leg_env = env[0]
    assert leg_env["ANTIGRAVITY_CSRF_TOKEN"] == TOKEN and leg_env["ANTIGRAVITY_CONVERSATION_ID"] == CONV
    assert TOKEN not in rec_path.read_text()
    assert all(TOKEN not in p.read_text(errors="replace") for p in tmp_path.rglob("*") if p.is_file())


def test_write_private_json_publishes_the_record_whole_with_no_temp_left(tmp_path):
    target = tmp_path / "rec.json"
    sidecar.write_private_json(target, {"nonce": "n", "port": 7})
    assert json.loads(target.read_text(encoding="utf-8")) == {"nonce": "n", "port": 7}
    assert [p.name for p in tmp_path.iterdir()] == ["rec.json"]
    if os.name != "nt":
        assert target.stat().st_mode & 0o077 == 0


def test_a_wrong_nonce_or_bad_conversation_id_starts_nothing(tmp_path, env):
    hub, _ = make_hub(tmp_path)
    for req in ({"nonce": "wrong", "conversationId": CONV}, {"nonce": "n0nce", "conversationId": "../x"},
                {"nonce": "n0nce"}, {"conversationId": CONV}):
        assert hub.answer(json.dumps(req)) == {"ok": False}
    assert hub.answer("{not json") == {"ok": False}
    assert env == [] and not (tmp_path / "unimatrix" / "agy-legs").exists()


def test_upgrade_pending_makes_only_this_agy_process_pending_legs_live(tmp_path, env):
    state = tmp_path / "unimatrix" / "agy-legs"
    state.mkdir(parents=True)
    other = "11111111-1111-1111-1111-111111111111"
    for conv, mode, agy in ((CONV, "pending", 4242), (other, "pending", 9999)):
        (state / f"{conv}.json").write_text(json.dumps({"pid": 4294967, "conversation": conv, "mode": mode, "agy_pid": agy,
                                                         "cwd": str(tmp_path), "endpoint": "e", "ls_address": None}))
    hub, _ = make_hub(tmp_path)
    hub.upgrade_pending()
    assert [e["ANTIGRAVITY_CONVERSATION_ID"] for _, e in env] == [CONV]
    assert json.loads((state / f"{CONV}.json").read_text())["mode"] == "live"
    assert json.loads((state / f"{other}.json").read_text())["mode"] == "pending"


def test_a_handoff_without_a_launch_dir_never_stores_the_sidecars_config_home_cwd(tmp_path, env, monkeypatch):
    fake = tmp_path / "fakehome"
    cfg = fake / ".gemini" / "config"
    cfg.mkdir(parents=True)
    monkeypatch.setattr(sidecar.Path, "home", classmethod(lambda cls: fake))
    monkeypatch.chdir(cfg)  # agy starts its sidecar in the config home
    hub, _ = make_hub(tmp_path)
    assert hub.answer(json.dumps({"nonce": "n0nce", "conversationId": CONV, "cwd": str(cfg)}).encode()) == {"ok": True}
    rec = json.loads((tmp_path / "unimatrix" / "agy-legs" / f"{CONV}.json").read_text())
    assert rec["cwd"] is None
    hub.answer(json.dumps({"nonce": "n0nce", "conversationId": CONV, "cwd": str(tmp_path)}).encode())
    assert json.loads((tmp_path / "unimatrix" / "agy-legs" / f"{CONV}.json").read_text())["cwd"] == str(tmp_path)


def serve_in_thread(hub, home, agy_pid, monkeypatch):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    rec = sidecar.record_dir(home) / f"{agy_pid}.json"
    rec.parent.mkdir(parents=True)
    rec.write_text(json.dumps({"pid": os.getpid(), "agy_pid": agy_pid, "port": srv.getsockname()[1], "nonce": hub.nonce}))
    alive = [True]
    t = threading.Thread(target=sidecar.serve, args=(hub, srv, lambda pid: alive[0]), daemon=True)
    monkeypatch.setattr(sidecar, "POLL_SECS", 0.1)
    t.start()
    return srv, alive, t


def test_handoff_round_trip_starts_the_leg_and_a_missing_or_dead_sidecar_answers_false(tmp_path, env, monkeypatch):
    hub, roster = make_hub(tmp_path)
    home = tmp_path
    assert sidecar.handoff_why(CONV, None, 4242, home, roster.pid_alive) == "no-record"  # no record
    srv, alive, t = serve_in_thread(hub, home, 4242, monkeypatch)
    try:
        assert sidecar.handoff_why(CONV, str(tmp_path), 4242, home, roster.pid_alive) is None
        assert (tmp_path / "unimatrix" / "agy-legs" / f"{CONV}.json").is_file()
        assert sidecar.handoff_why(CONV, None, 4242, home, lambda pid: False) == "dead-sidecar"  # sidecar pid dead
    finally:
        alive[0] = False
        t.join(5)
        srv.close()


def test_handoff_why_names_each_way_a_handoff_fails_and_none_on_ok(tmp_path, env, monkeypatch):
    hub, roster = make_hub(tmp_path)
    assert sidecar.handoff_why(CONV, None, 4242, tmp_path, roster.pid_alive) == "no-record"
    rec = sidecar.record_dir(tmp_path) / "4242.json"
    rec.parent.mkdir(parents=True)
    rec.write_text("{not json")
    assert sidecar.handoff_why(CONV, None, 4242, tmp_path, roster.pid_alive) == "no-record"
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()
    rec.write_text(json.dumps({"pid": os.getpid(), "agy_pid": 4242, "port": port, "nonce": "n0nce"}))
    assert sidecar.handoff_why(CONV, None, 4242, tmp_path, lambda pid: False) == "dead-sidecar"
    assert sidecar.handoff_why(CONV, None, 4242, tmp_path, lambda pid: True) == "refused"
    live = tmp_path / "live"
    srv2, alive, t = serve_in_thread(hub, live, 4343, monkeypatch)
    try:
        assert sidecar.handoff_why(CONV, str(tmp_path), 4343, live, lambda pid: True) is None
        assert sidecar.handoff_why("../x", None, 4343, live, lambda pid: True) == "bad-answer"
    finally:
        alive[0] = False
        t.join(5)
        srv2.close()


def test_a_sibling_import_failure_traces_import_failed_under_the_delivery_home_and_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.setattr(sidecar, "load_sibling", lambda name: (_ for _ in ()).throw(ImportError(name)))
    assert sidecar.main({}) == 1
    rows = [json.loads(x) for x in (tmp_path / "unimatrix" / "agy-sidecars.log").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["exit_reason"] == "import-failed" and rows[0]["agy_pid"] == os.getppid()


def test_an_import_failure_with_no_settings_home_traces_under_the_home_dir_default(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setattr(sidecar.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(sidecar, "load_sibling", lambda name: (_ for _ in ()).throw(ImportError(name)))
    assert sidecar.main({}) == 1
    log = tmp_path / ".coordinator-claude-settings" / "unimatrix" / "agy-sidecars.log"
    assert json.loads(log.read_text())["exit_reason"] == "import-failed"


def test_main_without_a_settings_home_traces_missing_settings_home_and_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setattr(sidecar.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(launch.Path, "home", classmethod(lambda cls: tmp_path))
    assert sidecar.main({"ANTIGRAVITY_LS_ADDRESS": "h:1", "ANTIGRAVITY_CSRF_TOKEN": TOKEN}) == 1
    rows = [json.loads(x) for x in (tmp_path / ".coordinator-claude-settings" / "unimatrix" / "agy-sidecars.log").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["exit_reason"] == "missing-settings-home"
    assert not (tmp_path / ".coordinator-claude-settings" / "unimatrix" / "agy-sidecars").exists()


def test_sessionstart_hands_off_to_a_live_sidecar_and_starts_pending_only_without_one(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.delenv("ANTIGRAVITY_CSRF_TOKEN", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_LS_ADDRESS", raising=False)
    spawned = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda cmd, **kw: spawned.append(kw["env"]) or FakeProc())
    side = sess.load_sibling("unimatrix-agy-sidecar")
    calls = []
    monkeypatch.setattr(side, "handoff_why", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(sess, "load_sibling", lambda name: side if name == "unimatrix-agy-sidecar" else load(name))
    sess.register({"conversationId": CONV, "cwd": str(tmp_path)}, 5)
    assert len(calls) == 1 and calls[0][:3] == (CONV, str(tmp_path), 5) and spawned == []  # the sidecar owns the start
    monkeypatch.setattr(side, "handoff_why", lambda *a, **k: "no-record")
    sess.register({"conversationId": CONV, "cwd": str(tmp_path)}, 5)
    rec = json.loads((tmp_path / "unimatrix" / "agy-legs" / f"{CONV}.json").read_text())
    assert len(spawned) == 1 and rec["mode"] == "pending" and rec["ls_address"] is None
    assert "ANTIGRAVITY_CSRF_TOKEN" not in spawned[0]


def test_main_binds_writes_its_record_answers_a_handoff_and_removes_the_record_on_exit(tmp_path, env, monkeypatch):
    seen = {}
    real_serve = sidecar.serve  # bound before the patch: the thread must run the real server, not serve_once

    def serve_once(hub, srv, pid_alive):
        rec_path = sidecar.record_dir(tmp_path) / f"{hub.agy_pid}.json"
        rec = json.loads(rec_path.read_text())
        seen.update(rec=rec, agy_pid=hub.agy_pid)
        assert rec["pid"] == os.getpid() and rec["port"] == srv.getsockname()[1] and rec["nonce"] == hub.nonce
        t = threading.Thread(target=real_serve, args=(hub, srv, lambda pid: not seen.get("done")), daemon=True)
        monkeypatch.setattr(sidecar, "POLL_SECS", 0.1)
        t.start()
        seen["ok"] = sidecar.handoff_why(CONV, str(tmp_path), hub.agy_pid, tmp_path, lambda pid: True)
        seen["done"] = True
        t.join(5)

    monkeypatch.setattr(sidecar, "serve", serve_once)
    assert sidecar.main() == 0
    assert seen["ok"] is None and (tmp_path / "unimatrix" / "agy-legs" / f"{CONV}.json").is_file()
    assert [e["ANTIGRAVITY_CONVERSATION_ID"] for _, e in env] == [CONV]
    assert not (sidecar.record_dir(tmp_path) / f"{seen['agy_pid']}.json").exists()  # cleaned up


def test_a_live_leg_drains_a_message_spooled_before_it_started(tmp_path, monkeypatch):
    leg = load("unimatrix-agy-leg")
    monkeypatch.setenv("AGY_LEG_SPOOL", str(tmp_path / "q.jsonl"))
    monkeypatch.setenv("AGY_LEG_LOG", str(tmp_path / "leg.log"))
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", CONV)
    leg.enqueue("while pending")
    sent = []
    monkeypatch.setattr(leg, "send", lambda text: sent.append(text) or {"ok": True})
    leg.drain()
    assert sent == ["while pending"] and (tmp_path / "q.jsonl").read_text() == ""
