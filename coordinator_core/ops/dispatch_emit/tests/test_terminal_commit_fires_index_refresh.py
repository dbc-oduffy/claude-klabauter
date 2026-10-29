"""A landed terminal commit fires one best-effort detached structural-index refresh; a refused commit fires none."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_untracked_dir_post_commit import (
    DIRNAME,
    _call,
    _declare_prefix_chunk,
    _seed_dir,
    _verdict,
)
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_commits_delivered_rows import (
    _REVIEWED,
    _SID,
    RUN,
    repo,  # noqa: F401  (fixture)
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _arm(monkeypatch) -> None:
    """Pytest re-sets PYTEST_CURRENT_TEST per phase, so the test body (not a fixture) must clear it."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv(terminal_commit._INDEX_REFRESH_OPT_OUT_ENV, raising=False)


@pytest.fixture
def spawns(monkeypatch):
    calls: list = []
    monkeypatch.setattr(terminal_commit, "_spawn_index_refresh", lambda root: calls.append(root) or True)
    return calls


def test_a_landed_commit_fires_exactly_one_refresh(repo, spawns, monkeypatch):  # noqa: F811
    _arm(monkeypatch)
    _declare_prefix_chunk(repo)
    _seed_dir(repo)

    out = _call(repo, _verdict(f"{DIRNAME} present untracked with 3 files"))

    assert out["committed"] is True, out
    assert len(spawns) == 1


def test_a_refused_commit_fires_none(repo, spawns, monkeypatch):  # noqa: F811
    _arm(monkeypatch)
    _declare_prefix_chunk(repo)
    _seed_dir(repo)
    params = {
        "script_path": "run.mjs", "incomplete_chunks": ["C9"], "inline_review": _REVIEWED,
        "session_id": _SID, "run_id": RUN,
        "ledger_verdict": _verdict(f"{DIRNAME} present untracked with 3 files"),
        "withhold_paths": [{"path": f"{DIRNAME}/f1.txt", "reason": "operator holds it"}],
    }

    out = terminal_commit._handler(params, repo_root=repo / ".git")

    assert out["committed"] is False
    assert spawns == []


def test_a_raising_spawn_leaves_the_verdict_unchanged(repo, monkeypatch):  # noqa: F811
    _arm(monkeypatch)
    monkeypatch.setattr(
        terminal_commit, "_spawn_index_refresh", lambda root: (_ for _ in ()).throw(OSError("boom"))
    )
    _declare_prefix_chunk(repo)
    _seed_dir(repo)

    out = _call(repo, _verdict(f"{DIRNAME} present untracked with 3 files"))

    assert out["committed"] is True, out


def test_opt_out_env_and_pytest_default_fire_none(repo, monkeypatch):  # noqa: F811
    calls: list = []
    monkeypatch.setattr(terminal_commit, "_spawn_index_refresh", lambda root: calls.append(root) or True)
    _declare_prefix_chunk(repo)
    _seed_dir(repo)

    _call(repo, _verdict(f"{DIRNAME} present untracked with 3 files"))

    assert calls == []


def test_child_argv_runs_the_admitted_refresh_through_the_manifest_entry(monkeypatch, tmp_path):
    seen: dict = {}

    def fake_popen(argv, **kw):
        seen["argv"] = argv
        seen["kw"] = kw

    import subprocess

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    assert terminal_commit._spawn_index_refresh(str(tmp_path)) is True
    argv = seen["argv"]
    assert argv[1] == "-c"
    assert "coordinator_core.workday_start.manifest" in argv[2]
    assert "structural_index_refresh_admitted" in argv[2]
    assert argv[3] == str(tmp_path)
    assert seen["kw"]["cwd"] == str(tmp_path)
