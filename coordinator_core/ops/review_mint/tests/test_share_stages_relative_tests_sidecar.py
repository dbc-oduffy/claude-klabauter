"""A repo-relative reverify tests.sidecar is resolved against repo_root before stage identity."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.review_mint import share_stages
from coordinator_core.ops.review_mint.share_stages import assemble_from_share

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PLAN = "pln-x-123456"
SHARE_REL = ".coordinator-local/subagent-share/sess-1"
RUNNER_NAME = "coordinator-review-test-runner.t1.md"


def _sc(share, name, **fm):
    share.mkdir(parents=True, exist_ok=True)
    (share / name).write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\nbody\n", encoding="utf-8")


def _build(root, monkeypatch, sidecar_value):
    monkeypatch.setattr(share_stages, "_delivery_verifier_rostered", lambda _root: True)
    share = root / SHARE_REL
    _sc(share, "coordinator-code-reviewer.a1.md", agent_type="coordinator:code-reviewer", target_plan=PLAN)
    _sc(share, "coordinator-test-runner.p1.md", agent_type="coordinator:test-runner",
        target_plan=PLAN, status="open", run_base_sha="abc1234", product_files=2)
    _sc(share, "coordinator-delivery-verifier.d1.md", agent_type="coordinator:delivery-verifier",
        plan=PLAN, verdict="PASS")
    # Bound, review-lens-shaped, but carries no pass/fail/not_run: only the reverify record supplies tests.
    _sc(share, RUNNER_NAME, agent_type="coordinator:review-test-runner", target_plan=PLAN, status="complete")
    record = {"tests": {"status": "pass", "run": 3, "failed": 0, "sidecar": sidecar_value},
              "delivery": {"verdict": "PASS"}, "supersedes": "unused.md"}
    monkeypatch.setattr(share_stages, "_reverify_record", lambda *_a, **_k: record)


def _assemble(root):
    return assemble_from_share(
        repo_root=root, session_id="sess-1", plan_id=PLAN, plan_stem=None,
        head="HEAD", judge_result={"status": "met", "observation": "holds"},
    )


def test_relative_tests_sidecar_is_the_tests_stage_and_not_a_reviewer_wave(tmp_path, monkeypatch):
    _build(tmp_path, monkeypatch, f"{SHARE_REL}/{RUNNER_NAME}")
    res = _assemble(tmp_path)
    assert res["used"]["tests"] == [f"{SHARE_REL}/{RUNNER_NAME}"]
    waves = [p.name for p in res["wave_sidecar_paths"]]
    assert RUNNER_NAME not in waves
    assert waves == ["coordinator-code-reviewer.a1.md"]


def test_unrelated_tests_sidecar_leaves_the_runner_a_reviewer_wave(tmp_path, monkeypatch):
    _build(tmp_path, monkeypatch, f"{SHARE_REL}/other.md")
    waves = [p.name for p in _assemble(tmp_path)["wave_sidecar_paths"]]
    assert RUNNER_NAME in waves
