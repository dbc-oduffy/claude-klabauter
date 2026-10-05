"""reverify persists the delivery-verifier's returned verdict into a provisioned sidecar and binds
the test-runner's sidecar to the plan; neither is left to the agent."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import reverify_delivery as rd
from coordinator_core.ops.review_mint.tests.test_execute_review import _STAGE_SCHEMAS, _v5_fragment

pytestmark = [pytest.mark.cadence]

_PLAN = "docs/plans/example.md"
_CLAIMS = [{"claim": "adds the frobnicator", "anchor": "no frob.py in diff"}]


def _script(**kw):
    return rd.compose_reverify_script(
        fragment=_v5_fragment(), stage_schemas=_STAGE_SCHEMAS, plan_path=_PLAN,
        run_record_rel="state/x/rec.md", plan_id="pln-1", run_base_sha="a" * 40,
        head_sha="b" * 40, claims=_CLAIMS, **kw,
    )


def test_verifier_brief_forbids_a_sidecar_write_and_names_no_sidecar_path():
    script = _script()
    assert "Write no sidecar" in script
    assert "sidecar_path" not in script


def test_ident_carries_plan_path_for_the_record_op():
    assert json.dumps(_PLAN) in _script()


def test_persist_writes_verdict_into_a_stamped_sidecar(tmp_path):
    rel = rd.persist_delivery_sidecar(
        repo_root=tmp_path, plan_path=_PLAN, session_id="sess-1", head_sha="c" * 40,
        verdict="FAIL", unbacked=_CLAIMS,
    )
    fm = rd._frontmatter(tmp_path / rel)
    assert fm["agent_type"] == "coordinator:delivery-verifier"
    assert fm["target_plan"] == _PLAN
    assert fm["verdict"] == "FAIL" and fm["claims_unbacked"] == _CLAIMS
    assert "# Delivery verdict: FAIL" in (tmp_path / rel).read_text(encoding="utf-8")
    assert rel.startswith(".coordinator-local/subagent-share/sess-1/example.delivery-reverify.")


def test_persist_is_idempotent_per_head_and_refuses_a_planless_result(tmp_path):
    kw = dict(repo_root=tmp_path, session_id="s", head_sha="d" * 40, verdict="PASS", unbacked=[])
    first = rd.persist_delivery_sidecar(plan_path=_PLAN, **kw)
    assert rd.persist_delivery_sidecar(plan_path=_PLAN, **kw) == first
    assert rd.persist_delivery_sidecar(plan_path=None, **kw) is None


_UNBOUND = "---\nstatus: complete\ntarget_plan: null\ncommits: []\n---\n\nbody\n"


def test_settle_binds_a_null_target_plan_and_missing_agent_type(tmp_path):
    sidecar = tmp_path / "tr.md"
    sidecar.write_text(_UNBOUND, encoding="utf-8", newline="\n")
    tests = {"status": "pass", "run": 3, "failed": 0, "sidecar": str(sidecar)}
    assert rd.settle_tests_sidecar(
        tmp_path, tests, plan_path=_PLAN, agent_type="coordinator:test-runner"
    )
    fm = rd._frontmatter(sidecar)
    assert fm["target_plan"] == _PLAN
    assert fm["agent_type"] == "coordinator:test-runner"
    assert fm["status"] == "complete" and fm["test_verdict"] == "pass"


def test_settle_never_overwrites_an_already_bound_sidecar(tmp_path):
    sidecar = tmp_path / "tr.md"
    sidecar.write_text(
        "---\nagent_type: coordinator:other\ntarget_plan: docs/plans/mine.md\ntest_verdict: pass\n---\n",
        encoding="utf-8", newline="\n",
    )
    tests = {"status": "pass", "run": 1, "failed": 0, "sidecar": str(sidecar)}
    assert not rd.settle_tests_sidecar(tmp_path, tests, plan_path=_PLAN, agent_type="coordinator:test-runner")
    fm = rd._frontmatter(sidecar)
    assert fm["agent_type"] == "coordinator:other" and fm["target_plan"] == "docs/plans/mine.md"


def test_record_cli_persists_and_binds_end_to_end(tmp_path):
    repo = tmp_path
    (repo / ".git").mkdir()
    record = repo / "rec.md"
    record.write_text("---\nkind: x\n---\n", encoding="utf-8")
    sidecar = repo / "tr.md"
    sidecar.write_text(_UNBOUND, encoding="utf-8", newline="\n")
    result = {
        "reverify_delivery": {
            "supersedes": "rec.md", "plan_id": "pln-1", "head_sha": "e" * 40,
            "plan_path": _PLAN, "tests_agent_type": "coordinator:test-runner",
        },
        "verdict": "PASS", "claims_unbacked": [],
        "tests": {"status": "pass", "run": 2, "failed": 0, "sidecar": str(sidecar)},
    }
    rc = rd.main([
        "record", "--run-record", str(record), "--repo-root", str(repo),
        "--session-id", "sess-2", "--result-json", json.dumps(result),
    ])
    assert rc == 0
    assert rd._frontmatter(sidecar)["target_plan"] == _PLAN
    assert rd._frontmatter(sidecar)["agent_type"] == "coordinator:test-runner"
    persisted = list((repo / ".coordinator-local" / "subagent-share" / "sess-2").glob("*.md"))
    assert len(persisted) == 1 and rd._frontmatter(persisted[0])["verdict"] == "PASS"
