"""
coordinator_core.ops.handoff_correct_body — JSON-RPC "handoff.correct_body" operation.

Purpose: an authorized, bounded body-correction door for a `status: claimed` (or
legacy `status: consumed`) `state/handoffs/*.md` file. `block_consumed_handoff_edit`
denies a NON-HOLDER's `Write`/`Edit` against such a file's body — this op applies
the correction as a guard-safe node-write instead, so the guard never sees it and is
never asked to allow anything.

POSSESSION FIRST, AUTHORSHIP SECOND — read the gate below, not this line alone.
The handler's first arm is POSSESSION: a caller whose session id matches the
resolved claim holder (ledger-first via `resolve_claim_state`) is authorized with
`basis = "holder"`, whatever `authoring_session` says. The authorship arm is the
FALLBACK, reached only when no holder resolves or the holder is someone else; there
the D2(viii) `authoring_session` equality gate applies. A claim holder is therefore
eligible even on a baton another session authored — the label the guard prints
(`possession-gated`, pinned by `test_correct_body_labeled_possession_not_authorship`)
is the accurate one. This paragraph exists because the pre-possession framing ("this
op exists solely for an author fixing a body they wrote") outlived the widening and
sent at least one reader — example-retrieval-repo-ue-addon-em, 2026-08-20 memo — to conclude
they were ineligible when the holder arm covered them. Do not restore an
authorship-first summary above the possession arm.

The original narrow case the guard's deny text conceded is legitimate — an author
fixing a factual error in a body they wrote, after a peer session claimed the baton
before the fix landed — is now one of two authorized bases, not the only one.

Authority: docs/decisions/DR-247-bounded-body-write-carveout-for-claimed-handoff.md
(D1/D2, all eight bounds). This op is the ONLY `handoff.*` verb DR-212 D2(iii)/D4's
frontmatter-only carve-out does not bind — `handoff.transition`/`handoff.stamp`/
`handoff.normalize` remain frontmatter-only exactly as DR-212 ratified.
Spec: docs/plans/2026-07-31-claimed-baton-body-correction-route.md, chunk C2.

ROADMAP-WORKFLOW-SHAPED authoring_session (DR-247 §3a, added by chunk C8,
docs/plans/2026-08-05-roadmap-graph-enforcement-gap.md): a roadmap baton's
`authoring_session` is a path (`state/roadmap/<id>/...`), not a session id, by
construction — no live session id can ever satisfy the D2(viii) equality gate
against it. `_is_roadmap_workflow_authoring_session` below recognizes that one
shape and skips both the sentinel/malformed check and the equality check for
it ONLY; every other authoring_session shape (UUID, sentinel, free text, any
other path) keeps both gates byte-for-byte. See DR-264's "General principle"
section for the authorization reasoning this substitution rests on.

Mechanism: single `old_string` -> `new_string` textual replacement below the
frontmatter delimiter, applied via `locked_rmw` (flock-protected read-mutate-write)
under `asyncio.to_thread` — the SAME seam `coordinator_core.ops.handoff_stamp`'s
`_handler` uses (DR-212 C1/D3), reused here rather than re-derived. `contained_path`
and `main_worktree_root` are the same reused primitives `handoff_stamp` uses for its
own `state/handoffs/`-only containment and worktree derivation.

THE AUTHORSHIP GATE (the fallback arm — see POSSESSION FIRST above) IS
ANTI-ACCIDENT, NOT ANTI-ADVERSARY (DR-247 § 3 — record this
honestly, it is silently breakable otherwise). `authoring_session` resolution is a
pure caller-controlled environment-variable lookup (`COORDINATOR_SESSION_ID` >
`CLAUDE_SESSION_ID` > `CLAUDE_CODE_SESSION_ID`), performed inside a subprocess the
CALLER ITSELF spawns. A caller that deliberately sets any of these three variables
before invoking this op passes the gate unconditionally — there is no
socket-authoritative or otherwise caller-independent source of session identity on
this invoke surface, so this gate is categorically WEAKER than the guard's own
recovery override (`COORDINATOR_OVERRIDE_CONSUMED_HANDOFF_EDIT`, unreachable from
inside a live session by construction and pinned unreachable by
`coordinator_core/bash_guards/tests/test_override_unreachability_boundary.py`). The
ACTUAL control this op relies on is not the gate — it is the stamped, auditable
correction note every applied correction writes (AC5, `_build_correction_note` /
`_append_correction_note` below): it names the resolved calling session id AND
which of the three environment variables resolved it. A spoofed invocation is not
prevented; it is made VISIBLE ON DISK rather than closed off. No future reader
should cite the authorship gate as a
security boundary.

DISPATCHED-SUBAGENT SESSION-ID RESOLUTION (load-bearing, silently breakable by any
future precedence-chain edit if undocumented): empirically, inside a dispatched
subagent, `CLAUDE_CODE_SESSION_ID` resolves to the LEAD session's id, not the
subagent's own — verified with `CLAUDE_CODE_CHILD_SESSION=1` set and the first two
precedence-chain vars (`COORDINATOR_SESSION_ID`, `CLAUDE_SESSION_ID`) empty. This is
why a dispatched review-integrator (not just the EM by hand) can pass this gate —
consistent with `block_em_hand_edit_pending_review_integration`'s premise that the
review-integrator, not the EM by hand, applies review findings.

The authorship-equality predicate mirrors the ONE already implemented in
`coordinator_core.baton_assemble._adopt_prior_attempt_scaffold_path` ("does a
candidate's own `authoring_session` field match the calling session id") — reused
here via `coordinator_core.baton_assemble._resolve_current_session_id` (the exact
same env-lookup chain) for the session-id VALUE used in the equality gate, rather
than re-derived. That shared function returns only the resolved value, not which of
the three variables produced it, so this module keeps its own small
`_resolve_session_id_with_source` (returning the source variable name too) SOLELY to
compose AC5's stamp text — it walks
`coordinator_core.session.core.SESSION_ENV_PRECEDENCE`, the single canonical
definition of the chain itself (no longer a local copy; see that constant's own
comment for why a guard/op divergence here is break-class).

Self-registration: importing this module fires ``@register_op("handoff.correct_body",
_handler)`` as a side-effect. Added to `coordinator_core/ops/__init__.py`'s eager
import list to trigger registration at `start_server()` time. UNLIKE
`handoff_stamp._repair_archived_shipped_in_handler` (deliberately NOT
`@register_op`-registered — called in-process only), this op IS registered: it also
carries an `OP_CLASSIFICATION` entry (`coordinator_core/authz/classification.py`)
and an `_OP_KEY_SCOPE` entry (`coordinator_core/op_scopes.py`, keyed
`"common_dir"`) — without both, `classify()` fail-closes to DENY (`coordinator_core/
authz/classification.py::classify`) or dispatch resolves `repo_root=None`,
shipping a second unreachable escape hatch, the defect this plan exists to close.

Exit-code contract (simple dict envelope — NOT the fleet {mode,dry_run,candidates}
shape, matching `handoff_stamp`'s own contract):
    exit_code 0, applied True  — replacement applied, correction note stamped
    exit_code 1, applied False — refused; a DISTINCT `error` string per precondition

Negative-spec (hard-won, DR-247 D2 + plan C2 anti-scope):
    - Does NOT git-commit. Pure filesystem mutation only — the caller commits.
    - Does NOT resolve its own replacement text — `old_string`/`new_string` are
      caller-supplied verbatim; this op never derives or guesses either.
    - Touches exactly one target file per call, resolved under EITHER
      `state/handoffs/*.md` (live) OR `archive/handoffs/**/*.md` (archived,
      following a concurrent `fleet.archive_handoffs` sweep — DR-247 D2(ii)
      as amended, mirroring `workstream_complete._resolve_handoff_path_str`'s
      own live-first resolution rule rather than re-deriving it). Never any
      OTHER path, and never any OTHER `handoff.*` op's archive access —
      `handoff_stamp`'s own narrow repair-handler `allowed_roots` stay
      untouched by this addition (see that module's updated comment). An
      archived target whose `deployment_state` is `shipped`, `continued`, or
      `closed` is refused outright (AC14) — the archive-follow widens WHERE
      this op looks, never WHAT it is allowed to touch once it finds a file.
    - Does NOT accept a whole-file content payload — the mutation is exactly one
      `old_string` -> `new_string` textual replacement below the frontmatter
      delimiter (DR-247 D2(iii)), never a section replace or free-form rewrite.
    - Does NOT grow into a general handoff-body editor — no section adds, no
      restructures, no status changes. Those stay `/handoff`'s, `ship-handoff`'s,
      and `handoff.stamp`'s jobs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.claim_state import resolve_claim_state
from coordinator_core.frontmatter.primitives import (
    read_fm_field_unquoted,
    split_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.ceremony.renderers import _ROADMAP_ID_ALLOWLIST_RE
from coordinator_core.ops.fleet._common import handoff_archive_dest, main_worktree_root
from coordinator_core.ops.session_context import resolve_current_session_id
from coordinator_core.session.core import SESSION_ENV_PRECEDENCE as _SESSION_ENV_PRECEDENCE

_LOG = logging.getLogger(__name__)

# UUID-shape guard — mirrors coordinator_core/archive_stamp.py's
# `_SESSION_ID_UUID_RE` and coordinator_core/coverage.py's `_UUID_RE` (both
# validate a Session-Id trailer's shape). Duplicated per this repo's own stated
# convention (see archive_stamp.py's comment above its own copy) rather than
# imported, since coverage.py/archive_stamp.py are off-limits to couple this
# op to for this fix.
_SESSION_ID_UUID_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]+[0-9a-fA-F]$")

# Sentinel denylist (AC4) — values observed in the live corpus that are NOT a
# genuine session id: a scaffold placeholder or the CLI's own explicit
# "nothing resolved" fallback string. Absence, free-text, and path-shaped
# values are already excluded by the UUID-shape check above; these three are
# the additional named sentinels that WOULD otherwise pass a bare non-empty
# check.
_SESSION_ID_SENTINELS = frozenset({"PLACEHOLDER", "em-unknown"})

# DR-247 §3a / DR-264 "General principle" (chunk C8,
# docs/plans/2026-08-05-roadmap-graph-enforcement-gap.md): a roadmap baton's
# `authoring_session` is path-shaped (`state/roadmap/<id>/...`) BY
# CONSTRUCTION, from two independent emitters — no live session id can ever
# equal it, so the UUID-shape / equality gates below would refuse every
# caller unconditionally rather than narrow anything. This predicate
# recognizes that one workflow-scoped shape; every other authoring_session
# value (UUID-shaped, sentinel, arbitrary free text, any other path, or a
# `state/roadmap/`-prefixed value whose `<id>` segment is not itself a
# genuine roadmap id) keeps the existing sentinel/malformed and equality
# gates byte-for-byte.
#
# Finding 1 (code-reviewer, 2026-08-05): the id segment is validated against
# `_ROADMAP_ID_ALLOWLIST_RE` — the SAME canonical regex
# `coordinator_core.ops.ceremony.renderers.refresh_roadmap_callout` uses to
# validate this identical `<roadmap_id>` segment, imported rather than
# re-derived so the two "is this a genuine roadmap id" checks cannot drift
# apart. An earlier, looser `re.match(r"^state/roadmap/[^/]+/")` matched any
# non-slash run before the second `/` — including
# `state/roadmap/../../etc/passwd` and `state/roadmap/ /x` — which skipped
# BOTH the sentinel/malformed and equality gates for a value that was
# neither a live session id nor a genuine roadmap-baton path. Shape-only
# trust (no check that `state/roadmap/<id>/` actually exists on disk) is
# retained deliberately — see DR-247 §3a.
_ROADMAP_AUTHORING_SESSION_PREFIX = "state/roadmap/"


def _is_roadmap_workflow_authoring_session(value: str) -> bool:
    """True when `value` is a roadmap-workflow-shaped `authoring_session`
    (`state/roadmap/<id>/...`, `<id>` itself a well-formed roadmap id) — the
    one path shape DR-247 §3a admits in place of the session-id-equality
    gate (DR-264's "General principle", site 2). Never matches a live
    session id (UUID- or otherwise-shaped) or a traversal/malformed-id
    near-miss, so this predicate only ever WIDENS which callers are
    authorized for this one artifact shape; it never narrows the existing
    gate."""
    if not value.startswith(_ROADMAP_AUTHORING_SESSION_PREFIX):
        return False
    rest = value[len(_ROADMAP_AUTHORING_SESSION_PREFIX):]
    # Reject ".." anywhere past the prefix, not only within the `<id>`
    # segment itself — an embedded-traversal tail such as
    # `state/roadmap/<id>/../../x` must be refused even when `<id>` alone
    # is well-formed (Finding 2, code-reviewer 2026-08-05).
    if ".." in rest:
        return False
    roadmap_id, sep, _tail = rest.partition("/")
    if not sep or not roadmap_id:
        return False
    return bool(_ROADMAP_ID_ALLOWLIST_RE.fullmatch(roadmap_id))

# Same three-variable precedence chain `_resolve_current_session_id` (baton_assemble)
# and `coordinator/bin/coordinator-doc-new.py`'s `_resolve_session_id` both use — see
# module docstring for why this op still needs its own source-variable-name lookup
# (`_resolve_session_id_with_source`, below), which the shared function does not
# expose. The chain ITSELF is no longer a local copy — imported from
# `coordinator_core.session.core` as `_SESSION_ENV_PRECEDENCE` (see that module's
# `SESSION_ENV_PRECEDENCE` comment for why a guard/op divergence here is
# break-class, not a style nit).

#: AC5 source label for the one case where no precedence-chain env var names the
#: resolved identity: a warm-served dispatch, where the caller's id arrives as a
#: per-request `ContextVar` binding rather than in this process's environment.
#: A label, never a variable to read — the stamp must not claim an env var
#: resolved the id when none did.
_WARM_REQUEST_SOURCE = "warm-request-identity"

# AC3 — the load-bearing discriminator between "correction" and "progress-journal
# append" (PM-confirmed 2026-07-31; a factual correction is characteristically a
# few hundred bytes).
_NET_GROWTH_CAP = 512

# Finding 1 (code-reviewer, 2026-07-31): the net-growth cap above bounds how much
# a correction ADDS, but nothing previously bounded what old_string may REPLACE —
# a whole-body old_string with a same-length new_string satisfied every existing
# precondition (occurrence-count, heading, delimiter, growth cap) while being a
# full free-form body rewrite, exactly what DR-247 D2(iii) forbids. These two
# constants bound old_string's own size/share of the body so the replacement
# target is provably a localized snippet, not the whole document. Residual NOT
# closed by these bounds: repeated small in-bound corrections can still grow a
# body incrementally across calls — the per-call cap bounds one call, not the
# aggregate. The control against that is AC5's stamp (every call appends a
# dated, greppable correction note, so N appends leave N visible markers), not
# a precondition — deliberately no rate limiter or invocation counter here.
_MAX_OLD_STRING_LEN = 1024
_MAX_OLD_STRING_BODY_RATIO = 0.5

_HEADING_LINE_RE = re.compile(r"^#{1,6} ", re.MULTILINE)
_DELIM_LINE_RE = re.compile(r"^---[ \t]*$", re.MULTILINE)

# AC5 — one fixed, machine-greppable marker prefix. The full line also carries a
# UTC timestamp, the resolved calling session id, and which env var resolved it;
# only this prefix is asserted as the forgery-guard / grep target, since the
# timestamp/session-id suffix varies per correction.
_CORRECTION_MARKER_PREFIX = "<!-- handoff-correct-body-correction:"
_CORRECTION_SECTION_HEADING = "## Correction Log (handoff.correct_body)"

# Finding 1 (security-audit, 2026-07-31): the marker-forgery guard below was a
# bare `_CORRECTION_MARKER_PREFIX in new_string` literal-substring test — a
# single U+200B ZERO WIDTH SPACE inserted mid-marker renders visually
# identical to a genuine stamp, defeats the substring test, AND is invisible
# to a grep for the real prefix, defeating both halves of AC5's forgery
# guard at once. These are the explicit zero-width characters checked in
# addition to the general Unicode category `Cf` (format) sweep below —
# named individually because they are the most common invisible-character
# forgery vectors, not because the `Cf` sweep alone would miss them (it
# would not; both checks are kept for defense-in-depth clarity).
_ZERO_WIDTH_CHARS = frozenset({
    "​",  # ZERO WIDTH SPACE
    "‌",  # ZERO WIDTH NON-JOINER
    "‍",  # ZERO WIDTH JOINER
    "⁠",  # WORD JOINER
    "﻿",  # ZERO WIDTH NO-BREAK SPACE / BOM
})


def _defang_for_marker_detection(s: str) -> str:
    """Return a DETECTION-ONLY copy of `s` with Unicode format characters
    (category `Cf`) and the named zero-width set removed, then NFKC-
    normalized — used SOLELY to widen the marker-forgery check's view so an
    invisible-character-disguised marker cannot slip past a bare substring
    test (Finding 1, security-audit 2026-07-31).

    CRITICAL: this return value is NEVER written to disk. The text actually
    persisted is always the caller's original, untouched `new_string` —
    silently rewriting an author's prose to "defang" it would be its own
    integrity defect, distinct from (and no better than) the forgery this
    function exists to catch.
    """
    stripped = "".join(
        ch for ch in s
        if unicodedata.category(ch) != "Cf" and ch not in _ZERO_WIDTH_CHARS
    )
    return unicodedata.normalize("NFKC", stripped)


def _contains_invisible_unicode(s: str) -> bool:
    """True if `s` contains any Unicode format character (category `Cf`) or a
    named zero-width character — used for a broad, marker-independent
    refusal (Finding 1, security-audit 2026-07-31): an invisible-character
    payload has no legitimate place in a frozen audit record's body at all,
    not only when it happens to be hiding a forged marker."""
    return any(
        unicodedata.category(ch) == "Cf" or ch in _ZERO_WIDTH_CHARS
        for ch in s
    )


# ---------------------------------------------------------------------------
# Byte-preserving frontmatter split (Finding 2, security-audit 2026-07-31)
# ---------------------------------------------------------------------------

# Mirrors frontmatter/primitives.py's own preamble/delimiter grammar exactly,
# but WITHOUT that module's CRLF->LF normalize-on-entry step (DR-148) — see
# `_split_frontmatter_raw_bytes` below for why a second, deliberately
# non-normalizing copy is kept here rather than reusing `split_frontmatter`.
_RAW_PREAMBLE_RE = re.compile(
    r'^(?:[ \t]*\r?\n|[ \t]*<!--[\s\S]*?-->[ \t]*\r?\n?)+'
)
_RAW_OPEN_RE = re.compile(r'^---[ \t]*\r?\n')
_RAW_CLOSE_RE = re.compile(r'^---[ \t]*\r?$', re.MULTILINE)


def _split_frontmatter_raw_bytes(raw_text: str) -> "tuple[str, str] | None":
    """Return ``(frontmatter_full_raw, body_raw)`` computed directly against
    ``raw_text`` — the EXACT bytes read from disk — without
    ``split_frontmatter``'s CRLF->LF normalize-on-entry step.

    ``frontmatter_full_raw`` is everything from the start of the file
    through the closing ``---`` line, byte-for-byte as authored (preamble,
    opening delimiter, frontmatter body, closing delimiter — including any
    ``\\r``). ``body_raw`` is everything after, its own leading newline
    included (mirroring ``FrontmatterSplit.body_with_leading_newline``'s
    convention).

    Finding 2 (security-audit, 2026-07-31): ``split_frontmatter`` normalizes
    CRLF->LF on entry (DR-148), so a "byte-identical frontmatter" assertion
    built from two of its snapshots compares two EQUALLY pre-normalized
    copies and can never detect that the on-disk frontmatter bytes actually
    changed — nor can it stop this op from silently downgrading a
    CRLF-authored frontmatter to LF on write, contradicting DR-247 D2(iv)'s
    literal (not "normalized-then-equal") byte-identical requirement. This
    function is the true-bytes source of truth this op needs; it
    deliberately mirrors ``split_frontmatter`` rather than reusing it, since
    reusing it would require IT to stop normalizing — a change with a much
    larger blast radius across every other frontmatter caller in the tree.

    Returns ``None`` when no valid frontmatter block is found — same failure
    shape as ``split_frontmatter``.
    """
    text = raw_text
    preamble = ""
    if not _RAW_OPEN_RE.match(text):
        m = _RAW_PREAMBLE_RE.match(text)
        if not m:
            return None
        after = text[m.end():]
        if not _RAW_OPEN_RE.match(after):
            return None
        preamble = m.group(0)
        text = after

    open_match = _RAW_OPEN_RE.match(text)
    rest = text[open_match.end():]
    close_match = _RAW_CLOSE_RE.search(rest)
    if not close_match:
        return None

    frontmatter_full_raw = preamble + text[: open_match.end()] + rest[: close_match.end()]
    body_raw = rest[close_match.end():]
    return frontmatter_full_raw, body_raw


def _err(msg: str) -> dict:
    """Return an exit_code=1 refusal dict. Mirrors handoff_stamp.py's `_err` shape."""
    _LOG.warning("handoff.correct_body: %s", msg)
    return {"exit_code": 1, "applied": False, "error": msg}


def _resolve_session_id_with_source() -> "tuple[Optional[str], Optional[str]]":
    """Return (session_id, source_label), or (None, None) if none resolved.

    Resolves through `ops.session_context.resolve_current_session_id` — the
    canonical chain — and then LABELS the result, rather than walking the env
    ladder itself. The label is derived, not the resolution: whichever
    precedence-chain var holds the resolved value names it, so the AC5 stamp
    and this op's `session_source` result key keep the exact strings they had
    (a `COORDINATOR_SESSION_ID`-resolved call still reports
    `"COORDINATOR_SESSION_ID"`).

    Negative-spec: do NOT re-derive identity by reading `os.environ` directly
    here. Under a warm-served dispatch the server's own environment names
    whoever SPAWNED it, not the session whose request is being served; the
    caller's true identity arrives as a per-request `ContextVar` binding
    (`warm.entry_seam.per_request_state` ->
    `session.core.session_identity_override`) that only the canonical resolver
    reads. A raw env read steps straight past it and authorizes this op's
    authorship gate against a stranger — then stamps that stranger into the
    AC6 correction note as the session that made the correction.

    This function and `baton_assemble._resolve_current_session_id` are
    cross-checked against each other by the caller below and a mismatch is a
    hard refusal, so the two must be migrated together — they now agree by
    delegating to the same resolver rather than by two copies of one ladder.
    """
    sid = resolve_current_session_id()
    if not sid:
        return None, None
    # The ladder walk below LABELS an identity already resolved above; it never
    # selects one. Changing it to prefer an env value over `sid` reintroduces the
    # warm-identity defect. Carried as a named exemption in
    # coordinator_core/tests/test_warm_identity_env_reads.py ::
    # LADDER_READ_EXEMPTIONS, whose reason text is the standard to re-read before
    # editing here — the ratchet cannot tell a label from a decision by itself.
    for var in _SESSION_ENV_PRECEDENCE:
        if os.environ.get(var) == sid:
            return sid, var
    return sid, _WARM_REQUEST_SOURCE


def _is_sentinel_or_malformed_session(value: str) -> bool:
    """True when `value` is a known sentinel or not UUID-shaped (AC4)."""
    if value in _SESSION_ID_SENTINELS:
        return True
    return not _SESSION_ID_UUID_RE.match(value)


def _count_matching_lines(text: str, pattern: "re.Pattern[str]") -> int:
    return sum(1 for line in text.splitlines() if pattern.match(line))


def _build_correction_note(
    session_id: str,
    source_var: str,
    old_string: str,
    new_string: str,
    basis: str,
    disagreement: bool,
    override_reason: str = "",
) -> str:
    """Compose the AC6 correction-note text to append under the canonical
    section. One marker line, dated, naming the resolved session id, which
    env var resolved it, and the ownership basis (holder/author/neither) —
    on ALL THREE arms, not only the override-applied neither case (AC6).
    When a claim-holder/self-held-claim-dir disagreement was detected
    (AC2/AC4, staff-eng re-review Finding 5), that is stamped too, and when
    the neither arm's override was used, the reason text is stamped
    verbatim (AC13). Modelled on
    `memo_transition._apply_realization_correction`'s `[correction {ts}: ...]`
    clause shape (one composition point, superseded value never dropped),
    not on commit-message tone.
    """
    ts = datetime.now(timezone.utc).isoformat()
    suffix = f", basis={basis}"
    if disagreement:
        suffix += ", claim-disagreement detected (claimed_by vs. self-held claim dir)"
    if override_reason:
        suffix += f", override_reason={override_reason!r}"
    return (
        f"{_CORRECTION_MARKER_PREFIX} {ts} by session {session_id} "
        f"(resolved via {source_var}){suffix} -->\n"
    )


def _heading_present(text: str, heading: str) -> bool:
    """True only where ``heading`` occurs as a REAL ATX heading line.

    Line-anchored, not a substring test. These markers are strings that
    reviewers, integrators, and docs quote in running prose while explaining
    the mechanism they drive, and a bare ``heading in text`` cannot tell the
    heading from a mention of it. Both failure directions are silent — see
    ``ops/append_integrator_dispositions._find_heading`` for the live 2026-08-10
    case that motivated line-anchoring every consumer of these markers.
    """
    return re.search(rf"(?m)^{re.escape(heading)}[ 	]*$", text) is not None


def _append_correction_note(body: str, note_line: str) -> str:
    """Append `note_line` under the canonical correction-log section, creating
    the section once and appending to it thereafter — never a new heading per
    correction (AC5)."""
    if _heading_present(body, _CORRECTION_SECTION_HEADING):
        # Section already exists — append the note line at the very end of
        # the body, under the existing section (no new heading).
        if not body.endswith("\n"):
            body += "\n"
        return body + note_line
    if not body.endswith("\n"):
        body += "\n"
    return body + "\n" + _CORRECTION_SECTION_HEADING + "\n\n" + note_line


@register_op("handoff.correct_body")
async def _handler(
    params: dict,
    repo_root: Optional[Path] = None,
) -> dict:
    """JSON-RPC "handoff.correct_body" handler.

    Applies a single caller-supplied `old_string` -> `new_string` replacement to
    the BODY (never the frontmatter) of a `status: claimed` (or legacy
    `status: consumed`) `state/handoffs/*.md` file, then stamps a visible
    correction note (AC5). See module docstring for the full authorship-gate
    framing and the DR-247 authority this op discharges.

    Params:
        handoff_path (str) — absolute or repo-relative path to the target
                              handoff file. Required. Must resolve under
                              `<worktree>/state/handoffs/` (live) OR
                              `<worktree>/archive/handoffs/YYYY-MM/` (archive-
                              follow, AC12) — live wins when both exist. An
                              archived target with a terminal `deployment_state`
                              (`shipped`/`continued`/`closed`) is refused (AC14).
        override_reason (str) — OPTIONAL. Only consulted when the calling
                              session is neither the claim holder
                              (`claimed_by`) nor the authoring session of the
                              target. Non-empty required to proceed in that
                              case (AC13); refused with a distinct error when
                              absent or empty. Never a
                              `COORDINATOR_OVERRIDE_*`/`COORDINATOR_ALLOW_*`
                              environment variable — an explicit op parameter,
                              stamped verbatim into the correction note on
                              use. Review: staff-eng chain-review F5 — "an
                              explicit, in-session-reachable op parameter" is
                              true on the JSON-RPC surface only; the CLI
                              veneer (`archive_stamp.cs_correct_handoff_body`)
                              builds params with only handoff_path/old_string/
                              new_string and never plumbs override_reason, so
                              a neither-arm caller on the CLI path can only
                              ever receive the refusal.
        old_string   (str) — the exact body text to replace. Required,
                              non-empty (whitespace-only is rejected), must
                              occur EXACTLY ONCE in the body below the
                              frontmatter delimiter, must not exceed 1024
                              characters, must not equal the whole body, and
                              must not exceed 50% of the body's length (Finding
                              1, code-reviewer 2026-07-31 — these three bound
                              the replacement TARGET to a localized snippet;
                              see `_MAX_OLD_STRING_LEN`/
                              `_MAX_OLD_STRING_BODY_RATIO` above).
        new_string   (str) — the replacement text. Required, must differ from
                              `old_string`, must not cross the frontmatter
                              delimiter, must not introduce a new heading line
                              (`^#{1,6} `) or a new `---` line, must not net-grow
                              the body by more than 512 UTF-8 bytes over
                              `old_string`, and must not contain this op's own
                              correction-note marker (forgery guard).

    Returns a dict with keys:
        exit_code    (int)      — 0 ok / 1 refused (see `error` for the reason).
        applied      (bool)     — True only when the replacement was written and
                                   the correction note stamped; False on every
                                   refusal path (nothing is written).
        session_id   (str|None) — the resolved calling session id used for the
                                   ownership gate and the stamp, on success.
        session_source (str|None) — which of the three precedence-chain env
                                   vars resolved `session_id`, on success.
        ownership_basis (str|None) — "holder"/"author"/"neither" (AC2/AC3a) —
                                   which arm authorized the write, on success.
        message      (str)      — human-readable outcome description (exit_code 0
                                   only).
        error        (str)      — human-readable refusal reason (exit_code 1
                                   only) — DISTINCT per precondition; see the
                                   preconditions enumerated in the module
                                   docstring / DR-247 D2 / plan C2 body.

    Exit-code contract:
        exit_code 0, applied True  — replacement applied, correction note stamped.
        exit_code 1, applied False — any precondition failed; nothing written.

    Negative-spec: see module docstring.
    """
    handoff_path_raw: str = params.get("handoff_path") or ""
    old_string_raw = params.get("old_string")
    new_string_raw = params.get("new_string")

    if not handoff_path_raw:
        return _err("missing required param: handoff_path")
    if old_string_raw is None:
        return _err("missing required param: old_string")
    # Finding 2 (code-reviewer, 2026-07-31): a non-None, non-string old_string
    # or new_string (0, False, [], {}) must refuse loudly, not silently coerce
    # to "" and fall through as a de-facto delete/malformed-replacement.
    if not isinstance(old_string_raw, str):
        return _err("old_string must be a string")
    if new_string_raw is None:
        return _err("missing required param: new_string")
    if not isinstance(new_string_raw, str):
        return _err("new_string must be a string")
    old_string: str = old_string_raw
    new_string: str = new_string_raw

    if repo_root is None:
        return _err(
            "handoff.correct_body: repo_root is required "
            "(no founding root available — handler called without socket-authoritative common_dir)"
        )

    worktree = main_worktree_root(repo_root)

    p = Path(handoff_path_raw)
    if not p.is_absolute():
        p = worktree / p

    # Precondition: target under state/handoffs/ (live) OR archive/handoffs/
    # (archive-follow, DR-247 D2(ii) as amended — AC12). `handoff_stamp.py`'s
    # own narrow repair-handler `allowed_roots` stay untouched by this
    # addition; this is a union-of-roots widening scoped to THIS op only.
    allowed_roots = [worktree / "state" / "handoffs", worktree / "archive" / "handoffs"]
    # Finding 3 (security-audit, 2026-07-31): `contained_path` only catches
    # `OSError` around its internal `Path.resolve()` calls, but an
    # embedded-NUL `handoff_path` (e.g. "foo\x00bar.md") makes `resolve()`
    # raise `ValueError` instead — that propagated straight out of this
    # handler as an unhandled crash rather than this op's documented
    # distinct-`_err(...)`-per-precondition refusal. No escape occurred
    # (fails safe either way); this restores the exit-code contract.
    try:
        p = contained_path(p, allowed_roots)
    except ValueError as exc:
        return _err(
            f"handoff_path is malformed (cannot be resolved as a filesystem "
            f"path): {exc}"
        )
    if p is None:
        return _err(
            f"handoff_path escapes state/handoffs/ and archive/handoffs/ — "
            f"the only two roots this op ever touches: {handoff_path_raw!r}"
        )

    # Archive-follow (AC12): live-first, matching
    # `workstream_complete._resolve_handoff_path_str`'s own resolution rule
    # exactly (a `candidate.is_file()` live check before the archived
    # fallback), reusing its `handoff_archive_dest` helper rather than
    # re-deriving the `YYYY-MM` placement convention. When a target exists
    # at BOTH roots (a live file and an archived twin) AND the caller supplies
    # the live path, the live one wins — never both (AC5's D2(i) note). Review:
    # staff-eng chain-review F5 — this only holds when the caller supplies the
    # live path; a caller supplying the archive path directly resolves to the
    # archived copy even when a live twin exists (an explicit request is
    # honored, not overridden by a live-twin check).
    is_archived = False
    if not p.is_file():
        archived_candidate = handoff_archive_dest(worktree, p)
        archived_candidate = contained_path(archived_candidate, allowed_roots)
        if archived_candidate is not None and archived_candidate.is_file():
            p = archived_candidate
            is_archived = True
        else:
            return _err(f"handoff not found on disk (checked state/handoffs/ and archive/handoffs/): {handoff_path_raw}")
    else:
        # `p` resolved live under state/handoffs/ (the branch above only
        # triggers when it is NOT a file there) — but a caller may also
        # supply an already-archived path directly (e.g. re-invoking against
        # a path it read back from a prior archive-follow response). Detect
        # that case from the resolved root rather than assuming "is_file()
        # true here always means live".
        # Review: staff-eng chain-review F1 — a bare `Path.relative_to` here
        # drops `contained_path`'s Windows extended-length-prefix (`\\?\`)
        # defence: `resolve()` adds that prefix to one operand and not the
        # other on a real Windows host with a long path, `relative_to` raises
        # `ValueError`, `is_archived` silently becomes False, and the AC14
        # terminal-state refusal below is skipped for an archived
        # shipped/continued/closed record. Reuse the guarded primitive
        # instead of re-deriving containment.
        is_archived = contained_path(p, [worktree / "archive" / "handoffs"]) is not None

    # Precondition: old_string non-empty / not whitespace-only. Checked explicitly
    # rather than relying on str.count('')'s accidental refusal.
    if not old_string.strip():
        return _err("old_string must be non-empty and not whitespace-only")

    # Precondition: new_string != old_string (a no-op replacement would still
    # stamp a correction note claiming something changed — corrupting the paper
    # trail; AC5).
    if new_string == old_string:
        return _err("new_string is identical to old_string — refusing a no-op correction")

    # Precondition (Finding 1, security-audit 2026-07-31): new_string must not
    # carry invisible/format Unicode characters at all — a frozen audit
    # record's body has no legitimate use for a zero-width or Unicode
    # category `Cf` character, and refusing them outright closes the
    # marker-forgery vector below at its root rather than only at the
    # marker-detection layer.
    if _contains_invisible_unicode(new_string):
        return _err(
            "new_string contains invisible or Unicode format characters "
            "(zero-width space/joiner, BOM, or category Cf) — refusing to "
            "write invisible content into a handoff body"
        )

    # Precondition: new_string must not contain the correction-note marker
    # (AC5 forgery guard — refuse before any other content check).
    if _CORRECTION_MARKER_PREFIX in new_string:
        return _err(
            "new_string contains this op's own correction-note marker — "
            "forged correction notes are refused"
        )

    # Precondition (Finding 1, security-audit 2026-07-31 — defense-in-depth):
    # also test a DEFANGED copy of new_string (Unicode format/zero-width
    # characters stripped, NFKC-normalized) against the marker prefix. The
    # invisible-character refusal above already closes the demonstrated
    # exploit; this check stays as a second, independent line of defense in
    # case a future edit narrows or removes that refusal without noticing
    # this guard depends on it. Distinct refusal reason from the raw check
    # above, so the two failure modes stay tellable apart in a log/report.
    if _CORRECTION_MARKER_PREFIX in _defang_for_marker_detection(new_string):
        return _err(
            "new_string contains this op's own correction-note marker "
            "disguised with invisible/format Unicode characters — forged "
            "correction notes are refused"
        )

    # Precondition: neither old_string nor new_string crosses/contains a
    # frontmatter-delimiter-shaped line. Both strings are searched/applied only
    # within the body (below the frontmatter delimiter) below, so this also
    # guards against a caller supplying delimiter-shaped text that could be
    # mistaken for frontmatter structure once written.
    if _DELIM_LINE_RE.search(old_string) or _DELIM_LINE_RE.search(new_string):
        return _err(
            "old_string or new_string contains a frontmatter-delimiter-shaped "
            "line ('---') — refusing a replacement that crosses the frontmatter "
            "delimiter"
        )

    # Precondition: authorship gate (DR-247 D2(viii) / AC2 / AC4). Resolved
    # BEFORE reading the target so a caller with no resolvable session id
    # never even reaches file I/O.
    session_id, session_source = _resolve_session_id_with_source()
    if not session_id:
        return _err(
            "no calling session id resolvable from COORDINATOR_SESSION_ID / "
            "CLAUDE_SESSION_ID / CLAUDE_CODE_SESSION_ID — cannot authorize a "
            "correction with no calling session identity"
        )
    # Cross-check against the shared, reused resolver (baton_assemble's own
    # authorship-predicate value) — must agree by construction (same chain);
    # a mismatch would indicate the two copies have drifted (see module
    # docstring's "if either copy's precedence chain ever changes" note).
    # Imported HERE, not at module scope — coordinator_core.baton_assemble
    # itself now imports several coordinator_core.ops.* modules at module
    # scope, and this module is one of the four that a module-scope import
    # back into baton_assemble would leave partially initialized during
    # coordinator_core.ops's eager import sweep, silently deregistering this
    # op (a stderr warning, not an exception — see
    # coordinator_core/session/claimed_plan.py's negative-spec for the same
    # hazard on the other end of this cycle). A future reader who hoists this
    # back to module scope will silently re-break op registration.
    from coordinator_core.baton_assemble import _resolve_current_session_id

    shared_session_id = _resolve_current_session_id()
    if shared_session_id != session_id:
        return _err(
            "internal: session-id precedence chain drift between "
            "handoff_correct_body and baton_assemble — refusing rather than "
            "risk gating on divergent values"
        )

    try:
        # Finding 2 (security-audit, 2026-07-31): read raw BYTES and decode,
        # never `Path.read_text()` — text-mode reads apply Python's universal-
        # newline translation (CRLF->LF) BEFORE any of this module's own code
        # ever sees the content, which would silently defeat byte-identical
        # preservation at the read step regardless of anything done below.
        text = p.read_bytes().decode("utf-8")
    except OSError as exc:
        return _err(f"cannot read handoff file: {exc}")

    split = split_frontmatter(text)
    if split is None:
        return _err(f"no valid YAML frontmatter block in: {handoff_path_raw}")

    # Finding 2 (security-audit, 2026-07-31): capture the TRUE, pre-lock
    # frontmatter bytes (untouched by split_frontmatter's CRLF->LF
    # normalize-on-entry) for the byte-identical assertion and rebuild below.
    raw_split = _split_frontmatter_raw_bytes(text)
    if raw_split is None:
        return _err(f"no valid YAML frontmatter block in: {handoff_path_raw}")
    frontmatter_full_raw, _ = raw_split

    status = read_fm_field_unquoted(split.fm_text, "status")
    if status not in ("claimed", "consumed"):
        return _err(
            f"handoff.correct_body only applies to status:claimed (or legacy "
            f"status:consumed) handoffs — {handoff_path_raw} carries status "
            f"{status!r}; an open baton needs no correction op, edit it normally"
        )

    # Terminal-state refusal on the archive arm (AC14, staff-eng re-review
    # Finding 0): once a target resolves under archive/handoffs/, a terminal
    # deployment_state means it is not an active, gate-pending record for
    # body-correction purposes — mirrors handoff_transition._close's own
    # refusal to overwrite a shipped/continued terminal, extended to closed
    # (a third party correcting a CLOSED record's body is a different
    # question from _close's own re-closing guard). Non-terminal archived
    # states (open/in_flight/ready_to_fire/awaiting_gate) stay in scope,
    # identically to a live target. Review: staff-eng chain-review F4 — this
    # is an allowlist-of-terminals, not a denylist-of-non-terminals: an
    # absent or unrecognised deployment_state is deliberately PERMITTED
    # (falls through, write proceeds), not refused. Deliberate — older
    # archived records predate the field, and refusing them by default would
    # be a real regression against that corpus shape — but it means this
    # block is fail-open on absent/unknown, not fail-closed.
    if is_archived:
        deployment_state = read_fm_field_unquoted(split.fm_text, "deployment_state")
        if deployment_state in ("shipped", "continued", "closed"):
            return _err(
                f"{handoff_path_raw} resolved under archive/handoffs/ with "
                f"deployment_state {deployment_state!r} — refusing to correct "
                "a terminal (shipped/continued/closed) archived baton's body"
            )

    # Ownership gate (DR-247 D2(viii) as amended — AC2/AC3a/AC4/AC13; widened
    # C6a: ledger-first authoritative read, docs/plans/2026-08-07-claim-
    # state-ledger-first-authoritative-read.md). The frontmatter `claimed_by`
    # mirror is branch-dependent and silently reverts on a branch switch
    # (see `coordinator_core.claim_state` module docstring's motivating
    # incident) — reading it alone means a desynced baton (ledger holds a
    # live claim, mirror empty) refuses the sanctioned body-correction door
    # AGAINST the session that is actually entitled to use it. The
    # ownership ORACLE is now `resolve_claim_state`'s `holder` (ledger wins
    # whenever it holds a live claim, mirror is the fallback when it does
    # not) — this WIDENS what the gate can see, it does not relax who it
    # lets through: a ledger claim naming a *different* session still
    # refuses exactly as before. `pickup_assemble._claim_already_self_held`
    # remains consulted ONLY for disagreement-detection below (never as a
    # competing predicate). If no holder resolves from either source
    # (legacy status:consumed batons with no ledger/mirror claim), fall
    # through to the author-arm rather than treating the caller as neither.
    claimed_by_raw = read_fm_field_unquoted(split.fm_text, "claimed_by")
    claimed_by = (claimed_by_raw or "").strip()

    claim_state = resolve_claim_state(p, repo_root=worktree)
    resolved_holder = (claim_state.holder or "").strip()

    authoring_session_raw = read_fm_field_unquoted(split.fm_text, "authoring_session")
    authoring_session = (authoring_session_raw or "").strip()

    basis: Optional[str] = None
    override_reason: str = ""

    if (
        resolved_holder
        and not _is_sentinel_or_malformed_session(resolved_holder)
        and resolved_holder == session_id
    ):
        # Holder arm (AC2) — the executing session's happy path. ALLOW
        # regardless of authoring_session; disagreement-detection below
        # still runs on this arm (staff-eng re-review Finding 5). Sourced
        # from `resolve_claim_state` (ledger-first, mirror fallback) rather
        # than the raw `claimed_by` field alone (C6a) — a live ledger claim
        # by THIS session opens the door even when the tracked mirror is
        # desynced/empty; a live ledger claim by a DIFFERENT session still
        # fails this comparison and falls through to the author/neither arms
        # exactly as a mismatched `claimed_by` always did.
        # Review: staff-eng chain-review F3 — the author arm already runs
        # `_is_sentinel_or_malformed_session`; without the same check here a
        # scaffold/sentinel holder value (`none`/`null`/`PLACEHOLDER`, live
        # corpus shapes) is a skeleton key any caller can match by setting
        # the same env literal. A sentinel holder falls through to the
        # author arm instead, the correct treatment of "this field records
        # nothing".
        basis = "holder"
    else:
        # Author-arm fallback — reached when claimed_by is absent/empty
        # (legacy consumed batons) OR present but naming someone else.
        if not authoring_session:
            return _err(
                f"{handoff_path_raw} has no authoring_session field and no "
                f"claimed_by match for calling session {session_id!r} — "
                "cannot authorize a correction against an untracked authorship"
            )
        is_roadmap_workflow = _is_roadmap_workflow_authoring_session(authoring_session)
        if is_roadmap_workflow:
            # DR-247 §3a / DR-264 site-2 (module comment above
            # `_is_roadmap_workflow_authoring_session`): a roadmap-workflow-
            # shaped authoring_session has no claim holder to compare
            # against, so it keeps resolving through this branch — it must
            # NOT fall into the "neither" arm. Authorized by the stamped
            # correction-note paper trail built below, which still records
            # the resolved calling session_id/session_source exactly as it
            # does for every other correction.
            basis = "author"
        else:
            if _is_sentinel_or_malformed_session(authoring_session):
                return _err(
                    f"{handoff_path_raw}'s authoring_session {authoring_session_raw!r} is "
                    "not a well-formed session id (sentinel or non-UUID-shaped) — "
                    "cannot authorize a correction against it"
                )
            if authoring_session == session_id:
                basis = "author"
            else:
                # Neither holder nor author — HARD REFUSE (AC3a) unless a
                # non-empty override_reason op param is supplied (AC13).
                # Two distinct error shapes: absent-entirely vs
                # present-but-empty, so C3's negative tests can tell them
                # apart. Never a COORDINATOR_OVERRIDE_*/COORDINATOR_ALLOW_*
                # environment variable — a plain op parameter only.
                if "override_reason" not in params or params.get("override_reason") is None:
                    return _err(
                        f"calling session {session_id!r} is neither the claim "
                        f"holder ({(resolved_holder or claimed_by)!r}) nor the authoring session "
                        f"({authoring_session!r}) of {handoff_path_raw} — claim "
                        "it via /pickup (a sanctioned path), or resubmit with a "
                        "non-empty override_reason param"
                    )
                override_reason_raw = params.get("override_reason")
                if not isinstance(override_reason_raw, str) or not override_reason_raw.strip():
                    return _err(
                        "override_reason must be a non-empty string when "
                        f"supplied by a caller who is neither holder nor author "
                        f"of {handoff_path_raw} (got {override_reason_raw!r})"
                    )
                override_reason = override_reason_raw.strip()
                basis = "neither"

    # Disagreement-detection (staff-eng re-review Finding 5): consulted on
    # EVERY arm that allows a write, holder included. Review: staff-eng
    # chain-review F2 — this catches only an INCOHERENT env-spoof (a
    # `claimed_by` value that disagrees with the claim dir's recorded
    # holder, e.g. a reaped or taken-over claim). It does NOT catch a spoof
    # coherent with both `claimed_by` and the claim dir: `_claim_already_
    # self_held` resolves "me" from the SAME caller-controlled env chain
    # (`COORDINATOR_SESSION_ID` > `CLAUDE_SESSION_ID` >
    # `CLAUDE_CODE_SESSION_ID`) this op already used to derive `session_id`,
    # so a coherent spoof is undetectable here by construction — see this
    # module's own "THE AUTHORSHIP GATE IS ANTI-ACCIDENT, NOT ANTI-
    # ADVERSARY" note above. `_claim_already_self_held` (never re-derived —
    # AC4) answers "does the
    # `.git/coordinator-sessions/handoff-claims/<basename>/` claim dir show
    # THIS session as the current holder?" — compared against what the
    # `claimed_by`-derived basis above implies should be true. A mismatch in
    # either direction (claim reaped/taken-over while claimed_by still names
    # this session, OR this session actually holds the claim while
    # claimed_by/authoring_session name someone else) is stamped, never
    # re-derived as a competing source of truth.
    from coordinator_core.pickup_assemble import _claim_already_self_held

    claim_self_held = _claim_already_self_held(worktree, "handoff", p.name)
    disagreement = (basis == "holder") != claim_self_held

    body = split.body_with_leading_newline
    occurrences = body.count(old_string)
    if occurrences == 0:
        return _err("old_string does not occur in the body below the frontmatter delimiter")
    if occurrences > 1:
        return _err(
            f"old_string occurs {occurrences} times in the body — refusing an "
            "ambiguous replacement (must occur exactly once)"
        )

    # Precondition (Finding 1, code-reviewer 2026-07-31): old_string absolute
    # size cap. A localized prose correction is a sentence to a paragraph; the
    # motivating incident was a wrong number. This is what catches a large
    # old_string outright, independent of the body it's drawn from.
    if len(old_string) > _MAX_OLD_STRING_LEN:
        return _err(
            f"old_string is {len(old_string)} characters, exceeding the "
            f"{_MAX_OLD_STRING_LEN}-character cap on replacement-target size — "
            "a correction targets a localized snippet, not a large block; "
            "split the correction into smaller calls"
        )

    # Precondition (Finding 1): old_string must never be the whole body —
    # closes the exact reproduction (old_string = body, same-length
    # new_string) that trivially satisfies "occurs exactly once."
    if old_string.strip() == body.strip():
        return _err(
            "old_string is the entire body — refusing a whole-body "
            "replacement; this op replaces a localized snippet, not the "
            "whole document (DR-247 D2(iii))"
        )

    # Precondition (Finding 1): majority-of-body bound. Catches short bodies
    # where the absolute cap above is larger than the document — replacing
    # half of a document is a rewrite regardless of absolute size.
    if len(old_string) > _MAX_OLD_STRING_BODY_RATIO * len(body):
        return _err(
            f"old_string is {len(old_string)} characters, exceeding "
            f"{_MAX_OLD_STRING_BODY_RATIO * 100:.0f}% of the body's "
            f"{len(body)} characters — refusing a replacement that replaces "
            "most of the document; split the correction into smaller calls"
        )

    # Precondition: new_string introduces no new heading line (AC3). `---`-line
    # introduction is already unconditionally refused above (crossing-delimiter
    # check), so no separate new-vs-old count is needed for that half of AC3.
    old_heading_count = _count_matching_lines(old_string, _HEADING_LINE_RE)
    new_heading_count = _count_matching_lines(new_string, _HEADING_LINE_RE)
    if new_heading_count > old_heading_count:
        return _err(
            "new_string introduces a new heading line ('#' through '######') — "
            "a correction repairs existing prose, it does not restructure the document"
        )

    # Precondition: net-growth cap (AC3). Measured in UTF-8 bytes (DR-247 says
    # "bytes") rather than Python len() code points — a code-point measurement
    # undercounts actual on-disk byte growth by up to 4x for non-ASCII text
    # (Finding 3, code-reviewer 2026-07-31). Byte measurement is strictly
    # tighter than the prior code-point measurement, never looser.
    net_growth = len(new_string.encode("utf-8")) - len(old_string.encode("utf-8"))
    if net_growth > _NET_GROWTH_CAP:
        return _err(
            f"new_string grows the body by {net_growth} bytes, exceeding the "
            f"{_NET_GROWTH_CAP}-byte net-growth cap — refusing a replacement "
            "shaped like a progress-journal append rather than a correction"
        )

    note_line = _build_correction_note(
        session_id, session_source, old_string, new_string,
        basis, disagreement, override_reason,
    )

    def _mutate(old_text: str) -> str:
        split_inner = split_frontmatter(old_text)
        if split_inner is None:
            raise MutateAbort(f"no valid YAML frontmatter block in: {handoff_path_raw}")
        # Finding 2 (security-audit, 2026-07-31): frontmatter must be
        # byte-identical before/after — assert against the TRUE raw bytes
        # (`_split_frontmatter_raw_bytes`), never `split_frontmatter`'s
        # CRLF-normalized `fm_text`. Comparing two equally-normalized copies
        # can never detect an actual on-disk frontmatter change, and the
        # prior rebuild silently downgraded a CRLF-authored frontmatter to
        # LF on every write — contradicting DR-247 D2(iv)'s literal
        # byte-identical requirement.
        #
        # `old_text` itself (locked_rmw's own read) has ALREADY been through
        # `Path.read_text()`'s universal-newline translation by the time it
        # reaches this function — it cannot be used to recover the true raw
        # bytes. Re-read the file directly (safe: still inside the flock
        # locked_rmw is holding for this whole read-mutate-write span) to get
        # the actual on-disk bytes for the byte-identity check and rebuild.
        try:
            raw_text_inner = p.read_bytes().decode("utf-8")
        except OSError as exc:
            raise MutateAbort(
                f"cannot re-read handoff file for byte-identity check: {exc}"
            )
        raw_inner = _split_frontmatter_raw_bytes(raw_text_inner)
        if raw_inner is None:
            raise MutateAbort(
                f"no valid YAML frontmatter block in: {handoff_path_raw}"
            )
        frontmatter_full_raw_inner, _ = raw_inner
        if frontmatter_full_raw_inner != frontmatter_full_raw:
            raise MutateAbort(
                f"{handoff_path_raw}'s frontmatter changed between read and lock "
                "acquisition — refusing to apply a body correction against a "
                "moving target"
            )
        inner_body = split_inner.body_with_leading_newline
        if inner_body.count(old_string) != 1:
            raise MutateAbort(
                f"{handoff_path_raw}'s body changed between read and lock "
                "acquisition (old_string no longer occurs exactly once) — "
                "refusing to apply a body correction against a moving target"
            )
        new_body = inner_body.replace(old_string, new_string, 1)
        new_body = _append_correction_note(new_body, note_line)
        # Rebuild using the RAW, byte-preserved frontmatter (never
        # `split.fm_text`'s normalized copy — Finding 2) so a CRLF-authored
        # handoff's frontmatter survives a correction with its original line
        # endings intact.
        new_text = frontmatter_full_raw_inner + new_body
        # Byte-identical frontmatter assertion, re-derived from the freshly
        # rebuilt text (never assumed from the pre-lock read above).
        final_raw = _split_frontmatter_raw_bytes(new_text)
        if final_raw is None or final_raw[0] != frontmatter_full_raw_inner:
            raise MutateAbort(
                f"{handoff_path_raw}: post-mutation frontmatter is not "
                "byte-identical to pre-mutation frontmatter — refusing to write"
            )
        return new_text

    try:
        await asyncio.to_thread(locked_rmw, p, _mutate, repo_root=repo_root)
    except LockTimeout as exc:
        return _err(f"lock timeout acquiring file lock: {exc}")
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "mutate aborted")
    except OSError as exc:
        return _err(f"cannot read/write handoff file: {exc}")

    _LOG.info(
        "handoff.correct_body: applied correction to %s (session %s via %s)",
        p, session_id, session_source,
    )
    return {
        "exit_code": 0,
        "applied": True,
        "session_id": session_id,
        "session_source": session_source,
        "ownership_basis": basis,
        "message": f"applied body correction to {handoff_path_raw}",
    }
