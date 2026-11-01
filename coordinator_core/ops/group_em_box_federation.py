"""
coordinator_core.ops.group_em_box_federation -- JSON-RPC "groupem.box_register", "groupem.box_place",
"groupem.box_send", "groupem.box_roster".

Purpose: the engine surface of multi-box group-em. Register a box (settings-home root + capability)
in this box's registry, place a whole run on a registered box, append one line to a box's inbox, and
read the federated roster of remote boxes.

Registered in `coordinator_core/ops/__init__.py`'s registration list, `_registry_map.py`'s
`OP_MODULE_MAP`, `op_scopes.py` (scope "none") and `authz/classification.py` (register, place and
send MUTATING; roster COMPUTE_ONLY). Enrolled in `_BUDGETED_ENTRYPOINTS` of the spawn audit with an
empty reachable spawn set.

Negative-spec:
    - Never raises: an invalid param or I/O failure is `{"error": ...}`.
    - Spawns nothing; every effect is a file read or write under a settings-home.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.group_em import box_probe, box_registry, box_send, placement
from coordinator_core.ipc import register_op


def _str(p: dict, key: str) -> Optional[str]:
    v = p.get(key)
    return v if isinstance(v, str) and v else None


@register_op("groupem.box_register")
def _groupem_box_register(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.box_register" handler.

    Params:
        root (str) -- the settings-home root of the box to register.
        capability (dict, optional) -- the capability record; default is the `capability` already in
            `<root>/state/group-em/box.json`, else `box_probe.write_capability(root)` (this machine's probe).

    Returns:
        {"registered": True, "box": <registry record>} or {"error": str}.
    """
    p = params if isinstance(params, dict) else {}
    root = _str(p, "root")
    if root is None:
        return {"error": "root (non-empty str) is required"}
    cap = p.get("capability")
    if cap is not None and not isinstance(cap, dict):
        return {"error": "capability must be an object"}
    try:
        if cap is None:
            cap = box_probe.stored_capability(Path(root))
        if cap is None:
            cap = box_probe.write_capability(Path(root)).get("capability") or {}
        return {"registered": True, "box": box_registry.register(root, cap)}
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"error": f"{type(exc).__name__}: {exc}"}


@register_op("groupem.box_place")
def _groupem_box_place(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.box_place" handler.

    Params:
        run (str) -- the run label to place.
        requirement (dict, optional) -- capability requirement (`min_<key>` floors, exact matches).
        box_id (str, optional) -- explicit target box.
        brief (str, optional) -- brief path or text carried on the placement.

    Returns:
        {"ok": True, "placement"} | {"ok": False, "reason"} or {"error": str}.
    """
    p = params if isinstance(params, dict) else {}
    run = _str(p, "run")
    if run is None:
        return {"error": "run (non-empty str) is required"}
    req = p.get("requirement", {})
    if not isinstance(req, dict):
        return {"error": "requirement must be an object"}
    box_id = p.get("box_id")
    if box_id is not None and not isinstance(box_id, str):
        return {"error": "box_id must be str"}
    brief = p.get("brief", "")
    if not isinstance(brief, str):
        return {"error": "brief must be str"}
    try:
        return placement.place_run(run, req, box_id=box_id, brief=brief)
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"error": f"{type(exc).__name__}: {exc}"}


@register_op("groupem.box_send")
def _groupem_box_send(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.box_send" handler.

    Params:
        box_id (str) -- registered target box; omitted when ``local`` is true.
        local (bool, optional) -- deliver into THIS box's own inbox (the far end of a cross-box relay,
            which cannot know the id the near box registered it under); self-registers if absent.
        to_session (str) -- the EM session the line is addressed to.
        text (str) -- the message.

    Returns:
        {"sent": True, "box_id", "path"} | {"sent": False, "reason"} or {"error": str}.
    """
    p = params if isinstance(params, dict) else {}
    box_id, to = _str(p, "box_id"), _str(p, "to_session")
    local = p.get("local") is True
    if (box_id is None and not local) or to is None:
        return {"error": "box_id (or local: true) and to_session (non-empty str) are required"}
    text = p.get("text")
    if not isinstance(text, str):
        return {"error": "text (str) is required"}
    try:
        if local:
            box_id = box_registry.local_box_id()
            if box_registry.box_root(box_id) is None:
                home = settings_home()
                box_id = box_registry.ensure_identity(home)
                box_registry.register(home, box_probe.stored_capability(home) or {}, box_id=box_id)
        return box_send.send(box_id, to, text)
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"error": f"{type(exc).__name__}: {exc}"}


@register_op("groupem.box_roster")
def _groupem_box_roster(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.box_roster" handler: the remote boxes' live runs, via `watch.remote_box_rows`.

    Returns:
        {"boxes": [registered box ids], "rows": [{box_id, session_id, run, live, reachable}, ...]} or {"error": str}.
    """
    try:
        from coordinator_core.group_em import watch

        rows = watch.remote_box_rows()
        return {"boxes": [b["box_id"] for b in box_registry.read_boxes()], "rows": rows}
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"error": f"{type(exc).__name__}: {exc}"}
