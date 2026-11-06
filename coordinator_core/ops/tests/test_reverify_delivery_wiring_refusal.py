"""record_delivery_verdict downgrades PASS to FAIL when the plan's `wiring:` refuses."""
from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import reverify_delivery as rd
from coordinator_core.ops.tests.test_review_stamp_superseding_record import _git, _record, _repo

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_WIRING = "wiring:\n  - id: reacher\n    status: not_built\n  - id: sink\n    status: blocked\n"


def _setup(tmp_path, wiring=_WIRING):
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    record = _record(repo, head, delivery={"verdict": "FAIL", "product_files": 1, "unbacked": []})
    plan = repo / "wired-plan.md"
    plan.write_text(f"---\ntitle: p\nstatus: executing\n{wiring}---\n\nbody\n", encoding="utf-8")
    return repo, record, record.relative_to(repo).as_posix(), head, plan


def _delivery(repo, rel):
    return rd._frontmatter(repo / rel)["delivery"]


def _record_pass(repo, rel, head, **kw):
    return rd.record_delivery_verdict(
        repo_root=repo, supersedes=rel, plan_id="pln-example-abc123", head_sha=head,
        verdict="PASS", unbacked=[], **kw,
    )


def test_pass_for_a_not_built_part_is_recorded_as_fail_with_per_part_entries(tmp_path):
    repo, _, rel, head, plan = _setup(tmp_path)
    out = _record_pass(repo, rel, head, plan_path=str(plan))
    delivery = _delivery(repo, out)
    assert delivery["verdict"] == "FAIL"
    assert delivery["unbacked"] == [
        {"part": "reacher", "status": "not_built", "reason": "declared"},
        {"part": "sink", "status": "blocked", "reason": "declared"},
    ]


def test_caller_unbacked_list_is_not_mutated(tmp_path):
    repo, _, rel, head, plan = _setup(tmp_path)
    mine = [{"claim": "c", "anchor": "a"}]
    rd.record_delivery_verdict(
        repo_root=repo, supersedes=rel, plan_id=None, head_sha=head, verdict="PASS",
        unbacked=mine, plan_path=str(plan),
    )
    assert mine == [{"claim": "c", "anchor": "a"}]


def test_all_wired_plan_keeps_pass(tmp_path):
    repo, _, rel, head, plan = _setup(tmp_path, wiring="")
    assert _delivery(repo, _record_pass(repo, rel, head, plan_path=str(plan)))["verdict"] == "PASS"


def test_absent_or_unresolvable_plan_path_keeps_pass(tmp_path):
    repo, _, rel, head, _ = _setup(tmp_path)
    assert _delivery(repo, _record_pass(repo, rel, head))["verdict"] == "PASS"
    assert _delivery(repo, _record_pass(repo, rel, head, plan_path="nope.md"))["verdict"] == "PASS"


def test_a_requested_fail_is_untouched(tmp_path):
    repo, _, rel, head, plan = _setup(tmp_path)
    out = rd.record_delivery_verdict(
        repo_root=repo, supersedes=rel, plan_id=None, head_sha=head, verdict="FAIL",
        unbacked=[{"claim": "c", "anchor": "a"}], plan_path=str(plan),
    )
    assert _delivery(repo, out)["unbacked"] == [{"claim": "c", "anchor": "a"}]


def test_cli_result_json_threads_the_plan_path(tmp_path, capsys):
    repo, record, rel, head, plan = _setup(tmp_path)
    result = {
        "verdict": "PASS", "claims_unbacked": [],
        "reverify_delivery": {"plan_id": "pln-example-abc123", "head_sha": head, "plan_path": str(plan)},
    }
    rc = rd.main(["record", "--run-record", rel, "--repo-root", str(repo), "--result-json", json.dumps(result)])
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()[-1]
    delivery = _delivery(repo, out)
    assert delivery["verdict"] == "FAIL"
    assert [u["part"] for u in delivery["unbacked"]] == ["reacher", "sink"]
