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
                                  A CONTRADICTED row whose citation the plan's frontmatter
                                  ``corrects: [{citation, fixed_by}]`` names, with ``fixed_by`` an
                                  open or coded spine row, is a note; a composite citation of
                                  several sentences is a note when every sentence is touched by a
                                  live entry, and pulls when any sentence is left uncited. A class-4 row whose optional
                                  ``baseline_ref`` differs from the plan's current falsifier
                                  ``baseline_ref`` is stale and a note. An UNRESOLVED row is exempt
                                  only through the plan's ``declared_absent: [{citation,
                                  tracked_by}]``, with ``tracked_by`` an existing repo-relative path,
                                  an http(s) URL, or text in the plan's ``## Open questions``;
                                  CONTRADICTED rows are never exempted by it.
                                  When passed, a ``premise-check`` reviews entry is dropped: the
                                  rows carry that verdict, and refusing the pair would hand the
                                  caller an unusable reply.

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
import re
from pathlib import Path
from typing import Any, List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops._param_alias import aliased_param, spellings
from coordinator_core.ops.plan_prep_gate import _handler as _prep_gate_handler
from coordinator_core.ops.review_findings_ledger import read_ledger
from coordinator_core.roadmap.prep_gate import ZERO_DISPATCHABLE_KIND

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
_LIVE_DISPOSITIONS = frozenset({"open", "coded"})
# A `fixed_by` of landed commits (the premise checker's own "on corrects: -> <sha>" form) names
# its fix as surely as a live row id; a token that is neither covers nothing.
_SHA_LIST = re.compile(r"[0-9a-f]{7,40}(\s*,\s*[0-9a-f]{7,40})*")
_PULL_VERDICTS = frozenset({"pulled", "judgment"})


def _clear(verdict: Any) -> bool:
    return isinstance(verdict, str) and verdict.strip().lower() in _CLEAR_VERDICTS


def _ledger_read(review: dict, repo_root: Optional[Path]) -> Optional[tuple]:
    """(stamped, rows) of the review's ledger sidecar, else None; resolves on the path alone."""
    ledger = review.get("ledger")
    if not isinstance(ledger, str) or not ledger.strip():
        return None
    path = Path(ledger.strip())
    if not path.is_absolute():
        if repo_root is None:
            return None
        path = repo_root / path
    return read_ledger(path)


def _row_cleared(row: dict) -> bool:
    return row.get("verified") is True and (row.get("status") or "applied") == "applied"


def _rows_clear(read: Optional[tuple]) -> bool:
    if read is None:
        return False
    stamped, rows = read
    return stamped and bool(rows) and all(_row_cleared(row) for row in rows)


def _ledger_verdict(review: dict) -> bool:
    verdict = review.get("verdict")
    return isinstance(verdict, str) and verdict.strip().lower() in _LEDGER_VERDICTS


def _ledger_clear(review: dict, repo_root: Optional[Path]) -> bool:
    """A WARN/BLOCKED review is clear only on a stamped sidecar whose rows are all applied+verified."""
    return _ledger_verdict(review) and _rows_clear(_ledger_read(review, repo_root))


def _uncleared_count(read: Optional[tuple]) -> int:
    """Uncleared ledger rows of a review that failed to clear; never below 1."""
    rows = read[1] if read is not None else []
    return max(sum(1 for row in rows if not _row_cleared(row)), 1)


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


def _norm(text: Any) -> str:
    return " ".join(text.split()) if isinstance(text, str) else ""


# ``_norm`` collapses newlines to spaces, so a sentence ends only at terminal punctuation.
_SENTENCE_BREAK = re.compile(r"[.;!?]+(?:\s+|$)")
_HAS_WORD = re.compile(r"\w")


def _covering_row(row: dict, corrects: Any, spine_rows: Optional[List[dict]]) -> Optional[tuple]:
    """(citation, row id) of the first ``corrects`` entry that covers this premise row, else None.
    ``fixed_by`` is a live (open/coded) row id or a comma list of commit shas; malformed entries,
    unknown row ids and rows not open/coded cover nothing."""
    if not isinstance(corrects, list):
        return None
    live = {
        str(r.get("id")).strip()
        for r in spine_rows or []
        if isinstance(r, dict) and str(r.get("disposition") or "").strip() in _LIVE_DISPOSITIONS
    }
    cited = _norm(row.get("citation"))
    entries = []
    for entry in corrects:
        if not isinstance(entry, dict):
            continue
        citation, fixed_by = _norm(entry.get("citation")), entry.get("fixed_by")
        fix = fixed_by.strip() if isinstance(fixed_by, str) else ""
        if citation and (fix in live or _SHA_LIST.fullmatch(fix)):
            entries.append((citation, fix))
    # Each live entry that occurs in the row covers every sentence its span touches; the row is
    # covered when no sentence is left uncited.
    spans = []
    for citation, fix in entries:
        start = cited.find(citation)
        if start >= 0:
            spans.append((start, start + len(citation), citation, fix))
    if not spans:
        return None
    hits, begin = [], 0
    for brk in [*_SENTENCE_BREAK.finditer(cited), None]:
        end = brk.start() if brk else len(cited)
        if _HAS_WORD.search(cited[begin:end]):
            hit = next((s for s in spans if s[0] < end and s[1] > begin), None)
            if hit is None:
                return None
            hits.append(hit[2:])
        begin = brk.end() if brk else end
    return (
        " + ".join(dict.fromkeys(h[0] for h in hits)),
        ", ".join(dict.fromkeys(h[1] for h in hits)),
    )


_OPEN_QUESTIONS = re.compile(r"^##[ \t]+Open questions[ \t]*$(.*?)(?=^##[ \t]|\Z)", re.M | re.S | re.I)


def _open_questions_body(text: str) -> str:
    match = _OPEN_QUESTIONS.search(text)
    return match.group(1) if match else ""


def _tracked(tracked_by: str, open_questions: str, repo_root: Optional[Path]) -> bool:
    """True when ``tracked_by`` names a filed record: an http(s) URL, a repo-relative path that
    exists under ``repo_root``, or text verbatim in the plan's ``## Open questions`` body."""
    if re.match(r"https?://\S", tracked_by):
        return True
    if len(tracked_by) >= 8 and tracked_by in open_questions:
        return True
    rel = Path(tracked_by)
    if repo_root is None or rel.is_absolute() or ".." in rel.parts:
        return False
    from coordinator_core.lifecycle import main_worktree_root

    try:
        return (Path(main_worktree_root(repo_root)) / rel).exists()
    except OSError:
        return False


def _declared_absent_entry(
    row: dict, declared_absent: Any, open_questions: str, repo_root: Optional[Path]
) -> Optional[tuple]:
    """(citation, tracked_by) of the first ``declared_absent`` entry that covers this UNRESOLVED
    premise row, else None. Malformed entries and an unresolvable ``tracked_by`` cover nothing."""
    if not isinstance(declared_absent, list):
        return None
    cited = _norm(row.get("citation"))
    for entry in declared_absent:
        if not isinstance(entry, dict):
            continue
        citation, tracked_by = _norm(entry.get("citation")), entry.get("tracked_by")
        tracked = tracked_by.strip() if isinstance(tracked_by, str) else ""
        if citation and citation in cited and tracked and _tracked(tracked, open_questions, repo_root):
            return citation, tracked
    return None


def _plan_inputs(prep_gate: dict, repo_root: Path) -> tuple:
    """(corrects, spine rows, falsifier baseline_ref, declared_absent, open-questions body) read
    from the plan file the prep gate resolved."""
    from coordinator_core.lifecycle import main_worktree_root
    from coordinator_core.roadmap.prep_gate import plan_frontmatter, raw_spine_rows

    path = Path(main_worktree_root(repo_root)) / str(prep_gate["plan"])
    text = path.read_text(encoding="utf-8", errors="replace")
    fm = plan_frontmatter(text)
    pec = fm.get("prime_exit_criterion")
    falsifier = pec.get("falsifier") if isinstance(pec, dict) else None
    ref = falsifier.get("baseline_ref") if isinstance(falsifier, dict) else None
    return (
        fm.get("corrects"),
        raw_spine_rows(text),
        ref.strip() if isinstance(ref, str) and ref.strip() else None,
        fm.get("declared_absent"),
        _open_questions_body(text),
    )


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
            if "baseline_ref" in row and not isinstance(row["baseline_ref"], str):
                raise ValueError(f"premise.rows[{i}].baseline_ref must be a string")
        rows = premise["rows"]
        reviews = [r for r in reviews or [] if r.get("reviewer") != _PREMISE_REVIEWER]
    return raw_plan.strip(), coverage, reviews or [], rows


def readiness_verdict(
    prep_gate: dict,
    coverage: Optional[dict],
    reviews: List[dict],
    repo_root: Optional[Path] = None,
    premise_rows: Optional[List[dict]] = None,
    corrects: Optional[Any] = None,
    spine_rows: Optional[List[dict]] = None,
    falsifier_baseline_ref: Optional[str] = None,
    declared_absent: Optional[Any] = None,
    open_questions: str = "",
) -> dict:
    """Verdict over a prep-gate reply plus coverage and reviewer values; the only read beyond
    them is a review's ledger sidecar, resolved against ``repo_root``. Only UNRESOLVED and
    CONTRADICTED premise rows block; UNCHECKABLE and RESOLVES rows never do. A CONTRADICTED row
    covered by ``corrects`` (a spine row in ``spine_rows`` that is open or coded fixes it) and a
    row whose ``baseline_ref`` differs from ``falsifier_baseline_ref`` are notes, not blockers, as
    is an UNRESOLVED row a ``declared_absent`` entry covers through a resolvable ``tracked_by``."""
    notes: List[str] = []
    repairs: List[dict] = []
    substantive: List[str] = []
    judgment: List[str] = []
    blocking_findings = advisory_findings = 0

    if prep_gate.get("verdict") not in _PREP_VERDICTS_COMPUTED:
        substantive.append(f"prep gate verdict {prep_gate.get('verdict')}")
    for name, cls in (prep_gate.get("classes") or {}).items():
        if cls.get("status") == "PASS":
            continue
        if cls.get("mechanical") and cls.get("repair"):
            repairs.append(_repair(str(cls.get("kind") or name), name, cls["repair"]))
        else:
            entry = f"{name}:{cls.get('kind') or cls.get('status')}"
            if cls.get("kind") == ZERO_DISPATCHABLE_KIND:
                entry += f" ({cls.get('detail')})"
            substantive.append(entry)

    if coverage is not None:
        mechanical = _findings(coverage.get("mechanicalFindings"), "mechanicalFindings")
        judged = _findings(coverage.get("judgmentFindings"), "judgmentFindings")
        if judged:
            judgment.append(f"{len(judged)} judgment coverage finding(s)")
            if str(coverage.get("verdict")).strip().upper() == "BLOCKED-SURFACE-TO-PM":
                blocking_findings += max(len(judged), 1)
            else:
                advisory_findings += len(judged)
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
        if _clear(review.get("verdict")):
            continue
        read = _ledger_read(review, repo_root)
        if _ledger_verdict(review) and _rows_clear(read):
            continue
        judgment.append(f"{review.get('reviewer')}: {review.get('verdict')!r}")
        if str(review.get("verdict")).strip().lower() == "warn":
            advisory_findings += _uncleared_count(read)
        else:
            blocking_findings += _uncleared_count(read)

    uncheckable = 0
    premise_blocking: List[str] = []
    covered: List[str] = []
    absent_notes: List[str] = []
    stale_refs: List[str] = []
    for row in premise_rows or []:
        ref = row.get("baseline_ref")
        if ref is not None and falsifier_baseline_ref is not None and ref.strip() != falsifier_baseline_ref:
            stale_refs.append(ref)
            continue
        if row["verdict"] in _PREMISE_BLOCKING:
            fixer = _covering_row(row, corrects, spine_rows) if row["verdict"] == "CONTRADICTED" else None
            absent = (
                _declared_absent_entry(row, declared_absent, open_questions, repo_root)
                if row["verdict"] == "UNRESOLVED"
                else None
            )
            if fixer is not None:
                covered.append(f"{fixer[0]} -> {fixer[1]}")
            elif absent is not None:
                absent_notes.append(f"{absent[0]} -> {absent[1]}")
            else:
                premise_blocking.append(f"{_PREMISE_REVIEWER}: {row['verdict']} {row['citation'][:200]}")
        elif row["verdict"] == "UNCHECKABLE":
            uncheckable += 1
    blocking_findings += len(premise_blocking)
    counts = f"needs an agent: {blocking_findings} blocking, {advisory_findings} advisory: "
    shown = list(premise_blocking)
    for i, entry in enumerate(shown):
        if len(counts) + len("; ".join(judgment + shown[: i + 1])) > _INSTRUCTION_MAX - 40:
            tail = len(shown) - i
            shown = shown[:i] + [f"+{tail} more premise row(s)"]
            break
    blocking_keys = [*substantive, *judgment, *premise_blocking]
    judgment.extend(shown)
    if uncheckable:
        notes.append(f"{uncheckable} UNCHECKABLE premise row(s) left to reviewer judgment")
    if covered:
        notes.append(f"{len(covered)} CONTRADICTED premise row(s) the plan corrects: " + "; ".join(covered))
    if absent_notes:
        notes.append(
            f"{len(absent_notes)} UNRESOLVED premise row(s) the plan declares absent: "
            + "; ".join(absent_notes)
        )
    if stale_refs:
        notes.append(
            f"{len(stale_refs)} premise row(s) read against superseded baseline "
            + ", ".join(sorted(set(stale_refs)))
        )

    if substantive:
        verdict, pull_class, repairs = "pulled", "substantive", []
        reason = "prep gate failure needs judgment: " + "; ".join(substantive)
    elif judgment:
        verdict, pull_class, repairs = "judgment", "none", []
        reason = counts + "; ".join(judgment)
    elif repairs:
        verdict, pull_class = "pulled", "mechanical"
        reason = f"{len(repairs)} mechanical repair(s) apply in one revise"
    else:
        verdict, pull_class = "ready", "none"
        blocking_findings = advisory_findings = 0
        reason = "prep gate passes with no outstanding findings"
        if notes:
            reason += "; " + "; ".join(notes)
    return {
        "verdict": verdict,
        "pull_class": pull_class,
        "mechanical_repairs": repairs,
        "reason": reason[:1000],
        "blocking_findings": blocking_findings,
        "advisory_findings": advisory_findings,
        "blocking": sorted({key[:300] for key in blocking_keys}) if verdict in _PULL_VERDICTS else [],
    }


@register_op("plan.readiness_verdict")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.readiness_verdict" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("plan.readiness_verdict requires a resolved repo_root")
    raw_plan, coverage, reviews, premise_rows = _check_params(params)
    prep_gate = _prep_gate_handler({"plan": raw_plan}, repo_root)
    corrects, spine_rows, ref, absent, questions = (
        _plan_inputs(prep_gate, repo_root) if premise_rows else (None, None, None, None, "")
    )
    out = readiness_verdict(
        prep_gate, coverage, reviews, repo_root, premise_rows, corrects, spine_rows, ref,
        absent, questions,
    )
    return {"plan": prep_gate["plan"], **out, "prep_gate": prep_gate}
