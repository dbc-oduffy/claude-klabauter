"""coordinator_core.write_guards.block_subagent_guard_grant_write — deny a
dispatched subagent's Write/Edit/MultiEdit/NotebookEdit against EITHER
EM-guard-grant artifact: the durable record, or the DR-260 unlock sentinel
itself.

Purpose: chunk C4,
docs/plans/2026-08-13-em-exercisable-in-band-grant-route.md. Models
directly on ``block_subagent_grant_record_write`` (the CLAUDE.md-grant
write-channel leg) but covers TWO artifacts, not one:

  (1) The durable grant record at its git-common-dir-RESOLVED path,
      ``<common-dir>/coordinator-sessions/<sid>/em-guard-grant.json`` — the
      record ``coordinator_core.session.em_guard_grant`` (chunk C1, landing
      in the same wave) writes.
  (2) The DR-260 unlock sentinel itself, at the path
      ``coordinator_core.session.guard_unlock_sentinel.sentinel_path``
      resolves — ``<platform-temp-dir>/coordinator-guard-unlock-
      <sid>.<guard-name>``.

Leg (2) is the more important of the two and has NO analogue in
``block_subagent_grant_record_write``/``claude_md_grant``: that sibling
pair's record IS the thing that clears its guard, so guarding the record
alone is complete. Here, the DURABLE RECORD is descriptive metadata (why a
grant was made) — the sentinel is the artifact
``write_guards.engine``/``bash_guards.dispatch`` actually ``consume()`` to
clear a hard-deny. A guard that covered only leg (1) would leave the real
bypass wide open: a subagent could hand-author a live-looking sentinel file
at the resolved temp-dir path for ANY ``(sid, guard_name)`` pair it chooses
and self-clear any grantable guard on its very next attempt, without ever
touching the durable record at all.

IDENTITY-GATE aspect: agent_id-PRESENCE only, same shape as
``block_subagent_grant_record_write`` — any dispatched subagent, of any
type, is in scope. Not narrowed to a single ``subagent_type`` for the same
reason that sibling gives: narrowing here would leave the exact write path
this module exists to close open to every other subagent type.

PATH-RESOLUTION aspect, leg (1) (the durable record): identical shape to
``block_subagent_grant_record_write``'s own — resolves the repo root via
``coordinator_core.write_guards._repo_root.resolve_repo_root``, then the
git COMMON dir via ``coordinator_core.git.git_dir.resolve_git_common_dir``,
joins ``coordinator-sessions/`` under that resolved common dir, and gates
containment/filename match on the RESOLVED, case-folded path — never a bare
lexical/regex match on the raw ``file_path`` string. A ``..`` traversal
segment is lexically COLLAPSED via ``posixpath.normpath`` before the
containment/filename check runs (same rationale as the sibling module's own
docstring: denying every ``..``-bearing candidate unconditionally would be
an unscoped false positive, not a fix).

The record's filename is imported as ``_GRANT_FILENAME`` from
``coordinator_core.session.em_guard_grant`` (chunk C1 of this same plan),
never hand-copied — so a rename there cannot silently desync the two
modules. This matches both the modelled precedent
(``block_subagent_grant_record_write`` imports the same constant from
``claude_md_grant``) and leg (2)'s ``_SENTINEL_PREFIX`` discipline below.

PATH-RESOLUTION aspect, leg (2) (the sentinel): resolves the platform temp
directory the SAME way ``guard_unlock_sentinel.sentinel_path`` does — via
``tempfile.gettempdir()`` — and matches any candidate whose containing
directory equals that resolved temp dir (case-folded) and whose basename
starts with ``guard_unlock_sentinel._SENTINEL_PREFIX``. Deliberately NOT
scoped to one specific ``(session_id, guard_name)`` pair: a subagent
attempting this bypass could name ANY session id or ANY grantable guard
name it wants (its own session id, a guessed guard name, anything) — the
guard must close the whole sentinel-shaped filename family under the
resolved temp dir, not one hand-picked pair. ``_SENTINEL_PREFIX`` is
imported (not hand-copied) from ``guard_unlock_sentinel`` itself, so a
rename there cannot silently desync the two modules — mirrors the
constant-import discipline the record leg would use once C1 lands.

Allow-conditions (fail-CLOSED is NOT appropriate here — mirrors
``block_subagent_grant_record_write``'s own shape, since the EM is the
legitimate writer of both artifacts):
  (1) No ``agent_id`` (top-level EM write) -> always allow.
  (2) Resolved path matches neither the grant-record shape nor the
      sentinel-filename-under-temp-dir shape -> allow.
  (3) ``tool_name`` not in ``_INTERCEPTED_TOOLS`` -> allow (defense-in-
      depth, matches every sibling ``write_guards`` module's convention).

Design-as-offers: the deny text does NOT name the grant CLI, and — per the
dispatch brief's B6/B8 framing — never reveals to a dispatched subagent
that an unlock mechanism exists at all. It states the refusal and offers
exactly one terse alternative (report BLOCKED to the EM), nothing more —
no self-legitimacy, no repetition, no apology, no override key.

Complementary channel, named honestly (not closed by this module alone):
this leg covers the Write-tool channel ONLY, for BOTH artifacts. The
Bash-channel sibling for the SAME two artifacts is
``coordinator_core.bash_guards.block_subagent_guard_grant`` (chunk C3,
landing in the same wave) — it classifies Bash invocation token shapes
instead of Write-tool payloads. Neither leg subsumes the other: a
Bash-spawned write that the C3 classifier does not recognise still reaches
either artifact, and this module cannot see it either, because its own
matcher is the Write-tool surface, not a write performed inside a spawned
process. This module does not, and does not claim to, provide complete
coverage on its own.

Negative-spec:
  - Does NOT gate on ``subagent_type`` — agent_id PRESENCE only.
  - Does NOT hand-copy the grant-record filename — imported as
    ``_GRANT_FILENAME`` from ``coordinator_core.session.em_guard_grant``,
    holding the modelled precedent's import-not-hand-copy discipline.
  - Does NOT scope the sentinel-leg match to one specific
    ``(session_id, guard_name)`` pair — matches the whole sentinel-filename
    family under the resolved platform temp dir (see PATH-RESOLUTION
    aspect, leg (2), above).
  - Does NOT reveal, in its deny text, that an unlock/sentinel mechanism
    exists — the text never names the grant CLI and never describes what
    either artifact is FOR, only that the write is not available to a
    dispatched agent.
  - Does NOT allow a ``..`` traversal segment to bypass the record leg's
    segment-count check — collapsed via ``posixpath.normpath`` before the
    containment/filename check runs, same as
    ``block_subagent_grant_record_write``.
  - Does NOT compare candidate and reference paths case-sensitively — both
    legs case-fold via ``casefold_path`` before comparison (Windows and
    macOS/APFS are case-insensitive filesystems).
  - Does NOT claim completeness on its own — see "Complementary channel"
    above; the Bash-channel sibling (C3) covers a different surface this
    module structurally cannot see.

Spec backlink:
  docs/plans/2026-08-13-em-exercisable-in-band-grant-route.md § C4.
Precedent (identity-gate + record-leg path-resolution shape):
  coordinator_core/write_guards/block_subagent_grant_record_write.py
Sentinel-path source of truth:
  coordinator_core/session/guard_unlock_sentinel.py
Complementary leg (Bash channel, same two artifacts):
  coordinator_core/bash_guards/block_subagent_guard_grant.py (chunk C3)
"""

from __future__ import annotations

import posixpath
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.session.em_guard_grant import _GRANT_FILENAME
from coordinator_core.session.guard_unlock_sentinel import _SENTINEL_PREFIX
from coordinator_core.write_guards._case_fold_path import casefold_path
from coordinator_core.write_guards._repo_root import resolve_repo_root

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]

#: PRIORITY 47 -- unique within the HARD-DENY phase (46 and 50 taken by
#: this module's grant-adjacent neighbours,
#: ``block_subagent_grant_record_write`` (46) and
#: ``block_consumed_handoff_edit`` (50)). Slotting immediately after 46
#: keeps the two grant-write-channel legs co-located in evaluation order;
#: the phase runs first-non-None-wins, so relative order among
#: non-overlapping-path guards has no behavioral effect — grouping only.
PRIORITY = 47

#: Reference-shape tool-name guard (mirrors every sibling write_guards
#: module's defense-in-depth tool_name check).
_INTERCEPTED_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

#: Hand-copied, NOT imported from ``coordinator_core.session.
#: em_guard_grant`` -- that module is chunk C1 of the same plan and had not
#: landed on disk at authoring time (parallel wave dispatch). See module
#: docstring Negative-spec: importing this once C1 lands is a trivial
#: follow-up, not a design change.

#: Rare-use escape hatch — read the module docstring before invoking.
_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_SUBAGENT_GUARD_GRANT_WRITE"


def _normalize_path(file_path: str) -> str:
    """Backslash -> slash, collapse repeated slashes — same helper shape as
    ``block_subagent_grant_record_write._normalize_path``, including its
    UNC-root preservation step."""
    normalized = file_path.replace("\\", "/")
    is_unc = normalized.startswith("//")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    if is_unc and not normalized.startswith("//"):
        normalized = "/" + normalized
    return normalized


def _collapse_traversal(abs_path: str) -> str:
    """Lexically collapse ``.``/``..`` segments in an already-absolute,
    forward-slash-normalized candidate via ``posixpath.normpath`` — pure
    string manipulation, no filesystem access. See
    ``block_subagent_grant_record_write._collapse_traversal`` for the full
    rationale (denying every ``..``-bearing candidate unconditionally is an
    unscoped false positive, not a fix)."""
    is_unc = abs_path.startswith("//")
    collapsed = posixpath.normpath(abs_path)
    if is_unc and not collapsed.startswith("//"):
        collapsed = "/" + collapsed
    return collapsed


def _extract_file_path(payload: Dict[str, Any]) -> str:
    """``file_path``, falling back to ``notebook_path`` for NotebookEdit."""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    return tool_input.get("file_path") or tool_input.get("notebook_path") or ""


def _resolve_git_common_dir(cwd: Optional[str]) -> Optional[str]:
    """Resolve ``cwd``'s repo's git COMMON dir without spawning ``git``.
    Mirrors ``block_subagent_grant_record_write._resolve_git_common_dir``."""
    repo_root = resolve_repo_root(cwd)
    if not repo_root:
        return None
    return str(resolve_git_common_dir(Path(repo_root)))


def _resolve_abs_candidate(normalized_file_path: str, base_dir: str) -> str:
    """Resolve ``normalized_file_path`` to an absolute, traversal-collapsed
    candidate against ``base_dir`` when it is not already absolute."""
    if normalized_file_path.startswith("/") or (
        len(normalized_file_path) >= 2 and normalized_file_path[1] == ":"
    ):
        abs_candidate = normalized_file_path
    else:
        abs_candidate = _normalize_path(str(Path(base_dir) / normalized_file_path))
    return _collapse_traversal(abs_candidate)


def _is_grant_record_path(normalized_file_path: str, common_dir: str) -> bool:
    """``True`` iff ``normalized_file_path``, resolved against
    ``common_dir``, is a
    ``coordinator-sessions/<any-sid>/<_GRANT_FILENAME>`` path under the
    resolved git common dir. Mirrors
    ``block_subagent_grant_record_write._is_grant_record_path`` exactly."""
    abs_candidate_cf = casefold_path(
        _resolve_abs_candidate(normalized_file_path, common_dir)
    )

    sessions_root = casefold_path(
        _normalize_path(str(Path(common_dir) / "coordinator-sessions")).rstrip("/")
        + "/"
    )

    if not abs_candidate_cf.startswith(sessions_root):
        return False

    remainder = abs_candidate_cf[len(sessions_root):]
    parts = remainder.split("/")
    # <sid>/<_GRANT_FILENAME> -- exactly two remaining path segments.
    return (
        len(parts) == 2
        and parts[0] != ""
        and parts[1] == casefold_path(_GRANT_FILENAME)
    )


def _is_sentinel_path(normalized_file_path: str) -> bool:
    """``True`` iff ``normalized_file_path`` resolves to a file directly
    under the resolved platform temp directory whose basename starts with
    ``guard_unlock_sentinel._SENTINEL_PREFIX``.

    Deliberately matches the WHOLE sentinel-filename family, not one
    ``(session_id, guard_name)`` pair — see module docstring PATH-
    RESOLUTION aspect, leg (2). ``base_dir`` for a relative candidate is
    the resolved temp dir itself: an attacker-controlled ``cwd`` is not a
    plausible base for a sentinel drop (the real writer always resolves
    via ``tempfile.gettempdir()``, never a repo-relative path), so a
    relative candidate is resolved against the temp dir the same way an
    absolute one is compared against it.
    """
    temp_dir = casefold_path(_normalize_path(str(Path(tempfile.gettempdir()))))
    abs_candidate = casefold_path(
        _resolve_abs_candidate(normalized_file_path, str(Path(tempfile.gettempdir())))
    )

    parent, _, basename = abs_candidate.rpartition("/")
    if parent != temp_dir.rstrip("/"):
        return False
    return basename.startswith(_SENTINEL_PREFIX.lower())


def _deny_reason(file_path: str, payload: Optional[Dict[str, Any]] = None) -> str:
    """Design-as-offers deny text. Never names the grant CLI, never reveals
    that an unlock mechanism exists — per B6/B8, a dispatched subagent gets
    the refusal and exactly one terse alternative, nothing else."""
    _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload)
    return (
        "BLOCKED: this write is not available to a dispatched agent.\n\n"
        "Report BLOCKED to your EM instead:\n"
        f"  Target: `{file_path}`\n"
        "  Reason: this artifact is EM-managed, not subagent-writable."
        + ("\n\n" + _note if _note else "")
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the guard-grant write guard against a PreToolUse payload.

    Returns ``None`` (allow) or the hard-deny envelope. See module
    docstring "Allow-conditions".
    """
    tool_name = payload.get("tool_name") or ""
    if tool_name not in _INTERCEPTED_TOOLS:
        return None

    # (1) No agent_id -> EM-inline write -> allow.
    agent_id = payload.get("agent_id") or ""
    if not agent_id:
        return None

    file_path = _extract_file_path(payload)
    if not file_path:
        return None

    normalized = _normalize_path(file_path)

    # Leg (2): sentinel path — does not require git resolution.
    if _is_sentinel_path(normalized):
        reason = _deny_reason(file_path, payload)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    # Leg (1): durable grant record — requires git common-dir resolution.
    cwd = payload.get("cwd")
    common_dir = _resolve_git_common_dir(cwd)
    if not common_dir:
        return None

    if not _is_grant_record_path(normalized, common_dir):
        return None

    reason = _deny_reason(file_path, payload)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
