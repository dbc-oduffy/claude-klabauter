# Unix shebang — was generator-owned by gen-launcher-shim.py --ensure-unix; that mode was retired 2026-07-28 (POSIX-EXEC-ASSUMPTION-GUARD, PM ruling) and no longer regenerates this line.
"""
coordinator_core.hooks.subagent_sidecar_fill_check — PostToolUse-Agent advisory op,
closing the surfacing half of the run-report sidecar contract (detection landed
cb374c0ef: coordinator_core.subagent_sandbox.detect_unfilled_sidecar +
coordinator/bin/check-sidecar-fill).

Purpose: an agent that goes idle with its provision_report.py-scaffolded sidecar
untouched (`status: open`, no agent-authored body) is silent by default today —
nothing runs the detector unless the dispatching EM remembers to. This op makes
that self-surfacing: it re-scans the CALLING SESSION's own
`state/subagent-share/<session_id>/` directory at a PostToolUse-Agent moment and,
only when at least one sidecar is flagged, returns a terse advisory naming the
runnable check.

SEAM CHOICE — PostToolUse-Agent, not SubagentStop. The prior executor's own
recommendation (a SubagentStop hook) was evaluated and rejected: SubagentStop is
an OPEN, upstream-declined harness bug (GH #25147, #33049, both closed
not-planned) that silently stops firing on some builds — this repo's own
zero-tool-use Stage 3 (hooks.subagent_zero_tool_use_resolve) exists SPECIFICALLY
because Stage 1's SubagentStop write cannot be trusted as the sole trigger, and
that module's docstring names "the existing PostToolUse Agent-tool hook, which
already fires reliably and already carries agent_id" as the moment to poll
instead (see hooks.track_dispatched_agents / hooks.agent_completion_log, both
already registered there). This op follows that same precedent rather than
re-opening a seam this codebase already measured and moved off of. It is also
EARLIER than a Stop/SessionEnd surface: it fires on the EM's own next turn,
before the EM has had a chance to commit the agent's work unverified.

Why re-scan the WHOLE session directory rather than try to correlate one
sidecar to this one PostToolUse-Agent event: `agent_id` (the spawned agent's own
id) is not the key sidecars are filed under —
`coordinator_core.subagent_sandbox.provision_report._provision` files every
sidecar under the REQUESTING EM's own `session_id` (see that module's
`lead_session_id` docstring note: "who dispatched, not the SPAWNED agent's own
id"), and a named-teammate sidecar's on-disk stem is a plan/chunk slug or
label+nonce with no recoverable relationship to `agent_id` at all. A per-agent
join is not attemptable from this op's own inputs without inventing a new
producer-side field; scanning the whole `state/subagent-share/<session_id>/`
directory — exactly what `check-sidecar-fill --session` already does — is the
correct-shaped alternative and matches this op's own design brief ("firing only
when status: open AND the body is unfilled is the signal", not per-agent
correlation).

FAIL OPEN, UNCONDITIONALLY. Every branch below — missing session_id, missing
repo_root, an unresolvable git root, any exception from the scan itself —
resolves to `no_advisory()`, never a raise and never a block. This mirrors
`coordinator_core.bash_guards._advisory_dedupe`'s own "an internal error must
never suppress OR block" contract, and the specific fleet-outage shape named in
this op's own design brief (`block-reviewer-bash-outside-allowlist` denying all
Bash from an internal `TypeError`) is exactly what this posture avoids: a
detector bug here must never cost the EM its turn.

DEDUPE — per (session_id, sorted flagged-path set) via
`coordinator_core.bash_guards._advisory_dedupe`, reused rather than
reimplemented: same fail-open contract, same gitdir-marker mechanism already
used for advisory suppression elsewhere in this tree. A session with the SAME
flagged set as a prior firing this session is degraded to the terse
alternative only (no repeated prose); a DIFFERENT flagged set (a new sidecar
went stale, or the set shrank) mints a fresh key and fires in full — matching
`_advisory_dedupe`'s own "different shape in the same session still fires"
guarantee. `advisory_dedupe_key` is not reused directly (it fingerprints an
envelope's `additionalContext`, and this op wants to key on the flagged PATH
SET before composing prose, not after) — this module derives its own shape key
from the sorted flagged path list and calls `already_advised`/`mark_advised`
directly.

Message register (docs/wiki/guard-messaging.md § Register): one fact
("N sidecar(s) open, unfilled") stated once, plus a terse runnable alternative
naming `check-sidecar-fill --session <id>` — no self-legitimacy, no
reassurance, no apology, no override key (there is nothing to override; this
is a non-blocking PostToolUse advisory, shape (d), same as
hooks.nudge_unauthorized_handoff / hooks.postuse_advisory_dispatch).

Cold-path remediation text names a runnable script
(`coordinator/bin/check-sidecar-fill`), never a slash command, per this
op's own design constraint.

Registration: this op is engine-side only. Wiring it into a hooks.json
PostToolUse-Agent matcher entry (mirroring the existing
`hooks.subagent_zero_tool_use_resolve` / `hooks.track_dispatched_agents`
registrations) is coordinator-claude's (DoE's) side of the producer/consumer
split this package's own `__init__` docstring documents for every other
cross-repo hook contract in this file — deliberately not done here.

Spec backlink: dispatching EM's 2026-08-15 break-class chunk (subagent-share
sidecar detection gap) — see
coordinator_core.subagent_sandbox.detect_unfilled_sidecar's own docstring for
the incident this closes the first half of.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.hooks._envelope import no_advisory, post_advisory
from coordinator_core.hooks._payload import field
from coordinator_core.lifecycle import git_common_dir, main_worktree_root

logger = logging.getLogger(__name__)


def _flagged_paths(worktree_root: str, session_id: str) -> List[str]:
    """Return the repo-relative paths of every flagged sidecar for
    ``session_id``, or an empty list on any failure. Never raises on its own
    account — this function has no try/except of its own around
    ``scan_session_dir``; it relies entirely on the caller's (`_handler`'s)
    broad except (module docstring, "FAIL OPEN, UNCONDITIONALLY").

    ``worktree_root`` must be the worktree root (not the gitdir) — the
    caller derives this via ``main_worktree_root(git_common_dir(repo_root))``
    before calling in, matching ``subagent_fabrication_check``'s idiom."""
    from coordinator_core.subagent_sandbox.detect_unfilled_sidecar import scan_session_dir

    verdicts = scan_session_dir(worktree_root, session_id)
    flagged = [v.path for v in verdicts if v.flagged]
    out = []
    for path in flagged:
        try:
            out.append(str(Path(path).relative_to(Path(worktree_root).resolve())))
        except (ValueError, OSError):
            # Fallback path: the relative-path computation exists solely to
            # build a stable dedupe shape key (its only consumer is
            # _dedupe_shape_key's sha256 digest — see _compose_message, which
            # only ever prints len(flagged), never a path). An absolute,
            # drive-lettered Windows path here would only ever change the
            # dedupe key under a race, never leak into the advisory message.
            out.append(path)
    return sorted(out)


def _dedupe_shape_key(flagged: List[str]) -> str:
    """Derive this firing's dedupe shape key from the sorted flagged-path
    set — module docstring, "DEDUPE"."""
    import hashlib

    joined = "\n".join(flagged)
    digest = hashlib.sha256(joined.encode("utf-8", errors="replace")).hexdigest()[:16]
    return "hooks.subagent_sidecar_fill_check__%s" % digest


def _compose_message(session_id: str, flagged: List[str]) -> str:
    """One fact, stated once, plus a terse runnable alternative — see
    module docstring, "Message register"."""
    n = len(flagged)
    noun = "sidecar" if n == 1 else "sidecars"
    return (
        f"{n} {noun} still status: open with no agent-authored body under "
        f"state/subagent-share/{session_id}/. Run: check-sidecar-fill --session {session_id}"
    )


@register_op("hooks.subagent_sidecar_fill_check")
async def _handler(params: dict, repo_root=None) -> dict:
    """PostToolUse-Agent advisory op: re-scan this session's own sidecar
    directory and surface an unfilled sidecar the EM has not yet noticed.

    Inputs (flat scalar, extracted via _payload.field(); "" treated as
    absent): session_id, hook_event_name.

    Returns `no_advisory()` when: session_id/repo_root absent, the scan finds
    nothing flagged, or ANY exception occurs anywhere in the scan/dedupe path
    (module docstring, "FAIL OPEN, UNCONDITIONALLY"). Returns `post_advisory()`
    (shape d, PostToolUse additionalContext, never blocking) exactly when at
    least one sidecar is flagged and this exact flagged set has not already
    fired this session.
    """
    session_id = field(params, "session_id")
    if not session_id or not repo_root:
        return no_advisory()

    try:
        try:
            worktree_root = str(main_worktree_root(git_common_dir(repo_root)))
        except RuntimeError:
            # Distinguishable from "nothing to flag" ONLY in the log, never in
            # the advisory surface itself (fail-open is unconditional; see
            # module docstring "FAIL OPEN, UNCONDITIONALLY") — this is exactly
            # the silent-non-firing shape the P1 fix addresses, so a permanently
            # unresolvable root is logged rather than indistinguishable from a
            # clean scan.
            logger.debug(
                "hooks.subagent_sidecar_fill_check: could not resolve worktree "
                "root from repo_root=%r; falling back to repo_root as-is",
                repo_root,
            )
            worktree_root = str(repo_root)

        flagged = _flagged_paths(worktree_root, session_id)
        if not flagged:
            return no_advisory()

        shape_key = _dedupe_shape_key(flagged)

        gitdir: Optional[Path] = None
        try:
            from coordinator_core.bash_guards._write_bump_marker import resolve_gitdir

            gitdir = resolve_gitdir(str(repo_root))
        except Exception:  # noqa: BLE001 -- fail open: dedupe unavailable, still advise
            gitdir = None

        try:
            from coordinator_core.bash_guards._advisory_dedupe import already_advised, mark_advised

            if already_advised(gitdir, session_id, shape_key):
                return no_advisory()
            mark_advised(gitdir, session_id, shape_key)
        except Exception:  # noqa: BLE001 -- fail open: dedupe unavailable, still advise
            pass

        return post_advisory(_compose_message(session_id, flagged))
    except Exception:  # noqa: BLE001 -- fail open: never block or crash the EM's turn
        return no_advisory()
