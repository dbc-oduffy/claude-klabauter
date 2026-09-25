"""
coordinator_core.session.tests.test_release_path_claim_reaches_agent_rows —
verify-then-fix pin for P088-C4 (docs/plans/2026-09-11-session-identity-
residue-memo-send-sent.md).

Reported (2026-09-11 inbox triage): a dispatched agent's path-touch claim row
is allegedly unreachable by `_release_path_claim_artifact` because it is
filed under the agent's OWN uuid rather than the EM's session id, so the
self-release scoped to `{my_sid}` misses it.

VERIFIED, NOT DESIGNED: `claim_index._enumerate_claim_sinks` already resolves
each agent-plane sink's `claimant_sid` through the `em-session-id.txt`
back-pointer (`_agent_owner_sid`), not through the agent directory's own
uuid — see `_enumerate_claim_sinks`'s own docstring, "agent dirs via their
`em-session-id.txt` back-pointer ... the OWNER sid stays the back-pointer
target". `_release_path_claim_everywhere` filters those triples on
`claimant_sid in sids`, so a `{my_sid}`-scoped release from
`_release_path_claim_artifact` already reaches an agent's touch-record row
whose back-pointer names `my_sid` — the fan-out this row worried was missing
is already covered by the enumerator, and this file pins that as green, not
red.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.session import claim_index, claims, core, scope, touch_record
from coordinator_core.win_portability import no_console_passthrough_kwargs

# `_make_repo` spawns real git (init/config/add/commit) because the
# production code under test resolves the git-common-dir via a real repo,
# mirroring test_claims.py's own spawn-marking convention for this fixture.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=tmp_path,
        **no_console_passthrough_kwargs(),
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=tmp_path, **no_console_passthrough_kwargs()
    )
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=tmp_path, **no_console_passthrough_kwargs()
    )
    return tmp_path


def _set_me(monkeypatch, sid="em-sid"):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _make_agent_dir_with_touch(repo, agent_id, owner_sid, path):
    """Construct `.agents/<agent_id>/` with an `em-session-id.txt`
    back-pointer to *owner_sid* and a `touch-record.jsonl` T-event for
    *path* — the row's own foreign-uuid scenario: the claim row is filed
    under the AGENT's directory name (its uuid), never under *owner_sid*
    directly."""
    adir = Path(repo) / ".git" / "coordinator-sessions" / ".agents" / agent_id
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "em-session-id.txt").write_text(owner_sid + "\n", encoding="utf-8")
    sink = adir / scope._TOUCH_RECORD_FILENAME
    touch_record.append_event(
        sink,
        session_id=owner_sid,
        agent_id=agent_id,
        verb=touch_record.VERB_TOUCH,
        path=path,
    )
    return adir


class TestReleasePathClaimReachesAgentRows:
    _TARGET = "src/dispatched_agent_owned.py"

    def test_self_release_reaches_a_dispatched_agents_touch_row(self, tmp_path, monkeypatch):
        """REFUTED, NOT REPRODUCED: the reported gap does not reproduce. The
        agent row's claimant_sid is already the EM's own sid (the
        back-pointer target), so `_release_path_claim_artifact`'s
        `{my_sid}`-scoped fan-out already reaches it without widening the
        `sids` set."""
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch, sid="em-sid")
        _make_agent_dir_with_touch(repo, "agent-uuid-1234", "em-sid", self._TARGET)

        base = core.sessions_dir(cwd=str(repo))
        assert claim_index.lookup([self._TARGET], sessions_dir=base, cwd=str(repo)) == {
            self._TARGET: ["em-sid"]
        }

        assert claims._release_path_claim_artifact(self._TARGET, cwd=str(repo)) is True

        assert claim_index.lookup([self._TARGET], sessions_dir=base, cwd=str(repo)) == {
            self._TARGET: []
        }

    def test_non_owning_sessions_agent_row_is_left_alone(self, tmp_path, monkeypatch):
        """Negative control: an agent row back-pointed to a DIFFERENT session
        is never released by this session's self-release call — the
        identity-checked contract `_release_path_claim_artifact`'s own
        docstring states ("releases only what THIS session ... holds, never
        a peer's claim") still holds once the fan-out is exercised."""
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch, sid="em-sid")
        _make_agent_dir_with_touch(repo, "agent-uuid-5678", "peer-sid", self._TARGET)

        base = core.sessions_dir(cwd=str(repo))
        assert claim_index.lookup([self._TARGET], sessions_dir=base, cwd=str(repo)) == {
            self._TARGET: ["peer-sid"]
        }

        assert claims._release_path_claim_artifact(self._TARGET, cwd=str(repo)) is True

        assert claim_index.lookup([self._TARGET], sessions_dir=base, cwd=str(repo)) == {
            self._TARGET: ["peer-sid"]
        }
