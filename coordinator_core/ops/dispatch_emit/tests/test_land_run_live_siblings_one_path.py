"""dispatch.land_run on two live ledger-bearing sibling ask runs that declare one product file: the
junior is refused while the senior holds the path, the senior lands it whole, the junior's re-fire
lands its own rows with the ledger met, and a ledger landing that withholds a product path for a
non-sibling reason commits with no ledger_met."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run, peer_hunk_gate, run_token
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

SELF = "5e1f0000-0000-4000-8000-0000000000c3"
SENIOR = "ask-20261024T100000-aaaaaa"
JUNIOR = "ask-20261024T100001-bbbbbb"
SOLO = "ask-20261024T100002-cccccc"
SIZING = "state/sizings/20260101-a.yaml"
F = "pkg/shared.py"
T = "tests/test_junior.py"
REVIEWED = reviewed()
VERDICT = {"items": [{"id": "R1", "status": "met", "wired": True}]}
DLV = "dlv-alpha-aaaaaa"
PLAN = "docs/plans/p.md"
NODE = "tests/test_a.py::test_x"
CLOSED: list = []
BASE = "base = 0\n"
BOTH_HUNKS = "base = 0\nsenior = 1\njunior = 1\n"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _author(root: Path, rel: str, text: str) -> None:
    """Write ``rel`` as the run session would: a claim plus a recorded hash of the bytes written."""
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
    CLOSED.clear()
    real_close = land_run._close_job

    def capture(r, ctx):
        CLOSED.append(dict(ctx["inputs"]))
        return real_close(r, ctx)

    monkeypatch.setattr(land_run, "_close_job", capture)
    return root


def _declare_run(root: Path, run_id: str, paths: tuple) -> str:
    """A live ledger-bearing ask run declaring ``paths`` in one chunk; returns its minted run token."""
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=paths),), deliverable_id=DLV, plan_path=PLAN)
    _write(root, f"{RUN_DIR_ROOT}/{run_id}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{run_id}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{run_id}/commit-request.txt",
    )
    _write(root, f"{RUN_DIR_ROOT}/{run_id}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, f"{run_id}.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{run_id}/manifest.json\n")
    rl.start(root, run_id, all_of_pm_ask=False)
    rl.bind(root, run_id, SIZING)
    return run_token.mint(root, run_id, f"{run_id}.mjs", session_id=SELF)


def _land(root: Path, run_id: str, token: str, **extra) -> dict:
    params = {
        "script_path": f"{run_id}.mjs", "incomplete_chunks": [], "inline_review": dict(REVIEWED),
        "session_id": SELF, "run_id": run_id, "run_token": token, "ledger_verdict": VERDICT, **extra,
    }
    return land_run._handler(params, repo_root=root / ".git")


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").strip()


def _tree(root: Path, rev: str = "HEAD") -> set:
    return set(_git(root, "show", "--name-only", "--format=", rev).split())


def _dirty(root: Path) -> set:
    return {ln[3:] for ln in _git(root, "status", "--porcelain", "-uall").splitlines()}


@pytest.fixture
def siblings(repo):
    """SENIOR then JUNIOR, minted in that order, both declaring F; JUNIOR also T. F carries both hunks."""
    senior_token = _declare_run(repo, SENIOR, (F,))
    time.sleep(0.05)
    junior_token = _declare_run(repo, JUNIOR, (F, T))
    _author(repo, F, BOTH_HUNKS)
    _author(repo, T, "def test_junior():\n    assert True\n")
    return repo, senior_token, junior_token


def test_the_junior_is_refused_while_the_senior_holds_the_shared_path(siblings):
    repo, senior_token, junior_token = siblings
    head = _head(repo)

    out = _land(repo, JUNIOR, junior_token)

    assert out.get("refused") == shared_path_landing.SHARED_PATH_SIBLING_HELD, out
    assert out.get("committed") is not True
    assert F in out["error"] and f"held by {SELF}" in out["error"], out
    assert out["peer_held_left"] == [{"path": F, "sessions": [SELF]}], out
    assert _head(repo) == head
    assert T in _dirty(repo) and F in _dirty(repo)
    assert run_token.admits(repo, JUNIOR, junior_token, f"{JUNIOR}.mjs")
    assert not CLOSED


def test_the_senior_lands_the_shared_file_whole_past_the_junior_declaration(siblings):
    repo, senior_token, junior_token = siblings

    out = _land(repo, SENIOR, senior_token)

    assert out["committed"] is True, out
    assert out.get("refused") != peer_hunk_gate.PEER_HUNKS_CO_DECLARED, out
    assert F in _tree(repo)
    assert _git(repo, "show", f"HEAD:{F}") == BOTH_HUNKS
    assert T in _dirty(repo)
    assert CLOSED and CLOSED[-1].get("ledger_met")


def test_the_junior_refire_after_the_senior_landed_lands_its_rows_with_the_ledger_met(siblings):
    repo, senior_token, junior_token = siblings
    refused = _land(repo, JUNIOR, junior_token)
    assert refused.get("refused") == shared_path_landing.SHARED_PATH_SIBLING_HELD, refused
    assert _land(repo, SENIOR, senior_token)["committed"] is True

    out = _land(repo, JUNIOR, junior_token)

    assert out["committed"] is True, out
    assert out.get("refused") is None, out
    assert T in _tree(repo)
    assert _git(repo, "show", f"HEAD:{F}") == BOTH_HUNKS
    assert F not in {p for ps in (out.get("stranded") or {}).values() for p in ps}
    assert F not in _dirty(repo) and T not in _dirty(repo)
    inputs = CLOSED[-1]
    assert inputs.get("ledger_met") and "unlanded" not in inputs, inputs
    assert shared_path_landing.LEDGER_WITHHELD_PRODUCT not in out


def test_a_ledger_landing_withholding_a_product_path_for_a_non_sibling_reason_commits_unmet(repo):
    token = _declare_run(repo, SOLO, (F, T))
    _author(repo, F, BOTH_HUNKS)
    _author(repo, T, "def test_solo():\n    assert True\n")

    out = _land(repo, SOLO, token, withhold_paths=[{"path": F, "reason": "em keeps it"}])

    assert out["committed"] is True, out
    assert T in _tree(repo) and F not in _tree(repo)
    assert F in _dirty(repo)
    assert "ledger_met" not in CLOSED[-1]
    assert out[shared_path_landing.LEDGER_WITHHELD_PRODUCT] == [F], out
    assert F in out["exit_criterion_not_stamped"], out
