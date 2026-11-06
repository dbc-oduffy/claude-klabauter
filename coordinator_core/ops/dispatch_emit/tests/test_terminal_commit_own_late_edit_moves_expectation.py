"""dispatch.terminal_commit moves the blob-drift expectation onto bytes the run's own record dir
holds (``kept.json`` / ``measured-rev.json``) when no touch ledger carries the writer's hash, as an
executor's Write/Edit does not; bytes matching none of the run's records still refuse. It also
credits bytes the run's stored review prep read, found by run id when the prep does not cover a
declared note."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract, drift_attribution, kept_work, terminal_commit
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_refuses_blob_drift import (  # noqa: F401 -- fixtures
    SID,
    _fire,
    _git,
    _record_at,
    _sha,
    repo,
)
from coordinator_core.session import touch_record

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN_ID = "run-1"


def _read_event(repo, rel: str, digest: str, at: float) -> None:
    """The expectation the session's ledger holds: a read of the pre-edit bytes."""
    sink = touch_record.sink_path(repo / ".git" / "coordinator-sessions" / SID)
    touch_record.append_event(
        sink, session_id=SID, agent_id=None, verb="T", path=rel, content_hash=digest, kind="r", timestamp=at,
    )


def _keep(repo, rel: str, text: str) -> None:
    record_dir = ask_contract.record_dir(repo, RUN_ID)
    blob = kept_work.blob_sha(text.encode("utf-8"))
    assert kept_work.write(record_dir, {"C4": {"paths": {rel: blob}}}, None) is not None


def test_the_runs_own_unledgered_late_edit_lands(repo, monkeypatch):
    (repo / "a.py").write_text("B\n", encoding="utf-8", newline="")
    _read_event(repo, "a.py", _sha("A\n"), 1000.0)
    _keep(repo, "a.py", "B\n")
    out, seen = _fire(repo, monkeypatch)
    assert out["committed"] is True, out
    assert [s["expected_content_sha256"] for s in seen] == [{"a.py": _sha("A\n")}, {"a.py": _sha("B\n")}]


def test_the_measured_rev_blob_owns_the_late_edit_too(repo, monkeypatch):
    (repo / "a.py").write_text("B\n", encoding="utf-8", newline="")
    _read_event(repo, "a.py", _sha("A\n"), 1000.0)
    record_dir = ask_contract.record_dir(repo, RUN_ID)
    record_dir.mkdir(parents=True, exist_ok=True)
    (record_dir / ask_contract.MEASURED_REV_RECORD).write_text(
        json.dumps({"blobs": {"a.py": kept_work.blob_sha(b"B\n")}}), encoding="utf-8"
    )
    out, _ = _fire(repo, monkeypatch)
    assert out["committed"] is True, out


def test_a_different_sessions_rewrite_of_the_kept_path_still_refuses(repo, monkeypatch):
    (repo / "a.py").write_text("FOREIGN\n", encoding="utf-8", newline="")
    _read_event(repo, "a.py", _sha("A\n"), 1000.0)
    _keep(repo, "a.py", "B\n")
    head = _git(["rev-parse", "HEAD"], repo)
    out, seen = _fire(repo, monkeypatch)
    assert out["committed"] is False
    assert out["refused"] == "blob-drift"
    assert out["blob_drift"][0]["path"] == "a.py"
    assert len(seen) == 1
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_a_kept_blob_for_another_path_owns_nothing(repo, monkeypatch):
    (repo / "a.py").write_text("B\n", encoding="utf-8", newline="")
    _read_event(repo, "a.py", _sha("A\n"), 1000.0)
    _keep(repo, "other.py", "B\n")
    out, _ = _fire(repo, monkeypatch)
    assert out["refused"] == "blob-drift"


def test_run_recorded_blobs_reads_only_the_asked_paths(tmp_path):
    kept_work.write(
        tmp_path, {"C1": {"paths": {"a.py": "b" * 40, "z.py": "c" * 40}}}, None, {"a.py": "d" * 40},
    )
    assert drift_attribution.run_recorded_blobs(tmp_path, ["./a.py"]) == {"a.py": ["b" * 40, "d" * 40]}
    assert drift_attribution.run_recorded_blobs(None, ["a.py"]) == {}
    assert drift_attribution.run_recorded_blobs(tmp_path / "missing", ["a.py"]) == {}


def _fire_with_note(repo, monkeypatch) -> tuple[dict, list]:
    """The commit declares a delivery note beside ``a.py``; the stored review prep reads only ``a.py``."""
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
    from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed

    request = CommitRequest(chunks=(
        ChunkCommit(id="C4", title="t4", paths=("a.py",)),
        ChunkCommit(id="docs", title="note", paths=("note.md",)),
    ))
    (repo / "note.md").write_text("note\n", encoding="utf-8", newline="")
    (repo / "run.mjs").write_text("// s\n" + render_marker(request) + "\n", encoding="utf-8", newline="")
    token = run_token.mint(repo, RUN_ID, "run.mjs")
    seen: list = []
    real = terminal_commit.reentrant_dispatch

    def spy(op, params, **kw):
        if op == "ceremony.commit_v2":
            seen.append(dict(params))
        return real(op, params, **kw)

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", spy)
    out = terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": reviewed(),
         "session_id": SID, "run_id": RUN_ID, "run_token": token},
        repo_root=repo / ".git",
    )
    return out, seen


def _store_review_pointer(repo, reviewed: dict) -> None:
    from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_refuses_blob_drift import (
        BASE,
        _review,
    )

    _review(repo, reviewed, 2000.0)
    share = repo / ".coordinator-local" / "subagent-share" / SID
    (share / f"ptr-run-{RUN_ID}.review-prep-ptr.json").write_text(
        json.dumps({"sidecar": f"manifest-run-1-{BASE[:12]}-prep.review-prep.md", "ambiguous": False}),
        encoding="utf-8",
    )


def test_a_late_leg_edit_the_review_read_lands_though_the_prep_never_covered_a_declared_note(repo, monkeypatch):
    (repo / "a.py").write_text("B\n", encoding="utf-8", newline="")
    _read_event(repo, "a.py", _sha("A\n"), 1000.0)
    _store_review_pointer(repo, {"a.py": _sha("B\n")})
    out, seen = _fire_with_note(repo, monkeypatch)
    assert out["committed"] is True, out
    assert seen[-1]["expected_content_sha256"]["a.py"] == _sha("B\n")


def test_a_foreign_rewrite_after_that_review_still_refuses(repo, monkeypatch):
    (repo / "a.py").write_text("FOREIGN\n", encoding="utf-8", newline="")
    _read_event(repo, "a.py", _sha("A\n"), 1000.0)
    _store_review_pointer(repo, {"a.py": _sha("B\n")})
    head = _git(["rev-parse", "HEAD"], repo)
    out, _ = _fire_with_note(repo, monkeypatch)
    assert out["committed"] is False
    assert out["refused"] == "blob-drift"
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_run_reviewed_hashes_needs_a_run_and_a_stored_reply(repo):
    assert drift_attribution.run_reviewed_hashes(repo, SID, RUN_ID) == {}
    assert drift_attribution.run_reviewed_hashes(repo, SID, None) == {}
    _store_review_pointer(repo, {"./a.py": _sha("B\n")})
    assert drift_attribution.run_reviewed_hashes(repo, SID, RUN_ID) == {"a.py": _sha("B\n")}
    assert drift_attribution.run_reviewed_hashes(repo, SID, "other-run") == {}


def test_own_drift_actuals_without_a_record_dir_is_unchanged(repo):
    (repo / "a.py").write_text("B\n", encoding="utf-8", newline="")
    _record_at(repo, "a.py", _sha("B\n"), 1000.0)
    drift = [{"path": "a.py", "basis": "content-sha256", "expected": _sha("A\n"), "actual": _sha("B\n")}]
    assert terminal_commit._own_drift_actuals(repo, None, drift, ["a.py"], SID, None) == {"a.py": _sha("B\n")}
