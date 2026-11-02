"""dispatch.land_run when several shared registry files carry the run's own appended hunks: every one
stages its own hunks, on first fire and on a repeat fire; a baseline blob lost from the object
store lands the files the run measured and verified, and refuses when they are not."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.commit import checkin_blob_sha
from coordinator_core.ops.dispatch_emit import land_run, run_token
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER, RUN_DIR_ROOT, StageManifest, measured_rev_record_path,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000e3"
RUN = "ask-20261102T100000-eeeeee"
SIZING = "state/sizings/20260101-a.yaml"
DLV = "dlv-alpha-eeeeee"
PLAN = "docs/plans/p.md"
REPORT = "reports/X1.md"
TARGETS = tuple(f"pkg/registry_{i}.py" for i in range(6))
SEED = "".join(f"line{i}\n" for i in range(1, 13))
VERDICT = {"items": [{"id": "R1", "status": "met", "wired": True}]}
PEER = SEED.replace("line1\n", "peer1\n")
BOTH = PEER + "own_registration = 1\n"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str, eol: str = "\n") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", eol).encode("utf-8"))


def _author(root: Path, rel: str, text: str, eol: str = "\n") -> None:
    _write(root, rel, text, eol)
    session_scope.touch(SELF, rel, cwd=str(root))
    touch_record.append_event(
        touch_record.sink_path(root / ".git" / "coordinator-sessions" / SELF),
        session_id=SELF, agent_id=None, verb="T", path=rel,
        content_hash=touch_record.compute_content_hash(root / rel), kind="w",
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "config", "core.autocrlf", "false")
    _write(root, ".gitignore", "scratch/\n.coordinator-local/\n")
    _write(root, "README.md", "seed\n")
    for t in TARGETS:
        _write(root, t, SEED)
    _write(
        root, SIZING,
        "schema: sizing-object\nintent: Test intent, verbatim.\nestimate:\n  tshirt: XS\n  provisional: true\n"
        "route: spec-dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\n"
        "premise:\n  provenance: read\n  evidence: fixture\npm_verbatims:\n- turn: 1\n  text: ship it\n",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    _write(
        root, PLAN,
        f"---\ntitle: p\ndeliverable_id: {DLV}\nsizing_object: {SIZING}\n---\n\n"
        "## Tasks\n\n```yaml plan-tasks\n- id: X1\n  title: t\n  disposition: open\n  body: |\n    b\n```\n",
    )
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(
        "coordinator_core.session.claims.list_claims_by_session_checked", lambda sid, cwd=None: ([], [])
    )
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    return root


def _declare(root: Path, baseline: dict) -> str:
    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="t", paths=TARGETS, report=REPORT),), deliverable_id=DLV, plan_path=PLAN
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", write_baseline=baseline,
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, f"{RUN}.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING)
    return run_token.mint(root, RUN, f"{RUN}.mjs", session_id=SELF)


def _land(root: Path, token: str) -> dict:
    slice_files = list(TARGETS)
    review = {
        **reviewed(),
        "prep": {"run_base_sha": None, "product_files": slice_files, "foreign_claims": [], "slice_files": slice_files},
        "delivery": {"verdict": "PASS"},
        "integration": {"unresolved": [], "confinement_violations": []},
    }
    return land_run._handler(
        {
            "script_path": f"{RUN}.mjs", "incomplete_chunks": ["X1"], "inline_review": review,
            "session_id": SELF, "run_id": RUN, "run_token": token, "ledger_verdict": VERDICT,
        },
        repo_root=root / ".git",
    )


def _stage(root: Path, *, baseline=None, eol: str = "\n") -> str:
    """Every target holds the peer's hunk at stage (``baseline`` None: captured, blob persisted), then
    the run appends its own registration."""
    for t in TARGETS:
        _write(root, t, PEER, eol)
    base = wb.capture(root, TARGETS) if baseline is None else baseline(root)
    _write(root, REPORT, "PARTIAL: X1\nNot done: nothing edited, files written by the run\n")
    for t in TARGETS:
        _author(root, t, BOTH, eol)
    return _declare(root, base)


def test_six_shared_files_stage_only_the_runs_own_hunks(repo):
    token = _stage(repo)

    out = _land(repo, token)

    assert out.get("committed") is True, out
    for t in TARGETS:
        assert _git(repo, "show", f"HEAD:{t}") == SEED + "own_registration = 1\n"
        assert (repo / t).read_text(encoding="utf-8") == BOTH


def test_a_repeat_fire_lands_the_same_set(repo):
    token = _stage(repo)
    first = _land(repo, token)
    assert first.get("committed") is True, first

    again = _land(repo, token)

    assert again.get("refused") != "own-hunk-stage-refused", again
    for t in TARGETS:
        assert _git(repo, "show", f"HEAD:{t}") == SEED + "own_registration = 1\n"


def _lost(root: Path) -> dict:
    return {t: blob_sha((root / t).read_bytes()) for t in TARGETS}


def _record_measured(root: Path, blobs: dict) -> None:
    record = measured_rev_record_path(root, RUN)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        json.dumps({"sha": "0" * 40, "base_sha": "0" * 40, "paths": list(TARGETS), "removed": [],
                    "tree": "0" * 40, "blobs": blobs}),
        encoding="utf-8",
    )


def test_a_baseline_blob_lost_from_the_store_lands_the_measured_files(repo):
    token = _stage(repo, baseline=_lost)
    _record_measured(repo, {t: checkin_blob_sha(repo, t) for t in TARGETS})

    out = _land(repo, token)

    assert out.get("committed") is True, out
    for t in TARGETS:
        assert _git(repo, "show", f"HEAD:{t}") == BOTH


def test_a_lost_baseline_blob_with_no_measured_record_still_refuses_each_fire(repo):
    head = _git(repo, "rev-parse", "HEAD").strip()
    token = _stage(repo, baseline=_lost)

    first, second = _land(repo, token), _land(repo, token)

    assert first.get("refused") == "own-hunk-stage-refused" and "unreadable" in first.get("error", ""), first
    assert second.get("refused") == "own-hunk-stage-refused", second
    assert _git(repo, "rev-parse", "HEAD").strip() == head


def test_a_lost_baseline_blob_whose_file_drifted_from_the_measured_blob_still_refuses(repo):
    token = _stage(repo, baseline=_lost)
    _record_measured(repo, {t: "1" * 40 for t in TARGETS})

    out = _land(repo, token)

    assert out.get("refused") == "own-hunk-stage-refused" and "unreadable" in out.get("error", ""), out
