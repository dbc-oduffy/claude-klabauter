"""Engine-side staged twin: the launcher child's run script with its manifest inlined.

A multi-ask launcher child receives no args and never calls ``workflow()``, so the stage op leaves a
copy of the child's script whose ``STAGED_INLINE_SLOT`` line carries the sha-verified manifest text
(``nested_run_contract.staged_twin_rel``); ``ensure_twin`` adds the run's escrowed token to that twin alone.
No git, no subprocess.
"""

from __future__ import annotations

import hashlib
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


_TWIN_NAME = re.compile(r"\.staged-[0-9a-f]{64}\.(?:workflow\.mjs|spill\.json)")


def _unlink_receipt(twin: Path) -> None:
    """Remove the twin's ``.emitted.json`` receipt beside it; a missing or locked receipt is ignored."""
    try:
        twin.with_name(twin.name + ".emitted.json").unlink(missing_ok=True)
    except OSError:
        pass


def _leave_receipt(twin: Path) -> None:
    """Leave the twin's fire receipt (best-effort, so the Workflow hook sees an engine-emitted script)."""
    if not twin.with_name(twin.name + ".emitted.json").exists():
        from coordinator_core.ops.workflow_fire.receipt import write_fire_receipt

        write_fire_receipt(twin)


def remove_twins(root: Path, run_id: object) -> int:
    """Unlink every ``scratch/warp/<run_id>.staged-<sha>`` twin (``.workflow.mjs``) and spill (``.spill.json``); return how many were removed.

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
                _unlink_receipt(directory / name)
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
                _unlink_receipt(path)
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
        estimate = sizing.get("estimate")
        size = estimate.get("tshirt") if isinstance(estimate, dict) else None
        tshirt = size if isinstance(size, str) and size.strip() else None
        block = {"arm": resolve_arm(sizing), "sizing_rel": sizing_rel, "plan_rel": plan_rel, "tshirt": tshirt}
    except Exception:
        return None
    return {key: block[key] for key in _nested.STAGED_RESUME_FIELDS}


def _slim_manifest(raw: bytes, handle: Mapping) -> tuple[str, dict] | None:
    """``(slim text, its handle)`` for a manifest with a ``rows`` list, else None (nothing to slim).

    Each row keeps ``SLIM_ROW_KEYS`` only; ``writes_count`` records the dropped ``writes`` length. Trap: a
    slim row's ``writes`` is ``[]``, so a reader must resolve the full list from the spill."""
    try:
        manifest = json.loads(raw)
    except ValueError:
        return None
    rows = manifest.get("rows") if isinstance(manifest, dict) else None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return None
    slim_rows = []
    for row in rows:
        slim = {key: row[key] for key in _nested.SLIM_ROW_KEYS if key in row}
        for key in _nested.SPILLED_ROW_KEYS:
            if isinstance(row.get(key), list):
                slim[key] = []
        if isinstance(row.get("writes"), list):
            slim["writes_count"] = len(row["writes"])
        slim_rows.append(slim)
    text = json.dumps({**manifest, "rows": slim_rows}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return text, {"path": handle.get("path"), "sha256": hashlib.sha256(text.encode("ascii")).hexdigest(), "bytes": len(text)}


def _land(target: Path, body: bytes, refuse: Callable[[str], Exception], label: str) -> None:
    """Write ``body`` to ``target`` atomically and verify it; no write when already identical."""
    try:
        if target.read_bytes() == body:
            return
    except OSError:
        pass
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(body)
        os.replace(tmp, target)
        if target.read_bytes() != body:
            raise OSError("file on disk differs from the body written")
    except OSError as exc:
        raise refuse(f"staged twin write failed: {label!r}: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)


_KEEP_SHAS = 3
_SHA_IN_NAME = re.compile(r"\.staged-([0-9a-f]{64})\.")


def _prune_run(root: Path, run_id: str, keep_sha: str) -> None:
    """Unlink this run's twins and spills except those of ``keep_sha`` and the newest ``_KEEP_SHAS - 1`` other
    engine-staged shas (a twin carrying its fire receipt); a receiptless twin is never kept.

    Failures are ignored. Exact-name match, this run only. Trap: a refire names the twin of the manifest
    it was handed, so a revise that restages mid-recompose must leave that earlier twin on disk; the
    bound keeps the token-bearing file count finite."""
    directory = root / _RUN_DIR_ROOT
    try:
        names = os.listdir(directory)
    except OSError:
        return
    newest: dict[str, int] = {}
    members: dict[str, list[str]] = {}
    for name in names:
        rest = name[len(run_id):]
        match = _SHA_IN_NAME.match(rest) if name.startswith(run_id) and _TWIN_NAME.fullmatch(rest) else None
        if match is None:
            continue
        sha = match.group(1)
        members.setdefault(sha, []).append(name)
        try:
            newest[sha] = max(newest.get(sha, 0), (directory / name).stat().st_mtime_ns)
        except OSError:
            pass
    staged = [
        sha
        for sha, files in members.items()
        if sha != keep_sha and any((directory / f"{n}.emitted.json").exists() for n in files if n.endswith(".workflow.mjs"))
    ]
    kept = set(sorted(staged, key=lambda sha: newest.get(sha, 0), reverse=True)[: _KEEP_SHAS - 1])
    for sha in members:
        if sha == keep_sha or sha in kept:
            continue
        for name in members[sha]:
            try:
                (directory / name).unlink()
                _unlink_receipt(directory / name)
            except OSError:
                pass


def ensure_twin(
    root: Path,
    sidecar_rel: str,
    raw: bytes,
    handle: Mapping,
    *,
    max_bytes: int,
    refuse: Callable[[str], Exception],
    refuse_over_cap: Callable[[str], Exception] | None = None,
) -> str | None:
    """Leave the staged twin for a launcher child's manifest; return its repo-relative path when the
    twin on disk now holds the body, else None (any other sidecar, a standalone run: no script or no slot).

    ``refuse`` builds the exception raised when the twin cannot be left on disk (unreadable script, several
    slots, uncontained target, write OSError; message starts ``staged twin write failed``);
    ``refuse_over_cap`` (default ``refuse``) builds the one raised when the twin still exceeds ``max_bytes``
    after slimming; both are passed in so this module never imports ``reply_sidecar``.
    A body over ``max_bytes`` is slimmed when the manifest has a ``rows`` list: row bulk (``SPILLED_ROW_KEYS``)
    leaves the inline text for a spill file beside the twin holding the full manifest.
    No write when the twin on disk is already identical. The run's escrowed token (if live) is inlined
    as ``runToken`` into the twin only.

    An absent run script is recomposed from the run's kept records (``halt_resume_build.recompose_script``)
    and staged from; it is refused only when that recompose is impossible, naming a fresh ask as the remedy.
    Trap: None means only "not a run manifest" or "script without an inline slot", never a missing script.
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
    except FileNotFoundError as exc:
        from coordinator_core.ops.dispatch_emit.halt_resume_build import recompose_script

        text, why = recompose_script(Path(root), run_id)
        if text is None:
            raise refuse(
                f"staged twin write failed: run script {script.name!r} is absent and cannot be recomposed ({why}); "
                f"a re-arm of this run cannot stage: start a fresh engine-ask on the run's sizing, "
                f"carrying the rows of {_WARP_HOME_REL}/{run_id}/requirements.md"
            ) from exc
        lines = text.splitlines(keepends=True)
    except (OSError, UnicodeDecodeError) as exc:
        raise refuse(f"staged twin write failed: run script {script.name!r} is unreadable: {exc}") from exc
    slots = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == _nested.STAGED_INLINE_SLOT]
    if not slots:
        return None
    if len(slots) != 1:
        raise refuse(f"staged twin write failed: run script {script.name!r} has {len(slots)} inline slots, not one")
    extras: dict = {}
    resume = _resume_block(Path(root), run_id)
    if resume is not None:
        extras[_nested.STAGED_RESUME_KEY] = resume
    token = _run_token.escrowed(Path(root), run_id)
    if token is not None:
        extras["runToken"] = token
    over_cap = refuse_over_cap or refuse

    def render(payload: dict) -> bytes:
        inline = json.dumps({**payload, **extras}, ensure_ascii=True)
        out = list(lines)
        ending = out[slots[0]][len(_nested.STAGED_INLINE_SLOT):]
        out[slots[0]] = f"const _STAGED_INLINE = {inline};{ending}"
        return "".join(out).encode("utf-8")

    twin_rel = _nested.staged_twin_rel(run_id, sha)
    target = _path_guard.contained_path(Path(root) / twin_rel, [Path(root) / _RUN_DIR_ROOT])
    if target is None:
        raise refuse(f"staged twin write failed: {twin_rel!r} is outside {_RUN_DIR_ROOT}/")
    body = render({"handle": dict(handle), "text": raw.decode("ascii")})
    spill_target = None
    if len(body) > max_bytes:
        slim = _slim_manifest(raw, handle)
        if slim is not None:
            spill_rel = _nested.staged_spill_rel(run_id, sha)
            spill_target = _path_guard.contained_path(Path(root) / spill_rel, [Path(root) / _RUN_DIR_ROOT])
            if spill_target is None:
                raise refuse(f"staged twin write failed: spill {spill_rel!r} is outside {_RUN_DIR_ROOT}/")
            slim_text, slim_handle = slim
            body = render(
                {"handle": slim_handle, "text": slim_text, "full_handle": dict(handle), _nested.STAGED_SPILL_KEY: spill_rel}
            )
    if len(body) > max_bytes:
        raise over_cap(f"{_nested.STAGED_TWIN_OVER_CAP_HEAD}: twin is {len(body)} bytes, over the {max_bytes}-byte cap")
    if spill_target is not None:
        _land(spill_target, raw, refuse, f"spill {spill_target.name}")
    _land(target, body, refuse, twin_rel)
    _prune_run(Path(root), run_id, sha)
    _leave_receipt(target)
    return Path(twin_rel).as_posix()
