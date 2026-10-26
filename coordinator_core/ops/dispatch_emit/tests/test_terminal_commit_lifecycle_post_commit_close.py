"""A lifecycle-only post_commit row counts as met for the terminal commit's flip and baton gates, and
an undischarged lifecycle act is reported in ``post_commit_owed``."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RUN_DIR_ROOT,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests import test_terminal_commit_register_ship_in_run as base
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_register_ship_in_run import (  # noqa: F401
    _ARCHIVED_BATON, _BATON, _PLAN, _SID, _SIZING, _git, _row, _sizing_text, repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "run1"
_LIFECYCLE = {
    "id": "R1", "status": "post_commit", "wired": True,
    "post_commit_acts": ["baton_archive", "plan_implemented"],
}
_XS_PLAN = base._PLAN_TEXT.replace("status: executing\n", "status: executing\nderived_from: sizing object\n")
_EXTRA_ROW = (
    "  deferred: false\n- id: C4\n  title: t4\n  change_kind: code-edit\n  surface: b.py\n"
    "  disposition: open\n  deferred: false\n```"
)


@pytest.fixture(autouse=True)
def _no_review_write_source(monkeypatch):
    from coordinator_core.ops.dispatch_emit import review_edits

    monkeypatch.setattr(review_edits, "review_writes", lambda *a, **k: {})


def _fire(repo, ledger_verdict, *, flip_refused: bool = False, chunk_report: str = "") -> dict:
    plan = _XS_PLAN.replace("  deferred: false\n```", _EXTRA_ROW) if flip_refused else _XS_PLAN
    (repo / _PLAN).write_text(plan, encoding="utf-8")
    (repo / _SIZING).write_text(_sizing_text([_row("R1")]), encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "xs shape"], repo)
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),), plan_path=_PLAN)
    run_dir = repo / RUN_DIR_ROOT / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "commit-request.txt").write_text(render_marker(request) + "\n", encoding="utf-8")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
    )
    (run_dir / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    (repo / "run.mjs").write_text(
        f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n", encoding="utf-8"
    )
    rl.start(repo, RUN, all_of_pm_ask=False)
    rl.bind(repo, RUN, _SIZING)
    review = {
        "integration_stem": "pln-x-123456.review-wave-bookkeeping", "slices": 1, "fixes": 0,
        "plan_id": "pln-x-123456", "prep_sidecar": None, "wave_sidecar_paths": [],
        "wave_reviews": reviewed()["wave_reviews"],
        "prep": {"run_base_sha": "a" * 40, "product_files": 1, "foreign_claims": [], "slice_files": ["a.py"]},
        "delivery": {"verdict": "PASS", "product_files": 1, "claims_unbacked": 0},
        "tests": {"status": "not_run", "run": 0, "failed": 0, "sidecar": None},
        "criterion": {"status": "indeterminate", "observation": "o", "sidecar": "j.md", "register_rows": []},
    }
    return terminal_commit._handler(
        {"inline_review": review, "script_path": "run.mjs", "incomplete_chunks": [], "session_id": _SID,
         "ledger_verdict": ledger_verdict},
        repo_root=repo / ".git",
    )


def test_a_lifecycle_only_post_commit_row_flips_the_plan_archives_the_baton_and_owes_nothing(repo):
    out = _fire(repo, {"items": [_LIFECYCLE]})

    assert out["committed"] is True and "coded_stamp_error" not in out, out
    assert out["plan_status"] == "implemented", out.get("plan_status_refusal")
    assert out["archived"][_BATON] == _ARCHIVED_BATON
    assert (repo / _ARCHIVED_BATON).is_file() and not (repo / _BATON).exists()
    assert "post_commit_owed" not in out


def test_a_refused_flip_reports_the_lifecycle_acts_still_owed(repo):
    out = _fire(repo, {"items": [_LIFECYCLE]}, flip_refused=True)

    assert out["committed"] is True, out
    assert out["plan_status"] == "refused"
    owed = out["post_commit_owed"]
    assert [o["id"] for o in owed] == ["R1"]
    assert "plan_implemented" in owed[0]["acts"] and owed[0]["sha"] == out["sha"]


def test_a_sha_memo_row_still_owes_its_memo_after_the_lifecycle_acts_land(repo):
    row = {**_LIFECYCLE, "post_commit_acts": ["baton_archive", "plan_implemented", "sha_memo"]}
    out = _fire(repo, {"items": [row]})

    assert out["committed"] is True, out
    assert [o["acts"] for o in out["post_commit_owed"]] == [row["post_commit_acts"]]


def test_a_lifecycle_row_does_not_regrade_a_chunk_whose_report_is_blocked(tmp_path):
    (tmp_path / "r.md").write_text("BLOCKED on: C1\n<exit-status>BLOCKED</exit-status>\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",), report="r.md"),))
    verdict = {"items": [_LIFECYCLE]}
    assert terminal_commit._ledger_met_ids(verdict) == ["R1"]
    review = {"prep": {"slice_files": ["a.py"]}, "integration": {}}
    regraded = terminal_commit._ledger_met_regrades(
        tmp_path, request, {"C1"}, review, {}, verdict, set()
    )
    assert regraded == {}
