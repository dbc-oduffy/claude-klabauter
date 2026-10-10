"""
coordinator_core.ops.criterion_evidence -- JSON-RPC "criterion_evidence.record".

Purpose: the sanctioned door for the EM to record evidence for ONE exit-criterion clause it ran
itself, because the run could not (a leg the heavy-command admission guard denies inside a
workflow, such as a Playwright/e2e suite). The entry joins the plan's evidence sidecar
(`<plan-stem>.evidence.yaml`) under `criterion_evidence`; the terminal judge and the
`--rejudge` route are already pointed at that sidecar, and read it as observed evidence.

Invariants:
    - Recorded evidence is input to the judge, never a verdict: the entry carries no status.
    - An entry is command + raw output, never an assertion alone: the output file must exist,
      be non-empty, and its sha256 and a bounded tail are stored so the entry outlives the file.
    - The plan body is never read or written, so `approved_body_sha` does not move.

Registered in `ops/__init__.py`'s list, `_registry_map.py`, `op_scopes.py` (common_dir),
`authz/classification.py` (MUTATING) and `.github/op-inventory.json`.

Negative-spec:
    - Never raises: an invalid param is `{"error": ...}`.
"""

from __future__ import annotations

import datetime
import hashlib
from pathlib import Path
from typing import Optional

import yaml

from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.plan_tasks_mutate import _PathNotContained, _resolve_path, evidence_sidecar_path

#: Last N characters of the output kept inline: a pass/fail summary sits at the tail.
EXCERPT_TAIL_CHARS = 3000


def _fail(message: str) -> dict:
    return {"error": f"criterion_evidence.record: {message}"}


def _text(params: dict, key: str) -> Optional[str]:
    value = params.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


@register_op("criterion_evidence.record")
def _criterion_evidence_record(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "criterion_evidence.record" handler.

    Params:
        plan_path (str) -- the plan under docs/plans/.
        clause (str) -- the exit-criterion clause id or its verbatim text.
        command (str) -- the exact command the EM ran.
        output_path (str) -- file holding the command's raw output (absolute, or relative to
            the worktree root).
        exit_code (int) -- the command's exit status; required, since a tail alone does not say
            whether the command passed.

    Returns:
        {"recorded": True, "sidecar": str, "clause": str, "output_sha256": str}
        or {"error": str}.
    """
    p = params if isinstance(params, dict) else {}
    plan_path, clause, command, output_path = (
        _text(p, "plan_path"), _text(p, "clause"), _text(p, "command"), _text(p, "output_path"),
    )
    for name, value in (("plan_path", plan_path), ("clause", clause), ("command", command), ("output_path", output_path)):
        if value is None:
            return _fail(f"{name!r} (non-empty str) is required")
    exit_code = p.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        return _fail("'exit_code' (int) is required")
    if repo_root is None:
        return _fail("repo_root is required (no founding root available)")

    worktree = main_worktree_root(repo_root)
    try:
        plan_file = _resolve_path(plan_path, worktree)
    except _PathNotContained as exc:
        return _fail(str(exc))
    if not plan_file.is_file():
        return _fail(f"plan not found: {plan_path}")
    out_file = Path(output_path)
    if not out_file.is_absolute():
        out_file = worktree / out_file
    try:
        raw = out_file.read_bytes()
    except OSError as exc:
        return _fail(f"output file unreadable: {exc}")
    if not raw.strip():
        return _fail("output file is empty; a command with no captured output is an assertion, not evidence")

    entry = {
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "clause": clause,
        "command": command,
        "exit_code": exit_code,
        "output_path": str(out_file),
        "output_sha256": hashlib.sha256(raw).hexdigest(),
        "output_tail": raw.decode("utf-8", errors="replace")[-EXCERPT_TAIL_CHARS:],
    }

    def mutate(old_text: str) -> str:
        try:
            loaded = yaml.safe_load(old_text) if old_text.strip() else None
        except yaml.YAMLError as exc:
            raise MutateAbort(f"sidecar is not valid YAML: {exc}") from exc
        loaded = {} if loaded is None else loaded
        if not isinstance(loaded, dict) or not isinstance(loaded.get("criterion_evidence", []), list):
            raise MutateAbort("sidecar has an unexpected shape")
        loaded.setdefault("criterion_evidence", []).append(entry)
        return yaml.safe_dump(loaded, sort_keys=False, allow_unicode=True, width=4096)

    sidecar = evidence_sidecar_path(plan_file)
    try:
        locked_rmw(sidecar, mutate, repo_root=repo_root, missing_ok=True)
    except LockTimeout as exc:
        return _fail(f"timed out waiting for file lock on {sidecar.name}: {exc}")
    except MutateAbort as exc:
        return _fail(exc.args[0] if exc.args else "mutation aborted")
    return {"recorded": True, "sidecar": sidecar.name, "clause": clause, "output_sha256": entry["output_sha256"]}
