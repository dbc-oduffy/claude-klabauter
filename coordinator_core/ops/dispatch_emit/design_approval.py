"""The design_approval writer: records a canvas board link and the PM's approval into the sizing's design_route block.

The single writer of `design_route.board_url` and `design_route.approval`; ask_gate routes its
`design_approval` record kind here. Spawns no process.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from coordinator_core.design_registry import ARTIFACT_URL_RE
from coordinator_core.frontmatter.primitives import write_fm_nested_field
from coordinator_core.frontmatter.schema_validate import format_validation_errors, validate_frontmatter
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit import design_route
from coordinator_core.session import record_homes

_SIZING_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
)
_BOARD_URL = re.compile("https://" + ARTIFACT_URL_RE.pattern)


def _err(message: str) -> dict:
    return {"exit_code": 1, "error": message}


def record(repo_root, sizing_rel: str, *, board_url: str) -> dict:
    """Write board_url and approval {by: pm, at: now} into the sizing's design_route; `{"exit_code": 0}` or `{"exit_code": 1, "error"}`.

    Refuses, leaving the sizing bytes unchanged: a non-canvas or malformed block, an already-approved
    sizing, a blank board_url, a board_url that is not exactly `https://claude.ai/artifact/<id>`, and a
    missing `<harvest>/harvest.json`. The stored approval is `{by, at}` only: the PM's verbatim
    ruling is not persisted here."""
    root = Path(repo_root)
    if not isinstance(board_url, str) or not board_url.strip():
        return _err("design_approval: board_url must be a non-empty string")
    url = board_url.strip()
    if _BOARD_URL.fullmatch(url) is None:
        return _err("design_approval: board_url must be exactly https://claude.ai/artifact/<id>")
    path = Path(sizing_rel)
    if not path.is_absolute():
        path = root / path
    path = contained_path(path, [Path(record_homes.home_dir(str(root), "sizings"))])
    if path is None:
        return _err(f"design_approval: sizing escapes the sizings home: {sizing_rel!r}")
    if not path.is_file():
        return _err(f"design_approval: sizing not found: {sizing_rel}")

    def mutate(old_text: str) -> str:
        try:
            doc = yaml.safe_load(old_text) or {}
        except Exception as exc:  # noqa: BLE001
            raise MutateAbort(f"design_approval: YAML parse error: {exc}") from exc
        if not isinstance(doc, dict):
            raise MutateAbort("design_approval: sizing is not a YAML mapping")
        raw = doc.get(design_route.DESIGN_ROUTE_KEY)
        if raw is None:
            raise MutateAbort("design_approval: the sizing records no design_route; only a canvas route takes an approval")
        errors = design_route.block_errors(raw)
        if errors:
            raise MutateAbort("design_approval: " + "; ".join(errors))
        block = design_route.wrap(raw)
        if block["route"] != "canvas":
            raise MutateAbort(f"design_approval: route is {block['route']}; only a canvas route takes an approval")
        if block["approval"] is not None:
            raise MutateAbort("design_approval: the canvas is already approved")
        harvest = design_route.board_paths(doc, sizing_rel)["harvest"] + "harvest.json"
        if not (root / harvest).is_file():
            raise MutateAbort(f"design_approval: {harvest} is missing; harvest the board first")
        block["board_url"] = url
        block["approval"] = {"by": "pm", "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        dumped = yaml.safe_dump(block, default_flow_style=False, sort_keys=False, allow_unicode=True, width=100000)
        text = write_fm_nested_field(old_text, design_route.DESIGN_ROUTE_KEY, "".join(f"  {line}\n" for line in dumped.splitlines()))
        try:
            new_doc = yaml.safe_load(text) or {}
        except Exception as exc:  # noqa: BLE001
            raise MutateAbort(f"design_approval: post-write YAML parse error: {exc}") from exc
        problems = validate_frontmatter(new_doc, _SIZING_SCHEMA_PATH)
        if problems:
            raise MutateAbort(f"design_approval: schema validation failed: {format_validation_errors(problems)}")
        return text

    try:
        locked_rmw(path, mutate, repo_root=root)
    except FileNotFoundError:
        return _err(f"design_approval: sizing disappeared: {sizing_rel}")
    except LockTimeout as exc:
        return _err(f"design_approval: timed out waiting for the file lock: {exc}")
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "design_approval: mutation aborted")
    except Exception as exc:  # noqa: BLE001
        return _err(f"design_approval: {type(exc).__name__}: {exc}")
    return {"exit_code": 0}
