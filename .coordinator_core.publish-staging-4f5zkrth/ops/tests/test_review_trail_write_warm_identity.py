"""
coordinator_core.ops.tests.test_review_trail_write_warm_identity — the
``review_trail.write`` handler stamps the CALLING session, not the process
environment.

Purpose: pin the fix for the own-commit half of
``state/bug-backlog/2026-08-19-inherited-chain-commit-review-records-are-unwritable.yaml``
(spec: ``state/handoffs/2026-08-19-review-trail-cannot-record-a-partitioned-close.md``
§ D1).

The defect: ``_review_trail_write_handler`` resolved session identity with a raw
``os.environ`` read. Under warm serving that handler runs inside a long-lived
server process whose environment names WHOEVER SPAWNED IT, not the session whose
request it is serving. Every record was therefore stamped with a stranger's id,
and — the expensive part — ``_guard_foreign_session_range`` compared each
commit's ``Session-Id`` trailer against that stranger, so a session's OWN
correctly-trailered commits read as foreign and the write was refused. Measured
2026-08-19: a fully-reviewed, correctly-partitioned close wrote no review trail
at all, ten refusals out of ten.

``coordinator_core.warm.entry_seam.per_request_state`` already binds the true
caller's cold-resolved identity for the duration of each dispatch, via
``session.core.session_identity_override``. That binding is a ContextVar: only a
resolver that reads it can see it, and a raw ``os.environ`` read steps straight
past it. These tests pin that the handler reads the binding.

Negative-spec:
    - The override MUST beat the process environment, and the two are set to
      DIFFERENT values here on purpose. A test that leaves them equal passes
      against the defect and proves nothing.
    - Identity must NOT be readable from ``params``. It is an input to an
      anti-forgery guard; a wire-supplied value would let any caller claim any
      session's commits. ``ipc.py`` does not strip unknown params keys, so the
      key can arrive over the wire — it must be ignored, and
      ``test_params_session_id_is_ignored`` pins that.
    - No git spawns: these run on the fast tier deliberately. The handler's
      foreign-session guard is a documented no-op against a caller_worktree that
      is not a git work tree, which is what makes a tmp_path fixture enough to
      exercise identity resolution.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from coordinator_core.ops.review_trail_write import _review_trail_write_handler
from coordinator_core.session.core import session_identity_override

#: Deliberately UUID-shaped: `session_identity_override` validates its argument
#: and silently no-ops on anything that is not, so a non-UUID sentinel here
#: would make every assertion below vacuous.
_CALLER_SID = "11111111-2222-3333-4444-555555555555"
_SERVER_ENV_SID = "99999999-8888-7777-6666-555555555555"


def _params(**overrides) -> dict:
    params = {
        "sha_range": "aaaaaaaaaaaa..bbbbbbbbbbbb",
        "reviewer": "code-reviewer",
        "scope": "session",
        "verdict": "pending",
        "diff_loc": 1,
        "scope_kind": "diff",
        "workstream": "warm-identity-pin",
    }
    params.update(overrides)
    return params


@pytest.fixture
def server_env(monkeypatch):
    """A process environment naming a session that is NOT the caller — the
    warm server's own spawner."""
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SERVER_ENV_SID)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SERVER_ENV_SID)


@pytest.fixture
def common_dir(tmp_path):
    """What ``ipc.py``'s ``_OP_KEY_SCOPE: common_dir`` hands the handler: the
    repo's shared ``.git`` directory, from which it derives the worktree root.

    A bare ``tmp_path`` is refused by ``main_worktree_root`` (correctly — it
    refuses to guess), and the resulting directory is deliberately NOT a real
    git work tree, which is the documented test-isolation contract that makes
    the foreign-session guard a no-op here. Identity resolution is what these
    tests exercise; the guard has its own suite.
    """
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    return git_dir


def _write(common_dir, params: dict) -> dict:
    return asyncio.run(_review_trail_write_handler(params, common_dir))


def _record(result: dict) -> dict:
    with open(result["out_path"], encoding="utf-8") as handle:
        return json.loads(handle.read())


class TestWarmCallerIdentity:
    def test_override_beats_process_env(self, common_dir, server_env):
        """The bound caller identity wins over the serving process's env — the
        defect itself, inverted."""
        with session_identity_override(_CALLER_SID):
            result = _write(common_dir, _params())

        assert result["session_id"] == _CALLER_SID
        assert _record(result)["session_id"] == _CALLER_SID

    def test_filename_carries_the_caller_not_the_server(self, common_dir, server_env):
        """Record filenames are ``{TIMESTAMP}-{SESSION_ID[:8]}.json``. The
        stranger's id leaked into the FILENAME too, so a corpus reader
        partitioning records by session mis-bucketed every warm-served write —
        pinned separately because the payload assertion above does not cover it.
        """
        with session_identity_override(_CALLER_SID):
            result = _write(common_dir, _params())

        name = str(result["out_path"]).replace("\\", "/").rsplit("/", 1)[-1]
        assert name.endswith(f"-{_CALLER_SID[:8]}.json")
        assert _SERVER_ENV_SID[:8] not in name

    def test_no_override_falls_through_to_env(self, common_dir, server_env):
        """The cold path is unchanged: with nothing bound, the env ladder still
        resolves identity. This is the whole non-warm fleet, so a fix that only
        honoured the override would be a regression, not a fix."""
        result = _write(common_dir, _params())

        assert result["session_id"] == _SERVER_ENV_SID

    def test_params_session_id_is_ignored(self, common_dir, server_env):
        """A wire-supplied ``session_id`` must not steer identity — see this
        module's negative-spec. Pinned because ``ipc.py`` forwards unknown
        params keys, so this one reaches the handler and must die there."""
        forged = "deadbeef-dead-beef-dead-beefdeadbeef"
        with session_identity_override(_CALLER_SID):
            result = _write(common_dir, _params(session_id=forged))

        assert result["session_id"] == _CALLER_SID
        assert result["session_id"] != forged

    def test_unresolvable_identity_refuses(self, common_dir, monkeypatch):
        """No override and no env is "session unknown", not "write it anyway" —
        a record with a fabricated or blank owner is worse than no record."""
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        with pytest.raises(ValueError, match="could not resolve session_id"):
            _write(common_dir, _params())


class TestNoLocalSessionEnvConstants:
    def test_module_declares_no_session_env_names(self):
        """The defect's shape was a module-local env-var name read directly.
        Reintroducing one is how it comes back, so the absence is pinned rather
        than left to review."""
        import coordinator_core.ops.review_trail_write as module

        offenders = [
            name
            for name, value in vars(module).items()
            if isinstance(value, str)
            and value in {"CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "COORDINATOR_SESSION_ID"}
        ]
        assert offenders == [], (
            "review_trail_write must resolve session identity through "
            "resolve_current_session_id, never a module-local env-var name: "
            f"{offenders}"
        )
