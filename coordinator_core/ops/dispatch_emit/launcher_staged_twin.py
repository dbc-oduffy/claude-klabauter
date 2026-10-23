"""Engine-side staged twin: the launcher child's run script with its manifest inlined.

A multi-ask launcher child receives no args and never calls ``workflow()``, so the stage op leaves a
copy of the child's script whose ``STAGED_INLINE_SLOT`` line carries the sha-verified manifest text
(``nested_run_contract.staged_twin_rel``); the stage-token op adds the run token to that twin alone.
No git, no subprocess.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable, Mapping

from coordinator_core.ops import _path_guard
from coordinator_core.ops.dispatch_emit import nested_run_contract as _nested

_RUN_DIR_ROOT = "scratch/warp"
_RECORD_ROOTS = ("state/warp", _RUN_DIR_ROOT)
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
    swallowed after not counting. Zero spawns. Trap: the only caller is the redeemed-claim path, so a
    run that never commits leaves its token-bearing twin on disk."""
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


def ensure_twin(
    root: Path,
    sidecar_rel: str,
    raw: bytes,
    handle: Mapping,
    *,
    max_bytes: int,
    refuse: Callable[[str], Exception],
    run_token: str | None = None,
) -> str | None:
    """Leave the staged twin for a launcher child's manifest; return its repo-relative path when the
    twin on disk now holds the body, else None (any other sidecar, a standalone run, no slot).

    ``refuse`` builds the exception raised when the twin exceeds ``max_bytes`` (passed in so this module
    never imports ``reply_sidecar``). No write when the twin on disk is already identical. A given
    ``run_token`` is inlined as ``runToken`` into the twin only; every None branch then raises through
    ``refuse``, and no message carries the token.
    """

    def _none(branch: str) -> None:
        if run_token is not None:
            raise refuse(f"staged twin for {sidecar_rel!r} not written: {branch}")
        return None

    run_id = manifest_run_id(sidecar_rel)
    if run_id is None:
        return _none("sidecar is not a run manifest")
    sha = handle.get("sha256")
    if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
        return _none(f"run {run_id} handle sha256 is malformed")
    script = Path(root) / _RUN_DIR_ROOT / f"{run_id}.workflow.mjs"
    try:
        lines = script.read_bytes().decode("utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return _none(f"run {run_id} script is unreadable")
    slots = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == _nested.STAGED_INLINE_SLOT]
    if len(slots) != 1:
        return _none(f"run {run_id} script has {len(slots)} staged-inline slot lines, not 1")
    payload = {"handle": dict(handle), "text": raw.decode("ascii")}
    if run_token is not None:
        payload["runToken"] = run_token
    inline = json.dumps(payload, ensure_ascii=True)
    ending = lines[slots[0]][len(_nested.STAGED_INLINE_SLOT):]
    lines[slots[0]] = f"const _STAGED_INLINE = {inline};{ending}"
    body = "".join(lines).encode("utf-8")
    if len(body) > max_bytes:
        raise refuse(f"staged twin is {len(body)} bytes, over the Workflow runner's {max_bytes}-byte cap")
    twin_rel = _nested.staged_twin_rel(run_id, sha)
    target = _path_guard.contained_path(Path(root) / twin_rel, [Path(root) / _RUN_DIR_ROOT])
    if target is None:
        return _none(f"run {run_id} twin path is outside {_RUN_DIR_ROOT}")
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
