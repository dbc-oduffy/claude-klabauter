"""coordinator_core.bash_guards._override_log_path — resolve the directory a
per-session AUDIT line is appended to, without ever minting a session directory.

Why this is shared rather than inlined at each call site: both writers
(`commit_tripwires._log_pathspec_divergence_override`,
`dispatch_checks`'s `COORDINATOR_OVERRIDE_BLANKET_ADD` leg) previously did
`os.makedirs(<sessions>/<sid>, exist_ok=True)` for whatever `session_id` they
were handed. `liveness.live_session_ids` enumerates every non-denylisted
child of `.git/coordinator-sessions/` as a SESSION, so an audit write could
manufacture a phantom session into the corpus that claim attribution and
scope computation both read. Two copies of that rule would drift; one will
not.

The fallback matters as much as the guard. These lines are the audit trail
for a deliberately-overridden safety check, so DROPPING one to avoid minting
a directory would trade a bookkeeping defect for a security-visibility one.
Instead an unknown session's line lands in the `no-session` bucket, which is
already a member of `liveness._NON_SESSION_DIR_NAMES` and therefore never
read as a session. The override stays recorded; nothing phantom is created.

Contrast `write_guards.guard_doctrine_surface_edits._log_repo_identity_gate`,
which drops its line outright in the same situation: that one is an ADVISORY
observation with no security content, so silence costs nothing there.

`session_audit_log_dir` generalizes the same rule for the DENY logs the three
subagent write-blockers keep (`block_subagent_plan_body_write`,
`block_subagent_plan_body_bash_write`, `block_subagent_archive_write`). Each of
those did its own `<hub>/<sid>` mkdir, which is the constructor bypass
`session/core.py::ensure_session` now owns: a deny audit line is not a session
and must never mint one. They carry security content, so they take the bucket
rather than dropping the line.
"""

from __future__ import annotations

import os
from typing import Optional

#: Mirrors the one entry of `liveness._NON_SESSION_DIR_NAMES` this module
#: needs. Deliberately NOT an import: these guards run in a PreToolUse hook
#: on the commit hot path, where pulling in the session package for one
#: string is cost the hook cannot justify. If the name ever changes,
#: `test_override_log_bucket_is_denylisted` fails — it imports the real set
#: and compares.
NO_SESSION_BUCKET = "no-session"


def session_audit_log_dir(git_root: str, session_id: Optional[str]) -> Optional[str]:
    """Return the directory a per-session audit line may be appended to —
    the session's OWN directory when it already exists, otherwise the
    denylisted `no-session` bucket — creating only a directory that is safe
    to create; `None` if no path resolves.

    Never mints `<hub>/<sid>`. `session/core.py::ensure_session` is the only
    constructor of a session directory, and an audit writer is not it: a
    directory minted here is enumerated as a SESSION by
    `liveness.live_session_ids`, with no `meta.json` for any peer to read.
    """
    if not git_root:
        return None
    sessions_root = os.path.join(git_root, ".git", "coordinator-sessions")
    sid = session_id or NO_SESSION_BUCKET
    sid_dir = os.path.join(sessions_root, sid)
    if sid != NO_SESSION_BUCKET and not os.path.isdir(sid_dir):
        sid_dir = os.path.join(sessions_root, NO_SESSION_BUCKET)
    # Only ever the `no-session` bucket or a directory that already exists.
    os.makedirs(sid_dir, exist_ok=True)
    return sid_dir


def _override_log_path(git_root: str, session_id: Optional[str]) -> Optional[str]:
    """Return the `overrides.log` path to append to, creating only a
    directory that is safe to create; `None` if no path resolves.

    A session's OWN directory is used when it already exists (created by
    `core.init`). Otherwise the line goes to the `no-session` bucket rather
    than minting `<sid>/`.
    """
    sid_dir = session_audit_log_dir(git_root, session_id)
    if sid_dir is None:
        return None
    return os.path.join(sid_dir, "overrides.log")
