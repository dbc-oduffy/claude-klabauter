from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Any

from coordinator_core.plan_assemble.predicates import PredicateContext, undetermined

_SCOPE_FILE_COUNT_THRESHOLD = 2


def _scope_list(context: PredicateContext) -> list[str] | None:
    if context.plan_frontmatter is None:
        return None
    scope = context.plan_frontmatter.get("scope")
    if not isinstance(scope, list):
        return None
    return [str(entry) for entry in scope]


def _crosses_repo_root(repo_root: Path, scope_entry: str) -> bool:
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
    scope = _scope_list(context)
    if scope is None:
        return undetermined("no plan_frontmatter with a scope: list in context")
    count = len(scope)
    return {
        "scope_file_count": count,
        "scope_file_count_le_2": count <= _SCOPE_FILE_COUNT_THRESHOLD,
    }


def collapse_no_cross_repo_contract(context: PredicateContext) -> dict[str, Any]:
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
