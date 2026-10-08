"""In-process agent signing for `commit_paths` (`gpg.format=ssh`).

A FAKE agent (throwaway ed25519 key, tmp unix socket, plus a uniquely named
Windows pipe on win32) stands in for the user's agent; nothing here touches a
real agent or real key. The signed object must pass `git verify-commit`, match
the shape of a git-made signed commit, and cost zero spawns.
"""

from __future__ import annotations

import base64
import socket
import struct
import subprocess
import sys
import threading
import uuid

import pytest
load_ssh_private_key = pytest.importorskip(
    "cryptography.hazmat.primitives.serialization"
).load_ssh_private_key

from coordinator_core.git import commit as commit_mod
from coordinator_core.git import sshsig
from coordinator_core.git import run as run_mod

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_HAS_UNIX = hasattr(socket, "AF_UNIX")


@pytest.fixture(autouse=True)
def _isolated_slots(tmp_path, monkeypatch):
    monkeypatch.setattr(sshsig, "_SLOT_DIR", tmp_path / "slots")


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check, **_NOWIN
    )


def _s(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


class FakeAgent:
    """Answers request-identities and sign-request for the keys it holds."""

    def __init__(self, private_key, pub_blob: bytes):
        self.key = private_key
        self.pub = pub_blob
        self.requests: "list[int]" = []
        self.signed: "list[bytes]" = []

    def respond(self, msg: bytes) -> bytes:
        kind = msg[0]
        self.requests.append(kind)
        if kind == 11:
            return bytes([12]) + struct.pack(">I", 1) + _s(self.pub) + _s(b"fake")
        if kind == 13:
            blob, off = sshsig._read_string(msg, 1)
            data, off = sshsig._read_string(msg, off)
            if blob != self.pub:
                return bytes([5])
            self.signed.append(data)
            sig = self.key.sign(data)
            return bytes([14]) + _s(_s(b"ssh-ed25519") + _s(sig))
        return bytes([5])


class UnixServer:
    def __init__(self, path: str, agent: FakeAgent):
        self.path = path
        self.agent = agent
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(16)
        self.closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.closed:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        with conn:
            while True:
                head = self._read(conn, 4)
                if head is None:
                    return
                body = self._read(conn, struct.unpack(">I", head)[0])
                if body is None:
                    return
                reply = self.agent.respond(body)
                conn.sendall(struct.pack(">I", len(reply)) + reply)

    @staticmethod
    def _read(conn, n):
        out = b""
        while len(out) < n:
            chunk = conn.recv(n - len(out))
            if not chunk:
                return None
            out += chunk
        return out

    def close(self):
        self.closed = True
        self.sock.close()


class PipeServer:
    """Win32 named-pipe agent: one instance per connection, so clients also see pipe-busy windows."""

    def __init__(self, name: str, agent: FakeAgent):
        import ctypes
        from ctypes import wintypes

        self.name = name
        self.agent = agent
        self.closed = False
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k = k
        H, D = wintypes.HANDLE, wintypes.DWORD
        k.CreateNamedPipeW.restype = H
        k.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, D, D, D, D, D, D, ctypes.c_void_p]
        k.ConnectNamedPipe.argtypes = [H, ctypes.c_void_p]
        k.ReadFile.argtypes = [H, ctypes.c_void_p, D, ctypes.POINTER(D), ctypes.c_void_p]
        k.WriteFile.argtypes = [H, ctypes.c_char_p, D, ctypes.POINTER(D), ctypes.c_void_p]
        k.DisconnectNamedPipe.argtypes = [H]
        k.CloseHandle.argtypes = [H]
        self._ct, self._D = ctypes, D
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        k = self.k
        while not self.closed:
            h = k.CreateNamedPipeW(self.name, 3, 0, 255, 65536, 65536, 0, None)
            if h in (None, self._ct.c_void_p(-1).value):
                return
            k.ConnectNamedPipe(h, None)
            if self.closed:
                k.CloseHandle(h)
                return
            threading.Thread(target=self._serve, args=(h,), daemon=True).start()

    def _read(self, h, n):
        out = b""
        while len(out) < n:
            buf = self._ct.create_string_buffer(n - len(out))
            got = self._D(0)
            if not self.k.ReadFile(h, buf, n - len(out), self._ct.byref(got), None) or got.value == 0:
                return None
            out += buf.raw[:got.value]
        return out

    def _serve(self, h):
        k = self.k
        try:
            while True:
                head = self._read(h, 4)
                if head is None:
                    return
                body = self._read(h, struct.unpack(">I", head)[0])
                if body is None:
                    return
                reply = self.agent.respond(body)
                data = struct.pack(">I", len(reply)) + reply
                wrote = self._D(0)
                k.WriteFile(h, data, len(data), self._ct.byref(wrote), None)
        finally:
            k.DisconnectNamedPipe(h)
            k.CloseHandle(h)

    def close(self):
        self.closed = True
        try:
            open(self.name, "r+b", buffering=0).close()
        except OSError:
            pass


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global-gitconfig"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    for key in ("GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL"):
        monkeypatch.delenv(key, raising=False)
    sshsig._UNREACHABLE.clear()
    commit_mod._CONFIG_IDENTITY_MEMO.clear()
    commit_mod._GPGSIGN_CACHE.clear()
    yield
    sshsig._UNREACHABLE.clear()


@pytest.fixture
def key(tmp_path):
    keyfile = tmp_path / "signing_key"
    made = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(keyfile), "-q"],
        capture_output=True, text=True, **_NOWIN,
    )
    assert made.returncode == 0, made.stderr
    private = load_ssh_private_key(keyfile.read_bytes(), password=None)
    pub_text = keyfile.with_name("signing_key.pub").read_text(encoding="utf-8").strip()
    return keyfile, private, pub_text, base64.b64decode(pub_text.split()[1])


def _repo(tmp_path, keyfile, pub_text, *, fmt="ssh"):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work/z")
    signers = tmp_path / "allowed_signers"
    signers.write_text(f't@local namespaces="git" {pub_text}\n', encoding="utf-8")
    cfg = (
        "[user]\n\temail = t@local\n\tname = t\n"
        f"\tsigningkey = {keyfile.as_posix()}\n"
        f"[gpg]\n\tformat = {fmt}\n"
        f'[gpg "ssh"]\n\tallowedSignersFile = {signers.as_posix()}\n'
    )
    (repo / ".git" / "config").write_text(cfg, encoding="utf-8")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    _git(repo, "config", "commit.gpgsign", "true")
    (repo / "new.txt").write_text("new\n", encoding="utf-8", newline="\n")
    return repo


def _spawn_spies(monkeypatch):
    popens, git_calls = [], []
    real_popen = subprocess.Popen

    class Spy(real_popen):  # type: ignore[misc,valid-type]
        def __init__(self, cmd, *a, **kw):
            popens.append(cmd)
            super().__init__(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", Spy)
    real_run_git = run_mod.run_git

    def spying(args, **kw):
        git_calls.append(list(args))
        return real_run_git(args, **kw)

    monkeypatch.setattr(run_mod, "run_git", spying)
    return popens, git_calls


def _shape(repo, sha):
    lines = _git(repo, "cat-file", "-p", sha).stdout.split("\n")
    lines = lines[:lines.index("")]
    order =[ln.split(" ", 1)[0] for ln in lines if ln and not ln.startswith(" ")]
    return order, [len(ln) for ln in lines if ln.startswith(("gpgsig", " "))], lines


def _git_made_signed(repo, keyfile, monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", str(repo.parent / "no-such-agent"))
    tree = _git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@local", "GIT_AUTHOR_DATE": "1700000000 +0000",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@local",
        "GIT_COMMITTER_DATE": "1700000000 +0000",
    }
    import os
    out = subprocess.run(
        ["git", "commit-tree", "-S", tree, "-p", head, "-m", "x"], cwd=str(repo),
        capture_output=True, text=True, env={**os.environ, **env}, **_NOWIN,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _assert_agent_commit(repo, outcome, keyfile, monkeypatch, popens, git_calls):
    assert outcome.sign_warning is None, outcome.sign_warning
    assert popens == [], f"agent signing spawned {popens}"
    assert not any(c[:1] == ["commit-tree"] for c in git_calls)
    verify = _git(repo, "verify-commit", outcome.sha, check=False)
    assert verify.returncode == 0, verify.stderr
    assert 'Good "git" signature' in verify.stderr
    theirs = _git_made_signed(repo, keyfile, monkeypatch)
    order_a, widths_a, lines_a = _shape(repo, outcome.sha)
    order_b, widths_b, _ = _shape(repo, theirs)
    assert order_a == order_b == ["tree", "parent", "author", "committer", "gpgsig"]
    assert order_a[-1] == "gpgsig"
    assert widths_a == widths_b
    assert lines_a[order_a.index("gpgsig") + 0].startswith("gpgsig -----BEGIN SSH SIGNATURE-----")
    assert " -----END SSH SIGNATURE-----" in lines_a


@pytest.mark.skipif(not _HAS_UNIX, reason="no AF_UNIX in this Python")
def test_agent_path_signs_verifies_and_spawns_nothing(tmp_path, key, monkeypatch):
    keyfile, private, pub_text, blob = key
    agent = FakeAgent(private, blob)
    sock = str(tmp_path / "a.sock")
    server = UnixServer(sock, agent)
    try:
        repo = _repo(tmp_path, keyfile, pub_text)
        monkeypatch.setenv("SSH_AUTH_SOCK", sock)
        popens, git_calls = _spawn_spies(monkeypatch)
        outcome = commit_mod.commit_paths(repo, ["new.txt"], "add new, signed")
        popens_snapshot, calls_snapshot = list(popens), list(git_calls)
        assert agent.requests == [11, 13]
        _assert_agent_commit(repo, outcome, keyfile, monkeypatch, popens_snapshot, calls_snapshot)
    finally:
        server.close()


@pytest.mark.skipif(sys.platform != "win32", reason="named pipes exist only on Windows")
def test_agent_path_over_windows_named_pipe(tmp_path, key, monkeypatch):
    keyfile, private, pub_text, blob = key
    agent = FakeAgent(private, blob)
    name = "\\\\.\\pipe\\claude-klabauter-test-agent-" + uuid.uuid4().hex
    server = PipeServer(name, agent)
    try:
        repo = _repo(tmp_path, keyfile, pub_text)
        monkeypatch.setenv("SSH_AUTH_SOCK", name)
        popens, git_calls = _spawn_spies(monkeypatch)
        outcome = commit_mod.commit_paths(repo, ["new.txt"], "add new, signed")
        popens_snapshot, calls_snapshot = list(popens), list(git_calls)
        assert agent.requests == [11, 13]
        _assert_agent_commit(repo, outcome, keyfile, monkeypatch, popens_snapshot, calls_snapshot)
    finally:
        server.close()


def test_no_agent_falls_back_to_git_commit_tree(tmp_path, key, monkeypatch):
    keyfile, _, pub_text, _ = key
    repo = _repo(tmp_path, keyfile, pub_text)
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "absent.sock"))
    _, git_calls = _spawn_spies(monkeypatch)
    outcome = commit_mod.commit_paths(repo, ["new.txt"], "add new, signed")
    assert [c[:2] for c in git_calls] == [["commit-tree", "-S"]]
    assert outcome.sign_warning is None
    assert _git(repo, "verify-commit", outcome.sha, check=False).returncode == 0


@pytest.mark.skipif(not _HAS_UNIX, reason="no AF_UNIX in this Python")
def test_key_missing_from_agent_falls_back(tmp_path, key, monkeypatch):
    keyfile, private, pub_text, blob = key
    other = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "other"), "-q"],
        capture_output=True, **_NOWIN,
    )
    assert other.returncode == 0
    other_blob = base64.b64decode((tmp_path / "other.pub").read_text().split()[1])
    agent = FakeAgent(private, other_blob)
    sock = str(tmp_path / "b.sock")
    server = UnixServer(sock, agent)
    try:
        repo = _repo(tmp_path, keyfile, pub_text)
        monkeypatch.setenv("SSH_AUTH_SOCK", sock)
        _, git_calls = _spawn_spies(monkeypatch)
        outcome = commit_mod.commit_paths(repo, ["new.txt"], "add new, signed")
        assert agent.requests == [11]
        assert [c[:2] for c in git_calls] == [["commit-tree", "-S"]]
        assert outcome.sign_warning is None
        assert _git(repo, "verify-commit", outcome.sha, check=False).returncode == 0
    finally:
        server.close()


@pytest.mark.skipif(not _HAS_UNIX, reason="no AF_UNIX in this Python")
def test_non_ssh_format_never_contacts_the_agent(tmp_path, key, monkeypatch):
    keyfile, private, pub_text, blob = key
    agent = FakeAgent(private, blob)
    sock = str(tmp_path / "c.sock")
    server = UnixServer(sock, agent)
    try:
        repo = _repo(tmp_path, keyfile, pub_text, fmt="openpgp")
        monkeypatch.setenv("SSH_AUTH_SOCK", sock)
        _, git_calls = _spawn_spies(monkeypatch)
        outcome = commit_mod.commit_paths(repo, ["new.txt"], "add new")
        assert agent.requests == []
        assert [c[:2] for c in git_calls] == [["commit-tree", "-S"]]
        assert outcome.sign_warning is not None
    finally:
        server.close()


def test_sshsig_encoding_matches_the_wire_format(key):
    _, private, _, blob = key
    data = b"tree 0\n\nmsg\n"
    signed = sshsig.sshsig_signed_blob(data)
    assert signed.startswith(b"SSHSIG\x00\x00\x00\x03git\x00\x00\x00\x00\x00\x00\x00\x06sha512\x00\x00\x00\x40")
    assert len(signed) == 6 + 7 + 4 + 10 + 68
    sig = _s(b"ssh-ed25519") + _s(private.sign(signed))
    armored = sshsig.armor(blob, sig)
    body = armored.split("\n")
    assert body[0] == "-----BEGIN SSH SIGNATURE-----" and body[-2] == "-----END SSH SIGNATURE-----"
    assert all(len(ln) <= 70 for ln in body[1:-2])
    raw = base64.b64decode("".join(body[1:-2]))
    assert raw[:10] == b"SSHSIG" + struct.pack(">I", 1)
    pub, off = sshsig._read_string(raw, 10)
    ns, off = sshsig._read_string(raw, off)
    reserved, off = sshsig._read_string(raw, off)
    alg, off = sshsig._read_string(raw, off)
    assert (pub, ns, reserved, alg) == (blob, b"git", b"", b"sha512")
    private.public_key().verify(private.sign(signed), signed)


def test_public_key_parsing_and_config_sections(tmp_path, key, monkeypatch):
    keyfile, _, pub_text, blob = key
    assert sshsig.parse_public_key(pub_text) == blob
    assert sshsig.parse_public_key("key::" + pub_text) == blob
    assert sshsig.parse_public_key("ssh-rsa " + pub_text.split()[1]) is None
    assert sshsig.load_signing_key(str(keyfile)) == blob
    assert sshsig.load_signing_key(str(keyfile) + ".pub") == blob
    assert sshsig.load_signing_key(str(tmp_path / "missing")) is None
    repo = tmp_path / "cfg"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        '[Gpg]\n\tformat = "ssh"\n[gpg "ssh"]\n\tprogram = /x/op-ssh-sign\n[user]\n\tsigningkey = k\n',
        encoding="utf-8",
    )
    assert commit_mod._signing_config(repo) == {
        "gpg.format": "ssh", "gpg.ssh.program": "/x/op-ssh-sign", "user.signingkey": "k",
    }


def test_include_or_env_config_defers_to_git(tmp_path, key, monkeypatch):
    keyfile, _, pub_text, _ = key
    repo = tmp_path / "inc"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        f'[gpg]\n\tformat = ssh\n[user]\n\tsigningkey = {keyfile}.pub\n'
        '[includeIf "gitdir:~/work/"]\n\tpath = work.gitconfig\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(sshsig, "sign_via_agent", lambda *a, **k: pytest.fail("agent contacted"))
    assert commit_mod._sign_commit_in_process(repo, "0" * 40, None, "t", "t@l", "1 +0000", "m\n") is None
    (repo / ".git" / "config").write_text(
        f'[gpg]\n\tformat = ssh\n[user]\n\tsigningkey = {keyfile}.pub\n', encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.signingKey")
    assert commit_mod._sign_commit_in_process(repo, "0" * 40, None, "t", "t@l", "1 +0000", "m\n") is None


def test_wrong_signature_algorithm_is_refused(key, monkeypatch):
    _, _, _, blob = key
    monkeypatch.setattr(
        sshsig, "_exchange",
        lambda addr, reqs, timeout: [bytes([12]) + struct.pack(">I", 1) + _s(blob) + _s(b"c")]
        if reqs[0][0] == 11 else [bytes([14]) + _s(_s(b"ssh-rsa") + _s(b"x"))],
    )
    with pytest.raises(sshsig.AgentUnavailable):
        sshsig._sign_locked("addr", blob, b"\x0d", 1.0)


_RFC8032_1 = (
    bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"),
    b"",
    bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a3"
        "3bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    ),
)


def test_ed25519_verify_matches_rfc8032_vector_and_rejects_tamper():
    pub, msg, sig = _RFC8032_1
    assert sshsig.ed25519_verify(pub, msg, sig)
    assert not sshsig.ed25519_verify(pub, b"x", sig)
    assert not sshsig.ed25519_verify(pub, msg, sig[:-1] + bytes([sig[-1] ^ 1]))
    assert not sshsig.ed25519_verify(pub, msg, sig[:10])


def test_verify_ssh_signature_ed25519_and_cost(key):
    import time

    _, private, _, blob = key
    signed = sshsig.sshsig_signed_blob(b"payload")
    good = _s(b"ssh-ed25519") + _s(private.sign(signed))
    start = time.perf_counter()
    assert sshsig.verify_ssh_signature(blob, good, signed)
    print(f"ed25519 verify {(time.perf_counter() - start) * 1000:.1f} ms")
    bad = _s(b"ssh-ed25519") + _s(bytes(64))
    assert not sshsig.verify_ssh_signature(blob, bad, signed)
    assert not sshsig.verify_ssh_signature(blob, good, signed + b"x")
    assert not sshsig.verify_ssh_signature(_s(b"ecdsa-sha2-nistp256") + _s(b"x"), good, signed)


def test_verify_ssh_signature_rsa_sha2_512_and_cost():
    import time

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nums = private.public_key().public_numbers()

    def mpint(v: int) -> bytes:
        raw = v.to_bytes((v.bit_length() + 8) // 8, "big")
        return _s(raw)

    blob = _s(b"ssh-rsa") + mpint(nums.e) + mpint(nums.n)
    signed = sshsig.sshsig_signed_blob(b"payload")
    raw = private.sign(signed, padding.PKCS1v15(), hashes.SHA512())
    good = _s(b"rsa-sha2-512") + _s(raw)
    start = time.perf_counter()
    assert sshsig.verify_ssh_signature(blob, good, signed)
    print(f"rsa verify {(time.perf_counter() - start) * 1000:.1f} ms")
    assert not sshsig.verify_ssh_signature(blob, good, signed + b"x")
    assert not sshsig.verify_ssh_signature(blob, _s(b"rsa-sha2-512") + _s(raw[:-1] + b"\x00"), signed)
    assert not sshsig.verify_ssh_signature(blob, _s(b"ssh-rsa") + _s(raw), signed)


def test_tampered_agent_signature_is_refused(key, monkeypatch):
    _, private, _, blob = key
    data = b"tree 0\n\nmsg\n"
    request = bytes([13]) + _s(blob) + _s(sshsig.sshsig_signed_blob(data)) + struct.pack(">I", 0)
    forged = bytearray(private.sign(sshsig.sshsig_signed_blob(data)))
    forged[0] ^= 1
    monkeypatch.setattr(
        sshsig, "_exchange",
        lambda addr, reqs, timeout: [bytes([12]) + struct.pack(">I", 1) + _s(blob) + _s(b"c")]
        if reqs[0][0] == 11 else [bytes([14]) + _s(_s(b"ssh-ed25519") + _s(bytes(forged)))],
    )
    with pytest.raises(sshsig.AgentUnavailable, match="does not verify"):
        sshsig._sign_locked("addr", blob, request, 1.0)


def test_unverifiable_key_type_never_contacts_the_agent(monkeypatch):
    monkeypatch.setattr(sshsig, "_exchange", lambda *a, **k: pytest.fail("agent contacted"))
    ecdsa = _s(b"ecdsa-sha2-nistp256") + _s(b"nistp256") + _s(b"point")
    with pytest.raises(sshsig.AgentUnavailable):
        sshsig.sign_via_agent(b"x", ecdsa, env={"SSH_AUTH_SOCK": "/nowhere"})


def test_relative_signingkey_goes_to_git(tmp_path, key, monkeypatch):
    keyfile, _, pub_text, blob = key
    monkeypatch.chdir(keyfile.parent)
    assert sshsig.load_signing_key(keyfile.name) is None
    assert sshsig.load_signing_key("./" + keyfile.name) is None
    assert sshsig.load_signing_key(str(keyfile)) == blob
    assert sshsig.load_signing_key("key::" + pub_text) == blob
    repo = tmp_path / "rel"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        "[gpg]\n\tformat = ssh\n[user]\n\tsigningkey = signing_key.pub\n", encoding="utf-8",
    )
    (repo / "signing_key.pub").write_text(pub_text, encoding="utf-8")
    monkeypatch.setattr(sshsig, "sign_via_agent", lambda *a, **k: pytest.fail("agent contacted"))
    assert commit_mod._sign_commit_in_process(repo, "0" * 40, None, "t", "t@l", "1 +0000", "m\n") is None


@pytest.mark.skipif(sys.platform != "win32", reason="named pipes exist only on Windows")
def test_absent_pipe_fails_fast():
    import time

    name = "\\\\.\\pipe\\claude-klabauter-test-absent-" + uuid.uuid4().hex
    sshsig._kernel32()
    start = time.perf_counter()
    with pytest.raises(sshsig.AgentUnavailable, match="win32 error 2"):
        sshsig._exchange(name, [b"\x0b"], 1.0)
    assert time.perf_counter() - start < 0.05
