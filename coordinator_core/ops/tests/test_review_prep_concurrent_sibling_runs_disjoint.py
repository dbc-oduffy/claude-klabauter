"""Two live runs fired from one session dirty disjoint files: each run's review.prep slices only its own."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops import warp_run_deliverable as wrd
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit import stage_dirt_snapshot as snap
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.session import claim_index, liveness
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SESSION = "sess-shared"
RUN_A = "warp-2026-11-16-d0c070-6-research-close-20261116T160610-c28297"
RUN_B = "warp-2026-11-16-62a222-1-memo-send-20261116T160555-c3a5c8"
A_OWN = "coordinator_core/ops/research_close.py"
A_TEST = "coordinator_core/ops/tests/test_research_close.py"
B_OWN = "coordinator_core/ops/fleet/memo_send.py"
B_OWN2 = "coordinator_core/ops/fleet/memo_wire.py"
PLAN = "docs/plans/p.md"
RUNS = {RUN_A: [A_OWN, A_TEST], RUN_B: [B_OWN, B_OWN2]}


class _Found(dict):
    recorded_kind = None


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    files = {rel: "v = 1\n" for rel in (A_OWN, A_TEST, B_OWN, B_OWN2)} | {PLAN: "---\nplan_id: pln-test-1\n---\n\n# p\n"}
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    for run, declared in RUNS.items():
        record = record_dir(tmp_path, run)
        record.mkdir(parents=True, exist_ok=True)
        manifest = {"review_declared_paths": declared, "rows": [{"writes": declared}]}
        (record / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        snap.write_snapshot(tmp_path, record)

    def lookup(paths, **kw):
        found = _Found({p: [SESSION] for p in paths})
        found.recorded_kind = {p: {SESSION: "write"} for p in paths}
        return found

    monkeypatch.setattr(claim_index, "lookup", lookup)
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: True)
    monkeypatch.setattr(liveness, "session_abandoned", lambda sid, cwd=None: False)
    monkeypatch.setattr(wrd, "live_run_write_claims", lambda *a, **kw: {})
    records = [{"run_id": run, "session_id": SESSION, "minted_at": float(i)} for i, run in enumerate(RUNS)]
    monkeypatch.setattr(run_token, "held_records", lambda root: records)
    monkeypatch.setattr(wrd, "_live_peer_run", lambda *a, **kw: True)
    for rel in (A_OWN, A_TEST, B_OWN, B_OWN2):
        (tmp_path / rel).write_text("v = 2\n", encoding="utf-8")
    return tmp_path


def _prep(repo: Path, run: str) -> dict:
    return rp._handler(
        {"run_id": run, "base": _git(repo, "rev-parse", "HEAD").strip(), "worktree": True, "plan_path": PLAN,
         "session_id": SESSION},
        repo,
    )


def test_each_sibling_run_slices_only_its_own_paths(repo):
    reply_a = _prep(repo, RUN_A)
    reply_b = _prep(repo, RUN_B)
    assert reply_a["error"] is None and reply_b["error"] is None
    assert set(reply_a["reviewed_sha256"]) == {A_OWN, A_TEST}
    assert set(reply_b["reviewed_sha256"]) == {B_OWN, B_OWN2}
    assert reply_a["undeclared_edits"] == [] and reply_b["undeclared_edits"] == []
    diff_a = Path(reply_a["whole_diff_path"]).read_text(encoding="utf-8")
    assert B_OWN not in diff_a and B_OWN2 not in diff_a


def test_without_the_sibling_declaration_its_session_claimed_dirt_joins_the_slices(repo, monkeypatch):
    # The witness is the sibling's declaration: with none live, one session claim cannot tell the runs apart.
    monkeypatch.setattr(run_token, "held_records", lambda root: [])
    reply = _prep(repo, RUN_A)
    assert reply["error"] is None
    assert {B_OWN, B_OWN2} <= set(reply["reviewed_sha256"])
