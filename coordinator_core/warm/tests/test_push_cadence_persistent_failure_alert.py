"""Persistent push failure: one alert per streak to the live G-EM and the owning sessions."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.group_em import nomination
from coordinator_core.ops.dispatch_emit import halt_split_tree as hst
from coordinator_core.session import core as session_core
from coordinator_core.warm import push_cadence as pc

BRANCH = "example-operator/machine-a/2026-11-19"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "repo"
    git_dir = root / ".git"
    git_dir.mkdir(parents=True)
    hub = tmp_path / "hub"
    hub.mkdir()
    sent: list = []
    monkeypatch.setattr(pc, "head_branch", lambda r: BRANCH)
    monkeypatch.setattr(pc, "resolve_git_common_dir", lambda r: git_dir)
    monkeypatch.setattr(session_core, "sessions_dir", lambda cwd=None: str(hub))
    monkeypatch.setattr(session_core, "stable_pid_alive", lambda pid, lstart="", epoch="": True)
    monkeypatch.setattr(hst, "_ALERT_SENDER", lambda name, text: sent.append((name, text)))
    monkeypatch.setattr(
        nomination, "identify", lambda r: {"state": "live", "holder": {"name": "gem-1"}}
    )
    pc.reset_cadence_for_test()
    yield root, git_dir, hub, sent
    pc.reset_cadence_for_test()


def _session(hub: Path, sid: str, name: str, branch: str) -> None:
    d = hub / sid
    d.mkdir()
    (d / "meta.json").write_text(
        json.dumps({"name": name, "branch": branch, "stable_pid": "4242", "stable_pid_start_epoch": "1"}),
        encoding="utf-8",
    )


def _fail(root: Path, times: int) -> None:
    for _ in range(times):
        pc._note_sweep_result(root, "failed")


def test_third_failure_alerts_gem_and_owning_session_once(world) -> None:
    root, git_dir, hub, sent = world
    _session(hub, "s1", "em-one", BRANCH)
    (git_dir / "push-failures.log").write_text(
        f"[t] PUSH FAILED on {BRANCH} (cadence-sweep/sweep-failed after 3) :: auth denied :: stderr=<empty>\n",
        encoding="utf-8",
    )
    _fail(root, 2)
    assert sent == []
    _fail(root, 1)
    assert sorted(n for n, _ in sent) == ["em-one", "gem-1"]
    text = sent[0][1]
    assert text.startswith(f"PUSH FAILING: repo {BRANCH} 3 sweeps; last error [failed] auth denied;")
    assert text.endswith("readback: coordinator-invoke push.state")
    _fail(root, 1)
    assert len(sent) == 2


def test_success_rearms_the_alert(world) -> None:
    root, _, _, sent = world
    _fail(root, 3)
    n = len(sent)
    assert n == 1
    pc._note_sweep_result(root, "pushed")
    _fail(root, 3)
    assert len(sent) == 2 * n


@pytest.mark.parametrize("result", ["skipped", "held", "declined", None])
def test_non_failed_results_never_alert(world, result) -> None:
    root, _, _, sent = world
    for _ in range(5):
        pc._note_sweep_result(root, result)
    assert sent == []


def test_session_on_another_branch_is_not_messaged(world) -> None:
    root, _, hub, sent = world
    _session(hub, "s1", "em-one", BRANCH)
    _session(hub, "s2", "em-other", "example-operator/machine-a/2026-11-1")
    _fail(root, 3)
    assert sorted(n for n, _ in sent) == ["em-one", "gem-1"]


def test_dead_session_is_not_messaged(world, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _, hub, sent = world
    _session(hub, "s1", "em-one", BRANCH)
    monkeypatch.setattr(session_core, "stable_pid_alive", lambda *a, **k: False)
    _fail(root, 3)
    assert [n for n, _ in sent] == ["gem-1"]


def test_no_live_gem_still_messages_sessions(world, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _, hub, sent = world
    _session(hub, "s1", "em-one", BRANCH)
    monkeypatch.setattr(nomination, "identify", lambda r: {"state": "none"})
    _fail(root, 3)
    assert [n for n, _ in sent] == ["em-one"]


def test_log_row_for_a_prefix_branch_is_not_claimed(world) -> None:
    _, git_dir, _, _ = world
    (git_dir / "push-failures.log").write_text(
        "[t] PUSH FAILED on example-operator/machine-a/2026-11-1 (cadence-sweep/x after 3) :: wrong :: stderr=<empty>\n",
        encoding="utf-8",
    )
    assert pc._last_logged_error(world[0], BRANCH) is None


def _tracking(monkeypatch: pytest.MonkeyPatch, head: str, upstream: str) -> None:
    from coordinator_core.git import git_objects, git_state
    from coordinator_core.ops import push_outstanding

    monkeypatch.setattr(push_outstanding, "owned_push_remote", lambda r, b: ("origin", None))
    monkeypatch.setattr(git_state, "head_sha", lambda r: head)
    monkeypatch.setattr(git_objects, "read_ref_loose_or_packed", lambda d, ref: upstream)


def test_unconfirmed_sweeps_whose_tracking_ref_equals_head_never_alert(world, monkeypatch) -> None:
    root, git_dir, hub, sent = world
    _session(hub, "s1", "em-one", BRANCH)
    (git_dir / "push-failures.log").write_text(
        f"[t] PUSH UNCONFIRMED on {BRANCH} (cadence-sweep/sweep-unconfirmed after 1) :: timed out :: stderr=<empty>\n",
        encoding="utf-8",
    )
    _tracking(monkeypatch, "a" * 40, "a" * 40)
    _fail(root, 3)
    assert sent == []


def test_unconfirmed_sweeps_with_ref_behind_head_alert_with_the_verb(world, monkeypatch) -> None:
    root, git_dir, hub, sent = world
    (git_dir / "push-failures.log").write_text(
        f"[t] PUSH UNCONFIRMED on {BRANCH} (cadence-sweep/sweep-unconfirmed after 1) :: timed out :: stderr=<empty>\n",
        encoding="utf-8",
    )
    _tracking(monkeypatch, "a" * 40, "b" * 40)
    _fail(root, 3)
    assert [n for n, _ in sent] == ["gem-1"]
    assert "last error [unconfirmed] timed out;" in sent[0][1]


def test_sender_failure_never_raises(world, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _, hub, _ = world
    _session(hub, "s1", "em-one", BRANCH)

    def boom(name, text):
        raise RuntimeError("send failed")

    monkeypatch.setattr(hst, "_ALERT_SENDER", boom)
    _fail(root, 3)
