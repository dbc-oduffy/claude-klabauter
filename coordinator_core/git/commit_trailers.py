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
Its resolution logic (session-id three-tier ladder + UUID fail-safe +
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
`_resolve_deliverable_id()` now re-checks example-doctrine-repo's own git-dir, located
via the `.doe-root` pointer convention, when the local git-dir's
`session-shape.json` lookup misses -- the structural miss for every commit
landed directly into claude-klabauter under the example-doctrine-repo->claude-klabauter cross-repo write
grant, since `session-shape.json` is written wherever `/pickup` actually
ran (almost always example-doctrine-repo), not wherever the eventual commit lands --
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
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import List, Optional, Sequence, Union

from coordinator_core.doe_root_pointer import read_doe_root_pointer_file
from coordinator_core.git import repo_root as _repo_root_seam

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
    """Three-tier ladder, verbatim parity with the hook: legacy env ->
    platform env -> sentinel file under `git_dir` (no subprocess)."""
    sid = os.environ.get("CLAUDE_SESSION_ID", "").strip()
    if sid:
        return sid
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if sid:
        return sid
    if git_dir:
        sentinel = os.path.join(git_dir, "coordinator-sessions", ".current-session-id")
        try:
            with open(sentinel, encoding="utf-8") as fh:
                return fh.read().strip()
        except Exception:
            return ""
    return ""


def _resolve_doe_root() -> str:
    """Locate example-doctrine-repo's repo root via the `.doe-root` pointer convention
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

    found: dict[str, str] = {}
    for rel_path in paths:
        full_path = Path(cwd) / rel_path
        deliverable_id = _read_deliverable_id_from_frontmatter(full_path)
        if deliverable_id:
            found[str(rel_path)] = deliverable_id

    distinct_values = sorted(set(found.values()))
    if not distinct_values:
        return ""
    if len(distinct_values) == 1:
        return distinct_values[0]

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
    then check `git_dir`, then fall back to example-doctrine-repo's own git-dir -- the
    cross-repo case where a commit lands directly into claude-klabauter under
    the standing example-doctrine-repo->claude-klabauter write grant while `session-shape.json` was
    written into example-doctrine-repo's git-dir (wherever `/pickup` actually ran).
    When all of those miss, fall back to the session's claimed PLAN (tier
    3 -- see `_resolve_deliverable_id_from_claimed_plan`), the same-session
    plan-execute-without-a-handoff door. Never fabricates a value; every
    lookup in the cascade is omit-rather-than-guess (tier 0's divergent-
    artifact case is the one exception -- it raises rather than omits, see
    that tier's own docstring). Tiers 1/1a stay verbatim parity with the
    hook's `_resolve_deliverable_id()` (2026-07-27 cross-repo-fallback
    mirror); tier 3 is shared with both mirrors; tier 0 is new to this
    engine module only -- `paths` is an addition to this module's own
    signature (not the hook script's), so mirroring tier 0 into the hook
    requires the hook to gain its own path-discovery leg (e.g.
    `git diff --cached --name-only`, safe there because the hook always
    runs with the correct index already in its own env) before it can carry
    the same tier -- see this module's header docstring on the mirrored-
    pair maintenance convention."""
    deliverable_id = _resolve_deliverable_id_from_paths(paths, cwd)
    if deliverable_id:
        return deliverable_id
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


def _has_trailer_line(commit_msg_file: Union[str, Path], prefix: str) -> bool:
    """True iff `commit_msg_file` already contains a line starting with
    `prefix` (e.g. "Session-Id:"). Any read failure -> False (caller degrades
    gracefully, verbatim parity with the hook)."""
    try:
        with open(commit_msg_file, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(prefix):
                    return True
    except Exception:
        return False
    return False


def compute_missing_trailer_args(
    commit_msg_file: Union[str, Path],
    cwd: Union[str, Path],
    paths: Optional[Sequence[str]] = None,
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
    """
    git_dir = _resolve_git_dir(cwd)
    session_id = _resolve_session_id(git_dir)
    if not session_id:
        return []

    # Fail-safe: a non-UUID resolved id must OMIT both trailers, never stamp
    # a wrong Session-Id (or a Deliverable-Id keyed off it).
    if not _UUID_RE.fullmatch(session_id):
        return []

    try:
        need_session_id = not _has_trailer_line(commit_msg_file, "Session-Id:")
        need_deliverable_id = not _has_trailer_line(commit_msg_file, "Deliverable-Id:")
    except Exception:
        return []

    trailer_args: List[str] = []
    if need_session_id:
        trailer_args += ["--trailer", f"Session-Id: {session_id}"]
    if need_deliverable_id:
        deliverable_id = _resolve_deliverable_id(git_dir, session_id, cwd, paths)
        if deliverable_id:
            trailer_args += ["--trailer", f"Deliverable-Id: {deliverable_id}"]

    return trailer_args
