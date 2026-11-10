"""Tests for unimatrix-passphrase.py: the 1Password source, askpass feed, encrypted mint and agent load."""
import base64
import importlib.util
import io
import os
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.complete

_SRC = Path(__file__).resolve().parents[1] / "unimatrix-passphrase.py"
_spec = importlib.util.spec_from_file_location("unimatrix_passphrase", _SRC)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

SECRET = "correct-horse-battery-staple-9"


def test_import_leaves_sys_path_unchanged():
    before = list(sys.path)
    spec = importlib.util.spec_from_file_location("unimatrix_passphrase_inert", _SRC)
    spec.loader.exec_module(importlib.util.module_from_spec(spec))
    assert sys.path == before


def test_main_askpass_subcommand_dispatches_to_askpass_main(monkeypatch):
    seen = []
    monkeypatch.setattr(pp, "askpass_main", lambda argv: seen.append(argv) or 7)
    assert pp.main(["askpass", "Enter passphrase: "]) == 7
    assert seen == [["Enter passphrase: "]]


def test_main_with_ref_env_runs_askpass_without_subcommand(monkeypatch):
    seen = []
    monkeypatch.setattr(pp, "askpass_main", lambda argv: seen.append(argv) or 0)
    monkeypatch.setenv(pp.REF_ENV, "op://x")
    assert pp.main(["prompt text"]) == 0
    assert seen == [[]]


def test_main_without_ref_env_prints_usage_and_returns_2(monkeypatch, capsys):
    monkeypatch.delenv(pp.REF_ENV, raising=False)
    assert pp.main([]) == 2
    assert capsys.readouterr().out.strip()


def _cp(rc=0, out="", err=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


class FakeOp:
    """Scripted `op` plus `_machine_local.py get`; records every argv."""

    def __init__(self, vault="Vault", item_exists=True, signed_in=True, stderr_locked="You are not currently signed in."):
        self.calls, self.vault, self.exists, self.signed_in, self.locked_err = [], vault, item_exists, signed_in, stderr_locked

    def __call__(self, cmd, **kw):
        self.calls.append((list(cmd), kw))
        if cmd[0] == "op":
            if not self.signed_in:
                return _cp(1, "", self.locked_err)
            if cmd[1] in ("read", "whoami"):
                if cmd[1] == "read" and not self.exists:
                    return _cp(1, "", '"unimatrix-x" isn\'t an item in the "Vault" vault.')
                return _cp(0, SECRET if cmd[1] == "read" else "user")
            if cmd[1] == "item" and cmd[2] == "create":
                self.exists = True
                return _cp(0)
            if cmd[1] == "item" and cmd[2] == "delete":
                existed, self.exists = self.exists, False
                return _cp(0) if existed else _cp(1, "", "isn't an item")
        if cmd[-2:] == ["get", pp.VAULT_KEY]:
            return _cp(0, self.vault + "\n") if self.vault else _cp(1)
        raise AssertionError(cmd)


@pytest.fixture
def home(tmp_path, monkeypatch):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "_machine_local.py").write_text("# fixture", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    return tmp_path


def test_op_ref_uses_machine_local_vault(home):
    assert pp.op_ref("far", FakeOp(vault="Infra")) == "op://Infra/unimatrix-far/password"


def test_op_ref_unset_vault_names_the_key(home):
    with pytest.raises(pp.PassphraseSourceMissing, match="unimatrix.passphrase_vault"):
        pp.op_ref("far", FakeOp(vault=""))


def test_missing_op_cli_is_a_typed_refusal(home):
    def run(cmd, **kw):
        if cmd[0] == "op":
            raise FileNotFoundError("op")
        return FakeOp()(cmd, **kw)
    with pytest.raises(pp.PassphraseSourceMissing) as ei:
        pp.read_passphrase("op://V/unimatrix-x/password", run)
    assert str(ei.value).startswith("passphrase_source_missing: op CLI")
    assert pp.state(run) == "missing"


def test_state_ready_and_locked(home):
    assert pp.state(FakeOp()) == "ready"
    assert pp.state(FakeOp(signed_in=False)) == "locked"


def test_read_passphrase_locked_raises_typed(home):
    with pytest.raises(pp.OnePasswordLocked, match="^1password_locked"):
        pp.read_passphrase("op://V/unimatrix-x/password", FakeOp(signed_in=False))


def test_read_passphrase_puts_value_only_in_return(home):
    op = FakeOp()
    assert pp.read_passphrase("op://V/unimatrix-x/password", op) == SECRET
    assert all(SECRET not in " ".join(c) for c, _ in op.calls)


def test_ensure_item_present_does_not_create(home):
    op = FakeOp(item_exists=True)
    assert pp.ensure_item("x", op) == "op://Vault/unimatrix-x/password"
    assert not [c for c, _ in op.calls if c[:3] == ["op", "item", "create"]]


def test_ensure_item_absent_creates_with_generated_password_not_argv_value(home):
    op = FakeOp(item_exists=False)
    ref = pp.ensure_item("x", op)
    create = [c for c, _ in op.calls if c[:3] == ["op", "item", "create"]][0]
    assert ref == "op://Vault/unimatrix-x/password"
    assert any(a.startswith("--generate-password") for a in create)
    assert "--title=unimatrix-x" in create and "--vault=Vault" in create


def test_ensure_item_locked_raises(home):
    with pytest.raises(pp.OnePasswordLocked):
        pp.ensure_item("x", FakeOp(signed_in=False))


def test_delete_item_removes_and_tolerates_absent(home):
    op = FakeOp(item_exists=True)
    pp.delete_item("x", op)
    assert op.exists is False
    pp.delete_item("x", op)


def test_delete_item_locked_raises(home):
    with pytest.raises(pp.OnePasswordLocked):
        pp.delete_item("x", FakeOp(signed_in=False))


def test_askpass_env_carries_ref_not_value():
    env = pp.askpass_env("op://V/unimatrix-x/password", platform="linux")
    assert env["SSH_ASKPASS_REQUIRE"] == "force"
    assert env[pp.REF_ENV] == "op://V/unimatrix-x/password"
    assert Path(env["SSH_ASKPASS"]).name == "unimatrix-passphrase.py"
    assert SECRET not in "".join(env.values())


def test_askpass_env_refused_on_windows():
    with pytest.raises(RuntimeError, match="askpass_unsupported"):
        pp.askpass_env("op://V/i/password", platform="win32")


def test_askpass_main_prints_value_from_env_ref(monkeypatch, home):
    monkeypatch.setenv(pp.REF_ENV, "op://Vault/unimatrix-x/password")
    out = io.StringIO()
    assert pp.askpass_main(['Enter passphrase for "k": '], FakeOp(), out) == 0
    assert out.getvalue() == SECRET + "\n"


def test_askpass_main_locked_exits_nonzero_without_value(monkeypatch, home):
    monkeypatch.setenv(pp.REF_ENV, "op://Vault/unimatrix-x/password")
    out = io.StringIO()
    assert pp.askpass_main([], FakeOp(signed_in=False), out) == 1
    assert out.getvalue() == ""


def test_mint_key_writes_encrypted_key_and_pub(tmp_path):
    priv = tmp_path / "ssh" / "unimatrix-far"
    line = pp.mint_key(priv, "unimatrix", SECRET)
    assert pp.is_encrypted(priv)
    assert line.startswith("ssh-ed25519 ") and line.endswith(" unimatrix")
    assert (tmp_path / "ssh" / "unimatrix-far.pub").read_text().strip() == line
    assert SECRET.encode() not in priv.read_bytes()
    from cryptography.hazmat.primitives import serialization as ser
    with pytest.raises(TypeError):
        ser.load_ssh_private_key(priv.read_bytes(), None)
    ser.load_ssh_private_key(priv.read_bytes(), SECRET.encode())
    with pytest.raises(ValueError):
        ser.load_ssh_private_key(priv.read_bytes(), b"wrong")


def test_mint_key_never_overwrites_and_refuses_empty(tmp_path):
    priv = tmp_path / "k"
    pp.mint_key(priv, "c", SECRET)
    with pytest.raises(RuntimeError, match="never overwritten"):
        pp.mint_key(priv, "c", SECRET)
    with pytest.raises(RuntimeError, match="empty passphrase"):
        pp.mint_key(tmp_path / "k2", "c", "")


def test_minted_key_reads_back_with_ssh_keygen(tmp_path):
    import shutil
    if not shutil.which("ssh-keygen"):
        pytest.skip("no ssh-keygen")
    priv = tmp_path / "k"
    line = pp.mint_key(priv, "c", SECRET)
    r = subprocess.run(["ssh-keygen", "-lf", str(priv) + ".pub"], capture_output=True, text=True)
    assert r.returncode == 0 and pp.public_fingerprint(str(priv) + ".pub") in r.stdout
    assert line.split()[1] in (tmp_path / "k.pub").read_text()
    empty = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", str(priv)], capture_output=True, text=True)
    right = subprocess.run(["ssh-keygen", "-y", "-P", SECRET, "-f", str(priv)], capture_output=True, text=True)
    if sys.platform == "win32":
        assert empty.returncode != 0 and "incorrect passphrase" in empty.stderr
        assert right.stdout.split()[1] == line.split()[1]


def test_is_encrypted_false_for_plain_key_and_missing_file(tmp_path):
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    plain = tmp_path / "plain"
    plain.write_bytes(Ed25519PrivateKey.generate().private_bytes(
        ser.Encoding.PEM, ser.PrivateFormat.OpenSSH, ser.NoEncryption()))
    assert pp.is_encrypted(plain) is False
    assert pp.is_encrypted(tmp_path / "absent") is False


def test_agent_add_sends_add_identity_and_accepts_success(tmp_path):
    priv = tmp_path / "unimatrix-far"
    pp.mint_key(priv, "unimatrix", SECRET)
    sent = []

    class Pipe(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def write(self, b):
            sent.append(bytes(b))
            return len(b)

    reply = Pipe(struct.pack(">I", 1) + b"\x06")
    pp.agent_add(priv, SECRET, pipe_open=lambda: reply)
    msg = sent[0]
    assert struct.unpack(">I", msg[:4])[0] == len(msg) - 4 and msg[4] == 0x11
    assert b"ssh-ed25519" in msg and SECRET.encode() not in msg


def test_agent_add_refusal_raises(tmp_path):
    priv = tmp_path / "k"
    pp.mint_key(priv, "c", SECRET)

    class Pipe(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with pytest.raises(RuntimeError, match="agent refused"):
        pp.agent_add(priv, SECRET, pipe_open=lambda: Pipe(struct.pack(">I", 1) + b"\x05"))


class Router:
    """FakeOp plus a scripted ssh-add."""

    def __init__(self, op, listed=""):
        self.op, self.listed, self.adds = op, listed, []

    def __call__(self, cmd, **kw):
        if cmd[0] == "ssh-add":
            if cmd[1:] == ["-l"]:
                return _cp(0 if self.listed else 1, self.listed)
            self.adds.append((cmd, kw))
            return _cp(0)
        return self.op(cmd, **kw)


def _minted(home, alias="far"):
    priv = home / ".secrets" / "ssh" / f"unimatrix-{alias}"
    pp.mint_key(priv, "unimatrix", SECRET)
    return priv


def test_ensure_agent_unencrypted_key(home, tmp_path):
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = tmp_path / "unimatrix-plain"
    k.write_bytes(Ed25519PrivateKey.generate().private_bytes(ser.Encoding.PEM, ser.PrivateFormat.OpenSSH, ser.NoEncryption()))
    assert pp.ensure_agent(k, Router(FakeOp())) == "unencrypted"


def test_ensure_agent_already_loaded_skips_op(home):
    priv = _minted(home)
    r = Router(FakeOp(), listed=f"256 {pp.public_fingerprint(str(priv) + '.pub')} unimatrix (ED25519)")
    assert pp.ensure_agent(priv, r) == "loaded"
    assert not r.adds and not r.op.calls


def test_ensure_agent_locked_and_missing(home):
    priv = _minted(home)
    assert pp.ensure_agent(priv, Router(FakeOp(signed_in=False))) == "locked"
    assert pp.ensure_agent(priv, Router(FakeOp(vault=""))) == "missing"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ssh-add askpass branch")
def test_ensure_agent_posix_loads_via_ssh_add_askpass(home):
    priv = _minted(home)
    r = Router(FakeOp())
    assert pp.ensure_agent(priv, r) == "loaded"
    cmd, kw = r.adds[0]
    assert cmd == ["ssh-add", str(priv)] and SECRET not in " ".join(cmd)
    assert kw["env"]["SSH_ASKPASS_REQUIRE"] == "force" and kw["env"][pp.REF_ENV].startswith("op://Vault/unimatrix-far")
    assert SECRET not in "".join(str(v) for v in kw["env"].values())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows in-process agent branch")
def test_ensure_agent_windows_uses_agent_protocol(home, monkeypatch):
    priv = _minted(home)
    seen = []
    monkeypatch.setattr(pp, "agent_add", lambda k, p: seen.append((k, p)))
    r = Router(FakeOp())
    assert pp.ensure_agent(priv, r) == "loaded"
    assert seen == [(priv, SECRET)] and not r.adds


def test_script_entry_with_ref_env_and_prompt_arg_takes_askpass_path_not_usage(monkeypatch):
    script = Path(pp.__file__)
    env = {**os.environ, pp.REF_ENV: "op://Vault/none/password", "PATH": os.devnull}
    with_ref = subprocess.run([sys.executable, str(script), 'Enter passphrase for "k": '],
                              env=env, capture_output=True, text=True, timeout=30)
    assert "askpass:" in with_ref.stderr and with_ref.returncode == 1
    env.pop(pp.REF_ENV)
    no_ref = subprocess.run([sys.executable, str(script), 'Enter passphrase for "k": '],
                            env=env, capture_output=True, text=True, timeout=30)
    assert no_ref.returncode == 2 and "askpass:" not in no_ref.stderr
