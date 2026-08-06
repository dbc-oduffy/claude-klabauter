"""coordinator_core.git -- package namespace for shared git-plumbing
primitives used across the bash_guards commit checks and their selectors.

Purpose: a home for predicates that must compute the SAME answer wherever
they are consulted -- starting with `divergence.diverging_paths`, extracted
from `bash_guards.commit_tripwires` so Check 13's advisory and any future
selector reading the same index/worktree divergence do not carry two
independently-driftable copies of the same three-git-call computation.

Negative-spec: keep this file minimal -- a thin namespace marker only.

Spec backlink: docs/plans/2026-07-27-commit-mechanism-selection.md § C1
"""
