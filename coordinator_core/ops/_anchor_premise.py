"""Anchor premises: parse the plan's declared load-bearing premises and compute the findings
that keep a plan with an unchecked one from reading as ready. No IO, no spawn.

Shape: frontmatter ``anchor_premises:`` is a list of ``{id, claim, check: {command, expect}}``
(``[]`` when the plan rests on none). A premise-checker verdict row names its anchor with
``anchor: <id>``; ``line`` is the 1-based plan line of that anchor's ``command:``.

Negative-spec: the engine never runs a check command and never reads prose for premises. A
command is only screened (``census.screen``) and a verdict is only matched by stripped id; a
verdict outside ``VERDICTS`` counts as none.
"""

from pathlib import Path
from typing import Any, Optional

from coordinator_core.ops._research_grounding import line_hashes

FIELD = "anchor_premises"
VERDICTS: frozenset = frozenset({"RESOLVES", "UNRESOLVED", "CONTRADICTED", "UNCHECKABLE"})
_CLAIM_CAP = 160


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _unchecked(label: str, reason: str) -> str:
    return f"anchor-premise-unchecked: {label} ({reason})"


def parse_anchors(fm: dict, repo_root: Optional[Path]) -> tuple[list[dict], list[str]]:
    """(well-formed anchors, defect findings) for the plan frontmatter ``fm``."""
    if FIELD not in fm:
        return [], [
            "anchor-premises-undeclared (declare each load-bearing premise as "
            "{id, claim, check: {command, expect}}, or [] when the plan rests on none)"
        ]
    entries = fm.get(FIELD)
    if not isinstance(entries, list):
        return [], ["anchor-premises-malformed"]

    from coordinator_core.roadmap import census

    tracked: Optional[frozenset] = None
    if repo_root is not None:
        tracked = census.tracked_targets(
            repo_root,
            [
                c
                for e in entries
                if isinstance(e, dict) and isinstance(e.get("check"), dict)
                for c in census.python_candidates(str(e["check"].get("command") or ""), repo_root)
            ],
        )

    anchors: list[dict] = []
    findings: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        label = f"#{index}"
        if not isinstance(entry, dict):
            findings.append(_unchecked(label, "not a mapping"))
            continue
        if _text(entry.get("id")):
            label = entry["id"].strip()
        if not _text(entry.get("id")) or not _text(entry.get("claim")):
            findings.append(_unchecked(label, "id and claim must be non-blank strings"))
            continue
        if label in seen:
            findings.append(_unchecked(label, "duplicate id"))
            continue
        seen.add(label)
        check = entry.get("check")
        if not isinstance(check, dict):
            findings.append(_unchecked(label, "check must be {command, expect}"))
            continue
        if not _text(check.get("command")) or not _text(check.get("expect")):
            findings.append(_unchecked(label, "check needs a non-blank command and expect"))
            continue
        refusal = census.screen(check["command"], repo_root, tracked)
        if refusal:
            findings.append(
                _unchecked(label, f"{refusal}; allowed form: {census.ALLOWED_FORM}")
            )
            continue
        anchors.append({**entry, "id": label})
    return anchors, findings


def unjudged(reason: str) -> str:
    return f"anchor-premises-unjudged: {reason}"


def _named(row: Any) -> Optional[str]:
    named = row.get("anchor") if isinstance(row, dict) else None
    return named.strip() if isinstance(named, str) else None


def _valid(verdict: Any) -> bool:
    return isinstance(verdict, str) and verdict in VERDICTS


def _verdict_for(anchor_id: str, passed: list, carried: list, live: frozenset) -> Optional[str]:
    for row in passed:
        if _named(row) == anchor_id and _valid(row.get("verdict")):
            return row["verdict"]
    for row in carried:
        if (
            _named(row) == anchor_id
            and _valid(row.get("verdict"))
            and row.get("line_hash") is not None
            and row.get("line_hash") in live
        ):
            return row["verdict"]
    return None


def anchor_findings(anchors: list, rows: list, carry_rows: list, plan_text: str) -> list[str]:
    """One finding per anchor not resolved. Passed ``rows`` win over ``carry_rows``; a carry row
    counts only while its ``line_hash`` is still a line of ``plan_text``."""
    live = frozenset(line_hashes(plan_text))
    out: list[str] = []
    known = {a["id"] for a in anchors}
    for index, row in enumerate(rows or []):
        if not isinstance(row, dict):
            out.append(f"anchor-premise-malformed-row: rows[{index}] (not a mapping)")
            continue
        named = _named(row)
        if named is not None and named not in known:
            out.append(f"anchor-premise-unknown: {named} (no declared anchor has this id)")
    for anchor in anchors:
        verdict = _verdict_for(anchor["id"], rows or [], carry_rows or [], live)
        if verdict is None:
            out.append(f"anchor-premise-no-verdict: {anchor['id']}")
        elif verdict != "RESOLVES":
            out.append(
                f"anchor-premise-{verdict}: {anchor['id']} -- {str(anchor['claim'])[:_CLAIM_CAP]}"
            )
    return out
