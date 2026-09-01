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
identity (bare hex, or ``<name>@session-<short>``). This guard re-derives
the same sanitization locally rather than importing ``provision_report``
-- that module's own import chain (``engine``, ``snippet_sync.registry``,
``session.scope``) is heavier than this guard's leg-1 pure-string budget
should carry on every PreToolUse write. The two patterns are pinned equal
by
``coordinator_core/write_guards/tests/test_block_foreign_family_sidecar_write.py::test_label_whitelist_matches_provision_report``,
not by a runtime import.

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
CALLING session via ``expected_em_session_id=payload["session_id"]``).
This is the right field to pin against because, inside a dispatched
subagent's own PreToolUse hook, ``CLAUDE_CODE_SESSION_ID``-derived
``session_id`` is the DISPATCHING EM's own id, not a distinct subagent
session -- ``coordinator_core/hooks/nudge_unrouted_sizing.py``'s
``_is_subagent_session`` documents this inheritance directly, and it is
exactly what the back-pointer's own ``em-session-id.txt`` is keyed on.
And denies ONLY when both of the following hold:
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
    with every other write-guard's identity-unresolvable posture.
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

Deliberate fail-open, named explicitly: every leg-2 failure resolves to
ALLOW, never DENY -- only a POSITIVELY confirmed same-family,
different-member write denies.

Negative-spec:
  - Does NOT gate on tool name beyond the standard Write/Edit/MultiEdit/
    NotebookEdit matcher set every write guard shares -- see ``MATCHERS``.
  - Does NOT convert an absolute ``file_path`` to a repo-relative path via
    ``git ls-files`` -- matches the normalized (slash-collapsed) path
    AS-IS, mirroring every sibling guard's discipline
    (``block_subagent_archive_write._normalize_path``'s own docstring notes
    the same choice).
  - Does NOT import ``coordinator_core.subagent_sandbox.provision_report``
    -- its import chain is too heavy for this guard's leg-1 budget; the
    sanitization convention is re-derived locally and pinned equal by a
    test (see module docstring above), not imported.
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
#: ``<label>.<agent_id>.md`` split happens separately below, on the FIRST
#: ``.`` (``partition``), not a greedy split.
_SIDECAR_LEAF_RE = re.compile(
    r"(^|/)state/subagent-share/(?P<session>[^/]+)/(?P<leaf>[^/]+)$"
)

#: Same single-segment whitelist ``provision_report._SEGMENT_WHITELIST_RE``
#: uses to sanitize the ``<label>`` component -- re-derived here rather than
#: imported (see module docstring's negative-spec bullet); pinned equal by
#: test_label_whitelist_matches_provision_report.
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
    # Review: coordinator:code-reviewer -- casefold the extension test so
    # `.MD`/`.Md` on a case-insensitive-but-preserving filesystem (NTFS,
    # default APFS) still resolves to the same leaf-shaped applicability
    # this guard protects, rather than falling through to ALLOW.
    if not leaf.casefold().endswith(".md"):
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
    # Review: coordinator:code-reviewer -- casefold both the label and
    # agent_id equality checks; a case-insensitive-but-preserving
    # filesystem write (e.g. `CoordinatorExecutor.<sibling>.MD`) lands on
    # the same physical file as the canonical-case leaf, so a
    # case-sensitive compare here would fall through to ALLOW on the exact
    # bypass shape this guard exists to deny. Agent ids (bare hex,
    # `<name>@session-<short>`) and sanitized labels never legitimately
    # differ from one another only by case, so casefolding only makes this
    # guard deny MORE, never produces a false deny.
    if not caller_label or caller_label.casefold() != leaf_info["label"].casefold():
        return None

    # The leaf may be keyed on EITHER form of this caller's id, so a match
    # against either one is this agent's own file. `provision_report` names
    # the leaf via `subagent_sandbox.engine._canonical_agent_id`, whose leg
    # (c) returns an already-canonical `<name>@session-<short>` id
    # UNCHANGED, while `_resolve_subagent_identity`'s leg (d) rebuilds that
    # same shape against the live session id. The two disagree exactly when
    # the embedded short is stale, and comparing only against the resolved
    # form then denies an agent writing its OWN sidecar --
    # `test_stale_embedded_short_self_write_allowed` is that case.
    # Checking both is the fail-open reading this guard's posture requires:
    # only a POSITIVELY confirmed foreign write denies, and an id that
    # matches either form of the caller is not positively foreign. Widening
    # here cannot admit a genuine sibling -- a different agent's id matches
    # neither form.
    for own_id in (resolved_agent_id, raw_agent_id):
        if own_id and leaf_info["agent_id"].casefold() == own_id.casefold():
            return None

    own_leaf = "state/subagent-share/%s/%s.%s.md" % (
        leaf_info["session"], leaf_info["label"], resolved_agent_id
    )
    # Register (docs/wiki/guard-messaging.md): one fact, once, plus the
    # terse alternative. The indented block is PATH ENTRIES ONLY -- every
    # non-blank line in it must be a bare path, or `_message_size`'s
    # `_LABELED_INDENT_BLOCK_RE` data exemption does not apply and the
    # echoed paths are charged against the 220-byte prose cap. A
    # `Caller: <id> (dispatched as <type>)` line in this block is what put
    # an earlier draft at 358 prose bytes against that cap.
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "BLOCKED: a sidecar leaf is written only by the agent it names.\n"
                "Your own leaf, then the one you targeted:\n"
                "  %s\n"
                "  %s\n"
                "Write yours, or skip the stamp and say so in your report."
                % (own_leaf, normalized)
            ),
        }
    }
