"""
coordinator_core.ops.plan_premise_carry — JSON-RPC "plan.premise_scope" and "plan.premise_record".

Purpose: let plan-blitz's premise check, on a refire, re-resolve only the citations that owe a
fresh check and carry every other verdict forward. Contract:
coordinator_core/contract/premise-carry-contract.md; pure core: ``_premise_carry``.

Wire params:
    plan.premise_scope   {plan | plan_path, brief_version}                 COMPUTE_ONLY
    plan.premise_record  {plan | plan_path, brief_version, rows, retired?, citations_skipped?}
                                                                            MUTATING

Negative-spec:
  - premise_scope writes nothing; premise_record writes only the plan's carry file under the
    gitignored plan-sidecars home, atomically, and nothing when it refuses.
  - Neither spawns a process; each reads one plan file and one carry file.
  - premise_record computes the scope itself and never trusts a caller-supplied one.
  - Any key outside the listed params is refused.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops import _premise_carry as carry
from coordinator_core.ops._param_alias import aliased_param, spellings

# The carry file is gitignored machinery under plan-sidecars/, not a tracked generated artifact.
GENERATES: list = []

_SCOPE_PARAMS = frozenset({"plan", "plan_path", "brief_version"})
_RECORD_PARAMS = _SCOPE_PARAMS | {"rows", "retired", "citations_skipped"}


def _common(params: dict, known: frozenset, repo_root: Optional[Path], op: str) -> tuple:
    """(plan rel posix path, plan text, brief_version, carry file path, record) for one call."""
    from coordinator_core.lifecycle import main_worktree_root
    from coordinator_core.session.machinery_paths import plan_sidecars_dir

    if repo_root is None:
        raise ValueError(f"{op} requires a resolved repo_root")
    extra = sorted(set(params) - known)
    if extra:
        raise ValueError(
            f"unknown param(s): {', '.join(repr(k) for k in extra)}. "
            f"Valid: {spellings('plan', 'plan_path')}, {', '.join(sorted(known - {'plan', 'plan_path'}))}"
        )
    raw_plan = aliased_param(params, "plan", "plan_path")
    if not isinstance(raw_plan, str) or not raw_plan.strip():
        raise ValueError(f"{spellings('plan', 'plan_path')} must be a non-empty string naming one plan file")
    version = params.get("brief_version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("brief_version must be a non-empty string")
    root = Path(main_worktree_root(repo_root))
    path = Path(raw_plan.strip())
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            raise ValueError(f"plan {raw_plan!r} is outside the repo root") from None
    if ".." in path.parts:
        raise ValueError(f"plan {raw_plan!r} must not contain '..'")
    rel = PurePosixPath(*path.parts).as_posix()
    full = root / path
    if not full.is_file():
        raise ValueError(f"plan not found: {rel}")
    text = full.read_text(encoding="utf-8", errors="replace")
    carry_path = Path(plan_sidecars_dir(str(root))) / f"{path.stem}.premise-carry.json"
    try:
        raw = carry_path.read_text(encoding="utf-8")
    except OSError:
        raw = ""
    return rel, text, version, carry_path, carry.parse_record(raw, rel)


@register_op("plan.premise_scope")
def _scope_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.premise_scope" handler. See module docstring."""
    rel, text, version, _, record = _common(params, _SCOPE_PARAMS, repo_root, "plan.premise_scope")
    return {"plan": rel, **carry.scope(record, text, version)}


@register_op("plan.premise_record")
def _record_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.premise_record" handler. See module docstring."""
    rel, text, version, carry_path, record = _common(params, _RECORD_PARAMS, repo_root, "plan.premise_record")
    if "rows" not in params:
        raise ValueError("rows is required (an empty list when nothing was re-checked)")
    new_record, merged = carry.merge(
        record,
        text,
        version,
        params["rows"],
        params.get("retired"),
        params.get("citations_skipped", 0),
        plan=rel,
    )
    _write_atomic(carry_path, new_record)
    fresh = len(params["rows"])
    from coordinator_core.lifecycle import main_worktree_root

    shown = carry_path.relative_to(Path(main_worktree_root(repo_root))).as_posix()
    return {"plan": rel, "path": shown, "rows": merged, "carried": len(merged) - fresh, "fresh": fresh}


def _write_atomic(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
