"""
coordinator_core.ops.session — Class-B session-substrate op package.

Purpose: Package namespace for ops that mutate untracked .git/coordinator-sessions/
substrate.  Class-B ops do NOT use git commit or archive_and_commit — they operate
on untracked directories inside the git dir and carry their own safety spec
(per-record idempotency, recency liveness, fail-closed-to-keep).

Spec backlink: pln-strang-11-b8-session-init-boot-f78455 § C4 / § Design-decisions #2
"""
