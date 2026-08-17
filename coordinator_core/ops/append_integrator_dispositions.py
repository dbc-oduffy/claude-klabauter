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
    agent whose deliverable IS a finding set. This is deliberately WIDER than
    the sibling guard's own single-literal `_REVIEWER_AGENT_TYPE`, which gates
    a different question (which sidecars block an EM hand-edit); see that
    constant's own comment below for why the two must not be reconciled,
  - the target actually carries filled-in findings — the `## Findings`
    section body (everything between that heading and whichever comes
    first of the `## Exit interview` heading, the `## Integrator
    Dispositions` heading, or end of document — NOT the whole document,
    and NOT truncated at the next `## `/`### ` sub-heading, since the
    reviewer's own layout nests `## Summary` and `### Finding N` INSIDE
    the findings body) is not still the unfilled-scaffold placeholder —
    there is nothing to disposition against an empty scaffold. Scoped to
    the section on purpose: the placeholder sentinel surviving elsewhere
    in the document (a quoted finding, an unrelated template remnant)
    must never trip this check. This check does not parse or count
    individual findings — it only distinguishes the pristine unfilled
    scaffold from a body that carries any real content.
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
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

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
#: the six Opus reviewer personas. Membership mirrors exactly the types
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
# Membership verified 2026-08-10 against DoE-claude's live
# `coordinator/subagent-sandbox-policy.yaml` `report_type_map:` block — the
# six `staff-eng-review`-mapped personas below match that file's rows
# verbatim. Re-check against that file if either side drifts; this repo has
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
    }
)
_DISPOSITIONS_HEADING = "## Integrator Dispositions"
_FINDINGS_HEADING = "## Findings"
_EXIT_INTERVIEW_HEADING = "## Exit interview"
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
    if agent_type not in _REVIEWER_AGENT_TYPES:
        raise DispositionsError(
            f"target's frontmatter agent_type is {agent_type!r}, which is not one "
            f"of the reviewer agent types this tool writes to "
            f"({', '.join(sorted(_REVIEWER_AGENT_TYPES))}) — it only ever writes to "
            "a sidecar whose deliverable IS a finding set, never a run-report, an "
            "assessment, or your OWN sidecar."
        )

    findings_section = _extract_findings_section(text)
    if findings_section is None:
        raise DispositionsError(
            f"target has no {_FINDINGS_HEADING!r} heading — this is not a "
            "review-findings sidecar."
        )

    if _has_heading(text, _DISPOSITIONS_HEADING):
        return {"path": str(sidecar_path), "already_dispositioned": True}

    if _findings_section_is_empty(findings_section):
        raise DispositionsError(
            f"have the reviewer replace the {_FINDINGS_HEADING!r} scaffold "
            "with its actual findings body before dispositioning — the "
            f"section between {_FINDINGS_HEADING!r} and the terminal "
            "boundary is still the unfilled scaffold (only the placeholder "
            "comment or nothing at all), so there is nothing to disposition."
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


def _split_ids(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [tok.strip() for tok in value.split(",") if tok.strip()]


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
    parser.add_argument("--applied", default=None, help="Comma-separated finding ids: applied.")
    parser.add_argument(
        "--escalated-disagree", default=None, help="Comma-separated finding ids: escalated-disagree."
    )
    parser.add_argument(
        "--escalated-ask", default=None, help="Comma-separated finding ids: escalated-ask."
    )
    parser.add_argument(
        "--escalated-p0", default=None, help="Comma-separated finding ids: escalated-p0."
    )
    parser.add_argument("--deferred", default=None, help="Comma-separated finding ids: deferred.")
    parser.add_argument(
        "--verified-no-action",
        default=None,
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
        "--rationale-file",
        default=None,
        help="Optional path to a file whose content becomes the block's prose ### Rationale section.",
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
    if args.rationale_file:
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
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
