"""Display metadata and legacy aliases preserve the underlying far-session route."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace


BIN = Path(__file__).resolve().parents[1]


def _load(filename, name):
    spec = importlib.util.spec_from_file_location(name, BIN / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


roster = _load("unimatrix-roster.py", "identity_roster")
relay = _load("unimatrix-relay.py", "identity_relay")
router = _load("unimatrix-peer-router.py", "identity_router")
send = _load("unimatrix-send.py", "identity_send")
UUID = "01a1b630-6d3a-7861-ae89-44e2b9b4cfaa"


def test_vendor_inference_uses_peer_identity_or_native_version():
    assert roster.session_identity({"sessionId": "peer-codex-repo-12"})["vendor"] == "codex"
    assert roster.session_identity({"sessionId": "peer-agy-repo-12"})["vendor"] == "agy"
    assert roster.session_identity({"sessionId": UUID, "version": "2.1.319"})["vendor"] == "claude"
    assert roster.session_identity({"name": "codex-repo"})["vendor"] == "unknown"
    assert roster.session_identity({"vendor": "codex", "sessionId": "peer-repo-12"})["vendor"] == "codex"


def test_relay_exports_additive_identity_without_mutating_native_registration(tmp_path, monkeypatch, capsys):
    original = {"pid": 1, "name": "native-claude", "sessionId": UUID, "version": "2.1.319"}
    (tmp_path / "key").write_text("test-token")
    monkeypatch.setattr(relay, "SESS", str(tmp_path))
    monkeypatch.setattr(relay, "entries", lambda: [(1, original, "key")])
    monkeypatch.setattr(relay, "engine_fields", lambda: {})
    monkeypatch.setattr(relay, "load_roster", lambda: roster)
    monkeypatch.setattr(roster.socket, "gethostname", lambda: "Mac-Mini")
    relay.roster()
    entry = json.loads(capsys.readouterr().out)["entry"]
    assert entry["name"] == original["name"]
    assert entry["vendor"] == "claude" and entry["box"] == "mac-mini"
    assert "vendor" not in original and "box" not in original


def test_strict_legacy_peer_name_is_readable_without_retargeting(tmp_path, monkeypatch):
    legacy = "codex-" + UUID
    entry = {"pid": 812, "name": legacy, "sessionId": "peer-" + legacy + "-812",
             "pidDomain": "darwin", "cwd": "/Users/me/X/claude-klabauter", "messagingSocketPath": "/far.sock"}
    monkeypatch.setattr(roster, "spawn", lambda command: SimpleNamespace(pid=31337))
    monkeypatch.setattr(roster.time, "sleep", lambda _: None)
    monkeypatch.setattr(roster, "proc_start", lambda pid: "local-start")
    monkeypatch.setattr(roster, "pid_domain", lambda: "win32:machine-a")
    monkeypatch.setattr(roster, "local_endpoint", lambda far: str(tmp_path / "local.sock"))
    monkeypatch.setattr(roster, "sessions_dir", lambda: str(tmp_path))
    monkeypatch.setattr(roster, "write_peer_key", lambda *args: None)
    monkeypatch.setattr(roster, "local_peer_key", lambda *args: {})
    cfg = SimpleNamespace(remote="me@macmini", key="unused", tunnel_remote=None, log=str(tmp_path / "log"))
    row = {"entry": entry, "key": "test-token"}
    roster.mirror(row, cfg, lambda _: None, scan=[])
    first = json.loads((tmp_path / "31337.json").read_text())
    assert first["name"].startswith("codex-claude-klabauter-") and first["name"].endswith("-on-macmini")
    assert "@" not in first["name"] and first["unimatrixFarName"] == legacy
    assert first["farPid"] == 812 and first["macSocketPath"] == "/far.sock"
    assert first["box"] == "macmini" and first["vendor"] == "codex"
    assert first["pidDomain"] == "win32:machine-a" and first["farPidDomain"] == "darwin"
    assert entry["name"] == legacy
    roster.rename_live_mirror(31337, "/far.sock", row, cfg, lambda _: None, scan=[])
    assert json.loads((tmp_path / "31337.json").read_text()) == first


def test_mirror_stamps_box_and_sync_restamps_older_box_none_mirror(tmp_path, monkeypatch):
    entry = {"pid": 9, "name": "group-em-ad", "sessionId": UUID, "version": "2.1.319",
             "box": None, "unimatrixBox": None, "messagingSocketPath": "/far.sock"}
    monkeypatch.setattr(roster, "spawn", lambda command: SimpleNamespace(pid=9569))
    monkeypatch.setattr(roster.time, "sleep", lambda _: None)
    monkeypatch.setattr(roster, "proc_start", lambda pid: "local-start")
    monkeypatch.setattr(roster, "pid_domain", lambda: "win32:machine-a")
    monkeypatch.setattr(roster, "local_endpoint", lambda far: str(tmp_path / "local.sock"))
    monkeypatch.setattr(roster, "sessions_dir", lambda: str(tmp_path))
    monkeypatch.setattr(roster, "write_peer_key", lambda *args: None)
    monkeypatch.setattr(roster, "local_peer_key", lambda *args: {})
    cfg = SimpleNamespace(remote="me@macmini", key="unused", tunnel_remote=None, log=str(tmp_path / "log"))
    row = {"entry": entry, "key": "test-token"}
    roster.mirror(row, cfg, lambda _: None, scan=[])
    path = tmp_path / "9569.json"
    fresh = json.loads(path.read_text())
    assert fresh["box"] == "macmini" and fresh["unimatrixBox"] == "macmini"
    old = dict(fresh, box=None)
    del old["unimatrixBox"], old["unimatrixRemote"]
    path.write_text(json.dumps(old))
    assert roster.remote_mirrors([old], "me@macmini") == [old]
    roster.rename_live_mirror(9569, "/far.sock", row, cfg, lambda _: None, scan=[])
    healed = json.loads(path.read_text())
    assert healed["box"] == "macmini" and healed["unimatrixBox"] == "macmini"
    assert roster.remote_mirrors([healed], "me@macmini") == [healed]
    assert roster.remote_mirrors([healed], "me@other") != [healed]


def test_explicit_peer_and_native_names_keep_their_stem_and_gain_the_box():
    assert roster.mirror_entry_name({"name": "friendly-codex"}, "macmini", scan=[]) == "friendly-codex.macmini"
    entry = {"name": "codex-" + UUID, "sessionId": UUID, "version": "2.1.319"}
    assert roster.mirror_entry_name(entry, "macmini", scan=[]) == f"{entry['name']}.macmini"


def test_far_claude_entry_name_carries_its_box_with_or_without_a_local_namesake(tmp_path, monkeypatch):
    entry = {"name": "example-game-repo-em", "sessionId": UUID, "version": "2.1.319"}
    assert roster.mirror_entry_name(entry, "me@machine-a", scan=[]) == "example-game-repo-em.machine-a"
    assert roster.mirror_entry_name(entry, "me@machine-a", scan=[("example-game-repo-em", "/local.sock")]) == "example-game-repo-em.machine-a"
    assert roster.mirror_entry_name(dict(entry, name="example-game-repo-em.machine-a"), "me@machine-a", scan=[]) == "example-game-repo-em.machine-a"


def test_legacy_agy_alias_keeps_its_single_on_box_label():
    entry = {"name": "agy-" + UUID, "sessionId": "peer-agy-repo-12", "cwd": "/work/repo"}
    name = roster.mirror_entry_name(entry, "me@Machine-a", scan=[])
    assert name.endswith("-on-machine-a") and name.count("-on-") == 1 and roster.LABEL_SEP not in name


def test_two_boxes_with_identical_far_socket_and_name_get_distinct_display_routes(tmp_path, monkeypatch):
    far_socket = "/tmp/cc-socks/cc-msg-086bf13a2ac11a310327603bcc2288c5.sock"
    name = "codex-claude-klabauter"
    entry = {"pid": 812, "name": name, "sessionId": "peer-codex-claude-klabauter-812",
             "cwd": "/work/claude-klabauter", "messagingSocketPath": far_socket}
    pids = iter((31337, 31338))
    monkeypatch.setattr(roster, "spawn", lambda command: SimpleNamespace(pid=next(pids)))
    monkeypatch.setattr(roster.time, "sleep", lambda _: None)
    monkeypatch.setattr(roster, "proc_start", lambda pid: "local-start")
    monkeypatch.setattr(roster, "pid_domain", lambda: "win32:machine-a")
    monkeypatch.setattr(roster, "pid_alive", lambda pid: True)
    monkeypatch.setattr(roster._liveness(), "pid_alive", lambda pid: True)
    monkeypatch.setattr(roster._liveness(), "proc_start", lambda pid: "local-start")
    monkeypatch.setattr(roster, "sessions_dir", lambda: str(tmp_path))
    monkeypatch.setattr(roster, "local_endpoint", lambda seed: str(tmp_path / (roster.hashlib.sha256(seed.encode()).hexdigest() + ".sock")))
    monkeypatch.setattr(roster, "write_peer_key", lambda *args: None)
    monkeypatch.setattr(roster, "local_peer_key", lambda *args: {})
    configurations = [SimpleNamespace(remote="me@192.168.0.175", key="unused", tunnel_remote=None,
                                      log=str(tmp_path / "a.log")),
                      SimpleNamespace(remote="me@192.168.0.116", key="unused", tunnel_remote=None,
                                      log=str(tmp_path / "b.log"))]
    row = {"entry": entry, "key": "test-token"}
    for cfg in configurations:
        roster.mirror(row, cfg, lambda _: None)
    for pid, cfg in zip((31337, 31338), configurations):
        roster.rename_live_mirror(pid, far_socket, row, cfg, lambda _: None, roster.live_sessions())
    entries = [json.loads((tmp_path / f"{pid}.json").read_text()) for pid in (31337, 31338)]
    assert {item["name"] for item in entries} == {name + ".192.168.0.175", name + ".192.168.0.116"}
    assert {item["unimatrixFarName"] for item in entries} == {name}
    assert len({item["messagingSocketPath"] for item in entries}) == 2
    assert len({key for _, key in roster.live_sessions()}) == 2


def test_router_and_cli_list_vendor_box_and_cwd_without_changing_names(tmp_path, monkeypatch, capsys):
    entries = [
        {"pid": 101, "name": "group-em-09", "version": "2.1.319", "status": "idle", "cwd": "/group"},
        {"pid": 102, "name": "remote-codex", "sessionId": "peer-codex-a-102", "box": "macmini",
         "macSocketPath": "/far.sock", "status": "idle", "cwd": "/work/repo",
         "unimatrixRemote": "user@macmini", "farPid": 7, "procStart": "start-102", "pidDomain": "domain",
         "unimatrixVendor": "codex", "unimatrixBox": "macmini", "messagingSocketPath": str(tmp_path / "local.sock")},
        {"pid": 103, "name": "hidden-transport", "unimatrixTransport": True},
    ]
    (tmp_path / "local.sock").write_text("")
    for entry in entries:
        (tmp_path / f"{entry['pid']}.json").write_text(json.dumps(entry))
    monkeypatch.setattr(router._roster(), "pid_alive", lambda pid: True)
    monkeypatch.setattr(router._roster(), "proc_start", lambda pid: "start-102")
    monkeypatch.setattr(router._roster(), "pid_domain", lambda: "domain")
    monkeypatch.setattr(router, "codex_is_sendable_root", lambda metadata: True)
    r = router.Router([{"name": "local-peer", "vendor": "codex", "target": "root", "cwd": "/local",
                       "codexPid": os.getpid()}],
                      sessions_dir=tmp_path, state_dir=tmp_path / "state")
    response = asyncio.run(r._dispatch({"op": "list"}))
    assert response["sessions"] == ["group-em-09", "remote-codex"]
    assert response["details"]["group-em-09"]["vendor"] == "claude"
    assert response["details"]["remote-codex"]["box"] == "macmini"
    fake = SimpleNamespace(ROUTER_ENDPOINT_SEED="unused", endpoint_path=lambda *args: "unused",
                           request_line=lambda *args: response)
    monkeypatch.setattr(send, "_router", lambda: fake)
    assert send.main(["--list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    remote = next(line for line in lines if line.startswith("remote-codex\t"))
    cols = remote.split("\t")
    assert cols[:2] == ["remote-codex", "macmini"] and cols[3:] == ["codex", "session"]
    assert response["details"]["remote-codex"]["cwd"] == "/work/repo"
    local = next(line for line in lines if line.startswith("local-peer\t")).split("\t")
    assert local[3:] == ["codex", "ready"]


def test_box_key_folds_case_user_and_domain_but_keeps_an_address_whole():
    assert roster.box_key("me@Machine-b.local") == roster.box_key("machine-b") == "machine-b"
    assert roster.box_key("me@192.168.0.175") == "192.168.0.175"
    assert roster.labelled("example-game-repo-em", "me@Machine-b") == "example-game-repo-em.Machine-b"


def test_only_the_lowest_alias_of_one_box_keeps_a_mirror_of_a_far_socket():
    sock = "/tmp/cc-socks/a.sock"
    held_by = lambda alias: [("example-game-repo-em", roster.mirror_scan_key(alias, sock))]
    assert roster.alias_duplicate("machine-b", sock, held_by("Machine-b.local")) is True
    assert roster.alias_duplicate("Machine-b.local", sock, held_by("machine-b")) is False
    assert roster.alias_duplicate("machine-b", sock, held_by("other")) is False
    assert roster.alias_duplicate("machine-b", "/tmp/cc-socks/b.sock", held_by("Machine-b.local")) is False


def test_every_mirror_row_the_roster_writes_carries_host_box_and_a_labelled_name(tmp_path, monkeypatch):
    entry = {"pid": 9, "name": "group-em-c1", "sessionId": UUID, "version": "2.1.319",
             "messagingSocketPath": r"\\.\pipe\LOCAL\cc-msg-90f66"}
    monkeypatch.setattr(roster, "spawn", lambda command: SimpleNamespace(pid=9570))
    monkeypatch.setattr(roster.time, "sleep", lambda _: None)
    monkeypatch.setattr(roster, "proc_start", lambda pid: "local-start")
    monkeypatch.setattr(roster, "pid_domain", lambda: "darwin")
    monkeypatch.setattr(roster, "local_endpoint", lambda far: str(tmp_path / "local.sock"))
    monkeypatch.setattr(roster, "sessions_dir", lambda: str(tmp_path))
    monkeypatch.setattr(roster, "write_peer_key", lambda *args: None)
    monkeypatch.setattr(roster, "local_peer_key", lambda *args: {})
    cfg = SimpleNamespace(remote="me@machine-b", key="unused", tunnel_remote=None, log=str(tmp_path / "log"))
    row = {"entry": entry, "key": "test-token"}
    roster.mirror(row, cfg, lambda _: None, scan=[])
    path = tmp_path / "9570.json"
    fresh = json.loads(path.read_text())
    assert fresh["unimatrixRemote"] == "me@machine-b" and fresh["unimatrixBox"] == "machine-b"
    assert fresh["name"] == roster.labelled(fresh["name"], cfg.remote) == "group-em-c1.machine-b"
    # A pre-fix row with a bare name and no host is healed by the reconcile pass.
    stale = {k: v for k, v in fresh.items() if k not in ("unimatrixRemote", "unimatrixBox")}
    stale["name"] = "group-em-c1"
    path.write_text(json.dumps(stale))
    roster.rename_live_mirror(9570, entry["messagingSocketPath"], row, cfg, lambda _: None, scan=[])
    healed = json.loads(path.read_text())
    assert healed["unimatrixRemote"] == "me@machine-b" and healed["unimatrixBox"] == "machine-b"
    assert healed["name"] == roster.labelled(healed["name"], cfg.remote) == "group-em-c1.machine-b"


def test_session_identity_origin_is_one_box_id_across_hostname_spellings(monkeypatch):
    for host in ("Machine-b.local", "machine-b", "MACHINE-B.Local"):
        monkeypatch.setattr(roster.socket, "gethostname", lambda host=host: host)
        assert roster.session_identity({"name": "x"})["box"] == "machine-b"
