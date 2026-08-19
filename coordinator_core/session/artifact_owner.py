"""
coordinator_core.session.artifact_owner — "who's on this?", keyed on an
artifact path.

Purpose: the resolver (`coordinator_core.session.reachability`) answers "how
do I reach the session that holds THIS uuid," keyed on a UUID you already
hold. The roster (`coordinator_core.session.peer_roster`) answers "who else
is around," keyed on a repo. Neither answers "who owns THIS artifact" --
handed an artifact path, this module extracts its recorded owner id(s) and
resolves each through `reachability.resolve_address()`. A thin composition
over the resolver (§ "What this covers" amendment,
`state/handoffs/2026-08-13-live-peer-roster.md` L57-62) -- the real work is
choosing the input surface and being honest when the holder is unreachable.

Spec backlink: `state/handoffs/2026-08-13-live-peer-roster.md`
§ "What this covers" amendment block (L52-62) and § Acceptance criteria
("The 'who's on this?' artifact-keyed read ships alongside the roster.").

Owner-id extraction (the real work -- do not silently pick one):
    An artifact can carry MORE than one recorded owner, and they are
    frequently DIFFERENT sessions (a handoff commonly carries both
    `claimed_by` and `authoring_session`, and the current baton this module
    itself ships in is exactly that case). This module returns every owner
    id it finds, each tagged with the field or record it came from, rather
    than collapsing to one. Six recording conventions, per `reachability`'s
    own docstring plus the claim-dir lookup added below:

        1. `claimed_by`           -- top-level frontmatter scalar.
        2. `claim_dir`            -- the artifact's OWN claim directory, at
                                     `<repo>/.git/coordinator-sessions/
                                     <class>-claims/<basename>/` (`class` in
                                     `handoff`|`memo`|`plan`), keyed on the
                                     artifact's BASENAME across all three
                                     classes -- `claims.claim_artifact`'s own
                                     claim identity (see
                                     `claims.relocate_artifact_claim`'s
                                     docstring for why basename-only identity
                                     matters, and NEVER infer the class from
                                     the artifact's own directory: a memo
                                     under `cross-repo/inbox` and a handoff
                                     under `state/handoffs` key the same way,
                                     and a rename can leave the two out of
                                     step). Owner id is that dir's
                                     `session_id` file; a class with no claim
                                     dir for this basename, or whose claim
                                     dir predates the `session_id` upgrade
                                     (legacy pid-only), emits no owner for
                                     that class. This is a SEPARATE on-disk
                                     record of CURRENT possession, not a
                                     frontmatter field on the artifact at
                                     all -- exactly why it outranks every
                                     provenance field (ordering rule, below).
        3. `authoring_session`    -- top-level frontmatter scalar.
        4. `created_by_session`   -- top-level frontmatter scalar.
        5. `agent_sessions`       -- nested frontmatter list, entries encoded
                                     `"<session_id>|<status>|<created_at>"`
                                     (`coordinator_core.ops.completion_ops`'s
                                     own write-side contract); one owner
                                     entry is emitted per list item, tagged
                                     `agent_sessions`.
        6. `state/subagent-share/<id>/` directory-name convention -- when
           the artifact path itself sits under a `state/subagent-share/`
           directory, the `<id>` path segment immediately following it is
           an owner id, tagged `subagent_share_dir`.

    Each `claim_dir` owner additionally carries its OWN liveness verdict
    (`OwnerRecord.claim_live`, from `liveness.claim_holder_live` on that
    claim dir) and its stage (`OwnerRecord.claim_stage`, `brief` or
    `apply`) -- a signal distinct from, and never collapsed into,
    `resolve_address`'s reachability outcome on the same owner id: a claim
    can be LIVE (its holder's session is running) while `resolve_address`
    still answers `not_reachable` for that same id (no messaging address
    currently resolvable), and the two facts answer different questions.
    Every other convention leaves `claim_live`/`claim_stage` as `None` --
    they name no claim dir of their own to ask.

    Ordering rule (when a caller wants a single "current holder"):
    `claimed_by` leads -- it is the current holder recorded ON the artifact
    itself. `claim_dir` sits directly behind it and AHEAD of every
    provenance field (`authoring_session`, `created_by_session`,
    `agent_sessions`, `subagent_share_dir`): a claim dir records CURRENT
    possession at the session-hub level -- the same altitude `claimed_by`
    answers at, just via a separate record -- so both outrank a field that
    only says who authored or last touched the artifact. This module itself
    never picks for the caller; `owners` is returned in this fixed order
    (`claimed_by`, `claim_dir` entries in class order `handoff`/`memo`/
    `plan`, `authoring_session`, `created_by_session`, `agent_sessions`
    entries in file order, `subagent_share_dir`) so a caller that wants
    "the" owner can safely take `owners[0]`, but every entry is returned
    regardless.

Negative-spec:
    - A `not_reachable` owner reads as "recorded owner, not currently
      reachable" -- NEVER as "unowned," "stale," or "free to take." This is
      the single most important behaviour in this module, and a `claim_dir`
      owner is exactly where a caller is most tempted to misread
      `not_reachable` as permission to take the claim: a stale claim reads
      as a stale claim, never as absence of an owner. `resolve_one`'s caller
      must not be able to read this output as permission to take a claim;
      do not summarize, flatten, or rename this outcome anywhere in this
      module or its op veneer.
    - `ambiguous` and `not_reachable` are never collapsed into a boolean
      "reachable"/"unreachable" -- the four-outcome contract from
      `reachability.resolve_address` is surfaced verbatim, unchanged.
    - An artifact with no recognised owner field is a distinct, explicit
      outcome (`owners == []`) -- never an error, never silently identical
      to "unreachable" (which requires a recorded-but-dead owner to exist).
    - Never re-derives an address or a ref -- every resolution is a pass-
      through call to `reachability.resolve_address`; this module holds no
      hash/ref logic of its own. Likewise never re-derives the claim-dir
      path by hand -- calls/mirrors `claims._claim_base`, the same seam
      `claims.claim_artifact` itself resolves through.
    - Never parses YAML frontmatter by hand -- uses
      `coordinator_core.frontmatter.primitives.split_frontmatter`,
      `read_fm_field_unquoted`, and `read_fm_nested_field` exclusively (same
      seam `coordinator_core/frontmatter/` already provides). Scalar owner
      ids use the `_unquoted` reader specifically -- these values are
      compared/looked-up as session ids (`reachability.resolve_address`),
      not merely echoed, so a literal-quoted YAML scalar (`claimed_by:
      "abc-123"`) must not leak its quote marks into the id used for lookup
      (2026-08-13 fix -- a quoted scalar owner previously resolved
      `not_reachable` for a live session because the quotes were compared
      literally).
    - Never auto-messages, schedules, or assigns work -- a read only, per
      the handoff's Anti-scope ("It is a read, never work assignment").
    - Missing/unreadable file, or a file with no parseable frontmatter,
      degrades every FRONTMATTER-sourced convention to contribute no
      owners, with `file_error` set on a read failure -- but the claim-dir
      lookup does NOT sit behind a successful frontmatter read: claim
      identity is basename-only and independent of the artifact's own
      contents, so a missing/unreadable/frontmatter-less artifact still
      resolves its `claim_dir` owner(s), and `owners` is no longer forced
      empty by a read failure. Never raises to the caller either way,
      matching `reachability.resolve_address`'s own advisory-read
      discipline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from coordinator_core.frontmatter.primitives import (
    read_fm_field_unquoted,
    read_fm_nested_field,
    split_frontmatter,
)
from coordinator_core.session import claims, liveness, reachability

#: `claimed_by` is handled separately (it leads the ordering, § docstring) --
#: this constant covers only the two remaining top-level scalar conventions.
_SCALAR_OWNER_FIELDS = ("authoring_session", "created_by_session")

#: Claim classes probed for a `claim_dir` owner, per `claims.claim_artifact`
#: -- basename-only identity across all three, never inferred from the
#: artifact's own directory (module docstring, convention 2).
_CLAIM_DIR_CLASSES = ("handoff", "memo", "plan")

#: Claim classes whose dir is keyed on the artifact's STEM, not its full
#: basename. `claims.claim_plan` REFUSES a `.md`-suffixed slug outright
#: (`cs_claim_plan: expected a bare plan slug`), so every plan claim dir on
#: disk is stem-keyed while handoff/memo dirs keep the extension. Probing
#: all three with one spelling silently misses the whole plan class -- the
#: "who owns this plan?" read returned no owners for plans that were in
#: fact claimed. Keyed off the writer's own contract, not a guess.
_STEM_KEYED_CLAIM_CLASSES = frozenset({"plan"})

_AGENT_SESSIONS_ENTRY_RE = re.compile(r'^\s*-\s*["\']?([^"\'|]+)')

_SUBAGENT_SHARE_DIR_RE = re.compile(r'(?:^|[/\\])state[/\\]subagent-share[/\\]([^/\\]+)(?:[/\\]|$)')


def _basename_cross_platform(artifact_path: str) -> str:
    """Basename of `artifact_path`, separator-flavor-independent.

    `os.path.basename` only recognises the HOST platform's separator (a
    single `/` on POSIX), so a Windows-style path (`state\\handoffs\\foo.md`)
    handed to a POSIX-running engine process would silently fail to reduce
    to `foo.md` -- no exception, just a claim-dir lookup that misses. Follows
    this module's own `_SUBAGENT_SHARE_DIR_RE` precedent of treating both
    `/` and `\\` as separators regardless of host OS (Windows is first-class,
    per project doctrine).

    Tradeoff (Review: coordinator:code-reviewer): a genuine POSIX filename
    containing a literal backslash byte is split on that byte too, same as
    `_SUBAGENT_SHARE_DIR_RE`'s existing precedent -- this module's own
    artifact universe (machine-authored `YYYY-MM-DD-*.md` slugs) never
    contains one, so the tradeoff is low-risk but unstated until now.
    """
    return re.split(r'[/\\]', artifact_path)[-1]


@dataclass(frozen=True)
class OwnerRecord:
    """One extracted owner id, tagged with the frontmatter field/convention
    it came from -- never collapsed with any other owner found on the same
    artifact (module docstring's "do not silently pick one").

    `claim_live`/`claim_stage` are populated ONLY for `source_field ==
    "claim_dir"` -- the claim dir's own liveness verdict
    (`liveness.claim_holder_live`) and stage (`brief`/`apply`), a signal
    distinct from `resolve_address`'s reachability outcome on the same id
    and never collapsed into it (AC2, module docstring). Every other
    convention leaves both `None`: it names no claim dir of its own to ask.
    """

    session_id: str
    source_field: str
    claim_live: Optional[bool] = None
    claim_stage: Optional[str] = None


@dataclass(frozen=True)
class OwnerResolution:
    """One `OwnerRecord` paired with its `reachability.resolve_address()`
    outcome, surfaced verbatim -- see module docstring's negative-spec on
    never collapsing `not_reachable`/`ambiguous` into a boolean."""

    owner: OwnerRecord
    result: "reachability.ResolveResult"


@dataclass(frozen=True)
class ArtifactOwnerResult:
    """The whole answer for one artifact path.

    `owners` is `[]` -- not `None`, not an error -- when the artifact has no
    recognised owner field; a distinct, explicit outcome from a read/parse
    failure, which instead sets `file_error`.
    """

    artifact_path: str
    owners: List[OwnerResolution] = field(default_factory=list)
    file_error: Optional[str] = None


def _extract_agent_sessions_ids(fm: str) -> List[str]:
    """Every `session_id` named by an `agent_sessions:` nested-block entry,
    in file order.

    Entries are encoded `"<session_id>|<status>|<created_at>"`
    (`coordinator_core.ops.completion_ops._apply_session_append`'s own
    write-side contract) -- this only reads the first `|`-delimited field,
    same as that module's own idempotency check does on the write side.
    """
    block = read_fm_nested_field(fm, "agent_sessions")
    if not block:
        return []
    ids: List[str] = []
    for line in block.splitlines():
        m = _AGENT_SESSIONS_ENTRY_RE.match(line)
        if m:
            ids.append(m.group(1).strip())
    return ids


def _extract_subagent_share_dir_id(artifact_path: str) -> Optional[str]:
    """The `<id>` path segment immediately after `state/subagent-share/`,
    when `artifact_path` sits under one -- the sixth recording convention
    (session directory name, not a frontmatter field)."""
    m = _SUBAGENT_SHARE_DIR_RE.search(artifact_path)
    return m.group(1) if m else None


def _extract_claim_dir_owners(artifact_path: str, cwd: Optional[str] = None) -> List[OwnerRecord]:
    """Every `claim_dir`-tagged owner recorded for `artifact_path`'s
    BASENAME, probed across all three claim classes in fixed order
    (`handoff`, `memo`, `plan`) -- the second recording convention (module
    docstring). Basename-only, per `claims.claim_artifact`'s own claim
    identity -- never inferred from `artifact_path`'s directory.

    Does not sit behind frontmatter parsing (AC5): claim identity depends
    only on the basename and the on-disk claim dir, never on the artifact's
    own contents, so this is safe to call on a missing/unreadable file.

    Resolves the claim base via `claims._claim_base` -- the same seam
    `claims.claim_artifact` itself resolves through -- rather than
    re-deriving the `.git/coordinator-sessions` join by hand (negative-
    spec). Returns `[]` when the base is unresolvable (not a git repo) or
    when no class has a claim dir for this basename.
    """
    basename = _basename_cross_platform(artifact_path)
    if not basename:
        return []

    owners: List[OwnerRecord] = []
    # Review: coordinator:code-reviewer -- `_claim_base`'s `class_` param is
    # only used inside its baton-repo diagnostic branch, and this call always
    # takes the legacy (baton_repo_root="") path where it's inert; all three
    # classes therefore resolve to the same base, so it is resolved ONCE
    # (using the first real class, keeping the call honest for a future
    # reader even though it's a no-op today) rather than once per iteration
    # -- the loop previously re-called this per class, and on the production
    # no-cwd path (`core.sessions_dir(cwd=None)` is deliberately never
    # cached) that meant 3 uncached `git rev-parse` subprocess spawns per
    # `resolve_artifact_owner` call instead of one.
    base = claims._claim_base(_CLAIM_DIR_CLASSES[0], "", cwd)
    if not base:
        return owners
    stem = basename[:-3] if basename.endswith(".md") else basename
    for class_ in _CLAIM_DIR_CLASSES:
        key = stem if class_ in _STEM_KEYED_CLAIM_CLASSES else basename
        claim_dir = Path(base) / f"{class_}-claims" / key
        if not claim_dir.is_dir():
            continue
        sid = claims._read_claim_field(claim_dir, "session_id")
        if not sid:
            # Legacy pid-only claim dir (pre `session_id` upgrade) -- no
            # session id to key an owner on under this convention.
            continue
        owners.append(
            OwnerRecord(
                session_id=sid,
                source_field="claim_dir",
                claim_live=liveness.claim_holder_live(str(claim_dir), cwd),
                claim_stage=claims.claim_stage(claim_dir),
            )
        )
    return owners


def extract_owners(artifact_path: str, file_text: str, cwd: Optional[str] = None) -> List[OwnerRecord]:
    """Every owner id recorded on `artifact_path`, tagged by source field,
    in the fixed order documented on this module (`claimed_by`, `claim_dir`
    entries in class order, `authoring_session`, `created_by_session`,
    `agent_sessions` entries in file order, `subagent_share_dir`).

    Returns `[]` -- not `None` -- when `file_text` has no parseable
    frontmatter, no recognised owner field, and no claim dir exists; that is
    a distinct, explicit "no owner recorded" outcome, not an error (module
    docstring negative-spec). The `claim_dir` convention is independent of
    `file_text` entirely (AC5) -- it still resolves when `file_text` is
    empty (unreadable source file) or has no frontmatter.
    """
    owners: List[OwnerRecord] = []

    split = split_frontmatter(file_text)
    claimed_by = read_fm_field_unquoted(split.fm_text, "claimed_by") if split is not None else None
    if claimed_by:
        owners.append(OwnerRecord(session_id=claimed_by, source_field="claimed_by"))

    # Deliberately OUTSIDE the `if split is not None:` guard below (AC5):
    # claim identity is basename-only and independent of a successful
    # frontmatter read, so this must not be moved inside that block.
    owners.extend(_extract_claim_dir_owners(artifact_path, cwd))

    if split is not None:
        fm = split.fm_text
        for field_name in _SCALAR_OWNER_FIELDS:
            value = read_fm_field_unquoted(fm, field_name)
            if value:
                owners.append(OwnerRecord(session_id=value, source_field=field_name))
        for sid in _extract_agent_sessions_ids(fm):
            if sid:
                owners.append(OwnerRecord(session_id=sid, source_field="agent_sessions"))

    dir_id = _extract_subagent_share_dir_id(artifact_path)
    if dir_id:
        owners.append(OwnerRecord(session_id=dir_id, source_field="subagent_share_dir"))

    return owners


def resolve_artifact_owner(artifact_path: str, cwd: Optional[str] = None) -> ArtifactOwnerResult:
    """Read `artifact_path`, extract every recorded owner id, and resolve
    each through `reachability.resolve_address()` -- surfaced verbatim, one
    `OwnerResolution` per owner found.

    A read/parse failure (missing file, unreadable, no frontmatter) sets
    `file_error` -- but, unlike the five frontmatter/path conventions, does
    NOT force `owners` empty (AC5): the `claim_dir` convention is keyed on
    the basename alone and resolves independently of a successful file
    read, so a missing or frontmatter-less artifact with a live claim still
    reports that owner. Never raises, matching `reachability.resolve_
    address`'s own advisory-read discipline. An artifact that reads fine
    but names no owner field and has no claim dir ALSO returns `owners=[]`,
    with `file_error=None` -- the two empty cases are distinguishable only
    via `file_error`, never conflated.
    """
    file_error: Optional[str] = None
    try:
        with open(artifact_path, "r", encoding="utf-8") as fh:
            file_text = fh.read()
    except OSError as exc:
        file_text = ""
        file_error = str(exc)

    owner_records = extract_owners(artifact_path, file_text, cwd)

    resolutions = [
        OwnerResolution(owner=owner, result=reachability.resolve_address(owner.session_id))
        for owner in owner_records
    ]

    return ArtifactOwnerResult(artifact_path=artifact_path, owners=resolutions, file_error=file_error)
