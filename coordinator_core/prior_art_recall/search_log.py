"""Validator for the prior-art sidecar's `## Search Log` block.

Thoroughness is mechanical, not trusted: every claim must carry one cell per
always-on corpus, the improvement queue must be enumerated, and plan code
anchors must be checked through example-retrieval-repo (or the gap named). A hole is a
refusal naming the missing cell. Pure functions over the caller's text.
"""
from __future__ import annotations

import re
from typing import Any

import yaml

ALWAYS_ON_CORPORA = (
    "project-wikis",
    "global-wikis",
    "doctrine-wiki",
    "decisions",
    "lessons",
    "improvement-queue",
    "skills",
)
ALLOWED_CELL_STATUS = ("absent", "unreadable")
RAG_TOOL_PREFIX = "project_"
MIN_REASON_CHARS = 15
_FILLER_REASONS = {"n/a", "na", "none", "same", "similar", "related", "tbd", "todo", "see above"}

_HEADING_RE = re.compile(r"^##\s+Search Log\s*$", re.MULTILINE)
_FENCE_RE = re.compile(r"```ya?ml\s*\n(.*?)\n```", re.DOTALL)
_CODE_ANCHOR_RE = re.compile(r"`([\w./\\-]+\.(?:py|cpp|hpp|h|cs|ts|tsx|js|rs|go)(?::\d+)?)`")


def plan_code_anchors(plan_text: str) -> list[str]:
    """Backticked code-file anchors in a plan; extension-limited so prose never counts."""
    seen: dict[str, None] = {}
    for m in _CODE_ANCHOR_RE.finditer(plan_text):
        seen.setdefault(re.sub(r":\d+$", "", m.group(1)).replace("\\", "/"), None)
    return list(seen)


def extract_log(sidecar_text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Return (search_log mapping, error); exactly one of the two is None."""
    m = _HEADING_RE.search(sidecar_text)
    if not m:
        return None, "no '## Search Log' section"
    fence = _FENCE_RE.search(sidecar_text, m.end())
    if not fence:
        return None, "'## Search Log' has no yaml block"
    try:
        data = yaml.safe_load(fence.group(1))
    except yaml.YAMLError as exc:
        return None, f"search log YAML does not parse: {exc}"
    log = data.get("search_log") if isinstance(data, dict) else None
    if not isinstance(log, dict):
        return None, "search log YAML has no top-level 'search_log' mapping"
    return log, None


def _named_reason(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    v = value.strip()
    return len(v) >= MIN_REASON_CHARS and v.lower() not in _FILLER_REASONS


def _int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _check_scope(log: dict[str, Any], errors: list[str]) -> set[int]:
    """Validate totals, cap and grouping; return the claim ids that need a full row of cells."""
    total, checked = log.get("plan_claims_total"), log.get("claims_checked")
    if not (_int(total) and total > 0):
        errors.append("plan_claims_total must be a positive integer")
        return set()
    limit = total
    cap = log.get("cap")
    if isinstance(cap, dict):
        if not (_int(cap.get("limit")) and 0 < cap["limit"] < total):
            errors.append(f"cap.limit must be a positive integer below plan_claims_total ({total})")
        elif not _named_reason(cap.get("reason")):
            errors.append("claims capped without a named reason (cap.reason)")
        else:
            limit = cap["limit"]
    elif cap not in (None, "none"):
        errors.append("cap must be 'none' or {limit, reason}")
    if checked != limit:
        errors.append(f"claims_checked={checked!r} but {limit} of {total} claims must be checked")
    grouping = log.get("grouping")
    if isinstance(grouping, list):
        for i, g in enumerate(grouping):
            ids = g.get("claims") if isinstance(g, dict) else None
            if not (isinstance(ids, list) and ids and all(_int(x) for x in ids)):
                errors.append(f"grouping[{i}] needs a claims list of ids")
            elif not _named_reason(g.get("reason")):
                errors.append(f"claims {ids} grouped without a named reason")
    elif grouping not in (None, "none"):
        errors.append("grouping must be 'none' or a list of {claims, reason}")
    return set(range(1, limit + 1))


def _check_cells(log: dict[str, Any], need: set[int], errors: list[str]) -> None:
    cells = log.get("cells")
    if not isinstance(cells, list):
        errors.append("cells must be a list")
        return
    have: set[tuple[int, str]] = set()
    for i, c in enumerate(cells):
        if not isinstance(c, dict):
            errors.append(f"cells[{i}] is not a mapping")
            continue
        claim, corpus = c.get("claim"), c.get("corpus")
        if not (_int(claim) and claim in need):
            errors.append(f"cells[{i}] names claim {claim!r} outside the checked set")
            continue
        if corpus not in ALWAYS_ON_CORPORA:
            continue  # extra corpora (peer-wikis, repo-code) are permitted, never required
        if c.get("status") in ALLOWED_CELL_STATUS:
            if not _named_reason(c.get("reason")):
                errors.append(f"cell claim {claim} x {corpus} is {c['status']} without a named reason")
                continue
        elif not (isinstance(c.get("query"), str) and c["query"].strip() and _int(c.get("hits"))):
            errors.append(f"cell claim {claim} x {corpus} needs a non-empty query and an integer hits")
            continue
        have.add((claim, corpus))
    missing = [(c, k) for c in sorted(need) for k in ALWAYS_ON_CORPORA if (c, k) not in have]
    if missing:
        shown = ", ".join(f"claim {c} x {k}" for c, k in missing[:6])
        errors.append(f"{len(missing)} missing search cells: {shown}" + (" ..." if len(missing) > 6 else ""))


def _check_queue(log: dict[str, Any], errors: list[str]) -> None:
    q = log.get("improvement_queue")
    if not isinstance(q, dict):
        errors.append("improvement_queue block missing: the queue must be enumerated, not grepped")
        return
    if not (isinstance(q.get("method"), str) and q["method"].strip()):
        errors.append("improvement_queue.method missing")
    if q.get("status") in ALLOWED_CELL_STATUS:
        if not _named_reason(q.get("reason")):
            errors.append(f"improvement queue {q['status']} without a named reason")
    elif not _int(q.get("enumerated")):
        errors.append("improvement_queue.enumerated must be the integer count of entries listed")
    elif q["enumerated"] == 0 and not _named_reason(q.get("empty_reason")):
        errors.append("improvement_queue enumerated 0 entries without empty_reason")


def _check_anchors(log: dict[str, Any], plan_text: str | None, errors: list[str]) -> None:
    ca = log.get("code_anchors")
    if not isinstance(ca, dict):
        errors.append("code_anchors block missing")
        return
    wanted = plan_code_anchors(plan_text) if plan_text else []
    status = ca.get("status")
    if status == "checked":
        checks = ca.get("checks")
        if not (isinstance(checks, list) and checks):
            errors.append("code_anchors.status=checked with no checks")
            return
        for i, ck in enumerate(checks):
            ok = (
                isinstance(ck, dict)
                and str(ck.get("tool", "")).startswith(RAG_TOOL_PREFIX)
                and ck.get("anchor")
                and ck.get("result")
            )
            if not ok:
                errors.append(f"code_anchors.checks[{i}] needs anchor, a project_* tool and result")
        logged = " ".join(str(ck.get("anchor", "")) for ck in checks if isinstance(ck, dict))
        absent = [a for a in wanted if a not in logged]
        if absent:
            errors.append(f"plan code anchors not checked via example-retrieval-repo: {', '.join(absent[:5])}")
    elif status in ("none", "unavailable"):
        if not _named_reason(ca.get("reason")):
            errors.append(f"code_anchors {status} without a named reason")
        elif status == "none" and wanted:
            errors.append(f"code_anchors none but the plan cites code anchors: {', '.join(wanted[:5])}")
    else:
        errors.append("code_anchors.status must be checked | none | unavailable")


def validate_sidecar(sidecar_text: str, plan_text: str | None = None) -> list[str]:
    """Return refusal reasons; empty means the search log satisfies the contract."""
    log, err = extract_log(sidecar_text)
    if log is None:
        return [err or "search log unreadable"]
    errors: list[str] = []
    need = _check_scope(log, errors)
    if need:
        _check_cells(log, need, errors)
    _check_queue(log, errors)
    _check_anchors(log, plan_text, errors)
    return errors
