"""Session-stamped spawn ledger: one JSON row per detached pid, plus the detach-shape classifier.

Layout: settings_home()/spawn-ledger/<session_id>/<pid>.json. A row answers "did this session launch
this pid at this create time". Every reader and writer is fail-soft; only `ledger_root` raises.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, List, Literal, Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.bash_guards._command_tokenizer import (
    _peel_command_position,
    normalize_executable_basename,
    tokenize_full_command,
)

LEDGER_RELPATH = "spawn-ledger"
SETTINGS_HOME_KEY = "COORDINATOR_SETTINGS_HOME"

# session_id becomes a directory name: no separator, dot or drive colon may pass.
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

DetachShape = Literal["none", "recordable", "opaque"]

# Cheap prefilter: text with none of these cannot carry a detach shape.
_DETACH_WORD_RE = re.compile(r"&|nohup|setsid|disown|start-process|\bsaps\b", re.IGNORECASE)
_START_PROCESS_NAMES = frozenset({"start-process", "saps"})
_DETACH_WRAPPERS = frozenset({"nohup", "setsid"})
_SHELLS_WITH_C = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_C_FLAG_RE = re.compile(r"^-[a-zA-Z]*c[a-zA-Z]*$")
_START_PROCESS_PATH_FLAGS = frozenset({"-filepath", "-path"})


class SpawnLedgerUnavailable(RuntimeError):
    """The ledger root cannot be resolved; the message names the settings-home key."""


@dataclass(frozen=True)
class LedgerRow:
    """One detached pid. `create_time` is psutil's float epoch seconds, compared by equality."""

    pid: int
    create_time: float
    session_id: str
    agent_id: str
    argv: List[str]


def valid_session_id(session_id: object) -> bool:
    return isinstance(session_id, str) and _SESSION_ID_RE.fullmatch(session_id) is not None


def ledger_root() -> Path:
    """settings_home()/spawn-ledger; raises SpawnLedgerUnavailable naming the key when unresolvable."""
    try:
        return settings_home() / LEDGER_RELPATH
    except (ValueError, OSError) as exc:
        raise SpawnLedgerUnavailable(
            f"spawn ledger root unresolved: set {SETTINGS_HOME_KEY} to an absolute path ({exc})"
        ) from exc


def _row_path(session_id: str, pid: int) -> Path:
    return ledger_root() / session_id / f"{int(pid)}.json"


def _load(path: Path) -> Optional[LedgerRow]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return LedgerRow(
            pid=int(d["pid"]),
            create_time=float(d["create_time"]),
            session_id=str(d["session_id"]),
            agent_id=str(d.get("agent_id", "")),
            argv=[str(a) for a in d.get("argv", [])],
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _reap(session_dir: Path) -> None:
    """Delete rows of `session_dir` whose pid is dead or reused: one listing, no process spawn."""
    try:
        import psutil
    except ImportError:
        return
    try:
        entries = list(os.scandir(session_dir))
    except OSError:
        return
    for entry in entries:
        if not entry.name.endswith(".json"):
            continue
        row = _load(Path(entry.path))
        stale = row is None
        if row is not None:
            try:
                stale = psutil.Process(row.pid).create_time() != row.create_time
            except psutil.NoSuchProcess:
                stale = True
            except (psutil.Error, OSError):
                stale = False
        if stale:
            try:
                os.unlink(entry.path)
            except OSError:
                pass


def write_row(row: LedgerRow) -> bool:
    """Atomically persist `row`, then reap its session's dead rows. False when refused or on I/O error."""
    if not valid_session_id(row.session_id) or not isinstance(row.pid, int) or row.pid <= 0:
        return False
    try:
        path = _row_path(row.session_id, row.pid)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, json.dumps(asdict(row)).encode("utf-8"))
        _reap(path.parent)
    except (SpawnLedgerUnavailable, OSError):
        return False
    return True


def read_row(session_id: str, pid: int) -> Optional[LedgerRow]:
    if not valid_session_id(session_id):
        return None
    try:
        return _load(_row_path(session_id, pid))
    except SpawnLedgerUnavailable:
        return None


def find_row(pid: int) -> Optional[LedgerRow]:
    """The row for `pid` in any session; names a foreign-session refusal. Never an admission source."""
    try:
        sessions = sorted(os.scandir(ledger_root()), key=lambda e: e.name)
    except (SpawnLedgerUnavailable, OSError):
        return None
    for entry in sessions:
        row = _load(Path(entry.path) / f"{int(pid)}.json")
        if row is not None:
            return row
    return None


def delete_row(session_id: str, pid: int) -> None:
    if not valid_session_id(session_id):
        return
    try:
        _row_path(session_id, pid).unlink(missing_ok=True)
    except (SpawnLedgerUnavailable, OSError):
        pass


def _non_literal(token: str) -> bool:
    return "$" in token or "`" in token


def _stage_is_opaque(stage: List[str]) -> bool:
    """True when the detached stage's command cannot be named from the text alone."""
    peeled, _conf = _peel_command_position(stage)
    if not peeled:
        return True
    head = peeled[0]
    base = normalize_executable_basename(head)
    if _non_literal(head) or base == "eval":
        return True
    if base in _START_PROCESS_NAMES:
        return _start_process_is_opaque(peeled[1:])
    if base in _SHELLS_WITH_C:
        return _shell_c_is_opaque(peeled[1:])
    return False


def _start_process_is_opaque(args: List[str]) -> bool:
    for i, tok in enumerate(args):
        if tok.lower() in _START_PROCESS_PATH_FLAGS:
            return i + 1 >= len(args) or _non_literal(args[i + 1])
    first = next((t for t in args if not t.startswith("-")), None)
    return first is None or _non_literal(first)


def _shell_c_is_opaque(args: List[str]) -> bool:
    for i, tok in enumerate(args):
        if tok != "-" and tok.startswith("-") and not tok.startswith("--") and _C_FLAG_RE.match(tok):
            if i + 1 >= len(args) or _non_literal(args[i + 1]):
                return True
            return detach_shape_of_payload_is_opaque(args[i + 1])
    return False


def detach_shape_of_payload_is_opaque(payload: str) -> bool:
    """Whether a literal `-c` body itself launches through an opaque head (eval, $VAR)."""
    tokens = tokenize_full_command(payload)
    if tokens is None:
        return True
    return any(_stage_is_opaque(seg) for seg in _split_stages(tokens))


def _split_stages(tokens: List[str]) -> List[List[str]]:
    return [stage for stage, _sep in _stages_with_separator(tokens)]


def _stages_with_separator(tokens: List[str]) -> List[tuple]:
    """(stage tokens, the separator token that ends it or '') in order."""
    out: List[tuple] = []
    cur: List[str] = []
    for tok in tokens:
        if re.fullmatch(r"[;&|]+", tok):
            if cur:
                out.append((cur, tok))
                cur = []
            continue
        cur.append(tok)
    if cur:
        out.append((cur, ""))
    return out


def _is_detaching_stage(stage: List[str], sep: str) -> bool:
    if sep == "&":
        return True
    peeled, _conf = _peel_command_position(stage)
    if peeled and normalize_executable_basename(peeled[0]) in _START_PROCESS_NAMES:
        return True
    prefix = stage[: len(stage) - len(peeled)]
    return any(normalize_executable_basename(t) in _DETACH_WRAPPERS for t in prefix)


def detach_shape(command: str, run_in_background: bool = False) -> DetachShape:
    """Classify `command`: "none" (nothing detaches), "recordable" (a literal detached stage whose argv
    the recorder can corroborate), or "opaque" (a detached stage headed by eval, a $VAR/$(...) expansion,
    or a `-c` body that is not literal). `run_in_background` marks the whole command as the detached stage."""
    if not isinstance(command, str):
        return "none"
    if not run_in_background and _DETACH_WORD_RE.search(command) is None:
        return "none"
    tokens = tokenize_full_command(command)
    if tokens is None:
        return "opaque"
    stages = _stages_with_separator(tokens)
    detached: List[List[str]] = []
    for i, (stage, sep) in enumerate(stages):
        head_base = normalize_executable_basename(stage[0])
        if head_base == "disown":
            if i > 0:
                detached.append(stages[i - 1][0])
            continue
        if run_in_background or _is_detaching_stage(stage, sep):
            detached.append(stage)
    if not detached:
        return "none"
    return "opaque" if any(_stage_is_opaque(s) for s in detached) else "recordable"
