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
                                  live entry, and pulls when any sentence is left uncited. A row
                                  with no entry text in it is covered when one of its anchors
                                  (``path:line``, ``<source> line N``, ``§ Section``) equals an
                                  anchor of a live entry; covered rows are notes marking an
                                  intended correction the resolver may take. A class-4 row whose optional
                                  ``baseline_ref`` differs from the plan's current falsifier
                                  ``baseline_ref`` is stale and a note. An UNRESOLVED row is exempt
                                  only through the plan's ``declared_absent: [{citation,
                                  tracked_by}]``, with ``tracked_by`` an existing repo-relative path,
                                  an http(s) URL, or text in the plan's ``## Open questions``;
                                  CONTRADICTED rows are never exempted by it.
                                  A CONTRADICTED row citing a path the plan's halted run (its
                                  ``sizing_object``) withholds is regraded RESOLVES with
                                  ``own_run_edit: <run_id>`` (``_own_run_premise``).
                                  When passed, a ``premise-check`` reviews entry is dropped: the
                                  rows carry that verdict, and refusing the pair would hand the
                                  caller an unusable reply. A row's optional string ``anchor`` names
                                  a plan ``anchor_premises[].id``: it is judged only through that
                                  anchor, and any verdict but RESOLVES pulls.

Plan inputs, parsed from the one plan read the prep gate makes: ``corrects``, ``declared_absent``, the spine, the falsifier
``baseline_ref``, ``## Open questions`` and ``anchor_premises`` (``[{id, claim, check: {command,
expect}}]``; absent, unchecked or verdict-less anchors pull, see ``_anchor_premise``). An anchor
with no passed row is also resolved by the plan's premise-carry record under ``plan_sidecars_dir``.
An unreadable plan file (``OSError``) pulls as ``anchor-premises-unjudged`` over a REFUSED prep gate.

A coverage verdict that is not clear but carries zero findings gives the plan nothing to fix:
it is a note on a ready reply, never a pull.

Reply: see coordinator_core/contract/readiness-verdict.schema.json (the schema wins).

Negative-spec:
  - Takes no skip, force or override parameter; any key outside the five above is refused.
  - Does NOT write, stamp or mutate anything, and does NOT sweep the corpus. It reads a review's
    ledger sidecar through ``review_findings_ledger.read_ledger`` and never calls ``verify``.
  - Reads the plan file once, through the prep gate; nothing here re-reads it.
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
from coordinator_core.ops import (
    _anchor_check,
    _anchor_premise,
    _own_run_premise,
    readiness_declared_absent,
)
from coordinator_core.ops._major_finding_answer import unanswered
from coordinator_core.ops._param_alias import aliased_param, spellings
from coordinator_core.ops.plan_prep_gate import prep_gate_with_text
from coordinator_core.ops.review_findings_ledger import read_ledger
from coordinator_core.roadmap.prep_gate import ZERO_DISPATCHABLE_KIND

GENERATES: list = []

_KNOWN_PARAMS = frozenset({"plan", "plan_path", "coverage", "reviews", "premise"})
# Mirrors DoE plan-blitz.mjs PREMISE_REVIEWER.
_PREMISE_REVIEWER = "premise-check"
_PREMISE_BLOCKING = frozenset({"UNRESOLVED", "CONTRADICTED"})
# Exempts only a CONTRADICTED row whose evidence carries this checker phrase; it depends on the
# checker's wording, and test_ref_stamped_drift_contradiction_is_a_note_... pins that others block.
_DRIFT_MARKER = "stated at the ref so drift, not load-bearing"


def _is_ref_stamped_drift(row: dict) -> bool:
    """A CONTRADICTED row whose own evidence says the claim is stamped at a ref and only drifted."""
    evidence = row.get("evidence")
    return isinstance(evidence, str) and _DRIFT_MARKER in " ".join(evidence.lower().split())


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
# A pull with at least this many blocking findings is too large for one baton.
SPLIT_BLOCKING_THRESHOLD = 12
_SUBJECT_MAX = 300
_PREMISE_ASK = {
    "CONTRADICTED": "add a `corrects:` entry naming the spine row that fixes it, or correct the plan's claim",
    "UNRESOLVED": "add a `declared_absent:` entry with a resolvable `tracked_by`, or supply the evidence",
    "DECLARED-ABSENT-PRESENT": "the plan declares this path absent but it exists at the base sha: "
    "drop it from `absent_at_head`, or fix the premise",
}


_ANCHOR_ASK = (
    "The route rests on this anchor premise: give it a check in the in-process form ("
    + _anchor_check.GRAMMAR
    + ") or, where a premise-checker runs, its verdict; or re-plan around the checked fact. "
    "A failed or unverifiable anchor halts."
)


def _ruling(index: int, source: str, subject: str, ask: str) -> dict:
    return {"id": f"J{index}", "source": source, "subject": subject[:_SUBJECT_MAX], "ask": ask}


def _premise_ask(entry: str) -> str:
    verdict = entry.split(": ", 1)[-1].split(" ", 1)[0]
    return "Rule on this premise row: " + _PREMISE_ASK.get(verdict, "resolve it in the plan") + "."


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


# A citation's anchor: `path.ext:LINE[-LINE]`, a `<source name> line N` reference, or a
# `§ Section` reference. Equality of anchors, never similarity of prose, ties a row to an entry.
_ANCHOR = re.compile(
    r"[\w./\\-]+\.\w+:\d+(?:-\d+)?"
    r"|[\w./\\-]+(?:\s[\w./\\-]+){0,2}\s+line\s+\d+(?:-\d+)?"
    r"|§\s*[^;.()]+"
)


def _anchors(text: str) -> frozenset:
    return frozenset(_norm(m.group(0)) for m in _ANCHOR.finditer(text))


def _anchor_cover(cited: str, entries: List[tuple]) -> Optional[tuple]:
    """The first live ``corrects`` entry whose declared anchor equals one of the row's anchors."""
    row_anchors = _anchors(cited)
    if not row_anchors:
        return None
    for citation, fix in entries:
        if row_anchors & _anchors(citation):
            return citation, fix
    return None


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
        return _anchor_cover(cited, entries)
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
        if not (citation and tracked and _names_citation(citation, cited)):
            continue
        if _tracked(tracked, open_questions, repo_root):
            return citation, tracked[:120]
    return None


_PATH_TOKEN = re.compile(r"[\w.@~+-]+(?:/[\w.@~+-]+)+|[\w-]+\.\w+")


def _writes_paths(spine_rows: Any) -> frozenset:
    """Posix repo-relative ``writes:`` paths of the spine rows: files the plan itself creates."""
    return frozenset(readiness_declared_absent._row_writes(spine_rows))


def _creates_citation(row: dict, created: frozenset) -> Optional[str]:
    """The path a spine row's ``writes:`` creates when this UNRESOLVED premise row's citation names
    it as a whole path and names no other path; None otherwise. Not base-sha gated: a ``writes:``
    path that exists at the base sha is exempted too; ``readiness_declared_absent`` is the gated check."""
    if not created:
        return None
    text = _norm(row.get("citation")).replace("\\", "/").replace("`", " ")
    tokens = {t[2:] if t.startswith("./") else t for t in _PATH_TOKEN.findall(text)}
    named = {t for t in tokens if t in created}
    if not named or tokens - named:
        return None
    return sorted(named)[0]


def _names_citation(entry_citation: str, cited: str) -> bool:
    """True when the premise row's citation contains the entry's citation, or the entry's leading
    token (its path: an entry may trail a parenthetical) as a whole token."""
    if entry_citation in cited:
        return True
    lead = entry_citation.split(" ", 1)[0].strip("`'\"")
    return ("/" in lead or "." in lead) and re.search(r"(?<![\w./-])" + re.escape(lead) + r"(?![\w/-]|\.\w)", cited) is not None


def _plan_inputs(text: str) -> tuple:
    """(corrects, spine rows, falsifier baseline_ref, declared_absent, open-questions body, plan
    text, frontmatter) parsed from the plan text the prep gate read."""
    from coordinator_core.roadmap.prep_gate import plan_frontmatter, raw_spine_rows

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
        text,
        fm,
    )


def _carry_rows(repo_root: Path, plan_rel: str) -> list:
    """Rows of the plan's premise-carry record (premise-carry-contract.md section 2); [] when the
    file is missing, unreadable or of another plan."""
    from coordinator_core.lifecycle import main_worktree_root
    from coordinator_core.ops import _premise_carry
    from coordinator_core.session.machinery_paths import plan_sidecars_dir

    path = Path(plan_sidecars_dir(str(main_worktree_root(repo_root)))) / f"{Path(plan_rel).stem}.premise-carry.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    record = _premise_carry.parse_record(raw, plan_rel)
    return record["rows"] if record else []


REGISTER_ROWS_UNCLAIMED_KIND = "register-rows-unclaimed"
MAJOR_FINDING_KIND = "major-finding-needs-fix-row"


def _repair(kind: str, target: str, instruction: str) -> dict:
    return {"kind": kind[:100], "target": target[:300], "instruction": instruction[:_INSTRUCTION_MAX]}


_UNMAPPED_ANCHOR = re.compile(r"anchor-premise-unchecked: #(\d+) \(not a mapping\)")
_LEADING_COUNT = re.compile(r"\s*(\d+)\b")


def _census_check(claim: str, census: Any) -> Optional[dict]:
    """A GRAMMAR check from the plan's own census whose question or command matches ``claim``;
    ``expect`` is the entry's own ``expect`` or the leading count of its recorded ``result``."""
    want = _norm(claim).casefold()
    for entry in census if isinstance(census, list) else []:
        if not isinstance(entry, dict):
            continue
        command, question = _norm(entry.get("command")), _norm(entry.get("question")).casefold()
        if not command or _anchor_check.evaluable(command) is not None:
            continue
        if not (
            (question and (question in want or want in question))
            or command.casefold() in want
        ):
            continue
        expect = entry.get("expect")
        if not (isinstance(expect, str) and expect.strip()):
            counted = _LEADING_COUNT.match(str(entry.get("result")))
            expect = counted.group(1) if counted else ""
        if expect:
            return {"command": command, "expect": expect.strip()}
    return None


def _split_unmapped_anchors(findings: list, entries: Any, census: Any) -> tuple:
    """(findings that still rule, mechanical repairs). A `(not a mapping)` anchor becomes a repair
    only when the plan's own census yields a GRAMMAR check for its claim; otherwise it stays."""
    kept: list = []
    repairs: List[dict] = []
    for finding in findings:
        match = _UNMAPPED_ANCHOR.fullmatch(finding) if isinstance(finding, str) else None
        index = int(match.group(1)) if match else -1
        claim = entries[index] if isinstance(entries, list) and 0 <= index < len(entries) else None
        check = _census_check(claim, census) if isinstance(claim, str) and claim.strip() else None
        if check is None:
            kept.append(finding)
            continue
        mapping = json.dumps({"id": f"A{index + 1}", "claim": claim, "check": check})
        instruction = f"replace anchor_premises entry #{index} with the mapping {mapping}"
        if len(instruction) > _INSTRUCTION_MAX:
            kept.append(finding)
            continue
        repairs.append(_repair("anchor-premise-unchecked", f"#{index}", instruction))
    return kept, repairs


def _row_outranks_match(verdicts: list, anchors: list, rows: list) -> list:
    """A passed premise row that is not RESOLVES stands over an engine-check match: a checker that
    ruled on the anchor is never overwritten by the in-process check."""
    known = frozenset(a["id"] for a in anchors)
    out = []
    for entry in verdicts:
        if entry["source"] == "engine-check" and entry["verdict"] == "RESOLVES":
            verdict, source = _anchor_premise._verdict_for(entry["id"], rows or [], [], frozenset(), known)
            if source == "row" and verdict != "RESOLVES":
                entry = {"id": entry["id"], "verdict": verdict, "source": "row", "observed": ""}
        out.append(entry)
    return out


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
            if not isinstance(row, dict) or row.get("verdict") not in _anchor_premise.VERDICTS:
                raise ValueError(
                    f"premise.rows[{i}].verdict must be one of {', '.join(sorted(_anchor_premise.VERDICTS))}"
                )
            if not isinstance(row.get("citation"), str):
                raise ValueError(f"premise.rows[{i}].citation must be a string")
            if "baseline_ref" in row and not isinstance(row["baseline_ref"], str):
                raise ValueError(f"premise.rows[{i}].baseline_ref must be a string")
            if "anchor" in row and not isinstance(row["anchor"], str):
                raise ValueError(f"premise.rows[{i}].anchor must be a string")
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
    anchor_findings: Optional[List[str]] = None,
    anchor_verdicts: Optional[List[dict]] = None,
    checker_ran: bool = True,
    declared: Optional[Any] = None,
    anchor_entries: Optional[Any] = None,
    census: Optional[Any] = None,
) -> dict:
    """Verdict over a prep-gate reply plus coverage and reviewer values; the only read beyond
    them is a review's ledger sidecar, resolved against ``repo_root``. Only UNRESOLVED and
    CONTRADICTED premise rows block; UNCHECKABLE and RESOLVES rows never do. A CONTRADICTED row
    covered by ``corrects`` (a spine row in ``spine_rows`` that is open or coded fixes it) and a
    row whose ``baseline_ref`` differs from ``falsifier_baseline_ref`` are notes, not blockers, as
    is an UNRESOLVED row a ``declared_absent`` entry covers through a resolvable ``tracked_by``.
    Each ``anchor_findings`` entry is a blocking judgment no ``corrects``/``declared_absent`` exempts;
    rows carrying ``anchor`` are judged only through them. ``anchor_verdicts`` is the recorded
    per-anchor verdict list: it is echoed in the reply and feeds a ready-reason note. With
    ``checker_ran`` either way, an anchor whose check is not evaluable in-process, or that the
    census screen refuses, is a note naming its id: never a repair, a pull or a halt."""
    notes: List[str] = []
    repairs: List[dict] = []
    substantive: List[str] = []
    judgment: List[str] = []
    judgment_source: List[str] = []
    blocking_findings = advisory_findings = 0

    if prep_gate.get("verdict") not in _PREP_VERDICTS_COMPUTED:
        substantive.append(f"prep gate verdict {prep_gate.get('verdict')}")
    for name, cls in (prep_gate.get("classes") or {}).items():
        if cls.get("status") == "PASS":
            continue
        if cls.get("mechanical") and cls.get("repair"):
            repairs.append(_repair(str(cls.get("kind") or name), name, cls["repair"]))
        elif cls.get("kind") == REGISTER_ROWS_UNCLAIMED_KIND:
            repairs.append(_repair(
                REGISTER_ROWS_UNCLAIMED_KIND, name,
                "re-plan in-run and claim the unclaimed register rows (claim_register_rows): "
                + str(cls.get("detail") or ""),
            ))
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
            judgment_source.append("coverage")
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
        read = _ledger_read(review, repo_root)
        # A verified major default-path finding with no fix row or G-EM scope ruling pulls under any review verdict.
        for row in unanswered(read[1] if read is not None else [], spine_rows, repo_root):
            repairs.append(_repair(
                MAJOR_FINDING_KIND, str(row.get("id") or "?"),
                "answer this major default-path finding with a fix row in the plan's spine "
                "(answered_by: {kind: fix-row, row: <id>}) or a G-EM scope ruling "
                "(answered_by: {kind: gem-scope-ruling, ref: <repo-relative file>}); "
                "a disclosure or PM waiver does not clear it",
            ))
        if _clear(review.get("verdict")):
            continue
        if _ledger_verdict(review) and _rows_clear(read):
            continue
        judgment.append(f"{review.get('reviewer')}: {review.get('verdict')!r}")
        judgment_source.append("review")
        if str(review.get("verdict")).strip().lower() == "warn":
            advisory_findings += _uncleared_count(read)
        else:
            blocking_findings += _uncleared_count(read)

    uncheckable = 0
    premise_blocking: List[str] = []
    covered: List[str] = []
    absent_notes: List[str] = []
    stale_refs: List[str] = []
    drift_notes: List[str] = []
    created = _writes_paths(spine_rows)
    for row in premise_rows or []:
        if "anchor" in row:
            continue
        ref = row.get("baseline_ref")
        if ref is not None and falsifier_baseline_ref is not None and ref.strip() != falsifier_baseline_ref:
            stale_refs.append(ref)
            continue
        if row["verdict"] == "CONTRADICTED" and _is_ref_stamped_drift(row):
            drift_notes.append(row["citation"][:120])
            continue
        if row["verdict"] in _PREMISE_BLOCKING:
            fixer = _covering_row(row, corrects, spine_rows) if row["verdict"] == "CONTRADICTED" else None
            ruling = (
                readiness_declared_absent.classify_unresolved(row.get("citation"), declared)
                if declared is not None and row["verdict"] == "UNRESOLVED"
                else readiness_declared_absent.BLOCKS
            )
            if ruling == readiness_declared_absent.DECLARED_ABSENT_BUT_PRESENT:
                premise_blocking.append(f"{_PREMISE_REVIEWER}: DECLARED-ABSENT-PRESENT {row['citation'][:200]}")
                continue
            if ruling == readiness_declared_absent.EXPECTED_ABSENT:
                absent_notes.append(f"{_norm(row.get('citation'))[:120]} -> expected absent: declared absent at the base sha")
                continue
            absent = (
                _declared_absent_entry(row, declared_absent, open_questions, repo_root)
                if row["verdict"] == "UNRESOLVED"
                else None
            )
            created_path = _creates_citation(row, created) if row["verdict"] == "UNRESOLVED" else None
            if fixer is not None:
                covered.append(f"{fixer[0]} -> {fixer[1]}")
            elif created_path is not None:
                absent_notes.append(f"{created_path} -> expected absent: created by a spine row's writes")
            elif absent is not None:
                absent_notes.append(f"{absent[0]} -> {absent[1]}")
            else:
                premise_blocking.append(f"{_PREMISE_REVIEWER}: {row['verdict']} {row['citation'][:200]}")
        elif row["verdict"] == "UNCHECKABLE":
            uncheckable += 1
    anchor_findings, refused = _anchor_premise.split_screen_refused(anchor_findings or [])
    anchor_findings, anchor_repairs = _split_unmapped_anchors(anchor_findings, anchor_entries, census)
    repairs.extend(anchor_repairs)
    blocking_findings += len(premise_blocking) + len(anchor_findings or [])
    counts = f"needs an agent: {blocking_findings} blocking, {advisory_findings} advisory: "
    # Anchor findings are budgeted before premise rows: the reason is cut at _INSTRUCTION_MAX,
    # and a cut that drops the anchor ids sends the reviser after the wrong premise.
    budget = _INSTRUCTION_MAX - 40
    anchor_shown = list(anchor_findings or [])
    for i in range(len(anchor_shown)):
        if len(counts) + len("; ".join(judgment + anchor_shown[: i + 1])) > budget:
            anchor_shown = anchor_shown[: max(i, 1)] + [
                f"+{len(anchor_shown) - max(i, 1)} more anchor finding(s)"
            ]
            break
    shown = list(premise_blocking)
    for i, entry in enumerate(shown):
        if len(counts) + len("; ".join(judgment + shown[: i + 1] + anchor_shown)) > budget:
            tail = len(shown) - i
            shown = shown[:i] + [f"+{tail} more premise row(s)"]
            break
    blocking_keys = [*substantive, *judgment, *premise_blocking, *(anchor_findings or [])]
    rulings: List[dict] = []
    for entry in substantive:
        rulings.append(_ruling(
            len(rulings) + 1, "prep-gate", entry,
            "Fix the named class in the plan so the prep gate passes, or rule it out of scope.",
        ))
    for entry, source in zip(judgment, judgment_source):
        ask = (
            "Rule on the judgment coverage findings: revise the plan or accept them."
            if source == "coverage"
            else "Rule on this review verdict: revise the plan for its findings or accept them."
        )
        rulings.append(_ruling(len(rulings) + 1, source, entry, ask))
    for entry in premise_blocking:
        rulings.append(_ruling(len(rulings) + 1, "premise", entry, _premise_ask(entry)))
    for entry in anchor_findings or []:
        rulings.append(_ruling(len(rulings) + 1, "premise", entry, _ANCHOR_ASK))
    judgment.extend(shown)
    judgment.extend(anchor_shown)
    engine_checked = sum(1 for v in anchor_verdicts or [] if v.get("source") == "engine-check")
    if engine_checked:
        notes.append(f"{engine_checked} anchor premise(s) resolved by in-process check")
    unevaluable = _anchor_premise.unevaluable_anchors(anchor_verdicts or []) + refused
    if unevaluable and checker_ran:
        notes.append(
            "anchor check not evaluable in-process, not blocking (premise-checker ran, no blocking row): "
            + "; ".join(f"{i} ({d[:80]})" for i, d in unevaluable)
        )
    elif unevaluable:
        for anchor_id, detail in unevaluable:
            repairs.append(_repair(
                "anchor-check-not-evaluable", anchor_id,
                f"rewrite anchor {anchor_id}'s check in the in-process form ({_anchor_check.GRAMMAR}); "
                f"it was not evaluable: {detail[:200]}",
            ))
    if uncheckable:
        notes.append(f"{uncheckable} UNCHECKABLE premise row(s) left to reviewer judgment")
    if covered:
        notes.append(
            f"{len(covered)} CONTRADICTED premise row(s) the plan corrects "
            "(intended corrections the resolver may take): " + "; ".join(covered)
        )
    if absent_notes:
        notes.append(
            f"{len(absent_notes)} UNRESOLVED premise row(s) the plan declares absent: "
            + "; ".join(absent_notes)
        )
    if drift_notes:
        notes.append(
            f"{len(drift_notes)} CONTRADICTED premise row(s) the checker calls ref-stamped drift: "
            + "; ".join(drift_notes)
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
    pulled = verdict in _PULL_VERDICTS
    return {
        "verdict": verdict,
        "rulings": rulings if pulled else [],
        "split_recommended": pulled and pull_class != "mechanical"
        and blocking_findings >= SPLIT_BLOCKING_THRESHOLD,
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
    declared = None
    checker_ran = params.get("premise") is not None or any(
        r.get("reviewer") == _PREMISE_REVIEWER for r in params.get("reviews") or []
    )
    try:
        prep_gate, plan_text = prep_gate_with_text(raw_plan, repo_root)
    except OSError as exc:
        reason = f"plan unreadable ({type(exc).__name__})"
        prep_gate = {
            "plan": raw_plan, "verdict": "REFUSED", "withheld_rows": [], "classes": {},
            "message": reason,
        }
        corrects, spine_rows, ref, absent, questions = None, None, None, None, ""
        anchor, verdicts, anchor_entries, census = [_anchor_premise.unjudged(reason)], [], None, None
    else:
        corrects, spine_rows, ref, absent, questions, text, fm = _plan_inputs(plan_text)
        from coordinator_core.lifecycle import main_worktree_root

        anchor_entries, census = fm.get(_anchor_premise.FIELD), fm.get("census")
        anchor_root = Path(main_worktree_root(repo_root))
        own = _own_run_premise.own_run_paths(anchor_root, fm.get("sizing_object"))
        premise_rows = _own_run_premise.regrade(premise_rows, own)
        anchors, defects = _anchor_premise.parse_anchors(fm, anchor_root)
        carried = (
            _own_run_premise.regrade(_carry_rows(repo_root, str(prep_gate["plan"])), own) if anchors else []
        )
        verdicts = _row_outranks_match(
            _anchor_premise.anchor_verdicts(anchors, premise_rows, carried, text, anchor_root),
            anchors, premise_rows,
        )
        anchor = defects + _anchor_premise.anchor_findings(
            anchors, premise_rows, carried, text, anchor_root, verdicts
        )
        # The plan's falsifier baseline_ref stands in for the run's base sha (the op's one sha input; it can predate the run);
        # the git read happens only when some UNRESOLVED premise row exists to rule on.
        if any(r.get("verdict") == "UNRESOLVED" and "anchor" not in r for r in premise_rows or []):
            declared = readiness_declared_absent.declared_absent_paths(repo_root, ref, spine_rows, fm)
    out = readiness_verdict(
        prep_gate, coverage, reviews, repo_root, premise_rows, corrects, spine_rows, ref,
        absent, questions, anchor, verdicts, checker_ran, declared, anchor_entries, census,
    )
    # A host may keep only a byte-bounded slice of the reply: the verdict fields lead.
    # `blocking` is uncapped in count, so on a pull verdict it can still outrun that slice.
    lead = {k: out[k] for k in ("verdict", "pull_class", "reason", "blocking")}
    reply = {"plan": prep_gate["plan"], **lead, **out, "prep_gate": prep_gate}
    if verdicts:
        reply["anchor_verdicts"] = verdicts
    return reply
