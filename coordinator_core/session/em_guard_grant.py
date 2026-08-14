"""
coordinator_core.session.em_guard_grant — session-scoped EM-exercisable
grant for a bounded tier of hard-deny guards, per
docs/plans/2026-08-13-em-exercisable-in-band-grant-route.md § C1.

Modeled structurally on ``coordinator_core.session.claude_md_grant`` — same
function quartet, same fail-closed-on-authorization posture, same
``_grant_file`` shape, same atomic ``tempfile.mkstemp`` + ``os.replace``.
This module additionally mints a DR-260 unlock sentinel
(``guard_unlock_sentinel.sentinel_path``) so the write it authorizes takes
effect the moment the grant is minted, with no separate operator hand-off.

WHY ``_VALID_GRANTED_BY`` IS ``{"em"}`` HERE WHEN ``claude_md_grant``
REJECTED AN EM-FLAVOURED VALUE (load-bearing — read this before touching
either constant)

``claude_md_grant.py`` deliberately rejected adding an ``"em-dispatch"``
value to its own ``_VALID_GRANTED_BY``: that module's gate
(``block_unauthorized_claude_md_write``) exists to stop *subagents* writing
doctrine files, so handing the EM-flavoured value to the very party the gate
restricts — a subagent could mint ``"em-dispatch"`` on every dispatch and
clear its own gate unconditionally — reopened the exact defect the guard
closed. Self-mintability by the gated party was the failing criterion, not
the honesty of the label.

That reasoning does not transfer here, because the *gated party is a
different actor*. The guards in ``_GRANTABLE_GUARDS`` (the ``bump-*`` pair
this wave) exist to slow down a session about to touch a foreign/outside
repo tree — the EM itself is the legitimate party the guard is warning, not
an actor the guard exists to keep out. A dispatched subagent is separately,
structurally barred from ever reaching this module's grant route at all:
the acquisition path is gated upstream by
``bash_guards/block_subagent_guard_grant.py`` (the Bash-channel gate) and
``write_guards/block_subagent_guard_grant_write.py`` (the direct-write
gate on this module's own artifact) — see C3/C4. ``"em"`` is dangerous only
if the gated party (a subagent) can mint it; here it cannot, by
construction of those two upstream guards, so there is no analogous defect
to reopen.

Writes/reads ``.git/coordinator-sessions/<sid>/em-guard-grant.json``, plus
the DR-260 sentinel at ``guard_unlock_sentinel.sentinel_path(sid,
guard_name)``.

Fields (exactly these — no expiry, no use-counter, mirrors
``claude_md_grant``'s session-scoped-not-time-scoped posture):
    granted_by   "em" (the only legal value — see above)
    granted_at   ISO-8601 UTC timestamp (``core.now_iso()``)
    session_id   the GRANTING session's sid (== the directory owner)
    guard_name   the guard this grant covers (one of ``_GRANTABLE_GUARDS``)
    reason       the verbatim reason given — stored exactly as given, never
                 summarized/truncated/normalized

Public functions:
    write_em_guard_grant  — writer. Validates ``guard_name`` against
                             ``_GRANTABLE_GUARDS``; rejects an empty or
                             whitespace-only ``reason``; writes the durable
                             record FIRST, then the DR-260 sentinel SECOND
                             (deliberate — see below).
    read_em_guard_grant    — raw reader. Returns the calling session's grant
                              record as persisted (or None), with NO
                              liveness check.
    check_em_guard_grant   — the authorization predicate: "does the calling
                              session hold a live grant for this guard?"
                              Returns ``(bool, Optional[dict])``.

RECORD-BEFORE-SENTINEL ORDERING (deliberate, per the plan's own C1 body): a
crash between the two writes leaves a durable record with no live sentinel
— harmless, the write stays denied and the record is just an audit
artifact with no effect. The reverse order would risk a live sentinel with
no record, an unaccountable clear. ``write_em_guard_grant`` therefore
NEVER writes the sentinel before the record succeeds.

CLI-reachable acquisition (module-level ``main`` mirrors
``claude_md_grant``'s CLI shape; stays inside this module rather than a new
``coordinator/bin/`` trampoline — C1's declared write-scope is
``coordinator_core/session/`` only):

    python3 -m coordinator_core.session.em_guard_grant grant <guard-name> "<reason>"
    python3 -m coordinator_core.session.em_guard_grant read
    python3 -m coordinator_core.session.em_guard_grant check <guard-name>

Exit codes mirror ``claude_md_grant``: 0/1 for the mapped bool-returning
function, 2 on a usage/validation error, 3 reserved for import-shape
failures (unused today, kept for parity with the sibling's documented
exit-code contract).

Spec backlink: pln-an-em-exercisable-in-band-gran-6bfb4a § C1
Precedent: coordinator_core/session/claude_md_grant.py
Precedent: coordinator_core/session/grant.py (Tier-U grant, DR-088 layer 5)

NEGATIVE SPEC — why this module does NOT and CANNOT self-discriminate its
caller (per the plan's § Design "The crux: the op seam cannot discriminate
the caller"): the CLI above takes no hook payload — no ``agent_id``, no
tool-call context of any kind — so there is nothing inside this process to
check "is the caller really the EM" against. A future reader tempted to add
that check HERE will find no signal to verify. That check does not belong
in this module; it belongs upstream, at the two places that actually see
caller identity:

  1. ``coordinator_core/bash_guards/block_subagent_guard_grant.py`` (C3) —
     gates the Bash-tool invocation shape that would reach this CLI's
     ``grant`` subcommand, denying when a dispatched subagent's
     ``agent_id`` is present (fail-closed when unresolvable).
  2. ``coordinator_core/write_guards/block_subagent_guard_grant_write.py``
     (C4) — gates a subagent's direct Write/Edit/MultiEdit/NotebookEdit
     against this module's own artifact path, independent of how the write
     was reached.

Negative-spec (path-scoped read — NEVER glob):
    Do NOT enumerate ``.git/coordinator-sessions/*/em-guard-grant.json``
    (or any other glob) to answer "is there a live grant." Resolve the
    CALLING session's own sid and read ONLY that one directory — a
    concurrent sibling EM session's live grant must NOT authorize this
    session (identical rationale to ``claude_md_grant``'s and ``grant.py``'s
    own path-scoped-read negative-spec).

Negative-spec (fail closed on authorization, open on infra):
    An absent file, an unreadable file, malformed JSON, an unrecognised
    ``granted_by``, a ``guard_name``/``session_id`` mismatch, or a grant
    whose session is no longer LIVE all read UNGRANTED, not "allow and
    log." Identical posture to ``claude_md_grant``/``grant.py``.

Negative-spec (no ``bash_guards`` import at module level):
    ``guard_unlock_sentinel.py`` deliberately refuses the
    ``session -> bash_guards`` edge, and this module is its sibling — it
    imports only ``guard_unlock_sentinel`` (also in ``session``), never
    ``bash_guards`` directly. The cross-registry check (that every
    ``_GRANTABLE_GUARDS`` member actually names a live guard somewhere in
    the fleet) lives in a TEST
    (``coordinator_core/session/tests/test_guard_name_uniqueness.py``, C0),
    not in this module's import graph.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.session import core, guard_unlock_sentinel, liveness

#: The bounded wave-1 tier — two members, not three. Deliberately excludes
#: ``scoped_git_commit_claim_conflict``: it is analysed and PM-authorized as
#: tier-eligible in the plan's § The grantable tier, but withheld from
#: wave-1 pending C10 (removes the bug generating most refusals at that
#: guard) and C6 (measures what remains) — adding the member back is then a
#: one-line diff, which is precisely what this enumerated-allowlist design
#: (a literal name list, never a derived predicate over guard metadata) was
#: chosen to buy. A name outside this set is a validation error (exit 2),
#: never a silent no-op — see ``write_em_guard_grant``.
# Generator-provenance declaration (generator_provenance.py). write_em_guard_grant
# writes only `.git/coordinator-sessions/<sid>/em-guard-grant.json` plus the DR-260
# unlock sentinel (also under the git-internal session hub) -- never a tracked repo
# artifact.
GENERATES = []

_GRANTABLE_GUARDS = frozenset({"bump-foreign-repo-write", "bump-outside-repo-write"})

#: The only legal value of the ``granted_by`` field — see module docstring
#: for why an EM-flavoured value is safe HERE when ``claude_md_grant``
#: rejected one for its own, differently-gated party.
_VALID_GRANTED_BY = frozenset({"em"})

_GRANT_FILENAME = "em-guard-grant.json"


def _grant_file(sid: str, cwd: Optional[str]) -> Optional[Path]:
    """Resolve ``<session_dir>/em-guard-grant.json`` for ``sid``, or None if
    the session dir is unresolvable (not in a git repo). Internal — every
    public function in this module routes through here so there is exactly
    ONE place that names the artifact's location."""
    sdir = core.session_dir(sid, cwd)
    if not sdir:
        return None
    return Path(sdir) / _GRANT_FILENAME


def write_em_guard_grant(
    guard_name: str,
    reason: str,
    *,
    session_id: Optional[str] = None,
    cwd: Optional[str] = None,
) -> bool:
    """Create-or-overwrite the CALLING session's grant for ``guard_name``,
    and mint the DR-260 unlock sentinel that makes it take effect.

    ``guard_name`` must be a member of ``_GRANTABLE_GUARDS`` — any other
    value raises ``ValueError`` (a caller programming error, not an infra
    failure; never a silent no-op). ``reason`` is required and must not be
    empty or whitespace-only — also a ``ValueError``. ``reason`` is stored
    VERBATIM — this function never summarizes, truncates, or normalizes it.

    ``session_id`` defaults to the resolved calling session
    (``core.resolve_session_id(cwd)``) — pass it explicitly only for tests
    or a caller that has already resolved its own id. If the session id
    cannot be resolved, this function returns False rather than raising:
    there is no session directory to write into, an infra condition, not a
    caller error.

    ORDERING (deliberate, see module docstring): the durable record is
    written FIRST; the DR-260 sentinel is minted SECOND, only after the
    record write succeeds. A crash between the two leaves a record with no
    live sentinel (harmless — the write stays denied); the reverse order
    would risk a live sentinel with no record.

    Atomicity: ``tempfile.mkstemp`` in the session dir + ``os.replace``
    (same discipline as ``claude_md_grant.write_claude_md_write_grant``) —
    a reader never observes a partially-written grant record.

    Returns True on success (record written AND sentinel minted); False on
    ANY infra failure (session dir unresolvable/uncreatable, record
    write/replace failure, or sentinel-mint failure).
    """
    if guard_name not in _GRANTABLE_GUARDS:
        raise ValueError(
            f"guard_name must be one of {sorted(_GRANTABLE_GUARDS)}, got {guard_name!r}"
        )
    if not reason or not reason.strip():
        raise ValueError("reason is required (must not be empty or whitespace-only)")

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
        "granted_by": "em",
        "granted_at": core.now_iso(),
        "guard_name": guard_name,
        "reason": reason,
    }

    grant_file = Path(sdir) / _GRANT_FILENAME
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f"{_GRANT_FILENAME}.", dir=str(sdir))
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
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

    # Sentinel minted only AFTER the record is durably on disk — see the
    # ordering paragraph above.
    sentinel_path = guard_unlock_sentinel.sentinel_path(sid, guard_name)
    try:
        sentinel_path.parent.mkdir(parents=True, exist_ok=True)
        sentinel_path.touch()
    except OSError:
        return False
    return True


def read_em_guard_grant(
    cwd: Optional[str] = None, *, session_id: Optional[str] = None
) -> Optional[dict]:
    """Raw reader — returns the CALLING (or given) session's grant record
    exactly as persisted on disk, or None if absent/unreadable/malformed.

    Path-scoped: resolves ONE session's own directory, never globs (see the
    module's path-scoped-read negative-spec). Performs NO liveness check
    and NO enum validation — this is the raw artifact, useful for
    inspection/audit tooling (the CLI's ``read`` subcommand). Use
    ``check_em_guard_grant`` to decide whether to ACT on a grant.

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


def check_em_guard_grant(
    guard_name: str,
    cwd: Optional[str] = None,
    *,
    session_id: Optional[str] = None,
) -> Tuple[bool, Optional[dict]]:
    """THE authorization predicate: does the CALLING session hold a live
    grant for ``guard_name``?

    Returns ``(granted, record)``:
      - ``granted`` — True iff ALL of: a grant file exists for the calling
        session, it parses as a JSON object, ``granted_by`` is a
        recognised value (``"em"``), the stored ``guard_name`` matches the
        one asked about, the stored ``session_id`` matches the resolved
        calling sid (a directory-owner tamper/corruption guard), AND that
        sid is confirmed LIVE via ``liveness.session_live``.
      - ``record`` — the raw grant dict whenever one was found and parsed
        as an object, REGARDLESS of whether ``granted`` is True.

    ``session_id`` defaults to the resolved calling session
    (``core.resolve_session_id(cwd)``) — pass it explicitly only for tests
    that want to check a specific (possibly non-current) session id without
    mutating environment/sentinel state. An unresolvable calling session id
    reads UNGRANTED with no record.

    This function never raises.
    """
    sid = session_id or core.resolve_session_id(cwd)
    if not sid:
        return False, None

    record = read_em_guard_grant(cwd, session_id=sid)
    if record is None:
        return False, None

    granted_by = record.get("granted_by")
    if granted_by not in _VALID_GRANTED_BY:
        return False, record

    if record.get("guard_name") != guard_name:
        return False, record

    if record.get("session_id") != sid:
        return False, record

    if not liveness.session_live(sid, cwd):
        return False, record

    return True, record


# ---------------------------------------------------------------------------
# CLI trampoline — mirrors coordinator_core.session.claude_md_grant's shape
# (grant | read | check), kept in-module rather than a new coordinator/bin/
# script because C1's declared write-scope is coordinator_core/session/
# only.
# ---------------------------------------------------------------------------

__all__ = [
    "write_em_guard_grant",
    "read_em_guard_grant",
    "check_em_guard_grant",
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
    """``python3 -m coordinator_core.session.em_guard_grant <subcommand>``.

    Exit codes: 0/1 for the mapped bool-returning function (grant/check),
    2 on a usage/validation error, 3 reserved for import-shape failures
    (unused today, kept for parity with ``claude_md_grant``'s documented
    exit-code contract).
    """
    if not argv:
        return _usage("em_guard_grant")
    subcmd, rest = argv[0], argv[1:]

    if subcmd in _HELP_FLAGS:
        print(f"usage: em_guard_grant <subcommand> <args...>\n{_SUBCOMMANDS}")
        return 0

    if subcmd == "grant":
        if len(rest) != 2:
            return _usage("em_guard_grant grant <guard_name> <reason>")
        guard_name, reason = rest
        try:
            return _bool_to_exit(write_em_guard_grant(guard_name, reason))
        except ValueError as exc:
            print(f"em_guard_grant: grant: {exc}", file=sys.stderr)
            return 2

    if subcmd == "read":
        if rest:
            return _usage("em_guard_grant read")
        record = read_em_guard_grant()
        if record is not None:
            print(json.dumps(record))
        return 0

    if subcmd == "check":
        if len(rest) != 1:
            return _usage("em_guard_grant check <guard_name>")
        (guard_name,) = rest
        granted, _record = check_em_guard_grant(guard_name)
        return _bool_to_exit(granted)

    print(f"em_guard_grant: unknown subcommand {subcmd!r}", file=sys.stderr)
    return _usage("em_guard_grant")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
