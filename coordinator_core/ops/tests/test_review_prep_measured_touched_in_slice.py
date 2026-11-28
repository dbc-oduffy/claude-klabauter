"""The review slice equals the run's own touched set: a run-written path with no base delta is credited
(22e80b), a sibling run's path on the shared tree is never sliced (8beeec)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops import warp_run_deliverable as wrd
from coordinator_core.ops.dispatch_emit import land_run, run_token
from coordinator_core.ops.dispatch_emit import stage_dirt_snapshot as snap
from coordinator_core.ops.dispatch_emit import terminal_commit_refusals as tcr
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.session import claim_index, liveness
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SESSION = "sess-shared"
RUN_A = "warp-2026-11-20-bounded-metadata-backfill-6d24f1-20261120T191758-22e80b"
RUN_B = "warp-2026-11-20-casper-daugaard-likeness-run-644291-20261120T192905-8beeec"
A_SRC = "example_retrieval_repo_core/metadata_backfill.py"
A_TEST = "tests/test_metadata_backfill.py"
A_CLI_TEST = "tests/test_metadata_backfill_cli.py"
B_SRC = "tools/likeness/delivery_shots.py"
B_PLAN = "docs/plans/2026-11/2026-11-19-sibling-run-plan-eaced5.md"
PLAN = "docs/plans/p.md"
RUNS = {RUN_A: [A_SRC, A_TEST, A_CLI_TEST], RUN_B: [B_SRC]}


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
    files = {rel: "v = 1\n" for rel in (A_SRC, A_TEST, A_CLI_TEST, B_SRC)}
    files[B_PLAN] = "---\nplan_id: pln-sibling-1\n---\n\n# sibling plan\n"
    files[PLAN] = "---\nplan_id: pln-test-1\n---\n\n# p\n"
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
    (tmp_path / A_SRC).write_text("v = 2\n" * 30, encoding="utf-8")
    (tmp_path / A_TEST).write_text("v = 2\n" * 30, encoding="utf-8")
    (tmp_path / A_CLI_TEST).write_text("v = 1\n", encoding="utf-8")  # rewritten byte-identical: no base delta
    (tmp_path / B_SRC).write_text("v = 2\n" * 30, encoding="utf-8")
    (tmp_path / B_PLAN).write_text("---\nplan_id: pln-sibling-1\n---\n\n# sibling plan\n\nedited\n", encoding="utf-8")
    return tmp_path


def _prep(repo: Path, run: str) -> dict:
    return rp._handler(
        {"run_id": run, "base": _git(repo, "rev-parse", "HEAD").strip(), "worktree": True, "plan_path": PLAN,
         "session_id": SESSION},
        repo,
    )


def _sliced(reply: dict) -> set:
    return {f for s in reply.get("slices") or [] for f in s.get("files") or []}


def _reconciled(repo: Path, reply: dict) -> dict:
    base = _git(repo, "rev-parse", "HEAD").strip()
    request = SimpleNamespace(chunks=[SimpleNamespace(paths=tuple(RUNS[RUN_A]))])
    inline_review = {"prep": {"run_base_sha": base, "product_files": reply["product_files"],
                              "slice_files": sorted(_sliced(reply)), "foreign_claims": []}}
    _, _, matched = land_run._reconcile_prep(repo, SESSION, RUN_A, request, inline_review)
    assert matched
    return inline_review


def test_a_run_written_path_with_no_base_delta_is_in_the_prep_block(repo):
    reply = _prep(repo, RUN_A)
    assert reply["error"] is None
    assert A_CLI_TEST not in _sliced(reply)
    assert {"path": A_CLI_TEST, "reason": "no-delta"} in reply["excluded"]
    assert A_CLI_TEST in rp.prep_block(reply)["slice_files"]


def test_the_22e80b_landing_marks_no_measured_touched_path_unreviewed(repo):
    inline_review = _reconciled(repo, _prep(repo, RUN_A))
    assert tcr._staged_outside_slices_provenance(inline_review, RUNS[RUN_A]) == []


def test_a_landing_path_the_run_did_not_write_stays_unreviewed(repo):
    inline_review = _reconciled(repo, _prep(repo, RUN_A))
    rows = tcr._staged_outside_slices_provenance(inline_review, [B_SRC])
    assert [r["path"] for r in rows] == [B_SRC]


def test_a_sibling_runs_path_on_the_shared_tree_is_not_in_the_slice(repo):
    reply = _prep(repo, RUN_A)
    assert reply["error"] is None
    assert B_SRC not in reply["reviewed_sha256"] and B_PLAN not in reply["reviewed_sha256"]
    assert B_PLAN not in _sliced(reply) and B_PLAN not in rp.prep_block(reply)["slice_files"]
    assert B_PLAN not in Path(reply["whole_diff_path"]).read_text(encoding="utf-8")


def test_a_sibling_plan_doc_is_foreign_and_the_runs_own_plan_doc_is_not(repo):
    (repo / PLAN).write_text("---\nplan_id: pln-test-1\n---\n\n# p\n\nown edit\n", encoding="utf-8")
    kept = rp.undeclared_edits_for_run(repo, RUN_A, RUNS[RUN_A], SESSION, plan_path=PLAN)
    assert B_PLAN not in kept
    assert PLAN in kept


PEER_TEST = "tests/install/test_prereq_resolver.py"


def test_a_red_overlay_naming_only_a_peer_runs_test_file_is_that_runs_not_this_landings():
    node = f"{PEER_TEST}::test_run_cmd_spawn_resolves_npm_cmd_through_shared_helper"
    assert land_run._red_is_foreign_runs([node], {PEER_TEST: "e7c174"}) is True


def test_a_red_overlay_with_any_failing_node_outside_a_peer_held_file_still_refuses():
    peer = f"{PEER_TEST}::test_a"
    own = "tests/test_own.py::test_b"
    held = {PEER_TEST: "e7c174"}
    assert land_run._red_is_foreign_runs([peer, own], held) is False
    assert land_run._red_is_foreign_runs([peer], {PEER_TEST: "unattributed"}) is False
    assert land_run._red_is_foreign_runs([], held) is False


def test_an_empty_plan_path_treats_no_plan_doc_as_foreign():
    assert rp._foreign_plan_doc(B_PLAN, "") is False
    assert rp._foreign_plan_doc(B_PLAN, PLAN) is True
    assert rp._foreign_plan_doc(PLAN, PLAN) is False
