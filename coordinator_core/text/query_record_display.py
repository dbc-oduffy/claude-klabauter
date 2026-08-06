"""
coordinator_core.text.query_record_display — native port of query-records.js's
``TYPE_DISPLAY`` table and ``formatRecords()`` (markdown-list/json/paths output).

Port source: coordinator/bin/query-records.js:307-340 (TYPE_DISPLAY), :1601-1638
(formatRecords). Consumed by coordinator_core.text.refresh_queries (the
BEGIN/END query-callout expander this ports formatRecords for).

Scope — bounded to the record types coordinator_core.ops.records_query._TYPE_TO_GLOB
(and its ceremony wrapper, coordinator_core.ops.ceremony.records_query.query_records)
can actually enumerate: handoff, handoff-archived, plan, cross-repo-memo, bug, debt,
improvement, decision-guide, completion, decision, review, lesson, handoff-ledger,
research-claim, research-synthesis, gap-report, coverage-audit — seventeen of the
oracle's TYPE_DISPLAY's ~20 entries. tracker/roadmap/health-status ARE queryable
natively but have NO TYPE_DISPLAY entry in the oracle either (bin/query-records.js:
307-340 never lists them) — they fall through to the oracle's own generic default
renderer, ported here as `_default_display`.

Negative-spec (SCOPE-DROP, flagged not silently dropped): cross-repo-memo's
`review-sidecar`/`prior-art-check`/`plan-coverage-check`/`docs-check-sidecar`/
`integration-summary`/`problem-set` TYPE_DISPLAY renderers are NOT ported — the
native reader (coordinator_core.ops.ceremony.records_query.query_records) has
no queryRecords equivalent for those types (no _TYPE_TO_GLOB entry, no
schema-sidecar parsing), so a renderer for them would be dead code with nothing to
ever call it. Widening the native reader to cover them is a separate, larger port.
`research-synthesis`/`gap-report`/`coverage-audit` were in this dropped list until
2026-07-22 (cross-repo/inbox/2026-07-22-claude-central-em-records-query-excluded-
types-doe-needs.md — 3 live example-doctrine-repo runtime consumers) — see records_query.py's own
Negative-spec for the wiring + sibling-exclusion-filter side of that change.

`archived-memo` is a DIFFERENT case, not part of the above SCOPE-DROP list: it
DOES have a `_TYPE_TO_GLOB` entry (`cross-repo/archive/*.md`, wired in
records_query.py) and IS collectible/queryable natively — it simply has no
dedicated TYPE_DISPLAY renderer yet, so it falls through to `_default_display`
(functionally fine, just unstyled) alongside tracker/roadmap/health-status
above. Review: code-reviewer (F3) — the prior wording of this docstring
misstated archived-memo's reason as "no _TYPE_TO_GLOB entry", which stopped
being true once that entry was wired.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Optional

_DisplayFn = Callable[[str, dict], str]


# ---------------------------------------------------------------------------
# Per-type renderers — each transcribed verbatim from its TYPE_DISPLAY entry.
# ---------------------------------------------------------------------------


def _display_handoff(link_path: str, fm: dict) -> str:
    """bin/query-records.js:308."""
    title = fm.get("title") or os.path.basename(link_path)
    state = fm.get("deployment_state") or fm.get("status") or "unknown"
    return f"- [{title}]({link_path}) — {state}"


def _display_handoff_archived(link_path: str, fm: dict) -> str:
    """bin/query-records.js:309."""
    title = fm.get("title") or os.path.basename(link_path)
    status = fm.get("status") or "unknown"
    shipped_in = fm.get("shipped_in")
    suffix = f" (shipped: {shipped_in})" if shipped_in else ""
    return f"- [{title}]({link_path}) — {status}{suffix}"


def _display_plan(link_path: str, fm: dict) -> str:
    """bin/query-records.js:311."""
    title = fm.get("title") or os.path.basename(link_path)
    status = fm.get("status") or "unknown"
    return f"- [{title}]({link_path}) — {status}"


def _display_cross_repo_memo(link_path: str, fm: dict) -> str:
    """bin/query-records.js:316."""
    title = fm.get("title") or os.path.basename(link_path)
    status = fm.get("status") or "unknown"
    from_repo = fm.get("from") or "?"
    return f"- [{title}]({link_path}) — {status} (from {from_repo})"


def _display_debt(link_path: str, fm: dict) -> str:
    """bin/query-records.js:317."""
    title = fm.get("title") or os.path.basename(link_path)
    severity = fm.get("severity") or "P?"
    status = fm.get("status") or "unknown"
    source = fm.get("source") or "?"
    return f"- [{title}]({link_path}) — {severity} {status} (source: {source})"


def _display_bug(link_path: str, fm: dict) -> str:
    """bin/query-records.js:320."""
    title = fm.get("title") or os.path.basename(link_path)
    severity = fm.get("severity") or "P?"
    status = fm.get("status") or "unknown"
    surface = fm.get("surface") or "?"
    return f"- [{title}]({link_path}) — {severity} {status} (surface: {surface})"


def _display_improvement(link_path: str, fm: dict) -> str:
    """bin/query-records.js:321."""
    title = fm.get("title") or os.path.basename(link_path)
    status = fm.get("status") or "unknown"
    action = fm.get("proposed_action") or "?"
    return f"- [{title}]({link_path}) — {status} (action: {action})"


def _display_decision_guide(link_path: str, fm: dict) -> str:
    """bin/query-records.js:333."""
    title = fm.get("title") or os.path.basename(link_path)
    status = fm.get("status") or "unknown"
    decision_count = fm.get("decision_count")
    suffix = f" ({decision_count} DRs)" if decision_count else ""
    return f"- [{title}]({link_path}) — {status}{suffix}"


def _display_decision(link_path: str, fm: dict) -> str:
    """bin/query-records.js:310."""
    title = fm.get("title") or os.path.basename(link_path)
    status = fm.get("status") or "unknown"
    return f"- [{title}]({link_path}) — {status}"


def _display_review(link_path: str, fm: dict) -> str:
    """bin/query-records.js:312. ``??``, not ``||`` — ``findings_count: 0`` must
    render as 0, not '?'."""
    title = fm.get("title") or os.path.basename(link_path)
    reviewer = fm.get("reviewer") or "?"
    findings_count = fm.get("findings_count")
    findings = "?" if findings_count is None else findings_count
    return f"- [{title}]({link_path}) — reviewer: {reviewer}, findings: {findings}"


def _display_lesson(link_path: str, fm: dict) -> str:
    """bin/query-records.js:313."""
    title = fm.get("title") or link_path
    tier = fm.get("tier") or "untagged"
    return f"- **{title}** [{tier}]"


def _display_handoff_ledger(link_path: str, fm: dict) -> str:
    """bin/query-records.js:315. ``??`` sites (agent/opus dispatch counts) must
    render a 0 value as 0, not '?'."""
    tshirt = fm.get("tshirt") or "?"
    agent_dispatches = fm.get("agent_dispatches")
    agents = "?" if agent_dispatches is None else agent_dispatches
    opus_dispatches = fm.get("opus_dispatches")
    opus = "?" if opus_dispatches is None else opus_dispatches
    session_id = fm.get("session_id") or "?"
    created = fm.get("created") or "?"
    return (
        f"- [{link_path}] tshirt={tshirt} agents={agents} opus={opus} "
        f"session={session_id} created={created}"
    )


def _display_research_claim(link_path: str, fm: dict) -> str:
    """bin/query-records.js:337."""
    claim_text = fm.get("claim_text") or link_path
    confidence = fm.get("confidence") or "?"
    claim_type = fm.get("type") or "?"
    return f"- {claim_text} [{confidence}] ({claim_type})"


def _display_research_synthesis(link_path: str, fm: dict) -> str:
    """bin/query-records.js:336. ``??``, not ``||`` — a ``coverage_score: 0``
    must render as 0, not '?'."""
    title = fm.get("title") or link_path
    pipeline = fm.get("pipeline") or "?"
    coverage_score = fm.get("coverage_score")
    score = "?" if coverage_score is None else coverage_score
    return f"- [{title}]({link_path}) — pipeline: {pipeline}, score: {score}"


def _display_coverage_audit(link_path: str, fm: dict) -> str:
    """bin/query-records.js:338. Links on the basename, not ``title`` — the
    oracle has no title field for this type. ``??`` on both counts — a
    ``present_count: 0``/``absent_count: 0`` must render as 0, not '?'."""
    present_count = fm.get("present_count")
    present = "?" if present_count is None else present_count
    absent_count = fm.get("absent_count")
    absent = "?" if absent_count is None else absent_count
    return f"- [{os.path.basename(link_path)}]({link_path}) — present: {present}, absent: {absent}"


def _display_gap_report(link_path: str, fm: dict) -> str:
    """bin/query-records.js:339. Links on the basename, same as coverage-audit.
    ``deepening_recommended`` has NO ``||``/``??`` fallback in the oracle — a
    missing value interpolates the literal string "undefined" (same faithful
    oracle quirk as `_display_completion`, via `_js_undefined_str`)."""
    gap_count = fm.get("gap_count")
    gaps = "?" if gap_count is None else gap_count
    coverage_score = fm.get("coverage_score")
    score = "?" if coverage_score is None else coverage_score
    deepening = _js_undefined_str(fm.get("deepening_recommended"))
    return (
        f"- [{os.path.basename(link_path)}]({link_path}) — gaps: {gaps}, "
        f"score: {score}, deepening: {deepening}"
    )


def _js_undefined_str(value: object) -> str:
    """JS template-literal interpolation of a missing property prints the
    literal string "undefined" (`` `${fm.title}` `` with no `` || `` fallback).
    `None` -> ``"undefined"``; a Python bool is lowered to JS's ``true``/
    ``false`` (``str(True)`` is ``"True"`` in Python but a JS template literal
    interpolates a boolean as lowercase) — needed by `_display_gap_report`'s
    `deepening_recommended` field, which is commonly boolean-valued YAML."""
    if value is None:
        return "undefined"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _display_completion(link_path: str, fm: dict) -> str:
    """bin/query-records.js:314 — faithful oracle quirk, NOT a bug to fix
    silently: `fm.title`/`fm.nature` have no `` || `` fallback in the oracle,
    so a completion record missing either field renders the literal string
    "undefined" (mirrored via `_js_undefined_str`), not an empty title."""
    title = _js_undefined_str(fm.get("title"))
    nature = _js_undefined_str(fm.get("nature"))
    chain = fm.get("chain") or "none"
    commits = fm.get("commits")
    commits_str = ", ".join(str(c) for c in commits) if commits else "no-commit"
    return f"- **{title}** [{nature}] (chain: {chain}) — {commits_str}"


def _default_display(link_path: str, fm: dict) -> str:
    """bin/query-records.js:1616 — the fallback used for any type absent from
    TYPE_DISPLAY (e.g. tracker/roadmap/health-status) or, in the multi-type
    `--unattached` lens (unsupported here — see module negative-spec), a
    record whose `_type` has no renderer either."""
    return f"- [{fm.get('title') or link_path}]({link_path})"


TYPE_DISPLAY: dict[str, _DisplayFn] = {
    "handoff": _display_handoff,
    "handoff-archived": _display_handoff_archived,
    "plan": _display_plan,
    "cross-repo-memo": _display_cross_repo_memo,
    "debt": _display_debt,
    "bug": _display_bug,
    "improvement": _display_improvement,
    "decision-guide": _display_decision_guide,
    "completion": _display_completion,
    "decision": _display_decision,
    "review": _display_review,
    "lesson": _display_lesson,
    "handoff-ledger": _display_handoff_ledger,
    "research-claim": _display_research_claim,
    "research-synthesis": _display_research_synthesis,
    "gap-report": _display_gap_report,
    "coverage-audit": _display_coverage_audit,
}


# ---------------------------------------------------------------------------
# formatRecords — markdown-list / json / paths, with depth-correct link
# rewriting relative to the embedding (callout) file's directory.
# ---------------------------------------------------------------------------


def _relativize_link(rel_path: str, root: Path, from_dir: Path) -> str:
    """Rewrite a repo-root-relative record path into one relative to `from_dir`.

    Port of bin/query-records.js:1627-1633. Fragment-suffixed synthetic paths
    (`#claim-N` from research-claim, `#ledger-N` from handoff-ledger) are
    exactly what this defensive handling was written for: the `#fragment` is
    split off before path resolution and reattached after, since relpath has
    no concept of a URL fragment and would otherwise corrupt it.
    """
    hash_idx = rel_path.find("#")
    path_part = rel_path if hash_idx == -1 else rel_path[:hash_idx]
    fragment = "" if hash_idx == -1 else rel_path[hash_idx:]
    abs_target = (root / path_part).resolve()
    link = os.path.relpath(str(abs_target), str(from_dir)).replace("\\", "/")
    return link + fragment


def format_records(
    records: list[dict],
    query_opts: dict,
    *,
    root: Optional[Path] = None,
    from_dir: Optional[Path] = None,
) -> str:
    """Render queried records per `query_opts["format"]` — mirrors formatRecords
    (bin/query-records.js:1601-1638).

    `root`/`from_dir` provided together enable the markdown-list link-rewrite
    (a query callout's expansion must link relative to the callout FILE's own
    directory, not the repo root — the same expansion embedded at different
    tree depths needs different `../` prefixes). Absent, `record["path"]`
    (repo-root-relative) is used verbatim — matches the oracle's own
    stdout/CLI-invocation shape, which has no "containing file" to be
    depth-correct against.
    """
    fmt = query_opts.get("format") or "markdown-list"

    if fmt == "json":
        return json.dumps(records, indent=2)
    if fmt == "paths":
        return "\n".join(r["path"] for r in records)

    global_display = TYPE_DISPLAY.get(query_opts.get("type"))
    lines: list[str] = []
    for rec in records:
        fn = global_display or TYPE_DISPLAY.get(rec.get("_type")) or _default_display
        link_path = rec["path"]
        if root is not None and from_dir is not None:
            link_path = _relativize_link(rec["path"], root, from_dir)
        lines.append(fn(link_path, rec.get("frontmatter") or {}))
    return "\n".join(lines)
