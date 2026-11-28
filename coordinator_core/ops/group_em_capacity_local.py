"""
coordinator_core.ops.group_em_capacity_local -- JSON-RPC "groupem.capacity_local",
"groupem.file_cache_report" and "groupem.file_cache_trim".

Purpose: this box's own answers, served so the fleet fan-out reaches a far box through its registered
coordinator-invoke (`box_transport.invoke_remote`) instead of shipping a program over ssh:
    - capacity_local: the live capacity row (maxima and current use).
    - file_cache_report: the file-cache dry-run view (counters only).
    - file_cache_trim: the file-cache trim on this box.

Negative-spec:
    - capacity_local and file_cache_report are read-only and COMPUTE_ONLY, so `policy.remote_safe`
      admits them on a far box; file_cache_trim is MUTATING and admitted only by
      `policy.PHASE2_MUTATING_ALLOWLIST`.
    - Never raises: a failure is a reply carrying `error` / `status: error`.
    - Takes no params; an unknown param is ignored.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import register_op


def _file_cache_trim() -> Any:
    """`file_cache_trim` by file path: it lives in bin/lib, which is not a package. Tries the claude-klabauter
    layout (`coordinator/bin/lib`) then the OSS mirror layout (`bin/lib`)."""
    from coordinator_core.bin_lib_binding import exec_module_bin_bound

    cached = sys.modules.get("file_cache_trim")
    if cached is not None and hasattr(cached, "local_trim"):
        return cached
    root = Path(__file__).resolve().parents[2]
    for lib in (root / "coordinator" / "bin" / "lib", root / "bin" / "lib"):
        path = lib / "file_cache_trim.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("file_cache_trim", path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules["file_cache_trim"] = module
            try:
                exec_module_bin_bound(spec.loader, module, str(lib))
            except BaseException:
                sys.modules.pop("file_cache_trim", None)
                raise
            return module
    raise ImportError("file_cache_trim.py not found under coordinator/bin/lib or bin/lib")


@register_op("groupem.capacity_local")
def _groupem_capacity_local(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.capacity_local" handler: ``fleet_live.sample_local()`` for this box.

    Returns:
        {"row": {<fleet_live.FIELDS>...}} (the probe sample, plus per-class cores when this box
        declares ``core_classes``) or {"error": str}.
    """
    try:
        from coordinator_core.group_em import fleet_live

        return {"row": fleet_live.sample_local()}
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"error": f"{type(exc).__name__}: {exc}"}


@register_op("groupem.file_cache_report")
def _groupem_file_cache_report(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.file_cache_report" handler: this box's file-cache dry-run view.

    Returns:
        {"status": "ok"|"not_applicable"|"error", "os", ...counters}; never trims.
    """
    try:
        return _file_cache_trim().local_report()
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}


@register_op("groupem.file_cache_trim")
def _groupem_file_cache_trim(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.file_cache_trim" handler: trim this box's file cache (MUTATING).

    Returns:
        {"status": "ok"|"failed"|"not_applicable"|"error", "exit", "output", "stderr"}.
    """
    try:
        return _file_cache_trim().local_trim()
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}
