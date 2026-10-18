"""dispatch.terminal_commit counts commit_v2's ``stopped_run_residue.carried`` paths as landed (in
neither undeclared field) and relays the key verbatim."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000a2"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
STRAY = "src/stray.py"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8", newline="")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("a\n", encoding="utf-8", newline="")
    (root / STRAY).write_text("stray\n", encoding="utf-8", newline="")
    return root


def _spy(monkeypatch, carried):
    """The real commit_v2 runs; the reply gains ``stopped_run_residue`` when ``carried`` is set."""
    real = terminal_commit.reentrant_dispatch

    def spy(op, params, repo_root=None):
        reply = real(op, params, repo_root=repo_root)
        if op == "ceremony.commit_v2" and carried is not None:
            return {**reply, "stopped_run_residue": {"carried": carried, "withheld": {}}}
        return reply

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", spy)


def _run(repo: Path) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("src/a.py",)),))
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8", newline="")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED,
         "session_id": SELF},
        repo_root=repo / ".git",
    )


def test_control_without_key_reports_the_stray_dirty(repo, monkeypatch):
    _spy(monkeypatch, None)
    out = _run(repo)
    assert out["committed"] is True, out
    assert STRAY in (out.get("undeclared_dirty") or []) or out.get("undeclared_dirty_peer_held")
    assert "stopped_run_residue" not in out


@pytest.mark.parametrize("spelling", [STRAY, "src\\stray.py"])
def test_carried_path_is_landed_and_key_relayed(repo, monkeypatch, spelling):
    _spy(monkeypatch, {"ask-20260101T000000-aaaaaa": [spelling]})
    out = _run(repo)
    assert out["committed"] is True, out
    assert STRAY not in (out.get("undeclared_dirty") or [])
    assert "undeclared_dirty_peer_held" not in out
    assert out["stopped_run_residue"]["carried"] == {"ask-20260101T000000-aaaaaa": [spelling]}


def test_malformed_key_carries_nothing():
    for bad in ({}, {"stopped_run_residue": None}, {"stopped_run_residue": {"carried": []}},
                {"stopped_run_residue": {"carried": {"r": "x"}}}):
        assert terminal_commit._stopped_run_carried(bad) == set()


def test_stopped_run_residue_never_carries_a_live_siblings_plan(tmp_path, monkeypatch):
    import json

    from coordinator_core.ops.dispatch_emit import stopped_runs
    from coordinator_core.ops.dispatch_emit.tests import test_stopped_runs as ts
    from coordinator_core.session import scope

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setattr(ts.warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(ts.warp_live, "legacy_top_level_path", lambda name: managed / name)
    monkeypatch.setattr(scope, "contested_by_live_peers", lambda paths, sid, cwd=None: {})

    stopped = ts._run_id(60, "aaaaaa")
    live = ts._run_id(30, "bbbbbb")
    ts._submit(root, tmp_path, stopped, body=ts._stopped_body(stopped))
    ts._artifacts(root, stopped, sizing="state/sizings/a.yaml")
    ts._write(root, f"state/warp/{live}/run.json", json.dumps({"sizing_rel": "state/sizings/b.yaml"}))
    ts._write(root, "state/sizings/b.yaml", "plan: docs/plans/sibling.md\n")
    ts._write(root, "state/sizings/a.yaml", "baton: state/handoffs/a.md\nplan: docs/plans/sibling.md\n")
    ts._write(root, "docs/plans/sibling.md", "---\nsizing_object: state/sizings/b.yaml\n---\n")

    got = stopped_runs.residue(root, ts.SESSION, [])
    assert "docs/plans/sibling.md" not in got.carried[stopped]
    assert "state/sizings/a.yaml" in got.carried[stopped]


def test_stopped_run_residue_withholds_a_plan_no_run_owns(tmp_path, monkeypatch):
    import json

    from coordinator_core.ops.dispatch_emit import stopped_runs
    from coordinator_core.ops.dispatch_emit.tests import test_stopped_runs as ts
    from coordinator_core.session import scope

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setattr(ts.warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(ts.warp_live, "legacy_top_level_path", lambda name: managed / name)
    monkeypatch.setattr(scope, "contested_by_live_peers", lambda paths, sid, cwd=None: {})

    stopped = ts._run_id(60, "aaaaaa")
    live = ts._run_id(30, "bbbbbb")
    ts._submit(root, tmp_path, stopped, body=ts._stopped_body(stopped))
    ts._artifacts(root, stopped, sizing="state/sizings/a.yaml")
    ts._write(root, f"state/warp/{live}/run.json", json.dumps({"sizing_rel": "state/sizings/b.yaml"}))
    ts._write(root, "state/sizings/b.yaml", "plan: docs/plans/sibling.md\n")
    ts._write(root, "state/sizings/a.yaml", "baton: state/handoffs/a.md\nplan: docs/plans/sibling.md\n")
    ts._write(root, "docs/plans/sibling.md", "---\ntitle: no owner\n---\n")

    got = stopped_runs.residue(root, ts.SESSION, [])
    assert "docs/plans/sibling.md" not in got.carried.get(stopped, ())
    assert "state/sizings/a.yaml" in got.carried[stopped]
