"""Restore a halted run's evicted work from its recorded own delta."""
from pathlib import Path

from coordinator_core.ipc import CallerFacingValidationError, register_op
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.halt_tree_evict import restore_own_delta


#: Typed refusal: evicted work is recorded but no path was restored or already in place.
RESTORE_NOTHING_REFUSAL = "restore-evicted-nothing-restored"


@register_op("dispatch.restore_evicted")
def _handler(params: dict, repo_root: Path | None = None) -> dict:
    if repo_root is None:
        raise CallerFacingValidationError("dispatch.restore_evicted requires a repository")
    root = Path(repo_root)
    run_id = params.get("run_id")
    try:
        record = record_dir(root, run_id)
    except ValueError as exc:
        raise CallerFacingValidationError(str(exc)) from exc
    paths = restore_own_delta(root, record, run_id)
    if paths and not any(item["status"] in ("restored", "already-restored") for item in paths.values()):
        return {"refused": RESTORE_NOTHING_REFUSAL, "paths": paths}
    return {"paths": paths}
