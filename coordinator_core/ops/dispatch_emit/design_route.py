"""The sizing's design_route contract: which design stage runs, who chose it, where a canvas board lands.

Imports nothing from dispatch_emit; ask_contract imports this module. Pure functions over a
sizing mapping -- no disk, no spawn.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Mapping, Optional

ROUTES = ("canvas", "reviewer", "none")
AUTHORITIES = ("pm", "apm", "em", "scout")
DESIGN_ROUTE_KEY = "design_route"
RECORD_KIND = "design_approval"
DRIVERS = {
    "canvas": "em draws on the Claude Design canvas; pm approves",
    "reviewer": "reviewer agent (staff-ux / senior-front-end)",
    "none": "none",
}
_APPROVAL_KEYS = ("by", "pm_ruling", "ruling_ref", "board_url", "on")
_BLOCK_KEYS = ("route", "authority", "approval")
_AUTHORITY_KEYS = ("by", "ref")
_DESIGN_DIR = "docs/design"
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _blank(value: Any) -> bool:
    return not isinstance(value, str) or not value.strip()


def block_errors(block: Any) -> list[str]:
    """Shape errors of a design_route mapping; [] when valid."""
    if not isinstance(block, Mapping):
        return [f"{DESIGN_ROUTE_KEY} must be a mapping"]
    errors = [f"{DESIGN_ROUTE_KEY}.{k}: unknown key" for k in block if k not in _BLOCK_KEYS]
    route = block.get("route")
    if route not in ROUTES:
        errors.append(f"{DESIGN_ROUTE_KEY}.route must be one of {'|'.join(ROUTES)}")
    authority = block.get("authority")
    if not isinstance(authority, Mapping):
        errors.append(f"{DESIGN_ROUTE_KEY}.authority must be a mapping")
    else:
        errors += [f"{DESIGN_ROUTE_KEY}.authority.{k}: unknown key" for k in authority if k not in _AUTHORITY_KEYS]
        by = authority.get("by")
        if by not in AUTHORITIES:
            errors.append(f"{DESIGN_ROUTE_KEY}.authority.by must be one of {'|'.join(AUTHORITIES)}")
        elif by != "scout" and _blank(authority.get("ref")):
            errors.append(f"{DESIGN_ROUTE_KEY}.authority.ref is required unless by is scout")
    approval = block.get("approval")
    if approval is not None:
        if route != "canvas":
            errors.append(f"{DESIGN_ROUTE_KEY}.approval is for the canvas route only")
        if not isinstance(approval, Mapping):
            errors.append(f"{DESIGN_ROUTE_KEY}.approval must be a mapping")
        else:
            errors += [f"{DESIGN_ROUTE_KEY}.approval.{k}: unknown key" for k in approval if k not in _APPROVAL_KEYS]
            if approval.get("by") != "pm":
                errors.append(f"{DESIGN_ROUTE_KEY}.approval.by must be pm")
            for key in ("pm_ruling", "ruling_ref", "board_url"):
                if _blank(approval.get(key)):
                    errors.append(f"{DESIGN_ROUTE_KEY}.approval.{key} is required")
    return errors


def board_paths(sizing: Mapping, sizing_rel: str) -> dict[str, str]:
    """Posix repo-relative `source` and `harvest` dirs of the sizing's canvas board."""
    name = sizing.get("name")
    stem = PurePosixPath(sizing_rel.replace("\\", "/")).stem
    slug = name.strip() if isinstance(name, str) and _SLUG.fullmatch(name.strip()) and ".." not in name else stem
    base = f"{_DESIGN_DIR}/{slug}"
    return {"source": f"{base}/project/", "harvest": f"{base}/harvest/"}


def read(sizing: Mapping, sizing_rel: str) -> tuple[Optional[dict], Optional[str]]:
    """(payload, refusal): (None, None) when no route is recorded, (None, refusal) when malformed."""
    if DESIGN_ROUTE_KEY not in sizing or sizing[DESIGN_ROUTE_KEY] is None:
        return None, None
    block = sizing[DESIGN_ROUTE_KEY]
    errors = block_errors(block)
    if errors:
        return None, "; ".join(errors)
    route = block["route"]
    authority = {"by": block["authority"]["by"]}
    if block["authority"].get("ref"):
        authority["ref"] = block["authority"]["ref"]
    payload: dict[str, Any] = {"route": route, "authority": authority, "driver": DRIVERS[route]}
    if route == "canvas":
        approval = block.get("approval")
        payload["board"] = {**board_paths(sizing, sizing_rel), "url": approval["board_url"] if approval else None}
        payload["approved"] = approval is not None
    return payload, None


def approval_touchpoint(sizing_rel: str) -> str:
    """What clears a design-review halt: the PM's approval written into the sizing, then the resubmit.

    dispatch.ask_gate has no `record` kind for it (`_RECORD_KINDS`), so the approval is written
    to the sizing's design_route.approval directly."""
    return (
        f"write {RECORD_KIND} into {sizing_rel} as design_route.approval "
        "{by: pm, pm_ruling: <the PM's approval, verbatim>, ruling_ref: <where the PM said it>, "
        "board_url: <board link>, on: <date>}; then resubmit engine-ask with "
        f"{{sizing: {sizing_rel}}}"
    )
