"""dispatch.terminal_commit prints remedies that run as printed, honours a direct ``withhold_paths``
param with no record dir, and anchors a review-only run's expectation at its fresh review."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SID = "c4e53fa2-2bda-51b1-833a-cf629f2b0cb6"
_REVIEWED = {
    "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
    "wave_reviews": [{"agent_type": "coordinator:reviewer", "verdict": "PASS", "sidecar_path": "../wave-review.md"}],
}
_OP = "dispatch.terminal_commit"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "wave-review.md").write_text("# review\n", encoding="utf-8")
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8", newline="")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SID)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return root


def _run(repo: Path, request: CommitRequest, **params) -> dict:
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    base = {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED}
    return terminal_commit._handler({**base, **params}, repo_root=repo / ".git")


def _committed(repo: Path) -> list:
    return _git(["log", "--name-only", "--format="], repo).split()


def _printed_params(error: str) -> dict:
    """The JSON object a printed ``coordinator-invoke`` line passes as the call's params."""
    line = next(ln for ln in error.splitlines() + [error] if "coordinator-invoke" in ln)
    tail = line[line.index("coordinator-invoke"):]
    quoted = re.findall(r"'(\{.*?\})'(?=\s|;|\.|$)", tail)
    assert quoted, tail
    return json.loads(quoted[-1])


def test_the_overlap_refusal_prints_an_invoke_line_whose_json_lands(repo):
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    request = CommitRequest(chunks=(
        ChunkCommit(id="C1", title="t1", paths=("a.py",)),
        ChunkCommit(id="C2", title="t2", paths=("b.py", "a.py")),
    ))
    held = [{"chunk": "C2", "reason": "halted"}]
    refused = _run(repo, request, held=held)
    assert refused["refused"] == "held-landed-overlap"
    assert "params.release_held" not in refused["error"]
    assert f"coordinator-invoke {_OP} --from-task-output TASK_OUTPUT_PATH" in refused["error"]
    printed = _printed_params(refused["error"])
    assert printed == {"release_held": ["C2"]}
    out = _run(repo, request, held=held, **printed)
    assert out["committed"] is True, out
    assert "a.py" in _committed(repo)


def test_a_direct_withhold_paths_param_leaves_the_path_dirty_with_no_record_dir(repo):
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    request = CommitRequest(chunks=(
        ChunkCommit(id="C1", title="t1", paths=("a.py",)),
        ChunkCommit(id="C2", title="t2", paths=("b.py",)),
    ))
    assert not (repo / "state" / "warp").exists()
    out = _run(repo, request, withhold_paths=[{"path": "b.py", "reason": "peer owns it"}])
    assert out["committed"] is True, out
    files = _committed(repo)
    assert "a.py" in files and "b.py" not in files
    assert "b.py" in _git(["status", "--porcelain"], repo)
    assert out["run_artifacts"]["withheld"]["b.py"] == "withhold_paths: peer owns it"


def test_a_malformed_withhold_paths_refuses_before_any_write(repo):
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),))
    head = _git(["rev-parse", "HEAD"], repo)
    out = _run(repo, request, withhold_paths=[{"path": "a.py"}])
    assert out.get("committed") is not True
    assert "withhold_paths" in out["error"]
    assert _git(["rev-parse", "HEAD"], repo) == head


def _record(repo: Path, rel: str, digest: str, at: float) -> None:
    sink = touch_record.sink_path(repo / ".git" / "coordinator-sessions" / SID)
    touch_record.append_event(
        sink, session_id=SID, agent_id=None, verb="T", path=rel, content_hash=digest, kind="w",
        timestamp=at,
    )


def test_a_ledgerless_blob_drift_names_no_revise_target_and_its_withhold_line_lands(repo):
    (repo / "a.py").write_text("B\n", encoding="utf-8", newline="")
    _record(repo, "a.py", _sha("A\n"), 1000.0)
    request = CommitRequest(chunks=(ChunkCommit(id="C4", title="t4", paths=("a.py",)),))
    refused = _run(repo, request, session_id=SID)
    assert refused["refused"] == "blob-drift", refused
    assert "workflow.revise" not in refused["error"]
    assert "--review-only" in refused["error"]
    printed = _printed_params(refused["error"])
    assert printed["withhold_paths"][0]["path"] == "a.py"
    out = _run(repo, request, session_id=SID, **printed)
    assert out.get("refused") != "blob-drift", out
    assert "a.py" not in _committed(repo)
    assert "a.py" in _git(["status", "--porcelain"], repo)


def test_a_ledger_bearing_blob_drift_keeps_the_revise_remedy(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import landing_remedy

    monkeypatch.setattr(landing_remedy, "revise_resolves", lambda root, run_id: True)
    (repo / "a.py").write_text("B\n", encoding="utf-8", newline="")
    _record(repo, "a.py", _sha("A\n"), 1000.0)
    request = CommitRequest(chunks=(ChunkCommit(id="C4", title="t4", paths=("a.py",)),))
    refused = _run(repo, request, session_id=SID, run_id="run-1")
    assert refused["refused"] == "blob-drift", refused
    assert "workflow.revise run_id=run-1" in refused["error"]


_PLAN = (
    "---\ntitle: p\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
    "- id: C4\n  title: t4\n  change_kind: code-edit\n  surface: a.py\n"
    "  disposition: open\n  deferred: false\n```\n"
)


def _review_only_fixture(repo: Path, edit_mtime: float, sidecar_mtime: float):
    """Shaped like cockpit wf_89e5ddd2-8e5: a test-runner prep sidecar, fixes 0, slices 0, the
    session's last write recorded before an edit that predates (or postdates) the sidecar."""
    (repo / "docs").mkdir()
    (repo / "docs" / "plan.md").write_text(_PLAN, encoding="utf-8", newline="")
    _git(["add", "docs/plan.md"], repo)
    _git(["commit", "-q", "-m", "plan"], repo)
    (repo / "a.py").write_text("A\n", encoding="utf-8", newline="")
    _git(["add", "a.py"], repo)
    _git(["commit", "-q", "-m", "executor a.py"], repo)
    base = _git(["rev-parse", "HEAD~1"], repo)
    _record(repo, "a.py", _sha("A\n"), 1000.0)
    (repo / "a.py").write_text("B\n", encoding="utf-8", newline="")
    os.utime(repo / "a.py", (edit_mtime, edit_mtime))
    sidecar = repo / ".coordinator-local" / "workflow-sidecars" / "prep.md"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("---\nagent_type: test-runner\ntests: 186/0\n---\n", encoding="utf-8")
    os.utime(sidecar, (sidecar_mtime, sidecar_mtime))
    request = CommitRequest(
        chunks=(ChunkCommit(id="C4", title="t4", paths=("a.py",)),),
        plan_path="docs/plan.md", review_only=True, review_open_rows=("C4",), review_run_base=base,
    )
    review = {
        **_REVIEWED, "fixes": 0, "slices": 0,
        "prep_sidecar": ".coordinator-local/workflow-sidecars/prep.md",
    }
    return request, review


def test_a_review_only_run_lands_a_path_edited_before_its_fresh_review(repo):
    request, review = _review_only_fixture(repo, edit_mtime=1500.0, sidecar_mtime=2000.0)
    out = _run(repo, request, session_id=SID, inline_review=review)
    assert out.get("refused") != "blob-drift", out
    assert out["committed"] is True, out
    assert "a.py" in _committed(repo)


def test_a_review_only_run_still_refuses_a_path_edited_after_its_review(repo):
    request, review = _review_only_fixture(repo, edit_mtime=3000.0, sidecar_mtime=2000.0)
    out = _run(repo, request, session_id=SID, inline_review=review)
    assert out["refused"] == "blob-drift", out
