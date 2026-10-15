"""Run-scoped commit authorization for a warp `--ask` Workflow's in-run terminal commit.

The emit mints one token per run and returns it in the fire args, never in the script's bytes:
the harness hands it to the run as `args.runToken`, so no file a peer or a run agent can read
carries it. The run's final stage hands it to `dispatch.terminal_commit` through a
`coordinator:git-commit-agent`. `block_subagent_commit` admits that one invocation through
`admits`, a read-only match; the op then `redeem`s the record, an atomic claim exactly one
caller wins (a racing second invocation passes the guard and is refused there). After commit_v2
the op `consume`s the claim when a commit landed, else `release`s it back to live. A token
admits one landed commit, never two. Accepted residual: an op process killed between `redeem`
and `release` strands the claim; recovery is a re-mint through workflow.revise. Every other
subagent commit stays denied.

Invariants: the record lives in the git common dir (never the worktree, so no commit can carry
it); only a sha256 of the token is stored; the record names the run's script and a token admits
nothing without that script; a record older than `TTL_SECONDS` admits nothing. In-process file
I/O only: no spawn.
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
from typing import Iterator, Optional

TTL_SECONDS = 24 * 3600
_DIR = "coordinator-warp-run-tokens"
#: Where `redeem` creates the claim record; `consume` or `release` ends it.
_CLAIMED = "claimed"
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


def _record_path(repo_root: Path, run_id: str, *, claimed: bool = False) -> Optional[Path]:
    if not _RUN_ID_RE.match(run_id or ""):
        return None
    common = _common_dir(repo_root)
    if common is None:
        return None
    base = common / _DIR
    return (base / _CLAIMED if claimed else base) / f"{run_id}.json"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _read(path: Optional[Path]) -> Optional[dict]:
    if path is None:
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _fresh(record: dict) -> bool:
    minted = record.get("minted_at")
    return isinstance(minted, (int, float)) and time.time() - minted <= TTL_SECONDS


def mint(
    repo_root: Path,
    run_id: str,
    script_path: str,
    *,
    session_id: Optional[str] = None,
    sizing_rel: Optional[str] = None,
) -> str:
    """Write the run's record and return the token. Raises ValueError when no git dir resolves,
    the run id is malformed, or `script_path` is empty: an unbound token is never minted.

    `session_id` and `sizing_rel` (None for a raw ask until its gate binds the sizing it minted,
    `bind_sizing`) let `run_owns_sizing` tell an in-flight run's sizing from an abandoned one."""
    if not isinstance(script_path, str) or not script_path.strip():
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
    stale = _record_path(repo_root, run_id, claimed=True)
    if stale is not None:
        stale.unlink(missing_ok=True)
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    return token


def _matches(record: Optional[dict], run_id: object, token: object, script_path: object) -> bool:
    if record is None or record.get("run_id") != run_id or not _fresh(record):
        return False
    recorded_script = record.get("script_path")
    if not isinstance(recorded_script, str) or not recorded_script or script_path != recorded_script:
        return False
    return hmac.compare_digest(str(record.get("sha256") or ""), _digest(str(token)))


def _well_formed(run_id: object, token: object, script_path: object) -> bool:
    return (
        isinstance(run_id, str) and isinstance(token, str) and bool(TOKEN_RE.match(token))
        and isinstance(script_path, str) and bool(script_path)
    )


def admits(repo_root: Path, run_id: object, token: object, script_path: object) -> bool:
    """Read-only: True while the run's live record is fresh and `token` and `script_path` match
    it. Claims nothing, so a guard that admits a command whose op then fails to parse strands
    no record; two racing invocations can both pass here and `redeem` refuses the loser."""
    if not _well_formed(run_id, token, script_path):
        return False
    return _matches(_read(_record_path(repo_root, run_id)), run_id, token, script_path)


def redeem(repo_root: Path, run_id: object, token: object, script_path: object) -> bool:
    """The op's atomic claim: True for exactly one caller whose `token` and `script_path` match
    the live record. The claim is an exclusive create of the claimed record (a rename of one
    source can succeed for several racing callers on Windows). The claimed record stays until
    `consume` (the commit landed) or `release` (it did not)."""
    if not _well_formed(run_id, token, script_path):
        return False
    live = _record_path(repo_root, run_id)
    claimed = _record_path(repo_root, run_id, claimed=True)
    if live is None or claimed is None:
        return False
    record = _read(live)
    if not _matches(record, run_id, token, script_path):
        return False
    try:
        claimed.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(claimed, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(record, fh, sort_keys=True)
    for _ in range(50):
        try:
            live.unlink(missing_ok=True)
            break
        except PermissionError:  # a racing reader holds the live record open (Windows)
            time.sleep(0.005)
    return True


def release(repo_root: Path, run_id: object, token: object, script_path: object) -> bool:
    """Return the record this invocation claimed to live after a commit that landed nothing.
    True when restored; False when the claim is gone (`consume`d, re-minted) or a live record
    already exists, so a release never revives a spent token or overwrites a fresh mint."""
    if not _well_formed(run_id, token, script_path):
        return False
    live = _record_path(repo_root, run_id)
    claimed = _record_path(repo_root, run_id, claimed=True)
    if live is None or claimed is None:
        return False
    if not _matches(_read(claimed), run_id, token, script_path) or live.exists():
        return False
    try:
        os.replace(claimed, live)
    except OSError:
        return False
    return True


def is_live(repo_root: Path, run_id: str) -> bool:
    """True while the run's record is unclaimed and inside its TTL."""
    record = _read(_record_path(repo_root, run_id))
    return record is not None and _fresh(record)


def live_records(repo_root: Path) -> Iterator[dict]:
    """Every unclaimed, unexpired record, in path order."""
    common = _common_dir(repo_root)
    if common is None:
        return
    try:
        paths = sorted((common / _DIR).glob("*.json"))
    except OSError:
        return
    for path in paths:
        record = _read(path)
        if record is not None and isinstance(record.get("run_id"), str) and _fresh(record):
            yield record


def consume(repo_root: Path, run_id: str) -> None:
    """Remove the run's live and claimed records (the commit landed, or the run ended); a
    missing record is already consumed, and nothing is left for `release` to restore."""
    for claimed in (False, True):
        path = _record_path(repo_root, run_id, claimed=claimed)
        if path is not None:
            path.unlink(missing_ok=True)


def bind_sizing(repo_root: Path, run_id: str, sizing_rel: str) -> bool:
    """Record the sizing a raw ask's run minted on its live record (the gate calls this once the
    sizing exists). A record already naming a sizing is left as it is. True on a write."""
    path = _record_path(repo_root, run_id)
    record = _read(path)
    if record is None or not _fresh(record) or record.get("sizing_rel"):
        return False
    from coordinator_core.atomic_replace import atomic_write_bytes

    record["sizing_rel"] = sizing_rel
    atomic_write_bytes(path, json.dumps(record, sort_keys=True).encode("utf-8"), preserve_mode=False)
    return True


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
def run_owns_sizing(repo_root: Path, session_id: str, sizing_rel: str) -> bool:
    """True when a live run this session emitted names `sizing_rel`: from its emit (a `--sizing`
    run) or from its gate's bind (a raw ask). A sizing the session merely wrote while a raw ask
    was being sized is not the run's until the gate binds it. Reads only the token dir."""
    if not session_id or not sizing_rel:
        return False
    want = sizing_rel.replace("\\", "/")
    return any(
        record.get("session_id") == session_id
        and str(record.get("sizing_rel") or "").replace("\\", "/") == want
        for record in live_records(repo_root)
    )


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
