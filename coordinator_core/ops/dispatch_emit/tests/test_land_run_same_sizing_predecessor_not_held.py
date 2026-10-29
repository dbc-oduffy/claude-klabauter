"""A refire's landing never counts a live same-sizing predecessor run as holding its paths; a live run
of a different sizing (or one whose sizing is unknown on either side) still does."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops import warp_run_deliverable
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

SELF = "5e1f0000-0000-4000-8000-0000000000d4"
PRED = "ask-20261029T100000-aaaaaa"
REFIRE = "ask-20261029T100001-bbbbbb"
SIZING = "state/sizings/20260101-a.yaml"
OTHER_SIZING = "state/sizings/20260101-b.yaml"
F = "pkg/shared.py"
REVIEWED = reviewed()
VERDICT = {"items": [{"id": "R1", "status": "met", "wired": True}]}
DLV = "dlv-alpha-aaaaaa"
PLAN = "docs/plans/p.md"
NODE = "tests/test_a.py::test_x"
BASE = "base = 0\n"
EDITED = "base = 0\nrefire = 1\n"


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
    _write(root, F, BASE)
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
    return root


def _declare_run(root: Path, run_id: str, paths: tuple, sizing: str = SIZING, resumed_from=None) -> str:
    """A live ledger-bearing ask run declaring ``paths`` whose run record names ``sizing`` ("" writes none)
    and, when given, the ``resumed_from`` predecessor it refires."""
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=paths),), deliverable_id=DLV, plan_path=PLAN)
    _write(root, f"{RUN_DIR_ROOT}/{run_id}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{run_id}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{run_id}/commit-request.txt",
    )
    _write(root, f"{RUN_DIR_ROOT}/{run_id}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, f"{run_id}.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{run_id}/manifest.json\n")
    rl.start(root, run_id, all_of_pm_ask=False, resumed_from=resumed_from)
    rl.bind(root, run_id, SIZING)
    if sizing != SIZING:
        _write(root, f"{RUN_DIR_ROOT}/{run_id}/run.json", json.dumps(
            {"sizing_rel": sizing, **({"resumed_from": resumed_from} if resumed_from else {})} if sizing else {}
        ))
    return run_token.mint(root, run_id, f"{run_id}.mjs", session_id=SELF)


def _land(root: Path, run_id: str, token: str) -> dict:
    params = {
        "script_path": f"{run_id}.mjs", "incomplete_chunks": [], "inline_review": dict(REVIEWED),
        "session_id": SELF, "run_id": run_id, "run_token": token, "ledger_verdict": VERDICT,
    }
    return land_run._handler(params, repo_root=root / ".git")


def _claims(root: Path, minted_before=None, exclude_run=REFIRE):
    return warp_run_deliverable.live_run_write_claims(
        root, [F], exclude_session="", cwd=str(root), minted_before=minted_before, exclude_run=exclude_run,
    )


def test_a_refire_lands_past_a_same_sizing_predecessor_holding_a_fresh_token(repo):
    _declare_run(repo, PRED, (F,))
    time.sleep(0.05)
    token = _declare_run(repo, REFIRE, (F,), resumed_from=PRED)
    _author(repo, F, EDITED)

    out = _land(repo, REFIRE, token)

    assert out.get("refused") != shared_path_landing.SHARED_PATH_SIBLING_HELD, out
    assert not out.get("peer_held_left"), out
    assert out["committed"] is True, out
    assert _git(repo, "show", f"HEAD:{F}") == EDITED


def test_the_claim_scan_skips_a_same_sizing_predecessor_of_the_landing_run(repo):
    _declare_run(repo, PRED, (F,))
    _declare_run(repo, REFIRE, (F,), resumed_from=PRED)

    assert _claims(repo) == {}


def test_a_concurrent_same_sizing_sibling_not_in_the_resume_chain_still_holds_the_path(repo):
    _declare_run(repo, PRED, (F,))
    _declare_run(repo, REFIRE, (F,))

    assert _claims(repo) == {F: [SELF]}


def test_a_different_sizing_predecessor_still_holds_the_path(repo):
    _declare_run(repo, PRED, (F,), sizing=OTHER_SIZING)
    _declare_run(repo, REFIRE, (F,), resumed_from=PRED)

    assert _claims(repo) == {F: [SELF]}


def test_an_unknown_predecessor_sizing_still_holds_the_path(repo):
    _declare_run(repo, PRED, (F,), sizing="")
    _declare_run(repo, REFIRE, (F,), resumed_from=PRED)

    assert _claims(repo) == {F: [SELF]}


def test_an_unknown_landing_run_sizing_still_holds_the_path(repo):
    _declare_run(repo, PRED, (F,))
    _declare_run(repo, REFIRE, (F,), sizing="", resumed_from=PRED)

    assert _claims(repo) == {F: [SELF]}


def test_no_landing_run_means_no_sizing_skip(repo):
    _declare_run(repo, PRED, (F,))

    assert _claims(repo, exclude_run=None) == {F: [SELF]}
