"""design.registry.lookup - JSON-RPC handler over `coordinator_core.design_registry.lookup`.

`paths` is a required list of repo-relative strings; the repo root comes from the `show_top`
scope. Reads the design registry file only; a named refusal comes back as `ok: false`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from coordinator_core import design_registry
from coordinator_core.ipc import register_op

BAD_PARAMS = "bad-params"


def _refusal(reason: str, detail: str) -> dict:
    return {"ok": False, "reason": reason, "detail": detail}


@register_op("design.registry.lookup")
def _handler(params: dict, repo_root: Any = None) -> dict:
    """Return the registry boards that draw `paths`; never raises for a named refusal."""
    params = params or {}
    paths = params.get("paths")
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return _refusal(BAD_PARAMS, "paths must be a list of strings")
    if not repo_root:
        return _refusal(BAD_PARAMS, "a repo root is required")
    hits, error = design_registry.lookup(repo_root, paths)
    if error:
        return _refusal(error, "design registry does not parse")
    path = design_registry.registry_path(repo_root)
    registry = path.relative_to(Path(repo_root).resolve()).as_posix() if path.is_file() else None
    return {"ok": True, "hits": hits, "registry": registry}
