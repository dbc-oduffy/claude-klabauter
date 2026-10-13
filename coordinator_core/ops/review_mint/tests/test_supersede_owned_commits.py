"""The range-prep counts only the plan's Deliverable-Id commits on a shared-branch range."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from coordinator_core.ops.review_mint.supersede import SupersedeRefused, record_superseding_review
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PLAN = "pln-x-123456"
DID = "del-own-1"


def _git(root, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root, capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()


def _commit(root, files, msg):
    for name in files:
        (root / name).write_text(msg)
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def shared_branch(tmp_path):
    _git(tmp_path, "init", "-q")
    base = _commit(tmp_path, ["base.txt"], "base")
    _commit(tmp_path, ["own1.txt", "own2.txt"], f"own a\n\nDeliverable-Id: {DID}")
    _commit(tmp_path, ["peer1.txt", "peer2.txt", "peer3.txt"], "peer work\n\nDeliverable-Id: del-peer")
    _commit(tmp_path, ["own2.txt", "own3.txt"], f"own b\n\nDeliverable-Id: {DID}")
    _commit(tmp_path, ["peer4.txt"], "untrailered peer")
    head = _git(tmp_path, "rev-parse", "HEAD")
    share = tmp_path / ".coordinator-local" / "subagent-share" / "sess-1"
    share.mkdir(parents=True)
    for name, fm in {
        "coordinator-code-reviewer.a1.md": {"agent_type": "coordinator:code-reviewer", "target_plan": PLAN},
        "coordinator-test-runner.t1.md": {
            "agent_type": "coordinator:test-runner", "target_plan": PLAN, "status": "pass", "run": 4, "failed": 0,
        },
        "coordinator-delivery-verifier.d1.md": {
            "agent_type": "coordinator:delivery-verifier", "plan": PLAN, "verdict": "PASS",
        },
    }.items():
        (share / name).write_text("---\n" + yaml.safe_dump(fm) + "---\nobserved\n", encoding="utf-8")
    return tmp_path, base, head


def _call(root, base, head, **kw):
    return record_superseding_review(
        repo_root=root, plan=PLAN, commit_range={"base": base, "head": head},
        wave_sidecar_paths=[], prep_sidecar=None, stage_returns=None,
        session_id="sess-1", from_share=True,
        judge_result={"status": "met", "observation": "green", "sidecar_path": ""}, **kw,
    )


def _product_files(root, res):
    prep = res["used"]["prep"][0]
    return yaml.safe_load((root / prep).read_text().split("---\n")[1])["product_files"]


def test_owned_deliverable_id_counts_only_owned_files(shared_branch):
    root, base, head = shared_branch
    res = _call(root, base, head, deliverable_id=DID)
    assert _product_files(root, res) == 3


def test_no_deliverable_id_is_refused_naming_the_field(shared_branch):
    root, base, head = shared_branch
    with pytest.raises(SupersedeRefused, match="no deliverable_id"):
        _call(root, base, head)


def test_deliverable_id_from_plan_file_frontmatter(shared_branch):
    root, base, head = shared_branch
    plan_file = root / "plan.md"
    plan_file.write_text(f"---\nplan_id: {PLAN}\ndeliverable_id: {DID}\n---\nbody\n", encoding="utf-8")
    res = record_superseding_review(
        repo_root=root, plan="plan.md", commit_range={"base": base, "head": head},
        wave_sidecar_paths=[], prep_sidecar=None, stage_returns=None,
        session_id="sess-1", from_share=True,
        judge_result={"status": "met", "observation": "green", "sidecar_path": ""},
    )
    assert _product_files(root, res) == 3


def test_deliverable_id_matching_no_commit_is_refused(shared_branch):
    root, base, head = shared_branch
    with pytest.raises(SupersedeRefused, match="carries Deliverable-Id del-none"):
        _call(root, base, head, deliverable_id="del-none")
