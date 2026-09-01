"""
coordinator_core.pickup_assemble.tests.test_session_baton_adoption

Purpose: proves C3's unwired half (docs/plans/2026-08-18-a-session-always-
has-a-baton.md § C3, "a pickup adopts the session baton as a fan-in edge")
is actually wired: a pickup `brief()` that takes the `claim_at_brief` lock
records the picked-up artifact's path into THIS session's baton record's
`adopted_artifacts[]` (`session_baton.store.merge_baton`).

Negative-spec covered:
  - re-briefing the same artifact in the same session never duplicates the
    entry (`merge_baton`'s own dedup-extend contract, exercised end to end)
  - a baton store that cannot be written (session hub unresolvable) never
    breaks the pickup itself — `_adopt_into_baton`'s fail-open posture,
    mirroring `quick_wrap_assemble._print_commits_into_baton`.

Run from the repo root: python -m pytest
coordinator_core/pickup_assemble/tests/test_session_baton_adoption.py -q
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

import coordinator_core.pickup_assemble as pa
from coordinator_core.session import liveness as liveness_mod
from coordinator_core.session_baton.store import read_baton

# Declared, not excused: spawns a real git process, same convention as the
# sibling files in this package (test_brief_claim_lease.py,
# test_pickup_claim_stage_stamp_evidence.py).
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


@pytest.fixture(autouse=True)
def _reset_registry_snapshot_cache():
    liveness_mod._registry_snapshot_cache = None
    yield
    liveness_mod._registry_snapshot_cache = None


def _isolated_git_env(anchor: Path) -> dict[str, str]:
    empty_config = anchor / "empty.gitconfig"
    if not empty_config.exists():
        empty_config.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = str(empty_config)
    env["GIT_CONFIG_SYSTEM"] = str(empty_config)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
        env=_isolated_git_env(repo.parent),
        **no_console_creationflags(),
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "work/test/2026-01-01")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


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
    """Pre-create the per-session directory ``cs_init`` mints on every real
    session start — this store (C6, docs/plans/2026-08-19-batons-unify-into-
    one-successor.md § C6) no longer mkdir's it itself, so a fixture binding
    a session id via env var alone (bypassing real session init) must bring
    it into being before `_adopt_into_baton`'s `merge_baton` call can land."""
    sdir = repo / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    return sdir


def test_pickup_adopts_artifact_into_session_baton(tmp_path, as_session):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")
    _ensure_session_dir(repo, "sid-a")

    pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)

    record = read_baton("sid-a", cwd=str(repo))
    assert record["adopted_artifacts"] == ["state/handoffs/h1.md"]


def test_rebrief_same_artifact_does_not_duplicate(tmp_path, as_session):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")
    _ensure_session_dir(repo, "sid-a")

    pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)
    pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)

    record = read_baton("sid-a", cwd=str(repo))
    assert record["adopted_artifacts"] == ["state/handoffs/h1.md"]


def test_brief_survives_broken_baton_store(tmp_path, as_session, monkeypatch):
    """A pickup that cannot touch its baton (session hub unresolvable) still
    succeeds — `_adopt_into_baton`'s fail-open posture must never surface
    into `brief()`'s own return value or raise."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")

    from coordinator_core.pickup_assemble import merge_baton as _mb  # noqa: F401
    import coordinator_core.pickup_assemble as pa_mod

    def _boom(*args, **kwargs):
        raise OSError("simulated baton-store failure")

    monkeypatch.setattr(pa_mod, "merge_baton", _boom)

    result = pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)

    assert result is not None
    assert result.decision_object["artifact"]["path"] == "state/handoffs/h1.md"

def test_intent_prefers_session_goal_when_the_handoff_carries_one(tmp_path, as_session):
    """`session_goal` is the field that means goal, so it wins outright."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md", fm_extra="session_goal: Ship the thing.\n")
    as_session("sid-goal")
    _ensure_session_dir(repo, "sid-goal")

    pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)

    record = read_baton("sid-goal", cwd=str(repo))
    assert record["intent"] == "Ship the thing."
    assert "(from summary)" not in record["intent"]


def test_intent_falls_back_to_summary_and_says_so(tmp_path, as_session):
    """With no `session_goal`, `intent` borrows `summary` -- labelled.

    `session_goal` is optional and, measured 2026-08-31, carried by 0 of 295
    live handoffs with no producer anywhere in the engine, so the derivation
    that read it alone could never fire: `intent` was null on 18 of 18 baton
    records while `title` reached 11 of 11 adopters. `summary` is required by
    cross-field rule and present on 295 of 295.

    The prefix is the load-bearing part, not decoration. A summary is
    retrospective and a goal is forward-looking; borrowing one for the other
    silently would make the record assert something it does not know. This
    pins that the borrowing is always disclosed on the record itself.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md", fm_extra="summary: What the session did.\n")
    as_session("sid-sum")
    _ensure_session_dir(repo, "sid-sum")

    pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)

    record = read_baton("sid-sum", cwd=str(repo))
    assert record["intent"] == "(from summary) What the session did."


def test_second_different_adoption_never_clobbers_first_intent(tmp_path, as_session):
    """Review: reviewer 2026-09-01-codereview-sliceB #1/#2 -- a title-less
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
    """No goal and no summary means no intent -- never an invented one."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-none")
    _ensure_session_dir(repo, "sid-none")

    pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)

    record = read_baton("sid-none", cwd=str(repo))
    assert not record.get("intent")
