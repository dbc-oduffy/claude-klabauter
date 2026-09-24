"""coordinator_core.write_guards.block_em_hand_edit_pending_review_integration
— advisory guard.

Mechanizes the "reviewer findings — apply, don't ratify" rule (DoE-claude
``global-doctrine/CLAUDE.md`` § Flag Severity: "Tradeoff-free correctness
fixes ... fold in silently via the review-integrator") and its EM-channel
restatement (``coordinator/snippets/em-operating-doctrine.md`` §
Engineering Remit: "'All of them get implemented' means dispatching the
review-integrator, never hand-authoring the changes"). Both surfaces were
audited as PROSE-ONLY, unenforced — this is the structural check the audit
named but did not build: "this rule is prose-only, unenforced ... exactly
the kind of thing that should be a guard ... rather than a paragraph asking
the EM to remember" (``inventory-coordinator.md``, row for "After every
review, dispatch review-integrator").

Purpose: advise against an EM's OWN hand-edit (``agent_id`` absent — see
Scope below) to a file that a live ``coordinator:code-reviewer`` findings
sidecar, in
THIS session's ``state/subagent-share/<session-id>/`` directory, still
cites as an unaddressed finding. "Unaddressed" is operationalized as: the
sidecar carries real findings content (the review-findings template's
placeholder sentinel has been replaced) AND has not yet received the
review-integrator's ``## Integrator Dispositions`` block — the ONE
sanctioned sidecar write the integrator baseline prompt permits
(``agents/review-integrator.md`` § Sidecar Immutability / "The ONE
sanctioned sidecar write"). Presence of that heading IS "a corresponding
review-integrator run-report has landed since" for this guard's purposes:
the integrator's disposition write is the single per-sidecar signal that a
review pass actually closed the loop, so checking for it is equivalent to
checking for the integrator run without adding a second cross-referenced
state surface.

Not a blind absolute: an integrator genuinely unavailable has a named
exit, cited by the advisory itself — DoE-claude
``coordinator/docs/wiki/review-integration-doctrine.md`` §
"Integrator unavailable — the named exit" (re-dispatch once; if not
break-class, park with an owner; if break-class, hand-apply after the
integrator's own fresh-disk re-check, deviation recorded in the commit).

Scope — why EM-inline only, mirroring
``block_unauthorized_claude_md_write``'s allow-condition (1), inverted:
this guard targets the EM's OWN hand-edit specifically ("Reviewer findings
— apply, don't ratify" and "never hand-authoring the changes" are both
EM-directed rules about EM behavior) — a DISPATCHED subagent's edit to the
same file is a different failure class (an executor silently re-touching a
file mid-review) already covered by other guards
(``block_subagent_plan_body_write``, the sandbox confinement policy) and is
out of scope here. ``payload.get("agent_id")`` present -> allow
unconditionally.

Session-scoping rationale: ``state/subagent-share/<session-id>/`` is keyed
to the DISPATCHING EM's session id, not the spawned subagent's own —
mirrored from ``block_unauthorized_claude_md_write``'s ``claude_md_grant``
session-resolution docstring ("a dispatched subagent's guard evaluation
resolves to the SAME session id as the EM that acquired the grant, with no
payload-session-id wiring required"). A ``coordinator:code-reviewer``
dispatched BY this EM session writes its findings sidecar into exactly this
EM session's ``state/subagent-share/<session-id>/`` directory — scoping the
scan to ``payload["session_id"]`` (present on every PreToolUse payload,
subagent-dispatched or not) keeps the filesystem walk to one flat directory
per check, never a repo-wide ``state/subagent-share/**`` sweep, which is
what keeps this guard compatible with the engine's zero-spawn/sub-10ms
per-check budget (``coordinator_core`` control-plane SLA).

Coverage heuristic (deliberately approximate, not a structured per-finding
citation field — none exists in the ``review-findings`` template as of
DR-091): a sidecar is judged to COVER the target ``file_path`` if the
sidecar body contains either (a) the normalized target path as a
substring, or (b) the target's basename as a substring, subject to a
minimum-length floor on (b) to cut obviously-generic-name false positives
(``__init__.py``, ``test.py``). This is a heuristic over free-text finding
citations, not a parsed structured field — see Negative-spec.

Negative-spec:
  - Does NOT gate on subagent-originated edits (``agent_id`` present) — see
    Scope above. A reviewer or a dispatched executor editing the same file
    mid-review is a different, already-covered failure class.
  - Does NOT scan any ``state/subagent-share/`` directory other than the
    CURRENT session's own — no repo-wide sweep, for the SLA reason stated
    above. A sidecar written under a DIFFERENT session id (e.g. a nested
    dispatch chain that resolved to a distinct session id) is invisible to
    this guard; that is an accepted scope gap, not a claimed-but-unmet
    coverage promise (see AC9-shaped precedent,
    ``block_unauthorized_claude_md_write``'s own header).
  - Gates on ``coordinator:code-reviewer`` findings sidecars (frontmatter
    ``agent_type`` exact match) OR a sidecar whose ``agent_type`` is
    non-empty and off the dispatch-seam roster (``_in_scope_agent_type``,
    same rule as ``bash_guards._helpers.is_confined_by_roster_absence``,
    evaluated locally against a roster resolved at most once per ``check()``
    call). Does NOT gate on an enumerated persona type
    (``coordinator:staff-eng`` and the other roster-listed personas) or on a
    sidecar with an empty/missing ``agent_type`` — both stay out of scope. A
    ``coordinator:review-integrator`` run-report, in particular, never trips
    this guard (it is not a findings sidecar at all).
  - Does NOT treat an UNFILLED review-findings scaffold (the template's
    placeholder sentinel still present, or no ``## Findings`` heading at
    all) as "findings exist" — a reviewer dispatch that hasn't returned yet
    flags nothing.
  - Does NOT parse a structured per-finding target-file field — there is no
    such field in the ``review-findings`` template as of DR-091; coverage
    is a substring heuristic over the sidecar's free-text findings body
    (see Coverage heuristic above), which can both under- and over-match.
    A false negative (miss) silently degrades to allow, matching this
    guard's fail-open discipline generally; a false positive (spurious
    advisory) is recoverable via the override env var below.
  - Does NOT fail closed on any error — every path-resolution, directory
    walk, or file-read failure degrades to ALLOW, matching every sibling
    write_guards module's fail-open-on-error discipline
    (``write_guards/INTERFACE.md`` fidelity rule 6).

Spec backlink: DoE-claude DoE-claude:pln-claude-md-altitude-triage-earn-31f32e
  § C14 (chunk id `REVIEW-INTEGRATOR-REQUIRED-GUARD`)
Audited-but-unbuilt precedent this closes: DoE-claude
  ``state/audits/2026-07-27-doctrine-envelope-classification.md``
  (``inventory-coordinator.md`` row, "After every review, dispatch
  review-integrator").
Precedent (module shape — session-scoped guard reading sidecar
  frontmatter/body off disk): ``block_unauthorized_claude_md_write.py``
  (session-grant resolution) and ``block_consumed_handoff_edit.py``
  (frontmatter-field extraction, fail-open discipline).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards._helpers import (
    _resolve_roster_accessor,
    operator_override_note,
)
from coordinator_core.session import machinery_paths

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 115

#: Rare-use escape hatch — read the module docstring before invoking.
_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_REVIEW_INTEGRATION_PENDING"

#: The one code-reviewer-identity agent_type this guard gates on
#: unconditionally. A sidecar with a DIFFERENT, non-empty agent_type is
#: ALSO in scope if that type is off the dispatch-seam roster (see
#: `_in_scope_agent_type` below) — an invented type nobody enumerated gets
#: no exemption. An enumerated persona (`coordinator:staff-eng` and the
#: other roster-listed personas) or a sidecar with an empty/missing
#: agent_type stays out of scope either way — see module Negative-spec.
#:
#: NARROWER, deliberately, than `ops.append_integrator_dispositions`'
#: `_REVIEWER_AGENT_TYPES` frozenset, which covers the reviewer personas too.
#: The two constants answer different questions and must not be reconciled into
#: one: that set decides which sidecars can RECEIVE a disposition block, while
#: this literal (plus the roster-absence leg) decides which sidecars BLOCK an
#: EM hand-edit. Widening this literal itself to match would make every
#: persona review start blocking EM edits — a behaviour change nothing has
#: asked for, and the reason Negative-spec bullet 3 above is stated as a
#: deliberate scope choice rather than a gap. If you are here because the two
#: "look out of sync": they are, on purpose.
_REVIEWER_AGENT_TYPE = "coordinator:code-reviewer"

#: The review-integrator's ONE sanctioned sidecar write
#: (agents/review-integrator.md § "The ONE sanctioned sidecar write").
#: Presence anywhere in the sidecar body means the loop already closed.
_DISPOSITIONS_HEADING = "## Integrator Dispositions"

#: The review-findings template's own heading
#: (provision_report.py's `_build_review_findings_doc_text`).
_FINDINGS_HEADING = "## Findings"

#: The review-findings template's unfilled-scaffold placeholder sentinel
#: (provision_report.py's `_build_review_findings_doc_text`, byte-for-byte).
_FINDINGS_SENTINEL = (
    "<!-- One entry per finding: `- [severity] <finding> "
    "— disposition: accepted | rejected | deferred — rationale: ...` -->"
)

#: Minimum basename length for the coverage heuristic's basename leg — cuts
#: obviously-generic false positives (`__init__.py`, `test.py`) without
#: requiring a structured citation field (see module docstring).
_MIN_BASENAME_MATCH_LEN = 8

#: Frontmatter `agent_type:` line extractor (first frontmatter block only).
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

    Line-anchored for the same reason as the sibling writer's own
    ``_find_heading`` (``ops/append_integrator_dispositions``) — kept as a
    separate copy rather than a shared import, matching this module's existing
    ``_normalize`` discipline (write_guards must not import ops; different
    package, different fail-open contract).

    The failure a bare substring test produces here is the dangerous
    direction, not the annoying one: a reviewer sidecar that merely QUOTES
    ``## Integrator Dispositions`` in its findings prose — which is exactly
    what a reviewer explaining this mechanism writes — would read as already
    integrated and this guard would stop firing, silently, while the findings
    sat undispositioned. A guard that disarms itself when someone describes it
    is worse than no guard, because the absence of an advisory reads as "all
    clear". Observed live 2026-08-10.
    """
    return re.search(rf"(?m)^{re.escape(heading)}[ 	]*$", text) is not None


def _normalize(value: str) -> str:
    """Backslash -> forward slash, collapse slash runs (parity with every
    sibling write_guards module's normalization discipline)."""
    normalized = value.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _extract_frontmatter_agent_type(text: str) -> str:
    """First frontmatter block only (mirrors
    `block_consumed_handoff_edit._extract_fm_field`'s discipline)."""
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


def _sidecar_covers_target(text: str, normalized_target: str, basename: str) -> bool:
    normalized_body = _normalize(text)
    if normalized_target and normalized_target in normalized_body:
        return True
    if basename and len(basename) >= _MIN_BASENAME_MATCH_LEN and basename in normalized_body:
        return True
    return False


class _LazyRoster:
    """Resolves `bash_guards._helpers`'s dispatch-seam roster at most ONCE,
    on the first `.get()` call, and caches the result (including a
    roster-load failure, cached as `None`) for the rest of this `check()`
    call. Shared by both guards' `_find_*` loops so that a `check()` call
    scanning multiple candidates or multiple sidecar directories still costs
    at most one `resolve_roster()` disk walk (Design decision, AC6)."""

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
    """Shared in-scope predicate for both review-sidecar guards (Design
    decision section of docs/plans/2026-09-23-sidecar-guard-keying.md).

    In scope iff `agent_type` equals `_REVIEWER_AGENT_TYPE` (unchanged leg),
    OR `agent_type` is non-empty and fails the SAME fail-closed rule
    `bash_guards._helpers.is_confined_by_roster_absence` applies (`roster is
    None or agent_type not in roster`), evaluated locally against a roster
    resolved lazily, at most once per `check()` call, via `lazy_roster`
    rather than calling the helper itself (which would resolve its own
    roster once per candidate). An empty/missing `agent_type` is always out
    of scope, matching the helper's own empty-type convention.

    Callers MUST evaluate this LAST among a candidate's filters — after
    every cheaper, roster-free check — so that the roster is only ever
    touched once a candidate has already passed everything else."""
    if agent_type == _REVIEWER_AGENT_TYPE:
        return True
    if not agent_type:
        return False
    roster = lazy_roster.get()
    return roster is None or agent_type not in roster


def _find_pending_sidecar(
    sidecar_dir: Path, normalized_target: str, basename: str, lazy_roster: "_LazyRoster"
) -> Optional[Path]:
    """Return the first sidecar in `sidecar_dir` that is a
    code-reviewer-or-off-roster findings sidecar with unaddressed findings
    covering the target file, or None. Every per-file failure degrades to
    "skip this candidate", never a raise (module-level fail-open
    discipline). The agent_type leg runs LAST, after every cheaper filter,
    so the roster is touched only for a candidate that already passed
    everything else (Design decision, hot-path cost)."""
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
        if _FINDINGS_SENTINEL in text:
            continue  # unfilled scaffold — reviewer hasn't returned yet
        if _heading_present(text, _DISPOSITIONS_HEADING):
            continue  # already integrated
        if not _sidecar_covers_target(text, normalized_target, basename):
            continue
        if not _in_scope_agent_type(_extract_frontmatter_agent_type(text), lazy_roster):
            continue

        return candidate

    return None


def _deny_reason(
    file_path: str, sidecar_path: str, payload: Optional[Dict[str, Any]] = None
) -> str:
    _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload)
    return (
        f"BLOCKED {file_path}: unaddressed code-reviewer finding.\n"
        "Use instead:\n"
        f"  dispatch coordinator:review-integrator against {sidecar_path}; "
        "if unavailable, re-dispatch once, then park with an owner, or "
        "hand-apply after a fresh-disk re-check if break-class"
        + ("\n\n" + _note if _note else "")
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        if os.environ.get(_OVERRIDE_ENV_VAR, "0") == "1":
            return None

        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            return None

        # Scope: EM-inline hand-edits only (see module docstring "Scope").
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
        # Both share roots (machinery_paths.share_dirs) — a session
        # provisioned before the relocation republished still writes under
        # the legacy root, and scanning one root alone is a guard that
        # cannot fire.
        candidate_dirs = [
            Path(d)
            for d in machinery_paths.share_dirs(str(base_dir), session_id)
        ]
        candidate_dirs = [d for d in candidate_dirs if d.is_dir()]
        if not candidate_dirs:
            return None

        normalized_target = _normalize(file_path)
        basename = normalized_target.rsplit("/", 1)[-1]

        # Never let the guard's own module file (or the share directory it
        # walks) trip itself — not a real risk given the agent_type gate,
        # but keeps the walk narrowly scoped to what it claims.
        lazy_roster = _LazyRoster()
        pending = None
        for sidecar_dir in candidate_dirs:
            pending = _find_pending_sidecar(
                sidecar_dir, normalized_target, basename, lazy_roster
            )
            if pending is not None:
                break
        if pending is None:
            return None

        reason = _deny_reason(file_path, str(pending), payload)
        # Advisory envelope (DR-277) — additionalContext only, NEVER
        # permissionDecision:"deny". See INTERFACE.md § Envelope — advisory.
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": reason,
            }
        }
    except Exception:
        # Fail-OPEN on any unexpected error (INTERFACE.md fidelity rule 6).
        return None
