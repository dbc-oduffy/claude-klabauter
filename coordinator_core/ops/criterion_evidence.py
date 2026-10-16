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
    - Reviews mode (`reviews`: list of five-field rows) appends to the sidecar's top-level
      `reviews:` list. A review row is a summary of a reviewer sidecar receipt, never a verdict.

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


#: The exact keys of one review-firing row; `recorded_at` is stamped by the writer.
REVIEW_ROW_FIELDS = ("leg", "plan_id", "findings_count", "max_severity", "applied")

#: Closed severity tokens for a review row; `None` means no severity data.
REVIEW_SEVERITIES = ("P0", "P1", "P2", "nit", "unranked")


def _fail(message: str) -> dict:
    return {"error": f"criterion_evidence.record: {message}"}


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _review_row_problem(row) -> Optional[str]:
    """Why `row` is not a valid review row, or None."""
    if not isinstance(row, dict) or set(row) != set(REVIEW_ROW_FIELDS):
        return f"each review row must carry exactly {list(REVIEW_ROW_FIELDS)}"
    if not isinstance(row["leg"], str) or not row["leg"].strip():
        return "review 'leg' must be a non-empty str"
    if row["plan_id"] is not None and not isinstance(row["plan_id"], str):
        return "review 'plan_id' must be a str or null"
    if not _is_count(row["findings_count"]) or not _is_count(row["applied"]):
        return "review 'findings_count' and 'applied' must be ints >= 0"
    if row["max_severity"] is not None and row["max_severity"] not in REVIEW_SEVERITIES:
        return f"review 'max_severity' must be one of {list(REVIEW_SEVERITIES)} or null"
    return None


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
        reviews (list[dict]) -- instead of clause/command/output_path/exit_code: rows with
            exactly REVIEW_ROW_FIELDS, appended under `reviews:`. Cannot combine with clause.

    Returns:
        {"recorded": True, "sidecar": str, "clause": str, "output_sha256": str}
        or {"error": str}. Reviews mode returns {"recorded": True, "sidecar": str, "reviews": int}.
    """
    p = params if isinstance(params, dict) else {}
    plan_path = _text(p, "plan_path")
    if plan_path is None:
        return _fail("'plan_path' (non-empty str) is required")
    reviews_mode = "reviews" in p
    if reviews_mode:
        if "clause" in p:
            return _fail("'reviews' cannot be combined with 'clause'")
        reviews = p["reviews"]
        if not isinstance(reviews, list) or not reviews:
            return _fail("'reviews' must be a non-empty list of rows")
        for row in reviews:
            problem = _review_row_problem(row)
            if problem:
                return _fail(problem)
    else:
        clause, command, output_path = _text(p, "clause"), _text(p, "command"), _text(p, "output_path")
        for name, value in (("clause", clause), ("command", command), ("output_path", output_path)):
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
    recorded_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if reviews_mode:
        key = "reviews"
        entries = [{**{f: row[f] for f in REVIEW_ROW_FIELDS}, "recorded_at": recorded_at} for row in reviews]
    else:
        out_file = Path(output_path)
        if not out_file.is_absolute():
            out_file = worktree / out_file
        try:
            raw = out_file.read_bytes()
        except OSError as exc:
            return _fail(f"output file unreadable: {exc}")
        if not raw.strip():
            return _fail("output file is empty; a command with no captured output is an assertion, not evidence")
        key = "criterion_evidence"
        entries = [{
            "recorded_at": recorded_at,
            "clause": clause,
            "command": command,
            "exit_code": exit_code,
            "output_path": str(out_file),
            "output_sha256": hashlib.sha256(raw).hexdigest(),
            "output_tail": raw.decode("utf-8", errors="replace")[-EXCERPT_TAIL_CHARS:],
        }]

    def mutate(old_text: str) -> str:
        try:
            loaded = yaml.safe_load(old_text) if old_text.strip() else None
        except yaml.YAMLError as exc:
            raise MutateAbort(f"sidecar is not valid YAML: {exc}") from exc
        loaded = {} if loaded is None else loaded
        if not isinstance(loaded, dict) or not isinstance(loaded.get(key, []), list):
            raise MutateAbort("sidecar has an unexpected shape")
        loaded.setdefault(key, []).extend(entries)
        return yaml.safe_dump(loaded, sort_keys=False, allow_unicode=True, width=4096)

    sidecar = evidence_sidecar_path(plan_file)
    try:
        locked_rmw(sidecar, mutate, repo_root=repo_root, missing_ok=True)
    except LockTimeout as exc:
        return _fail(f"timed out waiting for file lock on {sidecar.name}: {exc}")
    except MutateAbort as exc:
        return _fail(exc.args[0] if exc.args else "mutation aborted")
    if reviews_mode:
        return {"recorded": True, "sidecar": sidecar.name, "reviews": len(entries)}
    return {"recorded": True, "sidecar": sidecar.name, "clause": clause, "output_sha256": entries[0]["output_sha256"]}
