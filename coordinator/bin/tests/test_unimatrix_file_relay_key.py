"""The file key and the relay key are separate forced commands: the relay key cannot put/get, the file key can,
and a missing file key is a named refusal that leaves messaging working."""
import importlib.util
import io
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]


def load(filename, modname):
    spec = importlib.util.spec_from_file_location(modname, BIN / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = load("unimatrix-relay.py", "unimatrix_relay_key_t")
FR = load("unimatrix-file-relay.py", "unimatrix_file_relay_key_t")
F = load("unimatrix-file.py", "unimatrix_file_key_t")
FR._RELAY.append(R)

MEMBERS = [{"box": "machine-b", "host": "machine-b.local", "user": "dp", "relay_pub": "ssh-ed25519 AAAA", "from_cidrs": []}]


def wire(tmp_path):
    src = tmp_path / "src" / "set1"
    src.mkdir(parents=True)
    (src / "a.bin").write_bytes(b"payload")
    buf = io.BytesIO()
    R.pack(src, buf)
    return buf.getvalue()


def run_main(mod, argv, command, stdin, monkeypatch, home):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", command)
    monkeypatch.setattr(sys, "argv", argv)
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(stdin)))
    monkeypatch.setattr(sys, "stdout", types.SimpleNamespace(buffer=out))
    try:
        mod.main()
        code = None
    except SystemExit as ex:
        code = ex.code
    return code, out.getvalue()


@pytest.mark.parametrize("command", ["put set1", "get set1"])
def test_relay_key_forced_command_refuses_put_and_get_and_lands_nothing(tmp_path, monkeypatch, command):
    home = tmp_path / "home"
    code, out = run_main(R, ["unimatrix-relay.py", "--peer", "alpha"], command, wire(tmp_path), monkeypatch, home)
    assert code not in (0, None) and out == b""
    assert not (home / "unimatrix" / "drop").exists()


def test_file_key_forced_command_puts_then_gets_a_drop(tmp_path, monkeypatch):
    home = tmp_path / "home"
    argv = ["unimatrix-file-relay.py", "--peer", "alpha"]
    code, out = run_main(FR, argv, "put set1", wire(tmp_path), monkeypatch, home)
    assert code == 0 and b'"ok": true' in out
    assert (home / "unimatrix" / "drop" / "alpha" / "set1" / "a.bin").read_bytes() == b"payload"
    code, tar = run_main(FR, argv, "get set1", b"", monkeypatch, home)
    assert code == 0
    back = tmp_path / "back"
    assert R.unpack(io.BytesIO(tar), back) == {"a.bin": R.hashlib.sha256(b"payload").hexdigest()}


@pytest.mark.parametrize("command", ["roster", "source", "send 12", "inbox x", "membership get", "grant g n 10 -"])
def test_file_key_forced_command_refuses_every_message_mode(tmp_path, monkeypatch, command):
    code, out = run_main(FR, ["unimatrix-file-relay.py", "--peer", "alpha"], command, b"", monkeypatch, tmp_path / "home")
    assert code not in (0, None) and out == b""


def test_missing_file_key_is_a_named_refusal_while_the_relay_path_still_serves(tmp_path, monkeypatch):
    home = tmp_path / "settings"
    relay_key = home / ".secrets" / "ssh" / "unimatrix-zero"
    relay_key.parent.mkdir(parents=True)
    relay_key.write_text("relay private key")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    err = io.StringIO()
    src = tmp_path / "f.bin"
    src.write_bytes(b"x")

    def no_ssh(argv):
        raise AssertionError("ssh must not start without the file key")

    assert F.main(["put", "machine-b", str(src)], popen=no_ssh, members=MEMBERS, out=io.StringIO(), err=err) == 2
    assert "file_key_missing" in err.getvalue() and "unimatrix-file" in err.getvalue()
    code, out = run_main(R, ["unimatrix-relay.py"], "source", b"", monkeypatch, home)
    assert code is None and out == (BIN / "unimatrix-relay.py").read_bytes()


def test_put_refused_when_peer_already_holds_the_quota(tmp_path, monkeypatch):
    home = tmp_path / "home"
    held = home / "unimatrix" / "drop" / "alpha" / "old"
    held.mkdir(parents=True)
    (held / "big.bin").write_bytes(b"x" * 10)
    payload = wire(tmp_path)
    monkeypatch.setattr(FR, "MAX_PEER_DROP_BYTES", 10)
    code, out = run_main(FR, ["unimatrix-file-relay.py", "--peer", "alpha"], "put set1", payload, monkeypatch, home)
    assert code == 1 and b"peer_quota_full" in out
    assert not (home / "unimatrix" / "drop" / "alpha" / "set1").exists()
    monkeypatch.setattr(FR, "MAX_PEER_DROP_BYTES", 11)
    code, out = run_main(FR, ["unimatrix-file-relay.py", "--peer", "alpha"], "put set1", payload, monkeypatch, home)
    assert code == 0 and (home / "unimatrix" / "drop" / "alpha" / "set1").is_dir()


def test_put_refused_while_another_put_of_the_same_name_holds_the_claim(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "unimatrix" / "drop" / "alpha" / ".tmp-set1").mkdir(parents=True)
    code, out = run_main(FR, ["unimatrix-file-relay.py", "--peer", "alpha"], "put set1", wire(tmp_path), monkeypatch, home)
    assert code == 1 and b'"exists"' in out
    assert (home / "unimatrix" / "drop" / "alpha" / ".tmp-set1").is_dir()
