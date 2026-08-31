"""coordinator_core.git.commit_trailers -- Session-Id / Deliverable-Id
trailer computation, shared between the `prepare-commit-msg` git hook
(`coordinator/bin/coordinator-prepare-commit-msg`) and any commit mechanism
that bypasses git hooks entirely.

Why this module exists (AC18, docs/plans/2026-07-27-computed-commit-
mechanism-selection.md chunk C10-remainder): `git_native.commit_scoped()`'s
diverged-path branch lands a commit via `git commit-tree` + `update-ref` --
plumbing commands that run NO git hooks, `prepare-commit-msg` included. A
commit landed through that branch would silently carry neither trailer,
while an otherwise-identical commit through the agree branch (plain
`git commit`, hooks fire normally) keeps both. That is a silent divergence
in commit provenance keyed on an implementation detail (did the path set
happen to be partial-staged) the committer has no visibility into.

`compute_missing_trailer_args()` is the resolution logic the hook script
performs, extracted so a hook-bypassing commit mechanism can replay it
explicitly rather than reimplementing (and inevitably drifting from) a
second copy. The git hook at `coordinator/bin/coordinator-prepare-commit-msg`
is NOT changed to import this module -- it is a `sh`-invoked, extensionless
entrypoint script installed verbatim into `.git/hooks/`, not a package
member, and editing a live hook script mid-session on a tree other agents
are actively committing into is its own hazard independent of this dedup.
Its resolution logic (session-id two-tier env ladder + UUID fail-safe +
deliverable-id lookup + idempotent trailer-line check) is mirrored here
verbatim; a change to one must be mirrored in the other by hand until the
hook script itself is refactored to import this module. That residual
duplication is a known, named gap -- not silently reintroducing the
anti-pattern this module exists to close for the commit-tree path.

Spec backlink: docs/plans/2026-07-27-computed-commit-mechanism-selection.md
chunk C10-remainder (AC18).
Mirrors: coordinator/bin/coordinator-prepare-commit-msg (resolution logic,
verbatim ladder + fail-safe semantics -- including the 2026-07-27
cross-repo Deliverable-Id fallback added to both files in the same change:
`_resolve_deliverable_id()` now re-checks DoE-claude's own git-dir, located
via the `.doe-root` pointer convention, when the local git-dir's
`session-shape.json` lookup misses -- the structural miss for every commit
landed directly into claude-klabauter under the DoE->claude-klabauter cross-repo write
grant, since `session-shape.json` is written wherever `/pickup` actually
ran (almost always DoE-claude), not wherever the eventual commit lands --
and the 2026-08-01 claimed-plan tier added to both files in this same
change: when neither `session-shape.json` lookup yields a value,
`_resolve_deliverable_id()` falls back to the session's claimed PLAN, the
same-session plan-execute-without-a-handoff door named as a residual in
`archive/specs/2026-08/2026-08-01-deliverable-id-carry-onto-executing-
handoff.md`'s execution note and closed here per DR-207 DD#1). The
2026-08-04 artifact-first tier (`compute_missing_trailer_args(..., paths=
...)` -> `_resolve_deliverable_id_from_paths()`, tier 0, checked BEFORE
every session-keyed tier) is this module's own addition, NOT yet mirrored
into the hook script -- closes a cross-repo-reported defect (market-
intelligence-em -> claude-klabauter-em, 2026-08-04 memo, defect 2): every
tier below tier 0 is keyed on the SESSION, which is wrong the moment a
session holds more than one deliverable at once (`/pickup a AND b AND c`,
the pickup skill's documented Multi-Artifact Grab shape) -- a multi-baton
session's commits all resolved to whichever deliverable's session-shape
tier happened to answer last, mis-attributing every commit but one. Tier 0
resolves from the COMMITTED ARTIFACT's own `deliverable_id` frontmatter
first, falling through to the untouched session ladder only when the
artifact carries none. The hook script has no `paths` argument to receive
(it is not called from Python), so mirroring tier 0 there needs a hook-side
`git diff --cached --name-only` leg instead -- tracked as a known residual
of the existing hand-mirroring gap, not silently reintroduced as a NEW one.

`Session-Id` names the COMMITTER, and that is all it claims. The
`Absorbed-From` authorship qualifier that once ran beside it was killed
2026-08-19 (kill-ledger K-008): it had no reader, and the PM's ruling is
that commit attribution serves review enforcement and plan-unit
legibility only -- catching a swept stray is explicitly not a goal. See
`docs/reference/commit-trailer-contract.md` for the surviving fields.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence, Union

from coordinator_core.doe_root_pointer import read_doe_root_pointer_file
from coordinator_core.git import repo_root as _repo_root_seam
from coordinator_core.session import core as _session_core

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _resolve_git_dir(cwd: Union[str, Path]) -> str:
    """`git rev-parse --git-dir`, scoped to `cwd`. Returns "" on any failure
    (git missing, not a repo, spawn error, timeout) -- mirrors the hook's
    own degrade-gracefully contract."""
    git_dir = _repo_root_seam.git_dir(cwd=str(cwd)) or ""
    if not git_dir:
        return ""
    # `git rev-parse --git-dir` may return a path relative to `cwd`
    # ("./.git", ".git", "../.git") -- resolve it against `cwd` so callers
    # get a path usable regardless of the process's own cwd.
    git_dir_path = Path(git_dir)
    if not git_dir_path.is_absolute():
        git_dir_path = Path(cwd) / git_dir_path
    return str(git_dir_path)


def _resolve_session_id(git_dir: str) -> str:
    """Delegates to ``coordinator_core.session.core.resolve_session_id``
    (KS-6, 2026-08-07): the full 3-tier ``SESSION_ENV_PRECEDENCE`` ladder
    (``COORDINATOR_SESSION_ID``, ``CLAUDE_SESSION_ID``,
    ``CLAUDE_CODE_SESSION_ID``), widened from the prior 2-tier (legacy env
    -> platform env) chain to match the canonical reference -- see
    ``SESSION_ENV_PRECEDENCE``'s own docstring for the prior break-class
    defect two disagreeing copies of this ladder caused.

    Tier 3->4 (the `<git_dir>/coordinator-sessions/.current-session-id`
    sentinel file, plus its liveness gate `_sentinel_session_live`) was
    REMOVED here, not merely gated -- KS-1. Two independent reasons: (1) it
    is unsound by construction under this fleet's concurrency, documented
    as last-writer-wins in `coordinator_core/bash_guards
    /guard_inprocess_search.py` ~L84 -- ~18 concurrent sessions on one
    shared worktree means even a freshly-written sentinel hands session A
    the id of whichever session wrote last, so a liveness gate only makes
    it confidently wrong rather than obviously wrong; (2) its writer,
    `session-init.py` (DoE-claude SessionStart hook), was deleted by PM
    directive 2026-07-15 ("full-kill-keep-fast-orientation") -- no
    production writer survives anywhere. `git_dir` is accepted (and
    resolved by callers) purely for the Deliverable-Id lookups below, which
    still need it. `_sentinel_session_live` (added by dd6ffcbcc to gate
    this now-deleted tier) was removed alongside it -- do not restore
    either without a new writer for the sentinel file.

    Mirrored by hand in `coordinator/bin/coordinator-prepare-commit-msg`'s
    own `_resolve_session_id` (that script cannot cheaply import
    `coordinator_core` on its hot commit-hook path) -- this function,
    delegating to `core.resolve_session_id`, is that mirror's source of
    truth; a change here must be mirrored there too. The warm-served branch
    below needs NO mirror: that hook script runs in the committing session's
    own cold process and is never served by the warm server, so
    `in_warm_served_request()` is False there by construction.

    WARM-SERVED CALLS DO NOT REACH THE ENVIRONMENT TIERS. Inside a warm
    dispatch this process's `os.environ` belongs to whoever spawned the
    server, not to the session being served, so degrading to it stamps a
    stranger's id into another session's commit -- measured across three
    repos on 2026-08-29 (`state/bug-backlog/2026-08-29-the-warm-door-s-exe-
    route-stamps-the-ser-47373b19c77e.yaml`). Warm therefore resolves from
    tier 0 alone and returns EMPTY when the caller carried nothing, which
    `compute_missing_trailer_args` turns into an omitted trailer. An absent
    Session-Id is recoverable and honest; a confidently wrong one is neither,
    and is what made the 2026-08-29 window unusable as an attribution key.
    Cold is untouched: `os.environ` there IS the caller's own."""
    # Review: overengineering-reviewer (finding 2) — routed through the one
    # shared accessor (session.core.attributable_session_id) rather than
    # re-deriving the warm/cold branch here.
    return _session_core.attributable_session_id()


def _resolve_doe_root() -> str:
    """Locate DoE-claude's repo root via the `.doe-root` pointer convention
    (settings-home machine-local pointer first, then legacy `~/.claude`).
    Returns "" if neither resolves. No subprocess spawn -- verbatim parity
    with the hook's `_resolve_doe_root()` (2026-07-27 cross-repo-fallback
    mirror)."""
    # `expanduser("~")` passed explicitly rather than letting the helper default
    # to ${CLAUDE_HOME:-$HOME}: this runs from a git hook, where CLAUDE_HOME may
    # be set to something unrelated to the pointer's home. Behavior-identical to
    # the inline read this replaced.
    return read_doe_root_pointer_file(os.path.expanduser("~"))


def _resolve_deliverable_id_at(git_dir: str, session_id: str) -> str:
    """Read `<git_dir>/coordinator-sessions/<session_id>/session-shape.json`
    and return `pickup.deliverable_id` if present and non-blank, else "".
    Omit-rather-than-guess, verbatim parity with the hook."""
    if not git_dir or not session_id:
        return ""
    shape_path = os.path.join(
        git_dir, "coordinator-sessions", session_id, "session-shape.json"
    )
    try:
        with open(shape_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    pickup = data.get("pickup")
    if not isinstance(pickup, dict):
        return ""
    deliverable_id = pickup.get("deliverable_id")
    if isinstance(deliverable_id, str) and deliverable_id.strip():
        return deliverable_id.strip()
    return ""


def _list_held_plan_claims(cwd: Union[str, Path]) -> List[tuple]:
    """Lazy-imported wrapper over ``coordinator_core.session.claimed_plan.
    list_held_plan_claims`` (C1a) -- the shared enumeration both the
    scope-match tier and the ambiguity gate consume. ``claimed_plan`` is NOT
    under ``coordinator_core.ops`` (see that module's own negative-spec), so
    this import does not itself trigger the ~161-module eager sweep; it is
    still kept function-local, matching this module's existing lazy-import
    convention for every session-keyed lookup (see
    ``_resolve_deliverable_id_from_claimed_plan`` below). Never raises --
    an import failure or any exception from the callee both degrade to
    ``[]``, the same omit-rather-than-guess contract ``list_held_plan_claims``
    itself documents."""
    try:
        from coordinator_core.session.claimed_plan import list_held_plan_claims

        return list_held_plan_claims(cwd)
    except Exception:
        return []


def _normalize_committed_path(raw_path: str, cwd: Union[str, Path]) -> str:
    """Normalize one entry of a commit's pathspec to the same shape
    ``scope:`` frontmatter entries are authored in: repo-relative,
    forward-slash-separated. An absolute path is made relative to ``cwd``
    when possible; a path that cannot be related to ``cwd`` (rare -- a
    caller-supplied path genuinely outside the tree) is returned with
    separators normalized only, so it simply fails to match any scope entry
    rather than raising. On a case-insensitive Windows filesystem, a ``cwd``
    and incoming absolute path differing only in drive-letter or segment
    casing also hits the "cannot relate" branch (``relative_to`` raises
    ``ValueError`` on a case-sensitive string comparison) and abstains the
    same way -- fail-safe direction, flagged for awareness only (review-
    integrator P3, slice B, coordinatorcode-reviewer-f5f569aa.md)."""
    path = Path(raw_path)
    if path.is_absolute():
        try:
            path = path.relative_to(Path(cwd))
        except ValueError:
            pass
    return str(path).replace("\\", "/")


def _normalize_scope_path(raw_path: str) -> str:
    """Normalize one ``scope:`` frontmatter entry to the same shape
    ``_normalize_committed_path`` produces for a commit's pathspec:
    forward-slash-separated, no leading ``./``, no trailing ``/``. A
    ``scope:`` list is plan-author-written text, not machine-normalized
    like the committed pathspec is, so an entry authored with a leading
    ``./``, a trailing slash, or (rare on this codebase, but cheap to
    handle) backslash separators would otherwise never match a normalized
    committed path and the strict-containment check in
    ``resolve_deliverable_id_from_scope_match`` would silently abstain --
    see that finding (review-integrator P2, slice B,
    coordinatorcode-reviewer-f5f569aa.md) for the full asymmetry. Never
    raises; a blank/whitespace-only entry normalizes to ``""``."""
    normalized = raw_path.strip().replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if len(normalized) > 1:
        normalized = normalized.rstrip("/")
    return normalized


def _read_plan_scope_paths(cwd: Union[str, Path], plan_path: str) -> List[str]:
    """The claimed plan's own ``scope:`` frontmatter list, via the shared
    scanner (``coordinator_core.ops.extract_scope_paths``) rather than a
    second hand-rolled copy -- same convention ``ops.dirty_tree_gate``
    already established for the handoff-scope analogue of this read.
    Imported lazily (function-local), matching this module's own
    ops-import convention: a module-scope import of anything under
    ``coordinator_core.ops`` triggers that package's eager ~161-module
    sweep. Returns ``[]`` on any read/parse failure -- never raises.

    Each entry is run through ``_normalize_scope_path`` before being
    returned, so the set this feeds into
    ``resolve_deliverable_id_from_scope_match``'s strict-containment check
    is normalized on the SAME shape ``_normalize_committed_path`` produces
    for the commit's own pathspec -- both sides of that comparison share
    one normalization convention rather than trusting ``scope:`` authors
    to already write the canonical form."""
    from coordinator_core.ops.extract_scope_paths import _extract_scope_paths

    full_path = Path(cwd) / plan_path
    try:
        text = full_path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [_normalize_scope_path(p) for p in _extract_scope_paths(text)]


def _path_covered_by_scope_entry(scope_entry: str, normalized_path: str) -> bool:
    """True iff ``normalized_path`` (already ``_normalize_committed_path``-
    shaped) is covered by ``scope_entry`` (already ``_normalize_scope_path``-
    shaped): either they are EQUAL, or ``scope_entry`` names a directory that
    is a proper ancestor of ``normalized_path``.

    Ancestor containment is checked on ``/``-split path SEGMENTS, never a
    bare ``str.startswith`` -- a naive prefix check would let scope entry
    ``coordinator_core/ops/fleet/tests`` match the unrelated sibling
    ``coordinator_core/ops/fleet/tests_helper.py`` (both start with the same
    characters; only one is actually beneath the other as a directory).
    """
    if not scope_entry or not normalized_path:
        return False
    if scope_entry == normalized_path:
        return True
    entry_segments = scope_entry.split("/")
    path_segments = normalized_path.split("/")
    if len(path_segments) <= len(entry_segments):
        return False
    return path_segments[: len(entry_segments)] == entry_segments


def resolve_deliverable_id_from_scope_match(
    cwd: Union[str, Path],
    paths: Optional[Sequence[str]],
    claims: Sequence[tuple],
) -> str:
    """NEW scope-match tier (C2, spec § (1)): resolve a code-only commit's
    ``Deliverable-Id`` by matching its pathspec against the ``scope:`` of
    every plan THIS session holds a claim on (``claims``, C1a's
    ``list_held_plan_claims`` shape -- ``[(plan_path, claimed_at), ...]``),
    rather than any session-keyed tier below it.

    "Covered" is STRICT: every entry of ``paths`` (normalized to the same
    repo-relative, forward-slash shape ``scope:`` entries are authored in --
    see ``_normalize_committed_path``) must be a member of that ONE plan's
    scope list. If the pathspec is strictly covered by exactly ONE claimed
    plan, that plan's own ``deliverable_id`` frontmatter value is returned.
    Zero covering plans OR two-or-more covering plans both mean "this tier
    has nothing to say" -- returns ``""`` and the caller falls through.
    NEVER picks among multiple covering plans, and never partial-matches (a
    pathspec straddling two plans' scopes, or spilling outside every claimed
    plan's scope, abstains rather than guessing which plan it belongs to).

    Standalone and importable independent of the rest of `_resolve_deliverable_id`'s
    ladder (AC4/AC11) -- callers (this module's own ladder, and C4) pass
    ``claims`` in rather than this function re-deriving them, so it carries
    no dependency on session-keyed state beyond what it is given.

    Deliberately measured to abstain often: this is the accepted cost of a
    strict-covered predicate that produces zero over-matches on real data,
    not a shortfall to relax.

    "Covered" INCLUDES directory-prefix containment, not just exact
    membership -- a `scope:` entry is routinely a DIRECTORY (the plan
    scaffold's own comment invites `path/or/item/one`), and a path beneath a
    plan's declared directory scope IS in that plan's scope by the plain
    meaning of the entry. `_path_covered_by_scope_entry` is the sole
    containment check: exact-equal, OR the entry is a proper ancestor
    directory of the path (compared on `/`-joined path SEGMENTS, never a
    bare `str.startswith`, which would wrongly let scope entry
    `coordinator_core/ops/fleet/tests` match committed path
    `coordinator_core/ops/fleet/tests_helper.py`). This widening does NOT
    relax the strictness the earlier note protects -- that strictness is
    CROSS-PLAN (never guess which plan a straddling pathspec belongs to):
    every committed path must still be covered by ONE plan, and two-or-more
    covering plans still abstain (see the duplicate-counting note below).
    Directory-prefix containment only changes what "covered by" means for a
    single entry against a single path; it never lets a pathspec straddling
    two plans, or spilling outside every claimed plan's scope, resolve.
    """
    if not paths or not claims:
        return ""

    normalized_paths = [_normalize_committed_path(p, cwd) for p in paths]

    covering_deliverable_ids: List[str] = []
    for plan_path, _claimed_at in claims:
        scope_paths = set(_read_plan_scope_paths(cwd, plan_path))
        if not scope_paths:
            continue
        if all(
            any(_path_covered_by_scope_entry(entry, p) for entry in scope_paths)
            for p in normalized_paths
        ):
            covering_deliverable_ids.append(
                _read_deliverable_id_from_frontmatter(Path(cwd) / plan_path)
            )

    # NOTE: counts DUPLICATES, not distinct values -- two claimed plans that
    # both cover this pathspec and agree on the same deliverable_id still
    # abstain here, per spec § (1)'s literal "zero or two-or-more covering
    # plans" reading (review-integrator P3, slice B,
    # coordinatorcode-reviewer-f5f569aa.md). A plausible false-negative in a
    # genuinely unambiguous case (a plan claimed twice, or two sibling plans
    # sharing a deliverable_id by design) -- left as-is, not a deviation.
    if len(covering_deliverable_ids) != 1:
        return ""
    return covering_deliverable_ids[0]


def session_holds_multiple_plan_claims(claims: Sequence[tuple]) -> bool:
    """The ambiguity-gate predicate (C2, spec § (2)): does this session hold
    more than one plan claim, per C1a's ``list_held_plan_claims`` enumeration
    (``claims``, the same ``[(plan_path, claimed_at), ...]`` shape). A pure
    predicate over what it is given -- no re-derivation of the enumeration
    itself, so a caller (this module's own ladder, and C4) that already has
    ``claims`` in hand does not pay for a second lookup.

    ``_resolve_deliverable_id_at`` (the ``session-shape.json`` pickup tier)
    and ``_resolve_deliverable_id_from_claimed_plan`` (the claimed-plan
    tier) both answer ONLY when this predicate is False -- see
    ``_resolve_deliverable_id``'s own ladder. True means "omit, do not
    guess": holding two-or-more plan claims is a legitimate supported shape
    (``/pickup a AND b``), not an error, and neither session-keyed tier can
    tell which of the held claims a given commit is actually for.
    """
    return len(claims) > 1


def _resolve_deliverable_id_from_claimed_plan(cwd: Union[str, Path]) -> str:
    """Tier-3 fallback: the same-session plan-execute path (no handoff).

    `pickup.deliverable_id` (tiers 1/1a above) is populated ONLY by
    `record_pickup` on a `/pickup` of a HANDOFF. A session that claims a
    PLAN directly and executes it -- no handoff ever authored -- never
    writes that key, so every chunk commit that session makes carries a
    stale or absent `Deliverable-Id`, and `close_out_and_stamp` (joining on
    exact equality with the plan's own `deliverable_id`) never sees them.

    `archive/specs/2026-08/2026-08-01-deliverable-id-carry-onto-executing-
    handoff.md`'s execution note names this residual explicitly and records
    its own Anti-scope ("do NOT change the commit-trailer resolvers") as
    reasoning that assumed every execution is handoff-mediated -- an
    assumption that note itself calls incomplete. This function is the
    deliberate, documented reversal of that anti-scope for the same-session
    case; read that execution note before treating this tier as scope creep.
    Spec backlink: DR-207 DD#1 (mint once at the earliest artifact, carry
    verbatim downstream -- a claimed plan already IS that earliest artifact
    when no handoff intervenes).

    Resolves the claimed plan via the shared, already-verified
    `resolve_claimed_plan_path()` (deliberately NOT re-derived here -- see
    that module's own negative-spec on the `plan_claim_dir` import-cycle
    trap) then reads `deliverable_id` straight out of the plan's own
    frontmatter. Omit-rather-than-guess throughout: an unresolvable plan, a
    missing/unreadable file, or a missing/blank field all return `""` --
    never fabricates a value, never raises. A literal YAML `null`/`none`/`~`
    scalar is treated as blank too -- `read_fm_field_unquoted` is a text
    extractor, not a YAML-typed parser, so it returns the LITERAL string
    `"null"` for a `deliverable_id: null` line rather than Python `None`;
    the `("", "none", "null", "~")` blank-set is the SAME convention
    `baton_assemble.__init__`'s own `continued_into`/`predecessor` scalar
    reads already use for exactly this reason (see its call sites), reused
    here rather than re-derived.
    """
    from coordinator_core.session.claimed_plan import resolve_claimed_plan_path
    from coordinator_core.frontmatter.primitives import (
        read_fm_field_unquoted,
        split_frontmatter,
    )

    try:
        plan_path = resolve_claimed_plan_path(cwd)
    except Exception:
        return ""
    if not plan_path:
        return ""

    try:
        with open(Path(cwd) / plan_path, encoding="utf-8") as fh:
            text = fh.read()
    except Exception:
        return ""

    split = split_frontmatter(text)
    if split is None:
        return ""
    deliverable_id = read_fm_field_unquoted(split.fm_text, "deliverable_id")
    if isinstance(deliverable_id, str):
        cleaned = deliverable_id.strip()
        if cleaned and cleaned.lower() not in ("none", "null", "~"):
            return cleaned
    return ""


def _read_deliverable_id_from_frontmatter(full_path: Union[str, Path]) -> str:
    """Read `deliverable_id` straight out of `full_path`'s own frontmatter.

    Shared leaf-read used by both the tier-0 artifact lookup
    (`_resolve_deliverable_id_from_paths`) and, in spirit, tier 3's inline
    read (`_resolve_deliverable_id_from_claimed_plan`, which predates this
    extraction and is left as-is rather than churned mid-fix). Omit-rather-
    than-guess: a missing/unreadable file, a file with no frontmatter block,
    or a missing/blank `deliverable_id` field all return `""`, never raise.
    Same blank-set convention as tier 3 (`("", "none", "null", "~")`) for the
    same reason -- `read_fm_field_unquoted` is a text extractor, not a
    YAML-typed parser, so a literal `deliverable_id: null` line reads back as
    the string `"null"`, not Python `None`.
    """
    from coordinator_core.frontmatter.primitives import (
        read_fm_field_unquoted,
        split_frontmatter,
    )

    try:
        with open(full_path, encoding="utf-8") as fh:
            text = fh.read()
    except Exception:
        return ""

    split = split_frontmatter(text)
    if split is None:
        return ""
    deliverable_id = read_fm_field_unquoted(split.fm_text, "deliverable_id")
    if isinstance(deliverable_id, str):
        cleaned = deliverable_id.strip()
        if cleaned and cleaned.lower() not in ("none", "null", "~"):
            return cleaned
    return ""


def _resolve_deliverable_id_from_paths(
    paths: Optional[Sequence[str]], cwd: Union[str, Path]
) -> str:
    """Tier-0 fallback: resolve straight off the committed artifact(s), not
    the session.

    Cause of the defect this closes (2026-08-04 cross-repo memo,
    `example-market-data-repo-em` -> `claude-klabauter-em`, defect 2): every tier
    below this one is keyed on the SESSION (pickup record, then claimed
    plan) -- correct while a session holds exactly one deliverable, but
    `/pickup a AND b AND c` is a documented, supported shape (pickup skill's
    Multi-Artifact Grab -- "N independent dispositions, not one"). A session
    holding three claims and committing three different batons got the SAME
    trailer on all three commits, because nothing ever looked at which
    artifact a given commit was actually FOR. This tier does: when the
    commit's own pathspec identifies an artifact carrying its own
    `deliverable_id` frontmatter (a handoff, a plan), that artifact's value
    wins -- checked BEFORE any session-keyed tier, since the artifact is the
    more specific (and, for a multi-claim session, the only correct) answer.

    Omit-rather-than-guess: `paths` empty/`None`, or none of `paths` resolve
    to a file carrying a `deliverable_id`, returns `""` and the session
    tiers run exactly as they did before this tier existed (no behaviour
    change on the common single-deliverable-session path -- see
    `test_commit_trailers.py`'s pickup-tier-unchanged coverage).

    Genuinely ambiguous input -- two or more of `paths` carry DIFFERENT
    non-empty `deliverable_id` values -- is NOT guessed at, and per
    producer-contract § 3 (omit-rather-than-guess is the contract every
    tier in this ladder honors, not an exception one tier gets to opt out
    of) this tier OMITS: it returns `""` and lets the session tiers below
    it run exactly as they did before this tier existed, same as the
    empty/no-match case above. No trailer is stamped from a divergent
    pathspec; nothing raises.
    """
    if not paths:
        return ""

    found: dict[str, str] = {}
    for rel_path in paths:
        full_path = Path(cwd) / rel_path
        deliverable_id = _read_deliverable_id_from_frontmatter(full_path)
        if deliverable_id:
            found[str(rel_path)] = deliverable_id

    # When two or more paths carry the same raw value, the raw value is
    # chosen deterministically -- sorted by repo-relative path -- so the
    # same input always yields the same trailer.
    distinct_values = sorted(set(found.values()))
    if not distinct_values:
        return ""
    if len(distinct_values) == 1:
        winning_path = min(found)
        return found[winning_path]

    # Producer-contract § 3: omit, don't guess -- a divergent pathspec is
    # the same "cannot resolve" case as an empty/no-match one, not a
    # separate fail-loud posture. See this function's own docstring.
    return ""


def _resolve_deliverable_id(
    git_dir: str,
    session_id: str,
    cwd: Union[str, Path],
    paths: Optional[Sequence[str]] = None,
) -> str:
    """Tier 0 (artifact-first, see `_resolve_deliverable_id_from_paths`),
    then the scope-match tier (`resolve_deliverable_id_from_scope_match`,
    C2 spec § (1)) over every plan this session holds a claim on, then --
    gated by the ambiguity predicate `session_holds_multiple_plan_claims`
    (C2 spec § (2)), which OMITS rather than guesses whenever the session
    holds more than one plan claim and neither tier above disambiguated --
    check `git_dir`, then fall back to DoE-claude's own git-dir -- the
    cross-repo case where a commit lands directly into claude-klabauter under
    the standing DoE->claude-klabauter write grant while `session-shape.json` was
    written into DoE-claude's git-dir (wherever `/pickup` actually ran).
    When all of those miss (or the ambiguity gate fired), fall back to the
    session's claimed PLAN (tier 3 -- see
    `_resolve_deliverable_id_from_claimed_plan`), the same-session
    plan-execute-without-a-handoff door -- itself also gated by the same
    ambiguity predicate. Never fabricates a value; every lookup in the
    cascade is omit-rather-than-guess, including tier 0's divergent-artifact
    case (see that tier's own docstring). Tiers 1/1a stay verbatim parity with the hook's
    `_resolve_deliverable_id()` (2026-07-27 cross-repo-fallback mirror);
    tier 3 is shared with both mirrors; tier 0, the scope-match tier, and
    the ambiguity gate are new to this engine module only -- `paths` is an
    addition to this module's own signature (not the hook script's), so
    mirroring tier 0 (and the scope-match tier, which also needs `paths`)
    into the hook requires the hook to gain its own path-discovery leg
    (e.g. `git diff --cached --name-only`, safe there because the hook
    always runs with the correct index already in its own env) before it
    can carry the same tiers -- see this module's header docstring on the
    mirrored-pair maintenance convention."""
    deliverable_id = _resolve_deliverable_id_from_paths(paths, cwd)
    if deliverable_id:
        return deliverable_id

    claims = _list_held_plan_claims(cwd)

    deliverable_id = resolve_deliverable_id_from_scope_match(cwd, paths, claims)
    if deliverable_id:
        return deliverable_id

    # Ambiguity gate (C2, spec § (2)): neither tier 0 nor the scope-match
    # tier disambiguated. When the session holds more than one plan claim,
    # every remaining tier below is session-keyed and cannot tell which
    # held claim this commit is for -- omit rather than guess.
    if session_holds_multiple_plan_claims(claims):
        return ""

    deliverable_id = _resolve_deliverable_id_at(git_dir, session_id)
    if deliverable_id:
        return deliverable_id
    doe_root = _resolve_doe_root()
    if doe_root:
        doe_git_dir = os.path.join(doe_root, ".git")
        if os.path.normpath(doe_git_dir) != os.path.normpath(git_dir):
            deliverable_id = _resolve_deliverable_id_at(doe_git_dir, session_id)
            if deliverable_id:
                return deliverable_id
    return _resolve_deliverable_id_from_claimed_plan(cwd)


_TRAILER_LINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*:\s")
_TRAILER_CONT_RE = re.compile(r"^\s")


def _extract_trailer_block(text: str) -> List[str]:
    """Return the lines of `text`'s trailing trailer block, or `[]` if the
    message carries none -- a minimal reimplementation of git's own
    `interpret-trailers` block detection (see `git-interpret-trailers(1)`):
    the trailer block is the LAST paragraph of the message (the run of
    non-blank lines following the final blank line, or the whole message if
    it has no blank line), and ONLY counts as a trailer block when every one
    of its lines is either a `Token: value` line or a continuation line
    (leading whitespace). A body paragraph that merely happens to contain a
    colon-shaped line -- e.g. a hand-written `Deliverable-Id: <id>` sitting
    above a real trailing `Co-Authored-By:` block, separated by a blank line
    -- is therefore body text, not part of the trailer block, matching what
    `git log --format='%(trailers:...)'` actually parses.

    `_has_trailer_line` is this function's only caller; both Session-Id and
    Deliverable-Id idempotency checks route through it, so this fix's
    stricter semantics apply uniformly to both prefixes -- there is no
    caller relying on the old any-occurrence-anywhere behaviour to preserve.
    """
    lines = text.splitlines()
    while lines and lines[-1].strip() == "":
        lines.pop()
    if not lines:
        return []

    start = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == "":
            start = i + 1
            break
    if start is None:
        # No blank line anywhere: the whole message is one paragraph, which
        # git reads as the SUBJECT, never as a trailer block -- verified
        # against `git interpret-trailers --parse`, which returns nothing for
        # both "Deliverable-Id: x" alone and "subj\nDeliverable-Id: x".
        # Reporting a trailer here would re-open the very suppression this
        # block-awareness exists to close.
        return []

    block = lines[start:]
    if not block:
        return []
    for line in block:
        if not (_TRAILER_LINE_RE.match(line) or _TRAILER_CONT_RE.match(line)):
            return []
    return block


def _has_trailer_line(commit_msg_file: Union[str, Path], prefix: str) -> bool:
    """True iff `commit_msg_file`'s trailer block (see
    `_extract_trailer_block`) already contains a line starting with `prefix`
    (e.g. "Session-Id:") -- NOT merely a line starting with `prefix` anywhere
    in the message. A prior looser "any occurrence anywhere" check let a
    hand-written `Deliverable-Id:` line sitting in the message BODY suppress
    this engine's own correctly-placed trailer emission, leaving the commit
    with no machine-readable trailer at all (git's own parser recognises
    only the final paragraph as trailers, so the hand-written line was never
    parseable in the first place). Any read failure -> False (caller
    degrades gracefully, verbatim parity with the hook)."""
    try:
        with open(commit_msg_file, encoding="utf-8") as fh:
            text = fh.read()
    except Exception:
        return False
    for line in _extract_trailer_block(text):
        if line.startswith(prefix):
            return True
    return False


def read_trailer_value(
    commit_msg_file: Union[str, Path], prefix: str
) -> Optional[str]:
    """Return the value of the FIRST `prefix` line in `commit_msg_file`'s
    trailer block (see `_extract_trailer_block`), stripped, or `None` when
    the block carries no such line -- the value-reading counterpart to
    `_has_trailer_line`'s presence-only check.

    Exists because presence and correctness are different questions, and
    this module previously only ever asked the first. `compute_missing_
    trailer_args` treats an already-present `Deliverable-Id:` as settled and
    resolves nothing further -- correct for idempotency, but it means a
    value an AGENT typed into the message it hands to `commit_scoped` reaches
    the commit object having been read by nobody. `commit_scoped`'s own
    `_validate_explicit_deliverable_id` guards only the caller-supplied
    `deliverable_id` PARAMETER, so the message-authored route was the one
    unvalidated door into the same trailer (cross-repo/inbox/2026-08-20-
    example-retrieval-repo-em-wave-commit-deliverable-id-is-per-session.md: a wave
    commit agent wrote a BRANCH NAME, `work/machine-a/2026-08-16to18`, into
    this trailer and the commit exited 0).

    A blank value returns `None`, not `""` -- a `Deliverable-Id:` line with
    nothing after it carries no claim to check, and callers guarding on
    truthiness must not have to distinguish the two.

    Any read failure -> `None`, the same degrade-gracefully contract
    `_has_trailer_line` documents. A caller must therefore NOT read `None`
    as "the message asserts no id"; it means "no id was legible here", and
    the only safe action on it is to fall through to the ordinary
    resolution ladder, never to refuse.
    """
    try:
        with open(commit_msg_file, encoding="utf-8") as fh:
            text = fh.read()
    except Exception:
        return None
    for line in _extract_trailer_block(text):
        if line.startswith(prefix):
            value = line[len(prefix) :].strip()
            return value or None
    return None


_CLOSES_LINE_RE = re.compile(r"^Closes:\s*(.+?)\s*$")
_REVERT_LINE_RE = re.compile(
    r"^This reverts commit ([0-9a-fA-F]{7,40})\.?\s*$"
)


def _split_paragraphs(text: str) -> List[List[str]]:
    """Split `text` into paragraphs -- runs of non-blank lines separated by
    one-or-more blank lines. A leading/trailing run of blank lines produces
    no empty paragraph."""
    paragraphs: List[List[str]] = []
    current: List[str] = []
    for line in text.splitlines():
        if line.strip() == "":
            if current:
                paragraphs.append(current)
                current = []
        else:
            current.append(line)
    if current:
        paragraphs.append(current)
    return paragraphs


def _paragraph_is_trailer_shaped(paragraph: List[str]) -> bool:
    """True iff every line of `paragraph` is either a `Token: value` line or
    a continuation line (leading whitespace) -- the same per-line predicate
    `_extract_trailer_block` applies to git's own last-paragraph trailer
    block, reused here to widen the bound (see `_trailing_region_lines`)."""
    return all(
        _TRAILER_LINE_RE.match(line) or _TRAILER_CONT_RE.match(line)
        for line in paragraph
    )


def _trailing_region_lines(text: str) -> List[str]:
    """Return the lines of `text`'s TRAILING REGION: the last paragraph
    (always included, whatever its shape -- this is what admits a `git
    revert`-style "This reverts commit <sha>." paragraph, which is not
    itself trailer-shaped), plus every paragraph immediately preceding it,
    walking backward, that is entirely trailer-shaped (per
    `_paragraph_is_trailer_shaped`) -- stopping at the first paragraph
    (scanning backward) that is not.

    This is the bound named in this module's own commit_trailers.py C1
    supersession of DECISION-2 (docs/plans/2026-07-17-commit-closure-
    emission-fact.md): reading raw, line-anchored text is what defeats
    git's last-paragraph trailer-demotion rule (a `Closes:` line separated
    from a trailing `Commit-Token:` block by a blank line, e.g. built from
    successive `-m` args, still counts), while this bound is what keeps a
    quoted/embedded prior commit message elsewhere in the body -- separated
    from the trailing region by an ordinary (non-trailer-shaped) paragraph
    -- from being scanned at all. A quoted trailer-shaped paragraph sitting
    DIRECTLY adjacent to the real trailing region is a named, accepted
    hazard (not a defect this bound is meant to close -- see this module's
    C1 dispatch brief).

    Returns `[]` for an empty (or all-blank) `text`.
    """
    paragraphs = _split_paragraphs(text)
    if not paragraphs:
        return []

    region: List[str] = list(paragraphs[-1])
    for paragraph in reversed(paragraphs[:-1]):
        if _paragraph_is_trailer_shaped(paragraph):
            region = paragraph + region
        else:
            break
    return region


def extract_closure_facts_from_text(text: str) -> "tuple[List[str], Optional[str]]":
    """Extract closure facts from an already-in-hand commit message `text`
    (C1, commit-closure-pipe-carries-rows): the message's `Closes:` values,
    normalized to item_ids via `ops.emit.closure_trailer.parse_closure_
    trailers` (the existing normalizer -- never hand-rolled item-id
    parsing here), and any `This reverts commit <sha>` line's sha.

    Deliberately reads the RAW message text, line-anchored -- never git's
    parsed trailer block (`_extract_trailer_block`/`git interpret-trailers`)
    -- bounded to `_trailing_region_lines`'s trailing region so a quoted or
    embedded message elsewhere in the body cannot contribute a false
    `Closes:`/revert line (see that function's own docstring for the full
    reasoning and named hazard).

    A `Closes:` line's value is passed through `parse_closure_trailers`
    UNNORMALIZED-first (as the raw trailer value), same shape that
    function already expects from C3's git-native extraction -- multiple
    `Closes:` lines each contribute their own value, in message order; a
    value `parse_closure_trailers`'s pattern table rejects (not ID-shaped)
    is silently dropped by that normalizer, same as it already is for the
    git-native extraction path.

    Returns `([], None)` for text carrying neither -- never raises.
    """
    region_lines = _trailing_region_lines(text)

    raw_closes_values: List[str] = []
    reverts_sha: Optional[str] = None
    for line in region_lines:
        closes_match = _CLOSES_LINE_RE.match(line)
        if closes_match:
            raw_closes_values.append(closes_match.group(1))
            continue
        if reverts_sha is None:
            revert_match = _REVERT_LINE_RE.match(line)
            if revert_match:
                reverts_sha = revert_match.group(1)

    from coordinator_core.ops.emit.closure_trailer import parse_closure_trailers

    closes = parse_closure_trailers(raw_closes_values)
    return closes, reverts_sha


def extract_closure_facts(
    commit_msg_file: Union[str, Path]
) -> "tuple[List[str], Optional[str]]":
    """File-reading counterpart to `extract_closure_facts_from_text`, for a
    caller holding `commit_msg_file` rather than the message text itself --
    same degrade-gracefully contract as `_has_trailer_line`/`read_trailer_
    value` above: any read failure returns `([], None)`, never raises."""
    try:
        with open(commit_msg_file, encoding="utf-8") as fh:
            text = fh.read()
    except Exception:
        return [], None
    return extract_closure_facts_from_text(text)


def compute_missing_trailer_args(
    commit_msg_file: Union[str, Path],
    cwd: Union[str, Path],
    paths: Optional[Sequence[str]] = None,
    *,
    session_id_override: Optional[str] = None,
) -> List[str]:
    """Compute the `git interpret-trailers --trailer ...` argument list for
    whichever of Session-Id / Deliverable-Id are resolvable AND not already
    present in `commit_msg_file` -- the identical decision the
    `prepare-commit-msg` hook makes, for a caller (`git commit-tree` et al.)
    that hooks never fire for.

    Returns `[]` when nothing is resolvable or nothing is missing -- callers
    should treat an empty list as "no `interpret-trailers` call needed",
    never as an error (mirrors the hook's own idempotent-and-silent
    contract: it NEVER blocks a commit and NEVER stamps a guessed value).
    A genuinely ambiguous `paths` set omits the same way -- see `paths`
    below and `_resolve_deliverable_id_from_paths`'s docstring.

    Session-Id and Deliverable-Id have INDEPENDENT idempotency checks (a
    message may legitimately carry one without the other) and Deliverable-Id
    is only resolved (and only looked up) if it is itself missing --
    verbatim parity with the hook's ordering.

    `paths`: the pathspec of the commit being built (project-relative or
    absolute, either resolves against `cwd`), OPTIONAL and additive --
    omitting it (the default) reproduces the exact prior session-only
    resolution byte-for-byte, so an existing caller that has not been
    updated to pass its own pathspec sees no behaviour change. When given,
    it feeds the new artifact-first tier (tier 0) ahead of every session
    tier -- see `_resolve_deliverable_id_from_paths` for the multi-baton-
    session defect this closes and the omit-rather-than-guess posture on a
    genuinely divergent pathspec (returns `""`, same as no match; nothing
    raises).

    `session_id_override` (state/bug-backlog/2026-08-18-scoped-git-commit-
    stamps-a-foreign-session-id-8d21f0c4e7b9.yaml): the invoking session's
    OWN already-resolved identity, when a caller has one, takes precedence
    over the blind `_resolve_session_id(git_dir)` env-var read below --
    same authority `deliverable_id`/`--governing-plan-slug` already hold
    over their own session-keyed tiers (see `commit_anchors.py::
    _resolve_plan_from_governing_slug`, same disease and cure). Every
    production caller of this module reaches git through a shared-tree,
    many-concurrent-session Python process (this repo's own load norm):
    `os.environ` inside that process is not guaranteed to still equal the
    identity a caller resolved earlier in the SAME request via its own
    params-aware ladder (e.g. `scoped_git_commit.py::_resolve_committing_
    session_id`, which honors an explicit `params["session_id"]` override
    the blind env read has no way to see) -- stamping whichever the two
    disagree on is exactly the "foreign Session-Id" defect filed above.
    `None` (the default) reproduces the prior behaviour byte-for-byte for
    every caller not yet updated to pass one. A non-`None`, non-UUID-shaped
    override is treated as "no override" and falls through to the blind
    read, same fail-safe direction the UUID check below already applies to
    that read -- a caller-supplied override is not exempt from the
    invariant that a non-UUID id must never reach `Session-Id:`.
    """
    git_dir = _resolve_git_dir(cwd)
    session_id = (
        session_id_override
        if session_id_override and _UUID_RE.fullmatch(session_id_override)
        else _resolve_session_id(git_dir)
    )

    trailer_args: List[str] = []

    # Fail-safe: a non-UUID resolved id must OMIT both Session-Id and
    # Deliverable-Id, never stamp a wrong Session-Id (or a Deliverable-Id
    # keyed off it).
    if session_id and _UUID_RE.fullmatch(session_id):
        try:
            need_session_id = not _has_trailer_line(commit_msg_file, "Session-Id:")
            need_deliverable_id = not _has_trailer_line(commit_msg_file, "Deliverable-Id:")
        except Exception:
            need_session_id = False
            need_deliverable_id = False

        if need_session_id:
            trailer_args += ["--trailer", f"Session-Id: {session_id}"]
        if need_deliverable_id:
            deliverable_id = _resolve_deliverable_id(git_dir, session_id, cwd, paths)
            if deliverable_id:
                trailer_args += ["--trailer", f"Deliverable-Id: {deliverable_id}"]

    return trailer_args


def trailer_values_from_argv(trailer_args: Sequence[str]) -> List[str]:
    """Extract the `K: V` values from a flat `interpret-trailers` argv as
    `compute_missing_trailer_args` returns it (`["--trailer", "K: V", ...]`).

    Tolerates a trailing `--trailer` with no value rather than raising --
    `_drop_trailer_arg` preserves pairing, but a caller assembling argv by
    hand may not, and dropping an unpaired flag is the same thing git would
    do with it (nothing).
    """
    values: List[str] = []
    i = 0
    while i < len(trailer_args):
        if trailer_args[i] == "--trailer" and i + 1 < len(trailer_args):
            values.append(trailer_args[i + 1])
            i += 2
        else:
            i += 1
    return values


#: Prefixes git itself generates, from `trailer.c :: git_generated_prefixes`.
#: Their presence is what lets a block hold non-trailer lines at all (Rule 5).
_GIT_GENERATED_PREFIXES = ("Signed-off-by: ", "(cherry picked from commit ")

#: git-interpret-trailers(1): a block that is not all-trailers needs "at least
#: 25% trailers" alongside a git-generated one.
_TRAILER_BLOCK_MIN_PERCENT = 25


def _block_items(lines: List[str]) -> List[tuple]:
    """Group a candidate trailer block's raw lines (endings kept) into items:
    `(is_trailer, [raw_lines])`. A continuation line -- one opening with
    whitespace -- belongs to the trailer above it rather than standing alone,
    which is why a CRLF *inside* a folded value survives into git's output
    while the item's own terminator does not."""
    items: List[tuple] = []
    for raw in lines:
        bare = raw.rstrip("\r\n")
        if bare.lstrip().startswith("#"):
            # Rule 6 -- a comment INSIDE the block is invisible to the
            # acceptance test and dropped from the output, but ONLY when the
            # block is accepted; a rejected block is emitted verbatim and
            # keeps it.
            items.append(("c", [raw]))
        elif _TRAILER_CONT_RE.match(bare) and items and items[-1][0] == "t":
            items[-1][1].append(raw)
        elif _TRAILER_LINE_RE.match(bare):
            items.append(("t", [raw]))
        else:
            # A continuation whose trailer is separated from it by a comment
            # has nothing to fold into, so it lands here as a NON-trailer --
            # which is what makes `Co-Authored-By:` + `# c` + `  cont` a
            # rejected block rather than an accepted one.
            items.append(("n", [raw]))
    return items


def _block_is_trailers(items: List[tuple]) -> bool:
    """git's acceptance rule for the last paragraph, DERIVED from real git
    rather than read from `trailer.c` (no git source on this box) -- see
    `state/audits/2026-08-25-interpret-trailers-trailer-block-rule.py`, which
    re-derives it and is the thing to re-run if this is ever doubted.

    All lines trailer-shaped, OR the block carries a git-generated trailer
    AND is at least 25% trailer lines. The git-generated half is the part
    that is easy to miss: `Co-Authored-By`, `Acked-by` and `Deliverable-Id`
    do NOT unlock the proportional rule, only `Signed-off-by` (and the
    cherry-pick marker) do."""
    real = [(kind, raw) for kind, raw in items if kind != "c"]
    if not real:
        return False
    if all(kind == "t" for kind, _ in real):
        return True
    if not any(
        raw[0].startswith(_GIT_GENERATED_PREFIXES)
        for kind, raw in real
        if kind == "t"
    ):
        return False
    # The ratio counts ITEMS, not lines: a trailer with folded continuation
    # lines is one trailer, not several. Counting lines lets a single folded
    # trailer buy its own acceptance.
    trailers = sum(1 for kind, _ in real if kind == "t")
    return trailers * 100 >= len(real) * _TRAILER_BLOCK_MIN_PERCENT


def _render_block_item(kind: str, raw: List[str]) -> str:
    """A trailer item is re-emitted with a `\\n` terminator (its folded
    continuations keep their own endings verbatim); a non-trailer line is
    passed through byte-for-byte; a comment is dropped."""
    if kind == "c":
        return ""
    if kind != "t":
        return "".join(raw)
    return "".join(raw[:-1]) + raw[-1].rstrip("\r\n") + "\n"


def can_format_trailers_in_process(message: bytes) -> bool:
    """True iff `format_trailers_in_process` is VERIFIED byte-identical to
    `git interpret-trailers` for `message`.

    The verified envelope is: **no `#` comment line.** Line endings are NOT
    part of it -- CRLF is admitted and verified. Measured over the
    byte-identity corpus's fuzz generator across 20 seeds (8000 cases):
    **5483/5483 identical inside this envelope, and every one of the 6
    residual divergences outside it.** Both figures come from the same sweep;
    this is a measured boundary, not a guessed conservative one.

    CRLF IS DELIBERATELY ADMITTED, and getting that wrong is the trap. A
    narrower "no CR either" envelope looks safer and buys NOTHING on Windows:
    `Path.write_text` translates `\\n` to `\\r\\n` in text mode, so every
    `msg_file` a Python caller writes on this box is already CRLF. The
    census proved it -- the first wiring of this predicate rejected the
    engine's own probe message and left the spawn in place. An envelope that
    excludes the only shape the platform produces is not conservative, it is
    inert.

    Callers MUST spawn `git interpret-trailers` when this returns False. The
    residual classes all involve a comment line interacting with peeling --
    see `format_trailers_in_process`. Widening this predicate without first
    widening the corpus sweep is exactly the "hand-written replacement that
    guesses at trailer semantics" the ratified anti-scope forbids.

    DECIDED 2026-08-25 -- DO NOT WIDEN THIS, and here is the measurement so
    the question stops being reopened. The standing residual (10 known
    out-of-envelope divergences, all comment-line shapes) was carried as an
    open cost/benefit call: closing them widens the envelope and drops the
    fallback spawn. Measured against production rather than argued: over the
    last 2000 commit messages in this repo, exactly **1** carries a `#` line
    and would fall back -- a markdown `## The op` heading inside an
    EM-authored body. That is 0.05%, i.e. one extra git spawn per ~2000
    commits.

    So the trade is: reproduce git's comment-line-plus-peeling semantics --
    the single hardest corner of trailer handling and the precise hazard the
    ratified anti-scope names -- to save one spawn in two thousand. Not worth
    it. The fallback is correct BY CONSTRUCTION (it runs real git), which is
    exactly what a rarely-taken path should be. Reopen this only if the
    fallback rate is re-measured materially higher, and quote the new figure
    when you do.

    RE-DERIVATION TRAP: `git log -2000 --grep=...` is NOT this check. `-n`
    caps OUTPUT after `--grep` filtering, so it draws matches from the whole
    history (~25k commits here), not the last 2000 -- it returns 24, not 1,
    because it samples a different, much larger population. The real check
    reads the last 2000 messages (`git log -2000 --format=%B%x00`) and tests
    each with this function's own predicate, `line.lstrip().startswith(b"#")`.

    This is a proxy, not the exact population: it samples LANDED history, not
    the not-yet-committed message `_apply_trailers` actually hands this
    predicate. The two are typically near-identical (messages are authored
    once, close to verbatim) but could diverge. This kind of ratified
    cost/benefit call would conventionally live in a `docs/decisions/DR-*.md`
    per this repo's convention; it stays inline here for locality to the
    function it gates -- a deliberate tradeoff, not an oversight."""
    return not any(line.lstrip().startswith(b"#") for line in message.split(b"\n"))


def format_trailers_in_process(message: bytes, trailers: Sequence[str]) -> bytes:
    """Append `trailers` to `message`, byte-identically to
    `git interpret-trailers --no-divider --in-place --trailer <t> ...`.

    ADD-ONLY, and the narrowness is the safety argument. This reproduces git
    for the call shape the commit path actually issues -- every trailer is
    known ABSENT from the message (`compute_missing_trailer_args` returns
    only missing ones; `_check_deliverable_id_precedence` /
    `_drop_trailer_arg` exist so no key ever needs replacing). It does NOT
    implement `--if-exists`, `--if-missing`, `--where`, config-defined
    trailer aliases, or the `--divider`/scissors handling. Widening the call
    shape without widening the corpus below is how this becomes the
    "hand-written replacement that guesses at trailer semantics" the ratified
    anti-scope in `docs/plans/2026-08-21-a-commit-stops-paying-for-thirty-
    processes.md` forbids.

    *** ONLY CALL THIS BEHIND `can_format_trailers_in_process`. ***

    Status as of 2026-08-25, over the byte-identity corpus's fuzz generator
    swept across 20 seeds (8000 cases) plus the 25 named cases:

      * 25/25 named, and 400/400 at the corpus's pinned `FUZZ_SEED`.
      * 4704/4704 identical for messages with NO carriage return and NO `#`
        comment line -- the envelope `can_format_trailers_in_process` gates
        on, and the only shape the engine itself emits.
      * 6 divergences in 3296, ALL outside that envelope.

    THE PINNED SEED IS NOT ENOUGH, and that is worth more than the fix. An
    earlier round reached 400/400 on seed 20260825 and looked finished; six
    other seeds immediately produced ten failures, and fourteen more seeds
    produced seven further ones in shapes no earlier seed generated. A single
    pinned seed is a fixed case list wearing a fuzzer's clothes. Sweep seeds
    before believing a green run.

    The residual classes are all CRLF interacting with comment peeling: where
    a peeled CRLF blank goes when the block is rewritten, and whether a
    trailing CRLF comment is peeled or left in place. They are unreached by
    the gated call path. Fix them against the corpus -- NEVER by editing the
    corpus -- and re-sweep before widening the predicate.

    The block-boundary algorithm is no longer derived: it is
    `trailer.c :: find_trailer_block_start`, whose acceptance test is
    `recognized_prefix && trailer_lines * 3 >= non_trailer_lines`, or
    `trailer_lines && !non_trailer_lines`. `_block_is_trailers` implements the
    algebraically identical 25% form. `state/audits/
    2026-08-25-interpret-trailers-trailer-block-rule.py` re-derives it from
    live git and is the thing to run if it is ever doubted.

    ORACLE, not intuition: every rule here was read off real git output, not
    reasoned about. `state/audits/2026-08-25-interpret-trailers-byte-identity-
    corpus.py` is the differential -- 25 named message shapes plus a seeded
    400-case fuzz, run against both sides, asserted byte-for-byte. The named
    cases pass and the fuzz does not, which is the whole argument for having
    both: hand-picked cases only cover shapes the author imagined.
    Four of its rules are ones a careful implementer gets wrong by default:

      1. Trailing `#` comment lines and trailing blank lines are PEELED off
         the end, the trailers are appended, and the peeled lines are put
         back AFTER them. Trailers do not go at end-of-file.
      2. If the last remaining paragraph is trailer-shaped, the new trailers
         join it with NO blank line; otherwise a blank line is inserted. A
         paragraph that still contains the SUBJECT is never a trailer block,
         which is why `subject\nDeliverable-Id: x\n` gets a blank line and a
         second `Deliverable-Id:` rather than joining.
      3. git writes `\n`, always -- and REWRITES an existing trailer block's
         `\r\n` line endings to `\n` when it appends to that block. A CRLF
         message therefore ends up with mixed endings. That is git's
         behaviour, not a defect to normalise away.
      4. Trailer values are right-stripped.

    `message` and the return are BYTES, decoded with `surrogateescape`, so a
    commit message that is not valid UTF-8 round-trips unchanged rather than
    raising -- git does not care about the encoding and neither may this.
    """
    if not trailers:
        return message

    rendered = "".join(f"{value.rstrip()}\n" for value in trailers)
    text = message.decode("utf-8", errors="surrogateescape")

    if text == "":
        return ("\n" + rendered).encode("utf-8", errors="surrogateescape")

    if not text.endswith("\n"):
        text += "\n"

    lines = text.splitlines(keepends=True)

    # Rule 1 -- peel the trailing comment/blank region. A blank line ending
    # `\r\n` is NOT peeled: git leaves it in place as the separator and
    # appends after it, where an LF blank is peeled and put back below the
    # trailers. Same shape, different line ending, opposite placement.
    cut = len(lines)
    while cut > 0:
        stripped = lines[cut - 1].strip()
        if stripped == "" or stripped.startswith("#"):
            cut -= 1
        else:
            break
    head, suffix = lines[:cut], lines[cut:]

    if not head:
        # Rule 5 -- a message that is NOTHING but comments/blanks keeps that
        # region FIRST and takes the trailers after it. Peeling it and then
        # treating the remainder as empty put the trailers ABOVE the comment.
        # With a comment present the split is after the last comment line; an
        # all-blank region instead spends ONE blank as the separator and
        # carries the rest below the trailers.
        # A run of CRLF blanks is never split: it stays whole, ahead of the
        # trailers. Otherwise one line becomes the message and the rest
        # follows the trailers.
        lead = 0
        while (
            lead < len(suffix)
            and suffix[lead].strip() == ""
            and suffix[lead].endswith("\r\n")
        ):
            lead += 1
        keep = max(lead, 1)
        before = "".join(suffix[:keep])
        sep = "" if before.strip() == "" else "\n"
        after = "".join(suffix[keep:])
        return (before + sep + rendered + after).encode(
            "utf-8", errors="surrogateescape"
        )

    # Locate the last paragraph of what remains.
    para_start = len(head)
    while para_start > 0 and head[para_start - 1].strip() != "":
        para_start -= 1

    items = _block_items(head[para_start:])
    # Rule 2 -- a paragraph still holding the subject is never a trailer block.
    joins_existing_block = para_start > 0 and bool(items) and _block_is_trailers(items)

    if joins_existing_block:
        # Rule 3 -- git re-emits each parsed TRAILER with a `\n` terminator
        # while leaving non-trailer lines byte-verbatim, so a CRLF block ends
        # up with per-line mixed endings rather than a uniformly rewritten one.
        block = "".join(_render_block_item(kind, raw) for kind, raw in items)
        body = "".join(head[:para_start]) + block + rendered
        # Rule 7 -- a peeled CRLF blank is CONSUMED by the rewrite only when
        # the block's last item is a trailer (the rewrite reaches that line
        # and re-terminates it). If the block ends on a non-trailer line --
        # admitted by the proportional rule under a sign-off -- or a comment
        # follows, the blank stays put and separates the trailers instead.
        # An LF blank always returns below them.
        lead = 0
        while (
            lead < len(suffix)
            and suffix[lead].strip() == ""
            and suffix[lead].endswith("\r\n")
        ):
            lead += 1
        _ends_on_trailer = bool(items) and items[-1][0] == "t"
        if lead and (
            not _ends_on_trailer
            or any(raw.strip().startswith("#") for raw in suffix[lead:])
        ):
            body = (
                "".join(head[:para_start])
                + block
                + "".join(suffix[:lead])
                + rendered
            )
        tail = "".join(suffix[lead:])
    else:
        # Rejected: a peeled CRLF blank goes back where it was and serves as
        # the separator, rather than being synthesised and re-appended.
        lead = 0
        while (
            lead < len(suffix)
            and suffix[lead].strip() == ""
            and suffix[lead].endswith("\r\n")
        ):
            lead += 1
        if lead:
            body = "".join(head) + "".join(suffix[:lead]) + rendered
            tail = "".join(suffix[lead:])
        else:
            body = "".join(head) + "\n" + rendered
            tail = "".join(suffix)

    return (body + tail).encode("utf-8", errors="surrogateescape")


def apply_missing_trailers(
    message: str,
    cwd: Union[str, Path],
    paths: Optional[Sequence[str]] = None,
    *,
    session_id_override: Optional[str] = None,
) -> str:
    """Return `message` with every resolvable-and-missing Session-Id /
    Deliverable-Id trailer appended -- the shared attach point every commit
    ROUTE (hook-driven `git commit`, and any hook-bypassing mechanism such as
    `ceremony.commit_v2`) is meant to call so both land the same two
    trailers under the same resolution ladder, rather than each route
    reimplementing (and inevitably diverging on) its own copy.

    Pure orchestration over what this module already exposes:
    `compute_missing_trailer_args` decides WHAT is missing and resolvable
    (never guesses, see its own docstring); `can_format_trailers_in_process`
    / `format_trailers_in_process` apply it in-process for the byte-identity-
    verified envelope; a `git interpret-trailers` spawn (via the shared
    `git.run.run_git` seam) is the fallback for the narrow out-of-envelope
    shapes (a `#` comment line) that in-process formatting does not attempt --
    same split `git_native.py::_apply_trailers` already uses for the hook-
    driven route, restated here rather than imported (this module owns no
    dependency on `ops.ceremony.*`).

    `compute_missing_trailer_args` reads its idempotency check off a FILE
    (mirroring the `prepare-commit-msg` hook's own on-disk contract), so this
    function stages `message` into a throw-away temp file to reuse that
    logic unchanged rather than forking a second, text-only copy of the
    idempotency scan.

    Returns `message` UNCHANGED when nothing is resolvable, nothing is
    missing, or (fail-safe) the `git interpret-trailers` fallback itself
    fails to run -- never raises, matching every other function in this
    module's degrade-gracefully contract. A caller must not treat an
    unchanged return as an error.
    """
    fd, msg_path = tempfile.mkstemp(prefix="commit-trailers-stage-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(message)
        try:
            trailer_args = compute_missing_trailer_args(
                msg_path, cwd, paths=paths, session_id_override=session_id_override
            )
        except Exception:
            # Degrade-gracefully, same contract every resolver in this
            # module already honors -- a trailer problem must never block a
            # commit or corrupt the message it was asked to augment.
            return message
    finally:
        try:
            os.unlink(msg_path)
        except OSError:
            pass

    if not trailer_args:
        return message

    values = trailer_values_from_argv(trailer_args)
    raw = message.encode("utf-8", errors="surrogateescape")

    if can_format_trailers_in_process(raw):
        return format_trailers_in_process(raw, values).decode(
            "utf-8", errors="surrogateescape"
        )

    from coordinator_core.git.run import run_git

    fd2, apply_path = tempfile.mkstemp(prefix="commit-trailers-apply-")
    try:
        with os.fdopen(fd2, "wb") as handle:
            handle.write(raw)
        result = run_git(
            [
                "interpret-trailers",
                "--no-divider",
                "--in-place",
                *trailer_args,
                apply_path,
            ],
            cwd=str(cwd),
        )
        if not result.ok:
            return message
        return Path(apply_path).read_text(encoding="utf-8", errors="surrogateescape")
    finally:
        try:
            os.unlink(apply_path)
        except OSError:
            pass
