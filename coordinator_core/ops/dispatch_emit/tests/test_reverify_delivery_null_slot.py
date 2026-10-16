"""`--reverify-delivery` accepts a run record whose delivery slot is null and binds the record to the terminal commit."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import reverify_delivery as rd
from coordinator_core.ops.dispatch_emit.verdict_supersession import latest_delivery_binding
from coordinator_core.ops.tests.test_review_stamp_superseding_record import _git, _record, _repo

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_TERMINAL = "c" * 40


def _null_run(tmp_path, **overrides):
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    record = _record(repo, head, delivery=None, **overrides)
    return repo, record, record.relative_to(repo).as_posix()


def _plan_file(repo):
    plan = repo / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("---\nplan_id: pln-example-abc123\nstatus: executing\n---\nbody\n", encoding="utf-8")
    return plan


def test_null_slot_is_not_refused_and_has_no_claims(tmp_path):
    _, record, _ = _null_run(tmp_path)
    assert rd.prior_unbacked_claims(record)[1] == []


def test_verdictless_delivery_block_is_a_null_slot(tmp_path):
    repo = _repo(tmp_path)
    record = _record(repo, _git(repo, "rev-parse", "HEAD"), delivery={"verdict": None})
    assert rd.prior_unbacked_claims(record)[1] == []


def test_record_without_a_review_still_refuses(tmp_path):
    repo = _repo(tmp_path)
    record = _record(repo, _git(repo, "rev-parse", "HEAD"), delivery=None, prep=None)
    with pytest.raises(rd.ReverifyRefused, match="carries no delivery FAIL"):
        rd.prior_unbacked_claims(record)


@pytest.mark.usefixtures("verifier_roster")
def test_emit_binds_diff_sha_in_ident_and_briefs_the_reviewed_diff(tmp_path, monkeypatch):
    repo, record, rel = _null_run(tmp_path)
    plan = _plan_file(repo)
    monkeypatch.setattr(rd, "_head_sha", lambda root: "b" * 40)
    monkeypatch.setattr(rd, "_terminal_of", lambda root, pid, pp: (_TERMINAL, record))
    out = repo / "docs" / "plans" / "p.reverify.workflow.mjs"
    result = rd.emit_reverify(repo_root=repo, plan_path=str(plan), run_record=str(record), out_path=str(out))
    assert result["claims"] == 0 and result["supersedes"] == rel
    text = out.read_text(encoding="utf-8")
    assert f'"diff_sha": "{_TERMINAL}"' in text
    assert f"..{_TERMINAL}" in text


def test_emit_refuses_a_null_slot_when_no_terminal_commit_resolves(tmp_path, monkeypatch):
    repo, record, _ = _null_run(tmp_path)
    plan = _plan_file(repo)
    monkeypatch.setattr(rd, "_head_sha", lambda root: "b" * 40)
    monkeypatch.setattr(rd, "_terminal_of", lambda root, pid, pp: None)
    out = repo / "docs" / "plans" / "p.reverify.workflow.mjs"
    with pytest.raises(rd.ReverifyRefused, match="no Inline-Review terminal commit"):
        rd.emit_reverify(repo_root=repo, plan_path=str(plan), run_record=str(record), out_path=str(out))
    assert not out.exists()


def test_fail_record_emits_no_diff_sha(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    fail = {"verdict": "FAIL", "unbacked": [{"claim": "c", "anchor": "a"}]}
    record = _record(repo, _git(repo, "rev-parse", "HEAD"), delivery=fail)
    plan = _plan_file(repo)
    monkeypatch.setattr(rd, "_head_sha", lambda root: "b" * 40)
    out = repo / "docs" / "plans" / "p.reverify.workflow.mjs"
    rd.emit_reverify(repo_root=repo, plan_path=str(plan), run_record=str(record), out_path=str(out))
    assert "diff_sha" not in out.read_text(encoding="utf-8")


def test_recorded_verdict_carries_diff_sha_and_the_binding_reader_returns_it(tmp_path):
    repo, record, rel = _null_run(tmp_path)
    path = rd.record_delivery_verdict(
        repo_root=repo, supersedes=rel, plan_id="pln-example-abc123", head_sha="b" * 40,
        verdict="PASS", unbacked=[], diff_sha=_TERMINAL,
    )
    binding = latest_delivery_binding(repo, rel)
    assert binding["diff_sha"] == _TERMINAL and binding["record_path"] == path


def test_record_cli_passes_the_ident_diff_sha_through(tmp_path, capsys):
    repo, record, rel = _null_run(tmp_path)
    result = {
        "reverify_delivery": {"head_sha": "b" * 40, "plan_id": "pln-example-abc123", "diff_sha": _TERMINAL},
        "verdict": "PASS", "claims_unbacked": [],
    }
    assert rd.main(["record", "--run-record", str(record), "--repo-root", str(repo),
                    "--result-json", json.dumps(result)]) == 0
    assert latest_delivery_binding(repo, rel)["diff_sha"] == _TERMINAL


def test_record_without_diff_sha_omits_the_key(tmp_path):
    repo, _, rel = _null_run(tmp_path)
    rd.record_delivery_verdict(
        repo_root=repo, supersedes=rel, plan_id="pln-example-abc123", head_sha="b" * 40,
        verdict="PASS", unbacked=[],
    )
    assert latest_delivery_binding(repo, rel)["diff_sha"] is None
