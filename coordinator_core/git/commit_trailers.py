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
    truth; a change here must be mirrored there too."""
    return _session_core.resolve_session_id()


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
    """
    if not paths or not claims:
        return ""

    normalized_paths = [_normalize_committed_path(p, cwd) for p in paths]

    covering_deliverable_ids: List[str] = []
    for plan_path, _claimed_at in claims:
        scope_paths = set(_read_plan_scope_paths(cwd, plan_path))
        if not scope_paths:
            continue
        if all(p in scope_paths for p in normalized_paths):
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
    non-empty `deliverable_id` values -- is NOT guessed at. Raises
    `DivergentDeliverableIdError`, the same fail-loud posture
    `coordinator_core.ops.deliverable_carry.resolve_deliverable_and_initiative`
    already established for its own plan-vs-predecessor divergent join (see
    that class's docstring for the DR-207 DD#1 earliest-artifact-wins
    reasoning this function does not attempt to apply either, for the same
    reason: no timestamp/provenance ordering is available from a bare path
    list). Reused rather than forked per that module's own negative-spec
    ("do not fork a second copy of ... DivergentDeliverableIdError
    anywhere else").
    """
    if not paths:
        return ""

    from coordinator_core.ops.deliverable_carry import DivergentDeliverableIdError
    from coordinator_core.ops.deliverable_equivalence import canonicalize, load_equivalence_map

    equivalence_map = load_equivalence_map(Path(cwd))

    found: dict[str, str] = {}
    for rel_path in paths:
        full_path = Path(cwd) / rel_path
        deliverable_id = _read_deliverable_id_from_frontmatter(full_path)
        if deliverable_id:
            found[str(rel_path)] = deliverable_id

    # Divergence join canonicalized (C6b/AC11) -- a declared fork pair
    # covering the same commit is now the SAME distinct value here, not a
    # false "differing" refusal. Canonicalization is confined to this
    # equality check: `found` itself keeps the raw per-path values
    # (unchanged in the raised message below), AND the value this function
    # returns on the collapse-to-one path is also always a RAW value that
    # some staged artifact actually carries -- never the synthesized
    # canonical winner. Returning the canonical value here would stamp a
    # `Deliverable-Id:` trailer (this function's return value reaches
    # `git_native.commit_scoped` two hops up, via `_resolve_deliverable_id`)
    # that no staged artifact's own frontmatter carries verbatim, which is
    # exactly the mutation the plan's WRITE-PATH-SITE negative-spec forbids
    # (review-integrator P1, coordinatorcode-reviewer-0f04f47d.md). When two
    # or more raw values collapse to one canonical id, the raw value is
    # chosen deterministically -- sorted by repo-relative path -- so the
    # same input always yields the same trailer.
    canonical_by_path = {p: canonicalize(v, equivalence_map) for p, v in found.items()}
    distinct_canonical = sorted(set(canonical_by_path.values()))
    if not distinct_canonical:
        return ""
    if len(distinct_canonical) == 1:
        winning_path = min(found)
        return found[winning_path]

    conflict_desc = ", ".join(f"{p!r} -> {v!r}" for p, v in sorted(found.items()))
    raise DivergentDeliverableIdError(
        "compute_missing_trailer_args: this commit's pathspec names artifacts "
        f"with DIFFERING deliverable_id values ({conflict_desc}) -- refusing to "
        "guess which trailer applies. Split this commit so each deliverable's "
        "artifact(s) land in their own commit, or pass an explicit resolution "
        "upstream; see _resolve_deliverable_id_from_paths's own docstring for "
        "why an earliest-artifact tiebreak is not attempted here."
    )


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
    cascade is omit-rather-than-guess (tier 0's divergent-artifact case is
    the one exception -- it raises rather than omits, see that tier's own
    docstring). Tiers 1/1a stay verbatim parity with the hook's
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

    start = 0
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == "":
            start = i + 1
            break

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
    The one exception is a genuinely ambiguous `paths` set -- see `paths`
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
    session defect this closes and the fail-loud posture on a genuinely
    divergent pathspec (`DivergentDeliverableIdError`, propagated
    uncaught -- a caller that wants to catch and downgrade it to a
    non-raising failure must do so itself).

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
