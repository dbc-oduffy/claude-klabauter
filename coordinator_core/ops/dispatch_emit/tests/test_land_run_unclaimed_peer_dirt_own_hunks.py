"""dispatch.land_run on a path that holds an unclaimed peer's pre-run uncommitted hunks beside the run's
own: the commit carries HEAD plus the run's hunks only, and the peer hunks stay in the worktree."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run, run_token
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER, RUN_DIR_ROOT, StageManifest
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000e2"
RUN = "ask-20261031T100000-eeeeee"
SIZING = "state/sizings/20260101-a.yaml"
DLV = "dlv-alpha-eeeeee"
PLAN = "docs/plans/p.md"
REPORT = "reports/X1.md"
TARGET = "pkg/shared.py"
SEED = "".join(f"line{i}\n" for i in range(1, 13))
VERDICT = {"items": [{"id": "R1", "status": "met", "wired": True}]}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _author(root: Path, rel: str, text: str) -> None:
    _write(root, rel, text)
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
    _write(root, TARGET, SEED)
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
        chunks=(ChunkCommit(id="X1", title="t", paths=(TARGET,), report=REPORT),), deliverable_id=DLV, plan_path=PLAN
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
    slice_files = [TARGET]
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


def _stage(root: Path, peer_text: str, own_text: str, *, baselined: bool = True) -> str:
    _write(root, TARGET, peer_text)
    baseline = wb.capture(root, [TARGET]) if baselined else {}
    _write(root, REPORT, "PARTIAL: X1\nNot done: nothing edited, files written by the run\n")
    _author(root, TARGET, own_text)
    return _declare(root, baseline)


PEER = SEED.replace("line1\n", "peer1\n")
BOTH = PEER.replace("line10\n", "own10\n")


def test_unclaimed_peer_hunk_stays_out_of_the_commit_and_in_the_worktree(repo):
    token = _stage(repo, PEER, BOTH)

    out = _land(repo, token)

    assert out.get("committed") is True, out
    assert _git(repo, "show", f"HEAD:{TARGET}") == SEED.replace("line10\n", "own10\n")
    assert (repo / TARGET).read_text(encoding="utf-8") == BOTH
    assert TARGET in {ln[3:] for ln in _git(repo, "status", "--porcelain").splitlines()}


def test_unclaimed_peer_hunk_the_run_rewrote_lands_as_the_runs_own(repo):
    rewritten = PEER.replace("peer1\n", "own1\n")
    token = _stage(repo, PEER, rewritten)

    out = _land(repo, token)

    assert out.get("committed") is True, out
    assert _git(repo, "show", f"HEAD:{TARGET}") == rewritten


def test_a_path_with_no_recorded_baseline_lands_whole(repo):
    token = _stage(repo, PEER, BOTH, baselined=False)

    out = _land(repo, token)

    assert out.get("committed") is True, out
    assert _git(repo, "show", f"HEAD:{TARGET}") == BOTH


def test_a_wholly_own_path_lands_whole(repo):
    own = SEED.replace("line10\n", "own10\n")
    token = _stage(repo, SEED, own)

    out = _land(repo, token)

    assert out.get("committed") is True, out
    assert _git(repo, "show", f"HEAD:{TARGET}") == own
