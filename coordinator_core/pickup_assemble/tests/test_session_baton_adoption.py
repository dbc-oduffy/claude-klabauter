from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

import coordinator_core.pickup_assemble as pa
import coordinator_core.pickup_brief as pb
from coordinator_core.session import liveness as liveness_mod
from coordinator_core.session_baton.store import read_baton

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


@pytest.fixture(autouse=True)
def _reset_registry_snapshot_cache():
    liveness_mod._registry_snapshot_cache = None
    yield
    liveness_mod._registry_snapshot_cache = None


from coordinator_core.pickup_assemble.tests._git_harness import (
    git as _git,
    init_repo as _init_repo,
)


def _seed_handoff(repo: Path, name: str, fm_extra: str = "") -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: active\n"
        f"{fm_extra}"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


@pytest.fixture
def as_session(monkeypatch):
    def _bind(sid: str) -> None:
        monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)

    return _bind


def _ensure_session_dir(repo: Path, sid: str) -> Path:
    sdir = repo / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    return sdir


def test_pickup_adopts_artifact_into_session_baton(tmp_path, as_session):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")
    _ensure_session_dir(repo, "sid-a")

    pb.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)

    record = read_baton("sid-a", cwd=str(repo))
    assert record["adopted_artifacts"] == ["state/handoffs/h1.md"]


def test_rebrief_same_artifact_does_not_duplicate(tmp_path, as_session):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")
    _ensure_session_dir(repo, "sid-a")

    pb.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)
    pb.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)

    record = read_baton("sid-a", cwd=str(repo))
    assert record["adopted_artifacts"] == ["state/handoffs/h1.md"]


def test_brief_survives_broken_baton_store(tmp_path, as_session, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")

    def _boom(*args, **kwargs):
        raise OSError("simulated baton-store failure")

    monkeypatch.setattr(pb, "merge_baton", _boom)

    result = pb.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)

    assert result is not None
    assert result.decision_object["artifact"]["path"] == "state/handoffs/h1.md"

def test_intent_prefers_session_goal_when_the_handoff_carries_one(tmp_path, as_session):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md", fm_extra="session_goal: Ship the thing.\n")
    as_session("sid-goal")
    _ensure_session_dir(repo, "sid-goal")

    pb.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)

    record = read_baton("sid-goal", cwd=str(repo))
    assert record["intent"] == "Ship the thing."
    assert "(from summary)" not in record["intent"]


def test_intent_falls_back_to_summary_and_says_so(tmp_path, as_session):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md", fm_extra="summary: What the session did.\n")
    as_session("sid-sum")
    _ensure_session_dir(repo, "sid-sum")

    pb.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)

    record = read_baton("sid-sum", cwd=str(repo))
    assert record["intent"] == "(from summary) What the session did."


def test_second_different_adoption_never_clobbers_first_intent(tmp_path, as_session):
    """A title-less
    first adoption can legitimately stamp `intent` alone (fail-open posture,
    § `_adopt_into_baton` docstring). A SECOND, DIFFERENT artifact adopted in
    the same session must not re-fire the naming block and overwrite that
    `intent`, even though `title` is still unset -- the only scenario where
    the docstring's "never clobbers the first one" claim is load-bearing.
    Calls `_adopt_into_baton` directly (not `brief()`) to construct the
    title-less-but-intent-bearing `fm` the schema's own required-`title`
    field would otherwise prevent seeding through a real handoff.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    as_session("sid-two")
    _ensure_session_dir(repo, "sid-two")

    pa._adopt_into_baton(repo, "state/handoffs/first.md", {"session_goal": "First goal."})
    pa._adopt_into_baton(
        repo, "state/handoffs/second.md", {"title": "Second Title", "session_goal": "Second goal."}
    )

    record = read_baton("sid-two", cwd=str(repo))
    assert record["intent"] == "First goal."
    assert not record.get("title")


def test_intent_stays_unset_when_neither_field_is_present(tmp_path, as_session):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-none")
    _ensure_session_dir(repo, "sid-none")

    pb.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)

    record = read_baton("sid-none", cwd=str(repo))
    assert not record.get("intent")
