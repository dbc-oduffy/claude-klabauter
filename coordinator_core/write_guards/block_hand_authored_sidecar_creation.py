"""coordinator_core.write_guards.block_hand_authored_sidecar_creation —
hard-deny guard.

Purpose: refuse a hand-created run-report sidecar LOUDLY, at the point it is
CREATED, instead of letting a downstream CLI (e.g.
``coordinator/bin/append-integrator-dispositions.py`` ->
``coordinator_core.ops.append_integrator_dispositions.append_dispositions``,
which checks the target's frontmatter ``agent_type:`` against
``_REVIEWER_AGENT_TYPES``/``_REVIEWER_DOC_TYPE_TOKENS`` and refuses if it is
absent) discover the malformation late, to an agent that can do nothing
about it.

Incident (2026-08-16, state/handoffs/2026-08-16-sidecar-provisioning-is-the-
engines-job.md): an EM saw a reviewer stop for a missing sidecar and
hand-created six files to repair it. Five of those agents had ALREADY been
provisioned by the engine (``coordinator_core.subagent_sandbox.
provision_report``), so the repair produced a SECOND, frontmatter-less file
at a different path. The two integrators dispatched against those repaired
sidecars only discovered the missing ``agent_type:`` when
``append-integrator-dispositions.py`` refused their write — long after the
hand-authored file was created, to an agent with no path back to fix it.

Why this is the right seam. The engine's real provisioner
(``provision_report.py::_frontmatter``) always stamps ``status``,
``agent_type``, ``spawned_at``, ``lead_session_id``, ``divergence``,
``commits``, ``dispatch_feed`` in that fixed order (see that function's own
"field NAMES/ORDER" pin) — a file provisioned by the engine can never be
missing ``agent_type``. The ONLY way a sidecar under
``state/subagent-share/<session_id>/*.md`` can be born without one is a
hand-write (Write tool, not ``provision_report``'s ``open()`` call, which is
structurally exempt from PreToolUse per every sibling scaffold-offer guard's
own documented carve-out). Gating on "new file, this path shape, no
``agent_type:``" therefore catches exactly the hand-authored-scaffold case
this guard exists to stop, without touching a single legitimately-provisioned
sidecar (which always already carries the field) or a legitimate in-flight
BODY edit to an existing sidecar (Edit/MultiEdit are not matched — this
guard only gates the Write that CREATES the file).

``run-report.schema.json``'s own ``required`` list is only ``["status"]`` —
schema-shape validation alone (even under
``COORDINATOR_SCHEMA_STRICT=1``) would pass a hand-made file that sets
``status: open`` but omits ``agent_type`` entirely, and in the DEFAULT
(non-strict) mode schema-shape validation is advisory-only, never a deny
(2026-08-06 warn-not-block ruling,
``validate_frontmatter_schema_deny.py`` module docstring). Neither leg of
that guard would have caught this incident's shape unconditionally; this
guard closes that gap directly, unconditionally, independent of strict mode.

Scope — deliberately narrow:
  - MATCHERS = ["Write"] only. Creation is the incident's actual defect
    site; an Edit/MultiEdit against an EXISTING sidecar is normal in-flight
    authoring (agent-filled body sections, status transitions) and must
    never be blocked here.
  - Path shape: ``state/subagent-share/<session-id>/<name>.md`` (any
    depth-2 leaf under the session-keyed directory — the on-disk home every
    provisioning path uses, per ``provision_report.py``'s own path
    templating and this package's ``CONTRACT.md``).
  - Fires ONLY when the target does not already exist on disk (the file is
    genuinely being CREATED by this Write, not overwritten) — mirrors
    ``validate_frontmatter_schema_deny.py::_scaffold_offer_step``'s own
    ``not os.path.exists(abs_file_path)`` new-file gate.
  - Fires ONLY when the prospective content carries NO non-empty
    ``agent_type:`` frontmatter field. A hand-authored file that DOES stamp
    a real ``agent_type:`` (e.g. an agent copying an existing sidecar's
    shape by hand) is, for this guard's purposes, interchangeable with a
    provisioned one — AC's other permitted arm — and is let through.

Negative-spec:
  - Does NOT gate on any other required field (``status``, ``spawned_at``,
    ...) — ``agent_type`` is the one field this incident's downstream
    refusal actually keys on (``append_integrator_dispositions.py``'s own
    ``_extract_frontmatter_agent_type`` check), and widening the gate risks
    false-positive-denying a legitimate ad-hoc scaffold shape this module's
    author has not audited.
  - Does NOT attempt to detect a SECOND sidecar for an already-provisioned
    dispatch (the "five agents already provisioned" duplicate-file half of
    the incident) — the engine has no persisted manifest of "which sidecar
    was already provisioned for this session" that a stateless PreToolUse
    guard could check against without a new tracking surface; the AC this
    guard discharges is satisfied by the loud-refusal arm alone (see
    state/handoffs/2026-08-16-sidecar-provisioning-is-the-engines-job.md's
    own AC wording: "or the engine refuses the hand-created one loudly").
  - Does NOT run schema validation — this is a targeted single-field gate,
    not a redundant second schema-shape checker (that would reproduce the
    "second late checker" defect shape this guard's own dispatch brief
    warns against).
  - Never fails closed on an unexpected error — malformed payload, missing
    field, unreadable content: ALLOW, matching every sibling hard-deny
    guard's fail-open-on-internal-error discipline.

Spec backlink: state/handoffs/2026-08-16-sidecar-provisioning-is-the-engines-job.md
  (AC: "A hand-created sidecar and a provisioned one are either
  interchangeable, or the engine refuses the hand-created one loudly rather
  than letting a downstream CLI refuse it later.")
"""

from __future__ import annotations

import os
import posixpath
import re
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.session import machinery_paths
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter

CLASS = "hard-deny"
MATCHERS = ["Write"]
PRIORITY = 60

#: Escape hatch — recovery-only, mirrors sibling guards' override pattern.
_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_HAND_SIDECAR_WRITE"

#: Path-tail shape: <root>/subagent-share/<session-id>/<name>.md -- the
#: session-keyed sidecar home every provisioning path writes into.
#:
#: Owned by ``machinery_paths.subagent_share_leaf_pattern`` (see
#: ``block_foreign_family_sidecar_write``'s leg-1 note for why the
#: hand-rolled single-root form this replaces was silently dead). The
#: ``.md`` test moves off the regex and onto the captured leaf so it can be
#: casefolded: the prior ``\.md$`` was case-SENSITIVE, so a ``.MD`` leaf on
#: NTFS or default APFS -- the same file, as far as the filesystem is
#: concerned -- fell through to ALLOW. Sibling guard
#: ``block_foreign_family_sidecar_write`` already casefolds this test, on a
#: code-reviewer finding; this brings the two into line rather than leaving
#: one of a matched pair with the gap.
_SIDECAR_LEAF_RE = machinery_paths.subagent_share_leaf_pattern()


def _leaf_match(normalized_path: str):
    """Sidecar-leaf applicability: either machinery root, `.md` leaf,
    casefolded. Returns the match (truthy) or None."""
    match = _SIDECAR_LEAF_RE.search(normalized_path)
    if match is None or not match.group("leaf").casefold().endswith(".md"):
        return None
    return match


def _normalize(file_path: str) -> str:
    normalized = file_path.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _is_absolute(normalized: str) -> bool:
    return normalized.startswith("/") or (
        len(normalized) >= 2 and normalized[1] == ":"
    )


def _resolve_against_cwd(file_path: str, cwd: Optional[str]) -> str:
    """Resolve ``file_path`` against ``payload['cwd']`` when relative, mirroring
    ``block_fleet_delegation_write._resolve_candidate`` — Claude's Write
    ``tool_input.file_path`` is frequently relative to the session's cwd, not
    to this guard-process's own cwd, so an unresolved ``os.path.exists`` can
    resolve against the wrong base.
    """
    normalized = _normalize(file_path)
    if not _is_absolute(normalized):
        base = cwd or "."
        normalized = _normalize(str(Path(base) / normalized))
    return posixpath.normpath(normalized)


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        if os.environ.get(_OVERRIDE_ENV_VAR, "0") == "1":
            return None

        tool_name = payload.get("tool_name") or ""
        if tool_name != "Write":
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        file_path = tool_input.get("file_path") or ""
        if not file_path:
            return None

        normalized = _normalize(file_path)
        if not _leaf_match(normalized):
            return None

        # New-file gate: an overwrite of an already-provisioned sidecar is a
        # different (legitimate) event this guard does not touch. Resolve
        # against payload['cwd'] first -- Write's file_path is frequently
        # relative to the session's cwd, not this guard-process's own cwd.
        resolved_path = _resolve_against_cwd(file_path, payload.get("cwd"))
        if os.path.exists(resolved_path):
            return None

        content = tool_input.get("content") or ""
        split = split_frontmatter(content)
        agent_type = read_fm_field(split.fm_text, "agent_type") if split is not None else None

        if agent_type and agent_type.strip():
            return None

        _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload)
        reason = (
            f"{file_path} has no agent_type: frontmatter, so "
            "append-integrator-dispositions.py will refuse to write to it.\n"
            "Use instead: coordinator-doc-new --type run-report"
            + ("\n\n" + _note if _note else "")
        )

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    except Exception:
        # Fail-open on any unexpected error — mirrors every sibling
        # hard-deny guard's fail-open-on-internal-error discipline.
        return None
