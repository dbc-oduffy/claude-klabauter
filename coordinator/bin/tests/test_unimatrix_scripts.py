"""Behavior pins for the vendored unimatrix bridge/relay/rekey/inbox scripts."""
import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
NAMES = ("bridge", "relay", "rekey", "inbox")


def load(name):
    path = BIN / f"unimatrix-{name}.py"
    spec = importlib.util.spec_from_file_location(f"unimatrix_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_rekey_name_hashes_socket_path_verbatim_off_windows():
    rekey = load("rekey")
    sock = "/tmp/cc-socks/Sess-1.sock"
    assert rekey.rekey_name(42, sock, platform="linux") == f"42.{hashlib.sha256(sock.encode()).hexdigest()}.key"


def test_rekey_name_lowercases_on_windows_only():
    rekey = load("rekey")
    pipe = r"\\.\pipe\Claude-Sess"
    win = rekey.rekey_name(7, pipe, platform="win32")
    assert win == f"7.{hashlib.sha256(pipe.lower().encode()).hexdigest()}.key"
    assert rekey.rekey_name(7, pipe, platform="darwin") != win


@pytest.mark.parametrize("cmd,expected", [
    ("roster", ("roster", None)),
    ("send 1234", ("send", 1234)),
    ("inbox machine-a-1", ("inbox", "machine-a-1")),
    ("source", ("source", None)),
])
def test_relay_accepts_the_three_modes(cmd, expected):
    assert load("relay").parse_mode(cmd) == expected


@pytest.mark.parametrize("cmd", [
    "", None, "roster extra", "send", "send abc", "send 1 2", "inbox", "inbox Bad_Name",
    "inbox " + "a" * 41, "bash -c id", "send 12; id",
])
def test_relay_refuses_everything_else(cmd):
    assert load("relay").parse_mode(cmd) is None


def test_bridge_relay_mode_requires_remote():
    bridge = load("bridge")
    with pytest.raises(SystemExit):
        bridge.parse_args(["ep", "relay", "log.txt"])
    a = bridge.parse_args(["ep", "relay", "log.txt", "--remote", "farbox"])
    assert a.port is None and a.remote == "farbox"


def test_bridge_default_key_lives_under_settings_home_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    a = load("bridge").parse_args(["ep", "9000", "log.txt"])
    assert Path(a.key) == tmp_path / ".secrets" / "ssh" / "unimatrix-zero"
    assert a.port == 9000
    explicit = load("bridge").parse_args(["ep", "9000", "log.txt", "--key", "k"])
    assert explicit.key == "k"


def test_bridge_and_inbox_pick_the_lan_alias_through_the_vendored_roster(tmp_path):
    # An installed copy holds only the vendored scripts, so pick_remote must resolve roster beside itself.
    for name in ("bridge", "inbox", "roster"):
        (tmp_path / f"unimatrix-{name}.py").write_text((BIN / f"unimatrix-{name}.py").read_text(encoding="utf-8"),
                                                       encoding="utf-8")
    for name in ("bridge", "inbox"):
        spec = importlib.util.spec_from_file_location(f"inst_{name}", tmp_path / f"unimatrix-{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.pick_remote("lan", None) == "lan"


def test_bridge_relay_dials_the_picked_alias(monkeypatch, tmp_path):
    import subprocess
    import types
    bridge = load("bridge")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "1.json").write_text(json.dumps({"messagingSocketPath": "/m.sock", "macSocketPath": "/far/9.sock"}))
    monkeypatch.setattr(bridge, "SESSIONS", str(sessions))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    bridge.cfg = types.SimpleNamespace(endpoint="/m.sock", key="k", remote="lan", tunnel_remote="wg", port=None)
    bridge.log = open(tmp_path / "b.log", "a", encoding="utf-8")
    argvs = []

    def run(argv, **kw):
        argvs.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", run)

    class Conn:
        chunks = [b'{"from": "uds:/s.sock", "message": "hi"}\n', b""]

        def read(self):
            return self.chunks.pop(0)

    for lan_up, expect in ((True, "lan"), (False, "wg")):
        Conn.chunks = [b'{"from": "uds:/s.sock", "message": "hi"}\n', b""]
        monkeypatch.setattr(bridge, "pick_remote", lambda lan, tun, up=lan_up: lan if up else tun)
        bridge.relay_send(Conn())
        assert expect in argvs[-1] and ({"lan", "wg"} - {expect}).isdisjoint(argvs[-1])


def test_bridge_and_inbox_take_a_tunnel_alias_from_flag_or_environment(monkeypatch):
    monkeypatch.delenv("UNIMATRIX_TUNNEL_REMOTE", raising=False)
    assert load("bridge").parse_args(["ep", "relay", "l", "--remote", "lan"]).tunnel_remote is None
    assert load("inbox").parse_args(["1", "n", "l", "--remote", "lan"]).tunnel_remote is None
    monkeypatch.setenv("UNIMATRIX_TUNNEL_REMOTE", "wg-env")
    assert load("bridge").parse_args(["ep", "relay", "l", "--remote", "lan"]).tunnel_remote == "wg-env"
    assert load("inbox").parse_args(["1", "n", "l", "--remote", "lan", "--tunnel-remote", "wg"]).tunnel_remote == "wg"


def test_inbox_requires_remote():
    inbox = load("inbox")
    with pytest.raises(SystemExit):
        inbox.parse_args(["1", "n", "log.txt"])
    assert inbox.parse_args(["1", "n", "log.txt", "--remote", "farbox"]).remote == "farbox"


def test_bridge_pipe_names_are_cc_msg_32_hex_and_other_names_refused():
    bridge = load("bridge")
    pipe = bridge.mirror_pipe("/tmp/cc-socks/far.sock")
    assert re.fullmatch(r"\\\\\.\\pipe\\LOCAL\\cc-msg-[0-9a-f]{32}", pipe)
    bridge.check_endpoint(pipe, platform="win32")
    for bad in (r"\\.\pipe\LOCAL\gem-7d", r"\\.\pipe\LOCAL\cc-msg-" + "a" * 31, r"\\.\pipe\cc-msg-" + "G" * 32):
        with pytest.raises(SystemExit, match="cc-msg"):
            bridge.check_endpoint(bad, platform="win32")
    bridge.check_endpoint("/tmp/anything.sock", platform="linux")


def test_inbox_registration_sends_entry_and_key_only_with_no_key_hash():
    inbox = load("inbox")
    out = inbox.registration_payload('{"pid": 7, "name": "s"}', "secret")
    entry_line, key_line, rest = out.split(b"\n")
    assert json.loads(entry_line) == {"pid": 7, "name": "s"}
    assert key_line == b"secret" and rest == b""


def test_inbox_deliver_mode_rewrites_from_to_the_local_mirror(monkeypatch):
    inbox = load("inbox")
    mirror = {"macSocketPath": "/tmp/cc-socks/9.sock", "messagingSocketPath": r"\\.\pipe\LOCAL\cc-msg-" + "0" * 32}
    monkeypatch.setattr(inbox, "_mirrors", lambda: iter([mirror]))
    frame = {"from": "uds:/tmp/cc-socks/9.sock", "message": {"content": '<m from="uds:/tmp/cc-socks/9.sock">'}}
    out = json.loads(inbox.localize(frame, b"raw"))
    assert out["from"] == "uds:" + mirror["messagingSocketPath"]
    assert mirror["messagingSocketPath"] in out["message"]["content"]


def test_relay_refuses_a_name_a_live_relay_already_holds(monkeypatch):
    relay = load("relay")
    monkeypatch.setattr(relay, "entries", lambda: iter([(11, {"name": "far-1", "unimatrixMirror": True}, "11.k.key")]))
    with pytest.raises(SystemExit, match="already mirrored by live relay pid 11"):
        relay.check_name_free("far-1")
    relay.check_name_free("other-name")


def test_relay_refuses_a_name_a_live_local_session_holds(monkeypatch):
    relay = load("relay")
    monkeypatch.setattr(relay, "entries", lambda: iter([(11, {"name": "local-1"}, "11.k.key")]))
    with pytest.raises(SystemExit, match="live local session"):
        relay.check_name_free("local-1")


def test_relay_stores_the_far_socket_and_drops_unlisted_remote_fields():
    relay = load("relay")
    raw = {"messagingSocketPath": r"\\.\pipe\far", "pid": 1, "procStart": "x", "cwd": "/w", "name": "evil"}
    entry = relay.mirror_entry(raw, "far-1")
    assert entry == {"cwd": "/w", "name": "far-1", "unimatrixRemoteSocket": r"\\.\pipe\far"}


def test_relay_send_swaps_a_far_reply_address_for_the_mirror_socket(monkeypatch):
    relay = load("relay")
    mirror = {"unimatrixMirror": True, "unimatrixRemoteSocket": r"\\.\pipe\far", "messagingSocketPath": "/tmp/cc-socks/m.sock"}
    monkeypatch.setattr(relay, "entries", lambda: iter([(3, mirror, "3.k.key")]))
    out = json.loads(relay.local_reply_address(json.dumps({"from": "uds:" + r"\\.\pipe\far"}).encode()))
    assert out["from"] == "uds:/tmp/cc-socks/m.sock"


def test_relay_rewrites_the_reply_address_inside_message_content(monkeypatch):
    relay = load("relay")
    far = r"\\.\pipe\far"
    mirror = {"unimatrixMirror": True, "unimatrixRemoteSocket": far, "messagingSocketPath": "/tmp/cc-socks/m.sock"}
    monkeypatch.setattr(relay, "entries", lambda: iter([(3, mirror, "3.k.key")]))
    content = f'<cross-session-message from="uds:{far}" from-name="g">hi</cross-session-message>'
    line = json.dumps({"from": "uds:" + far, "message": {"content": content}}).encode()
    out = json.loads(relay.local_reply_address(line))
    assert 'from="uds:/tmp/cc-socks/m.sock"' in out["message"]["content"]
    assert far not in out["message"]["content"]


def test_relay_inbox_serve_returns_on_stdin_eof(monkeypatch):
    relay = load("relay")
    monkeypatch.setattr(relay.select, "select", lambda r, w, x: ([42], [], []))
    monkeypatch.setattr(relay.os, "read", lambda fd, n: b"")
    relay.serve(object(), 42, None)


def test_relay_release_unlinks_only_its_own_socket_inode(tmp_path):
    relay = load("relay")
    sock, entry = tmp_path / "s.sock", tmp_path / "1.json"
    sock.write_text("a")
    entry.write_text("e")
    ino = sock.stat().st_ino
    relay.release(str(sock), ino, [str(entry)])
    assert not sock.exists() and not entry.exists()
    sock.write_text("successor")
    entry.write_text("e")
    relay.release(str(sock), ino + 1, [str(entry)])
    assert sock.exists() and not entry.exists()


def test_relay_key_file_is_named_by_the_relay_from_its_own_socket_path():
    sock = "/tmp/cc-socks/unimatrix-far-1.sock"
    assert load("relay").key_file_name(9, sock) == f"9.{hashlib.sha256(sock.encode()).hexdigest()}.key"


def test_bridge_relay_mode_drops_the_auth_line_before_send():
    auth = b'{"type": "auth", "token": "t"}'
    msg = b'{"from": "uds:x", "message": "hi"}'
    assert load("bridge").frames_to_send(auth + b"\n" + msg + b"\n") == [msg]


@pytest.mark.parametrize("platform,kind", [("win32", "pipe"), ("darwin", "socket"), ("linux", "socket")])
def test_relay_local_endpoint_kind_per_platform(platform, kind):
    assert load("relay").endpoint_kind(platform) == kind


def test_relay_send_refuses_foreign_pipe_on_windows(monkeypatch):
    relay = load("relay")
    monkeypatch.setattr(relay.sys, "platform", "win32")
    monkeypatch.setattr(relay, "entries", lambda: iter([(5, {"messagingSocketPath": "not-a-pipe"}, "5.k.key")]))
    with pytest.raises(SystemExit, match="non-session pipe"):
        relay.send(5)


def test_relay_send_refuses_socket_outside_session_dir(monkeypatch):
    relay = load("relay")
    monkeypatch.setattr(relay, "endpoint_kind", lambda platform=None: "socket")
    monkeypatch.setattr(relay, "entries", lambda: iter([(5, {"messagingSocketPath": "/etc/passwd"}, "5.k.key")]))
    with pytest.raises(SystemExit, match="non-session socket"):
        relay.send(5)


def test_inbox_pid_must_be_an_integer():
    with pytest.raises(SystemExit):
        load("inbox").parse_args(["../x", "n", "log.txt", "--remote", "farbox"])


def test_relay_has_no_top_level_win32_import():
    src = (BIN / "unimatrix-relay.py").read_text(encoding="utf-8")
    assert not [l for l in src.splitlines() if re.match(r"(import|from)\s+(pywintypes|win32)", l)]


def test_bridge_and_inbox_import_without_pywin32():
    # Top-level win32 imports would break macOS/Linux; importing must not need them.
    for name in ("bridge", "inbox"):
        src = (BIN / f"unimatrix-{name}.py").read_text(encoding="utf-8")
        top_level = [l for l in src.splitlines() if re.match(r"(import|from)\s+(pywintypes|win32)", l)]
        assert top_level == []


IPV4 = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
USER_AT_HOST = re.compile(r"\b[A-Za-z0-9_.-]+@[A-Za-z0-9][A-Za-z0-9.-]*\b")


@pytest.mark.parametrize("name", NAMES)
def test_no_ip_literal_or_user_at_host(name):
    src = (BIN / f"unimatrix-{name}.py").read_text(encoding="utf-8")
    assert IPV4.findall(src) == []
    assert USER_AT_HOST.findall(src) == []


@pytest.mark.parametrize("name", ("bridge", "inbox", "roster"))
def test_every_ssh_argv_pins_identities_only(name):
    src = (BIN / f"unimatrix-{name}.py").read_text(encoding="utf-8")
    argvs = re.findall(r'\[\s*"ssh"[^\]]*\]', src)
    assert argvs, "no ssh argv found; the pin would be vacuous"
    for argv in argvs:
        assert '"IdentitiesOnly=yes"' in argv
        assert '"BatchMode=yes"' in argv


def test_ssh_pin_regex_catches_a_missing_option():
    argvs = re.findall(r'\[\s*"ssh"[^\]]*\]', '["ssh", "-i", k, "-o", "BatchMode=yes", r, "x"]')
    assert argvs and '"IdentitiesOnly=yes"' not in argvs[0]


def test_scan_regexes_catch_what_they_claim():
    assert IPV4.search("MAC = 10.1.2.3")
    assert USER_AT_HOST.search("someone@somehost")
    assert not USER_AT_HOST.search("ssh-ed25519 <key-body> unimatrix")
