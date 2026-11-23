"""dispatch.land_run at default arguments never replies committed true for a commit carrying only run
bookkeeping while the run's product paths stay withheld; a commit carrying product still does."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.session_hub_path import session_dir_for
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

SELF = "5e1f0000-0000-4000-8000-0000000000d5"
RUN = "ask-20261118T114303-a611e3"
SIZING = "state/sizings/20260101-a.yaml"
PLAN = "docs/plans/p.md"
DLV = "dlv-alpha-aaaaaa"
NODE = "tests/test_a.py::test_x"
F = "pkg/shared.py"
T = "tests/test_new.py"
VERDICT = {"items": [{"id": "R1", "status": "met", "met_by": "a.py", "wired": True}]}


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
        touch_record.sink_path(session_dir_for(root / ".git", SELF)),
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
    _write(root, F, "base = 0\n")
    _write(
        root, SIZING,
        "schema: sizing-object\nintent: Test intent, verbatim.\nestimate:\n  tshirt: S\n  provisional: true\n"
        "route: spec-dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\n"
        "premise:\n  provenance: read\n  evidence: fixture\npm_verbatims:\n- turn: 1\n  text: ship shared.py\n",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    _write(
        root, PLAN,
        f"---\ntitle: p\ndeliverable_id: {DLV}\nsizing_object: {SIZING}\nprime_exit_criterion:\n"
        f"  statement: done\n  falsifier:\n    how: pytest {NODE} -q\n    expected_when_true: passes\n---\n\n"
        "## Tasks\n\n```yaml plan-tasks\n- id: C1\n  title: t\n  disposition: open\n  body: |\n    b\n```\n",
    )
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(
        "coordinator_core.session.claims.list_claims_by_session_checked", lambda sid, cwd=None: ([], [])
    )
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: True)
    return root


def _declare_run(root: Path) -> str:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=(F, T)),), deliverable_id=DLV, plan_path=PLAN)
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, f"{RUN}.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING)
    token = run_token.mint(root, RUN, f"{RUN}.mjs", session_id=SELF)
    _author(root, F, "base = 0\nnew = 1\n")
    _author(root, T, "def test_new():\n    assert True\n")
    return token


def _land(root: Path, token: str, **extra) -> dict:
    params = {
        "script_path": f"{RUN}.mjs", "incomplete_chunks": [], "inline_review": dict(reviewed()),
        "session_id": SELF, "run_id": RUN, "run_token": token, "ledger_verdict": VERDICT, **extra,
    }
    return land_run._handler(params, repo_root=root / ".git")


def _dirty(root: Path) -> set:
    return {ln[3:] for ln in _git(root, "status", "--porcelain", "-uall").splitlines()}


def test_a_commit_of_only_bookkeeping_with_product_withheld_is_not_landed(repo):
    token = _declare_run(repo)
    head = _git(repo, "rev-parse", "HEAD").strip()

    out = _land(repo, token, withhold_paths=[{"path": F, "reason": "em keeps it"}, {"path": T, "reason": "em keeps it"}])

    assert out.get("committed") is not True, out
    assert out.get("sha") is None, out
    assert out.get("not_landed") is True, out
    assert F in out["error"] and T in out["error"], out
    assert out[shared_path_landing.LEDGER_WITHHELD_PRODUCT] == sorted([F, T]), out
    assert {F, T} <= _dirty(repo)
    assert out["bookkeeping_sha"], out
    assert _git(repo, "rev-parse", "HEAD").strip() == out["bookkeeping_sha"] != head
    carried = set(_git(repo, "show", "--name-only", "--format=", out["bookkeeping_sha"]).split())
    assert carried and not carried & {F, T}, carried


def test_a_commit_carrying_a_product_path_still_replies_committed(repo):
    token = _declare_run(repo)

    out = _land(repo, token, withhold_paths=[{"path": F, "reason": "em keeps it"}])

    assert out["committed"] is True, out
    assert out["sha"], out
    assert T in set(_git(repo, "show", "--name-only", "--format=", out["sha"]).split())
    assert not out.get("not_landed")
