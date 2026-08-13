"""
coordinator_core.ops.fleet.migrate_handoff_vocabulary — fleet.migrate_handoff_vocabulary op.

Purpose: one-shot corpus migrator for the DR-084 handoff-lifecycle vocabulary
(``docs/plans/2026-07-22-handoff-lifecycle-vocabulary-overhaul-scope.md`` § C7). The
dual-read tolerance restored in ``coverage.py._parse_handoff_consumed_by``,
``ops/ceremony/resolver.py``, and ``session_hierarchy/derive.py`` (fix commit
``9d00b459``) is transitional, not permanent — its named exit condition is that
every consumer repo's on-disk handoff corpus is migrated to the new vocabulary AND
a pre-flight scan confirms zero surviving old tokens. This op IS that migration: it
mechanically re-expresses ``state/handoffs/**``, ``archive/handoffs/**``, and the
hidden ``state/handoffs/.archive/`` (a P4 landmine named in the plan's execution
notes — outside every glob, harmless under dual-read, fatal once tolerance
retires) against an arbitrary ``repo_root`` — claude-klabauter's own or a consumer repo's
(example-retrieval-repo, example-cockpit-repo).

Mapping (mirrors claude-klabauter's own C5+C8 live-data migration, ``e2cf1a08``, byte-for-byte
— that commit's per-record decisions ARE the specification, not a template to
reinterpret):
  - ``status: active`` → ``open``; ``status: consumed`` → ``claimed``.
    ``status: superseded`` is UNTOUCHED — a permanently grandfathered, separate axis
    on the archived schema (2026-06-26 DO-NOT-NARROW invariant; see
    ``coordinator_core/ops/emit/sections/handoffs.py`` module docstring).
  - ``consumed_at`` → ``claimed_at``, ``consumed_by`` → ``claimed_by`` — the key
    NAME is renamed in place (same line position), the value is untouched. A record
    carrying BOTH the old and new name for a pair has the new name win and the old
    name dropped — every such collision is still recorded in the report, never
    silently resolved without a trace.
  - ``deployment_state: abandoned`` splits on positive succession proof, checked
    via a THREE-rung ladder (reverse-lineage FIRST, never reordered — see
    ``_resolve_superseded_by`` for why): (1) a live OR archived handoff naming
    this record via a ``predecessor``/``additional_predecessors``/
    ``origin_handoff`` reverse lineage edge (``coordinator_core.dag.referenced_by``
    — the exact mechanism ``e2cf1a08``'s commit message names, "succession child
    found via reverse lineage edge"); (2) else this record's OWN
    ``superseded_by`` field (foreign-corpus tolerance only — NOT a claude-klabauter
    handoff-schema field), resolved to exactly one on-disk corpus path; (3) else
    a ``deliverable_id`` JOIN — see ``_resolve_deliverable_id_successor`` — for
    the one shape neither of the first two rungs can see: a ``kind:
    spinoff-roadmap`` stub graduating into its ``kind: session-handoff``
    execution baton, where NEITHER record names the other and the only link is
    a shared ``deliverable_id``. Any rung re-expresses this record as
    ``continued`` + ``continued_into: <successor handoff_id, or its
    repo-relative path if the successor has no handoff_id>`` — the report line
    also names which rung fired (``reverse-lineage`` / ``superseded_by`` /
    ``deliverable_id-join``), so a receiving EM can audit the verdict without
    re-deriving it. No successor found on any rung → ``closed`` +
    ``closed_reason: stale`` + a one-line ``# migration: ...`` provenance comment
    (mirrors ``e2cf1a08``'s comment shape, extended per the negative-spec note
    below). This is the ONLY non-mechanical rule — see ``_find_successor``,
    ``_resolve_superseded_by``, and ``_resolve_deliverable_id_successor``.

    ``closed``+``stale`` on this fall-through is a WORKING-TREE-WALK NEGATIVE
    RESULT, never a positive death claim — "no successor found by walking the
    tree" and "this work died" are different claims (example-cockpit-repo's
    2026-07-23 dr084 ask). A successor deleted from the corpus without being
    archived survives only as a git blob and is structurally invisible to all
    three rungs; ``stale`` is the enum's nearest available value for that
    residual (the ``closed_reason`` enum — ``cancelled | displaced | stale`` —
    is schema-frozen upstream and this op does not invent a fourth value), not
    an assertion the work is dead. Every such record is stamped with an
    explicit provenance comment saying so (see the ``else:`` branch of
    ``_plan_one``'s split) and collected into its own ``closed_unverified``
    report bucket (path list, alongside ``failures``/``repairs``) so a
    receiving EM can find the whole set and check git history before trusting
    any one of them.

    ``deployment_state: abandoned`` on a ``kind: spinoff-roadmap`` stub is, in
    the resolvable case, a LIFECYCLE TRANSITION, not deliverable death — the
    stub ends because its deliverable graduated into an executed plan, not
    because the work died. Evidence from a real consumer corpus (example-cockpit-repo,
    2026-07-23 memo): ``"The deployment_state: abandoned on the clki-04 handoffs
    is stub-lifecycle-ended (graduated into its executed plan), NOT deliverable
    death."`` Rung 3 above is what mechanizes this for the resolvable cases —
    a graduated stub whose execution baton is NOT independently resolvable
    (ambiguous deliverable_id match, missing baton, etc.) still falls through
    to ``closed`` + ``stale``, and that residual is a known, documented limit of
    this migration pass, not a silent one. This op does NOT invent a new
    ``closed_reason`` value for that residual — the enum (``cancelled |
    displaced | stale``) is schema-frozen in
    ``coordinator_core/frontmatter/schemas/handoff-archived.schema.json``.

Dry-run by DEFAULT — mirrors ``normalize_claimed_frontmatter.py``'s CLI convention
and ``cruft_sweep.py``'s ``apply`` JSON-RPC param default. A bare invocation (CLI
``--root`` alone, or the op with ``apply`` omitted/false) is incapable of mutating
anything; writing requires ``--apply`` (CLI) or ``params.apply: true`` (op).

Idempotent: a record already in new vocabulary (status open/claimed, no
consumed_at/consumed_by, deployment_state != abandoned) produces zero planned
changes and is never opened for write — a second run over an already-migrated
corpus reports zero changes and exits 0.

Fail-loud on the unclassifiable: a record with an unparseable frontmatter block, a
``status``/``deployment_state`` value outside the known enum (old ∪ new), or a
block-scalar field ``replace_fm_field``/``insert_fm_field`` refuses to touch is
reported in the ``failures`` list and left byte-for-byte untouched — never guessed
at, never partially written. A non-empty ``failures`` list is a non-zero exit code
(CLI) / ``exit_code: 1`` (op), even when other records in the same run migrated
cleanly.

Spec backlink: pln-handoff-lifecycle-vocabulary-o-22ada6 § C7
Fix commit (dual-read restoration this migration retires): 9d00b459
Live-data migration oracle (claude-klabauter's own corpus, same mapping): e2cf1a08

Negative-spec:
    - Reads ``status``/``deployment_state`` comment-aware (a trailing ``# ...``
      does not change the parsed value — matches ``emit/sections/handoffs.py``'s
      reader), via ``coordinator_core.dag._strip_inline_comment``, NOT a
      hand-rolled ``split('#')`` (which would wrongly treat a ``#`` inside a
      quoted scalar as a comment). Rewriting a status/deployment_state line
      that DOES carry a trailing comment drops that comment — ``replace_fm_field``
      replaces the whole rest of the line — a documented, accepted tradeoff, BUT
      the assumption behind it is narrower than earlier phrasing here claimed.
      This module used to assert the dropped comment "was almost always about
      the OLD value being migrated away" — a real counterexample refutes that:
      example-cockpit-repo's corpus carried
      ``status: consumed  # reconciled 2026-07-17: already landed (7aa0e015
      ancestor of HEAD; WSC ceremony closed on main); do NOT re-run wsc_commit``,
      a live operational warning about the CURRENT record state, not commentary
      on the value being replaced — and this op silently ate it. The drop
      behavior itself is unchanged (still an accepted tradeoff — restoring
      arbitrary prose into a rewritten scalar line is out of scope for a
      mechanical migrator), but it is no longer silent: every dropped comment is
      now surfaced as its own report line (record path, field name, verbatim
      comment text — see ``_trailing_comment_text`` and the ``dropped_comments``
      plan/record key), so the receiving EM can decide whether to restore it by
      reading the report instead of discovering the loss by grep.
    - Does NOT invent a successor. ``continued`` is stamped ONLY on a positive
      reverse-lineage edge found in another record's OWN frontmatter, on its own
      ``superseded_by`` field resolving unambiguously, or on an unambiguous
      ``deliverable_id`` join to a later execution baton — never on a liveness
      guess, never on prose in the body, matching the plan's Anti-scope
      ("continued without a recorded continued_into successor is the banned shape
      wearing a new label").
    - ``claimed_by``/``consumed_by`` session-id equality across two records is
      NOT a succession edge and this op never treats it as one. It shows only
      that one session was active across both records — no lineage claim
      follows from that. (Rejected candidate from example-cockpit-repo's own audit:
      a session-id-derived successor resolved to a path absent from disk.)
    - A completion entry (``archive/completed/**``) is NOT a successor and is
      never a ``continued_into`` target. ``iter_handoff_files`` scans ONLY
      ``state/handoffs/`` and ``archive/handoffs/`` — deliberately, not an
      oversight — because widening the scan to completion entries would let a
      resolver point ``continued_into`` at something that is not a handoff,
      producing a dangling reference. A record whose real continuation lives
      only in a completion entry, with no intermediate handoff, has no valid
      successor under this op's contract and correctly falls through to
      ``closed`` + ``stale``.
    - Does NOT stamp ``closed`` under any circumstance the plan's Anti-scope
      forbids for a live writer — this is a one-time, PM-authorized, archive-only,
      reversible migration pass over already-terminal (``abandoned``) records, the
      same carve-out C8 used ("migration-time re-expression under PM
      plan-authorization sits outside the automated-writer closed-stamp ban").
    - Does NOT touch ``status: superseded``, non-abandoned ``deployment_state``
      values (``awaiting_gate``/``ready_to_fire``/``in_flight``/``shipped``/
      ``continued``/``closed``), or any field this module does not name above.
    - Does NOT re-derive frontmatter primitives — routes every mutation through
      ``coordinator_core.frontmatter.primitives`` (the same module
      ``handoff_transition.py``/``normalize_claimed_frontmatter.py`` share),
      preserving frontmatter key order and body bytes exactly outside the touched
      lines.
    - Does NOT assume ``os.getcwd()`` or a repo boundary — ``repo_root`` is always
      an explicit parameter; this op runs inside consumer-repo trees, not just
      claude-klabauter's own.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from coordinator_core.dag import _strip_inline_comment, referenced_by
from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    rebuild,
    remove_fm_field,
    replace_fm_field,
    split_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.ops.deliverable_equivalence import canonicalize, load_equivalence_map
from coordinator_core.ops.fleet._common import (
    build_setup_error_result,
    check_repo_root,
    main_worktree_root,
)

_PROG = "migrate-handoff-vocabulary"

_LOG = logging.getLogger(__name__)

# Old ∪ new status/deployment_state enums — anything outside these is
# unclassifiable (fail loud, never guessed at). "superseded" is the permanently
# grandfathered archived-schema status axis (module docstring) — recognized but
# never rewritten.
_KNOWN_STATUSES: Set[str] = {"active", "consumed", "open", "claimed", "superseded"}
_KNOWN_DEPLOYMENT_STATES: Set[str] = {
    "awaiting_gate", "ready_to_fire", "in_flight", "shipped",
    "abandoned", "continued", "closed",
}

# deployment_state: superseded is INVALID on every vocabulary (old or new) — it is
# the handoff STATUS axis's retired 2026-06-26 token (coordinator-claude ruling: retirement
# replacement expression is status: consumed + deployment_state: abandoned)
# written onto the wrong axis. Observed in example-retrieval-repo's corpus on records dated
# AFTER that retirement (2026-07-03..07-14) — fleet-migration fallout, not a
# data-entry error local to that repo. Recognized (not fail-loud) so it can be
# REPAIRED (normalized to "abandoned" per the 2026-06-26 ruling, then split by
# the same succession-proof rule) rather than reported unclassifiable — but
# tracked as its own category (plan["repairs"]) so a reviewer never mistakes a
# repair for an ordinary vocabulary rename. Distinct from (and must never be
# confused with) status: superseded, the permanently grandfathered STATUS-axis
# value on archived records, which this module never touches.
_DEPLOYMENT_STATE_REPAIRABLE: Set[str] = {"superseded"}

_STATUS_OLD_TO_NEW: Dict[str, str] = {"active": "open", "consumed": "claimed"}

# (old_field, new_field) rename pairs — key name renamed in place, value untouched.
_FIELD_RENAMES = (("consumed_at", "claimed_at"), ("consumed_by", "claimed_by"))

# Succession edges only — mirrors archive_handoffs.py's _HEIR_EDGE_KINDS, PLUS
# origin_handoff (added 2026-07-23 per example-cockpit-repo's dr084 memo). A
# `kind: spinoff` handoff carries `predecessor: none` BY DESIGN (coordinator
# spinoff-handoff schema — see coordinator-claude `docs/wiki/spinoff-handoffs.md` §
# "predecessor is none by design") and names its parent in `origin_handoff:`
# instead, so without this edge every spinoff succession looks like an orphan.
# origin_handoff is a registered walkable edge in dag.EDGE_KIND_META, so
# referenced_by handles it with no other change once it's unioned in here.
#
# forked_from is branch-point/derivation ancestry (DR-224), not succession, and
# is deliberately excluded: a spinoff does not retire its origin.
_HEIR_EDGE_KINDS = {"predecessor", "additional_predecessors", "origin_handoff"}

def _clean_scalar(raw: Optional[str]) -> Optional[str]:
    """Strip a trailing YAML inline comment from a raw ``read_fm_field`` capture.

    ``read_fm_field`` (``coordinator_core.frontmatter.primitives``) is a raw
    text extractor — it returns everything after ``key:`` to end-of-line,
    trailing comment included, by design (callers that need the on-disk bytes
    verbatim, e.g. this module's own ``_rename_fm_key``, want that). Reading
    ``status``/``deployment_state`` for CLASSIFICATION, though, needs the bare
    scalar: ``status: consumed  # reconciled ...`` is legal YAML for
    ``consumed`` — a trailing comment does not change the value. Reuses
    ``coordinator_core.dag._strip_inline_comment`` (the quote-aware
    implementation ``dag.referenced_by``'s own frontmatter reader already
    uses, and the same shape ``emit/sections/handoffs.py``'s reader relies on)
    rather than hand-rolling comment stripping — a naive ``split('#')`` would
    wrongly treat a ``#`` inside a quoted scalar as a comment opener.
    """
    return _strip_inline_comment(raw) if raw is not None else None


def _trailing_comment_text(raw: Optional[str]) -> Optional[str]:
    """Return the trailing ``# ...`` YAML inline comment on a raw
    ``read_fm_field`` capture, or None if there is none.

    Deliberately mirrors ``coordinator_core.dag._strip_inline_comment``'s
    quote-aware comment-boundary rule (a ``#`` opens a comment only when NOT
    inside a quoted span AND preceded by whitespace AND followed by whitespace
    or end-of-string) rather than reusing it directly — ``_strip_inline_comment``
    returns the PREFIX (the value) and discards the comment; this needs the
    comment itself, purely for report-line purposes (see the
    ``dropped_comments`` plan/record key). Never used for classification —
    ``_clean_scalar`` (which does use ``_strip_inline_comment``) is the only
    classification reader.
    """
    if raw is None:
        return None
    in_single = False
    in_double = False
    for i, c in enumerate(raw):
        if c == '"' and not in_single:
            in_double = not in_double
        elif c == "'" and not in_double:
            if in_single and i + 1 < len(raw) and raw[i + 1] == "'":
                continue
            in_single = not in_single
        elif c == '#' and not in_single and not in_double:
            if i > 0 and raw[i - 1] in (' ', '\t'):
                if i + 1 >= len(raw) or raw[i + 1] in (' ', '\t'):
                    return raw[i:].strip()
    return None


_KEY_LINE_RE_CACHE: Dict[str, "re.Pattern[str]"] = {}


def _key_line_re(key: str) -> "re.Pattern[str]":
    """Cached ``^key:`` locator sharing ``frontmatter/primitives.py``'s
    key-resolution boundary lookahead ``(?=[ \\t]|\\r?$)``.

    The ``\\r?`` half (2026-07-28) is load-bearing, not cosmetic: without it a
    present-but-empty ``key:\\r\\n`` in a Windows-authored handoff does not
    match, so ``_rename_fm_key`` cannot locate a key ``read_fm_field`` reports
    as present — the migration then either no-ops or raises on a file it should
    have rewritten. No upstream LF-only normalization of handoff text exists to
    lean on.
    """
    pattern = _KEY_LINE_RE_CACHE.get(key)
    if pattern is None:
        pattern = re.compile(r"^" + re.escape(key) + r":(?=[ \t]|\r?$)", re.MULTILINE)
        _KEY_LINE_RE_CACHE[key] = pattern
    return pattern


def _rename_fm_key(fm_text: str, old_key: str, new_key: str) -> str:
    """Rename a frontmatter key in place, preserving its line position and value.

    Unlike ``remove_fm_field`` + ``insert_fm_field`` (delete-then-append-elsewhere),
    this substitutes only the ``old_key:`` token at the START of its line — the
    value (and any would-be block-scalar continuation lines, which this does NOT
    touch) stays exactly where it was. Mirrors ``e2cf1a08``'s live-data migration,
    where ``claimed_at``/``claimed_by`` replaced ``consumed_at``/``consumed_by`` at
    the SAME line, not at a new position.

    No block-scalar guard is needed here (unlike ``replace_fm_field``/
    ``remove_fm_field``): renaming the key label never touches the value or any
    continuation line that follows it.

    Raises ValueError if old_key is not present (callers must check
    ``read_fm_field`` first — mirrors the other primitives' contract of raising
    on a precondition the caller was responsible for checking).
    """
    pattern = _key_line_re(old_key)
    # negative-spec: the replacement MUST stay a callable. `re` processes backslash
    # escapes in a replacement TEMPLATE, so a template built from a runtime value is a
    # latent crash (`\U`, `\a`, `\b`, `\f`, `\n`, `\r`, `\t`, `\v`, `\<digit>`) and a
    # latent mis-group against any backreference. Enforced by
    # coordinator_core/tests/test_re_sub_replacement_template_is_literal_or_callable.py.
    new_text, count = pattern.subn(lambda _m: new_key + ":", fm_text, count=1)
    if count == 0:
        raise ValueError(f"_rename_fm_key: {old_key!r} not found in frontmatter")
    return new_text


def _insert_raw_line_after(fm_text: str, after_key: str, raw_line: str) -> str:
    """Insert a raw (non key:value) line immediately after ``after_key:``'s line.

    Same anchored-insert mechanics as ``insert_fm_field`` (falls back to append-at-
    end if the anchor is absent), but for a bare comment line — used for the
    migration-provenance ``# migration: ...`` note, which is not itself a
    schema-visible field.
    """
    pattern = _key_line_re(after_key)
    m = pattern.search(fm_text)
    if m is None:
        trimmed = fm_text.rstrip()
        return trimmed + "\n" + raw_line + "\n"
    line_end = fm_text.find("\n", m.end())
    insert_at = len(fm_text) if line_end == -1 else line_end + 1
    return fm_text[:insert_at] + raw_line + "\n" + fm_text[insert_at:]


def _repo_rel(path: Path, repo_root: Path) -> str:
    """Repo-relative, forward-slash path — matches the ``predecessor:`` field
    convention already used across the handoff corpus (e.g.
    ``state/handoffs/2026-07-15_....md``)."""
    try:
        rel = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return str(path)
    return str(rel).replace(os.sep, "/")


def iter_handoff_files(repo_root: Path) -> List[Path]:
    """Every handoff record under ``state/handoffs/`` and ``archive/handoffs/``,
    INCLUDING the hidden ``state/handoffs/.archive/`` dir.

    ``Path.rglob`` already descends into dot-directories (unlike a shell glob), so
    a plain ``rglob("*.md")`` over ``state/handoffs/`` already picks up
    ``.archive/`` — mirrors
    ``coordinator_core/contract/cockpit_schema/tests/test_verify_vocabulary_retirement.py``'s
    ``_iter_handoff_md_files`` scan, the precedent for this exact hidden-dir
    requirement (C8 execution note: "a hidden dir outside every glob, harmless
    under dual-read but a P4 landmine once old-vocabulary acceptance retires").
    Deliberately NOT reused via import: that function is test-local (parity/lock
    test, not a shared library surface) and reads from a hardcoded module-relative
    repo root, incompatible with this op's arbitrary-repo_root contract.
    """
    state_dir = repo_root / "state" / "handoffs"
    archive_dir = repo_root / "archive" / "handoffs"
    files: Set[Path] = set()
    for base in (state_dir, archive_dir):
        if base.is_dir():
            files.update(p.resolve() for p in base.rglob("*.md") if p.is_file())
    return sorted(files)


def _find_successor(
    candidate_path: Path, all_paths: List[Path], state_dir: Path
) -> Optional[Path]:
    """Positive succession proof: a live/archived handoff naming ``candidate_path``
    via a ``predecessor``/``additional_predecessors``/``origin_handoff`` reverse
    lineage edge.

    Reuses ``coordinator_core.dag.referenced_by`` — the same primitive
    ``archival.reverse_membership`` (and, transitively, ``archive_handoffs.py``'s
    heir-branch classifier) is built on — rather than re-deriving edge resolution.
    Deterministic on a diamond (>1 successor): lexicographically-first absolute
    path wins, matching ``archive_handoffs.py``'s ``_classify_heir_children``.

    Returns None on zero referencers OR on a ``referenced_by`` raise (empty
    ``all_paths``, bad ``handoff_dir``) — fail-closed to the "no successor found"
    branch (``closed``+``stale``), never silently treated as "found".
    """
    try:
        result = referenced_by(
            str(candidate_path),
            [str(p) for p in all_paths],
            edge_kinds=_HEIR_EDGE_KINDS,
            handoff_dir=str(state_dir),
            exclude=[str(candidate_path)],
        )
    except (ValueError, TypeError):
        return None
    referencers = result.get("referencedBy") or []
    if not referencers:
        return None
    return Path(sorted(referencers)[0])


def _resolve_superseded_by(
    fm_text: str, repo_root: Path, all_paths: List[Path]
) -> Optional[Path]:
    """Fallback successor lookup: this record's OWN ``superseded_by`` field,
    resolved against the on-disk corpus.

    ``superseded_by`` is NOT a claude-klabauter handoff-schema field — it lives only on
    the memo schema (``coordinator_core/frontmatter/schema_validate.py``
    ~:1258-1270). This rung exists purely as tolerance for foreign-corpus
    shapes encountered when this op runs against a CONSUMER tree (example-retrieval-repo
    et al.), never as an endorsement of ``superseded_by`` on claude-klabauter's own
    handoffs. Mirrors a sibling-repo lesson: example-retrieval-repo's own migrator
    mis-closed records by treating reverse-lineage as the only succession
    channel — a record can carry positive succession proof reachable ONLY via
    its own forward ``superseded_by`` pointer, never named by any other
    record's ``predecessor``/``additional_predecessors``.

    Called ONLY as a fallback when ``_find_successor`` (reverse-lineage) finds
    nothing — reverse-lineage stays first-checked to preserve byte-parity with
    the already-shipped C5+C8 migration pass (``e2cf1a08``); this rung must
    never override a reverse-lineage hit.

    Fail-closed, matching ``_find_successor``'s convention: a prose value (does
    not resolve to any corpus path), a value resolving to zero or to more than
    one corpus path, or any error during resolution all return ``None`` — never
    guessed at, never treated as "found" on ambiguity.

    Candidate values are comma/semicolon-separated repo-relative (or absolute)
    paths, resolved against ``repo_root`` and matched against ``all_paths``
    (the same corpus set ``_find_successor`` searches) — the same
    repo-relative-path convention already used by ``predecessor``.
    """
    raw = _clean_scalar(read_fm_field(fm_text, "superseded_by"))
    if raw is None:
        return None
    value = raw.strip().strip("\"'")
    if not value:
        return None

    try:
        all_resolved: Set[Path] = {p.resolve() for p in all_paths}
        matched: Set[Path] = set()
        for token in re.split(r"[,;]", value):
            token = token.strip().strip("\"'")
            if not token:
                continue
            candidate = (repo_root / token).resolve()
            if candidate in all_resolved:
                matched.add(candidate)
    except (OSError, ValueError):
        return None

    if len(matched) != 1:
        return None
    return next(iter(matched))


def _parse_record_timestamp(value: str) -> Optional[datetime.datetime]:
    """Parse a frontmatter timestamp scalar (``created:`` value or similar) into
    a naive UTC ``datetime`` for comparison. Accepts a full RFC3339/ISO
    timestamp (``2026-07-11T00:21:00Z`` or with an explicit offset — converted
    to UTC and made naive) or a bare ``YYYY-MM-DD`` date (treated as midnight).
    Returns None on anything unparseable — callers treat that as "not
    comparable", never as "earliest possible".
    """
    text = value.strip()
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        pass
    try:
        d = datetime.date.fromisoformat(text[:10])
        return datetime.datetime.combine(d, datetime.time.min)
    except ValueError:
        return None


_FILENAME_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{6})")


def _record_timestamp(fm_text: str, path: Path) -> Optional[datetime.datetime]:
    """Best-effort comparable timestamp for a handoff record: the ``created:``
    frontmatter field if present and parseable, else the leading
    ``YYYY-MM-DD_HHMMSS`` filename timestamp convention already used across the
    handoff corpus, else None (not comparable — callers fail closed on that,
    per ``_resolve_deliverable_id_successor``'s match rule).
    """
    created_raw = _clean_scalar(read_fm_field(fm_text, "created"))
    if created_raw:
        parsed = _parse_record_timestamp(created_raw.strip().strip("\"'"))
        if parsed is not None:
            return parsed
    m = _FILENAME_TIMESTAMP_RE.match(path.name)
    if m is None:
        return None
    try:
        return datetime.datetime.strptime(m.group(1) + m.group(2), "%Y-%m-%d%H%M%S")
    except ValueError:
        return None


def _resolve_deliverable_id_successor(
    fm_text: str,
    candidate_path: Path,
    all_paths: List[Path],
    repo_root: Path,
    equivalence_map: Dict[str, str],
) -> Optional[Path]:
    """Third rung of the successor ladder: a JOIN on shared ``deliverable_id``,
    for the one succession shape neither reverse-lineage nor ``superseded_by``
    can see.

    ``equivalence_map`` (``coordinator_core.ops.deliverable_equivalence``,
    ``{loser_id: winner_id}``) is applied via ``canonicalize()`` at the JOIN
    predicate ONLY — both the subject's and each candidate's ``deliverable_id``
    are canonicalized before comparison, never written back to ``fm_text`` or
    to any record on disk. This re-enables the join for a declared fork pair
    (e.g. ``sat-01``'s ``dlv-sat-01`` / ``dlv-sat-01-sovereign-tracker-...``)
    where the raw ids differ but both legs continue the same deliverable. A
    fork with no declared equivalence entry canonicalizes to itself, so this
    is a pure widening of the match set, never a narrowing.

    A roadmap stub (``kind: spinoff-roadmap``) and its execution baton
    (``kind: session-handoff``) continue the same work while sharing NO
    lineage field — both carry ``predecessor: none``, and neither names the
    other. The only link is a shared ``deliverable_id``. Because there is no
    edge to walk, this is a JOIN over the corpus, not a graph traversal — it
    cannot go through ``coordinator_core.dag.referenced_by`` the way
    ``_find_successor`` does.

    Called ONLY as the third fallback, strictly after ``_find_successor``
    (reverse-lineage) and ``_resolve_superseded_by`` both find nothing —
    ordering is load-bearing: reverse-lineage stays first-checked for
    byte-parity with the already-shipped C5+C8 migration pass (``e2cf1a08``),
    and this rung must never override either earlier rung.

    Match conditions — ALL must hold, fail-closed on any ambiguity:
      - ``candidate_path`` (the subject record) has ``kind: spinoff-roadmap``.
        This rung models exactly one shape — a roadmap stub graduating into its
        execution baton — and deliberately does NOT fire on arbitrary
        same-deliverable siblings of any other kind.
      - The subject has a non-empty ``deliverable_id``.
      - A candidate successor is a DIFFERENT on-disk path in ``all_paths`` with
        an IDENTICAL ``deliverable_id`` and ``kind: session-handoff``.
      - The candidate is STRICTLY LATER than the subject (see
        ``_record_timestamp``) — a candidate with an incomparable or equal
        timestamp is excluded, not guessed at.
      - Exactly ONE candidate survives all of the above. Zero → None
        (deliberately no positive result). More than one → also None — this is
        a DELIBERATE divergence from ``_find_successor``'s lexicographic
        diamond tie-break: that tie-break exists because a lineage edge is
        itself a positive assertion (multiple children of one predecessor is a
        real, resolvable diamond), whereas a ``deliverable_id`` join carries no
        lineage assertion at all to disambiguate a tie with — picking one of
        two same-deliverable batons by sort order would be a guess, and this
        module's contract is to never guess at a successor. This "exactly
        one" constraint is evaluated PER-SUBJECT (does this ONE stub have
        exactly one later same-deliverable session-handoff candidate?), not
        per-target — there is deliberately no cross-subject exclusivity
        check, so two DIFFERENT ``spinoff-roadmap`` stubs sharing one
        ``deliverable_id`` and both resolving to the SAME later baton is an
        accepted outcome, not a bug: two stubs genuinely graduating into one
        merged execution baton is a real corpus shape.

    Any exception during resolution (file I/O, parse failure, malformed
    timestamp) returns None — this rung never raises out to its caller.
    """
    try:
        kind_raw = _clean_scalar(read_fm_field(fm_text, "kind"))
        kind = kind_raw.strip().strip("\"'") if kind_raw else None
        if kind != "spinoff-roadmap":
            return None

        deliverable_raw = _clean_scalar(read_fm_field(fm_text, "deliverable_id"))
        subject_raw_id = deliverable_raw.strip().strip("\"'") if deliverable_raw else None
        if not subject_raw_id:
            return None
        deliverable_id = canonicalize(subject_raw_id, equivalence_map)

        subject_ts = _record_timestamp(fm_text, candidate_path)
        subject_resolved = candidate_path.resolve()

        matches: List[Path] = []
        for other in all_paths:
            other_resolved = other.resolve()
            if other_resolved == subject_resolved:
                continue
            try:
                with open(other, "r", encoding="utf-8", errors="replace") as fh:
                    other_text = fh.read()
            except OSError:
                continue
            other_split = split_frontmatter(other_text)
            if other_split is None:
                continue
            other_fm = other_split.fm_text

            other_kind_raw = _clean_scalar(read_fm_field(other_fm, "kind"))
            other_kind = other_kind_raw.strip().strip("\"'") if other_kind_raw else None
            if other_kind != "session-handoff":
                continue

            other_deliverable_raw = _clean_scalar(read_fm_field(other_fm, "deliverable_id"))
            other_raw_id = (
                other_deliverable_raw.strip().strip("\"'") if other_deliverable_raw else None
            )
            other_deliverable = canonicalize(other_raw_id, equivalence_map)
            if other_deliverable != deliverable_id:
                continue

            other_ts = _record_timestamp(other_fm, other)
            if subject_ts is None or other_ts is None or not (other_ts > subject_ts):
                continue

            matches.append(other)
            # Review finding 3 (2026-08-02): log which equivalence entry
            # fired whenever the join succeeded ONLY because canonicalization
            # changed one or both raw ids -- i.e. the raw ids differed but
            # the canonical ids matched. This is traceability only, not a
            # gate: the correctness burden stays on the human-reviewed
            # equivalence artifact (AC7), per the EM's confirmation that no
            # consumer-local validation should be added here. Makes a
            # wrongly-declared equivalence entry's effect on THIS join
            # visible after the fact rather than silent.
            if subject_raw_id != other_raw_id and (
                subject_raw_id != deliverable_id or other_raw_id != other_deliverable
            ):
                _LOG.info(
                    "migrate_handoff_vocabulary: deliverable_id join between "
                    "%s (%r) and %s (%r) fired via equivalence canonicalization "
                    "-> %r; verify state/deliverable-equivalence.yaml's entry "
                    "for this pair if this successor looks wrong",
                    candidate_path,
                    subject_raw_id,
                    other,
                    other_raw_id,
                    deliverable_id,
                )

        if len(matches) != 1:
            return None
        return matches[0]
    except Exception:
        return None


def _successor_ref(successor_path: Path, repo_root: Path) -> str:
    """The value to stamp into ``continued_into``: the successor's own
    ``handoff_id`` field if present (id-preferred, per the plan's C2 memo item on
    ``continued_into``'s cross-repo target format), else its repo-relative path
    (path fallback) — mirrors ``e2cf1a08``'s own migration
    (``continued_into: hnd-coverage-gate-single-graph-wal-057c56``, an id, not a
    path, on that record)."""
    try:
        with open(successor_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return _repo_rel(successor_path, repo_root)
    split = split_frontmatter(text)
    if split is not None:
        handoff_id = read_fm_field(split.fm_text, "handoff_id")
        if handoff_id:
            return handoff_id.strip().strip("\"'")
    return _repo_rel(successor_path, repo_root)


def _plan_one(
    path: Path,
    repo_root: Path,
    state_dir: Path,
    all_paths: List[Path],
    equivalence_map: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    """Plan one record's migration. Returns None on a genuine no-op (already fully
    new-vocabulary — the idempotency contract). Raises ValueError on an
    unclassifiable record (caller converts to a ``failures`` entry) — never
    guesses, never partially applies.
    """
    with open(path, "r", encoding="utf-8", newline="") as fh:
        original = fh.read()

    split = split_frontmatter(original)
    if split is None:
        raise ValueError("no parseable YAML frontmatter")

    fm_text = split.fm_text
    # Comment-aware: a trailing `# ...` on the status/deployment_state line is
    # legal YAML and must not change classification (see _clean_scalar). The
    # `_full` captures are kept alongside for the dropped-comment report (see
    # _trailing_comment_text) — classification never reads them directly.
    status_raw_full = read_fm_field(fm_text, "status")
    deployment_raw_full = read_fm_field(fm_text, "deployment_state")
    status_raw = _clean_scalar(status_raw_full)
    deployment_raw = _clean_scalar(deployment_raw_full)

    if status_raw is None or status_raw not in _KNOWN_STATUSES:
        raise ValueError(f"unrecognized status {status_raw!r}")
    if deployment_raw is None:
        # Required-field defect, not a vocabulary issue — this tool repairs
        # vocabulary, it does not invent a missing required field. Reported
        # unfixable-by-this-tool for the owning EM to resolve; never guessed.
        raise ValueError("deployment_state field is absent (required-field defect, not a vocabulary issue)")
    if (
        deployment_raw not in _KNOWN_DEPLOYMENT_STATES
        and deployment_raw not in _DEPLOYMENT_STATE_REPAIRABLE
    ):
        raise ValueError(f"unrecognized deployment_state {deployment_raw!r}")

    changes: List[str] = []
    collisions: List[str] = []
    dropped_comments: List[str] = []
    closed_unverified = False
    is_repair = deployment_raw in _DEPLOYMENT_STATE_REPAIRABLE

    if status_raw in _STATUS_OLD_TO_NEW:
        new_status = _STATUS_OLD_TO_NEW[status_raw]
        dropped = _trailing_comment_text(status_raw_full)
        if dropped:
            dropped_comments.append(f"status: {dropped}")
        fm_text = replace_fm_field(fm_text, "status", new_status)
        changes.append(f"status: {status_raw} → {new_status}")

    for old_key, new_key in _FIELD_RENAMES:
        old_val = read_fm_field(fm_text, old_key)
        new_val = read_fm_field(fm_text, new_key)
        if old_val is not None and new_val is not None:
            fm_text = remove_fm_field(fm_text, old_key)
            collisions.append(
                f"{old_key} ({old_val!r}) and {new_key} ({new_val!r}) both present "
                f"— kept {new_key}, dropped {old_key}"
            )
            changes.append(f"{old_key}: dropped (superseded by existing {new_key})")
        elif old_val is not None:
            fm_text = _rename_fm_key(fm_text, old_key, new_key)
            changes.append(f"{old_key} → {new_key} (renamed, value preserved: {old_val})")

    if deployment_raw == "abandoned" or is_repair:
        # Ladder: reverse-lineage first (byte-parity with the shipped C5+C8
        # pass, e2cf1a08 — do NOT reorder), THEN this record's own
        # superseded_by as a fallback channel (see _resolve_superseded_by),
        # THEN a deliverable_id join for the roadmap-stub-graduates-into-baton
        # shape neither earlier rung can see (see
        # _resolve_deliverable_id_successor). Each rung is only tried when the
        # one before it found nothing; the rung that actually resolved the
        # successor is named in the report so a receiving EM can audit a
        # `continued` verdict without re-deriving it.
        successor = _find_successor(path, all_paths, state_dir)
        successor_rung: Optional[str] = "reverse-lineage" if successor is not None else None
        if successor is None:
            successor = _resolve_superseded_by(fm_text, repo_root, all_paths)
            if successor is not None:
                successor_rung = "superseded_by"
        if successor is None:
            successor = _resolve_deliverable_id_successor(
                fm_text, path, all_paths, repo_root, equivalence_map
            )
            if successor is not None:
                successor_rung = "deliverable_id-join"
        today = datetime.date.today().isoformat()
        repair_prefix = "REPAIR: deployment_state: superseded (invalid — " \
            "coordinator-claude 2026-06-26 retired 'superseded' as a handoff STATUS; the ruling's " \
            "own replacement expression is status: consumed + deployment_state: " \
            "abandoned, so this value is normalized to abandoned before the usual " \
            "split) → " if is_repair else "deployment_state: abandoned → "
        dropped = _trailing_comment_text(deployment_raw_full)
        if dropped:
            dropped_comments.append(f"deployment_state: {dropped}")
        if successor is not None:
            continued_into = _successor_ref(successor, repo_root)
            fm_text = replace_fm_field(fm_text, "deployment_state", "continued")
            fm_text = insert_fm_field(fm_text, "continued_into", continued_into, "deployment_state")
            if is_repair:
                fm_text = _insert_raw_line_after(
                    fm_text, "continued_into",
                    f"# migration: DR-084 consumer-corpus REPAIR {today} "
                    f"(was: deployment_state: superseded, invalid on any vocabulary — "
                    f"normalized to abandoned per coordinator-claude's 2026-06-26 status retirement, "
                    f"then continued via reverse lineage edge)",
                )
            changes.append(
                f"{repair_prefix}continued "
                f"(successor: {continued_into}, via {_repo_rel(successor, repo_root)}, "
                f"rung: {successor_rung})"
            )
        else:
            closed_unverified = True
            fm_text = replace_fm_field(fm_text, "deployment_state", "closed")
            fm_text = insert_fm_field(fm_text, "closed_reason", "stale", "deployment_state")
            comment = (
                f"# migration: DR-084 consumer-corpus REPAIR {today} "
                f"(was: deployment_state: superseded, invalid on any vocabulary — "
                f"normalized to abandoned per coordinator-claude's 2026-06-26 status retirement; "
                f"no successor found by a working-tree walk across all three rungs — "
                f"this is NOT a death claim, a successor deleted without archival "
                f"survives only as a git blob invisible to this walk; 'stale' is the "
                f"enum's nearest available value, check git history before trusting it)"
                if is_repair else
                f"# migration: DR-084 consumer-corpus re-expression {today} "
                f"(was: abandoned; no successor found by a working-tree walk across "
                f"reverse-lineage/superseded_by/deliverable_id-join — this is NOT a "
                f"death claim, a successor deleted without archival survives only as "
                f"a git blob invisible to this walk; 'stale' is the enum's nearest "
                f"available value, check git history before trusting it)"
            )
            fm_text = _insert_raw_line_after(fm_text, "closed_reason", comment)
            changes.append(
                f"{repair_prefix}closed + closed_reason: stale "
                "(no successor found via reverse-lineage, superseded_by, or "
                "deliverable_id-join — working-tree-walk negative result, not a "
                "death claim; see closed_unverified)"
            )

    if not changes:
        return None

    rebuilt = rebuild(split, fm_text)
    return {
        "path": _repo_rel(path, repo_root),
        "_abs_path": str(path),
        "changes": changes,
        "collisions": collisions,
        "dropped_comments": dropped_comments,
        "repair": is_repair,
        "closed_unverified": closed_unverified,
        "_rebuilt": rebuilt,
    }


def plan_migration(repo_root: str) -> Dict[str, Any]:
    """Scan ``repo_root``'s full handoff corpus and build a per-record migration
    plan. Pure read — never writes. Two-phase design (plan fully, THEN
    ``apply_migration`` writes): the reverse-lineage successor search
    (``_find_successor``) must see every OTHER record's ORIGINAL, pre-migration
    ``predecessor``/``additional_predecessors`` frontmatter — those field names
    are unaffected by this migration, but reading from disk mid-write-pass would
    still risk observing a partially-migrated corpus. Planning first avoids that
    entirely.

    ``closed_unverified`` (top-level key, mirrors ``repairs``): repo-relative
    paths of every record that fell through all three succession rungs to
    ``closed``+``closed_reason: stale``. Named as its own bucket, not just a
    per-record flag buried in ``changes``, so a receiving EM can find this
    exact set without reading every change line — see example-cockpit-repo's
    dr084 ask: a working-tree walk finding no successor is a different claim
    from "this work died" (a successor deleted without archival survives only
    as a git blob, invisible to any of the three rungs), and this bucket is
    where that residual — real but unverified — is meant to be looked up.
    """
    root = Path(repo_root).resolve()
    state_dir = root / "state" / "handoffs"
    all_paths = iter_handoff_files(root)
    equivalence_map = load_equivalence_map(root)

    records: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []

    for path in all_paths:
        try:
            plan = _plan_one(path, root, state_dir, all_paths, equivalence_map)
        except ValueError as exc:
            failures.append({"path": _repo_rel(path, root), "reason": str(exc)})
            continue
        except OSError as exc:
            failures.append({"path": _repo_rel(path, root), "reason": f"read error: {exc}"})
            continue
        if plan is not None:
            records.append(plan)

    repairs = [r["path"] for r in records if r["repair"]]
    closed_unverified = [r["path"] for r in records if r["closed_unverified"]]

    return {
        "repo_root": str(root),
        "records": records,
        "failures": failures,
        "repairs": repairs,
        "closed_unverified": closed_unverified,
    }


def apply_migration(plan: Dict[str, Any]) -> None:
    """Write every planned record's rebuilt text. Never called for a record with
    zero planned changes (``plan_migration`` excludes those) or for a failure
    (``failures`` entries carry no ``_rebuilt`` payload to write)."""
    for rec in plan["records"]:
        with open(rec["_abs_path"], "w", encoding="utf-8", newline="") as fh:
            fh.write(rec["_rebuilt"])


def _public_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Strip internal (``_``-prefixed) keys before the record crosses a public
    boundary (CLI stdout, JSON-RPC response)."""
    return {
        "path": rec["path"], "changes": rec["changes"], "collisions": rec["collisions"],
        "dropped_comments": rec["dropped_comments"], "repair": rec["repair"],
        "closed_unverified": rec["closed_unverified"],
    }


def _print_report(plan: Dict[str, Any], *, apply: bool) -> None:
    """Prints renames/splits and REPAIRS (invalid deployment_state: superseded
    values) as two visually distinct sections — a reviewer must never mistake a
    REPAIR of an invalid value for an ordinary old→new vocabulary rename."""
    verb = "Applying" if apply else "Would update"
    renames = [r for r in plan["records"] if not r["repair"]]
    repairs = [r for r in plan["records"] if r["repair"]]

    for rec in renames:
        sys.stdout.write(f"{verb} {rec['path']}\n")
        for c in rec["changes"]:
            sys.stdout.write(f"  {c}\n")
        for col in rec["collisions"]:
            sys.stdout.write(f"  COLLISION: {col}\n")
        for dropped in rec["dropped_comments"]:
            sys.stdout.write(f"  DROPPED COMMENT ({rec['path']}): {dropped}\n")

    if repairs:
        sys.stdout.write("\n=== REPAIRS (invalid deployment_state: superseded values) ===\n")
        for rec in repairs:
            sys.stdout.write(f"{verb} {rec['path']}\n")
            for c in rec["changes"]:
                sys.stdout.write(f"  {c}\n")
            for col in rec["collisions"]:
                sys.stdout.write(f"  COLLISION: {col}\n")
            for dropped in rec["dropped_comments"]:
                sys.stdout.write(f"  DROPPED COMMENT ({rec['path']}): {dropped}\n")

    sys.stdout.write(
        f"\n{len(renames)} record(s) with vocabulary changes, "
        f"{len(repairs)} record(s) repaired (invalid deployment_state), "
        f"{len(plan['failures'])} unclassifiable.\n"
    )
    if plan["closed_unverified"]:
        sys.stdout.write(
            f"\n{len(plan['closed_unverified'])} record(s) closed+stale on a "
            "working-tree-walk negative result (no successor found by this "
            "op's three rungs — NOT a positive death claim; a successor "
            "deleted without archival survives only as a git blob, invisible "
            "to this walk — check git history before trusting closed+stale):\n"
        )
        for path in plan["closed_unverified"]:
            sys.stdout.write(f"  {path}\n")
    for f in plan["failures"]:
        sys.stderr.write(f"FAIL {f['path']}: {f['reason']}\n")


def main(argv: List[str]) -> int:
    """CLI entry: ``migrate-handoff-vocabulary --root <path> [--apply]``.

    ``--root`` is REQUIRED (no cwd fallback, no ``__file__``-walking) — this op
    runs against an arbitrary repo, most often a consumer repo's tree, not the
    caller's own cwd. Exit code is non-zero whenever ANY record is unclassifiable
    (``failures`` non-empty), independent of ``--apply``/dry-run.
    """
    root: Optional[str] = None
    apply = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--apply":
            apply = True
        elif a == "--root":
            i += 1
            root = argv[i] if i < len(argv) else None
        else:
            sys.stderr.write(f"Unknown argument: {a}\n")
            return 1
        i += 1

    if not root:
        sys.stderr.write(f"{_PROG}: --root <path> is required\n")
        return 1

    plan = plan_migration(root)
    _print_report(plan, apply=apply)
    if apply:
        apply_migration(plan)
    return 1 if plan["failures"] else 0


@register_op("fleet.migrate_handoff_vocabulary")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """fleet.migrate_handoff_vocabulary — DR-084 consumer-corpus migration op.

    Dry-run by DEFAULT: ``params.apply`` must be explicitly ``true`` to write.

    Wire contract (own, no prior producer contract to extend):
        request  {"apply": bool (default false), "repo_root": str (optional,
                   D3 consistency check only — see check_repo_root)}
        response {"exit_code": 0|1, "applied": bool, "records": [{path,
                   changes[], collisions[], dropped_comments[], repair,
                   closed_unverified}], "failures": [{path, reason}],
                   "repairs": [path], "closed_unverified": [path]}
        exit_code 1 iff failures is non-empty, independent of apply.
        closed_unverified: paths closed+stale on a working-tree-walk negative
            result only — see plan_migration's docstring; not a positive
            death claim (a successor deleted without archival is invisible
            to this walk and survives only as a git blob).

    repo_root (handler arg) is the engine-supplied git common dir
    (_OP_KEY_SCOPE="common_dir", registered in op_scopes.py) — the main worktree
    is derived via ``main_worktree_root``, mirroring
    ``fleet.archive_completed_handoffs``'s own handler. params.repo_root is
    NEVER used as the scan-root source, only as the D3 mismatch check.
    """
    apply = bool(params.get("apply", False))

    if repo_root is None:
        return {
            "exit_code": 1, "applied": False, "records": [], "failures": [], "repairs": [],
            "closed_unverified": [], "error": "repo_root handler arg is None",
        }

    common_dir = Path(repo_root) if not isinstance(repo_root, Path) else repo_root
    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        return {
            "exit_code": 1, "applied": False, "records": [], "failures": [], "repairs": [],
            "closed_unverified": [], "error": mismatch,
        }

    worktree = main_worktree_root(common_dir)
    plan = await asyncio.to_thread(plan_migration, str(worktree))
    if apply:
        await asyncio.to_thread(apply_migration, plan)

    return {
        "exit_code": 1 if plan["failures"] else 0,
        "applied": apply,
        "records": [_public_record(r) for r in plan["records"]],
        "failures": plan["failures"],
        "repairs": plan["repairs"],
        "closed_unverified": plan["closed_unverified"],
    }


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
