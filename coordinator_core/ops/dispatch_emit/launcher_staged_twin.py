"""Engine-side staged twin: the launcher child's run script with its manifest inlined.

A multi-ask launcher child receives no args and never calls ``workflow()``, so the stage op leaves a
copy of the child's script whose ``STAGED_INLINE_SLOT`` line carries the sha-verified manifest text
(``nested_run_contract.staged_twin_rel``); ``ensure_twin`` adds the run's escrowed token to that twin alone.
No git, no subprocess.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Callable, Mapping

from coordinator_core.ops import _path_guard
from coordinator_core.ops.dispatch_emit import run_token as _run_token
from coordinator_core.ops.dispatch_emit import nested_run_contract as _nested
from coordinator_core.session import record_homes

_RUN_DIR_ROOT = "scratch/warp"
_WARP_HOME_REL = Path(record_homes.home_dir("", "warp")).as_posix()
_RECORD_ROOTS = (_WARP_HOME_REL, _RUN_DIR_ROOT)
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def manifest_run_id(sidecar_rel: str) -> str | None:
    """The run id when ``sidecar_rel`` is ``<state/warp|scratch/warp>/<run_id>/manifest.json``, else None."""
    parts = sidecar_rel.split("/")
    if len(parts) != 4 or "/".join(parts[:2]) not in _RECORD_ROOTS or parts[3] != "manifest.json":
        return None
    return parts[2] if _RUN_ID.fullmatch(parts[2]) and ".." not in parts[2] else None


_TWIN_NAME = re.compile(r"\.staged-[0-9a-f]{64}\.workflow\.mjs")


def remove_twins(root: Path, run_id: object) -> int:
    """Unlink every ``scratch/warp/<run_id>.staged-<sha>.workflow.mjs``; return how many were removed.

    Exact-name match on this run only (no glob); an invalid run_id removes nothing; OSErrors are
    swallowed after not counting. Zero spawns. Trap: a run's twin is removed only by a commit leg's
    call; a run that never commits leaves its token-bearing twin until ``sweep_expired_twins``."""
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id) or ".." in run_id:
        return 0
    directory = Path(root) / _RUN_DIR_ROOT
    prefix = run_id
    removed = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    for name in names:
        if name.startswith(prefix) and _TWIN_NAME.fullmatch(name[len(prefix):]):
            try:
                (directory / name).unlink()
                removed += 1
            except OSError:
                pass
    return removed


def sweep_expired_twins(root: Path, now: float | None = None) -> int:
    """Unlink every staged twin in ``scratch/warp`` older than ``run_token.TTL_SECONDS``; return the count.

    Same exact twin-name shape as ``remove_twins``, any run id; age is file mtime. OSErrors are
    swallowed after not counting. Zero spawns. Also sweeps expired token escrows (not counted): the
    plaintext token outlives its run the same way the twin does, and this is the only periodic caller."""
    _run_token.sweep_expired_escrows(root, now)
    cutoff = (time.time() if now is None else now) - _run_token.TTL_SECONDS
    directory = Path(root) / _RUN_DIR_ROOT
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    removed = 0
    for name in names:
        match = _TWIN_NAME.search(name)
        if (
            match is None
            or match.start() == 0
            or match.end() != len(name)
            or not _RUN_ID.fullmatch(name[: match.start()])
        ):
            continue
        path = directory / name
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def _resume_block(root: Path, run_id: str) -> dict | None:
    """The twin's resume block (``STAGED_RESUME_FIELDS``) from the run's ``run.json`` and its sizing,
    else None when any read fails. Imports are deferred: ``terminal_commit`` imports this module.
    Trap: None omits the block, and a child without it re-runs every pre-stage leg (the re-size the block exists to prevent)."""
    try:
        record = json.loads((Path(record_homes.home_dir(str(root), "warp")) / run_id / "run.json").read_text(encoding="utf-8"))
        sizing_rel = record.get("sizing_rel") if isinstance(record, dict) else None
        if not isinstance(sizing_rel, str) or not sizing_rel.strip():
            return None
        from coordinator_core.ops.dispatch_emit.sizing_fire import load_sizing, resolve_arm

        sizing = load_sizing(root, sizing_rel)
        edge = sizing.get("plan")
        plan_rel = edge if isinstance(edge, str) and edge.strip() else None
        block = {"arm": resolve_arm(sizing), "sizing_rel": sizing_rel, "plan_rel": plan_rel}
    except Exception:
        return None
    return {key: block[key] for key in _nested.STAGED_RESUME_FIELDS}


def ensure_twin(
    root: Path,
    sidecar_rel: str,
    raw: bytes,
    handle: Mapping,
    *,
    max_bytes: int,
    refuse: Callable[[str], Exception],
) -> str | None:
    """Leave the staged twin for a launcher child's manifest; return its repo-relative path when the
    twin on disk now holds the body, else None (any other sidecar, a standalone run, no slot).

    ``refuse`` builds the exception raised when the twin exceeds ``max_bytes`` (passed in so this module
    never imports ``reply_sidecar``). No write when the twin on disk is already identical. The run's
    escrowed token (if live) is inlined as ``runToken`` into the twin only.
    """
    run_id = manifest_run_id(sidecar_rel)
    if run_id is None:
        return None
    sha = handle.get("sha256")
    if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
        return None
    script = Path(root) / _RUN_DIR_ROOT / f"{run_id}.workflow.mjs"
    try:
        lines = script.read_bytes().decode("utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return None
    slots = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == _nested.STAGED_INLINE_SLOT]
    if len(slots) != 1:
        return None
    payload = {"handle": dict(handle), "text": raw.decode("ascii")}
    resume = _resume_block(Path(root), run_id)
    if resume is not None:
        payload[_nested.STAGED_RESUME_KEY] = resume
    token = _run_token.escrowed(Path(root), run_id)
    if token is not None:
        payload["runToken"] = token
    inline = json.dumps(payload, ensure_ascii=True)
    ending = lines[slots[0]][len(_nested.STAGED_INLINE_SLOT):]
    lines[slots[0]] = f"const _STAGED_INLINE = {inline};{ending}"
    body = "".join(lines).encode("utf-8")
    if len(body) > max_bytes:
        raise refuse(f"staged twin is {len(body)} bytes, over the Workflow runner's {max_bytes}-byte cap")
    twin_rel = _nested.staged_twin_rel(run_id, sha)
    target = _path_guard.contained_path(Path(root) / twin_rel, [Path(root) / _RUN_DIR_ROOT])
    if target is None:
        return None
    try:
        if target.read_bytes() == body:
            return Path(twin_rel).as_posix()
    except OSError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(body)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return Path(twin_rel).as_posix()
