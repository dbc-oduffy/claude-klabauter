"""terminal_commit._write_usage reads the run's own wf_ journal once per consumer, in proportion to the run."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_usage import SID, _stage
from coordinator_core.usage import run_locate

import pytest
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _journal_fixture(session: Path, count: int) -> None:
    workflows = session / "subagents" / "workflows"
    for i in range(count):
        wf = workflows / f"wf_{i:05d}"
        wf.mkdir(parents=True)
        events = [{"type": "started", "agentId": f"a{i}", "label": "work"},
                  {"type": "result", "agentId": f"a{i}"}]
        (wf / "journal.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def test_terminal_commit_never_locates_the_run_dir_and_lands_with_a_failing_locate(tmp_path, monkeypatch):
    import subprocess

    from coordinator_core.ops.dispatch_emit import terminal_commit
    from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER, RUN_DIR_ROOT, StageManifest
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
    from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
    from coordinator_core.usage import agent_usage
    from coordinator_core.win_portability import no_console_creationflags

    _stage(tmp_path, monkeypatch)
    session = run_locate.session_dir_for(SID)
    _journal_fixture(session, 350)

    def boom(*_a, **_k):
        raise AssertionError("the commit path touched the usage locate or scan")

    monkeypatch.setattr(run_locate, "locate_run_dir", boom)
    monkeypatch.setattr(agent_usage, "scan_run", boom)

    repo = tmp_path / "ask-repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    for args in (["add", "."], ["commit", "-q", "-m", "seed"]):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    run_dir = repo / RUN_DIR_ROOT / "run1"
    run_dir.mkdir(parents=True)
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),))
    (run_dir / "commit-request.txt").write_text(render_marker(request) + "\n", encoding="utf-8")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/run1", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/run1/commit-request.txt",
    )
    (run_dir / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "run.mjs").write_text(f"{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/run1/manifest.json\n", encoding="utf-8")

    out = terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": reviewed(), "session_id": SID},
        repo_root=repo / ".git",
    )

    assert out["committed"] is True, out
    assert out["usage_total"] is None and out["usage_reason"]
