"""codex_runner.execute -- run one plan chunk under `codex exec --sandbox workspace-write` and audit its writes.

Implements ModeRun (contract.py). The chunk row comes from the shared spine reader
(dispatch_emit.spine_read.read_spine); a chunk with no declared writes, or one the
reader excludes (disposition != open, gated, ...), is refused before any spawn.

Scope audit: every `file_change` event path is compared with the chunk's declared
`writes`. Trap: only apply_patch edits emit `file_change`; a file written through the
shell emits only `command_execution`, so this audit is detection of patch edits, not a
guarantee over shell writes. No git is spawned and nothing is reverted.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

from coordinator_core.codex_runner.contract import (
    STATUS_FAILED,
    STATUS_LIMIT_EXHAUSTED,
    STATUS_OK,
    STATUS_SCOPE_BREACH,
    OnSpawn,
    RunRequest,
    RunResult,
)
from coordinator_core.codex_runner.resolve import ApiKeyRefused, CodexUnavailable, preflight_auth, resolve_codex
from coordinator_core.codex_runner.review import _LIMIT_MARKERS, _build_env, _usage

EXECUTE_TIMEOUT_S = 3600

class ChunkRefused(ValueError):
    """The chunk cannot be run: absent from the spine, excluded, or declaring no writes."""


def _norm(path: str) -> str:
    return os.path.normpath(path.replace("\\", "/")).replace("\\", "/")


def load_chunk(plan: str, chunk: str) -> dict[str, Any]:
    """Return `{title, body, writes}` of an open chunk; raise ChunkRefused otherwise."""
    from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED, read_spine

    exclusions: list[dict] = []
    try:
        rows = read_spine(plan, exclusions=exclusions)
    except (OSError, ValueError) as exc:
        raise ChunkRefused(f"cannot read spine of {plan}: {exc}") from exc
    for row in rows:
        if row.id != chunk:
            continue
        if row.writes is UNDECLARED or not row.writes:
            raise ChunkRefused(f"chunk {chunk} declares no writes")
        return {"title": row.title, "body": row.body, "writes": [str(w) for w in row.writes]}
    for ex in exclusions:
        if ex.get("id") == chunk:
            raise ChunkRefused(f"chunk {chunk} is not open: {ex.get('reason')}: {ex.get('detail')}")
    raise ChunkRefused(f"chunk {chunk} not found in {plan}")


def build_prompt(chunk: dict[str, Any]) -> str:
    """Prompt carrying the chunk title, body and writes list."""
    writes = "\n".join(f"- {w}" for w in chunk["writes"])
    return (
        f"{chunk['title']}\n\n{chunk['body']}\n\n"
        f"Declared writes:\n{writes}\n\n"
        "Write only these files. Edit them with apply_patch; do not touch any other path."
    )


def _rel_to_root(path: str, repo_root: str) -> str:
    """Repo-relative normalised path, or the absolute normalised path when outside the root."""
    root = os.path.realpath(repo_root)
    p = path if os.path.isabs(path) else os.path.join(root, path)
    real = os.path.realpath(p)
    try:
        if os.path.commonpath([root, real]) == root:
            return _norm(os.path.relpath(real, root))
    except ValueError:
        pass
    return _norm(real)


def audit_scope(events: list[dict], writes: list[str], repo_root: str) -> dict[str, Any]:
    """Compare file_change paths with declared writes; `paths` lists the out-of-scope ones."""
    allowed = {_norm(w) for w in writes}
    changed: list[str] = []
    for ev in events:
        item = ev.get("item")
        if not isinstance(item, dict) or item.get("type") != "file_change":
            continue
        for ch in item.get("changes") or []:
            p = ch.get("path") if isinstance(ch, dict) else None
            if isinstance(p, str) and p:
                rel = _rel_to_root(p, repo_root)
                if rel not in changed:
                    changed.append(rel)
    out = [p for p in changed if p not in allowed]
    return {"breached": bool(out), "paths": out, "changed": changed}


def _child_env(requested: bool) -> dict[str, str]:
    """Scrubbed child env; the per-request allow_api_key flag admits the API-key vars."""
    return _build_env(requested=requested, role="worker")


def _parse_events(lines: list[str]) -> list[dict]:
    events = []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            events.append(ev)
    return events


def _error_text(events: list[dict]) -> Optional[str]:
    msgs = []
    for ev in events:
        if ev.get("type") == "error" and isinstance(ev.get("message"), str):
            msgs.append(ev["message"])
        elif ev.get("type") == "turn.failed":
            err = ev.get("error")
            if isinstance(err, dict) and isinstance(err.get("message"), str):
                msgs.append(err["message"])
    return "; ".join(msgs) if msgs else None


def run(req: RunRequest, run_dir: Path, on_spawn: OnSpawn) -> RunResult:
    """Run `req.chunk` of `req.plan` in the shared tree and return the audited result."""
    run_id = Path(run_dir).name
    try:
        preflight_auth(requested=req.allow_api_key)
    except ApiKeyRefused as exc:
        return RunResult(run_id=run_id, status=STATUS_FAILED, error=f"refused: {exc}")
    try:
        chunk = load_chunk(req.plan, req.chunk)
    except ChunkRefused as exc:
        return RunResult(run_id=run_id, status=STATUS_FAILED, error=f"refused: {exc}")
    try:
        argv = resolve_codex()
    except CodexUnavailable as exc:
        return RunResult(run_id=run_id, status=STATUS_FAILED, error=f"codex unavailable: {exc}")
    argv = argv + ["exec", "--json", "--sandbox", "workspace-write", build_prompt(chunk)]

    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "codex.stderr", "wb") as err_file:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=req.repo_root,
                env=_child_env(req.allow_api_key),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=err_file,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            return RunResult(run_id=run_id, status=STATUS_FAILED, error=f"codex spawn failed: {exc}")
        try:
            on_spawn(proc.pid)
        except BaseException:
            proc.kill()
            proc.communicate()
            raise
        try:
            stdout_text, _ = proc.communicate(timeout=EXECUTE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return RunResult(
                run_id=run_id, status=STATUS_FAILED, codex_pid=proc.pid,
                error=f"codex timed out after {EXECUTE_TIMEOUT_S}s",
            )
        lines = (stdout_text or "").splitlines()
        code = proc.returncode
    (run_dir / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    events = _parse_events(lines)
    scope = audit_scope(events, chunk["writes"], req.repo_root)
    usage = _usage(events)
    error = _error_text(events)
    common = dict(run_id=run_id, scope=scope, codex_pid=proc.pid, usage=usage)
    if scope["breached"]:
        return RunResult(
            status=STATUS_SCOPE_BREACH,
            error=f"file_change outside declared writes: {', '.join(scope['paths'])}",
            **common,
        )
    if error and any(m in error.lower() for m in _LIMIT_MARKERS):
        return RunResult(status=STATUS_LIMIT_EXHAUSTED, error=error, **common)
    if code != 0 or error:
        return RunResult(status=STATUS_FAILED, error=error or f"codex exited {code}", **common)
    return RunResult(status=STATUS_OK, **common)
