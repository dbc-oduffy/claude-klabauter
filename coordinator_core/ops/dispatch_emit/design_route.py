"""The sizing's design_route contract: which design stage runs, who chose it, where a canvas board lands.

Imports nothing from dispatch_emit; ask_contract imports this module. Pure functions over a
sizing mapping -- no disk, no spawn.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Mapping, Optional

ROUTES = ("canvas", "reviewer", "none")
DESIGN_ROUTE_KEY = "design_route"
RECORD_KIND = "design_approval"
DRIVERS = {
    "canvas": "em draws on the Claude Design canvas; pm approves",
    "reviewer": "reviewer agent (staff-ux / senior-front-end)",
    "none": "none",
}
_APPROVAL_KEYS = ("by", "at")
_BLOCK_KEYS = ("route", "authority", "board_url", "approval")
_DESIGN_DIR = "docs/design"
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _blank(value: Any) -> bool:
    return not isinstance(value, str) or not value.strip()


def block_errors(block: Any) -> list[str]:
    """Shape errors of a design_route mapping; [] when valid."""
    if isinstance(block, str):
        return [] if block in ROUTES else [f"{DESIGN_ROUTE_KEY} must be one of {'|'.join(ROUTES)}"]
    if not isinstance(block, Mapping):
        return [f"{DESIGN_ROUTE_KEY} must be a route name or a mapping"]
    errors = [f"{DESIGN_ROUTE_KEY}.{k}: unknown key" for k in block if k not in _BLOCK_KEYS]
    errors += [f"{DESIGN_ROUTE_KEY}.{k} is required" for k in _BLOCK_KEYS if k not in block]
    route = block.get("route")
    if route not in ROUTES:
        errors.append(f"{DESIGN_ROUTE_KEY}.route must be one of {'|'.join(ROUTES)}")
    for key in ("authority", "board_url"):
        value = block.get(key)
        if value is not None and _blank(value):
            errors.append(f"{DESIGN_ROUTE_KEY}.{key} must be null or a non-blank string")
    approval = block.get("approval")
    if approval is not None:
        if route != "canvas":
            errors.append(f"{DESIGN_ROUTE_KEY}.approval is for the canvas route only")
        if block.get("board_url") is None:
            errors.append(f"{DESIGN_ROUTE_KEY}.approval needs a board_url")
        if not isinstance(approval, Mapping):
            errors.append(f"{DESIGN_ROUTE_KEY}.approval must be a mapping")
        else:
            errors += [f"{DESIGN_ROUTE_KEY}.approval.{k}: unknown key" for k in approval if k not in _APPROVAL_KEYS]
            if approval.get("by") != "pm":
                errors.append(f"{DESIGN_ROUTE_KEY}.approval.by must be pm")
            if _blank(approval.get("at")):
                errors.append(f"{DESIGN_ROUTE_KEY}.approval.at is required")
    return errors


def wrap(block: Any) -> dict:
    """The full four-key block: a bare route string expands, a mapping is copied."""
    if isinstance(block, str):
        return {"route": block, "authority": None, "board_url": None, "approval": None}
    return dict(block)


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
    block = wrap(block)
    route = block["route"]
    payload: dict[str, Any] = {"route": route, "authority": block["authority"], "driver": DRIVERS[route]}
    if route == "canvas":
        payload["board"] = {**board_paths(sizing, sizing_rel), "url": block["board_url"]}
        payload["approved"] = block["approval"] is not None
    return payload, None


STAGE_REFUSAL_PREFIX = "design-review:"


def stage_refusal(payload: Optional[Mapping]) -> Optional[str]:
    """Refusal text for staging a plan under an unapproved canvas payload; None when staging may go.

    Route, authority and board source lead the text: the halt reason is cut at
    ask_contract.HALT_REASON_MAX_CHARS."""
    if not payload or payload.get("route") != "canvas" or payload.get("approved") is not False:
        return None
    board = payload.get("board") or {}
    return (
        f"{STAGE_REFUSAL_PREFIX} route canvas, board {board.get('source')}; "
        "the PM has not approved the canvas board"
    )


def approval_touchpoint(sizing_rel: str) -> str:
    """What clears a design-review halt: the approval record call, then the refire."""
    return (
        "run `coordinator-invoke dispatch.ask_gate` with "
        f"record {{kind: {RECORD_KIND}, board_url: <board link>, "
        "pm_ruling: <the PM's approval, verbatim>, ruling_ref: <where the PM said it>}; "
        f"then refire engine-ask with {{sizing: {sizing_rel}}}"
    )


def next_action(sizing_rel: str) -> str:
    """The EM's line at a design-review halt: draw, harvest, get PM review, record, refire."""
    return (
        "open the canvas board with the Artifact tool (quickstart intent \"design\") on the board source, "
        "harvest it, get the PM's review; then " + approval_touchpoint(sizing_rel)
    )


def with_route(prior: Any, route: str, authority: Optional[str] = None) -> dict:
    """The four-key design_route block to write. board_url and approval carry over only when the route is unchanged.

    Raises ValueError carrying block_errors when the result is malformed."""
    block: dict[str, Any] = {"route": route, "authority": authority, "board_url": None, "approval": None}
    if prior is not None and not block_errors(prior):
        old = wrap(prior)
        if old["route"] == route:
            block["board_url"] = old["board_url"]
            block["approval"] = dict(old["approval"]) if old["approval"] is not None else None
    errors = block_errors(block)
    if errors:
        raise ValueError("; ".join(errors))
    return block
