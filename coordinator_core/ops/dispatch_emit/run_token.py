"""Run-scoped commit authorization for a warp `--ask` Workflow's in-run terminal commit.

The emit mints one token per run and embeds it in the script; the run's final stage hands it to
`dispatch.terminal_commit` through a `coordinator:git-commit-agent`. `block_subagent_commit` admits
that one invocation when the token matches an unconsumed record for the run; the op consumes the
record on admit, so a failed commit needs a re-mint. Every other subagent commit stays denied.

Invariants: the record lives in the git common dir (never the worktree, so no commit can carry
it); only a sha256 of the token is stored; a record older than `TTL_SECONDS` admits nothing.
In-process file I/O only: no spawn.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Optional

TTL_SECONDS = 24 * 3600
_DIR = "coordinator-warp-run-tokens"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def _common_dir(repo_root: Path) -> Optional[Path]:
    """`<main worktree>/.git` for a worktree root or a common dir; None when neither holds."""
    root = Path(repo_root)
    if root.name == ".git" and root.is_dir():
        return root
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        # A linked worktree: `gitdir: <common>/worktrees/<name>`.
        text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("gitdir:"):
            gitdir = Path(text[len("gitdir:"):].strip())
            if not gitdir.is_absolute():
                gitdir = (root / gitdir).resolve()
            return gitdir.parent.parent if gitdir.parent.name == "worktrees" else gitdir
    return None


def _record_path(repo_root: Path, run_id: str) -> Optional[Path]:
    if not _RUN_ID_RE.match(run_id or ""):
        return None
    common = _common_dir(repo_root)
    return None if common is None else common / _DIR / f"{run_id}.json"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def mint(
    repo_root: Path,
    run_id: str,
    script_path: str,
    *,
    session_id: Optional[str] = None,
    sizing_rel: Optional[str] = None,
) -> str:
    """Write the run's record and return the token. Raises ValueError when no git dir resolves.

    `session_id` and `sizing_rel` let `warp_live` tell an in-flight run's sizing from an
    abandoned one. Minting again for the same `run_id` overwrites the record (re-mint on retry)."""
    if not isinstance(script_path, str) or not script_path:
        raise ValueError("run token: script_path is required")
    path = _record_path(repo_root, run_id)
    if path is None:
        raise ValueError(f"run token: no git common dir under {repo_root} or a malformed run id {run_id!r}")
    token = secrets.token_hex(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": run_id,
        "sha256": _digest(token),
        "script_path": script_path,
        "minted_at": time.time(),
        "session_id": session_id,
        "sizing_rel": sizing_rel,
    }
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    return token


def _live_record(repo_root: Path, run_id: object, token: object, script_path: object) -> Optional[Path]:
    """The record path when `token` matches the live record for `run_id` and its recorded script.

    A missing or mismatched `script_path`, or a record with no script, is refused."""
    if not isinstance(run_id, str) or not isinstance(token, str) or not TOKEN_RE.match(token):
        return None
    path = _record_path(repo_root, run_id)
    if path is None or not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("run_id") != run_id:
        return None
    minted = record.get("minted_at")
    if not isinstance(minted, (int, float)) or time.time() - minted > TTL_SECONDS:
        return None
    recorded_script = record.get("script_path")
    if not recorded_script or not isinstance(script_path, str) or script_path != recorded_script:
        return None
    if not hmac.compare_digest(str(record.get("sha256") or ""), _digest(str(token))):
        return None
    return path


def verifies(repo_root: Path, run_id: object, token: object, script_path: object = None) -> bool:
    """`admits` without consuming: the commit guard's check ahead of the op that consumes.

    Trap: the guard must never consume -- `dispatch.terminal_commit` admits the same token
    moments later, and a record the guard burned refuses every in-run commit."""
    return _live_record(repo_root, run_id, token, script_path) is not None


def admits(repo_root: Path, run_id: object, token: object, script_path: object = None) -> bool:
    """True once for a `token` matching the live record for `run_id` and its recorded script.

    Success consumes the record by renaming it to a unique claim name: of two racing admits only
    the rename winner returns True. Verification precedes the claim, so a wrong token never burns
    a live record."""
    path = _live_record(repo_root, run_id, token, script_path)
    if path is None:
        return False
    claim = path.with_name(f"{path.name}.claim.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        os.replace(path, claim)
    except OSError:
        return False
    try:
        claim.unlink()
    except OSError:
        pass
    return True


def consume(repo_root: Path, run_id: str) -> None:
    """Remove the run's record; a missing record is already consumed."""
    path = _record_path(repo_root, run_id)
    if path is not None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def normalize_sizing_rel(repo_root: Path, sizing_path: str) -> str:
    """`sizing_path` as a repo-relative forward-slash path: absolute, `./`-prefixed and
    backslash spellings name the same sizing. A path outside the repo is returned in posix form."""
    text = (sizing_path or "").replace("\\", "/")
    if not text:
        return ""
    p = Path(text)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(Path(repo_root).resolve()).as_posix()
        except (ValueError, OSError):
            return text
    return os.path.normpath(text).replace("\\", "/")


def token_params_from_command(raw_cmd: str) -> Optional[dict]:
    """`{run_id, run_token, script_path}` from a `dispatch.terminal_commit` command line's JSON
    params (inline argument or `--params-file -` heredoc body), else None. Text parse only."""
    start = raw_cmd.find("{")
    end = raw_cmd.rfind("}")
    if start < 0 or end <= start:
        return None
    body = raw_cmd[start:end + 1].replace("'\\''", "'")
    try:
        params = json.loads(body)
    except ValueError:
        return None
    if not isinstance(params, dict):
        return None
    return {k: params.get(k) for k in ("run_id", "run_token", "script_path")}
