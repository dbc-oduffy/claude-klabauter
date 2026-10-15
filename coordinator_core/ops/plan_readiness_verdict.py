"""
coordinator_core.ops.plan_readiness_verdict — JSON-RPC "plan.readiness_verdict" operation.

Purpose: answer "is this plan ready, and is its only failure mechanical?" for ONE plan,
in-process. The prep gate is computed through ``plan.prep_gate``'s handler (the same
``roadmap.prep_gate.gate_plan`` bar), then combined with the optional plan-coverage and
reviewer values a plan-blitz run already holds. The verdict is a pure function of those
inputs, so no prep-gate or readiness agent is owed when nothing needs repair.

Wire params:
    plan     (str, required) — repo-relative or absolute plan path (alias ``plan_path``).
    coverage (object, optional) — {verdict, mechanicalFindings, judgmentFindings}; a findings
                                  value is a list or a non-negative count.
    reviews  (list, optional) — [{reviewer, verdict, ledger?}]; ``ledger`` is a repo-relative or
                                absolute path to that reviewer's findings sidecar. A WARN or
                                BLOCKED review is clear only when that sidecar carries a
                                ``findings_ledger:`` stamp and every row is ``verified: true``
                                with status absent or ``applied``.
    premise  (object, optional) — {rows: [{verdict, citation, ...}]}; verdict is RESOLVES |
                                  UNRESOLVED | CONTRADICTED | UNCHECKABLE. Only UNRESOLVED and
                                  CONTRADICTED rows pull; UNCHECKABLE is a note on a ready reply.
                                  Refused together with a ``premise-check`` reviews entry.

A coverage verdict that is not clear but carries zero findings gives the plan nothing to fix:
it is a note on a ready reply, never a pull.

Reply: see coordinator_core/contract/readiness-verdict.schema.json (the schema wins).

Negative-spec:
  - Takes no skip, force or override parameter; any key outside the five above is refused.
  - Does NOT write, stamp or mutate anything, and does NOT sweep the corpus. It reads a review's
    ledger sidecar through ``review_findings_ledger.read_ledger`` and never calls ``verify``.
  - Spawns nothing of its own; the prep gate's one batched ``git ls-files`` for python census
    targets is the only process it can cost, and only for a plan that declares such targets.
  - An unrecognised reviewer or coverage verdict reads as blocking: the verdict fails closed to
    ``judgment`` rather than ``ready``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops._param_alias import aliased_param, spellings
from coordinator_core.ops.plan_prep_gate import _handler as _prep_gate_handler
from coordinator_core.ops.review_findings_ledger import read_ledger

GENERATES: list = []

_KNOWN_PARAMS = frozenset({"plan", "plan_path", "coverage", "reviews", "premise"})
# Mirrors DoE plan-blitz.mjs PREMISE_REVIEWER.
_PREMISE_REVIEWER = "premise-check"
_PREMISE_VERDICTS = frozenset({"RESOLVES", "UNRESOLVED", "CONTRADICTED", "UNCHECKABLE"})
_PREMISE_BLOCKING = frozenset({"UNRESOLVED", "CONTRADICTED"})
_CLEAR_VERDICTS = frozenset(
    {
        "approve", "approved", "pass", "passed", "clear", "clean", "ready", "ok", "accept",
        "accepted", "complete",
    }
)
_LEDGER_VERDICTS = frozenset({"warn", "blocked"})
_PREP_VERDICTS_COMPUTED = frozenset({"PREPPED", "NOT-PREPPED"})
_INSTRUCTION_MAX = 1000


def _clear(verdict: Any) -> bool:
    return isinstance(verdict, str) and verdict.strip().lower() in _CLEAR_VERDICTS


def _ledger_clear(review: dict, repo_root: Optional[Path]) -> bool:
    """A WARN/BLOCKED review is clear only on a stamped sidecar whose rows are all applied+verified."""
    verdict = review.get("verdict")
    ledger = review.get("ledger")
    if not (isinstance(verdict, str) and verdict.strip().lower() in _LEDGER_VERDICTS):
        return False
    if not isinstance(ledger, str) or not ledger.strip():
        return False
    path = Path(ledger.strip())
    if not path.is_absolute():
        if repo_root is None:
            return False
        path = repo_root / path
    read = read_ledger(path)
    if read is None:
        return False
    stamped, rows = read
    return stamped and bool(rows) and all(
        row.get("verified") is True and (row.get("status") or "applied") == "applied"
        for row in rows
    )


def _findings(value: Any, label: str) -> List[Any]:
    """Normalise a findings value (list or count) to a list; a count becomes placeholders."""
    if value is None:
        return []
    if isinstance(value, bool):
        raise ValueError(f"coverage.{label} must be a list or a non-negative count")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"coverage.{label} must be a list or a non-negative count")
        return [None] * value
    if isinstance(value, list):
        return value
    raise ValueError(f"coverage.{label} must be a list or a non-negative count")


def _render(finding: Any) -> str:
    if isinstance(finding, str):
        return finding
    return json.dumps(finding, sort_keys=True, default=str)


def _repair(kind: str, target: str, instruction: str) -> dict:
    return {"kind": kind[:100], "target": target[:300], "instruction": instruction[:_INSTRUCTION_MAX]}


def _check_params(params: dict) -> tuple:
    extra = sorted(set(params) - _KNOWN_PARAMS)
    if extra:
        raise ValueError(
            f"unknown param(s): {', '.join(repr(k) for k in extra)}. "
            f"Valid: {spellings('plan', 'plan_path')}, coverage, reviews, premise"
        )
    raw_plan = aliased_param(params, "plan", "plan_path")
    if not isinstance(raw_plan, str) or not raw_plan.strip():
        raise ValueError(
            f"{spellings('plan', 'plan_path')} must be a non-empty string naming one plan file"
        )
    coverage = params.get("coverage")
    if coverage is not None and not isinstance(coverage, dict):
        raise ValueError("coverage must be an object {verdict, mechanicalFindings, judgmentFindings}")
    reviews = params.get("reviews")
    if reviews is not None:
        if not isinstance(reviews, list) or not all(isinstance(r, dict) for r in reviews):
            raise ValueError("reviews must be a list of {reviewer, verdict} objects")
    premise = params.get("premise")
    rows: List[dict] = []
    if premise is not None:
        if not isinstance(premise, dict) or not isinstance(premise.get("rows"), list):
            raise ValueError("premise must be an object {rows: [{verdict, citation}]}")
        for i, row in enumerate(premise["rows"]):
            if not isinstance(row, dict) or row.get("verdict") not in _PREMISE_VERDICTS:
                raise ValueError(
                    f"premise.rows[{i}].verdict must be one of {', '.join(sorted(_PREMISE_VERDICTS))}"
                )
            if not isinstance(row.get("citation"), str):
                raise ValueError(f"premise.rows[{i}].citation must be a string")
        rows = premise["rows"]
        if any(r.get("reviewer") == _PREMISE_REVIEWER for r in reviews or []):
            raise ValueError(
                f"premise and a {_PREMISE_REVIEWER!r} reviews entry are mutually exclusive; pass the rows once"
            )
    return raw_plan.strip(), coverage, reviews or [], rows


def readiness_verdict(
    prep_gate: dict,
    coverage: Optional[dict],
    reviews: List[dict],
    repo_root: Optional[Path] = None,
    premise_rows: Optional[List[dict]] = None,
) -> dict:
    """Verdict over a prep-gate reply plus coverage and reviewer values; the only read beyond
    them is a review's ledger sidecar, resolved against ``repo_root``. Only UNRESOLVED and
    CONTRADICTED premise rows block; UNCHECKABLE and RESOLVES rows never do."""
    notes: List[str] = []
    repairs: List[dict] = []
    substantive: List[str] = []
    judgment: List[str] = []

    if prep_gate.get("verdict") not in _PREP_VERDICTS_COMPUTED:
        substantive.append(f"prep gate verdict {prep_gate.get('verdict')}")
    for name, cls in (prep_gate.get("classes") or {}).items():
        if cls.get("status") == "PASS":
            continue
        if cls.get("mechanical") and cls.get("repair"):
            repairs.append(_repair(str(cls.get("kind") or name), name, cls["repair"]))
        else:
            substantive.append(f"{name}:{cls.get('kind') or cls.get('status')}")

    if coverage is not None:
        mechanical = _findings(coverage.get("mechanicalFindings"), "mechanicalFindings")
        judged = _findings(coverage.get("judgmentFindings"), "judgmentFindings")
        if judged:
            judgment.append(f"{len(judged)} judgment coverage finding(s)")
        for finding in mechanical:
            repairs.append(
                _repair(
                    "coverage-finding",
                    "plan",
                    "apply the mechanical plan-coverage finding in place"
                    + (f": {_render(finding)}" if finding is not None else ""),
                )
            )
        if not _clear(coverage.get("verdict")) and not mechanical and not judged:
            notes.append(f"coverage verdict {coverage.get('verdict')!r} carries no findings")
    for review in reviews:
        if not _clear(review.get("verdict")) and not _ledger_clear(review, repo_root):
            judgment.append(f"{review.get('reviewer')}: {review.get('verdict')!r}")

    uncheckable = 0
    blocking: List[str] = []
    for row in premise_rows or []:
        if row["verdict"] in _PREMISE_BLOCKING:
            blocking.append(f"{_PREMISE_REVIEWER}: {row['verdict']} {row['citation'][:200]}")
        elif row["verdict"] == "UNCHECKABLE":
            uncheckable += 1
    for i, entry in enumerate(blocking):
        if len("; ".join(judgment + blocking[: i + 1])) > _INSTRUCTION_MAX - 40:
            tail = len(blocking) - i
            blocking = blocking[:i] + [f"+{tail} more premise row(s)"]
            break
    judgment.extend(blocking)
    if uncheckable:
        notes.append(f"{uncheckable} UNCHECKABLE premise row(s) left to reviewer judgment")

    if substantive:
        verdict, pull_class, repairs = "pulled", "substantive", []
        reason = "prep gate failure needs judgment: " + "; ".join(substantive)
    elif judgment:
        verdict, pull_class, repairs = "judgment", "none", []
        reason = "needs an agent: " + "; ".join(judgment)
    elif repairs:
        verdict, pull_class = "pulled", "mechanical"
        reason = f"{len(repairs)} mechanical repair(s) apply in one revise"
    else:
        verdict, pull_class = "ready", "none"
        reason = "prep gate passes with no outstanding findings"
        if notes:
            reason += "; " + "; ".join(notes)
    return {
        "verdict": verdict,
        "pull_class": pull_class,
        "mechanical_repairs": repairs,
        "reason": reason[:1000],
    }


@register_op("plan.readiness_verdict")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.readiness_verdict" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("plan.readiness_verdict requires a resolved repo_root")
    raw_plan, coverage, reviews, premise_rows = _check_params(params)
    prep_gate = _prep_gate_handler({"plan": raw_plan}, repo_root)
    out = readiness_verdict(prep_gate, coverage, reviews, repo_root, premise_rows)
    return {"plan": prep_gate["plan"], **out, "prep_gate": prep_gate}
