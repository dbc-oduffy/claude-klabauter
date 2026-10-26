"""P-C replays: workflow.revise replies a MutationReply (non-empty `applied`) or `ok:false` naming what it refused,
and never warns that a plan omits a row when that plan is an unfilled scaffold the run did not author."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.pipeline_contract import RUN_DIR_ROOT as PIPE_DIR
from coordinator_core.ops.dispatch_emit.pipeline_run import REVISIONS_MARKER
from coordinator_core.ops.dispatch_emit.run_truth_contract import MUTATION_REPLY_FIELDS

SIZING_REL = "state/sizings/2026-10-25-fixture.yaml"
PLAN_REL = "docs/plans/2026-10-25-fixture.md"
PIPE_RID = "pipeline-research-20261024T000000-abc123"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _stale_plan_repo(repo, plan_body):
    rows = [{"id": f"R{i}", "status": "open", "source_text": f"words {i}", "surface": "internal"} for i in (1, 2, 9)]
    sizing = {"plan": PLAN_REL, "requirement_register": {"sources": [{"doc": "ask", "ref": "ask"}], "rows": rows}}
    (repo / "state/sizings").mkdir(parents=True)
    (repo / "docs/plans").mkdir(parents=True)
    (repo / SIZING_REL).write_text(yaml.safe_dump(sizing), encoding="utf-8", newline="\n")
    claims = f"---\nregister_claims:\n  sizing: {SIZING_REL}\n  rows: [R1, R2]\n---\n"
    (repo / PLAN_REL).write_text(claims + plan_body, encoding="utf-8", newline="\n")


def test_reply_carries_every_mutation_reply_field_and_ok_follows_applied(repo):
    out = rl._mutation_reply(("row Q1",), (), "run-1", extra=1)
    assert all(f in out for f in MUTATION_REPLY_FIELDS) and out["ok"] is True and out["extra"] == 1
    refusal = rl._mutation_reply((), ({"row": "Q1", "reason": "x"},), "run-1")
    assert refusal["ok"] is False and refusal["applied"] == [] and "nothing applied" in refusal["error"]


def test_research_pipeline_delivery_names_the_revisions_file(repo):
    d = repo / PIPE_DIR
    d.mkdir(parents=True)
    (d / f"{PIPE_RID}.workflow.mjs").write_text(f"// {REVISIONS_MARKER}\n", encoding="utf-8")
    (d / f"{PIPE_RID}.workflow.mjs.emitted.json").write_text("{}", encoding="utf-8")
    out = rl._handler({"run_id": PIPE_RID, "note": "narrow to cell A"}, repo_root=repo)
    assert out["ok"] is True and out["delivered"] is True
    assert out["applied"] == [f"revision appended to {out['revisions']}"] and out["target"] == PIPE_RID


def test_plan_omits_warning_is_not_issued_against_an_unfilled_scaffold_plan(repo):
    _stale_plan_repo(repo, "# Plan\n\nPLACEHOLDER\n")
    assert rl._unclaimed_rows(repo, SIZING_REL) == (None, [])


def _emitted_run(repo, run_id="ask-20261025T000000-aaaaaa"):
    rl.start(repo, run_id)
    return run_id


def test_owned_red_is_recorded_and_the_reply_names_it(repo):
    run_id = _emitted_run(repo)
    node = "coordinator_core/tests/test_x.py::test_vendored_schema_version_parity[review-findings]"
    out = rl._handler({"run_id": run_id, "owned_red": [{"nodeid": node, "owner_run_id": "25583b"}]}, repo_root=repo)
    assert out["ok"] is True and "owned_red" in out["applied"]
    assert out["owned_red"][0]["nodeid"] == node and out["owned_red"][0]["owner_run_id"] == "25583b"
    recorded = (rl.run_dir(repo, run_id) / rl.OWNED_RED_FILE).read_text(encoding="utf-8")
    assert node in recorded and "25583b" in recorded


@pytest.mark.parametrize(
    "bad", [[{"nodeid": "not-a-node", "owner_run_id": "25583b"}], [{"nodeid": "a.py::t", "owner_run_id": " "}], ["a.py::t"]]
)
def test_owned_red_that_cannot_be_consumed_is_refused_and_records_nothing(repo, bad):
    run_id = _emitted_run(repo)
    out = rl._handler({"run_id": run_id, "owned_red": bad}, repo_root=repo)
    assert "error" in out and out.get("ok") is not True
    assert not (rl.run_dir(repo, run_id) / rl.OWNED_RED_FILE).exists()


def test_plan_omits_warning_still_names_rows_for_a_filled_plan(repo):
    _stale_plan_repo(repo, "# Plan\n\nReal body.\n")
    assert rl._unclaimed_rows(repo, SIZING_REL) == (PLAN_REL, ["R9"])
