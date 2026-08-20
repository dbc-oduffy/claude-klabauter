"""
coordinator_core.plan_assemble.predicates.shared_booleans — Layer 1: the
two shared booleans every Layer 2 composer at `:44`/`:57`/`:59` depends on,
`:105 (3a)` (scope file count) and `:105 (3d)` (no cross-repo contract).

Purpose: DoE's `plan-assemble` contract has NO row for either of these two
facts — its own coverage join admits the gap at `:744-749` of the amended
spec. The plan's own Layer partition therefore promotes them to first-class
Branch B rows here, in this package, rather than blocking chunk C12's five
Layer 2 composers (`:44`, `:57`, `:59`, `:105 (1)`, `:105 (2)`) on DoE's
correction landing. Per the inherited ruling on the predecessor handoff:
write them now as Branch B rows; if DoE's promoted rows arrive later under
different field names, renaming what this module emits is a one-line
change — the next reader should expect a rename here, not read one as a
conflict with an upstream authority this module never had.

Two rows, both into `gates.substrate.*`:

  :105(3a)  collapse.scope_file_count_le_2 (bool), .scope_file_count (int)
            — a literal count over the plan's `scope:` frontmatter list.
  :105(3d)  collapse.no_cross_repo_contract (bool), .crossing_paths (list)
            — does any `scope:` entry resolve outside `context.repo_root`.

Both rows read only `context.plan_frontmatter["scope"]` — no git, no grep,
no second disk read beyond what `PredicateContext.from_paths` already did.

Negative-spec:
  - Does NOT read `context.plan_path` or re-parse the plan file. The scope
    list is read exclusively off `context.plan_frontmatter`, which
    `PredicateContext.from_paths` already parsed once.
  - Does NOT normalise, dedupe, or sort the `scope:` list before counting —
    `.scope_file_count` is a literal `len()` over whatever the frontmatter
    declares, matching the contract's "literal count" framing at `:105(3a)`.
  - Does NOT decide whether a crossing path is a PROBLEM — `:105(3d)`'s
    `.no_cross_repo_contract` is a computed fact (no path crosses), not a
    judgment about whether crossing is acceptable in this plan's context.
    Candidate evidence only; no `U`-classified disposition is emitted here.
  - Does NOT treat an absent `plan_frontmatter` or an absent `scope` key as
    "zero paths, so no crossing" — both rows emit `undetermined` when the
    context carries no plan (or a plan whose frontmatter has no `scope:`
    list), never a false-by-default `0`/`[]`/`True`.
  - Does NOT resolve `..`-relative paths against the filesystem (no `.
    resolve()` against a possibly-nonexistent tree) — crossing is decided
    by pure path-string composition against `context.repo_root`, matching
    how every other Layer 0 reader in this package treats `scope:` entries
    as declared strings, not filesystem probes.

Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C9
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Any

from coordinator_core.plan_assemble.predicates import PredicateContext, undetermined

#: `:105(3a)`'s threshold — the contract names "≤2" verbatim.
_SCOPE_FILE_COUNT_THRESHOLD = 2


def _scope_list(context: PredicateContext) -> list[str] | None:
    """Return the plan's `scope:` frontmatter list, or `None` if a plan
    was not supplied, carried no parseable frontmatter, or its
    frontmatter has no `scope:` key at all."""
    if context.plan_frontmatter is None:
        return None
    scope = context.plan_frontmatter.get("scope")
    if not isinstance(scope, list):
        return None
    return [str(entry) for entry in scope]


def _crosses_repo_root(repo_root: Path, scope_entry: str) -> bool:
    """Does *scope_entry*, composed against *repo_root* as a pure path
    string (no filesystem resolution), land outside *repo_root*?

    An absolute path is crossing unless it is already rooted under
    *repo_root*. A relative path is crossing if any `..` component walks
    it above *repo_root* before the composition settles back inside.
    """
    entry_path = PurePosixPath(scope_entry)
    if entry_path.is_absolute():
        try:
            entry_path.relative_to(PurePosixPath(repo_root.as_posix()))
        except ValueError:
            return True
        return False

    composed = PurePosixPath(repo_root.as_posix()) / entry_path
    normalized = os.path.normpath(str(composed))
    root_normalized = os.path.normpath(repo_root.as_posix())
    return not (
        normalized == root_normalized
        or normalized.startswith(root_normalized + os.sep)
    )


def collapse_scope_file_count(context: PredicateContext) -> dict[str, Any]:
    """`:105(3a)` — `gates.substrate.collapse.scope_file_count_le_2` and
    `.scope_file_count`, a literal count over the plan's `scope:` list.

    `undetermined` when no plan (or no `scope:` list) is in hand.
    """
    scope = _scope_list(context)
    if scope is None:
        return undetermined("no plan_frontmatter with a scope: list in context")
    count = len(scope)
    return {
        "scope_file_count": count,
        "scope_file_count_le_2": count <= _SCOPE_FILE_COUNT_THRESHOLD,
    }


def collapse_no_cross_repo_contract(context: PredicateContext) -> dict[str, Any]:
    """`:105(3d)` — `gates.substrate.collapse.no_cross_repo_contract` and
    `.crossing_paths`, over the plan's `scope:` list against
    `context.repo_root`.

    `undetermined` when no plan (or no `scope:` list) is in hand.
    """
    scope = _scope_list(context)
    if scope is None:
        return undetermined("no plan_frontmatter with a scope: list in context")
    crossing = [
        entry for entry in scope if _crosses_repo_root(context.repo_root, entry)
    ]
    return {
        "crossing_paths": crossing,
        "no_cross_repo_contract": not crossing,
    }


__all__ = ["collapse_scope_file_count", "collapse_no_cross_repo_contract"]
