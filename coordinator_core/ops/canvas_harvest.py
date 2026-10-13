"""canvas.harvest - JSON-RPC handler over `coordinator_core.canvas_harvest.api`.

`mode` is `harvest` or `probe`. A relative `out` resolves against the `repo_root` the
`show_top` scope supplies; with none it is a params refusal. A HarvestError comes back as
`ok: false` with its reason. Every result carries the reserved max-paths key so an IPC
dispatch claims the whole record in one batch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from coordinator_core import ipc
from coordinator_core.canvas_harvest import api, model
from coordinator_core.ipc import register_op

BAD_PARAMS = "bad-params"
_MODES = ("harvest", "probe")


def _result(ok: bool, reason: str = "", detail: str = "", wrote=(), deleted=()) -> dict:
    return {
        "ok": ok,
        "reason": reason,
        "detail": detail,
        "wrote": [str(p) for p in wrote],
        "deleted": [str(p) for p in deleted],
        ipc._SCOPE_TOUCH_MAX_PATHS_KEY: model.MAX_RECORD_FILES,
    }


def _text(params: dict, key: str) -> str | None:
    value = params.get(key)
    return value if isinstance(value, str) and value else None


@register_op("canvas.harvest")
async def _handler(params: dict, repo_root: Any = None) -> dict:
    """Run one canvas harvest or probe; never raises for a named refusal."""
    params = params or {}
    mode = params.get("mode")
    if mode not in _MODES:
        return _result(False, BAD_PARAMS, f"mode must be one of {', '.join(_MODES)}")
    url = _text(params, "url")
    out = _text(params, "out")
    if url is None or out is None:
        return _result(False, BAD_PARAMS, "url and out are required strings")
    session_id = params.get("session_id")
    transcript = params.get("transcript")
    if session_id is not None and not isinstance(session_id, str):
        return _result(False, BAD_PARAMS, "session_id must be a string")
    if transcript is not None and not isinstance(transcript, str):
        return _result(False, BAD_PARAMS, "transcript must be a string")
    out_dir = Path(out)
    if not out_dir.is_absolute():
        if not repo_root:
            return _result(False, BAD_PARAMS, "a relative out needs a repo root")
        out_dir = Path(repo_root) / out_dir
    call = api.harvest if mode == "harvest" else api.probe
    try:
        written = call(url, out_dir, session_id=session_id, transcript=transcript)
    except model.HarvestError as exc:
        return _result(False, exc.reason, exc.detail)
    except ValueError as exc:
        return _result(False, BAD_PARAMS, str(exc))
    return _result(True, wrote=written.written, deleted=written.deleted)
