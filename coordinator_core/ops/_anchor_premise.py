"""Anchor premises: parse the plan's declared load-bearing premises and compute the findings
that keep a plan with an unchecked one from reading as ready. No IO, no spawn.

Shape: frontmatter ``anchor_premises:`` is a list of ``{id, claim, check: {command, expect}}``
(``[]`` when the plan rests on none). A premise-checker verdict row names its anchor with
``anchor: <id>`` or a citation opening ``anchor <id>``, where the spec may be a list or range of ids. A RESOLVES row naming no anchor otherwise clears
an anchor whose id is a whole token of its citation or evidence.
``line`` is the 1-based plan line of that anchor's ``command:``.

Negative-spec: the engine never spawns or walks for a check and never reads prose for
premises. It screens a command (``census.screen``), evaluates the ``_anchor_check`` subset
in-process, and fills only the no-verdict gap; a verdict is only matched by stripped id, and a
verdict outside ``VERDICTS`` counts as none.
"""

import re
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ops import _anchor_check
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


_ID = r"[A-Za-z0-9_][A-Za-z0-9_.-]*"
_CITED = re.compile(rf"^\s*anchors?[:\s]+({_ID}(?:,\s*{_ID})*)", re.IGNORECASE)
_RANGE = re.compile(r"^(\D.*?)(\d+)(?:-|\.\.)(\1)(\d+)$")
_RANGE_CAP = 50


def _named(row: Any) -> Optional[str]:
    """The anchor id spec a row names: its ``anchor`` key, else a citation opening ``anchor <spec>``."""
    if not isinstance(row, dict):
        return None
    named = row.get("anchor")
    if isinstance(named, str):
        return named.strip()
    cited = row.get("citation")
    match = _CITED.match(cited) if isinstance(cited, str) else None
    return match.group(1).rstrip(".-") if match else None


def _expand(spec: str, known: frozenset) -> list[str]:
    """Ids a spec names: the whole spec when declared, else its list/range members (a range of
    at most ``_RANGE_CAP`` ids), else the spec itself."""
    if spec in known:
        return [spec]
    out: list[str] = []
    for token in (t.rstrip(".-") for t in re.split(r"[,\s]+", spec) if t):
        span = _RANGE.match(token)
        if token not in known and span and 0 <= int(span[4]) - int(span[2]) < _RANGE_CAP:
            width = len(span[2]) if span[2].startswith("0") else 0
            out += [f"{span[1]}{n:0{width}d}" for n in range(int(span[2]), int(span[4]) + 1)]
        else:
            out.append(token)
    return out or [spec]


def named_ids(row: Any, known: frozenset) -> Optional[list[str]]:
    """Every id a row's anchor spec names, or None when it names none."""
    spec = _named(row)
    return None if spec is None else _expand(spec, known)


def _valid(verdict: Any) -> bool:
    return isinstance(verdict, str) and verdict in VERDICTS


def _mentions(row: Any, anchor_id: str) -> bool:
    """True when a RESOLVES row carries ``anchor_id`` as a whole token in its citation or evidence."""
    if not isinstance(row, dict) or row.get("verdict") != "RESOLVES":
        return False
    pattern = re.compile(r"(?<![A-Za-z0-9_.-])" + re.escape(anchor_id) + r"(?![A-Za-z0-9_-])")
    return any(
        isinstance(row.get(key), str) and pattern.search(row[key]) for key in ("citation", "evidence")
    )


def _names(row: Any, anchor_id: str, known: frozenset = frozenset()) -> bool:
    ids = named_ids(row, known | {anchor_id})
    return anchor_id in ids if ids is not None else _mentions(row, anchor_id)


def _verdict_for(
    anchor_id: str, passed: list, carried: list, live: frozenset, known: frozenset = frozenset()
) -> tuple[Optional[str], str]:
    """(verdict, source) from the passed rows, then the live carry rows; ``(None, "none")`` else."""
    for row in passed:
        if _names(row, anchor_id, known) and _valid(row.get("verdict")):
            return row["verdict"], "row"
    for row in carried:
        if (
            _names(row, anchor_id, known)
            and _valid(row.get("verdict"))
            and row.get("line_hash") is not None
            and row.get("line_hash") in live
        ):
            return row["verdict"], "carry"
    return None, "none"


_ENGINE_VERDICT = {"MATCH": "RESOLVES", "MISMATCH": "CONTRADICTED"}


def anchor_verdicts(anchors, rows, carry_rows, plan_text, repo_root) -> list[dict]:
    """One {id, verdict, source, observed} per declared anchor, in declaration order.
    source: "row" | "carry" | "engine-check" | "none"; verdict is None only when source is "none".
    Precedence: passed row > live carry row > engine check (MATCH -> RESOLVES,
    MISMATCH -> CONTRADICTED) > none. NOT_EVALUABLE leaves source "none"."""
    live = frozenset(line_hashes(plan_text))
    known = frozenset(a["id"] for a in anchors)
    out: list[dict] = []
    for anchor in anchors:
        verdict, source = _verdict_for(anchor["id"], rows or [], carry_rows or [], live, known)
        observed = ""
        if verdict is None and repo_root is not None:
            check = anchor.get("check") or {}
            result = _anchor_check.evaluate(
                str(check.get("command") or ""), str(check.get("expect") or ""), repo_root
            )
            observed = str(result.get("observed") or "")
            engine = _ENGINE_VERDICT.get(result.get("state"))
            if engine is not None:
                verdict, source = engine, "engine-check"
            else:
                observed = str(result.get("detail") or "")
        out.append({"id": anchor["id"], "verdict": verdict, "source": source, "observed": observed})
    return out


def unevaluable_anchors(verdicts: list) -> list[tuple[str, str]]:
    """(id, detail) per none-source anchor whose engine check was NOT_EVALUABLE."""
    return [(v["id"], v["observed"]) for v in verdicts if v["verdict"] is None and v["observed"]]


def anchor_findings(
    anchors: list,
    rows: list,
    carry_rows: list,
    plan_text: str,
    repo_root: Optional[Path] = None,
    verdicts: Optional[list] = None,
    *,
    checker_ran: bool = True,
) -> list[str]:
    """One finding per anchor not resolved. Passed ``rows`` win over ``carry_rows``; a carry row
    counts only while its ``line_hash`` is still a line of ``plan_text``. A none-source anchor
    whose check was NOT_EVALUABLE gets the no-verdict finding with the suffix
    '(check not evaluable in-process: <detail>)', except when ``checker_ran`` is False: no
    premise-checker can supply its verdict, so ``unevaluable_anchors`` carries it instead."""
    out: list[str] = []
    known = frozenset(a["id"] for a in anchors)
    for index, row in enumerate(rows or []):
        if not isinstance(row, dict):
            out.append(f"anchor-premise-malformed-row: rows[{index}] (not a mapping)")
            continue
        named = _named(row)
        if named is not None and not known.intersection(_expand(named, known)):
            out.append(f"anchor-premise-unknown: {named} (no declared anchor has this id)")
    claims = {a["id"]: a["claim"] for a in anchors}
    if verdicts is None:
        verdicts = anchor_verdicts(anchors, rows, carry_rows, plan_text, repo_root)
    for entry in verdicts:
        verdict = entry["verdict"]
        if verdict is None:
            if not checker_ran and entry["observed"]:
                continue
            detail = (
                f" (check not evaluable in-process: {entry['observed']})" if entry["observed"] else ""
            )
            out.append(f"anchor-premise-no-verdict: {entry['id']}{detail}")
        elif verdict != "RESOLVES":
            out.append(
                f"anchor-premise-{verdict}: {entry['id']} -- {str(claims[entry['id']])[:_CLAIM_CAP]}"
            )
    return out
