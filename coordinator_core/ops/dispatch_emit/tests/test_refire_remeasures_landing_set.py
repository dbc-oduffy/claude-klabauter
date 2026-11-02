"""Refire loops through dispatch.land_run at default params: the refire a landing refusal's remedy names
(re-measure via dispatch.measured_rev; workflow.revise plus a fresh review) must land the run's own
edits, while a foreign byte change after measure or review still refuses.

Case A: a path the run wrote outside the verify leg's measured path list. Case B: a review-applied fix
refused as blob-drift. Case N: foreign drift refuses on both gates.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run, measured_rev, requirements_ledger
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import (  # noqa: F401
    RUN2,
    halted,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git, _write, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_land_run_landing_remedies import (  # noqa: F401
    RUN,
    SELF,
    _author,
    _declare,
    _land,
)
from coordinator_core.ops.dispatch_emit.tests.test_land_run_landing_remedies import repo as drift_repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_land_run_met_over_footprint_breach_incident import (
    C3_PATH,
    C4_PATH,
    LANE,
    LANE_TEST,
    _stage_replanned_run,
)
from coordinator_core.ops.dispatch_emit.tests.test_ledger_remaining_leg_footprint_fence import _terminal_with
from coordinator_core.ops.dispatch_emit.tests.test_met_refire_lands_its_own_product_changes import _composed
from coordinator_core.session import core as session_core
from coordinator_core.session import touch_record

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_VERDICT = {"items": [{"id": "R1", "status": "met", "wired": True, "met_by": ["C3"]}]}
_AGENT = "agent-review-refire"


def _verify_leg_paths(root: Path) -> list:
    """The path list the verify leg hands dispatch.measured_rev: the manifest's review_declared_paths."""
    manifest = json.loads((root / RUN_DIR_ROOT / RUN2 / "manifest.json").read_text(encoding="utf-8"))
    return list(manifest["review_declared_paths"])


def _stage_case_a(root: Path) -> tuple:
    """Run RUN2 halted; its re-plan appended row C5 (LANE, LANE_TEST) after the verify leg's declared
    list was fixed, so the run wrote paths the list does not name."""
    resume = _composed(root)
    token = _stage_replanned_run(root, '  register_rows: ["R2"]\n')
    manifest_path = root / RUN_DIR_ROOT / RUN2 / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["review_declared_paths"] = [C3_PATH, C4_PATH]
    _write(root, f"{RUN_DIR_ROOT}/{RUN2}/manifest.json", json.dumps(manifest))
    return resume, token


def _measure_declared_only(root: Path, monkeypatch) -> dict:
    """The pre-fix verify leg: dispatch.measured_rev over the manifest's declared list alone, the op not
    yet unioning the run's own recorded writes."""
    with monkeypatch.context() as ctx:
        ctx.setattr(measured_rev, "_run_landing_paths", lambda _root, _run_id: [])
        out = measured_rev.measured_rev(RUN2, _verify_leg_paths(root), root)
    assert out["paths"] and LANE not in out["paths"], out
    return out


def _remeasure(root: Path) -> dict:
    """The call the remedy prints: `dispatch.measured_rev {"run_id": ..., "paths": []}`."""
    out = measured_rev.measured_rev(RUN2, [], root)
    assert {LANE, LANE_TEST} <= set(out["paths"]), out
    return out


def test_case_a_first_halt_is_the_unmeasured_run_edit(halted, monkeypatch):  # noqa: F811
    resume, token = _stage_case_a(halted)
    _measure_declared_only(halted, monkeypatch)

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is False and out["refused"] == land_run.LANDING_TREE_DIFFERS, out
    assert set(out["paths"]) == {LANE, LANE_TEST}, out
    assert f'"run_id": "{RUN2}", "paths": []' in out["error"], out


def test_case_a_verify_leg_measure_already_carries_the_runs_own_edit(halted):  # noqa: F811
    resume, token = _stage_case_a(halted)

    measured_rev.measured_rev(RUN2, _verify_leg_paths(halted), halted)
    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out.get("refused") is None and out["committed"] is True, out


def test_case_a_refire_after_remeasure_lands_the_runs_own_edit(halted, monkeypatch):  # noqa: F811
    resume, token = _stage_case_a(halted)
    _measure_declared_only(halted, monkeypatch)
    first = _terminal_with(halted, token, resume, _VERDICT)
    assert first["refused"] == land_run.LANDING_TREE_DIFFERS, first

    _remeasure(halted)
    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out.get("refused") is None and out["committed"] is True, out
    landed = set(_git(halted, "show", "--name-only", "--format=", out["sha"]).split())
    assert {C3_PATH, LANE, LANE_TEST} <= landed


def test_case_n_foreign_edit_after_measure_still_refuses_landing_tree_differs(halted):  # noqa: F811
    resume, token = _stage_case_a(halted)
    measured_rev.measured_rev(RUN2, [C3_PATH, C4_PATH, LANE, LANE_TEST], halted)
    _write(halted, C3_PATH, "a = 3  # foreign edit after the measure\n")
    head = _git(halted, "rev-parse", "HEAD")

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is False and out["refused"] == land_run.LANDING_TREE_DIFFERS, out
    assert C3_PATH in out["paths"], out
    assert _git(halted, "rev-parse", "HEAD") == head


def _submit(root: Path, at: float) -> None:
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/submission.json", json.dumps({"submitted_at": at}))


def _review_agent_writes(root: Path, rel: str, text: str, at: float) -> None:
    """A review agent rewrites `rel` and records a hashed write event in its own sink, as the production
    touch hook does and `_review_write_events` reads."""
    _write(root, rel, text)
    hub = Path(session_core.sessions_dir(str(root)))
    agent_dir = hub / ".agents" / _AGENT
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "em-session-id.txt").write_text(SELF + "\n", encoding="utf-8")
    (hub / SELF / "dispatched-agents.txt").write_text(
        f"{_AGENT}\thost\tcoordinator:code-reviewer\t1\n", encoding="utf-8"
    )
    with (agent_dir / touch_record.RECORD_FILENAME).open("ab") as fh:
        fh.write(
            touch_record.encode_line(
                session_id=SELF, agent_id=_AGENT, verb=touch_record.VERB_TOUCH, path=rel, timestamp=at,
                content_hash=touch_record.compute_content_hash(root / rel), kind=touch_record.KIND_WRITE,
            )
        )


def _stage_case_b(root: Path) -> float:
    """A ledger run whose a.py carries a review-applied fix no recorded write explains: blob-drift."""
    now = time.time()
    _submit(root, now - 100)
    _author(root, "a.py", "a = 1\n")
    _declare(root, [("C1", ["a.py"])])
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/run.json", json.dumps({"run_id": RUN}))
    _write(root, "a.py", "a = 2  # review-applied fix\n")
    return now


def test_case_b_first_halt_is_blob_drift_over_the_review_fix(drift_repo):  # noqa: F811
    _stage_case_b(drift_repo)

    out = _land(drift_repo)

    assert out["committed"] is False and out["refused"] == "blob-drift", out
    assert f"workflow.revise run_id={RUN}" in out["error"], out


def test_case_b_refire_whose_review_wrote_the_fix_lands(drift_repo):  # noqa: F811
    """Positive control, green at base: a fresh submission plus a hashed review write is a baseline."""
    now = _stage_case_b(drift_repo)
    first = _land(drift_repo)
    assert first["refused"] == "blob-drift", first

    revised = requirements_ledger._handler(
        {"run_id": RUN, "note": "review applied a fix to a.py"}, repo_root=drift_repo / ".git"
    )
    assert "error" not in revised, revised
    _submit(drift_repo, now + 1)
    _review_agent_writes(drift_repo, "a.py", "a = 3  # the refire review's fix\n", now + 2)
    out = _land(drift_repo)

    assert out.get("refused") is None and out["committed"] is True, out
    assert "a.py" in _git(drift_repo, "show", "--name-only", "--format=", "HEAD").split()


def test_case_b_land_after_revise_alone_still_refuses_and_says_a_review_must_run_first(drift_repo):  # noqa: F811
    """workflow.revise records a note and re-baselines nothing: bytes no review read stay refused, and
    the remedy line printed says the expectation moves only with the refire's own review pass."""
    _stage_case_b(drift_repo)
    first = _land(drift_repo)
    assert first["refused"] == "blob-drift", first

    revised = requirements_ledger._handler(
        {"run_id": RUN, "note": "review applied a fix to a.py"}, repo_root=drift_repo / ".git"
    )
    assert "error" not in revised, revised
    head = _git(drift_repo, "rev-parse", "HEAD")
    out = _land(drift_repo)

    assert out["committed"] is False and out["refused"] == "blob-drift", out
    assert "a land before that review repeats this refusal" in out["error"], out
    assert "the refire's review re-reads the current bytes" not in out["error"], out
    assert _git(drift_repo, "rev-parse", "HEAD") == head


def test_case_n_foreign_write_after_the_refire_review_still_refuses_blob_drift(drift_repo):  # noqa: F811
    now = _stage_case_b(drift_repo)
    requirements_ledger._handler({"run_id": RUN, "note": "review applied a fix"}, repo_root=drift_repo / ".git")
    _submit(drift_repo, now + 1)
    _review_agent_writes(drift_repo, "a.py", "a = 3  # the refire review's fix\n", now + 2)
    _write(drift_repo, "a.py", "a = 4  # foreign edit after the review\n")

    out = _land(drift_repo)

    assert out["committed"] is False and out["refused"] == "blob-drift", out
