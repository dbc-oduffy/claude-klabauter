"""
coordinator_core.write_guards.block_foreign_family_sidecar_write -- denies a
subagent write into a run-report sidecar leaf named for a DIFFERENT agent of
the SAME dispatched family (subagent_type), under
``state/subagent-share/<session>/``.

Purpose: an agent told "sidecar provisioning missed" has no file of its own
to stamp, and on 2026-08-31 one of them wrote its ``integrated_from``
receipt into a CONCURRENTLY-RUNNING sibling integrator's sidecar instead.
That field is what ``guard-kira-verdict-routed`` and the ``review_receipt``
gate read to decide whether a review was integrated, so a present,
well-formed stamp about someone else's work is strictly worse than
absence -- absence is the thing those gates are built to notice. Filed at
``state/bug-backlog/2026-08-31-missing-sidecar-provisioning-sends-an-
integrator-receipt-into-a-siblings-file.yaml``.

The sidecar leaf naming convention this guard reads is
``coordinator_core.subagent_sandbox.provision_report``'s own
``<label>.<agent_id>.md`` scheme (``_provision``'s ``derived_key`` branch:
``f"{sanitized_label}.{agent_id}"``), where ``label`` is the sanitized
(``_SEGMENT_WHITELIST_RE``-reduced, i.e. every non
``[A-Za-z0-9._@-]`` character dropped -- a colon-bearing ``agent_type``
string like ``coordinator:executor`` collapses to ``coordinatorexecutor``)
effective dispatched type and ``agent_id`` is the resolved canonical
identity (bare hex, or ``<name>@session-<short>``). This guard never
imports that module (a PreToolUse hook and a spawn-time provisioner have no
runtime dependency on one another); it re-derives the same sanitization
locally, by name, so both sides read one convention rather than risking two
independently-drifting copies.

Two legs, in budget order:

Leg 1 (every write, pure string work, no I/O): does the target even LOOK
like a sidecar leaf under ``state/subagent-share/<session>/``? A target that
does not resolve under that directory, or whose leaf does not split into
``<label>.<agent_id>.md``, is outside this guard's remit entirely -- it
falls through to ALLOW without ever reaching leg 2. This is an applicability
gate, not a deny path in itself: nothing about a non-sidecar write, or a
malformed leaf, is denied HERE (a malformed leaf is exactly the shape a
legitimately hand-authored non-sidecar doc under that directory would have,
and this guard's remit is the foreign-family case, not filename hygiene).

Leg 2 (only reached for a leaf-shaped target): resolves the CALLER's own
dispatched ``subagent_type`` via the shared back-pointer chain
(``_subagent_identity._read_backpointer_subagent_type``, pinned to the
CALLING session via ``expected_em_session_id``) and denies ONLY when both
of the following hold:
  (a) the resolved type's sanitized label form equals the leaf's ``<label>``
      -- the write is INTO this agent's own dispatched family's sidecar
      shape, AND
  (b) the leaf's ``<agent_id>`` is NOT the caller's own resolved canonical
      agent id -- the write targets a DIFFERENT family member's file.

Every other outcome is an ALLOW, and each is a DELIBERATE fail-open, not an
omission:
  - No ``agent_id`` on the payload at all (EM main-loop write, never a
    subagent write) -> allow. This guard only ever fires on a SUBAGENT
    write; the EM writing into ``state/subagent-share/`` (e.g. populating
    ``commits:`` after the EM-serial commit, per every run-report citizen's
    own Commit Discipline) is unconditionally out of scope.
  - No git root resolvable (best-effort ``git rev-parse --show-toplevel``
    walk failed) -> allow. This guard has no independent way to resolve the
    back-pointer chain without a git root, and a PreToolUse hook must never
    brick a spawn over an unresolvable environment fact.
  - The caller's own ``agent_id`` does not resolve through
    ``_resolve_subagent_identity`` (unrecognised shape) -> allow. Symmetric
    with every other write-guard's identity-unresolvable posture (see
    ``block_subagent_archive_write``'s own presence-only fire gate, though
    that guard's downstream allow-condition posture is asymmetric for a
    DIFFERENT, named reason -- see this module's own asymmetry note below).
  - The back-pointer lookup itself fails (absent, unreadable, malformed
    ``em-session-id.txt``, session-id mismatch against
    ``expected_em_session_id``, missing/ambiguous ``dispatched-agents.txt``
    row) -> allow (``_read_backpointer_subagent_type`` returns ``""``,
    which cannot equal any sanitized non-empty label).
  - The resolved type's sanitized label does not equal the leaf's label
    (a DIFFERENT family, e.g. a code-reviewer writing into an executor's
    sidecar shape by hand) -> allow. This guard's remit is SAME-family
    misdelivery -- a cross-family write is a different failure mode this
    guard does not model and must not conflate with.
  - The leaf's ``agent_id`` IS the caller's own resolved id (writing its own
    file) -> allow, trivially -- this is the entire population of writes
    this guard exists to let through unimpeded.

Deliberate fail-open, named explicitly (mirrors
``block_subagent_archive_write``'s own explicit-asymmetry discipline, for a
DIFFERENT direction and a DIFFERENT reason): EVERY leg-2 failure mode above
resolves to ALLOW, never DENY. Unlike ``block_subagent_archive_write``'s
review-integrator allow-condition (whose failed lookup falls through to a
deny-everything-under-archive/ DEFAULT), this guard's only failure-free path
IS the deny -- there is no safe default to fail closed TO. A caller whose
identity this guard cannot positively resolve, or whose family this guard
cannot positively confirm, is not distinguishable from a legitimate
first-class writer (a hand-authored non-sidecar doc, a differently-shaped
dispatch, an EM write missing only its ``agent_id`` stamp), and denying that
population on an unresolvable fact would be a false positive with no
recovery path narrower than "stop dispatching". Only a POSITIVELY confirmed
same-family, different-member write denies.

Negative-spec:
  - Does NOT gate on tool name beyond the standard Write/Edit/MultiEdit/
    NotebookEdit matcher set every write guard shares -- see ``MATCHERS``.
  - Does NOT convert an absolute ``file_path`` to a repo-relative path via
    ``git ls-files`` -- matches the normalized (slash-collapsed) path
    AS-IS, mirroring every sibling guard's discipline
    (``block_subagent_archive_write._normalize_path``'s own docstring notes
    the same choice).
  - Does NOT import ``coordinator_core.subagent_sandbox.provision_report``
    -- a PreToolUse hook has no runtime dependency on the spawn-time
    provisioner; the sanitization convention is re-derived locally by name
    (see module docstring above), not imported.
  - Does NOT compare the leaf's ``<agent_id>`` against the caller's RAW
    ``agent_id`` -- ``provision_report``'s own ``derived_key`` branch keys
    the leaf on the RESOLVED canonical id (``resolve_effective_types``'s
    first return leg), so this guard resolves the caller's raw id through
    the same ``_resolve_subagent_identity`` chain before comparing, or the
    id-match allow-condition would never fire for a named-teammate dispatch
    whose raw and resolved shapes differ.
  - Does NOT write an audit log on deny (unlike
    ``block_subagent_archive_write``'s best-effort deny log) -- no caller
    has asked for one, and adding one here would be a second write path for
    a chunk scoped to detection only.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from coordinator_core.write_guards._repo_root import resolve_repo_root
from coordinator_core.write_guards._subagent_identity import (
    _read_backpointer_subagent_type,
    _resolve_subagent_identity,
)

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 30

#: No filesystem writes of its own -- detection only.
GENERATES = []

_INTERCEPTED_TOOLS = {"Write", "Edit", "NotebookEdit", "MultiEdit"}

#: Leg 1 -- pure-string applicability gate. Captures the session-id
#: directory segment and the leaf basename; the leaf's own
#: ``<label>.<agent_id>.md`` split happens separately below (an
#: ``<agent_id>`` may legitimately contain a literal ``.``, e.g. a
#: ``<name>@session-<short>`` teammate id never does today but the leaf
#: grammar does not forbid it, so the FIRST ``.`` -- not a greedy split --
#: decides where ``<label>`` ends).
_SIDECAR_LEAF_RE = re.compile(
    r"(^|/)state/subagent-share/(?P<session>[^/]+)/(?P<leaf>[^/]+)$"
)

#: Same single-segment whitelist ``provision_report._SEGMENT_WHITELIST_RE``
#: uses to sanitize the ``<label>`` component -- re-derived here rather than
#: imported (see module docstring's negative-spec bullet).
_LABEL_WHITELIST_RE = re.compile(r"[^A-Za-z0-9._@-]")


def _sanitize_label(value: str) -> str:
    """Reduce ``value`` (a ``subagent_type``/``agent_type`` string, e.g.
    ``coordinator:executor``) to the same sanitized form
    ``provision_report`` stamps into a sidecar's filename leaf --
    everything outside ``[A-Za-z0-9._@-]`` dropped, nothing rejected (a
    degenerate empty result compares equal only to another degenerate empty
    result, which never matches a non-empty leaf label).
    """
    return _LABEL_WHITELIST_RE.sub("", value or "")


def _extract_file_path(payload: Dict[str, Any]) -> str:
    """file_path, falling back to notebook_path for NotebookEdit."""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    return tool_input.get("file_path") or tool_input.get("notebook_path") or ""


def _normalize_path(file_path: str) -> str:
    """Backslash -> slash, collapse repeated slashes."""
    normalized = file_path.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _split_sidecar_leaf(normalized_path: str) -> Optional[Dict[str, str]]:
    """Leg 1: does ``normalized_path`` resolve under
    ``state/subagent-share/<session>/`` with a leaf splitting into
    ``<label>.<agent_id>.md``?

    Returns ``{"session": ..., "label": ..., "agent_id": ...}`` on a match,
    ``None`` otherwise (not leaf-shaped, or not under the sidecar
    directory -- this guard's applicability gate, not a deny path).
    """
    match = _SIDECAR_LEAF_RE.search(normalized_path)
    if not match:
        return None
    leaf = match.group("leaf")
    if not leaf.endswith(".md"):
        return None
    stem = leaf[: -len(".md")]
    if "." not in stem:
        return None
    label, _, agent_id = stem.partition(".")
    if not label or not agent_id:
        return None
    return {"session": match.group("session"), "label": label, "agent_id": agent_id}


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the foreign-family sidecar-write guard against a PreToolUse
    payload.

    Returns ``None`` (allow) or the nested hard-deny envelope. Fails open on
    every unresolvable fact -- see module docstring's fail-open enumeration.
    """
    tool_name = payload.get("tool_name") or ""
    if tool_name not in _INTERCEPTED_TOOLS:
        return None

    file_path = _extract_file_path(payload)
    if not file_path:
        return None

    normalized = _normalize_path(file_path)

    # Leg 1: pure-string applicability gate -- no I/O.
    leaf_info = _split_sidecar_leaf(normalized)
    if leaf_info is None:
        return None

    # No agent_id at all -> top-level EM write -> always allow.
    raw_agent_id = payload.get("agent_id") or ""
    if not raw_agent_id:
        return None

    session_id = payload.get("session_id") or ""

    # Leg 2: only reached for a leaf-shaped target.
    resolved_agent_id = _resolve_subagent_identity(raw_agent_id, session_id)
    if not resolved_agent_id:
        return None

    git_root = resolve_repo_root(payload.get("cwd"))
    if not git_root:
        return None

    caller_subagent_type = _read_backpointer_subagent_type(
        git_root, resolved_agent_id, expected_em_session_id=session_id
    )
    if not caller_subagent_type:
        return None

    caller_label = _sanitize_label(caller_subagent_type)
    if not caller_label or caller_label != leaf_info["label"]:
        return None

    if leaf_info["agent_id"] == resolved_agent_id:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "BLOCKED: a run-report sidecar leaf may only be written by "
                "the agent it names.\n"
                f"  Caller:   {resolved_agent_id} (dispatched as "
                f"{caller_subagent_type})\n"
                f"  Target:   {normalized}\n"
                "Use instead: your own sidecar (the path your dispatch "
                "brief named), not a sibling's."
            ),
        }
    }
