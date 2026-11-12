"""``dispatch.reemit``: the one EM-facing hand re-emit route.

Params: ``{params_file: str}`` (a path, absolute or under the repo root) or ``{params: dict}``: the
``dispatch.emit`` params to re-run. Reply: ``{ok, script_path, refusal?}``; ``script_path`` is repo-relative.
Writes one deferred script under ``scratch/warp/`` and returns; the EM starts it, and it emits the run, follows
the launcher chain hand-off and answers every ``stage_pending`` with the staged twin. No git, no subprocess,
no in-process ``dispatch.emit``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root

OP_NAME = "dispatch.reemit"
REFUSAL_NO_REPO = "no-repo-root"
REFUSAL_BAD_PARAMS = "bad-params"


def _refuse(refusal: str) -> dict:
    return {"ok": False, "script_path": None, "refusal": refusal}


def _read_params(root: Path, p: dict) -> tuple[Optional[dict], Optional[str]]:
    """``(emit_params, None)`` or ``(None, one-line refusal)``."""
    inline = p.get("params")
    if isinstance(inline, dict) and inline:
        return inline, None
    raw = p.get("params_file")
    if not isinstance(raw, str) or not raw.strip():
        return None, f'{REFUSAL_BAD_PARAMS}: pass {{"params_file": "<path>"}} or {{"params": {{...}}}}'
    path = Path(raw.strip().replace("\\", "/"))
    path = path if path.is_absolute() else root / path
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{REFUSAL_BAD_PARAMS}: cannot read params_file {raw.strip()}: {type(exc).__name__}"
    if not isinstance(loaded, dict) or not loaded:
        return None, f"{REFUSAL_BAD_PARAMS}: params_file {raw.strip()} is not a non-empty JSON object"
    return loaded, None


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.reemit": write the deferred script for one ``dispatch.emit`` params set."""
    from coordinator_core.ops.dispatch_emit.deferred_emit_script import write_deferred_script
    from coordinator_core.ops.dispatch_emit.fan_launcher import LAUNCHER_CHILD_EMIT_PARAM

    if repo_root is None:
        return _refuse(f"{REFUSAL_NO_REPO}: {OP_NAME} requires repo_root")
    # Trap: the engine keys this op `common_dir`, so repo_root arrives as `<repo>/.git`, not the worktree root.
    root = main_worktree_root(Path(repo_root))
    emit_params, refusal = _read_params(root, params if isinstance(params, dict) else {})
    if refusal is not None:
        return _refuse(refusal)
    # A hand re-emit is run by the EM, not a launcher: keeping the flag would render the hand-back-only script.
    emit_params = {k: v for k, v in emit_params.items() if k != LAUNCHER_CHILD_EMIT_PARAM}
    try:
        script = Path(write_deferred_script(root, emit_params, branch="reemit"))
    except OSError as exc:
        return _refuse(f"write-failed: cannot write the deferred script: {type(exc).__name__}")
    try:
        rel = script.relative_to(root).as_posix()
    except ValueError:
        rel = script.as_posix()
    return {"ok": True, "script_path": rel}
