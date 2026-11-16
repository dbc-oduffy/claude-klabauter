"""Writes the distill launcher: a payload-only Workflow script that nests the harvest script by scriptPath.

`write_launcher` carries the packed launch args and one ``workflow({scriptPath}, ARGS)`` call, so the
payload never rides in the Workflow tool call or the EM context and the harvest script is not copied
(a copy plus the payload would exceed the runner cap). One nesting level: the harvest calls no workflow().
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from coordinator_core.ops.dispatch_emit import launcher_staged_twin as _twin
from coordinator_core.ops.dispatch_emit.emit import _WORKFLOW_SCRIPT_BYTE_CAP
from coordinator_core.ops.dispatch_emit.nested_run_contract import STAGED_UNPACK_JS
from coordinator_core.ops.workflow_fire.receipt import write_fire_receipt

__all__ = ["LAUNCHER_FILE_NAME", "write_launcher"]

LAUNCHER_FILE_NAME = "launch.workflow.mjs"

_META_JS = (
    "export const meta = {\n"
    "  name: 'distill-launch',\n"
    "  description: 'Fires distill-harvest by scriptPath with the packed launch args',\n"
    "  phases: [],\n"
    "};\n"
)


def _literal(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _render(text: str, workflow_script: str) -> str:
    packed = _twin._pack_manifest(text)
    if packed is None:
        args_js = f"const ARGS = JSON.parse({_literal(text)});\n"
    else:
        packed_text, pack = packed
        args_js = (
            f"{STAGED_UNPACK_JS}\n"
            f"const ARGS = JSON.parse(_unpackStaged({{text: {_literal(packed_text)}, pack: {_literal(pack)}}}).text);\n"
        )
    return f"{_META_JS}{args_js}return await workflow({{scriptPath: {_literal(workflow_script)}}}, ARGS);\n"


def write_launcher(
    args: dict[str, Any], *, workflow_script: str, run_dir: Path, file_name: str = LAUNCHER_FILE_NAME
) -> dict[str, Any]:
    """Write ``<run_dir>/<file_name>`` and return ``{"scriptPath", "sha256", "launcher_bytes"}``.

    `args` is the already chunk_arrays'd launch payload. `scriptPath` is the launcher's absolute posix
    path; its fire receipt sits beside it. A launcher over the Workflow runner cap raises ValueError
    naming the bytes and the cap, with nothing written; an empty `workflow_script` raises ValueError.
    """
    if not isinstance(workflow_script, str) or not workflow_script.strip():
        raise ValueError("write_launcher needs workflow_script: the harvest script path the launcher nests")
    text = json.dumps(args, separators=(",", ":"), ensure_ascii=False)
    body = _render(text, workflow_script.replace("\\", "/")).encode("utf-8")
    if len(body) > _WORKFLOW_SCRIPT_BYTE_CAP:
        raise ValueError(
            f"distill launcher is {len(body)} bytes, over the Workflow runner cap of {_WORKFLOW_SCRIPT_BYTE_CAP} bytes"
        )
    target = Path(run_dir).resolve() / file_name
    _twin._land(target, body, ValueError, file_name)
    write_fire_receipt(target, script_bytes=body)
    return {"scriptPath": target.as_posix(), "sha256": hashlib.sha256(body).hexdigest(), "launcher_bytes": len(body)}
