"""dispatch.land_run: a manifest ``review_code_paths`` entry still differing from HEAD after the commit
is named in ``ledger_withheld_product``, clears ``ledger_met``, demotes the criterion and credits no row."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run, run_token
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import shared_path_landing
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER, RUN_DIR_ROOT, StageManifest
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000d2"
RUN = "ask-20261029T100000-d2d2d2"
SIZING = "state/sizings/20260101-a.yaml"
DLV = "dlv-alpha-aaaaaa"
PLAN = "docs/plans/p.md"
REPORT = "reports/X1.md"
DOC = "docs/" "wiki/note.md"
CODE = "coordinator_core/mod.py"
UNTRACKED_TEST = "coordinator_core/tests/test_mod_new.py"
VERDICT = {"items": [{"id": "R1", "status": "met", "wired": True}]}
MET = {"status": "met", "observation": "obs", "sidecar": "j.md"}


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
    _write(root, CODE, "v = 0\n")
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


def _land(root: Path, *, leave_code_dirty: bool) -> dict:
    """The 6a66d3 shape: the request declares only the doc; the manifest's review_code_paths name the
    code and a new test, left dirty (``leave_code_dirty``) or committed alongside."""
    _author(root, DOC, "doc\n")
    _write(root, REPORT, "PARTIAL: X1\nNot done: nothing edited, files written by the run\n")
    declared = (DOC,) if leave_code_dirty else (DOC, CODE, UNTRACKED_TEST)
    if leave_code_dirty:
        _write(root, CODE, "v = 1\n")
        _write(root, UNTRACKED_TEST, "def test_x():\n    pass\n")
    else:
        _author(root, CODE, "v = 1\n")
        _author(root, UNTRACKED_TEST, "def test_x():\n    pass\n")
    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="t", paths=declared, report=REPORT),), deliverable_id=DLV, plan_path=PLAN
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(),
        review_code_paths=(CODE, UNTRACKED_TEST),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, f"{RUN}.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING)
    token = run_token.mint(root, RUN, f"{RUN}.mjs", session_id=SELF)
    return land_run._handler(
        {
            "script_path": f"{RUN}.mjs", "incomplete_chunks": [],
            "inline_review": {**reviewed(), "integration_stem": "s", "criterion": dict(MET)},
            "session_id": SELF, "run_id": RUN, "run_token": token, "ledger_verdict": VERDICT,
        },
        repo_root=root / ".git",
    )


def test_review_code_paths_left_dirty_are_named_demote_the_criterion_and_credit_no_row(repo):
    out = _land(repo, leave_code_dirty=True)

    assert out["committed"] is True and out.get("refused") is None, out
    assert out[shared_path_landing.LEDGER_WITHHELD_PRODUCT] == sorted([CODE, UNTRACKED_TEST])
    assert out["criterion_status"] == "not_met"
    assert out["credited_chunks"] == []
    assert CODE in out["exit_criterion_not_stamped"]
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").split().count(CODE) == 0


def test_review_code_paths_committed_leave_no_gap_and_keep_the_criterion_met(repo):
    out = _land(repo, leave_code_dirty=False)

    assert out["committed"] is True, out
    assert shared_path_landing.LEDGER_WITHHELD_PRODUCT not in out
    assert out["criterion_status"] == "met"
    assert CODE in _git(repo, "show", "--name-only", "--format=", "HEAD").split()
