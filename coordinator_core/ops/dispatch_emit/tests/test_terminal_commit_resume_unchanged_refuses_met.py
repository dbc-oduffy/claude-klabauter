"""A ledger resume's terminal gate refuses a blocking row judged met while no declared product path changed since the halt."""

from __future__ import annotations

import json
import subprocess

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, UNCHANGED_SINCE_HALT_REASON
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261017T000000-abc123"
REL = "state/sizings/2026-10-17-resume.yaml"
MET = {"items": [{"id": "R1", "status": "met", "wired": True}]}


def _git(root, *a):
    out = subprocess.run(
        ["git", *a], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    )
    return out.stdout.strip()


@pytest.fixture
def run(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "x", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "deliver the owed work"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    (tmp_path / "prod.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "halt")
    halt = _git(tmp_path, "rev-parse", "HEAD")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL)
    run_dir = tmp_path / RECORD_DIR_ROOT / RUN
    manifest = run_dir / "manifest.json"
    manifest.write_text(
        json.dumps({"rows": [
            {"id": "C1", "writes": ["prod.py", f"{RECORD_DIR_ROOT}/x.md", "state/y.yaml"]},
        ]}),
        encoding="utf-8",
    )
    return tmp_path, run_dir, manifest, halt


def _gate(run, resume, verdict=MET, manifest=True):
    root, run_dir, mpath, _ = run
    return tc._ledger_gate(root, run_dir, verdict, mpath if manifest else None, resume_ledger=resume)[0]


def _resume(run, sha=None):
    return {"run_id": "old", "halt_sha": run[3] if sha is None else sha, "blocking": ["R1"]}


def test_unchanged_tree_with_met_verdict_is_refused(run):
    refusal = _gate(run, _resume(run))
    assert refusal["refused"] == "ledger-unmet"
    assert refusal["ledger_failures"] == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


def test_process_file_change_does_not_count(run):
    (run[0] / "state" / "y.yaml").write_text("a: 1\n", encoding="utf-8")
    assert _gate(run, _resume(run))["refused"] == "ledger-unmet"


def test_out_of_band_commit_to_a_declared_path_passes(run):
    (run[0] / "prod.py").write_text("x = 2\n", encoding="utf-8")
    _git(run[0], "add", "prod.py")
    _git(run[0], "commit", "-q", "-m", "hand fix")
    assert _gate(run, _resume(run)) is None


def test_uncommitted_edit_to_a_declared_path_passes(run):
    (run[0] / "prod.py").write_text("x = 3\n", encoding="utf-8")
    assert _gate(run, _resume(run)) is None


def test_new_untracked_file_at_a_declared_path_passes(run):
    run[2].write_text(json.dumps({"rows": [{"id": "C1", "writes": ["prod.py", "fresh.py"]}]}), encoding="utf-8")
    (run[0] / "fresh.py").write_text("y = 1\n", encoding="utf-8")
    assert _gate(run, _resume(run)) is None


def test_no_param_behaves_as_before_and_spawns_no_git(run, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("git spawned")

    monkeypatch.setattr(tc, "run_git", boom)
    assert _gate(run, None) is None


@pytest.mark.parametrize("sha", ["main", "abc1234", "HEAD", ""])
def test_non_40_hex_halt_sha_is_refused(run, sha):
    (run[0] / "prod.py").write_text("x = 4\n", encoding="utf-8")
    refusal = _gate(run, _resume(run, sha))
    assert refusal["ledger_failures"][0]["reason"] == UNCHANGED_SINCE_HALT_REASON


def test_missing_halt_sha_key_is_refused(run):
    (run[0] / "prod.py").write_text("x = 5\n", encoding="utf-8")
    assert _gate(run, {"blocking": ["R1"]})["refused"] == "ledger-unmet"


def test_empty_declared_set_is_refused(run):
    run[2].write_text(json.dumps({"rows": [{"id": "C1", "writes": ["state/y.yaml"]}]}), encoding="utf-8")
    (run[0] / "prod.py").write_text("x = 6\n", encoding="utf-8")
    assert _gate(run, _resume(run))["refused"] == "ledger-unmet"


def test_unknown_sha_git_error_is_refused(run):
    (run[0] / "prod.py").write_text("x = 7\n", encoding="utf-8")
    assert _gate(run, _resume(run, "0" * 40))["refused"] == "ledger-unmet"


def test_not_met_verdict_is_not_touched_by_the_diff(run, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("git spawned")

    monkeypatch.setattr(tc, "run_git", boom)
    verdict = {"items": [{"id": "R1", "status": "not_met", "remaining": ["wire it"]}]}
    refusal = _gate(run, _resume(run), verdict)
    assert all(f["reason"] != UNCHANGED_SINCE_HALT_REASON for f in refusal["ledger_failures"])


def test_verified_only_with_executed_evidence_passes(run):
    resume = {**_resume(run), "verified_only": {"R1": {"command": "python gate20.py", "observed": "20/20, 0 -32004"}}}
    assert _gate(run, resume) is None


@pytest.mark.parametrize("ev", [{}, {"command": "python gate20.py"}, {"observed": "ok"}, {"command": " ", "observed": "ok"}, True])
def test_verified_only_without_evidence_is_refused(run, ev):
    resume = {**_resume(run), "verified_only": {"R1": ev}}
    assert _gate(run, resume)["ledger_failures"] == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


# ask_gate's resume ledger also composes `since_sha` (the param that outranks a resolved halt) and
# `halt_sha` (legacy, outranked); `_composed` is the legacy {run_id, halt_sha, blocking} shape and
# never a `plan`.
PLAN = "docs/plans/2026-10-17-resume.md"


def _composed(halt_sha=None):
    return {"run_id": RUN, "halt_sha": halt_sha, "blocking": ["R1"]}


def _stamp_head(run, head, writes=("prod.py", f"{RECORD_DIR_ROOT}/x.md", "state/y.yaml")):
    run[2].write_text(
        json.dumps({"rows": [{"id": "C1", "writes": list(writes)}], "head_sha": head}), encoding="utf-8"
    )


def _gate_plan(run, resume, plan_rel=None):
    root, run_dir, mpath, _ = run
    return tc._ledger_gate(root, run_dir, MET, mpath, resume_ledger=resume, plan_rel=plan_rel)[0]


def _halt_commit(root, plan=PLAN):
    from coordinator_core.ops.dispatch_emit.ask_contract import halted_record_message

    message = halted_record_message(RUN, {"halted": "ledger-check", "plan": plan}) + "\nLands-Chunks: C1"
    _git(root, "commit", "-q", "--allow-empty", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def test_composed_param_with_stage_head_passes_when_declared_path_changed(run):
    _stamp_head(run, run[3])
    (run[0] / "prod.py").write_text("x = 8\n", encoding="utf-8")
    assert _gate_plan(run, _composed()) is None


def test_composed_param_with_stage_head_refuses_unchanged_and_names_the_base(run):
    _stamp_head(run, run[3])
    refusal = _gate_plan(run, _composed())
    assert refusal["refused"] == "ledger-unmet"
    assert refusal["ledger_failures"] == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]
    assert refusal["since_halt"] == {"base": run[3], "base_source": "stage-head", "outcome": "unchanged"}


def test_composed_param_resolves_halt_commit_from_plan_rel(run):
    halt = _halt_commit(run[0])
    (run[0] / "prod.py").write_text("x = 9\n", encoding="utf-8")
    assert _gate_plan(run, _composed(), plan_rel=PLAN) is None
    _git(run[0], "checkout", "--", "prod.py")
    refusal = _gate_plan(run, _composed(), plan_rel=PLAN)
    assert refusal["since_halt"] == {"base": halt, "base_source": "resolved", "outcome": "unchanged"}


def test_param_sha_outranks_plan_rel_and_stage_head(run):
    _halt_commit(run[0])
    _stamp_head(run, run[3])
    refusal = _gate_plan(run, {**_composed(), "since_sha": run[3]}, plan_rel=PLAN)
    assert refusal["since_halt"] == {"base": run[3], "base_source": "param", "outcome": "unchanged"}


def test_a_legacy_halt_sha_param_is_outranked_by_the_resolved_first_halt(run):
    halt = _halt_commit(run[0])
    _stamp_head(run, run[3])
    refusal = _gate_plan(run, _composed(run[3]), plan_rel=PLAN)
    assert refusal["since_halt"] == {"base": halt, "base_source": "resolved", "outcome": "unchanged"}


def test_no_base_at_all_is_refused_as_no_base(run):
    (run[0] / "prod.py").write_text("x = 10\n", encoding="utf-8")
    refusal = _gate_plan(run, _composed(), plan_rel=PLAN)
    assert refusal["refused"] == "ledger-unmet"
    assert refusal["since_halt"] == {"base": None, "base_source": None, "outcome": "no-base"}


def test_empty_declared_set_is_named_no_declared_paths(run):
    _stamp_head(run, run[3], writes=("state/y.yaml",))
    (run[0] / "prod.py").write_text("x = 11\n", encoding="utf-8")
    refusal = _gate_plan(run, _composed())
    assert refusal["since_halt"]["outcome"] == "no-declared-paths"


def test_git_failure_is_named_by_its_exit_code(run):
    (run[0] / "prod.py").write_text("x = 12\n", encoding="utf-8")
    refusal = _gate_plan(run, _composed("0" * 40))
    assert refusal["since_halt"] == {"base": "0" * 40, "base_source": "param", "outcome": "git-exit-128"}


def test_stage_head_that_is_not_a_full_sha_is_no_base(run):
    _stamp_head(run, "HEAD")
    (run[0] / "prod.py").write_text("x = 13\n", encoding="utf-8")
    assert _gate_plan(run, _composed())["since_halt"]["outcome"] == "no-base"


def test_product_landed_in_a_commit_before_the_halt_reports_already_landed(run):
    root, _, _, halt = run
    (root / "prod.py").write_text("x = 9\n", encoding="utf-8")
    _git(root, "add", "prod.py")
    _git(root, "commit", "-q", "-m", "checkpoint(wave 0): 1 rows" + chr(10)*2 + "Checkpoint-Plan: docs/plans/p.md")
    carrier = _git(root, "rev-parse", "HEAD")
    (root / "note.txt").write_text("halt\n", encoding="utf-8")
    _git(root, "add", "note.txt")
    _git(root, "commit", "-q", "-m", "halt record")
    halt_sha = _git(root, "rev-parse", "HEAD")
    reply = _gate(run, {**_resume(run, halt_sha), "plan": "docs/plans/p.md"})
    assert reply["already_landed"] is True and reply["committed"] is True
    assert reply["sha"] == carrier and "refused" not in reply
    unrelated = _gate(run, _resume(run, halt_sha))
    assert unrelated["refused"] == "ledger-unmet", "a carrier naming neither the plan nor the run is not this run's landing"


def test_product_carried_by_the_halt_commit_itself_is_still_refused(run):
    assert _gate(run, _resume(run))["refused"] == "ledger-unmet"
