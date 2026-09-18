"""
coordinator_core.ops.tests.test_session_hierarchy_derive_warm_identity —
``session_hierarchy.derive`` stamps the CALLING session's identity, not the
warm server's own process environment.

Purpose: pin the fix landed under
``docs/plans/2026-09-06-partitioned-close-review-identity-triage.md`` C1
(AC2b) — ``_run`` resolved the ``created_by_session`` field with a raw
``os.environ.get("CS_SESSION_ID", "")`` read. ``session_hierarchy_derive``
registers ``@register_op("session_hierarchy.derive")`` directly, so it is
warm-reachable by construction (``warm.client.try_warm_dispatch`` applies no
method allowlist — see ``test_warm_identity_env_reads.py``'s module
docstring, REACHABILITY RULE). Under a warm-served request ``os.environ``
names whoever spawned the resident server, not the session whose request is
being served, so every derived ``state/session-hierarchy.<slug>.json`` shard
stamped the spawner's id onto every record's ``system.created_by_session``
field — the identical defect/fix shape ``ops/queue_append.py`` and
``ops/handoff_correct_body.py`` already carry (D1's applied pattern), and the
same field name.

Negative-spec:
    - The bound `session_identity_override` MUST beat the process
      environment, and the two are set to DIFFERENT values here on purpose —
      a test that leaves them equal passes against the defect and proves
      nothing (mirrors the deleted
      ``test_review_trail_write_warm_identity.py::test_override_beats_process_env``).
    - The cold path (no override bound) is unchanged: falls through to the
      same env ladder ``resolve_current_session_id`` already covers.
    - No git spawns: `_run` is exercised directly with `query_records`,
      `derive`, and `_atomic_write_json` patched out, same seam
      ``test_session_hierarchy_derive_repo_root.py`` already uses.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from coordinator_core.ops import session_hierarchy_derive as sut
from coordinator_core.session.core import session_identity_override

#: Deliberately UUID-shaped: `session_identity_override` validates its
#: argument and silently no-ops on anything that is not, so a non-UUID
#: sentinel here would make every assertion below vacuous.
_CALLER_SID = "11111111-2222-3333-4444-555555555555"
_SERVER_ENV_SID = "99999999-8888-7777-6666-555555555555"


def _run_and_capture_created_by_session(worktree_root: Path) -> str:
    """Run `_run`, capturing the `created_by_session` positional arg
    `derive()` was called with — the value that lands on every record's
    `system.created_by_session` field."""
    (worktree_root / "state").mkdir(parents=True, exist_ok=True)
    with mock.patch.object(
        sut, "query_records", return_value=[]
    ), mock.patch.object(
        sut, "derive", return_value=[]
    ) as mock_derive, mock.patch.object(
        sut, "_atomic_write_json"
    ):
        sut._run(worktree_root)
    args, _kwargs = mock_derive.call_args
    return args[2]


class TestWarmCallerIdentity:
    def test_override_beats_process_env(self, tmp_path, monkeypatch):
        """The bound caller identity wins over the serving process's env —
        the defect itself, inverted."""
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_SESSION_ID", _SERVER_ENV_SID)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SERVER_ENV_SID)

        with session_identity_override(_CALLER_SID):
            created_by_session = _run_and_capture_created_by_session(tmp_path)

        assert created_by_session == _CALLER_SID
        assert created_by_session != _SERVER_ENV_SID

    def test_no_override_falls_through_to_env(self, tmp_path, monkeypatch):
        """The cold path is unchanged: with nothing bound, the env ladder
        still resolves identity. This is the whole non-warm fleet, so a fix
        that only honoured the override would be a regression, not a fix."""
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_SESSION_ID", _SERVER_ENV_SID)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SERVER_ENV_SID)

        created_by_session = _run_and_capture_created_by_session(tmp_path)

        assert created_by_session == _SERVER_ENV_SID
