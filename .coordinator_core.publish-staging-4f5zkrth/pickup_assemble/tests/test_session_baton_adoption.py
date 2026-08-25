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

Run: cd X:/claude-klabauter && python -m pytest
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


def _seed_handoff(repo: Path, name: str) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: active\n"
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
