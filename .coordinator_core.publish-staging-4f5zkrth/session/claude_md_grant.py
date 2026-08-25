"""
coordinator_core.session.claude_md_grant — session-scoped PM authorization
grant for CLAUDE.md-class writes, per DoE-claude
docs/plans/2026-07-27-claude-md-altitude-triage.md § C5.

Modeled DIRECTLY on ``coordinator_core.session.grant`` (the Tier-U grant,
DR-088 layer 5): same ``.git/coordinator-sessions/<sid>/`` directory, same
``liveness.session_live`` gate, same fail-closed-on-authorization posture,
same sibling-isolation (path-scoped read, NEVER a glob — load-bearing on
this fleet's shared-branch reality, several EM sessions routinely sharing
one working tree). This module does NOT re-derive that concurrency
semantics — it is a structural copy of ``grant.py``'s shape, narrowed to a
single ``granted_by`` value ("pm") because C5's authorization source is,
by design, always an explicit PM ratification (see C4's negative-spec: the
guard's own deny text states the 2026-07-27 PM ratification is a narrow
named exception, not a reopening) — there is no ceremony-implicit route
for a CLAUDE.md-class write the way there is for Tier-U.

Writes/reads ``.git/coordinator-sessions/<sid>/claude-md-write-grant.json``.

Purpose: C4's write-guard (``block_unauthorized_claude_md_write``, not yet
landed at the time this module was authored — Wave 4 gates C5 alongside
C4) denies Write/Edit to any CLAUDE.md-class path absent a live grant from
THIS module. The grant is the guard's named override path, not a bypass of
it — see C4's deny-text negative-spec (design-as-offers: the deny names the
override, never dead-ends).

SUBAGENT-RESOLVABILITY (load-bearing — this is the whole reason the grant
exists, not an incidental property): a PreToolUse guard evaluating a
DISPATCHED SUBAGENT's Write/Edit call runs inside the SAME underlying
Claude Code harness session as the EM that created the grant — one
session, many tool-call turns, including subagent turns dispatched via the
Agent tool. ``core.resolve_session_id()``'s tier 1-3 (``COORDINATOR_SESSION_ID``
/ ``CLAUDE_SESSION_ID`` / ``CLAUDE_CODE_SESSION_ID``) reads process-wide
environment variables set once for the whole harness session, not
per-tool-call — so a subagent's guard evaluation and the granting EM's own
``write_claude_md_write_grant()`` call resolve to the IDENTICAL session id
by construction, with no separate wiring required. See
``TestSubagentResolvability`` in this module's test suite for the case
this closes: a grant written with NO explicit ``session_id`` (i.e. via the
default env-driven resolution any caller — EM or subagent-context guard —
would use) is visible to a caller that also resolves via the default path.
This is a DIFFERENT (weaker, and here sufficient) claim than the Tier-U
grant's sibling-isolation guarantee: the Tier-U grant's tests prove a
DIFFERENT session's grant does NOT leak across; this module's
subagent-resolvability test proves the SAME session's grant DOES resolve
identically regardless of which turn (EM-inline vs. dispatched-agent
context) reads it. Both are true simultaneously and answer different
questions.

Fields (exactly these — no expiry, no use-counter, mirrors the Tier-U
grant's session-scoped-not-time-scoped posture):
    granted_by   "pm" (the only legal value — see module docstring)
    granted_at   ISO-8601 UTC timestamp (``core.now_iso()``)
    session_id   the GRANTING session's sid (== the directory owner, by
                 construction — the writer always writes into its OWN
                 session dir)
    note         the verbatim PM utterance (mirrors the
                 ``execution_authorized_note`` / Tier-U grant ``note``
                 convention) — stored exactly as given, never
                 summarized/truncated/normalized

Public functions:
    write_claude_md_write_grant  — writer. Validates ``granted_by``;
                                    atomic create-or-overwrite of the
                                    CALLING session's own grant.
    read_claude_md_write_grant   — raw reader. Returns the calling
                                    session's grant record as persisted (or
                                    None), with NO liveness check — see
                                    ``check_claude_md_write_grant`` for the
                                    authorization-grade predicate.
    check_claude_md_write_grant  — THE authorization predicate: "does the
                                    CALLING session hold a live CLAUDE.md
                                    write grant?" Returns
                                    ``(bool, Optional[dict])`` so a caller
                                    (the C4 guard) can quote the note in a
                                    denial even when the grant reads
                                    UNGRANTED.

CLI-reachable acquisition (module-level ``main`` mirrors
``coordinator/bin/tier-u-grant-cli``'s subcommand shape, but stays inside
this module rather than a new ``coordinator/bin/`` trampoline — C5's
declared write-scope is ``coordinator_core/session/`` only):

    python3 -m coordinator_core.session.claude_md_grant grant pm "<verbatim PM note>"
    python3 -m coordinator_core.session.claude_md_grant read
    python3 -m coordinator_core.session.claude_md_grant check

Run with ``CLAUDE_KLABAUTER_ROOT`` (or the claude-klabauter checkout) on ``PYTHONPATH``
so ``coordinator_core`` is importable, and from the repo whose CLAUDE.md-
class write the grant should cover (the CLI resolves the calling session
via cwd, same as every other session-hub op). Exit codes mirror
``tier-u-grant-cli``: 0/1 for the mapped bool-returning function, 2 on a
usage/validation error, 3 on a transport failure (import/argv problem
internal to this trampoline — there is no CLAUDE_KLABAUTER_ROOT indirection here
since the module already lives in ``coordinator_core``, so 3 is reserved
for import-shape errors only).

Spec backlink: DoE-claude DoE-claude:pln-claude-md-altitude-triage-earn-31f32e § C5
Precedent: coordinator_core/session/grant.py (Tier-U grant, DR-088 layer 5)
Precedent (persistence-shape cross-check, per C5's own instruction):
    coordinator_core/workday_complete/autonomous_verb.py — that module
    wraps an EXISTING CLI (``misc-session-and-guards autonomous-sentinel``)
    rather than persisting a session-hub artifact of its own; it has no
    ``.git/coordinator-sessions/<sid>/`` write of its own to cross-check
    against. ``grant.py`` remains the sole applicable persistence-shape
    precedent for a NEW per-session grant artifact.

NEGATIVE SPEC — why this module does NOT self-discriminate its caller
(per docs/plans/2026-08-08-discriminate-the-caller-on-the-write-grant.md
§ C6, and do NOT reopen this): the CLI defined above takes no hook payload
— no ``agent_id``, no tool-call context of any kind — so there is nothing
inside this process for it to check against. A future reader tempted to
add "verify the caller is an EM, not a subagent" HERE will find no signal
to verify. That check does not belong in this module; it belongs upstream,
at the two places that actually see caller identity:

  1. ``coordinator_core/bash_guards/block_subagent_grant_acquisition.py``
     — gates the Bash-tool invocation shape (``-m`` module form and the
     widened ``-c`` inline-import form) that would reach this CLI's
     ``grant`` subcommand, denying when a dispatched subagent's
     ``agent_id`` is present (fail-closed when unresolvable).
  2. ``coordinator_core/write_guards/block_subagent_grant_record_write.py``
     — gates a subagent's direct Write/Edit/MultiEdit/NotebookEdit against
     this module's own artifact path
     (``.git/coordinator-sessions/<sid>/claude-md-write-grant.json``),
     independent of how the write was reached.

``_VALID_GRANTED_BY`` stays ``frozenset({"pm"})`` — the plan considered and
rejected adding an honest ``em-dispatch`` value recording the delegation
chain. That value would be truthful in a way a subagent-minted ``"pm"``
record is not (a subagent CAN observe "an EM dispatched me"; it cannot
observe "a PM ratified this"), but honesty of the label was never the
failing criterion — self-mintability was. A subagent minting
``em-dispatch`` still clears its own gate unconditionally on every
dispatch, so the ledger becomes truthful and the gate stays advisory,
which is the exact defect being closed. ``granted_by: "pm"`` means "a PM
authorized this, with the EM relaying" — not "a human typed this command."
The ``note`` field carries the verbatim PM utterance; that contract is
sound and unchanged by this plan.

Named residual (not closed by either guard leg above — state this
honestly, do not claim completeness): a Bash route that the acquisition
guard's token-shape classifier does not recognise — e.g. a subagent that
writes a script to disk and then executes it, or reaches this module
through some other indirection the classifier does not tokenize for —
still reaches this file and its CLI. The write-guard leg cannot see that
route either, because the resulting artifact write happens *inside the
spawned Python process itself* and never surfaces as a Write-tool call for
that guard to match. See the plan's "Complementarity and the residual that
survives both legs" subsection: both legs together raise the cost of
self-acquisition, they do not eliminate it.

Negative-spec (path-scoped read — NEVER glob):
    Do NOT enumerate ``.git/coordinator-sessions/*/claude-md-write-grant.json``
    (or any other glob) to answer "is there a live grant." Resolve the
    CALLING session's own sid and read ONLY that one directory — identical
    rationale to ``grant.py``'s own negative-spec (a live grant held by a
    concurrent sibling EM session must NOT authorize a different session's
    CLAUDE.md write).

Negative-spec (fail closed on authorization, open on infra):
    Identical posture to ``grant.py``: an absent file, an unreadable file,
    malformed JSON, an unrecognised ``granted_by``, or a ``session_id``
    mismatch, or a grant whose session is no longer LIVE all read
    UNGRANTED, not "allow and log." See ``grant.py``'s own negative-spec
    for the full "both rules are simultaneously true" reasoning — that
    reasoning is not re-derived here, only inherited.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.session import core, liveness

# Generator-provenance declaration (generator_provenance.py). write_claude_md_write_
# grant writes only `.git/coordinator-sessions/<sid>/claude-md-write-grant.json` --
# git-internal session-hub state, never a tracked repo artifact.
GENERATES = []

#: The only legal value of the ``granted_by`` field — see module docstring
#: for why this module has no ceremony-implicit route the way Tier-U does.
_VALID_GRANTED_BY = frozenset({"pm"})

_GRANT_FILENAME = "claude-md-write-grant.json"


def _grant_file(sid: str, cwd: Optional[str]) -> Optional[Path]:
    """Resolve ``<session_dir>/claude-md-write-grant.json`` for ``sid``, or
    None if the session dir is unresolvable (not in a git repo). Internal —
    every public function in this module routes through here so there is
    exactly ONE place that names the artifact's location."""
    sdir = core.session_dir(sid, cwd)
    if not sdir:
        return None
    return Path(sdir) / _GRANT_FILENAME


def write_claude_md_write_grant(
    granted_by: str,
    note: str,
    *,
    session_id: Optional[str] = None,
    cwd: Optional[str] = None,
) -> bool:
    """Create-or-overwrite the CALLING session's CLAUDE.md write grant.

    ``granted_by`` must be ``"pm"`` — any other value raises ``ValueError``
    (a caller programming error, not an infra failure). ``note`` is
    required and stored VERBATIM — this function never summarizes,
    truncates, or normalizes it (mirrors ``grant.write_tier_u_grant``'s
    ``note`` convention).

    ``session_id`` defaults to the resolved calling session
    (``core.resolve_session_id(cwd)``) — pass it explicitly only for tests
    or a caller that has already resolved its own id. If the session id
    cannot be resolved, this function returns False rather than raising:
    there is no session directory to write into, which is an infra
    condition, not a caller error.

    Atomicity: ``tempfile.mkstemp`` in the session dir + ``os.replace``
    (same discipline as ``grant.write_tier_u_grant``) — a reader never
    observes a partially-written grant.

    Returns True on success; False on ANY infra failure (session dir
    unresolvable/uncreatable, write/replace failure).
    """
    if granted_by not in _VALID_GRANTED_BY:
        raise ValueError(
            f"granted_by must be one of {sorted(_VALID_GRANTED_BY)}, got {granted_by!r}"
        )
    if not note:
        raise ValueError("note is required (the verbatim PM utterance)")

    sid = session_id or core.resolve_session_id(cwd)
    if not sid:
        return False

    sdir = core.session_dir(sid, cwd)
    if not sdir:
        return False
    try:
        Path(sdir).mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    record = {
        "schema_version": 1,
        "session_id": sid,
        "granted_by": granted_by,
        "granted_at": core.now_iso(),
        "note": note,
    }

    grant_file = Path(sdir) / _GRANT_FILENAME
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f"{_GRANT_FILENAME}.", dir=str(sdir))
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(record, fh)
            fh.write("\n")
        os.replace(tmp_name, grant_file)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            # Best-effort tmp-file cleanup on the error path; the caller
            # already gets a False return regardless.
            pass
        return False
    return True


def read_claude_md_write_grant(
    cwd: Optional[str] = None, *, session_id: Optional[str] = None
) -> Optional[dict]:
    """Raw reader — returns the CALLING (or given) session's grant record
    exactly as persisted on disk, or None if absent/unreadable/malformed.

    Path-scoped: resolves ONE session's own directory, never globs (see
    the module's path-scoped-read negative-spec). Performs NO liveness
    check and NO enum validation — this is the raw artifact, useful for
    inspection/audit tooling (the CLI's ``read`` subcommand). Use
    ``check_claude_md_write_grant`` to decide whether to ACT on a grant.

    Fail-open on infra (never raises except via ``core.session_dir``'s own
    contract): an unresolvable session id, an unresolvable session dir, an
    absent file, an unreadable file, or malformed JSON all return None.
    """
    sid = session_id or core.resolve_session_id(cwd)
    if not sid:
        return None
    grant_file = _grant_file(sid, cwd)
    if grant_file is None or not grant_file.is_file():
        return None
    try:
        raw = grant_file.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    return record


def check_claude_md_write_grant(
    cwd: Optional[str] = None, *, session_id: Optional[str] = None
) -> Tuple[bool, Optional[dict]]:
    """THE authorization predicate: does the CALLING session hold a live
    CLAUDE.md write grant?

    Returns ``(granted, record)``:
      - ``granted`` — True iff ALL of: a grant file exists for the calling
        session, it parses as a JSON object, ``granted_by`` is a
        recognised value (``"pm"``), the stored ``session_id`` matches the
        resolved calling sid (a directory-owner tamper/corruption guard),
        AND that sid is confirmed LIVE via ``liveness.session_live`` —
        presence and well-formedness alone are explicitly insufficient
        (see the module's liveness negative-spec).
      - ``record`` — the raw grant dict whenever one was found and parsed
        as an object, REGARDLESS of whether ``granted`` is True — so the
        C4 guard's denial text can quote the ``note`` / ``granted_by`` /
        ``granted_at`` even for an ungranted (e.g. dead-session) result.
        None only when nothing parseable was found at all.

    ``session_id`` defaults to the resolved calling session
    (``core.resolve_session_id(cwd)``) — pass it explicitly only for tests
    that want to check a specific (possibly non-current) session id
    without mutating environment/sentinel state. An unresolvable calling
    session id reads UNGRANTED with no record (there is no directory to
    have read).

    This function never raises.
    """
    sid = session_id or core.resolve_session_id(cwd)
    if not sid:
        return False, None

    record = read_claude_md_write_grant(cwd, session_id=sid)
    if record is None:
        return False, None

    granted_by = record.get("granted_by")
    if granted_by not in _VALID_GRANTED_BY:
        return False, record

    if record.get("session_id") != sid:
        return False, record

    if not liveness.session_live(sid, cwd):
        return False, record

    return True, record


# ---------------------------------------------------------------------------
# CLI trampoline — see module docstring "CLI-reachable acquisition" for the
# exact invocation the EM runs between waves. Kept in-module (not a new
# coordinator/bin/ script) because C5's declared write-scope is
# coordinator_core/session/ only. Subcommand shape mirrors
# coordinator/bin/tier-u-grant-cli exactly (grant | read | check), minus the
# --ceremony flag this module's single-value granted_by has no use for.
# ---------------------------------------------------------------------------

__all__ = [
    "write_claude_md_write_grant",
    "read_claude_md_write_grant",
    "check_claude_md_write_grant",
    "main",
]

_SUBCOMMANDS = "subcommands: grant | read | check"

_HELP_FLAGS = ("--help", "-h", "help")


def _usage(prog: str) -> int:
    print(f"usage: {prog} <subcommand> <args...>\n{_SUBCOMMANDS}", file=sys.stderr)
    return 2


def _bool_to_exit(result: bool) -> int:
    return 0 if result else 1


def main(argv: List[str]) -> int:
    """``python3 -m coordinator_core.session.claude_md_grant <subcommand>``.

    Exit codes: 0/1 for the mapped bool-returning function (grant/check),
    2 on a usage/validation error, 3 reserved for import-shape failures
    (unused today — this module has no cross-repo CLAUDE_KLABAUTER_ROOT indirection
    to fail, unlike ``tier-u-grant-cli``'s trampoline leg — kept for
    parity with that CLI's documented exit-code contract).
    """
    if not argv:
        return _usage("claude_md_grant")
    subcmd, rest = argv[0], argv[1:]

    if subcmd in _HELP_FLAGS:
        print(f"usage: claude_md_grant <subcommand> <args...>\n{_SUBCOMMANDS}")
        return 0

    if subcmd == "grant":
        if len(rest) != 2:
            return _usage("claude_md_grant grant <granted_by> <note>")
        granted_by, note = rest
        try:
            return _bool_to_exit(write_claude_md_write_grant(granted_by, note))
        except ValueError as exc:
            print(f"claude_md_grant: grant: {exc}", file=sys.stderr)
            return 2

    if subcmd == "read":
        if rest:
            return _usage("claude_md_grant read")
        record = read_claude_md_write_grant()
        if record is not None:
            print(json.dumps(record))
        return 0

    if subcmd == "check":
        if rest:
            return _usage("claude_md_grant check")
        granted, _record = check_claude_md_write_grant()
        return _bool_to_exit(granted)

    print(f"claude_md_grant: unknown subcommand {subcmd!r}", file=sys.stderr)
    return _usage("claude_md_grant")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
