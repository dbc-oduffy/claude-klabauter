"""The post-land re-run of the tripwires a test verdict recorded NOT RUN: one detached `test_leg_run` at the landed sha."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit, tripwire_rerun
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER, RUN_DIR_ROOT, StageManifest
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000d1"
RUN = "run1"
TRIPWIRES = ["t/test_a.py", "t/test_b.py"]


@pytest.fixture
def spawns(monkeypatch):
    calls: list = []

    def fake(root, script, args=None, **_kw):
        calls.append((str(root), script, list(args or [])))
        return True

    monkeypatch.setattr(tripwire_rerun, "spawn_detached", fake)
    return calls


LEDGER_RUN = "ask-20261001T000000-bbbbbb"
SIZING_REL = "state/sizings/2026-10-01-rerun.yaml"
OWNER = "0w4e0000-0000-4000-8000-0000000000aa"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    session_core.init(SELF, cwd=str(root))
    run_dir = root / RUN_DIR_ROOT / RUN
    run_dir.mkdir(parents=True)
    request = CommitRequest(chunks=(ChunkCommit(id="A", title="A", paths=("a.py",)),))
    (run_dir / "commit-request.txt").write_text(render_marker(request) + "\n", encoding="utf-8")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
    )
    (run_dir / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    (root / "run.mjs").write_text(f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n", encoding="utf-8")
    (root / "a.py").write_text("a\n", encoding="utf-8")
    return root


def _terminal_commit(repo: Path, **extra) -> dict:
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": reviewed(), "session_id": SELF, **extra},
        repo_root=repo / ".git",
    )


@pytest.mark.spawns_process
def test_terminal_commit_relaunches_the_not_run_tripwires_at_the_landed_sha(repo, spawns):
    out = _terminal_commit(
        repo, targeted_tests={"status": "pass", "tests_run": 9, "importer_overrun": True, "tripwires_not_run": TRIPWIRES}
    )

    assert out.get("committed") is True, out
    assert len(spawns) == 1
    args = spawns[0][2]
    landed = args[args.index("--head-sha") + 1]
    assert landed == out["sha"] and _git(["cat-file", "-t", landed], repo) == "commit"
    assert args[args.index("--out") + 1] == str(repo / RUN_DIR_ROOT / RUN / "tripwire-rerun.json")
    assert args[args.index("--") + 1:] == TRIPWIRES


@pytest.mark.spawns_process
def test_terminal_commit_launches_nothing_when_the_verdict_names_no_tripwires(repo, spawns):
    out = _terminal_commit(repo, targeted_tests={"status": "pass", "tests_run": 9})

    assert out.get("committed") is True, out
    assert spawns == []
