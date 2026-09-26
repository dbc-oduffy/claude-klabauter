"""coordinator_core.write_guards.nudge_sentinel_retained_review_sidecar
— advisory guard.

Purpose: cover a measured gap in the review-findings-ledger contract
(DoE-claude docs/plans/2026-09-26-retire-review-integrator.md, row M4):
some reviewer sidecars carry real ``### Finding`` content AND still retain
the review-findings template's unfilled-scaffold sentinel comment
(``_FINDINGS_SENTINEL``) — reviewers append findings without deleting the
boilerplate. Measured across this tree on 2026-08-06: **80 of 414** reviewer
findings sidecars fit that shape. A sidecar in that state has NOT yet been
`review-findings-ledger verify`-stamped, and its own ambiguous scaffold
state (real content mixed with the pristine-looking placeholder) is exactly
the population worth a nudge before an EM hand-edits a file the sidecar
still cites unaddressed.

PM ruling (2026-08-06): this population WARNS, it does not deny — this
module has always been advisory-only, never a hard-deny counterpart.

RRI-M4 repoint: this module previously imported its scope/coverage helpers
from the now-retired ``block_em_hand_edit_pending_review_integration``
guard (deleted, row M1) and its "already closed" check from
``ops.append_integrator_dispositions`` (deleted, row M2). The
review-integrator agent no longer exists, so the retired ``## Integrator
Dispositions`` marker heading is never written by a NEW sidecar; this
module is now fully self-contained (no cross-module import of deleted
code) and reads "already closed" off a verified ``findings_ledger:``
frontmatter stamp (``review_findings_ledger.verify``'s own contract)
instead. The advisory text no longer recommends dispatching
``coordinator:review-integrator`` — a reviewer applies its own findings
and runs ``review-findings-ledger verify`` itself.

Spec backlink: cross-repo memo
  cross-repo/inbox/2026-08-06-example-market-data-repo-em-append-integrator-dispositions-refuses-every-reviewer-sidecar.md
  and commit 347a6a98f532 (the original ``_extract_findings_section`` /
  ``_findings_section_is_empty`` fix, ported verbatim below rather than
  re-derived — see the docstrings on those two functions).

Everything below is self-contained: no import from any other write_guards
module or from ``coordinator_core.ops`` — this module owns its own copy of
every helper it needs (write_guards convention: "write_guards owns its own
helpers rather than importing ops"), except for the frontmatter-bounds
reader it shares with ``review_findings_ledger`` (the module that OWNS the
`findings_ledger:` stamp's shape) rather than re-deriving a second parser.

Negative-spec:
  - Does NOT deny/block anything — CLASS is "advisory"; the envelope carries
    only ``additionalContext``, never ``permissionDecision``.
  - Does NOT edit, touch, or write to any sidecar — read-only inspection.
  - Does NOT fire when the sentinel is absent — a fully-filled,
    un-scaffolded sidecar reads as unambiguous and is out of this module's
    population.
  - Does NOT fire when a verified ``findings_ledger:`` frontmatter stamp is
    present — the loop already closed (``review_findings_ledger.verify``
    ran and passed).
  - Does NOT fire on a pristine, genuinely-unfilled scaffold (sentinel
    present AND nothing else in the findings section) — this is exactly
    what ``_findings_section_is_empty`` distinguishes.
  - Does NOT fire for a dispatched subagent's edit (``agent_id`` present) —
    this guard targets the EM's OWN hand-edit only.
  - Does NOT fail closed on any error — every path-resolution, import,
    directory-walk, or file-read failure degrades to ALLOW/no-op, matching
    every sibling write_guards module's fail-open discipline
    (``write_guards/INTERFACE.md`` fidelity rule 6).

Precedent (module shape): ``nudge_tasks_state_folder_split.py`` (advisory
CLASS, docstring/negative-spec layout, offer-not-nag advisory text).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards._helpers import _resolve_roster_accessor
from coordinator_core.ops.review_findings_ledger import (
    _extract_frontmatter_key,
    _frontmatter_bounds,
)
from coordinator_core.session import machinery_paths

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
#: Advisories run in their OWN phase (engine.py: first non-None advisory wins),
#: so proximity to any hard-deny guard's own priority buys nothing on its own —
#: what this value actually buys is first place among advisories, ahead of
#: validate_frontmatter_schema_advisory (100) and every nudge_* above it. That
#: precedence is deliberate: when this fires, an unaddressed reviewer finding
#: cites the very file being edited, which outranks any formatting or
#: routing nudge competing for the same single-envelope slot.
PRIORITY = 76

#: Rare-use escape hatch.
_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_REVIEW_INTEGRATION_PENDING"

#: The one code-reviewer-identity agent_type this guard gates on
#: unconditionally. A sidecar with a DIFFERENT, non-empty agent_type is
#: ALSO in scope if that type is off the dispatch-seam roster (see
#: `_in_scope_agent_type` below) — an invented type nobody enumerated gets
#: no exemption. An enumerated persona or a sidecar with an empty/missing
#: agent_type stays out of scope either way.
_REVIEWER_AGENT_TYPE = "coordinator:code-reviewer"

#: The review-findings template's own heading
#: (provision_report.py's `_build_review_findings_doc_text`).
_FINDINGS_HEADING = "## Findings"

#: The retired review-integrator's disposition heading. Never written by a
#: NEW sidecar post-retirement — kept ONLY as a historical-sidecar
#: fallback, mirrored from `_findings_reap.is_integrated`'s own reasoning
#: for the same marker.
_DISPOSITIONS_HEADING = "## Integrator Dispositions"

_EXIT_INTERVIEW_HEADING = "## Exit interview"

#: The review-findings template's unfilled-scaffold placeholder sentinel
#: (provision_report.py's `_build_review_findings_doc_text`, byte-for-byte).
_FINDINGS_SENTINEL = (
    "<!-- One entry per finding: `- [severity] <finding> "
    "— disposition: accepted | rejected | deferred — rationale: ...` -->"
)

#: Minimum basename length for the coverage heuristic's basename leg — cuts
#: obviously-generic false positives (`__init__.py`, `test.py`) without
#: requiring a structured citation field.
_MIN_BASENAME_MATCH_LEN = 8

_AGENT_TYPE_RE = re.compile(r"^agent_type:[ \t]*(.*)$")

#: Session-id path-segment safety gate — reject anything that could smuggle
#: a directory separator or traversal rather than resolving it.
_UNSAFE_SEGMENT_RE = re.compile(r"[/\\]|\.\.")


def _extract_file_path(payload: Dict[str, Any]) -> str:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    return tool_input.get("file_path") or tool_input.get("notebook_path") or ""


def _heading_present(text: str, heading: str) -> bool:
    """True only where ``heading`` occurs as a REAL ATX heading line.

    Line-anchored, not a substring test: these markers are strings a
    reviewer quotes in running prose while explaining the mechanism they
    drive, and ``heading in text`` cannot tell the heading from a mention
    of it. See ``ops/review_findings_ledger``'s own line-anchoring
    discipline for the live 2026-08-10 case that motivated this everywhere.
    """
    return re.search(rf"(?m)^{re.escape(heading)}[ \t]*$", text) is not None


def _find_findings_heading(text: str) -> Optional[tuple]:
    """`(start, end_of_heading_line)` of the reviewer's `## Findings`
    heading, tolerating a qualifier after the word (`## Findings (3
    blocking)`) — same tolerance as `review_findings_ledger`'s own
    `_find_findings_heading`."""
    match = re.search(rf"(?m)^{re.escape(_FINDINGS_HEADING)}\b[^\n]*$", text)
    return (match.start(), match.end()) if match is not None else None


def _extract_findings_section(text: str) -> Optional[str]:
    """Body of the `## Findings` section, ending at whichever of
    `## Exit interview`, `## Integrator Dispositions` (historical), or
    end-of-document comes first. Returns None if the heading is absent.
    Ported verbatim (in spirit) from the retired
    `ops.append_integrator_dispositions._extract_findings_section`."""
    span = _find_findings_heading(text)
    if span is None:
        return None
    rest = text[span[1]:]
    end = len(rest)
    for boundary in (_EXIT_INTERVIEW_HEADING, _DISPOSITIONS_HEADING):
        idx = re.search(rf"(?m)^{re.escape(boundary)}[ \t]*$", rest)
        if idx is not None and idx.start() < end:
            end = idx.start()
    return rest[:end]


def _findings_section_is_empty(section: str) -> bool:
    """A findings section is substantively empty if, once the unfilled-
    scaffold sentinel comment is stripped out, nothing but whitespace
    remains."""
    return section.replace(_FINDINGS_SENTINEL, "").strip() == ""


def _normalize(value: str) -> str:
    normalized = value.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


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


def _has_verified_findings_ledger(text: str) -> bool:
    """True when `text`'s frontmatter carries a non-empty `findings_ledger:`
    stamp (`review_findings_ledger.verify`'s own contract) — the
    post-retirement "loop already closed" signal."""
    bounds = _frontmatter_bounds(text)
    if bounds is None:
        return False
    head = text[: bounds[1]]
    value = _extract_frontmatter_key(head, "findings_ledger")
    return bool(value)


def _sidecar_covers_target(text: str, normalized_target: str, basename: str) -> bool:
    normalized_body = _normalize(text)
    if normalized_target and normalized_target in normalized_body:
        return True
    if basename and len(basename) >= _MIN_BASENAME_MATCH_LEN and basename in normalized_body:
        return True
    return False


class _LazyRoster:
    """Resolves the dispatch-seam roster at most ONCE per `check()` call,
    caching the result (including a load failure, cached as `None`)."""

    __slots__ = ("_resolved", "_value")

    def __init__(self) -> None:
        self._resolved = False
        self._value: Optional[Any] = None

    def get(self) -> Optional[Any]:
        if not self._resolved:
            roster, _error = _resolve_roster_accessor()()
            self._value = roster
            self._resolved = True
        return self._value


def _in_scope_agent_type(agent_type: str, lazy_roster: "_LazyRoster") -> bool:
    """In scope iff `agent_type` equals `_REVIEWER_AGENT_TYPE`, OR
    `agent_type` is non-empty and off the dispatch-seam roster. An
    empty/missing `agent_type` is always out of scope. Callers MUST
    evaluate this LAST among a candidate's filters."""
    if agent_type == _REVIEWER_AGENT_TYPE:
        return True
    if not agent_type:
        return False
    roster = lazy_roster.get()
    return roster is None or agent_type not in roster


def _find_sentinel_retained_sidecar(
    sidecar_dir: Path, normalized_target: str, basename: str, lazy_roster: "_LazyRoster"
) -> Optional[Path]:
    """Return the first sidecar in `sidecar_dir` that is a
    code-reviewer-or-off-roster findings sidecar retaining the
    unfilled-scaffold sentinel AND carrying real findings content anyway,
    covering the target file, with NO verified findings_ledger stamp yet.
    Or None. Every per-file failure degrades to "skip this candidate",
    never a raise."""
    try:
        candidates: List[Path] = sorted(sidecar_dir.glob("*.md"))
    except OSError:
        return None

    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        if not _heading_present(text, _FINDINGS_HEADING):
            continue
        if _heading_present(text, _DISPOSITIONS_HEADING):
            continue  # historical marker present — already integrated
        if _has_verified_findings_ledger(text):
            continue  # verify already ran and passed — loop already closed
        if _FINDINGS_SENTINEL not in text:
            continue  # not the population this guard covers

        findings_section = _extract_findings_section(text)
        if findings_section is None:
            continue
        if _findings_section_is_empty(findings_section):
            continue  # genuinely pristine scaffold — nothing to warn about

        if not _sidecar_covers_target(text, normalized_target, basename):
            continue
        if not _in_scope_agent_type(_extract_frontmatter_agent_type(text), lazy_roster):
            continue

        return candidate

    return None


def _advisory_text(file_path: str, sidecar_path: str) -> str:
    return (
        f"WARNING (not a block): sidecar for {file_path} may carry real "
        "code-reviewer findings but retains the template's placeholder "
        "scaffold comment (ambiguous filled state), with no verified "
        "findings ledger yet.\n"
        "Use instead:\n"
        f"  apply every finding in {sidecar_path} in place, then run "
        "`review-findings-ledger verify --sidecar "
        f"{sidecar_path}`"
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        if os.environ.get(_OVERRIDE_ENV_VAR, "0") == "1":
            return None

        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            return None

        # Scope: EM-inline hand-edits only.
        if payload.get("agent_id"):
            return None

        file_path = _extract_file_path(payload)
        if not file_path:
            return None

        session_id = payload.get("session_id") or ""
        if not session_id or _UNSAFE_SEGMENT_RE.search(session_id):
            return None

        cwd = payload.get("cwd") or None
        base_dir = Path(cwd) if cwd else Path.cwd()
        # Both share roots — a session provisioned before a relocation
        # republished still writes under the legacy root; one root alone is
        # a guard that cannot fire.
        candidate_dirs = [
            Path(d)
            for d in machinery_paths.share_dirs(str(base_dir), session_id)
        ]

        normalized_target = _normalize(file_path)
        basename = normalized_target.rsplit("/", 1)[-1]

        lazy_roster = _LazyRoster()
        found = None
        for sidecar_dir in candidate_dirs:
            if not sidecar_dir.is_dir():
                continue
            found = _find_sentinel_retained_sidecar(
                sidecar_dir, normalized_target, basename, lazy_roster
            )
            if found is not None:
                break
        if found is None:
            return None

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": _advisory_text(file_path, str(found)),
            }
        }
    except Exception:
        # Fail-OPEN on any unexpected error (INTERFACE.md fidelity rule 6).
        return None
