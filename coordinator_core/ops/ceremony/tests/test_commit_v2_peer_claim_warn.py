"""A5/DD4: `ceremony.commit_v2` warns (never refuses) when a live peer
holds a touch-claim on a path this call is committing.

Mirrors `test_commit_v2_claim_release.py`'s fixture shape (real git repo,
real `session_core`/`session_scope` claim writes) rather than mocking the
claim substrate -- the property under test is the wiring between
`claim_index.lookup`, `liveness.session_live`, and the warnings this
handler returns, which a mock of either module would beg the question of.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.session import core as session_core
from coordinator_core.session import liveness
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


def _own_sid(monkeypatch, sid: str) -> None:
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _call(repo: Path, params: dict) -> dict:
    return commit_v2._handler(params, repo_root=repo / ".git")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _seed_tracked(repo: Path, rel: str, body: str) -> None:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body, encoding="utf-8")
    _git(["add", "--", rel], repo)
    _git(["commit", "-q", "-m", "seed " + rel], repo)


def test_live_peer_holder_warns_and_still_commits(repo, monkeypatch):
    peer_sid = "commit-v2-peer-claim-live"
    rel = "state/handoff-tracker.md"
    _seed_tracked(repo, rel, "v1\n")

    session_core.init(peer_sid, cwd=str(repo))
    session_scope.touch(peer_sid, rel, cwd=str(repo))

    (repo / rel).write_text("v2\n", encoding="utf-8")

    sid = "commit-v2-peer-claim-self"
    _own_sid(monkeypatch, sid)

    result = _call(repo, {"paths": [rel], "message": "update tracker"})

    assert result["committed"] is True
    assert any(
        rel in w and peer_sid in w for w in result["warnings"]
    ), result["warnings"]
    body = subprocess.run(
        ["git", "log", "-1", "--format=%B"], cwd=str(repo), capture_output=True,
        text=True, check=True, **no_console_creationflags(),
    ).stdout
    assert f"{commit_v2.ABSORBED_PEER_CLAIM_PREFIX} {peer_sid} {rel}" in body, body


def test_dead_holder_no_warning(repo, monkeypatch):
    peer_sid = "commit-v2-peer-claim-dead"
    rel = "state/handoff-tracker.md"
    _seed_tracked(repo, rel, "v1\n")

    session_core.init(peer_sid, cwd=str(repo))
    session_scope.touch(peer_sid, rel, cwd=str(repo))
    monkeypatch.setattr(
        commit_v2.session_liveness, "session_live", lambda sid, cwd=None: False
    )

    (repo / rel).write_text("v2\n", encoding="utf-8")

    sid = "commit-v2-peer-claim-self2"
    _own_sid(monkeypatch, sid)

    result = _call(repo, {"paths": [rel], "message": "update tracker"})

    assert result["committed"] is True
    assert result["warnings"] == []


def test_self_held_no_warning(repo, monkeypatch):
    rel = "state/handoff-tracker.md"
    _seed_tracked(repo, rel, "v1\n")

    sid = "commit-v2-peer-claim-self-held"
    _own_sid(monkeypatch, sid)
    session_core.init(sid, cwd=str(repo))
    session_scope.touch(sid, rel, cwd=str(repo))

    (repo / rel).write_text("v2\n", encoding="utf-8")

    result = _call(repo, {"paths": [rel], "message": "update tracker"})

    assert result["committed"] is True
    assert result["warnings"] == []


def test_lookup_raises_indeterminate_warning_still_commits(repo, monkeypatch):
    rel = "state/handoff-tracker.md"
    _seed_tracked(repo, rel, "v1\n")
    (repo / rel).write_text("v2\n", encoding="utf-8")

    def _boom(paths, cwd=None):
        raise RuntimeError("simulated claim_index failure")

    monkeypatch.setattr(commit_v2.session_claim_index, "lookup", _boom)

    result = _call(repo, {"paths": [rel], "message": "update tracker"})

    assert result["committed"] is True
    assert any("indeterminate" in w for w in result["warnings"])


def test_both_message_lines_present_for_mixed_paths(repo, monkeypatch):
    live_rel = "state/live-tracker.md"
    dead_rel = "state/dead-tracker.md"
    _seed_tracked(repo, live_rel, "v1\n")
    _seed_tracked(repo, dead_rel, "v1\n")

    live_peer = "commit-v2-peer-claim-mixed-live"
    dead_peer = "commit-v2-peer-claim-mixed-dead"
    session_core.init(live_peer, cwd=str(repo))
    session_scope.touch(live_peer, live_rel, cwd=str(repo))
    session_core.init(dead_peer, cwd=str(repo))
    session_scope.touch(dead_peer, dead_rel, cwd=str(repo))

    real_session_live = commit_v2.session_liveness.session_live

    def _fake_live(sid, cwd=None):
        if sid == dead_peer:
            return False
        return real_session_live(sid, cwd)

    monkeypatch.setattr(commit_v2.session_liveness, "session_live", _fake_live)

    (repo / live_rel).write_text("v2\n", encoding="utf-8")
    (repo / dead_rel).write_text("v2\n", encoding="utf-8")

    sid = "commit-v2-peer-claim-mixed-self"
    _own_sid(monkeypatch, sid)

    result = _call(
        repo, {"paths": [live_rel, dead_rel], "message": "update trackers"}
    )

    assert result["committed"] is True
    joined = " ".join(result["warnings"])
    assert live_peer in joined and live_rel in joined
    assert dead_peer not in joined


def _age_session(repo: Path, sid: str, *, record_files: bool) -> None:
    """Age meta `last_activity` past the abandonment window; with `record_files`, every regular file too."""
    sdir = Path(session_core.session_dir(sid, str(repo)))
    meta = sdir / "meta.json"
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["last_activity"] = "2020-01-01T00:00:00Z"
    meta.write_text(json.dumps(data), encoding="utf-8")
    if record_files:
        old = time.time() - 2 * liveness._ABANDONMENT_WINDOW_SEC
        for f in sdir.iterdir():
            if f.is_file():
                os.utime(f, (old, old))


def _live_holder(repo, monkeypatch, peer_sid, rel):
    _seed_tracked(repo, rel, "v1\n")
    session_core.init(peer_sid, cwd=str(repo))
    session_scope.touch(peer_sid, rel, cwd=str(repo))
    monkeypatch.setattr(
        commit_v2.session_liveness, "session_live", lambda sid, cwd=None: True
    )
    (repo / rel).write_text("v2\n", encoding="utf-8")
    _own_sid(monkeypatch, peer_sid + "-self")


def test_abandoned_live_holder_is_released_and_disclosed(repo, monkeypatch):
    peer_sid = "commit-v2-peer-claim-abandoned"
    rel = "state/handoff-tracker.md"
    _live_holder(repo, monkeypatch, peer_sid, rel)
    _age_session(repo, peer_sid, record_files=True)

    result = _call(repo, {"paths": [rel], "message": "update tracker"})

    assert result["committed"] is True
    assert any(rel in w and peer_sid in w and "released" in w for w in result["warnings"]), result["warnings"]
    body = subprocess.run(
        ["git", "log", "-1", "--format=%B"], cwd=str(repo), capture_output=True,
        text=True, check=True, **no_console_creationflags(),
    ).stdout
    assert f"{commit_v2.ABSORBED_PEER_CLAIM_PREFIX} {peer_sid} {rel}" in body, body

    peer_holds: list = []
    abandoned_holds: list = []
    commit_v2._peer_claim_warnings(
        repo, [rel], peer_holds, own_sid="someone-else", abandoned_holds=abandoned_holds
    )
    assert abandoned_holds == [(rel, peer_sid)]
    assert peer_holds == []


def test_last_activity_aged_only_is_still_a_peer_hold(repo, monkeypatch):
    peer_sid = "commit-v2-peer-claim-fresh-record"
    rel = "state/handoff-tracker.md"
    _live_holder(repo, monkeypatch, peer_sid, rel)
    _age_session(repo, peer_sid, record_files=False)

    peer_holds: list = []
    abandoned_holds: list = []
    commit_v2._peer_claim_warnings(
        repo, [rel], peer_holds, own_sid="someone-else", abandoned_holds=abandoned_holds
    )
    assert peer_holds == [(rel, peer_sid)]
    assert abandoned_holds == []


def test_session_abandoned_raising_keeps_the_live_hold(repo, monkeypatch):
    peer_sid = "commit-v2-peer-claim-abandon-raises"
    rel = "state/handoff-tracker.md"
    _live_holder(repo, monkeypatch, peer_sid, rel)

    def _boom(sid, cwd=None):
        raise RuntimeError("simulated session_abandoned failure")

    monkeypatch.setattr(commit_v2.session_liveness, "session_abandoned", _boom)

    peer_holds: list = []
    abandoned_holds: list = []
    warnings = commit_v2._peer_claim_warnings(
        repo, [rel], peer_holds, own_sid="someone-else", abandoned_holds=abandoned_holds
    )
    assert peer_holds == [(rel, peer_sid)]
    assert abandoned_holds == []
    assert any(peer_sid in w for w in warnings)


def test_claimant_holding_two_paths_gets_one_liveness_verdict(repo, monkeypatch):
    peer_sid = "commit-v2-peer-claim-two-paths"
    rel_a, rel_b = "state/a-tracker.md", "state/b-tracker.md"
    _seed_tracked(repo, rel_b, "v1\n")
    _live_holder(repo, monkeypatch, peer_sid, rel_a)
    session_scope.touch(peer_sid, rel_b, cwd=str(repo))

    live_calls: list = []
    abandoned_calls: list = []
    monkeypatch.setattr(
        commit_v2.session_liveness, "session_live",
        lambda sid, cwd=None: live_calls.append(sid) or True,
    )
    monkeypatch.setattr(
        commit_v2.session_liveness, "session_abandoned",
        lambda sid, cwd=None: abandoned_calls.append(sid) or False,
    )

    peer_holds: list = []
    warnings = commit_v2._peer_claim_warnings(
        repo, [rel_a, rel_b], peer_holds, own_sid="someone-else"
    )

    assert live_calls == [peer_sid]
    assert abandoned_calls == [peer_sid]
    assert sorted(peer_holds) == sorted([(rel_a, peer_sid), (rel_b, peer_sid)])
    assert len([w for w in warnings if peer_sid in w]) == 2
