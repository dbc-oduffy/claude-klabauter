"""
coordinator_core.write_guards.block_subagent_archive_write — Python
engine-ification of DoE's retired
``coordinator/hooks/scripts/block-subagent-archive-write.sh`` PreToolUse
hook (deleted 2026-07-16, DoE ``2f8b8450``).

Purpose: unconditional backstop blocking subagent writes anywhere under
``archive/`` outside two narrow, dated carve-outs. Per-dispatch prose
("don't write to archive/") is insufficient calibration on its own —
executors under wrap-up pressure repeatedly self-log completion into
``archive/`` even when the dispatch brief says not to; this guard is the
fail-closed safety-net layer.

Originally a faithful engine-ification rather than a redesign — the
reference hook's guard order, regexes, carve-outs, and deny-reason text
were ported verbatim. The two 2026-08-03 widenings below deliberately
depart from that fidelity. The 2026-08-06 widenings depart further:
``_deny_reason`` now routes three ways rather than reusing the reference
hook's single unconditional line. A week-changelogs target (allow-condition
(7) below is the carve-out; this is about the deny path for targets that
still miss it) gets its own "Use instead" line. A daily-summary-SHAPED
target — directly under ``archive/``, or under ``archive/daily-summaries/``
but missing ``_DAILY_SUMMARY_RE`` — keeps the reference hook's line
byte-for-byte, since that is the only case it actually describes. Every
other denied ``archive/`` subtree (``specs/``, ``lessons-archived/``,
``release-notes/``, and any other/future subtree) gets a safe default
naming no writable path at all — see ``_deny_reason`` and the negative-spec
bullet below. This was found to matter in practice: `/distill` § 5a
dispatches an apply-agent to trim ``archive/specs/YYYY-MM/<name>.md`` in
place, and `/update-docs` Phase 6 → `/learn-lessons` Phase 4 dispatches an
agent to append to ``archive/lessons-archived/YYYY-MM.md`` — both denied by
this guard, and both were misrouted to "use instead" the daily-summaries
path before this widening.

Policy (stated here because it was previously only an emergent property of
two guards composing): archived plan/spec bodies are NOT append-never.
Annotating an archived body whose premise a later memo or finding changes
is sanctioned work — see the pickup skill's memo-to-plan write-through
rule. What this guard blocks is unsanctioned wrap-up self-logging into
``archive/``, not annotation; a writer with a legitimate reason to annotate
gets a sanctioned door (allow-condition (5)), never an override.

Spec backlink: archive/specs/2026-05-27-cqcs-cluster1-delegate-constraint-adherence.md § E5
Ported from the retired DoE bash guard ``block-subagent-archive-write.sh``
  (deleted 2026-07-16, DoE ``2f8b8450``).

Widened 2026-08-03 to close a naming-dependent hole (see
``cross-repo/inbox/2026-08-03-doe-claude-em-archive-write-guard-pincer.md``):
the guard now gates on RAW ``agent_id`` presence (any non-empty value),
the exact complement of
``block_em_hand_edit_pending_review_integration.py``'s own EM/subagent
split (``payload.get("agent_id")`` present -> allow, at that guard's
line 252) — so the two guards' allow-conditions are complementary rather
than leaving a gap for a named-teammate ``agent_id`` shape
(``a<name>-<16hex>``) to fall through both. The resolved/canonical
identity (via the shared ``_subagent_identity`` resolver,
factored out of ``block_subagent_plan_body_write.py``) is used only for
the deny-reason text and the deny audit-log line, and for the new
review-integrator allow-condition below — never for the fire/no-fire
decision itself, which is presence-only.

Also widened 2026-08-03 to give ``coordinator:review-integrator`` a
sanctioned ``archive/`` write path (same memo): the EM-side guard
``block_em_hand_edit_pending_review_integration`` routes its deny to
"dispatch coordinator:review-integrator against <sidecar>", but without
this allow-condition that dispatch's own write to ``archive/`` was denied
right back — a composition leaving no sanctioned writer and training
operators toward an unrelated override.

Fires on Write|Edit|MultiEdit|NotebookEdit when:
  (1) the top-level ``agent_id`` field is present and non-empty (subagent
      write, not a top-level EM write) — RAW presence, not format-gated
      (see 2026-08-03 widening above), AND
  (2) the normalized ``file_path`` matches ``(^|/)archive/`` (any path
      under ``archive/``), AND
  (3) the resolved agent's back-pointer ``subagent_type`` is NOT
      ``coordinator:review-integrator`` (see allow-condition (5) below).

Allow conditions (pass through):
  (1) No agent_id at all (top-level EM write) -> always allow.
  (2) Path not under archive/ -> allow.
  (3) Path IS archive/daily-summaries/YYYY-MM-DD[-<machine>].md -> allow
      (daily-summary carve-out; sanctioned subagent write path).
  (4) Path IS archive/completed/YYYY-MM/YYYY-MM-DD-<slug>.md -> allow
      (per-entry completion fallback carve-out; restored per the
      hooks-behavior.test.js:791 regression oracle — see reference hook
      comment on commit 62bb1cd9).
  (5) The resolved agent's back-pointer ``subagent_type`` is exactly
      ``coordinator:review-integrator`` -> allow (2026-08-03 widening;
      sanctioned archive/ writer for applying review findings against an
      archived plan/spec body). Asymmetric fail-open discipline, NAMED
      explicitly because a future reader could otherwise "fix" it to
      match ``block_subagent_plan_body_write``'s opposite-direction
      default: a FAILED or missing back-pointer lookup here returns ``""``,
      which does NOT match ``coordinator:review-integrator`` and therefore
      falls through to the normal deny path. Unlike
      ``block_subagent_plan_body_write`` (where a lookup-fail allows,
      PM-directed 2026-06-09, because that guard's default is per-kind and
      most kinds are legitimate plan-body editors), THIS guard's default is
      deny-everything-under-archive/ — a lookup-fail must not silently
      reopen that backstop, so only a POSITIVE
      ``coordinator:review-integrator`` match allows.
  (6) Override env COORDINATOR_OVERRIDE_SUBAGENT_ARCHIVE=1 -> allow.
  (7) Path IS archive/week-changelogs/YYYY-MM-DD/<basename>, where
      <basename> is either a dated daily block
      YYYY-MM-DD[-<suffix>].md (suffix case-mixed on disk, e.g.
      ``2026-07-03-Machine-b-backfill.md`` -- unlike allow-condition (3),
      this is NOT lowercase-restricted) or a week rollup
      WEEK-SUMMARY.md / WEEK-SUMMARY.partial.md -> allow (week-changelogs
      carve-out; sanctioned subagent write path). Widened 2026-08-06 to
      close a carve-out gap and a deny-message misroute reported in
      ``cross-repo/inbox/2026-08-06-example-cockpit-repo-em-archive-write-guard-week-changelogs-gap.md``:
      three subagents holding legitimate weekly rollups were hard-denied,
      and the guard's own deny text told them to file the rollup as a
      daily summary under a date it did not describe. Does NOT carve out
      ``HEADER.priorities.<hash>.md`` (arrives by ``mv``/``git mv``, which
      this guard's tool-name gate never intercepts; a subagent has no
      legitimate reason to author it in place).

Negative-spec:
  - Does NOT restrict the fire condition to any particular ``agent_id``
    shape (bare-hex or named-teammate) — RAW presence only, since
    2026-08-03. Recognises every subagent identity shape rather than
    treating an unrecognised shape as "no agent_id" (allow). Fidelity to
    the older, simpler reference ``.sh`` (which only ever accepted the
    bare-hex ``^[a-f0-9]{12,}$`` shape) is deliberately BROKEN here: that
    bare-hex-only gate made enforcement of this backstop depend on whether
    a dispatch happened to be named, which is not a property of what the
    write is doing. See the module-level widening note above.
  - Does NOT convert an absolute ``file_path`` to a repo-relative path via
    ``git ls-files`` — the reference hook regex-matches the normalized
    (slash-collapsed) path AS-IS, absolute or relative.
  - Does NOT allow on a failed/missing back-pointer lookup for the
    review-integrator allow-condition — see allow-condition (5)'s
    asymmetric-fail-open note. This is the opposite fail direction from
    ``block_subagent_plan_body_write``'s lookup-fail-is-allow default;
    both are deliberate, for different reasons, per guard.
  - The deny text must NEVER name a writable path that does not describe
    the artifact actually being written. That misroute (always pointing a
    denied write at the daily-summaries carve-out, even for a weekly
    rollup, or for an unrelated subtree like ``archive/specs/`` or
    ``archive/lessons-archived/``) is the defect the 2026-08-06 widenings
    close -- a future reader collapsing ``_deny_reason`` back to a single
    unconditional "Use instead" line would reintroduce it. The default
    (non-week-changelog, non-daily-summary-shaped) branch in particular
    must NEVER name a path at all -- a future reader adding a fourth
    ``archive/`` subtree with its own sanctioned write location must give
    that subtree its own correctly-scoped "Use instead" line, or leave it
    falling through to the safe default; it must never reuse a
    neighbouring subtree's path (e.g. daily-summaries or week-changelogs)
    for a target that path does not describe.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.write_guards._repo_root import resolve_repo_root
from coordinator_core.write_guards._subagent_identity import (
    _read_backpointer_subagent_type,
    _resolve_subagent_identity,
)

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 30

#: Generator-provenance declaration (coordinator_core/ops/generator_provenance.py).
#: This module's only write is _write_block_log()'s best-effort deny-audit
#: append to <git_root>/.git/coordinator-sessions/<session_id>/archive-write-block.log
#: -- inside .git/, never a tracked repo artifact.
GENERATES = []

#: Reference hook — tool-name guard.
_INTERCEPTED_TOOLS = {"Write", "Edit", "NotebookEdit", "MultiEdit"}

#: subagent_type value that is the sole new (2026-08-03) allow-condition.
_REVIEW_INTEGRATOR_TYPE = "coordinator:review-integrator"

#: Reference hook — fast-exit "anywhere under archive/" match.
_ARCHIVE_RE = re.compile(r"(^|/)archive/")

#: Reference hook — daily-summaries dated-file carve-out.
#: The optional ``.observer`` segment (2026-08-07 widening) admits the
#: strategic-observer sidecar that /workday-complete Step 7 dispatches a
#: SUBAGENT to write. Without it the ceremony and this guard were jointly
#: unsatisfiable: the observer's only specified output path is under
#: ``archive/``, so the guard hard-denied every well-behaved observer, and the
#: strategic-observer trail sat empty from 2026-07-23 onward across at least
#: two repos -- silently, because /workweek-complete Step 9's arch-pass
#: skip-condition reads an empty trail as "no architectural risk".
#: Deliberately narrow: a single literal segment on an already-carved-out
#: dated path, not a general subagent exemption. The guard's wrap-up
#: self-log backstop purpose is unchanged -- a ceremony-specified sidecar is
#: not the over-eager-self-log class it exists to stop.
_DAILY_SUMMARY_RE = re.compile(
    r"(^|/)archive/daily-summaries/[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"(-[a-z0-9][a-z0-9-]*)?(\.observer)?\.md$"
)

#: Reference hook — per-entry completion fallback carve-out.
_COMPLETED_RE = re.compile(
    r"(^|/)archive/completed/[0-9]{4}-[0-9]{2}/[0-9]{4}-[0-9]{2}-[0-9]{2}-.+\.md$"
)

#: week-changelogs carve-out (2026-08-06 widening; see module docstring):
#: a dated directory containing either a dated daily block (case-mixed
#: machine/variant suffix permitted, unlike ``_DAILY_SUMMARY_RE``, per the
#: on-disk ``2026-07-03-Machine-b-backfill.md`` shape) or a week rollup.
#: Deliberately does NOT match ``HEADER.priorities.<hash>.md`` -- that
#: artifact arrives by ``mv``, which this guard's tool-name gate never sees.
_WEEK_CHANGELOG_RE = re.compile(
    r"(^|/)archive/week-changelogs/[0-9]{4}-[0-9]{2}-[0-9]{2}/"
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}(-[A-Za-z0-9][A-Za-z0-9-]*)?"
    r"|WEEK-SUMMARY(\.partial)?)\.md$"
)

#: Control-whitespace/C0-control sanitization before interpolating an
#: attacker-influenced file_path into a deny reason.
_CONTROL_WHITESPACE_RE = re.compile(r"[\t\r\n\f\v]")
_C0_CONTROL_RE = re.compile(r"[\x00-\x1f]")

#: Escape-hatch env var named in the deny message.
_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_SUBAGENT_ARCHIVE"


def _normalize_path(file_path: str) -> str:
    """Backslash -> slash, collapse repeated slashes.

    Deliberately simpler than ``subagent_sandbox.engine``'s normalizer: this
    guard never converts an absolute path to repo-relative via
    ``git ls-files`` and does not special-case a leading UNC ``//`` — the
    reference ``.sh`` collapses ALL repeated slashes down to one, full stop.
    """
    normalized = file_path.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _extract_file_path(payload: Dict[str, Any]) -> str:
    """file_path, falling back to notebook_path for NotebookEdit."""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    return tool_input.get("file_path") or tool_input.get("notebook_path") or ""


def _sanitize_file_path_for_reason(file_path: str) -> str:
    """Port of FILE_PATH_SAFE."""
    safe = _CONTROL_WHITESPACE_RE.sub(" ", file_path)
    return _C0_CONTROL_RE.sub("", safe)


def _resolve_git_root(cwd: Optional[str]) -> Optional[str]:
    """``git rev-parse --show-toplevel``; best-effort.

    AC4 (docs/plans/2026-08-07-no-window-subprocess-primitive.md, chunk C3b):
    delegates to the shared, process-lifetime-memoized
    ``write_guards._repo_root.resolve_repo_root`` instead of hand-rolling its
    own spawn — same fail-open-to-``None`` contract as the prior inline
    ``subprocess.run``, so no verdict changes (a ``None`` here still both
    skips the deny-log and falls through the review-integrator
    allow-condition, exactly as before). The prior inline spawn also logged
    a forensic diagnostic on ``OSError`` ("skipping deny-log"); the shared
    resolver swallows all failures silently (never raises), so that
    diagnostic is restored here explicitly rather than lost.
    """
    result = resolve_repo_root(cwd)
    if result is None:
        print(
            f"block-subagent-archive-write: no git root resolved for cwd="
            f"{cwd!r}, skipping deny-log (decision unaffected)",
            file=sys.stderr,
        )
    return result


def _write_block_log(
    git_root: Optional[str], session_id: str, agent_id: str, file_path: str
) -> None:
    """Best-effort per-session deny log.

    Wrapped so any failure (missing git root, unwritable dir, ...) can NEVER
    flip the ALLOW/DENY decision — mirrors bash's trailing ``|| true``.
    """
    if not session_id or not git_root:
        return
    try:
        log_dir = Path(git_root) / ".git" / "coordinator-sessions" / session_id
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(log_dir / "archive-write-block.log", "a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{ts} | DENY | agent_id={agent_id} | path={file_path}\n")
    except OSError as exc:
        # Best-effort audit log only -- never flips the ALLOW/DENY decision
        # (the deny itself already happened by the time this runs). Surfaced
        # because a silently-lost DENY audit entry is worth knowing about.
        print(
            f"block-subagent-archive-write: failed to write deny audit log "
            f"under {git_root}: {exc}",
            file=sys.stderr,
        )


#: A denied write shaped like a plausible wrap-up self-log: directly under
#: ``archive/`` itself, or under ``archive/daily-summaries/`` but missing
#: ``_DAILY_SUMMARY_RE`` (e.g. wrong date shape). Anything else denied
#: under ``archive/`` is a different subtree entirely and MUST NOT be told
#: to file itself as a daily summary -- see ``_deny_reason``.
_DAILY_SUMMARY_SHAPED_RE = re.compile(
    r"(^|/)archive/(daily-summaries/)?[^/]*$"
)


def _deny_reason(
    agent_id: str,
    file_path: str,
    normalized_file_path: str = "",
    payload: Optional[Dict[str, Any]] = None,
) -> str:
    """Deny-reason text, routed by what was actually attempted (2026-08-06
    widenings; see module docstring and
    ``cross-repo/inbox/2026-08-06-example-cockpit-repo-em-archive-write-guard-week-changelogs-gap.md``).

    Three-way routing, none of which may name a writable path that
    misdescribes the artifact actually denied:
      1. A week-changelogs target gets the week-changelogs "Use instead" line.
      2. A daily-summary-SHAPED target (directly under ``archive/``, or under
         ``archive/daily-summaries/`` but missing ``_DAILY_SUMMARY_RE``, e.g.
         a malformed date) gets the legacy byte-for-byte daily-summaries line
         -- this is the only case that line actually describes.
      3. Everything else denied under ``archive/`` (``specs/``,
         ``lessons-archived/``, ``release-notes/``, and any other/future
         subtree) gets a safe default that names NO writable path at all,
         per the module docstring's negative-spec bullet.

    ``agent_id`` here is the resolved/canonical identity (bare-hex
    unchanged, or the named-teammate ``<name>@session-<short>`` form) —
    see the 2026-08-03 widening note in the module docstring.

    ``normalized_file_path`` (already backslash/doubled-slash normalized)
    decides the routing so a write under an unrelated subtree is never told
    to file itself under a carve-out it does not describe.
    """
    file_path_safe = _sanitize_file_path_for_reason(file_path)
    if re.search(r"(^|/)archive/week-changelogs/", normalized_file_path):
        use_instead = (
            "Use instead: `archive/week-changelogs/YYYY-MM-DD/` (the week-start "
            "date), containing either a dated daily block "
            "`YYYY-MM-DD[-<suffix>].md` or a week rollup `WEEK-SUMMARY.md` "
            "(or `WEEK-SUMMARY.partial.md`).\n\n"
        )
    elif _DAILY_SUMMARY_SHAPED_RE.search(normalized_file_path):
        use_instead = (
            "Use instead: `archive/daily-summaries/YYYY-MM-DD.md` (or `-<machine>.md`).\n\n"
        )
    else:
        use_instead = (
            "Use instead: stage your output to your scratchpad and report the "
            "path to your dispatching EM.\n\n"
        )
    _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload)
    return (
        "BLOCKED: subagent write under archive/ (wrap-up self-log backstop).\n"
        f"  Subagent: {agent_id}\n"
        f"  Target:   {file_path_safe}\n"
        + use_instead.rstrip("\n")
        + ("\n\n" + _note if _note else "")
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the archive-write guard against a PreToolUse payload.

    Returns ``None`` (allow) or the nested hard-deny envelope. Fails open
    (returns ``None``) on any missing/unparseable field, matching the
    reference hook's ``-e``-omitted, fail-open-on-error contract.
    """
    # Honor escape hatch first.
    if os.environ.get(_OVERRIDE_ENV_VAR, "0") == "1":
        return None

    # Tool-name guard — defense-in-depth.
    tool_name = payload.get("tool_name") or ""
    if tool_name not in _INTERCEPTED_TOOLS:
        return None

    # agent_id: RAW presence only, since 2026-08-03 (see module docstring
    # widening note) -- the exact complement of
    # block_em_hand_edit_pending_review_integration's own EM/subagent split.
    # No agent_id at all -> top-level EM write -> allow.
    raw_agent_id = payload.get("agent_id") or ""
    if not raw_agent_id:
        return None

    # Resolved/canonical identity, used for the deny-reason/audit-log line
    # and the review-integrator allow-condition below -- never for the
    # fire/no-fire decision itself, which is presence-only (see above).
    session_id = payload.get("session_id") or ""
    agent_id = _resolve_subagent_identity(raw_agent_id, session_id)

    # file_path (or notebook_path fallback).
    file_path = _extract_file_path(payload)
    if not file_path:
        return None

    normalized = _normalize_path(file_path)

    # Fast exit: path not under archive/ -> allow.
    if not _ARCHIVE_RE.search(normalized):
        return None

    # Daily-summaries carve-out.
    if _DAILY_SUMMARY_RE.search(normalized):
        return None

    # Per-entry completion fallback carve-out.
    if _COMPLETED_RE.search(normalized):
        return None

    # Week-changelogs carve-out (2026-08-06 widening).
    if _WEEK_CHANGELOG_RE.search(normalized):
        return None

    git_root = _resolve_git_root(payload.get("cwd"))

    # review-integrator allow-condition (2026-08-03 widening -- see module
    # docstring). Asymmetric fail-open discipline, deliberately: a failed or
    # missing back-pointer lookup returns "" here, which does NOT match
    # _REVIEW_INTEGRATOR_TYPE and therefore falls through to the deny path
    # below -- it must NOT allow, or this would reopen the naming-dependent
    # hole this same widening closed.
    if agent_id and git_root:
        subagent_type = _read_backpointer_subagent_type(git_root, agent_id)
        if subagent_type == _REVIEW_INTEGRATOR_TYPE:
            return None

    # Subagent writing under archive/ outside the daily-summaries carve-out
    # and not a sanctioned review-integrator write -> block.
    session_id = payload.get("session_id") or ""
    _write_block_log(git_root, session_id, agent_id or raw_agent_id, file_path)

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _deny_reason(
                agent_id or raw_agent_id, file_path, normalized, payload
            ),
        }
    }
