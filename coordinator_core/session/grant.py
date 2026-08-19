"""
coordinator_core.session.grant — Tier-U (full test suite) authorization-grant
writer/reader, per DR-088 (test-breadth ladder, tiered invocation authority)
layer 5: "Authorization grant — Tier U requires a live token; qualifying
ceremonies write an implicit one."

Writes/reads ``.git/coordinator-sessions/<sid>/tier-u-grant.json`` — a
per-session artifact precedented by ``session-shape.json`` (see
``coordinator_core.session.shape``): same directory, same
``tempfile.mkstemp`` + ``os.replace`` atomicity discipline, same sid
resolution via ``core.session_dir`` / ``core.resolve_session_id``.

Ownership: this module implements BOTH halves DoE's spec asked for, because
the liveness leg the reader needs (``session_live`` / ``live_session_ids``)
lives in this repo and nowhere else — see the schema this ports,
``coordinator/schemas/tier-u-grant.schema.json`` (DoE-owned, not yet on disk
at the time this module was written; DoE also owns the registry manifest
entry). The grant is an AUTHORITY control, not a resource control: it bounds
*who may ask* to run the full suite, not how many suites run concurrently
(that is ``coordinator_core.testing.suite_mutex``'s job) or whether the
result is trustworthy (the subagent Bash-test-deny guards' job).

Fields (exactly these — no expiry, no use-counter, PM-ruled session-scoped):
    granted_by   "pm" | "ceremony"
    granted_at   ISO-8601 UTC timestamp (``core.now_iso()``)
    session_id   the GRANTING session's sid (== the directory owner, by
                 construction — the writer always writes into its OWN
                 session dir)
    ceremony     nullable string; set ONLY for implicit ceremony grants
                 (``granted_by == "ceremony"``); null/absent otherwise
    note         the verbatim PM utterance (mirrors the
                 ``execution_authorized_note`` convention) — stored exactly
                 as given, never summarized/truncated/normalized

Public functions:
    write_tier_u_grant   — writer. Validates the enum + the ceremony
                           cross-field rule; atomic create-or-overwrite of
                           the CALLING session's own grant.
    read_tier_u_grant    — raw reader. Returns the calling session's grant
                           record as persisted (or None), with NO liveness
                           check — see ``check_tier_u_grant`` for the
                           authorization-grade predicate.
    check_tier_u_grant   — THE authorization predicate: "does the CALLING
                           session hold a live Tier-U grant?" Returns
                           ``(bool, Optional[dict])`` so a caller can quote
                           the note in a denial or an audit line even when
                           the grant reads UNGRANTED.
    revoke_tier_u_grant  — hand the token back. Unlinks the CALLING (or
                           given) session's own grant file via
                           ``_grant_file`` — the single location-naming
                           seam, never a glob. Idempotent: revoking an
                           absent grant is success, not an error. No
                           expiry, no use-counter, no new record field —
                           the schema is DoE-owned and stays
                           ``schema_version`` 1. A CEREMONY passes
                           ``only_ceremony=<its own name>`` so it can only
                           take back what it minted; the unguarded form is
                           for the PM/session-owner path alone.

Spec backlink: cross-repo/inbox/2026-07-23-claude-central-em-dr088-grant-spec-and-layer2-seam.md § Ask 2
Spec backlink: DoE-claude docs/decisions/DR-088-test-breadth-ladder-tiered-invocation-authority.md § Decision, layer 5

Negative-spec (path-scoped read — NEVER glob):
    Do NOT enumerate ``.git/coordinator-sessions/*/tier-u-grant.json`` (or
    any other glob) to answer "is there a live grant." Resolve the CALLING
    session's own sid and read ONLY that one directory. A glob would let a
    concurrent sibling EM session's live grant authorize a DIFFERENT
    session's Tier-U request — a real hazard on this fleet, where several
    EM sessions routinely share one working tree. See
    ``GrantSiblingIsolationTest`` in this module's test suite for the case
    this closes: a live grant held by session B must NOT authorize session A.

Negative-spec (fail closed on authorization, open on infra — a deliberate
inversion of the house hook convention):
    The house convention (``docs/wiki/`` hook-authoring guidance) is: never
    block on infra failure — an unreadable file should not wedge the
    machine. ``check_tier_u_grant`` inverts that on purpose: an absent file,
    an unreadable file, malformed JSON, an unrecognised ``granted_by``, a
    ``session_id``/``ceremony`` cross-field mismatch, or a grant whose
    session is no longer LIVE all read UNGRANTED (``False``), not "allow
    and log." The reasoning: an unreadable *guard* (e.g. a hook that can't
    read its own config) should fail open so it doesn't block unrelated
    work, but an unreadable *grant* is not an authorization — there is
    nothing to fail open TO. Both rules are simultaneously true in this
    codebase and answer different questions; do NOT "fix" this function to
    match the hook convention.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional, Tuple

from coordinator_core.session import core, liveness

#: The only two legal values of the ``granted_by`` field (DR-088 § Decision:
#: "Authorization is granted two ways" — explicit PM, or implicit ceremony).
# Generator-provenance declaration (generator_provenance.py). write_tier_u_grant/
# revoke_tier_u_grant write/unlink only `.git/coordinator-sessions/<sid>/tier-u-
# grant.json` -- git-internal session-hub state, never a tracked repo artifact.
GENERATES = []

_VALID_GRANTED_BY = frozenset({"pm", "ceremony"})

_GRANT_FILENAME = "tier-u-grant.json"


def _grant_file(sid: str, cwd: Optional[str]) -> Optional[Path]:
    """Resolve ``<session_dir>/tier-u-grant.json`` for ``sid``, or None if
    the session dir is unresolvable (not in a git repo). Internal — every
    public function in this module routes through here so there is exactly
    ONE place that names the artifact's location."""
    sdir = core.session_dir(sid, cwd)
    if not sdir:
        return None
    return Path(sdir) / _GRANT_FILENAME


def write_tier_u_grant(
    granted_by: str,
    note: str,
    *,
    ceremony: Optional[str] = None,
    session_id: Optional[str] = None,
    cwd: Optional[str] = None,
) -> bool:
    """Create-or-overwrite the CALLING session's Tier-U grant.

    ``granted_by`` must be ``"pm"`` or ``"ceremony"`` — any other value
    raises ``ValueError`` (a caller programming error, not an infra
    failure). ``ceremony`` MUST be a non-empty string when
    ``granted_by == "ceremony"`` and MUST be absent/None when
    ``granted_by == "pm"`` — the cross-field mismatch also raises
    ``ValueError``. ``note`` is required and stored VERBATIM — this function
    never summarizes, truncates, or normalizes it (mirrors the
    ``execution_authorized_note`` convention).

    ``session_id`` defaults to the resolved calling session
    (``core.resolve_session_id(cwd)``) — pass it explicitly only for tests
    or a caller that has already resolved its own id and wants to avoid a
    second resolution. If the session id cannot be resolved (not in a git
    repo, ambiguous concurrent sessions — see
    ``core.resolve_session_id``'s Tier-4 ambiguity guard), this function
    returns False rather than raising: there is no session directory to
    write into, which is an infra condition, not a caller error.

    Atomicity: ``tempfile.mkstemp`` in the session dir + ``os.replace``
    (same discipline as ``coordinator_core.session.shape.session_shape_set``'s
    atomic write leg) — a reader never observes a partially-written grant.
    No cross-process lock is taken (unlike ``session_shape_set``): a grant
    is written once per session by that session's own ceremony/PM-ask flow,
    never concurrently merged from multiple fragments, so the shape module's
    mkdir-lock discipline does not apply here.

    Returns True on success; False on ANY infra failure (session dir
    unresolvable/uncreatable, write/replace failure).
    """
    if granted_by not in _VALID_GRANTED_BY:
        raise ValueError(
            f"granted_by must be one of {sorted(_VALID_GRANTED_BY)}, got {granted_by!r}"
        )
    if granted_by == "ceremony" and not ceremony:
        raise ValueError("ceremony is required (non-empty) when granted_by == 'ceremony'")
    if granted_by == "pm" and ceremony:
        raise ValueError("ceremony must be absent/None when granted_by == 'pm'")
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
        "ceremony": ceremony,
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


def read_tier_u_grant(
    cwd: Optional[str] = None, *, session_id: Optional[str] = None
) -> Optional[dict]:
    """Raw reader — returns the CALLING (or given) session's grant record
    exactly as persisted on disk, or None if absent/unreadable/malformed.

    Path-scoped: resolves ONE session's own directory, never globs (see the
    module's path-scoped-read negative-spec). Performs NO liveness check and
    NO enum/cross-field validation — this is the raw artifact, useful for
    inspection/audit tooling (the CLI's ``read`` subcommand) that wants to
    see what is on disk even if it would not authorize anything.
    ``check_tier_u_grant`` is the authorization-grade predicate; use that to
    decide whether to ACT on a grant.

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


def _grant_is_own_ceremony(grant_file: Path, ceremony: str) -> bool:
    """Is the persisted grant at ``grant_file`` one that ``ceremony`` minted
    and is therefore entitled to hand back?

    True only for a readable JSON object carrying ``granted_by ==
    "ceremony"`` and ``ceremony == <ceremony>``. Absent, unreadable,
    malformed, PM-minted, or another ceremony's grant all read False — the
    guard's default is "not mine, leave it alone," so an unattributable
    record is never destroyed by a ceremony that cannot prove it wrote it.

    Deliberately does NOT route through ``read_tier_u_grant``: that reader
    resolves the calling session's own sid, while this guard must inspect
    the exact file the caller already resolved (``session_id`` may have
    been passed explicitly)."""
    try:
        raw = grant_file.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        record = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(record, dict):
        return False
    return record.get("granted_by") == "ceremony" and record.get("ceremony") == ceremony


def revoke_tier_u_grant(
    cwd: Optional[str] = None,
    *,
    session_id: Optional[str] = None,
    only_ceremony: Optional[str] = None,
) -> bool:
    """Hand the token back: unlink the CALLING (or given) session's own
    grant file, and ONLY that session's — routes through ``_grant_file``,
    the single location-naming seam, never a glob over
    ``*/tier-u-grant.json`` (see the module's path-scoped-read
    negative-spec). A sibling session's live grant is untouched.

    Semantics: unlink, not tombstone (see the plan's Key decisions —
    ``read_tier_u_grant`` exists for audit tooling and the grant's own
    ``note`` already captures the ask; no second record shape is needed).

    Idempotent: revoking an absent grant returns True — there is no error
    condition for "nothing to revoke," only for an infra failure while
    trying.

    ``only_ceremony`` is the GUARDED handback a ceremony must use
    (cross-repo/inbox/2026-08-04-doe-claude-em-ceremony-grants-belong-in-
    code-not-prose.md § 1). When set, the grant is unlinked only if it was
    minted by a ceremony (``granted_by == "ceremony"``) AND that ceremony
    is this one (``ceremony == only_ceremony``). Anything else — an
    explicit ``granted_by == "pm"`` grant, or a grant minted by a DIFFERENT
    ceremony (the ``/workweek-complete`` -> ``/merging-to-main`` nesting
    case, where one grant file per session means the inner write replaced
    the outer one) — is left in place and reported as success: the guard
    did its job, and the outer ceremony's handback correctly no-ops.

    Negative-spec: a ceremony must NEVER call this with ``only_ceremony``
    omitted. The unguarded form unlinks whatever grant the session holds
    regardless of who minted it, so a bare ceremony-end revoke silently
    destroys a live PM grant that happens to be held by the same session.

    Returns True when, after this call, no grant file this call was
    entitled to remove exists for the session (removed just now, already
    absent, or — under ``only_ceremony`` — present but not this ceremony's
    to take). Returns False only on an actual infra failure (session dir
    unresolvable, or the file exists but could not be removed).
    """
    sid = session_id or core.resolve_session_id(cwd)
    if not sid:
        return False

    grant_file = _grant_file(sid, cwd)
    if grant_file is None:
        return False
    if only_ceremony is not None and not _grant_is_own_ceremony(
        grant_file, only_ceremony
    ):
        return True
    try:
        # Review: coordinator:code-reviewer — unlink(missing_ok=True) inside
        # the try/except closes the TOCTOU window between an existence check
        # and unlink(): a concurrent double-revoke racing this call must not
        # turn an already-satisfied postcondition into a false infra failure.
        grant_file.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def check_tier_u_grant(
    cwd: Optional[str] = None, *, session_id: Optional[str] = None
) -> Tuple[bool, Optional[dict]]:
    """THE authorization predicate: does the CALLING session hold a live
    Tier-U grant?

    Returns ``(granted, record)``:
      - ``granted`` — True iff ALL of: a grant file exists for the calling
        session, it parses as a JSON object, ``granted_by`` is a
        recognised value, the ``ceremony`` cross-field rule holds, the
        stored ``session_id`` matches the resolved calling sid (a
        directory-owner tamper/corruption guard), AND that sid is
        confirmed LIVE via ``liveness.session_live`` — presence and
        well-formedness alone are explicitly insufficient (see the
        module's liveness negative-spec).
      - ``record`` — the raw grant dict whenever one was found and parsed
        as an object, REGARDLESS of whether ``granted`` is True — so a
        denial or an audit line can quote the ``note`` / ``granted_by`` /
        ``granted_at`` even for an ungranted (e.g. dead-session) result.
        None only when nothing parseable was found at all.

    ``session_id`` defaults to the resolved calling session
    (``core.resolve_session_id(cwd)``) — pass it explicitly only for tests
    that want to check a specific (possibly non-current) session id without
    mutating environment/sentinel state. An unresolvable calling session id
    reads UNGRANTED with no record (there is no directory to have read).

    This function never raises.
    """
    sid = session_id or core.resolve_session_id(cwd)
    if not sid:
        return False, None

    record = read_tier_u_grant(cwd, session_id=sid)
    if record is None:
        return False, None

    granted_by = record.get("granted_by")
    if granted_by not in _VALID_GRANTED_BY:
        return False, record

    ceremony = record.get("ceremony")
    if granted_by == "ceremony" and not ceremony:
        return False, record
    if granted_by == "pm" and ceremony:
        return False, record

    if record.get("session_id") != sid:
        return False, record

    if not liveness.session_live(sid, cwd):
        return False, record

    return True, record
