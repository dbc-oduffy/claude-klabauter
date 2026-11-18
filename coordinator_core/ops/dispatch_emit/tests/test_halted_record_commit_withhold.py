"""terminal_commit halt route: a path in the run's withhold.jsonl stays out of the halt commit."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from coordinator_core.ops.dispatch_emit import run_artifacts, run_withhold
from coordinator_core.ops.dispatch_emit.tests.test_halted_record_withholds_peer_claims import oracle  # noqa: F401
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    LEDGER,
    PASSING,
    RUN,
    _call,
    _landed,
    _marker_script,
    _write_record,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_artifacts_only_ledger_halt_with_zero_review_verdicts_commits(repo):
    # A ledger halt with no targeted-test verdict withholds product (halt-record-only) and still
    # commits the record; the review floor never refuses it.
    _write_record(repo, RUN)
    (repo / "done.py").write_text("d = 1\n", encoding="utf-8")
    _marker_script(repo, [("C1", ["done.py"])], {"C1": "DONE: C1\n"})

    out = _call(repo, LEDGER, inline_review={"integration_stem": "s", "slices": 1, "fixes": 0})

    assert out["committed"] is True, out
    assert out.get("refused") != "review-floor"
    names = _landed(repo, out, "--name-only")
    assert "done.py" not in names
    assert run_artifacts.outcome_rel(RUN) in names


def test_withheld_dirty_declared_path_stays_out_of_the_halt_commit(repo):
    _write_record(repo, RUN)
    (repo / "done.py").write_text("d = 1\n", encoding="utf-8")
    (repo / "peer_owned.py").write_text("o = 1\n", encoding="utf-8")
    _marker_script(
        repo, [("C1", ["done.py"]), ("C2", ["peer_owned.py"])], {"C1": "DONE: C1\n", "C2": "DONE: C2\n"}
    )
    run_withhold.record(
        repo / RECORD_DIR_ROOT / RUN, [{"path": "peer_owned.py", "reason": "owned by peer run d238dc"}], "t"
    )

    out = _call(repo, LEDGER, **PASSING)

    # done.py passed review but the run recorded no write baseline for it, so hunk provenance holds it.
    assert out["withheld"] == {"peer_owned.py": "owned by peer run d238dc", "done.py": "foreign-hunk-unproduced"}
    names = _landed(repo, out, "--name-only")
    assert "done.py" not in names and "peer_owned.py" not in names


def _claimant(repo, suffix, stage, writes):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    run_dir = repo / RECORD_DIR_ROOT / f"ask-{stamp}-{suffix * 3}"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({"rows": [{"writes": writes}]}), encoding="utf-8")
    (run_dir / "run-stage.json").write_text(json.dumps({"stage": stage}), encoding="utf-8")


def _indeterminate(repo, oracle, *paths):
    _write_record(repo, RUN)
    (repo / "done.py").write_text("d = 1\n", encoding="utf-8")
    (repo / "mod.py").write_text("m = 1\n", encoding="utf-8")
    (repo / "test_mod.py").write_text("from mod import m\n", encoding="utf-8")
    _marker_script(repo, [("C1", ["done.py"]), ("C2", ["mod.py", "test_mod.py"])], {"C1": "DONE: C1\n", "C2": "DONE: C2\n"})
    oracle.warnings = [f"claim state indeterminate for {p!r}" for p in paths]
    return _call(repo, LEDGER, **PASSING)


def test_manifests_of_landed_and_halted_runs_are_no_peer_claim(repo, oracle):
    for suffix, stage in (("a1", "landed"), ("a2", "landed"), ("a3", "landed"), ("a4", "halted")):
        _claimant(repo, suffix, stage, ["done.py"])

    out = _indeterminate(repo, oracle, "done.py")

    assert "peer-claim-indeterminate" not in out["withheld"].values()


def test_a_live_claimant_manifest_still_withholds(repo, oracle):
    _claimant(repo, "b1", "landed", ["done.py"])
    _claimant(repo, "b2", "staged", ["done.py"])

    out = _indeterminate(repo, oracle, "done.py")

    assert out["withheld"]["done.py"] == "peer-claim-indeterminate"


def test_no_claimant_manifest_still_withholds(repo, oracle):
    out = _indeterminate(repo, oracle, "done.py")

    assert out["withheld"]["done.py"] == "peer-claim-indeterminate"


def test_a_run_written_module_is_not_withheld_from_its_seeded_test(repo, oracle):
    out = _indeterminate(repo, oracle, "mod.py")

    assert out["withheld"].get("mod.py") != "peer-claim-indeterminate"
    assert "peer-claim-indeterminate" not in out["withheld"].values()


def test_an_unrelated_run_path_is_still_withheld_on_its_own_indeterminate_claim(repo, oracle):
    out = _indeterminate(repo, oracle, "done.py")

    assert out["withheld"]["done.py"] == "peer-claim-indeterminate"
    assert out["withheld"].get("mod.py") != "peer-claim-indeterminate"
