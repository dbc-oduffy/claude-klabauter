"""coordinator_core.write_guards.block_em_hand_edit_pending_review_integration
— advisory guard.

Mechanizes the "reviewer findings — apply, don't ratify" rule (example-doctrine-repo
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
exit, cited by the advisory itself — example-doctrine-repo
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
  - Does NOT gate on ANY sidecar type other than ``coordinator:code-reviewer``
    findings sidecars (frontmatter ``agent_type`` exact match) — a
    ``coordinator:review-integrator`` run-report, a ``staff-eng-review``, or
    an ``assessment`` sidecar in the same directory never trips this guard.
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

Spec backlink: example-doctrine-repo docs/plans/2026-07-27-claude-md-altitude-triage.md
  § C14 (chunk id `REVIEW-INTEGRATOR-REQUIRED-GUARD`)
Audited-but-unbuilt precedent this closes: example-doctrine-repo
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

from coordinator_core.bash_guards._helpers import operator_override_note

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 115

#: Rare-use escape hatch — read the module docstring before invoking.
_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_REVIEW_INTEGRATION_PENDING"

#: The one code-reviewer-identity agent_type this guard gates on. Any other
#: agent_type sidecar (run-report, staff-eng-review, assessment, or a
#: different reviewer persona) is out of scope — see module Negative-spec.
#:
#: NARROWER, deliberately, than `ops.append_integrator_dispositions`'
#: `_REVIEWER_AGENT_TYPES` frozenset, which covers the reviewer personas too.
#: The two constants answer different questions and must not be reconciled into
#: one: that set decides which sidecars can RECEIVE a disposition block, while
#: this literal decides which sidecars BLOCK an EM hand-edit. Widening this one
#: to match would make every persona review start blocking EM edits — a
#: behaviour change nothing has asked for, and the reason Negative-spec bullet 3
#: above is stated as a deliberate scope choice rather than a gap. If you are
#: here because the two "look out of sync": they are, on purpose.
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


def _find_pending_sidecar(
    sidecar_dir: Path, normalized_target: str, basename: str
) -> Optional[Path]:
    """Return the first sidecar in `sidecar_dir` that is a
    code-reviewer findings sidecar with unaddressed findings covering the
    target file, or None. Every per-file failure degrades to "skip this
    candidate", never a raise (module-level fail-open discipline)."""
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
        if not _heading_present(text, _FINDINGS_HEADING):
            continue
        if _FINDINGS_SENTINEL in text:
            continue  # unfilled scaffold — reviewer hasn't returned yet
        if _heading_present(text, _DISPOSITIONS_HEADING):
            continue  # already integrated

        if _sidecar_covers_target(text, normalized_target, basename):
            return candidate

    return None


def _deny_reason(
    file_path: str, sidecar_path: str, payload: Optional[Dict[str, Any]] = None
) -> str:
    _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload)
    return (
        "Use instead:\n"
        f"  dispatch coordinator:review-integrator against {sidecar_path} for "
        f"{file_path} - an unaddressed code-reviewer finding on this file must "
        "be applied via the integrator, not hand-edited\n\n"
        "If the integrator is genuinely unavailable, re-dispatch once; if "
        "still unavailable and not break-class, park with an owner instead "
        "of hand-editing; if break-class, hand-apply after your own "
        "fresh-disk re-check and record the deviation in the commit"
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
        sidecar_dir = base_dir / "state" / "subagent-share" / session_id
        if not sidecar_dir.is_dir():
            return None

        normalized_target = _normalize(file_path)
        basename = normalized_target.rsplit("/", 1)[-1]

        # Never let the guard's own module file (or the state directory it
        # walks) trip itself — not a real risk given the agent_type gate,
        # but keeps the walk narrowly scoped to what it claims.
        pending = _find_pending_sidecar(sidecar_dir, normalized_target, basename)
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
