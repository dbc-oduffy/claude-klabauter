"""agent_pipe.agy -- Antigravity adapter: headless `agy -p ... --output-format stream-json` in, `--conversation <id> -p` out.

Messages are `step_update` events of `step_type == "agent_response"` with non-blank `text_delta`;
every other event (init, user_input, whitespace-only DONE delta, result) is not a message. The
conversation id rides on each step_update.

Every headless resume goes through `claim_resume`: a conversation whose presence lock is held (a live
interactive session) or whose resume is already running gets the text spooled to the Unimatrix
`<conv>.queue.jsonl` instead, and never a second `agy --conversation <id> -p`.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Generator, Optional, Sequence

from coordinator_core._settings_home import settings_home
from coordinator_core.agent_pipe.acp import AcpDriver
from coordinator_core.agent_pipe.contract import KIND_AGY, _pipe_root, classify_exit
from coordinator_core.agent_pipe.session_driver import ApprovalCallback

SPOOL_MAX_BYTES = 1 << 20
SPOOL_LOCK_TIMEOUT_S = 15.0
RESUME_WAIT_S = 0.25
_LOCK_POLL_S = 0.05
_CONV_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_OUTPUT_FLAGS = ["--output-format", "stream-json"]
_LEG_SCRIPTS = (
    "unimatrix-agy-leg.py",
    "unimatrix-agy-hook-launch.py",
    "unimatrix-peer-agy.py",
    "unimatrix-roster.py",
)
_GIT_VERBS = ("status", "diff", "log", "show", "rev-parse", "add", "commit")
_GIT_ALLOWED_VERBS = frozenset(_GIT_VERBS)
_INTERPRETERS = frozenset({"python", "python3"})
_SHELL_META = frozenset(";&|`$<>\n\r")
DEFAULT_ALLOW_COMMANDS: tuple[str, ...] = (
    *(f"{py} coordinator/bin/{s}" for py in ("python", "python3") for s in _LEG_SCRIPTS),
    *(f"nohup {py} coordinator/bin/{s}" for py in ("python", "python3") for s in _LEG_SCRIPTS),
    *(f"git {v}" for v in _GIT_VERBS),
)


def _settings_flags(allow_commands: Sequence[str]) -> list[str]:
    """Inline --settings granting each command prefix as `command(<prefix> *)`; headless agy auto-denies unlisted commands."""
    rules = [f"command({c} *)" for c in allow_commands]
    return ["--settings", json.dumps({"permissions": {"allow": rules}})]


def _validated(allow_commands: Sequence[str]) -> tuple[str, ...]:
    """Exact command prefixes (whitespace-normalised, multi-token allowed); refuses wildcards, shell metacharacters, a bare interpreter/git/nohup, `python -c`, and any git verb outside the local set (so never push or fetch)."""
    cleaned = tuple(" ".join(str(c).split()) for c in allow_commands)
    for c in cleaned:
        if not c or "*" in c or "(" in c or ")" in c or any(ch in _SHELL_META for ch in c):
            raise ValueError(f"agy command allowlist entry must be a plain command prefix, got {c!r}")
        tokens = c.split(" ")
        if tokens[0] == "nohup":
            tokens = tokens[1:]
        head = tokens[0] if tokens else ""
        if not tokens or head == "nohup":
            raise ValueError(f"agy command allowlist entry must not be a bare nohup, got {c!r}")
        if head in _INTERPRETERS and (len(tokens) < 2 or tokens[1].startswith("-")):
            raise ValueError(f"agy command allowlist entry must name a script path after the interpreter, got {c!r}")
        if head == "git" and (len(tokens) < 2 or tokens[1] not in _GIT_ALLOWED_VERBS):
            raise ValueError(f"agy command allowlist git entry must name a local verb {sorted(_GIT_ALLOWED_VERBS)}, got {c!r}")
    if not cleaned:
        raise ValueError("agy command allowlist must not be empty")
    return cleaned


def _agy() -> str:
    return shutil.which("agy") or "agy"


def family_stage_argv(
    prefix: Sequence[str],
    *,
    log_file: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
) -> list[str]:
    """Stream-json argv for an agy family-stage run, unrestricted per PM ruling: `--dangerously-skip-permissions`, no sandbox, shell and file permission granted.

    Single builder called by `workflow._family_argv` for agy family stages; `AgyAdapter` argv stay bypass-free. The prompt goes on stdin.
    """
    argv = [*prefix, "--input-format", "stream-json", "--output-format", "stream-json",
            "-p=", "--dangerously-skip-permissions"]
    if model:
        argv += ["--model", str(model)]
    if effort:
        argv += ["--effort", str(effort)]
    if log_file:
        argv += ["--log-file", str(log_file)]
    return argv


class SpoolRefused(OSError):
    """The conversation's spool refused a line (full, lock timeout, unsafe id)."""


def _try_lock(fh) -> bool:
    """One nonblocking exclusive byte lock on `fh`; False when another holder owns it."""
    fh.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            return False
        raise
    return True


def _unlock(fh) -> None:
    fh.seek(0)
    if sys.platform == "win32":
        import msvcrt
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def presence_dir() -> Path:
    """agy's per-conversation presence locks, beside its brain root (AGY_BRAIN_DIR)."""
    brain = os.environ.get("AGY_BRAIN_DIR")
    return (Path(brain).parent if brain else Path.home() / ".gemini" / "antigravity-cli") / "presence"


def presence_held(conversation: str) -> bool:
    """True while another process holds `presence/<conversation>.lock` (a live interactive agy session)."""
    if not _CONV_RE.fullmatch(conversation or "") or conversation in (".", ".."):
        return False
    try:
        fh = open(presence_dir() / f"{conversation}.lock", "r+b" if sys.platform == "win32" else "rb")
    except PermissionError:
        return True  # invariant: an existing lock file we cannot open is held by a sharing-denying owner
    except OSError:
        return False
    with fh:
        try:
            if not _try_lock(fh):
                return True
            _unlock(fh)
        except (OSError, ImportError):
            return False
    return False


def spool_dir() -> Path:
    """The Unimatrix agy-legs state dir the leg, hook and spool share."""
    return settings_home() / "unimatrix" / "agy-legs"


@contextlib.contextmanager
def _sidecar_lock(path: Path) -> Generator[None, None, None]:
    """Exclusive lock on the never-replaced `<path>.lock` sidecar the Unimatrix leg uses; TimeoutError past SPOOL_LOCK_TIMEOUT_S."""
    with open(f"{path}.lock", "a+b") as fh:
        deadline = time.monotonic() + SPOOL_LOCK_TIMEOUT_S
        while not _try_lock(fh):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"spool lock timed out: {path}")
            time.sleep(0.02)
        try:
            yield
        finally:
            _unlock(fh)


def spool_text(conversation: str, text: str, sender: str) -> str:
    """Append one Unimatrix-shaped `{"id","text","from"}` line to `<conv>.queue.jsonl` and return its id; SpoolRefused on refusal."""
    if not _CONV_RE.fullmatch(conversation or "") or conversation in (".", ".."):
        raise SpoolRefused(f"unsafe conversation id: {conversation!r}")
    state = spool_dir()
    path = state / f"{conversation}.queue.jsonl"
    msg_id = uuid.uuid4().hex
    line = (json.dumps({"id": msg_id, "text": text, "from": sender}) + "\n").encode()
    try:
        state.mkdir(parents=True, exist_ok=True)
        with _sidecar_lock(path):
            if path.exists() and path.stat().st_size + len(line) > SPOOL_MAX_BYTES:
                raise SpoolRefused("spool full")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        _ensure_pending_record(state, conversation)
    except SpoolRefused:
        raise
    except (OSError, TimeoutError) as exc:
        raise SpoolRefused(str(exc) or "spool write failed") from exc
    return msg_id


def take_spool(conversation: str) -> list[str]:
    """Claim and return the texts spooled for `conversation` (empty when none, unsafe id, or presence held); the claimed file is removed."""
    if not _CONV_RE.fullmatch(conversation or "") or conversation in (".", "..") or presence_held(conversation):
        return []
    path = spool_dir() / f"{conversation}.queue.jsonl"
    claimed = path.with_name(f"{path.name}.claim.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    try:
        with _sidecar_lock(path):
            os.replace(path, claimed)
    except (OSError, TimeoutError):
        return []
    texts: list[str] = []
    try:
        for raw in claimed.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get("text"), str) and row["text"]:
                texts.append(row["text"])
    finally:
        claimed.unlink(missing_ok=True)
    return texts


def _ensure_pending_record(state: Path, conversation: str) -> None:
    record = state / f"{conversation}.json"
    try:
        with _sidecar_lock(record):
            if record.exists():
                return
            tmp = record.with_name(f"{record.name}.tmp.{os.getpid()}")
            tmp.write_bytes(json.dumps({"conversation": conversation, "mode": "pending", "spooled": True}).encode())
            os.replace(tmp, record)
    except (OSError, TimeoutError):  # invariant: the line is already spooled; a missing record only hides the leg from --list
        pass


class ResumeLease:
    """Ownership of one conversation's headless-resume slot; `release()` is idempotent and callable from any thread."""

    def __init__(self, fh) -> None:
        self._fh = fh

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            _unlock(fh)
        finally:
            fh.close()

    def __enter__(self) -> "ResumeLease":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _resume_lock_path(conversation: str) -> Path:
    key = conversation if _CONV_RE.fullmatch(conversation or "") and conversation not in (".", "..") \
        else hashlib.sha256(str(conversation).encode()).hexdigest()
    return _pipe_root() / "agy-resume" / f"{key}.lock"


def claim_resume(conversation: str, text: str, sender: str, wait_s: float = RESUME_WAIT_S) -> Optional[ResumeLease]:
    """Lease to spawn a headless resume under, or None after spooling `text` (presence held, or another resume alive past `wait_s`); SpoolRefused on spool refusal."""
    def spooled() -> None:
        if text:
            spool_text(conversation, text, sender)
        return None

    if presence_held(conversation):
        return spooled()
    path = _resume_lock_path(conversation)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    deadline = time.monotonic() + wait_s
    try:
        while not _try_lock(fh):
            if time.monotonic() >= deadline:
                fh.close()
                return spooled()
            time.sleep(_LOCK_POLL_S)
    except BaseException:
        fh.close()
        raise
    lease = ResumeLease(fh)
    if presence_held(conversation):
        lease.release()
        return spooled()
    return lease


class AgyAdapter:
    """Adapter for the Antigravity CLI; argv carries scoped `command(<prefix> *)` allow rules via --settings and never a permission-bypass flag or a bare wildcard.

    `allow_commands` is the caller's list of command prefixes the leg may run (default: the unimatrix leg scripts under coordinator/bin via python/python3 or nohup of those, plus local git status/diff/log/show/rev-parse/add/commit); an entry failing `_validated` raises ValueError.
    `parse_line` records the first failure `cause` (see contract.classify_exit); stateful per run.
    """

    def __init__(self, allow_commands: Optional[Sequence[str]] = None) -> None:
        self.cause: Optional[str] = None
        self._settings = _settings_flags(_validated(DEFAULT_ALLOW_COMMANDS if allow_commands is None else allow_commands))

    def spawn_argv(self, prompt: str, conversation: Optional[str] = None) -> list[str]:
        """argv for a fresh headless run; a given `conversation` resumes it instead."""
        if conversation:
            return self.resume_argv(conversation, prompt)
        return [_agy(), "-p", prompt, *_OUTPUT_FLAGS, *self._settings]

    def resume_argv(self, conversation: str, text: str) -> list[str]:
        """argv delivering `text` into an existing agy conversation."""
        return [_agy(), "--conversation", conversation, "-p", text, *_OUTPUT_FLAGS, *self._settings]

    def parse_line(self, line: str) -> Optional[tuple[str, str]]:
        """(conversation, content) for an agent_response step_update carrying text, else None."""
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return None
        if not isinstance(event, dict):
            return None
        if event.get("event") != "step_update":
            self.cause = self.cause or classify_exit(line)
            return None
        step = event.get("step_update")
        if not isinstance(step, dict) or step.get("step_type") != "agent_response":
            return None
        text = step.get("text_delta")
        conversation = step.get("conversation_id")
        if not isinstance(text, str) or not text.strip() or not isinstance(conversation, str) or not conversation:
            return None
        return conversation, text.strip()


class AgyAcpDriver(AcpDriver):
    """SessionDriver for agy: the generic ACP client over `agy acp`; `argv` is overridable so tests drive a fake agent."""

    def __init__(self, approve: Optional[ApprovalCallback] = None, argv: Optional[list[str]] = None, **kw) -> None:
        super().__init__(list(argv) if argv is not None else [_agy(), "acp"], approve, kind=KIND_AGY, **kw)
