"""`--reverify-delivery ""` takes the reverify route and resolves the record from the plan's plan_id."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import reverify_delivery as rd
from coordinator_core.ops.tests.test_review_stamp_superseding_record import _git, _record, _repo

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLAN_ID = "pln-example-abc123"
_FAIL = {
    "verdict": "FAIL",
    "product_files": 1,
    "unbacked": [{"claim": "adds the frobnicator", "anchor": "no frob.py in diff"}],
}


def _plan(repo):
    plan = repo / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(f"---\nplan_id: {_PLAN_ID}\nstatus: executing\n---\nbody\n", encoding="utf-8")
    return plan


def _argv(repo, plan, out):
    return [
        "--repo-root", str(repo), "--plan", str(plan), "--out", str(out),
        "--reverify-delivery", "",
    ]


def test_empty_value_without_a_bookkeeping_record_refuses_naming_the_glob(tmp_path, capsys):
    repo = _repo(tmp_path)
    plan = _plan(repo)
    out = repo / "docs" / "plans" / "p.reverify.workflow.mjs"
    rc = cli_module.main(_argv(repo, plan, out))
    err = capsys.readouterr().err
    assert rc != cli_module.EXIT_OK
    assert not out.exists()
    assert not (repo / "docs" / "plans" / "p.workflow.mjs").exists()
    assert f"plan_id: {_PLAN_ID}" in err


def test_empty_value_with_a_bookkeeping_record_emits_the_reverify_script(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    record = _record(repo, _git(repo, "rev-parse", "HEAD"), delivery=_FAIL)
    share = repo / ".coordinator-local" / "subagent-share" / "sid-1"
    share.mkdir(parents=True)
    (share / f"{_PLAN_ID}.review-wave-bookkeeping.md").write_bytes(record.read_bytes())
    plan = _plan(repo)
    monkeypatch.setattr(rd, "_head_sha", lambda root: "b" * 40)
    out = repo / "docs" / "plans" / "p.reverify.workflow.mjs"
    rc = cli_module.main(_argv(repo, plan, out))
    assert rc == cli_module.EXIT_OK, capsys.readouterr().err
    text = out.read_text(encoding="utf-8")
    assert f"supersedes: .coordinator-local/subagent-share/sid-1/{_PLAN_ID}.review-wave-bookkeeping.md" in text


def test_omitted_out_defaults_beside_the_plan(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    record = _record(repo, _git(repo, "rev-parse", "HEAD"), delivery=_FAIL)
    share = repo / ".coordinator-local" / "subagent-share" / "sid-1"
    share.mkdir(parents=True)
    (share / f"{_PLAN_ID}.review-wave-bookkeeping.md").write_bytes(record.read_bytes())
    plan = _plan(repo)
    monkeypatch.setattr(rd, "_head_sha", lambda root: "b" * 40)
    argv = ["--repo-root", str(repo), "--plan", str(plan), "--reverify-delivery", ""]
    rc = cli_module.main(argv)
    assert rc == cli_module.EXIT_OK, capsys.readouterr().err
    assert (repo / "docs" / "plans" / "p.workflow.mjs").exists()


def test_reverify_without_a_plan_names_the_out_default(capsys):
    rc = cli_module.main(["--reverify-delivery", ""])
    assert rc == cli_module.EXIT_USAGE
    assert "--out defaults to" in capsys.readouterr().err


def test_completion_receipt_path_resolves_from_the_plan(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    record = _record(repo, _git(repo, "rev-parse", "HEAD"), delivery=_FAIL)
    share = repo / ".coordinator-local" / "subagent-share" / "sid-1"
    share.mkdir(parents=True)
    (share / f"{_PLAN_ID}.review-wave-bookkeeping.md").write_bytes(record.read_bytes())
    receipt = repo / "state" / "completion-receipts" / "r.md"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(
        f"---\nschema: completion-receipt\nplan_id: {_PLAN_ID}\nverdict: null\n---\n", encoding="utf-8"
    )
    plan = _plan(repo)
    monkeypatch.setattr(rd, "_head_sha", lambda root: "b" * 40)
    out = repo / "docs" / "plans" / "p.reverify.workflow.mjs"
    argv = _argv(repo, plan, out)
    argv[-1] = str(receipt)
    rc = cli_module.main(argv)
    assert rc == cli_module.EXIT_OK, capsys.readouterr().err
    assert "review-wave-bookkeeping.md" in out.read_text(encoding="utf-8")
