"""An override AUDIT write must never mint a session directory.

Both override-logging call sites (`commit_tripwires._log_pathspec_divergence_
override` and `dispatch_checks`'s `COORDINATOR_OVERRIDE_BLANKET_ADD` leg) used
to `os.makedirs(<sessions>/<sid>, exist_ok=True)` for whatever `session_id`
they were handed. `liveness.live_session_ids` enumerates every non-denylisted
child of `.git/coordinator-sessions/` as a SESSION, so an audit write could
manufacture a phantom session into the corpus claim attribution and scope
computation read.

Found 2026-08-19 via `test_every_non_uuid_real_child_is_denylisted_or_a_file`,
which had gone red against this repo's own hub with nine such directories in
it — several still accruing writes, so deleting them without fixing the
writers would not have held.
"""

from __future__ import annotations

import os

from coordinator_core.bash_guards._override_log_path import (
    NO_SESSION_BUCKET,
    _override_log_path,
)


def _sessions_root(tmp_path):
    return tmp_path / ".git" / "coordinator-sessions"


def test_unknown_session_lands_in_the_no_session_bucket(tmp_path):
    """The line must still be RECORDED — this is the audit trail for a
    deliberately-overridden safety check, so dropping it to avoid minting a
    directory would trade a bookkeeping defect for a security one."""
    path = _override_log_path(str(tmp_path), "sess-not-a-real-session")

    assert path is not None
    assert os.path.basename(os.path.dirname(path)) == NO_SESSION_BUCKET
    assert not (_sessions_root(tmp_path) / "sess-not-a-real-session").exists()


def test_existing_session_dir_is_used_as_is(tmp_path):
    own = _sessions_root(tmp_path) / "11111111-2222-3333-4444-555555555555"
    own.mkdir(parents=True)

    path = _override_log_path(str(tmp_path), own.name)

    assert path == str(own / "overrides.log")


def test_absent_session_id_uses_the_bucket_directly(tmp_path):
    path = _override_log_path(str(tmp_path), None)

    assert path is not None
    assert os.path.basename(os.path.dirname(path)) == NO_SESSION_BUCKET


def test_no_git_root_resolves_nothing(tmp_path):
    assert _override_log_path("", "sess-x") is None


def test_override_log_bucket_is_denylisted():
    """The fallback bucket is only safe because `live_session_ids` refuses to
    read it as a session. This module keeps its own copy of the name to stay
    off the session package on the commit hot path, so pin the two together —
    a rename on either side must fail here, not silently start minting
    phantom sessions again."""
    from coordinator_core.session import liveness

    assert NO_SESSION_BUCKET in liveness._NON_SESSION_DIR_NAMES
