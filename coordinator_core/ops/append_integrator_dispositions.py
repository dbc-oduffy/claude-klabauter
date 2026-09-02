"""
coordinator_core.ops.append_integrator_dispositions — one-call writer for the
`review-integrator`'s ONE sanctioned sidecar write (`agents/review-integrator.md`
§ Sidecar Immutability, DoE-claude): the bulk `## Integrator Dispositions` block
appended to a reviewer findings sidecar after every finding has been triaged.

Purpose: close the seam between "the integrator applied every finding" and
"the guard sees the loop closed". `block_em_hand_edit_pending_review_integration`
(this package) denies an EM hand-edit to any file a live `coordinator:code-reviewer`
sidecar still cites as unaddressed, and unblocks the instant that sidecar's body
contains the literal heading `## Integrator Dispositions` — nothing else. An
integrator that hand-authors its own run-report but never appends that heading
to the REVIEWER's sidecar leaves every cited file blocked with no signal pointing
at the omission; the next, unrelated writer hits the guard cold. This module
gives the integrator a single call that (a) can only target a real, still-open
reviewer findings sidecar — never its own run-report, never an arbitrary file —
and (b) writes the canonical block shape byte-for-byte, so there is no
hand-authored YAML to get subtly wrong or skip.

This is deliberately a WRITE-time correctness tool, not a read-time heuristic:
it does not change `block_em_hand_edit_pending_review_integration`'s own
coverage logic at all. The guard still only ever checks for the heading's
literal presence in the reviewer sidecar; this module is simply the one
sanctioned way to put it there correctly.

Validation is fail-LOUD, not fail-open — the opposite discipline from the
sibling write_guards package. A guard's job is to never brick a spawn on an
unexpected shape; this tool's job is to never let a misdirected write silently
"succeed" against the wrong file or in a shape the guard's heuristic can't see.
Concretely, `append_dispositions` refuses (raises `DispositionsError`) unless:
  - the target path resolves under `state/subagent-share/` (never any other
    tree — in particular never the caller's OWN run-report sidecar, which
    lives in the same directory but carries a different `agent_type`),
  - the target's frontmatter `agent_type:` is one of `_REVIEWER_AGENT_TYPES` —
    the code-reviewer family plus the six Opus reviewer personas, i.e. every
    agent whose deliverable IS a finding set — OR one of
    `_REVIEWER_DOC_TYPE_TOKENS`, the doc-TYPE tokens `coordinator-doc-new`'s
    self-persist fallback stamps into that same field on the (undispatched-
    sidecar) fallback path. `_REVIEWER_AGENT_TYPES` is deliberately WIDER
    than the sibling guard's own single-literal `_REVIEWER_AGENT_TYPE`, which
    gates a different question (which sidecars block an EM hand-edit); see
    that constant's own comment below for why the two must not be reconciled,
  - the target actually carries filled-in findings, checked under whichever
    of two named shapes it matches (`_detect_findings_shape`): the
    `review-findings` shape (`## Findings` section body — everything
    between that heading and whichever comes first of the `## Exit
    interview` heading, the `## Integrator Dispositions` heading, or end
    of document — NOT the whole document, and NOT truncated at the next
    `## `/`### ` sub-heading, since the reviewer's own layout nests
    `## Summary` and `### Finding N` INSIDE the findings body) is not still
    the unfilled-scaffold placeholder; or the `staff-eng-review` shape (a
    fenced ```json code block with a non-empty top-level `findings` array —
    the shape every Opus reviewer persona actually emits, distinct from the
    heading-scaffold above). A target matching NEITHER shape is refused,
    naming both. Scoped to the section/block on purpose: the placeholder
    sentinel or a quoted mention surviving elsewhere in the document must
    never trip either check. Neither check parses or counts individual
    findings beyond emptiness.
  - at least one finding id was supplied across the five disposition buckets,
  - the target does not already carry the heading (idempotent no-op instead —
    see `append_dispositions`'s return value).

Negative-spec:
  - Does NOT verify that the supplied finding ids are a set-complete partition
    of every finding actually in the sidecar body — the review-findings
    template (DR-091) has no structured per-finding id field to check
    against (same absence `block_em_hand_edit_pending_review_integration`
    documents for its own coverage heuristic). Completeness is the calling
    agent's responsibility, per `agents/review-integrator.md` § Sidecar
    Disposition Annotation ("Every finding in the sidecar must appear in
    exactly one bucket").
  - Does NOT touch `state/review-trail/*.json` trail records — a completely
    separate ledger (`agents/review-integrator.md` § Trail-File Ownership);
    this module only ever writes to the `.md` findings sidecar.
  - Does NOT rewrite, reorder, or otherwise touch anything already in the
    sidecar — strictly an append of one new block at the end (Sidecar
    Immutability baseline).
  - Does NOT re-open a sidecar that already carries the heading and treat it
    as an error — a second call against an already-dispositioned sidecar is a
    no-op (`already_dispositioned=True` in the returned result), matching the
    guard's own "no separate step needed" framing: calling this tool twice
    must never be a way to corrupt a sidecar.

Spec backlink: cross-repo dispatch defect surfaced 2026-07-29 (EM hand-diagnosed
  a review-integrator run that applied all findings correctly but never appended
  the disposition block to the reviewer's sidecar, leaving files blocked with no
  signal pointing at the omission).
Sibling guard: coordinator_core/write_guards/block_em_hand_edit_pending_review_integration.py
Sibling doctrine: DoE-claude coordinator/agents/review-integrator.md
  § Sidecar Disposition Annotation
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.session.declared_writes import declare_write

# Generator-provenance declaration (generator_provenance.py). append_dispositions()
# appends a disposition block to whichever open reviewer findings sidecar the
# caller names (state/subagent-share/<session>/*.md, see the CLI --sidecar
# help text) -- the sidecar set is data-dependent, not a fixed artifact.
MUTATES = ["state/subagent-share/**/*.md"]

#: The disposition buckets, in the canonical emission order. The FIRST FIVE
#: match `agents/review-integrator.md` § Sidecar Disposition Annotation's
#: example block byte-for-byte; the sixth is ours and is described below.
#:
#: `verified-no-action` (sixth entry) is a claude-klabauter-side extension, NOT part of
#: DoE-claude's example block. It is appended LAST, deliberately, so a block
#: using only the original five buckets still emits byte-identically to
#: today — the byte-parity claim in this module's docstring survives for all
#: existing usage. Inserting it anywhere else would break that claim
#: silently. Do not "sort" it into the middle in a future tidying pass; it
#: stays last pending DoE-claude's own adoption of the bucket.
#:
#: `verified-no-action` means: the integrator independently VERIFIED the
#: finding and concluded no artifact change is needed. It is NOT
#: `escalated-disagree` — there is no disagreement with the reviewer; the
#: reviewer was right to raise it. It is NOT `deferred` — nothing is being
#: put off; the question is closed.
BUCKET_ORDER = (
    "applied",
    "escalated-disagree",
    "escalated-ask",
    "escalated-p0",
    "deferred",
    "verified-no-action",
)

#: YAML key per bucket — differs from the CLI flag spelling only in that the
#: flag uses hyphens throughout while `escalated-p0` doubles as both (kept as
#: one constant so the two never drift independently).
_BUCKET_YAML_KEY = {
    "applied": "applied",
    "escalated-disagree": "escalated-disagree",
    "escalated-ask": "escalated-ask",
    "escalated-p0": "escalated-p0",
    "deferred": "deferred",
    "verified-no-action": "verified-no-action",
}

#: The agent types whose DELIVERABLE is a finding set, and which may therefore
#: receive an `## Integrator Dispositions` block: the code-reviewer family plus
#: the eight Opus reviewer personas. Membership mirrors exactly the types
#: DoE-claude's `report_type_map:` routes to the `review-findings` or
#: `staff-eng-review` provisioning templates — that map is the REASON for this
#: membership, never a runtime input. This module must not read a peer repo's
#: policy file to decide a gate; the drift risk is carried by
#: `test_reviewer_agent_types_pinned` instead, so widening this set is a
#: deliberate edit against a failing test rather than a silent slide.
#:
#: Deliberately WIDER than the single-literal `_REVIEWER_AGENT_TYPE` in the
#: sibling guard `write_guards/block_em_hand_edit_pending_review_integration.py`.
#: The two answer different questions and must not be "reconciled": that guard
#: decides which sidecars BLOCK an EM hand-edit — advisory, fail-open, and
#: narrow by an explicit negative-spec in its own module docstring — while this
#: set decides which sidecars can RECEIVE a disposition block. Widening the
#: guard to match would start blocking EM edits on every persona review, which
#: nothing asks for. The divergence is intentional; it is not drift.
# Membership verified 2026-08-29 against DoE-claude's live
# `coordinator/subagent-sandbox-policy.yaml` `report_type_map:` block — the
# eight `staff-eng-review`-mapped personas below match that file's rows
# verbatim. The 2026-08-10 verification recorded six; `overengineering-reviewer`
# (Kaya) and `apm` (Angelique) were added on the peer side after it, and both
# arrived here in one re-check rather than one memo each — the drift this
# comment warns about is cumulative, not single-row.
# Re-check against that file if either side drifts; this repo has
# no automated way to catch a mismatch at authorship time, only the pin
# test below catching FUTURE accidental drift.
_REVIEWER_AGENT_TYPES = frozenset(
    {
        "coordinator:code-reviewer",
        "coordinator:code-reviewer-weekly",
        "coordinator:staff-eng",
        "coordinator:staff-data-sci",
        "coordinator:senior-front-end",
        "coordinator:staff-ux",
        "coordinator:vp-product",
        "coordinator:eng-director",
        "coordinator:overengineering-reviewer",
        "coordinator:apm",
    }
)

#: A SECOND, deliberately separate vocabulary this gate accepts: doc-TYPE
#: tokens (not agent-type names) that `coordinator-doc-new --type <token>`
#: stamps into a sidecar's `agent_type:` frontmatter key on its SELF-PERSIST
#: fallback path — reached only when a code-reviewer dispatch arrives with no
#: sidecar pre-provisioned by the dispatching EM. See
#: `coordinator-doc-new.py::_scaffold_review_findings`'s own docstring
#: ("`agent_type` is stamped as the literal 'review-findings' — this
#: scaffolder's doc TYPE, not a spawned agent's role — provision_report's own
#: agent_type field records the SPAWNED agent's type instead, e.g.
#: 'code-reviewer'; the two producers diverge on this one field's semantic,
#: not its presence"). Confirmed live 2026-08-22: 56 committed
#: `state/subagent-share/**/*.md` sidecars carry `agent_type: review-findings`
#: today, all via this same fallback path.
#:
#: This is NOT the same defect as widening `_REVIEWER_AGENT_TYPES` to paper
#: over drift — the two vocabularies answer different questions on purpose
#: (agent IDENTITY vs. document TYPE) and are produced by two distinct,
#: individually-documented code paths that were never meant to converge on
#: one spelling. A sidecar this gate must accept can therefore carry EITHER,
#: never neither. Only `review-findings` exists in this population today —
#: `coordinator-doc-new.py` has no analogous self-scaffold literal for the
#: `staff-eng-review` shape (persona reviewers always go through
#: `provision_report`, which stamps their real subagent_type).
_REVIEWER_DOC_TYPE_TOKENS = frozenset({"review-findings"})
_DISPOSITIONS_HEADING = "## Integrator Dispositions"
_FINDINGS_HEADING = "## Findings"
_EXIT_INTERVIEW_HEADING = "## Exit interview"

#: Named findings-bearing shapes this module can extract from. Two, not one:
#: `review-findings` (the `## Findings` heading scaffold, DR-091) and
#: `staff-eng-review` -- what every Opus reviewer persona
#: (`_REVIEWER_AGENT_TYPES`'s persona subset) ACTUALLY writes when
#: dispatched, which is NOT the `## Findings`-headed scaffold
#: `provision_report._build_staff_eng_review_doc_text` provisions at spawn
#: time. Personas write their own free-form document over that scaffold, and
#: the one constant across observed real sidecars (H1 wording varies:
#: "# Staff review", "# Staff-eng review", "# Review", "# Re-review", ...)
#: is a fenced ```json code block carrying a top-level `findings` array. See
#: `_find_json_findings_block` for why detection is content-addressed
#: (the JSON payload) rather than heading-anchored (the H1 text).
_SHAPE_REVIEW_FINDINGS = "review-findings"
_SHAPE_STAFF_ENG_REVIEW = "staff-eng-review"

#: Matches a single fence DELIMITER LINE (an opening ```<lang> or a bare
#: closing ```), anchored to line start (mirrors `_find_heading`'s own
#: line-anchoring discipline, for the same reason -- a ``` mentioned
#: mid-line in prose must never be read as a fence). Deliberately NOT a
#: whole-block matcher -- see `_iter_fenced_blocks` for why.
_FENCE_DELIM_RE = re.compile(r"(?m)^```[A-Za-z0-9_-]*[ \t]*\r?$")
_FINDINGS_SENTINEL = (
    "<!-- One entry per finding: `- [severity] <finding> "
    "— disposition: accepted | rejected | deferred — rationale: ...` -->"
)
_AGENT_TYPE_RE = re.compile(r"^agent_type:[ \t]*(.*)$")

#: Same normalization discipline as the sibling guard's own `_normalize` —
#: kept as a separate copy rather than a shared import so this module has no
#: import-time dependency on write_guards (a different package with a
#: different fail-open contract).
def _normalize(value: str) -> str:
    normalized = value.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _find_heading(text: str, heading: str) -> Optional[int]:
    """Index of ``heading`` where it occurs as a REAL ATX heading line, or None.

    Line-anchored on purpose. Every one of these headings is a string that
    reviewers, integrators, and this module's own docstrings quote in running
    prose — usually in backticks, while explaining the very mechanism it
    drives. A bare ``heading in text`` substring test cannot tell the heading
    from a mention of it, and both failure directions are silent:

      - ``_DISPOSITIONS_HEADING``: a sidecar whose findings body merely NAMES
        the block reads as already-dispositioned. `append_dispositions` then
        no-ops with ``already_dispositioned=True`` and the block is never
        written, and the sibling guard
        (``write_guards/block_em_hand_edit_pending_review_integration``, which
        keeps its own line-anchored copy of this check for the same reason)
        reads the loop as closed and stops blocking.
      - ``_FINDINGS_HEADING``: a body quoting the heading before the real one
        carves the section from the wrong offset.

    Observed live 2026-08-10: a code-reviewer's summary quoted
    ``` `## Integrator Dispositions` ``` while explaining the extractor's
    boundary logic, and the CLI silently refused to write that sidecar's block.
    Trailing whitespace is tolerated; leading whitespace is not (an indented
    ``##`` inside a fenced block is not a heading).
    """
    match = re.search(rf"(?m)^{re.escape(heading)}[ 	]*$", text)
    return match.start() if match is not None else None


def _has_heading(text: str, heading: str) -> bool:
    return _find_heading(text, heading) is not None


class DispositionsError(ValueError):
    """Raised for every fail-loud validation failure in this module."""


def _extract_findings_section(text: str) -> Optional[str]:
    """Return the body of the `## Findings` section — everything after the
    heading up to (not including) whichever comes first of the
    `## Exit interview` heading, the `## Integrator Dispositions` heading, or
    end of document. Returns ``None`` if the `## Findings` heading is absent.

    Deliberately does NOT stop at the next `## `/`### ` sub-heading or a
    `---` horizontal rule: the real reviewer layout nests `## Summary` and
    one or more `### Finding N: <title>` sub-sections (and sometimes a
    `---` rule between findings) INSIDE the findings body, between
    `## Findings` and `## Exit interview` — those are part of the body, not
    section boundaries. This function does not parse or count individual
    findings; it only carves out the span later checked by
    `_findings_section_is_empty` for pristine-scaffold vs. filled."""
    idx = _find_heading(text, _FINDINGS_HEADING)
    if idx is None:
        return None
    rest = text[idx + len(_FINDINGS_HEADING):]
    end = len(rest)
    for boundary in (_EXIT_INTERVIEW_HEADING, _DISPOSITIONS_HEADING):
        boundary_idx = _find_heading(rest, boundary)
        if boundary_idx is not None and boundary_idx < end:
            end = boundary_idx
    return rest[:end]


def _findings_section_is_empty(section: str) -> bool:
    """A findings section is substantively empty if, once the unfilled-
    scaffold sentinel comment is stripped out, nothing but whitespace
    remains. Scoped to the section body only — a sentinel string surviving
    elsewhere in the document (a template remnant, a quoted finding) never
    factors in."""
    return section.replace(_FINDINGS_SENTINEL, "").strip() == ""


def _iter_fenced_blocks(text: str):
    """Yield the body of every CONSECUTIVE pair of fence-delimiter lines, in
    document order.

    Deliberately delimiter-based, not block-based. The prior implementation
    matched a whole ```lang\\n...\\n``` block in one DOTALL regex with no
    pairing awareness: `.*?` happily spans across an intervening ``` line,
    so an EARLIER, unterminated fence (a narrative section's formatting
    slip, no matching close) greedily expanded past it looking for the next
    line anywhere later in the document that looked like a closing fence --
    which could be the closing fence of a real, well-formed block. That
    steals the real block's closing delimiter: the real block's own opening
    line is consumed as body text of the bogus match (never seen as an
    opening at all), the bogus match's body -- garbage spanning both blocks
    -- fails `json.loads`, and the genuinely populated findings block
    becomes invisible to detection. See the 2026-08-19 review finding this
    fixes for a worked repro.

    Pairing every fence-delimiter line with its immediate successor (rather
    than trying to track nesting/well-formedness) means a bad pairing just
    fails to parse as JSON and the scan moves on to the next pair, instead
    of consuming a good block whole. Line-anchored like `_find_heading`,
    for the same reason: a ``` mentioned mid-line in prose must never read
    as a fence.
    """
    delimiters = list(_FENCE_DELIM_RE.finditer(text))
    for opening, closing in zip(delimiters, delimiters[1:]):
        body_start = opening.end()
        if body_start < len(text) and text[body_start] == "\n":
            body_start += 1
        body = text[body_start:closing.start()]
        if body.endswith("\n"):
            body = body[:-1]
        if body.endswith("\r"):
            body = body[:-1]
        yield body


def _find_json_findings_block(text: str) -> Optional[List[Any]]:
    """Return the `findings` list off the first fenced code block whose body
    parses as a JSON object carrying a top-level `findings` array, or
    ``None`` if no such block exists.

    Deliberately content-addressed, not heading-anchored: real persona
    sidecars observed on disk use inconsistent H1 wording (see
    `_SHAPE_STAFF_ENG_REVIEW`'s comment), so pinning a literal heading
    string here would only re-create the review-findings extractor's own
    former fragility under a different label. A fenced block is only ever
    treated as a match if `json.loads` succeeds AND the parsed value is a
    dict with a list-typed `findings` key -- prose that quotes the shape
    (a finding's own text describing the JSON mechanism, a fenced snippet
    that isn't itself valid JSON) fails one of those two conditions and is
    correctly not detected, which is this function's share of the same
    prose-quoting negative-spec `_find_heading` documents. See
    `_iter_fenced_blocks` for why candidate bodies come from consecutive
    delimiter pairs rather than a single whole-block regex.
    """
    for body in _iter_fenced_blocks(text):
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("findings"), list):
            return parsed["findings"]
    return None


def _detect_findings_shape(text: str) -> "tuple[Optional[str], bool]":
    """Dispatch to whichever of the two named shapes `text` matches.

    Returns ``(shape, is_empty)``. ``shape`` is ``None`` when neither
    extractor matches -- the caller refuses, naming both shapes.

    The two shapes are NOT mutually exclusive, and assuming they were is how
    this dispatcher would quietly reproduce the very defect it fixes. Measured
    against the live corpus (307 persona sidecars under state/subagent-share/):
    142 carry a JSON findings block only, 76 a `## Findings` heading only, and
    19 carry BOTH -- `provision_report._build_staff_eng_review_doc_text`
    scaffolds the heading at spawn time, and a persona that writes its JSON
    without clearing that scaffold leaves both on disk.

    So `review-findings` is tried first, but a review-findings match whose
    section is still the UNFILLED SCAFFOLD does not end the search: an empty
    scaffold attests nothing, and refusing on it while a populated JSON block
    sits in the same file is the original bug wearing a new label. All 19
    current both-shape files happen to carry a filled section, so this
    fall-through fires zero times today -- it is here so the outcome does not
    depend on that continuing to be true.
    """
    section = _extract_findings_section(text)
    findings_list = _find_json_findings_block(text)
    if section is not None and not _findings_section_is_empty(section):
        return _SHAPE_REVIEW_FINDINGS, False
    if findings_list is not None:
        return _SHAPE_STAFF_ENG_REVIEW, len(findings_list) == 0
    if section is not None:
        return _SHAPE_REVIEW_FINDINGS, True
    return None, False


def _extract_frontmatter_agent_type(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        return ""
    for line in lines[1:]:
        if line.rstrip() == "---":
            break
        m = _AGENT_TYPE_RE.match(line)
        if m:
            return m.group(1).strip()
    return ""


_INTEGRATOR_AGENT_TYPE = "coordinator:review-integrator"

#: The stamp the Kira-verdict stop guard reads to decide a reviewer's findings
#: were routed (`hooks/stop_dispatch.py :: _guard_kira_verdict_routed`, and the
#: DoE-claude script it ports). It must be a TOP-LEVEL frontmatter key at column
#: zero in the integrator's OWN run-report: the guard walks sibling sidecars and
#: matches this list against the reviewer sidecar's stem.
#:
#: WHY THIS LIVES HERE. This module exists because hand-authored YAML gets
#: subtly wrong or skipped, and it removed that risk for the disposition block
#: -- while leaving `integrated_from`, written by the same agent in the same
#: moment, still hand-authored. Observed 2026-09-01: an integrator reported
#: writing the stamp "at column zero", and had not; the field was prose in its
#: run-notes body and the frontmatter carried none. Its work was correct and
#: invisible, and the guard read routed findings as unrouted.
_INTEGRATED_FROM_RE = re.compile(r"^integrated_from\s*:", re.MULTILINE)


class StampOutcome:
    """What `_stamp_integrated_from` did, so the caller can report it truthfully.

    Deliberately NOT an exception on the failure paths. The disposition block is
    the primary write and must not be rolled back because the secondary stamp
    could not be placed -- but every outcome is reported, because a stamp that
    silently did not happen is the exact defect this exists to close
    (state/bug-backlog/2026-09-01-an-integrator-ran-with-no-provisioned-sidecar.yaml).
    """

    def __init__(self, status, path=None, detail=""):
        self.status = status
        self.path = path
        self.detail = detail


def _frontmatter_bounds(text):
    """`(body_start, body_end)` of the frontmatter block, or None if absent.

    `body_end` is the offset of the closing `---` line, so an insert at that
    offset lands inside the block and at column zero.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != "---":
        return None
    start = len(lines[0])
    offset = start
    for line in lines[1:]:
        if line.rstrip() == "---":
            return (start, offset)
        offset += len(line)
    return None


def _already_routed_by(sidecar_path, reviewer_stem):
    """A sibling integrator run-report already naming `reviewer_stem`, or None.

    `_discover_run_report` deliberately excludes run-reports that carry a stamp,
    so on an idempotent re-run it returns nothing -- which is indistinguishable
    from "no run-report exists" unless this is checked first. Warning about a
    missing stamp that is present would train a reader to ignore the warning
    that matters.
    """
    try:
        entries = sorted(sidecar_path.parent.glob("*.md"))
    except OSError:
        return None
    for entry in entries:
        if entry.name.endswith(".blocks.md") or entry == sidecar_path:
            continue
        try:
            text = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _extract_frontmatter_agent_type(text) != _INTEGRATOR_AGENT_TYPE:
            continue
        bounds = _frontmatter_bounds(text)
        if bounds is None:
            continue
        head = text[bounds[0]:bounds[1]]
        match = _INTEGRATED_FROM_RE.search(head)
        if match is None:
            continue
        line_end = head.find("\n", match.start())
        line = head[match.start():line_end if line_end != -1 else len(head)]
        if reviewer_stem in line:
            return entry
    return None


def _kira_stem_for_sidecar(sidecar_path):
    """The reviewer sidecar's BARE FILENAME without `.md`.

    Must stay byte-identical to the stop guard's own `_kira_stem`
    (`hooks/stop_dispatch.py`), which is what the stamp is matched against.
    A stem spelled any other way -- a path, an absolute path, a different
    extension rule -- writes a stamp the guard cannot see, which is
    indistinguishable from not writing one.
    """
    name = sidecar_path.name
    return name[:-3] if name.endswith(".md") else name


def _discover_run_report(sidecar_path, reviewer_stem):
    """Integrator run-reports beside `sidecar_path` still owed a stamp.

    A candidate carries `agent_type: coordinator:review-integrator` and an
    engine-spliced `integrator_receipt`, and does not already name
    `reviewer_stem`. A run-report stamped for a DIFFERENT reviewer is still a
    candidate: one integrator answering two reviewers is legitimate, and
    excluding every stamped report made that append path unreachable.

    Returns the candidate list. Auto-stamping is only safe when exactly one
    matches: several concurrent integrators in one session share this directory,
    and stamping the wrong run-report attests dispositions its agent never made.
    Ambiguity is reported, never guessed.
    """
    directory = sidecar_path.parent
    try:
        entries = sorted(directory.glob("*.md"))
    except OSError:
        return []
    candidates = []
    for entry in entries:
        if entry.name.endswith(".blocks.md") or entry == sidecar_path:
            continue
        try:
            text = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _extract_frontmatter_agent_type(text) != _INTEGRATOR_AGENT_TYPE:
            continue
        bounds = _frontmatter_bounds(text)
        if bounds is None:
            continue
        head = text[bounds[0]:bounds[1]]
        if "integrator_receipt:" not in head:
            continue
        match = _INTEGRATED_FROM_RE.search(head)
        if match is not None:
            line_end = head.find("\n", match.start())
            line = head[match.start():line_end if line_end != -1 else len(head)]
            if reviewer_stem in line:
                continue
        candidates.append(entry)
    return candidates


def _stamp_integrated_from(run_report, reviewer_stem):
    """Write `integrated_from: [<reviewer_stem>]` into `run_report`'s frontmatter.

    Idempotent: a run-report already naming this reviewer is left untouched.
    A run-report naming a DIFFERENT reviewer gets this stem APPENDED rather than
    replaced -- one integrator answering two reviewers is legitimate, and
    overwriting the earlier name would un-route findings that were routed.
    """
    try:
        text = run_report.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return StampOutcome("unwritable", str(run_report), "cannot read: %s" % exc)

    bounds = _frontmatter_bounds(text)
    if bounds is None:
        return StampOutcome(
            "no-frontmatter",
            str(run_report),
            "run-report carries no `---` frontmatter block to stamp",
        )

    head, rest = text[:bounds[1]], text[bounds[1]:]
    existing = _INTEGRATED_FROM_RE.search(head)

    if existing is not None:
        line_end = head.find("\n", existing.start())
        if line_end == -1:
            line_end = len(head)
        line = head[existing.start():line_end]
        if reviewer_stem in line:
            return StampOutcome("already-stamped", str(run_report))
        trimmed = line.rstrip()
        if trimmed.endswith("]"):
            merged = trimmed[:-1].rstrip()
            joiner = "" if merged.endswith("[") else ", "
            merged = merged + joiner + reviewer_stem + "]"
        else:
            merged = trimmed + " [" + reviewer_stem + "]"
        head = head[:existing.start()] + merged + head[line_end:]
        try:
            run_report.write_text(head + rest, encoding="utf-8")
        except OSError as exc:
            return StampOutcome("unwritable", str(run_report), "cannot write: %s" % exc)
        return StampOutcome("appended", str(run_report))

    stamped = head + "integrated_from: [" + reviewer_stem + "]\n"
    try:
        run_report.write_text(stamped + rest, encoding="utf-8")
    except OSError as exc:
        return StampOutcome("unwritable", str(run_report), "cannot write: %s" % exc)
    return StampOutcome("stamped", str(run_report))


def _resolve_git_root(cwd: Optional[str] = None) -> str:
    root = show_toplevel(cwd)
    if root is None:
        raise DispositionsError(f"cannot resolve git repo root from {cwd or '.'}")
    return root


def _build_block(
    buckets: Dict[str, List[str]],
    rationale: Optional[str],
    *,
    no_findings: bool = False,
) -> str:
    """Render the canonical `## Integrator Dispositions` block, matching
    `agents/review-integrator.md`'s own example byte-for-byte in structure
    (schema_version, the five buckets in fixed order, optional Rationale).

    `no_findings` renders a `no_findings: true` marker. An all-empty block is
    otherwise indistinguishable from one a lazy caller produced, and the flag
    cannot be verified against reviewer prose at write time — so the block
    records that the claim was made, making a zero-finding close greppable
    after the fact rather than silent. Renders only when set, so an ordinary
    call keeps byte-parity with a pre-flag block.
    """
    lines: List[str] = []
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(_DISPOSITIONS_HEADING)
    lines.append("")
    lines.append("```yaml")
    lines.append("schema_version: 1")
    for bucket in BUCKET_ORDER:
        ids = buckets.get(bucket) or []
        # The original five buckets always render (even empty, as `[]`) to
        # match DoE-claude's documented example exactly. `verified-no-action`
        # is a claude-klabauter-side extension: it renders ONLY when actually used, so
        # a call using none of it still emits byte-identical output to a
        # pre-extension five-bucket block — the byte-parity claim this
        # module's docstring makes about DoE's example.
        if bucket == "verified-no-action" and not ids:
            continue
        rendered = "[" + ", ".join(ids) + "]"
        lines.append(f"{_BUCKET_YAML_KEY[bucket]}: {rendered}")
    if no_findings:
        lines.append("no_findings: true")
    lines.append("```")
    if rationale and rationale.strip():
        lines.append("")
        lines.append("### Rationale")
        lines.append("")
        lines.append(rationale.strip())
    lines.append("")
    return "\n".join(lines)


def append_dispositions(
    sidecar_path: Path,
    buckets: Dict[str, List[str]],
    *,
    rationale: Optional[str] = None,
    git_root: Optional[Path] = None,
    no_findings: bool = False,
) -> Dict[str, object]:
    """Validate `sidecar_path` and append the disposition block to it.

    Returns ``{"path": str, "already_dispositioned": bool}``. Raises
    `DispositionsError` on any validation failure — see module docstring for
    the full fail-loud checklist. Never partially writes: the block is only
    ever appended after every check above passes.

    `no_findings` records an all-buckets-empty block for a reviewer that filled
    its findings section and declared no findings in it. NEGATIVE SPEC: this is
    not a bypass for an integrator that simply supplied no ids — that stays a
    hard error, because the two are indistinguishable from the id set alone and
    only one of them is honest. It is also not a substitute for the unfilled-
    scaffold check above, which still fires first: a reviewer that never wrote
    its findings body has not declared anything.
    """
    if not sidecar_path.is_file():
        raise DispositionsError(f"sidecar not found: {sidecar_path}")

    normalized = _normalize(str(sidecar_path))
    if not normalized.endswith(".md"):
        raise DispositionsError(
            f"target must be a `.md` findings sidecar, got: {sidecar_path}"
        )

    if git_root is not None:
        try:
            rel = sidecar_path.resolve().relative_to(Path(git_root).resolve())
        except ValueError:
            rel = None
        rel_normalized = _normalize(str(rel)) if rel is not None else normalized
    else:
        rel_normalized = normalized

    if "state/subagent-share/" not in rel_normalized:
        raise DispositionsError(
            "target must live under state/subagent-share/<session_id>/ "
            f"(the reviewer's own provisioned sidecar) — got: {sidecar_path}\n"
            "This is almost always a sign the wrong path was passed — e.g. "
            "your OWN run-report sidecar rather than the reviewer's findings "
            "sidecar you were dispatched to disposition."
        )

    text = sidecar_path.read_text(encoding="utf-8", errors="replace")

    agent_type = _extract_frontmatter_agent_type(text)
    if agent_type not in _REVIEWER_AGENT_TYPES and agent_type not in _REVIEWER_DOC_TYPE_TOKENS:
        accepted = sorted(_REVIEWER_AGENT_TYPES) + sorted(_REVIEWER_DOC_TYPE_TOKENS)
        raise DispositionsError(
            f"target's frontmatter agent_type is {agent_type!r}, which is not one "
            f"of the values this tool writes to — either a reviewer agent type or "
            f"a self-scaffolded reviewer doc-type token "
            f"({', '.join(accepted)}) — it only ever writes to "
            "a sidecar whose deliverable IS a finding set, never a run-report, an "
            "assessment, or your OWN sidecar."
        )

    shape, is_empty = _detect_findings_shape(text)
    if shape is None:
        raise DispositionsError(
            f"target matches neither supported shape — no {_FINDINGS_HEADING!r} "
            "heading (review-findings) and no fenced ```json block carrying a "
            "`findings` list (staff-eng-review) — this is not a dispositionable "
            "reviewer sidecar."
        )

    if _has_heading(text, _DISPOSITIONS_HEADING):
        return {"path": str(sidecar_path), "already_dispositioned": True}

    if is_empty:
        if shape == _SHAPE_REVIEW_FINDINGS:
            raise DispositionsError(
                f"have the reviewer replace the {_FINDINGS_HEADING!r} scaffold "
                "with its actual findings body before dispositioning — the "
                f"section between {_FINDINGS_HEADING!r} and the terminal "
                "boundary is still the unfilled scaffold (only the placeholder "
                "comment or nothing at all), so there is nothing to disposition."
            )
        raise DispositionsError(
            "the fenced ```json block's `findings` array is empty — there is "
            "nothing to disposition."
        )

    total_ids = sum(len(buckets.get(bucket) or []) for bucket in BUCKET_ORDER)
    if no_findings and total_ids:
        raise DispositionsError(
            "--no-findings records an empty disposition set, but finding ids "
            f"were supplied ({total_ids}). Drop the flag and bucket them."
        )
    if total_ids == 0 and not no_findings:
        raise DispositionsError(
            "no finding ids supplied across any disposition bucket — nothing "
            "to record. Every finding in the sidecar must appear in exactly "
            "one bucket (applied / escalated-disagree / escalated-ask / "
            "escalated-p0 / deferred / verified-no-action). If the reviewer "
            "declared no findings at all, pass --no-findings."
        )

    block = _build_block(buckets, rationale, no_findings=no_findings)
    with sidecar_path.open("a", encoding="utf-8") as handle:
        handle.write(block)

    # DR-276: declared AFTER the write lands, never before — the contract is a
    # report of what was ACTUALLY written, not of an intended surface.
    declare_write(sidecar_path)

    return {"path": str(sidecar_path), "already_dispositioned": False}


def _split_ids(value: Union[str, List[str], None]) -> List[str]:
    """Flattens one bucket flag's ids, preserving order and dropping duplicates.

    Accepts a list because every bucket flag is `action="append"`: a caller that
    passes `--applied 1 --applied 2` means BOTH, and an earlier plain-`store`
    spelling silently kept only the last occurrence while still exiting 0 —
    a disposition record that read complete and was not.
    """
    if not value:
        return []
    chunks = [value] if isinstance(value, str) else value
    out: List[str] = []
    for chunk in chunks:
        for tok in chunk.split(","):
            tok = tok.strip()
            if tok and tok not in out:
                out.append(tok)
    return out


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="append-integrator-dispositions",
        description=(
            "Append the canonical `## Integrator Dispositions` block to a "
            "reviewer findings sidecar (see `_REVIEWER_AGENT_TYPES`) — the "
            "ONE sanctioned sidecar write review-integrator dispatches make "
            "(agents/review-integrator.md § Sidecar Disposition Annotation)."
        ),
    )
    parser.add_argument(
        "--sidecar",
        required=True,
        help="Path to the reviewer's review-findings sidecar (state/subagent-share/<session>/*.md).",
    )
    parser.add_argument(
        "--run-report",
        default=None,
        help=(
            "Path to the CALLER'S OWN run-report sidecar, stamped with "
            "`integrated_from: [<reviewer stem>]` so the Kira-verdict stop guard can "
            "see this routing. Omit it and the caller's run-report is auto-discovered "
            "beside --sidecar; discovery refuses to guess when several integrators in "
            "the session are still owed a stamp. Never hand-write this field: an "
            "integrator that believed it had is why this flag exists."
        ),
    )
    parser.add_argument("--applied", action="append", default=None, help="Comma-separated finding ids: applied.")
    parser.add_argument(
        "--escalated-disagree", action="append", default=None, help="Comma-separated finding ids: escalated-disagree."
    )
    parser.add_argument(
        "--escalated-ask", action="append", default=None, help="Comma-separated finding ids: escalated-ask."
    )
    parser.add_argument(
        "--escalated-p0", action="append", default=None, help="Comma-separated finding ids: escalated-p0."
    )
    parser.add_argument("--deferred", action="append", default=None, help="Comma-separated finding ids: deferred.")
    parser.add_argument(
        "--verified-no-action",
        action="append", default=None,
        help="Comma-separated finding ids: verified-no-action.",
    )
    parser.add_argument(
        "--no-findings",
        action="store_true",
        help=(
            "Record an empty disposition set for a reviewer that declared no findings. "
            "Mutually exclusive with any bucket flag."
        ),
    )
    parser.add_argument(
        "--rationale-stdin",
        action="store_true",
        help=(
            "Read the block's prose ### Rationale section from stdin. PREFER THIS over "
            "--rationale-file: it needs no path, so no concurrent session can clobber it."
        ),
    )
    parser.add_argument(
        "--rationale-file",
        default=None,
        help=(
            "Path to a file whose content becomes the block's prose ### Rationale section, "
            "or \"-\" to read stdin (same as --rationale-stdin). Prefer stdin — a "
            "caller-chosen path is shared state, and a concurrent session that reuses it "
            "replaces this rationale with its own, silently and with a correct exit code."
        ),
    )
    parser.add_argument(
        "--root",
        default=None,
        help="Repo root to resolve the state/subagent-share/ scope check against (default: git rev-parse from cwd).",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    buckets = {
        "applied": _split_ids(args.applied),
        "escalated-disagree": _split_ids(args.escalated_disagree),
        "escalated-ask": _split_ids(args.escalated_ask),
        "escalated-p0": _split_ids(args.escalated_p0),
        "deferred": _split_ids(args.deferred),
        "verified-no-action": _split_ids(args.verified_no_action),
    }

    rationale: Optional[str] = None
    if args.rationale_stdin and args.rationale_file and args.rationale_file != "-":
        print(
            "append-integrator-dispositions: --rationale-stdin and --rationale-file are "
            "mutually exclusive; pass one.",
            file=sys.stderr,
        )
        return 2
    if args.rationale_stdin or args.rationale_file == "-":
        # No path, so nothing for a concurrent session to clobber -- the whole reason
        # this channel exists (see --rationale-file's help, and
        # state/bug-backlog/2026-08-31-concurrent-sessions-collide-on-a-shared.yaml).
        #
        # `--rationale-file -` is accepted as the same thing: a caller reaching for the
        # POSIX stdin convention has understood the contract, and refusing it would
        # push them back onto a real temp path -- the exact hazard this channel exists
        # to remove. A caller who genuinely means a file named "-" can write "./-".
        if sys.stdin is None:
            print(
                "append-integrator-dispositions: no stdin to read; a dispatched agent "
                "runs without one. Pass --rationale-file <path>.",
                file=sys.stderr,
            )
            return 2
        rationale = sys.stdin.read()
    elif args.rationale_file:
        try:
            rationale = Path(args.rationale_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"append-integrator-dispositions: cannot read --rationale-file: {exc}", file=sys.stderr)
            return 2

    try:
        git_root = Path(args.root) if args.root else Path(_resolve_git_root())
    except DispositionsError as exc:
        print(f"append-integrator-dispositions: {exc}", file=sys.stderr)
        return 2

    sidecar_path = Path(args.sidecar)
    if not sidecar_path.is_absolute():
        sidecar_path = git_root / sidecar_path

    try:
        result = append_dispositions(
            sidecar_path,
            buckets,
            rationale=rationale,
            git_root=git_root,
            no_findings=args.no_findings,
        )
    except DispositionsError as exc:
        print(f"append-integrator-dispositions: {exc}", file=sys.stderr)
        return 1

    if result["already_dispositioned"]:
        print(
            f"append-integrator-dispositions: OK — {result['path']} already carries "
            f"{_DISPOSITIONS_HEADING!r}, no-op."
        )
    else:
        print(f"append-integrator-dispositions: OK — appended dispositions block to {result['path']}.")

    # Secondary write: the routing stamp the stop guard reads. Never fails the
    # call -- the dispositions block above is the primary deliverable and is
    # already on disk -- but every outcome is printed, because the failure this
    # closes is a stamp that did not happen and said it did.
    reviewer_stem = _kira_stem_for_sidecar(sidecar_path)
    if args.run_report:
        run_report = Path(args.run_report)
        if not run_report.is_absolute():
            run_report = git_root / run_report
        candidates = [run_report]
        chosen_by = "--run-report"
    else:
        candidates = _discover_run_report(sidecar_path, reviewer_stem)
        chosen_by = "auto-discovery"

    if len(candidates) == 1:
        outcome = _stamp_integrated_from(candidates[0], reviewer_stem)
        if outcome.status in ("stamped", "appended"):
            print(
                f"append-integrator-dispositions: OK — {outcome.status} "
                f"`integrated_from: [{reviewer_stem}]` in {outcome.path}."
            )
        elif outcome.status == "already-stamped":
            print(
                f"append-integrator-dispositions: OK — {outcome.path} already names "
                f"{reviewer_stem}, stamp left as-is."
            )
        else:
            print(
                f"append-integrator-dispositions: WARNING — dispositions landed, but the "
                f"routing stamp did NOT: {outcome.detail} ({outcome.path}). The guard will "
                f"read this reviewer as unrouted until `integrated_from: [{reviewer_stem}]` "
                f"is a top-level frontmatter key at column zero.",
                file=sys.stderr,
            )
    elif (prior := _already_routed_by(sidecar_path, reviewer_stem)) is not None:
        print(
            f"append-integrator-dispositions: OK — {prior.name} already names "
            f"{reviewer_stem}; routing stamp already in place."
        )
    elif not candidates:
        print(
            f"append-integrator-dispositions: WARNING — dispositions landed, but no "
            f"integrator run-report was found to stamp ({chosen_by}). If this dispatch "
            f"was provisioned no sidecar, that is "
            f"state/bug-backlog/2026-09-01-an-integrator-ran-with-no-provisioned-sidecar.yaml; "
            f"the routing is real but invisible to the guard.",
            file=sys.stderr,
        )
    else:
        named = ", ".join(sorted(c.name for c in candidates))
        print(
            f"append-integrator-dispositions: WARNING — dispositions landed, but "
            f"{len(candidates)} integrator run-reports are owed a stamp and this call "
            f"will not guess which is yours: {named}. Re-run with --run-report <path>. "
            f"Stamping the wrong one attests dispositions its agent never made.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
