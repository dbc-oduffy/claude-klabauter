"""coordinator_core.write_guards.nudge_sentinel_retained_review_sidecar
— advisory guard.

Purpose: cover a measured gap in the sibling hard-deny guard
``block_em_hand_edit_pending_review_integration``. That guard's
``_find_pending_sidecar`` treats presence of the review-findings template's
unfilled-scaffold sentinel comment (``_FINDINGS_SENTINEL``) as proof the
reviewer hasn't returned yet, and skips the sidecar entirely. Measured across
this tree on 2026-08-06: **80 of 414** reviewer findings sidecars carry real
``### Finding`` content AND still retain the template's placeholder sentinel
comment (reviewers append findings without deleting the boilerplate). Those
80 pass through the sibling's hard-deny silently — a real coverage hole.

PM ruling (2026-08-06): this newly-covered population WARNS, it does not
deny. The sibling hard-deny guard's deny CONDITION is not touched at all
(INTERFACE.md fidelity rule 1) — this is a wholly separate advisory module
that fires on exactly the population the sibling deliberately skips.

Spec backlink: cross-repo memo
  cross-repo/inbox/2026-08-06-example-market-data-repo-em-append-integrator-dispositions-refuses-every-reviewer-sidecar.md
  and commit 347a6a98f532 (the `_extract_findings_section` /
  `_findings_section_is_empty` fix in
  ``coordinator_core.ops.append_integrator_dispositions`` this guard reuses
  to distinguish "sentinel retained but findings are real" from "sentinel
  retained because the scaffold is genuinely still pristine").

Cross-package import note: this module imports
``_extract_findings_section`` and ``_findings_section_is_empty`` from
``coordinator_core.ops.append_integrator_dispositions`` — a DELIBERATE
cross-package dependency for single-source-of-truth on the
filled-vs-scaffold question (the same span-carving and sentinel-stripping
logic the review-integrator's own dispositioning path uses), rather than
re-implementing a second, potentially-drifting copy of that logic here. Per
module fail-open discipline (see below), a failure of that import itself
must degrade this guard to a no-op exactly like every other unexpected
error — it is wrapped in the same top-level ``try/except`` as everything
else in ``check()``.

Everything else — subagent scoping (``agent_id`` present -> allow), session
resolution, sidecar-directory resolution, frontmatter ``agent_type``
extraction, path normalization, and the ``_sidecar_covers_target`` coverage
heuristic — is REUSED BY IMPORT from the sibling hard-deny module
(``block_em_hand_edit_pending_review_integration``), not copied, so the
warn population and the sibling's deny population can never silently drift
apart from each other.

Advisory text leads with the alternative (design-tooling-as-offers, per
global CLAUDE.md § Implementation Standards): name the sidecar path and
recommend dispatching ``coordinator:review-integrator`` against it, then
state plainly why this is a warning and not a block — the sidecar retains
the template placeholder comment, so its filled/unfilled state is
ambiguous, and the hard-deny sibling deliberately skips exactly this case.

Negative-spec:
  - Does NOT deny/block anything — CLASS is "advisory"; the envelope carries
    only ``additionalContext``, never ``permissionDecision``.
  - Does NOT change the sibling hard-deny guard's deny CONDITION in any way
    — that guard's ``_FINDINGS_SENTINEL in text -> skip`` branch is
    untouched; this module fires ONLY on the population that branch skips.
  - Does NOT edit, touch, or write to any sidecar — read-only inspection.
  - Does NOT fire when the sentinel is absent — that is the sibling's own
    deny territory (a fully-filled, un-scaffolded sidecar), not this
    guard's population.
  - Does NOT fire when ``## Integrator Dispositions`` is present — the loop
    already closed.
  - Does NOT fire on a pristine, genuinely-unfilled scaffold (sentinel
    present AND nothing else in the findings section) — this is exactly
    what ``_findings_section_is_empty`` from
    ``append_integrator_dispositions`` is reused to distinguish.
  - Does NOT fire for a dispatched subagent's edit (``agent_id`` present) —
    mirrors the sibling's EM-inline-only scope exactly.
  - Does NOT fail closed on any error — every path-resolution, import,
    directory-walk, or file-read failure degrades to ALLOW/no-op, matching
    every sibling write_guards module's fail-open discipline
    (``write_guards/INTERFACE.md`` fidelity rule 6).

Precedent (module shape): ``nudge_tasks_state_folder_split.py`` (advisory
CLASS, docstring/negative-spec layout, offer-not-nag advisory text) and
``block_em_hand_edit_pending_review_integration.py`` (the sidecar-scanning
shape and helpers this module imports rather than reimplements).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.write_guards.block_em_hand_edit_pending_review_integration import (
    _DISPOSITIONS_HEADING,
    _FINDINGS_SENTINEL,
    _REVIEWER_AGENT_TYPE,
    _UNSAFE_SEGMENT_RE,
    _OVERRIDE_ENV_VAR,
    _extract_file_path,
    _extract_frontmatter_agent_type,
    _normalize,
    _sidecar_covers_target,
)

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
#: Advisories run in their OWN phase (engine.py: first non-None advisory wins),
#: so proximity to the sibling hard-deny's 75 buys nothing on its own — what 76
#: actually buys is first place among advisories, ahead of
#: validate_frontmatter_schema_advisory (100) and every nudge_* above it. That
#: precedence is deliberate: when this fires, an unaddressed reviewer finding
#: cites the very file being edited, which outranks any formatting or
#: routing nudge competing for the same single-envelope slot.
PRIORITY = 76

#: The review-findings template's own heading (parity with the sibling).
_FINDINGS_HEADING = "## Findings"


def _find_sentinel_retained_sidecar(
    sidecar_dir: Path, normalized_target: str, basename: str
) -> Optional[Path]:
    """Return the first sidecar in `sidecar_dir` that is a code-reviewer
    findings sidecar retaining the unfilled-scaffold sentinel AND carrying
    real findings content anyway, covering the target file. Or None. Every
    per-file failure degrades to "skip this candidate", never a raise."""
    from coordinator_core.ops.append_integrator_dispositions import (
        _extract_findings_section,
        _findings_section_is_empty,
    )

    try:
        candidates: List[Path] = sorted(sidecar_dir.glob("*.md"))
    except OSError:
        return None

    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        if _extract_frontmatter_agent_type(text) != _REVIEWER_AGENT_TYPE:
            continue
        if _FINDINGS_HEADING not in text:
            continue
        if _DISPOSITIONS_HEADING in text:
            continue  # already integrated
        if _FINDINGS_SENTINEL not in text:
            continue  # not the population this guard covers — sibling's deny territory

        findings_section = _extract_findings_section(text)
        if findings_section is None:
            continue
        if _findings_section_is_empty(findings_section):
            continue  # genuinely pristine scaffold — nothing to warn about

        if _sidecar_covers_target(text, normalized_target, basename):
            return candidate

    return None


def _advisory_text(file_path: str, sidecar_path: str) -> str:
    return (
        "Consider instead:\n"
        f"  dispatch coordinator:review-integrator against {sidecar_path} for "
        f"{file_path} - this sidecar appears to carry real code-reviewer "
        "findings covering this file.\n\n"
        "This is a warning, not a block: the sidecar still retains the "
        "review-findings template's placeholder scaffold comment, so its "
        "filled/unfilled state is ambiguous, and the hard-deny sibling "
        "guard deliberately skips a sidecar in that state."
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        if os.environ.get(_OVERRIDE_ENV_VAR, "0") == "1":
            return None

        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            return None

        # Scope: EM-inline hand-edits only, mirroring the sibling guard.
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
        sidecar_dir = base_dir / "state" / "subagent-share" / session_id
        if not sidecar_dir.is_dir():
            return None

        normalized_target = _normalize(file_path)
        basename = normalized_target.rsplit("/", 1)[-1]

        found = _find_sentinel_retained_sidecar(sidecar_dir, normalized_target, basename)
        if found is None:
            return None

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": _advisory_text(file_path, str(found)),
            }
        }
    except Exception:
        # Fail-OPEN on any unexpected error (INTERFACE.md fidelity rule 6),
        # including a failure of the cross-package
        # append_integrator_dispositions import (see module docstring).
        return None
