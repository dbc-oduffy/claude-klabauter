"""dispatch.terminal_commit: an executor writes a declared path, the review pass then edits the same
path, and the land succeeds because the expectation is the run's own last write; bytes nobody on the
run wrote after that still refuse blob-drift."""

from __future__ import annotations

import json

import pytest

from coordinator_core.session_hub_path import hub_for_common, session_dir_for
from coordinator_core.ops.dispatch_emit import ask_contract, kept_work, review_edits
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_refuses_blob_drift import (  # noqa: F401 -- repo is a fixture
    _WITH_BASE,
    BASE,
    SID,
    _fire,
    _git,
    _record_at,
    _review,
    _sha,
    repo,
)
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_revise_leg_not_drift import _agent
from coordinator_core.session import touch_record

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN_ID = "run-1"


def _executor_then_review_edit(repo, monkeypatch, *, final: str, review_in_ledger: bool = True,
                               review_named: bool = False) -> None:
    """The session read ``A`` (its pre-run ledger event), the executor wrote ``B`` (its own sink, plus
    the run's kept record), the review prep read ``B``, then the review pass edited it to ``C``."""
    monkeypatch.setattr(
        review_edits, "review_writes",
        lambda root, sid, since: {"a.py": "rv1"} if review_named else {},
    )
    (repo / "a.py").write_bytes(final.encode("utf-8"))
    _record_at(repo, "a.py", _sha("A\n"), 1000.0)
    _agent(repo, "ex1", [("a.py", _sha("B\n"), 1500.0)])
    record_dir = ask_contract.record_dir(repo, RUN_ID)
    assert kept_work.write(record_dir, {"C4": {"paths": {"a.py": kept_work.blob_sha(b"B\n")}}}, None) is not None
    _review(repo, {"a.py": _sha("B\n")}, 2000.0)
    if review_in_ledger:
        _agent(repo, "rv1", [("a.py", _sha("C\n"), 2500.0)], subagent_type="coordinator:code-reviewer")
    else:
        hub = hub_for_common(repo / ".git") / ".agents" / "rv1"
        hub.mkdir(parents=True, exist_ok=True)
        (hub / "em-session-id.txt").write_text(SID + "\n", encoding="utf-8")
        with (hub / touch_record.RECORD_FILENAME).open("ab") as fh:
            fh.write(touch_record.encode_line(
                session_id=SID, agent_id="rv1", verb=touch_record.VERB_TOUCH, path="a.py",
                timestamp=2500.0, content_hash=_sha("C\n"), kind="w",
            ))
    session_dir_for(repo / ".git", SID).mkdir(parents=True, exist_ok=True)


def test_a_review_pass_edit_after_the_executor_write_lands(repo, monkeypatch):
    _executor_then_review_edit(repo, monkeypatch, final="C\n")
    out, seen = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["committed"] is True, out
    assert "refused" not in out
    assert seen[-1]["expected_content_sha256"] == {"a.py": _sha("C\n")}


def test_a_review_pass_edit_named_by_the_review_lands(repo, monkeypatch):
    _executor_then_review_edit(repo, monkeypatch, final="C\n", review_named=True)
    out, seen = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["committed"] is True, out
    assert seen[-1]["expected_content_sha256"] == {"a.py": _sha("C\n")}


def test_a_review_edit_the_dispatch_ledger_never_listed_still_lands_when_the_review_names_it(repo, monkeypatch):
    _executor_then_review_edit(repo, monkeypatch, final="C\n", review_in_ledger=False, review_named=True)
    out, _ = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["committed"] is True, out


def test_a_foreign_write_after_the_review_pass_edit_still_refuses(repo, monkeypatch):
    _executor_then_review_edit(repo, monkeypatch, final="FOREIGN\n")
    head = _git(["rev-parse", "HEAD"], repo)
    out, _ = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["committed"] is False
    assert out["refused"] == "blob-drift"
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_the_executors_bytes_after_a_later_review_edit_are_still_the_runs_own(repo, monkeypatch):
    """The review pass's edit was reverted by nobody: bytes equal to the run's older write also land."""
    _executor_then_review_edit(repo, monkeypatch, final="B\n")
    out, _ = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["committed"] is True, out


def _measured(repo, text: str) -> None:
    record_dir = ask_contract.record_dir(repo, RUN_ID)
    record_dir.mkdir(parents=True, exist_ok=True)
    (record_dir / ask_contract.MEASURED_REV_RECORD).write_text(
        json.dumps({"base_sha": "0" * 40, "blobs": {"a.py": kept_work.blob_sha(text.encode("utf-8"))}}),
        encoding="utf-8",
    )


def test_no_ledger_at_all_the_measured_record_after_a_stale_kept_blob_owns_the_review_edit(repo, monkeypatch):
    """The shape of a workflow run whose executor and review-pass writes reach no touch ledger:
    ``kept.json`` holds the executor's first blob, ``measured-rev.json`` the later bytes."""
    monkeypatch.setattr(review_edits, "review_writes", lambda root, sid, since: {})
    (repo / "a.py").write_bytes(b"C\n")
    _record_at(repo, "a.py", _sha("A\n"), 1000.0)
    record_dir = ask_contract.record_dir(repo, RUN_ID)
    assert kept_work.write(record_dir, {"C4": {"paths": {"a.py": kept_work.blob_sha(b"B\n")}}}, None) is not None
    _measured(repo, "C\n")
    out, seen = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["committed"] is True, out
    assert seen[-1]["expected_content_sha256"] == {"a.py": _sha("C\n")}


def test_no_ledger_and_bytes_past_the_measured_record_still_refuse(repo, monkeypatch):
    monkeypatch.setattr(review_edits, "review_writes", lambda root, sid, since: {})
    (repo / "a.py").write_bytes(b"FOREIGN\n")
    _record_at(repo, "a.py", _sha("A\n"), 1000.0)
    _measured(repo, "C\n")
    out, _ = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["refused"] == "blob-drift"


def test_the_kept_record_alone_does_not_own_a_later_unrecorded_edit(repo, monkeypatch):
    _executor_then_review_edit(repo, monkeypatch, final="UNRECORDED\n")
    record = json.loads((ask_contract.record_dir(repo, RUN_ID) / "kept.json").read_text(encoding="utf-8"))
    assert record["chunks"]["C4"]["paths"]["a.py"] == kept_work.blob_sha(b"B\n")
    out, _ = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["refused"] == "blob-drift"


def _revise_note(repo, text: str = "Blob drift is this run's own earlier pass; move the expectation and land.") -> None:
    record_dir = ask_contract.record_dir(repo, RUN_ID)
    record_dir.mkdir(parents=True, exist_ok=True)
    with (record_dir / "notes.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"note": text, "at": "2026-11-09T14:34:00Z"}) + "\n")


def test_revise_note_then_refire_whose_review_read_the_new_bytes_lands(repo, monkeypatch):
    """A first pass left a kept blob and a ledger write; the revise leg rewrote the path with no
    ledger event; the refire's review pass read those bytes: the expectation is that read."""
    monkeypatch.setattr(review_edits, "review_writes", lambda root, sid, since: {})
    (repo / "a.py").write_bytes(b"D\n")
    _record_at(repo, "a.py", _sha("A\n"), 1000.0)
    record_dir = ask_contract.record_dir(repo, RUN_ID)
    assert kept_work.write(record_dir, {"C4": {"paths": {"a.py": kept_work.blob_sha(b"B\n")}}}, None) is not None
    _measured(repo, "B\n")
    _revise_note(repo)
    _review(repo, {"a.py": _sha("D\n")}, 3000.0)
    out, seen = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["committed"] is True, out
    assert seen[-1]["expected_content_sha256"] == {"a.py": _sha("D\n")}


def test_revise_note_does_not_own_bytes_nothing_on_the_run_recorded(repo, monkeypatch):
    monkeypatch.setattr(review_edits, "review_writes", lambda root, sid, since: {})
    (repo / "a.py").write_bytes(b"FOREIGN\n")
    _record_at(repo, "a.py", _sha("A\n"), 1000.0)
    _measured(repo, "B\n")
    _revise_note(repo)
    _review(repo, {"a.py": _sha("D\n")}, 3000.0)
    out, _ = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["refused"] == "blob-drift"


def test_a_stale_commit_prep_read_does_not_mask_the_run_keyed_review_read(repo, monkeypatch):
    """The commit's own prep lookup returned reviewed bytes that predate the drifted bytes; the
    run-keyed stored review read them: own_drift_actuals credits both."""
    from coordinator_core.ops.dispatch_emit.own_drift import _own_drift_actuals

    _review(repo, {"a.py": _sha("D\n")}, 3000.0)
    share = repo / ".coordinator-local" / "subagent-share" / SID
    (share / "ptr-run-run-1.review-prep-ptr.json").write_text(
        json.dumps({"sidecar": f"manifest-run-1-{BASE[:12]}-prep.review-prep.md", "ambiguous": False}),
        encoding="utf-8",
    )
    drift = [{"path": "a.py", "basis": "content-sha256", "expected": _sha("A\n"), "actual": _sha("D\n")}]
    got = _own_drift_actuals(
        repo, None, drift, ["a.py"], SID, {}, {"a.py": _sha("OLD\n")}, None, RUN_ID,
    )
    assert got == {"a.py": _sha("D\n")}
    drift[0]["actual"] = _sha("FOREIGN\n")
    assert _own_drift_actuals(
        repo, None, drift, ["a.py"], SID, {}, {"a.py": _sha("OLD\n")}, None, RUN_ID,
    ) == {}
