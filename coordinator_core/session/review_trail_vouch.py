"""
coordinator_core.session.review_trail_vouch — per-session, PM-granted
relaxation of Case 1 (the foreign-attributed-commit hard refusal) in
``coordinator_core.ops.review_trail_write._guard_foreign_session_range``.

Problem this exists to solve: Case 1 correctly refuses (no override) any
``sha_range`` containing a commit whose OWN ``Session-Id`` trailer names a
DIFFERENT session — see ``archive/specs/2026-07/2026-07-27-review-trail-
scope-guard.md`` § C7 for why that refusal is right. But it produces an
unresolvable deadlock at a chain-terminal ``/workstream-complete``: the
coverage gate HALTs on an uncovered predecessor commit, and the trail writer
forbids the closing session from recording a review of that predecessor's
commit — even when the closing session genuinely read and reviewed it. This
module builds the deliberate, evidenced exception: a session-scoped,
liveness-gated grant that names the SPECIFIC foreign SHA(s) a PM has
authorized this session to vouch for, mirroring
``coordinator_core.session.grant`` (the Tier-U full-suite grant) shape for
shape and audit precedent — see that module's docstring for the sibling
mechanism this one is deliberately NOT reinventing a divergent idiom for.

Two-part mechanism (write-time authorization + permanent read-side effect):

  1. **Grant (this module, session-scoped, liveness-gated, PM-only).** A PM
     utterance writes ``.git/coordinator-sessions/<sid>/review-trail-
     vouch.json`` for the CALLING session, naming the exact foreign SHA(s)
     vouched for and the verbatim note. ``check_review_trail_vouch`` is the
     authorization predicate ``_guard_foreign_session_range`` consults: it
     returns only the subset of a requested SHA set that a LIVE grant for
     THIS session actually names — never a blanket "this session may vouch
     for anything" (design constraint: a wide grant would recreate the
     hazard C7 closed).
  2. **Waiver (persisted by the writer, ``coordinator_core.ops.
     review_trail_write._record_pm_vouch_waivers``, under
     ``state/review-trail/pm-vouches/<sha>.json``).** Once a write proceeds
     under a live grant, the specific vouched SHA(s) are recorded as a
     PERMANENT, idempotent waiver — a durable historical fact, not a
     liveness-gated one. This is deliberate: the record being written now
     may be read by the coverage gate in a FUTURE session, long after the
     granting/writing session has ended and its liveness has expired. If the
     read side re-checked THIS module's liveness-gated grant at read time,
     the relaxation would evaporate the moment the writing session closed —
     defeating its own purpose (a `/workstream-complete` chain-terminal
     session that closes immediately after writing the record). The waiver
     file is the read side's actual authority
     (``coordinator_core.coverage._narrow_foreign_session_scope`` subtracts
     it from the foreign-SHA set it strips); this module's grant is
     write-time-only authorization to CREATE that waiver, never itself
     consulted again after the write.

This module provides no cryptographic or runtime check that the CLI's
``grant pm`` call was actually driven by a human PM utterance — the same
posture as ``session.grant``'s Tier-U precedent (Review: code-reviewer —
Finding 1: the earlier "self-grant is structurally impossible" framing
asserted a guarantee the code doesn't enforce). The guard against silent
self-grant is procedural and social: the CLI's naming
(``grant pm "<verbatim utterance>"``) and its lone entrypoint make an
agent-initiated grant a deliberate, visible, auditable act (the verbatim
note is on the record) rather than a silent one — not a structural
impossibility. See the negative-spec below for why a bare env var or bare
file-touch was rejected as the mechanism instead.

Fields (exactly these — no expiry, no use-counter, mirrors grant.py):
    granted_by   "pm" (the only legal value — there is no "ceremony" implicit
                 variant for this grant; every review-trail vouch is an
                 explicit, evidenced PM ask, never an implicit ceremony
                 default)
    granted_at   ISO-8601 UTC timestamp (``core.now_iso()``)
    session_id   the GRANTING session's sid (== the directory owner, by
                 construction)
    note         the verbatim PM utterance (mirrors ``grant.py``'s
                 ``note``/``execution_authorized_note`` convention) — stored
                 exactly as given, never summarized/truncated/normalized
    shas         list[str] — the SPECIFIC foreign SHA(s) this grant vouches
                 for, stored as FULL 40-char hex SHAs ONLY (Review:
                 code-reviewer — Finding 3). The PM utterance may name a
                 full OR abbreviated hex token — the common human form, e.g.
                 "vouch for c126235" — and ``write_review_trail_vouch``
                 expands each token to its full commit SHA via
                 ``git rev-parse <token>^{commit}`` at GRANT time, once,
                 raising ``ValueError`` (never silently storing/authorizing
                 nothing) on an unresolvable or ambiguous token. Expanding at
                 the input boundary, rather than at check time, keeps
                 ``check_review_trail_vouch``'s comparison a plain full-SHA
                 set intersection with no prefix-matching logic anywhere — an
                 abbreviated stored token could otherwise never intersect the
                 always-full-SHA ``requested`` side (git's ``%H``), silently
                 authorizing nothing. NEVER a wildcard/blanket value —
                 narrowness is the point (design constraint: a grant naming
                 SHA X must not authorize a range containing foreign SHA Y).

Public functions:
    write_review_trail_vouch — writer. Validates the enum + non-empty
                                shas/note; expands each sha token to its full
                                40-char commit SHA (see `shas` field above);
                                atomic create-or-overwrite of the CALLING
                                session's own grant.
    read_review_trail_vouch  — raw reader. Returns the calling session's
                                grant record as persisted (or None), with NO
                                liveness check.
    check_review_trail_vouch — THE authorization predicate: "of the SHAs the
                                caller is asking about, which does the
                                CALLING session hold a LIVE PM grant for?"
                                Returns (frozenset(vouched_subset), record).

Spec backlink: archive/specs/2026-07/2026-07-27-review-trail-scope-guard.md § C7
    (the refusal this module builds a narrow, evidenced exception to)
Spec backlink: coordinator_core/session/grant.py (the Tier-U grant this
    module's shape is deliberately mirrored from, per PM design constraint
    "mirror the existing precedent rather than inventing one")

Negative-spec (why not an env var, why not a bare file):
    An env var is self-grantable by any subagent — REJECTED. A file an agent
    can simply create (with no gate on who created it) is equally
    self-grantable — REJECTED. This module's grant file lives under the
    SAME session-scoped, liveness-gated directory convention as
    ``session.grant``, and the only sanctioned writer is a human-invoked CLI
    call (``review-trail-vouch-cli grant pm ...``) — the write path itself
    performs no cryptographic PM-identity check (neither does ``grant.py``'s
    Tier-U precedent), but the naming/procedure makes an accidental or
    automated self-grant a deliberate, visible act rather than a silent one.

Negative-spec (path-scoped read — NEVER glob):
    Do NOT enumerate ``.git/coordinator-sessions/*/review-trail-vouch.json``
    to answer "is there a live grant for SHA X." Resolve the CALLING
    session's own sid and read ONLY that one directory — same reasoning as
    ``session.grant``'s identical negative-spec (a concurrent sibling
    session's grant must never authorize a different session's write).

Negative-spec (grant liveness is write-time only, not read-time):
    ``check_review_trail_vouch`` (this module) is consulted ONLY from
    ``_guard_foreign_session_range`` at WRITE time. It is never consulted
    from the coverage read side — the read side's authority is the
    persisted waiver file (see part 2 above), which by design outlives the
    granting session's liveness window. Do NOT thread this module's
    liveness check into the read side "for consistency" — doing so would
    silently re-close the exact deadlock this mechanism exists to open (a
    chain-terminal session's grant going dead the moment it closes, which
    is exactly when the coverage gate needs to read the record it wrote).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import FrozenSet, Iterable, List, Optional, Tuple

from coordinator_core.session import core, liveness

#: The only legal value of the ``granted_by`` field — deliberately narrower
#: than ``session.grant``'s two-value enum: there is no implicit-ceremony
#: variant for a review-trail vouch (see module docstring).
_VALID_GRANTED_BY = frozenset({"pm"})

_GRANT_FILENAME = "review-trail-vouch.json"


def _grant_file(sid: str, cwd: Optional[str]) -> Optional[Path]:
    """Resolve ``<session_dir>/review-trail-vouch.json`` for ``sid``, or None
    if the session dir is unresolvable (not in a git repo). Internal — every
    public function in this module routes through here so there is exactly
    ONE place that names the artifact's location (mirrors ``session.grant``'s
    ``_grant_file``)."""
    sdir = core.session_dir(sid, cwd)
    if not sdir:
        return None
    return Path(sdir) / _GRANT_FILENAME


def _expand_sha(token: str, cwd: Optional[str]) -> Optional[str]:
    """Resolve ``token`` (a full OR abbreviated hex commit reference, as a
    PM utterance names it) to its full 40-char commit SHA via
    ``git rev-parse --verify --quiet <token>^{commit}``, or ``None`` if the
    token is unresolvable OR ambiguous.

    Review: code-reviewer — Finding 3 (P2): ``granted_shas`` and the
    ``requested`` side of ``check_review_trail_vouch``'s set intersection
    were never comparable when a PM named an abbreviated token, because the
    ``requested`` side always comes from git's ``%H`` (full 40-char, see
    ``coordinator_core.session_attribution.trailer_foreign_shas``) — an
    abbreviated stored token could never intersect it, silently authorizing
    nothing. This resolves the mismatch at grant time (the input boundary),
    once, so the stored record and every subsequent comparison stay
    full-SHA-only; ``check_review_trail_vouch`` keeps a bare set
    intersection with NO prefix-matching logic (deliberately — prefix
    matching at CHECK time is where an abbreviated-token collision would be
    dangerous; ``git rev-parse`` resolving a prefix to exactly one commit,
    or failing loudly on an ambiguous one, is the safe place to do this
    resolution).

    ``^{commit}`` peels the token to a commit object specifically — a token
    that resolves to a non-commit object (blob/tree/tag not pointing at a
    commit) is treated as unresolvable, same as an unknown token.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{token}^{{commit}}"],
            capture_output=True,
            text=True,
            cwd=cwd,
            **core._NO_CONSOLE,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return out or None


def write_review_trail_vouch(
    shas: Iterable[str],
    note: str,
    *,
    granted_by: str = "pm",
    session_id: Optional[str] = None,
    cwd: Optional[str] = None,
) -> bool:
    """Create-or-overwrite the CALLING session's review-trail-vouch grant.

    ``granted_by`` must be ``"pm"`` — any other value raises ``ValueError``
    (a caller programming error, not an infra failure; kept as a parameter
    rather than hardcoded so a future, deliberately-added grant class does
    not require an incompatible function signature change).

    ``shas`` must be a non-empty iterable of non-empty strings — the SPECIFIC
    foreign SHA(s) this grant authorizes THIS session to vouch for. Each
    token may be a full OR abbreviated hex commit reference, as a PM
    utterance would naturally name one (Review: code-reviewer — Finding 3);
    every token is resolved to its FULL 40-char commit SHA via
    ``git rev-parse <token>^{commit}`` (``_expand_sha``, above) BEFORE
    storage — the stored record's ``shas`` list is always full-SHA-only, and
    ``ValueError`` is raised (never a silent store) if any token is
    unresolvable or ambiguous. Stored deduplicated (post-expansion) and
    sorted. ``note`` is required and stored VERBATIM (mirrors
    ``session.grant``'s ``note`` convention — never summarized or truncated).

    ``session_id`` defaults to the resolved calling session
    (``core.resolve_session_id(cwd)``) — pass it explicitly only for tests or
    a caller that has already resolved its own id.

    Atomicity: ``tempfile.mkstemp`` in the session dir + ``os.replace`` (same
    discipline as ``session.grant.write_tier_u_grant``) — a reader never
    observes a partially-written grant.

    Returns True on success; False on ANY infra failure (session dir
    unresolvable/uncreatable, write/replace failure) — mirrors
    ``write_tier_u_grant``'s contract exactly. Raises ``ValueError`` for a
    caller-programming-error-shaped input, including an unresolvable/
    ambiguous sha token — this is deliberately NOT folded into the False/
    infra-failure return, mirroring how the enum/note/shas-presence checks
    below already raise rather than return False.
    """
    if granted_by not in _VALID_GRANTED_BY:
        raise ValueError(
            f"granted_by must be one of {sorted(_VALID_GRANTED_BY)}, got {granted_by!r}"
        )
    if not note:
        raise ValueError("note is required (the verbatim PM utterance)")
    raw_shas: List[str] = sorted({s.strip() for s in shas if s and s.strip()})
    if not raw_shas:
        raise ValueError(
            "shas is required (non-empty) — a review-trail vouch must name the "
            "specific foreign SHA(s) it authorizes; a blanket/empty grant is not "
            "supported by design"
        )

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

    # Expand each sha token to its full 40-char commit SHA AFTER the
    # infra/session-resolution checks above have already succeeded — an
    # unresolvable ``cwd``/session is an infra failure (returns False, same
    # as before this expansion was added), never confused with a
    # resolvable-repo-but-bad-sha-token failure (raises ValueError, see
    # ``_expand_sha`` and the docstring above).
    expanded_shas: List[str] = []
    for token in raw_shas:
        full = _expand_sha(token, cwd)
        if full is None:
            raise ValueError(
                f"shas: {token!r} did not resolve to a unique commit via "
                f"`git rev-parse {token!r}^{{commit}}` (unknown or ambiguous "
                "hex prefix) — a review-trail vouch must name a resolvable, "
                "unambiguous commit; an unresolvable/ambiguous token is "
                "never stored as-is and never silently authorizes anything"
            )
        expanded_shas.append(full)
    sha_list: List[str] = sorted(set(expanded_shas))

    record = {
        "schema_version": 1,
        "session_id": sid,
        "granted_by": granted_by,
        "granted_at": core.now_iso(),
        "note": note,
        "shas": sha_list,
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
            pass
        return False
    return True


def read_review_trail_vouch(
    cwd: Optional[str] = None, *, session_id: Optional[str] = None
) -> Optional[dict]:
    """Raw reader — returns the CALLING (or given) session's grant record
    exactly as persisted on disk, or None if absent/unreadable/malformed.
    Performs NO liveness check and NO enum validation — mirrors
    ``session.grant.read_tier_u_grant``; use ``check_review_trail_vouch`` to
    decide whether to ACT on a grant."""
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


def check_review_trail_vouch(
    shas: Iterable[str],
    cwd: Optional[str] = None,
    *,
    session_id: Optional[str] = None,
) -> Tuple[FrozenSet[str], Optional[dict]]:
    """THE authorization predicate: of the SHAs in ``shas``, which does the
    CALLING session hold a LIVE, well-formed PM grant for?

    Returns ``(vouched, record)``:
      - ``vouched`` — the subset of ``shas`` (as a frozenset) that ALL of:
        a grant file exists for the calling session, parses as a JSON
        object, ``granted_by == "pm"``, the stored ``session_id`` matches
        the resolved calling sid (directory-owner tamper/corruption guard,
        mirrors ``session.grant``), that sid is confirmed LIVE via
        ``liveness.session_live``, AND the SHA is named in the grant's
        ``shas`` list. A grant naming SHA X never authorizes a different SHA
        Y not in that list (design constraint: narrow-by-SHA, not
        blanket-by-session).
      - ``record`` — the raw grant dict whenever one was found and parsed as
        an object, regardless of whether any SHA was authorized — so a
        caller can quote ``note``/``granted_at`` in a log/audit line even
        for a partially- or fully-unauthorized request.

    ``shas`` with a dead/absent/malformed grant, or a grant belonging to a
    DIFFERENT (non-live, or non-matching) session, returns an EMPTY
    ``vouched`` set — mirrors ``check_tier_u_grant``'s fail-closed-on-
    authorization posture exactly (an unreadable *grant* is not an
    authorization; there is nothing to fail open to).

    This function never raises.
    """
    requested = frozenset(s.strip() for s in shas if s and s.strip())
    sid = session_id or core.resolve_session_id(cwd)
    if not sid or not requested:
        return frozenset(), None

    record = read_review_trail_vouch(cwd, session_id=sid)
    if record is None:
        return frozenset(), None

    granted_by = record.get("granted_by")
    if granted_by not in _VALID_GRANTED_BY:
        return frozenset(), record

    if record.get("session_id") != sid:
        return frozenset(), record

    if not liveness.session_live(sid, cwd):
        return frozenset(), record

    granted_shas = frozenset(record.get("shas") or [])
    return (granted_shas & requested), record
