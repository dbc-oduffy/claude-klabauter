"""
coordinator_core.ops.session — Class-B session-substrate op package.

Purpose: Package namespace for ops that mutate untracked .git/coordinator-sessions/
substrate.  Class-B ops do NOT use git commit or archive_and_commit — they operate
on untracked directories inside the git dir and carry their own safety spec
(per-record idempotency, recency liveness, fail-closed-to-keep).

Spec backlink: docs/plans/2026-07-06-strang-11-b8-session-init-op-absorption.md § C4 / § Design-decisions #2
"""
