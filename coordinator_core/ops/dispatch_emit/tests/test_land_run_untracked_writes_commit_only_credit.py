"""dispatch.land_run on a commit-only run whose one row came back PARTIAL, whose writes-list files are all
untracked (a new run dir) and whose ledger is met: the row is credited and lands with no EM credit."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run, run_token
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER, RUN_DIR_ROOT, StageManifest
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000d4"
RUN = "ask-20261024T100000-dddddd"
SIZING = "state/sizings/20260101-a.yaml"
DLV = "dlv-alpha-aaaaaa"
PLAN = "docs/plans/p.md"
REPORT = "reports/X1.md"
WRITES = ("exp/r4/a.txt", "docs/research/r4/b.txt", "docs/research/r4/c.txt")
PRODUCT = [WRITES[0]]  # review.prep slice_files never list a docs/research path
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


def _declare(root: Path) -> str:
    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="t", paths=WRITES, report=REPORT),), deliverable_id=DLV, plan_path=PLAN
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, f"{RUN}.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING)
    return run_token.mint(root, RUN, f"{RUN}.mjs", session_id=SELF)


def _review(slice_files: list) -> dict:
    return {
        **reviewed(),
        "prep": {"run_base_sha": None, "product_files": slice_files, "foreign_claims": [], "slice_files": slice_files},
        "delivery": {"verdict": "PASS"},
        "integration": {"unresolved": [], "confinement_violations": []},
    }


def _land(root: Path, token: str, slice_files: list) -> dict:
    return land_run._handler(
        {
            "script_path": f"{RUN}.mjs", "incomplete_chunks": ["X1"], "inline_review": _review(slice_files),
            "session_id": SELF, "run_id": RUN, "run_token": token, "ledger_verdict": VERDICT,
        },
        repo_root=root / ".git",
    )


def _stage(root: Path) -> str:
    for rel in WRITES:
        _author(root, rel, f"{rel}\n")
    _write(root, REPORT, "PARTIAL: X1\nNot done: nothing edited, files written by the run\n")
    return _declare(root)


def test_a_partial_row_with_untracked_writes_and_a_met_ledger_is_credited_and_lands(repo):
    token = _stage(repo)

    out = _land(repo, token, PRODUCT)

    assert out.get("committed") is True, out
    assert set(WRITES) <= set(_git(repo, "show", "--name-only", "--format=", "HEAD").split())
    assert "X1" in _git(repo, "log", "-1", "--format=%B", "HEAD")


def test_the_same_row_stays_stranded_when_the_review_did_not_cover_its_files(repo):
    token = _stage(repo)

    out = _land(repo, token, [])

    assert out.get("refused") == "no-credited-chunk-landed", out
