"""dispatch.land_run lands from the emission receipt's landing markers when the script is gone, refuses
`script-missing` without them, and stamps the receipt landed after a product commit."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.emission_receipt import emission_receipt_path
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000b2"
RUN = "plan-20261021T100000-bbbbbb"
SCRIPT = "run-part.workflow.mjs"
PLAN = "docs/plans/p.md"
REVIEWED = reviewed()


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    _write(root, "README.md", "seed\n")
    _write(root, PLAN, "---\ntitle: p\n---\nbody\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    return root


def _author(root: Path, rel: str, text: str) -> None:
    _write(root, rel, text)
    session_scope.touch(SELF, rel, cwd=str(root))
    sink = touch_record.sink_path(root / ".git" / "coordinator-sessions" / SELF)
    touch_record.append_event(
        sink, session_id=SELF, agent_id=None, verb="T", path=rel,
        content_hash=touch_record.compute_content_hash(root / rel), kind="w",
    )


def _marker() -> str:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("src/a.py",)),), plan_path=PLAN)
    return render_marker(request)


def _receipt(root: Path, **keys) -> Path:
    path = emission_receipt_path(root / SCRIPT)
    path.write_text(
        json.dumps({"sha256": "0" * 64, "emitted_at": "2026-10-21T20:00:00", **keys}), encoding="utf-8"
    )
    return path


def _land(root: Path) -> dict:
    params = {
        "script_path": SCRIPT, "incomplete_chunks": [], "inline_review": dict(REVIEWED),
        "session_id": SELF, "run_id": RUN,
    }
    return land_run._handler(params, repo_root=root / ".git")


def test_a_missing_script_lands_from_the_receipt_marker_and_stamps_it_landed(repo):
    _author(repo, "src/a.py", "a = 1\n")
    receipt = _receipt(repo, landing_markers=[_marker()])
    assert not (repo / SCRIPT).exists()

    out = _land(repo)

    assert out["committed"] is True, out
    assert out["script_source"] == "receipt"
    assert "src/a.py" in _git(["show", "--name-only", "--format=", "HEAD"], repo).split()
    stamped = json.loads(receipt.read_text(encoding="utf-8"))["landed"]
    assert stamped["sha"] == out["sha"]


def test_a_present_script_lands_without_script_source_and_stamps_its_receipt(repo):
    _author(repo, "src/a.py", "a = 1\n")
    _write(repo, SCRIPT, "// emitted\n" + _marker() + "\n")
    receipt = _receipt(repo)

    out = _land(repo)

    assert out["committed"] is True, out
    assert "script_source" not in out
    assert json.loads(receipt.read_text(encoding="utf-8"))["landed"]["sha"] == out["sha"]


@pytest.mark.parametrize("keys", [None, {}], ids=["no-receipt", "receipt-without-markers"])
def test_a_missing_script_without_markers_refuses_script_missing_naming_the_rebuild_command(repo, keys):
    _author(repo, "src/a.py", "a = 1\n")
    receipt = _receipt(repo, **keys) if keys is not None else None
    head = _git(["rev-parse", "HEAD"], repo)

    out = _land(repo)

    assert out["committed"] is False
    assert out["refused"] == "script-missing"
    assert "python -m coordinator_core.ops.dispatch_emit.stranded_part_rebuild" in out["error"]
    assert f"--script {SCRIPT}" in out["error"]
    assert _git(["rev-parse", "HEAD"], repo) == head
    if receipt is not None:
        assert "landed" not in json.loads(receipt.read_text(encoding="utf-8"))
