"""In-process SSHSIG signing through the ssh-agent protocol: zero spawns, no private key material.

`sign_via_agent(data, pub_blob)` returns the armored `-----BEGIN SSH SIGNATURE-----` block
(namespace `git`, hash sha512) that `git verify-commit` accepts, or raises `AgentUnavailable`
for every condition the caller should answer by falling back to `git commit-tree -S`
(no agent, key not loaded, agent refused, timeout).
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional, Tuple

WINDOWS_PIPE_PREFIX = "\\\\.\\pipe\\"
WINDOWS_PIPE = WINDOWS_PIPE_PREFIX + "openssh-ssh-agent"

_SSH_AGENTC_REQUEST_IDENTITIES = 11
_SSH_AGENT_IDENTITIES_ANSWER = 12
_SSH_AGENTC_SIGN_REQUEST = 13
_SSH_AGENT_SIGN_RESPONSE = 14
_RSA_SHA2_512 = 4

_MAX_MESSAGE = 256 * 1024
_CONNECT_ATTEMPTS = 100
_CONNECT_PAUSE_S = 0.005
_UNREACHABLE_TTL_S = 30.0
_DEFAULT_TIMEOUT_S = 15.0

_UNREACHABLE: "dict[str, float]" = {}

MAX_IN_FLIGHT = 8
_SLOT_WAIT_S = 1.0
_SLOT_POLL_S = 0.01
_SLOT_DIR: Optional[Path] = None


class AgentUnavailable(Exception):
    """The agent path cannot produce this signature; use the git signing path."""


def _s(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def _read_string(buf: bytes, off: int) -> Tuple[bytes, int]:
    (n,) = struct.unpack_from(">I", buf, off)
    return buf[off + 4:off + 4 + n], off + 4 + n


def parse_public_key(text: str) -> Optional[bytes]:
    """Key blob from a literal ``[key::]<type> <base64> [comment]`` line, or None."""
    text = text.strip()
    if text.startswith("key::"):
        text = text[5:]
    parts = text.split()
    if len(parts) < 2:
        return None
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except ValueError:
        return None
    try:
        key_type, _ = _read_string(blob, 0)
    except struct.error:
        return None
    return blob if key_type.decode("ascii", "replace") == parts[0] else None


def load_signing_key(signingkey: str) -> Optional[bytes]:
    """Public key blob for a ``user.signingkey`` value (literal key or .pub path); None if unusable.

    A private-key path resolves to its sibling ``.pub``; no private bytes are ever read.
    A relative path is None: git alone knows what it is relative to.
    """
    value = signingkey.strip()
    if not value:
        return None
    literal = parse_public_key(value)
    if literal is not None:
        return literal
    path = Path(os.path.expanduser(value))
    if not path.is_absolute():
        return None
    candidates = [path] if path.suffix == ".pub" else [path.with_name(path.name + ".pub")]
    for candidate in candidates:
        try:
            for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
                blob = parse_public_key(line)
                if blob is not None:
                    return blob
        except OSError:
            continue
    return None


def agent_address(env: "os._Environ[str] | dict") -> Optional[str]:
    sock = env.get("SSH_AUTH_SOCK")
    if sock:
        return sock
    return WINDOWS_PIPE if os.name == "nt" else None


_ERROR_PIPE_BUSY = 231
_PIPE_BUSY_WAIT_MS = 20
_PIPE_BUSY_ATTEMPTS = 50
_k32 = None


def _kernel32():
    global _k32
    if _k32 is None:
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.restype = wintypes.HANDLE
        k.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        k.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        k.WaitNamedPipeW.restype = wintypes.BOOL
        _k32 = k
    return _k32


def _open_pipe(path: str):
    """Connect to a named pipe: an absent pipe fails at once, only PIPE_BUSY is waited out."""
    import ctypes
    import msvcrt

    k = _kernel32()
    invalid = ctypes.c_void_p(-1).value
    for _ in range(_PIPE_BUSY_ATTEMPTS):
        handle = k.CreateFileW(path, 0xC0000000, 0, None, 3, 0, None)
        if handle not in (None, invalid):
            fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
            return os.fdopen(fd, "r+b", buffering=0)
        err = ctypes.get_last_error()
        if err != _ERROR_PIPE_BUSY:
            raise AgentUnavailable(f"agent pipe {path} unreachable (win32 error {err})")
        k.WaitNamedPipeW(path, _PIPE_BUSY_WAIT_MS)
    raise AgentUnavailable(f"agent pipe {path} stayed busy")


def _exchange_pipe(path: str, requests: "List[bytes]") -> "List[bytes]":
    f = _open_pipe(path)
    with f:
        def read_exact(n: int) -> bytes:
            out = b""
            while len(out) < n:
                chunk = f.read(n - len(out))
                if not chunk:
                    raise AgentUnavailable("agent closed the connection")
                out += chunk
            return out

        replies = []
        for msg in requests:
            f.write(struct.pack(">I", len(msg)) + msg)
            (n,) = struct.unpack(">I", read_exact(4))
            if n > _MAX_MESSAGE:
                raise AgentUnavailable("agent reply too large")
            replies.append(read_exact(n))
        return replies


def _exchange_unix(path: str, requests: "List[bytes]", timeout: float) -> "List[bytes]":
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)

        def read_exact(n: int) -> bytes:
            out = b""
            while len(out) < n:
                chunk = sock.recv(n - len(out))
                if not chunk:
                    raise AgentUnavailable("agent closed the connection")
                out += chunk
            return out

        replies = []
        for msg in requests:
            sock.sendall(struct.pack(">I", len(msg)) + msg)
            (n,) = struct.unpack(">I", read_exact(4))
            if n > _MAX_MESSAGE:
                raise AgentUnavailable("agent reply too large")
            replies.append(read_exact(n))
        return replies
    finally:
        sock.close()


def _exchange(address: str, requests: "List[bytes]", timeout: float) -> "List[bytes]":
    """Requests ride one connection, in order. Callers bound the call with `_run_bounded`."""
    try:
        if address.startswith(WINDOWS_PIPE_PREFIX):
            return _exchange_pipe(address, requests)
        return _exchange_unix(address, requests, timeout)
    except AgentUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 -- any transport fault answers with the git path
        raise AgentUnavailable(f"agent transport failed: {exc!r}")


def _run_bounded(fn, timeout: float, release):
    """Run ``fn`` on a daemon thread, waiting at most ``timeout``. ``release`` runs in the
    worker's ``finally``: a timed-out worker keeps its slot until it actually finishes."""
    box: dict = {}

    def run() -> None:
        try:
            box["ok"] = fn()
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the caller's thread
            box["err"] = exc
        finally:
            release()

    worker = threading.Thread(target=run, daemon=True)
    try:
        worker.start()
    except BaseException:
        release()
        raise
    worker.join(timeout)
    if worker.is_alive():
        raise AgentUnavailable(f"agent did not answer within {timeout:.0f}s")
    if "err" in box:
        raise box["err"]
    return box["ok"]


def _identities(reply: bytes) -> "List[bytes]":
    if not reply or reply[0] != _SSH_AGENT_IDENTITIES_ANSWER:
        raise AgentUnavailable("agent did not answer request-identities")
    (count,) = struct.unpack_from(">I", reply, 1)
    off = 5
    blobs = []
    for _ in range(count):
        blob, off = _read_string(reply, off)
        _, off = _read_string(reply, off)
        blobs.append(blob)
    return blobs


def sshsig_signed_blob(data: bytes, namespace: bytes = b"git") -> bytes:
    """The preimage the agent signs: magic, namespace, reserved, hash algorithm, H(data)."""
    return b"SSHSIG" + _s(namespace) + _s(b"") + _s(b"sha512") + _s(hashlib.sha512(data).digest())


def armor(pub_blob: bytes, agent_signature: bytes, namespace: bytes = b"git") -> str:
    blob = (
        b"SSHSIG" + struct.pack(">I", 1) + _s(pub_blob) + _s(namespace) + _s(b"")
        + _s(b"sha512") + _s(agent_signature)
    )
    b64 = base64.b64encode(blob).decode("ascii")
    lines = [b64[i:i + 70] for i in range(0, len(b64), 70)]
    return "-----BEGIN SSH SIGNATURE-----\n" + "\n".join(lines) + "\n-----END SSH SIGNATURE-----\n"


def _try_lock(fd: int) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _acquire_slot() -> int:
    directory = _SLOT_DIR or Path(tempfile.gettempdir()) / "claude-klabauter-sshsig-slots"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AgentUnavailable(f"slot dir unusable: {exc}")
    deadline = time.monotonic() + _SLOT_WAIT_S
    while True:
        for n in range(MAX_IN_FLIGHT):
            try:
                # O_NOFOLLOW: a shared temp dir must not let a planted symlink pick the file we open.
                cand = os.open(
                    str(directory / f"slot-{n}"),
                    os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
            except OSError:
                continue
            if _try_lock(cand):
                return cand
            os.close(cand)
        if time.monotonic() >= deadline:
            raise AgentUnavailable("all agent slots busy")
        time.sleep(_SLOT_POLL_S)


@contextmanager
def agent_slot():
    """One of ``MAX_IN_FLIGHT`` machine-wide agent-request slots, held via OS file locks
    (released on process death). Raises `AgentUnavailable` if none frees within ``_SLOT_WAIT_S``."""
    fd = _acquire_slot()
    try:
        yield
    finally:
        os.close(fd)  # closing releases the lock


def sign_via_agent(
    data: bytes,
    pub_blob: bytes,
    env: "Optional[dict]" = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
) -> str:
    """Armored SSHSIG over ``data`` (namespace ``git``, sha512) made by the agent holding ``pub_blob``."""
    address = agent_address(os.environ if env is None else env)
    if address is None:
        raise AgentUnavailable("no ssh agent address (SSH_AUTH_SOCK unset)")
    if _UNREACHABLE.get(address, 0.0) > time.monotonic():
        raise AgentUnavailable(f"agent {address} recently unreachable")

    key_type, _ = _read_string(pub_blob, 0)
    if key_type not in (b"ssh-ed25519", b"ssh-rsa"):
        raise AgentUnavailable(f"key type {key_type!r} is not verified in-process")
    flags = _RSA_SHA2_512 if key_type == b"ssh-rsa" else 0
    sign_request = (
        bytes([_SSH_AGENTC_SIGN_REQUEST]) + _s(pub_blob)
        + _s(sshsig_signed_blob(data)) + struct.pack(">I", flags)
    )

    fd = _acquire_slot()
    try:
        return _run_bounded(
            lambda: _sign_locked(address, pub_blob, sign_request, timeout),
            2 * timeout,
            lambda: os.close(fd),
        )
    except AgentUnavailable as exc:
        if "did not answer" in str(exc):
            _UNREACHABLE[address] = time.monotonic() + _UNREACHABLE_TTL_S
        raise


def _sign_locked(address: str, pub_blob: bytes, sign_request: bytes, timeout: float) -> str:
    # Probe first, as its own exchange: a missing key must fail before any sign request
    # (slow, possibly prompting) reaches the agent.
    try:
        (ids_reply,) = _exchange(address, [bytes([_SSH_AGENTC_REQUEST_IDENTITIES])], timeout)
    except AgentUnavailable:
        _UNREACHABLE[address] = time.monotonic() + _UNREACHABLE_TTL_S
        raise
    if pub_blob not in _identities(ids_reply):
        raise AgentUnavailable("signing key is not loaded in the agent")

    (reply,) = _exchange(address, [sign_request], timeout)
    if not reply or reply[0] != _SSH_AGENT_SIGN_RESPONSE:
        raise AgentUnavailable("agent refused to sign")
    signature, _ = _read_string(reply, 1)
    # ssh-keygen -Y verify rejects SHA-1 `ssh-rsa` signatures, and a signature whose
    # algorithm does not match the key cannot verify; either must take the git path,
    # never land as an unverifiable gpgsig.
    key_type, _ = _read_string(pub_blob, 0)
    sig_type, _ = _read_string(signature, 0)
    if sig_type != _expected_sig_type(key_type):
        raise AgentUnavailable(f"agent signed with {sig_type!r}, expected key type {key_type!r}")
    signed, _ = _read_string(sign_request, _read_string(sign_request, 1)[1])
    if not verify_ssh_signature(pub_blob, signature, signed):
        raise AgentUnavailable("agent signature does not verify against the signing key")
    return armor(pub_blob, signature)


def _expected_sig_type(key_type: bytes) -> bytes:
    return b"rsa-sha2-512" if key_type == b"ssh-rsa" else key_type


_P = 2**255 - 19
_Q = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _ed_add(a, b):
    A = (a[1] - a[0]) * (b[1] - b[0]) % _P
    B = (a[1] + a[0]) * (b[1] + b[0]) % _P
    C = 2 * a[3] * b[3] * _D % _P
    D = 2 * a[2] * b[2] % _P
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _ed_mul(s: int, pt):
    acc = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            acc = _ed_add(acc, pt)
        pt = _ed_add(pt, pt)
        s >>= 1
    return acc


def _ed_equal(a, b) -> bool:
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _ed_recover_x(y: int, sign: int) -> Optional[int]:
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P:
        return None
    return _P - x if (x & 1) != sign else x


def _ed_decompress(raw: bytes):
    y = int.from_bytes(raw, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _ed_recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


_ED_BASE_Y = 4 * pow(5, _P - 2, _P) % _P
_ED_BASE_X = _ed_recover_x(_ED_BASE_Y, 0)
_ED_BASE = (_ED_BASE_X, _ED_BASE_Y, 1, _ED_BASE_X * _ED_BASE_Y % _P)


def ed25519_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """RFC 8032 section 5.1.7 verification, pure stdlib."""
    if len(public) != 32 or len(signature) != 64:
        return False
    a_pt = _ed_decompress(public)
    r_pt = _ed_decompress(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if a_pt is None or r_pt is None or s >= _Q:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % _Q
    return _ed_equal(_ed_mul(s, _ED_BASE), _ed_add(r_pt, _ed_mul(h, a_pt)))


_SHA512_DIGEST_INFO = bytes.fromhex("3051300d060960864801650304020305000440")


def rsa_sha512_verify(n: int, e: int, message: bytes, signature: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 with SHA-512 (RFC 8017 section 8.2.2), pure stdlib."""
    k = (n.bit_length() + 7) // 8
    tail = _SHA512_DIGEST_INFO + hashlib.sha512(message).digest()
    if len(signature) != k or k < len(tail) + 11:
        return False
    sig_int = int.from_bytes(signature, "big")
    if sig_int >= n:
        return False
    em = pow(sig_int, e, n).to_bytes(k, "big")
    return em == b"\x00\x01" + b"\xff" * (k - len(tail) - 3) + b"\x00" + tail


def verify_ssh_signature(pub_blob: bytes, signature: bytes, signed: bytes) -> bool:
    """True iff ``signature`` (an SSH signature blob) is valid over ``signed`` for ``pub_blob``.
    Only ssh-ed25519 and rsa-sha2-512 are checked; every other type is False."""
    try:
        key_type, off = _read_string(pub_blob, 0)
        sig_type, soff = _read_string(signature, 0)
        raw, _ = _read_string(signature, soff)
        if key_type == b"ssh-ed25519" and sig_type == b"ssh-ed25519":
            public, _ = _read_string(pub_blob, off)
            return ed25519_verify(public, signed, raw)
        if key_type == b"ssh-rsa" and sig_type == b"rsa-sha2-512":
            e_raw, off = _read_string(pub_blob, off)
            n_raw, _ = _read_string(pub_blob, off)
            return rsa_sha512_verify(
                int.from_bytes(n_raw, "big"), int.from_bytes(e_raw, "big"), signed, raw,
            )
    except (struct.error, ValueError):
        return False
    return False
