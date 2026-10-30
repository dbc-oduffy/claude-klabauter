"""A terminal commit re-reads the run's own record-named run-artifact paths instead of refusing
blob-drift on them; a product path, or a sizing the run's record does not name, still refuses."""

from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from coordinator_core.ops import warp_run_deliverable
from coordinator_core.ops.review_mint import wave_bookkeeping
from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SID = "11111111-2222-3333-4444-555555555555"
RUN = "run-1"
OWN = "state/sizings/own.yaml"
OTHER = "state/sizings/other.yaml"


def _git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write(repo, rel, text):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _fire(tmp_path, monkeypatch, *, drift: set) -> tuple:
    """Record a session write of every declared path at its seed bytes, rewrite the ``drift``
    paths on disk with no touch event, fire the terminal commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "t"], repo)
    declared = ["foo.py", OWN, OTHER]
    for rel in declared:
        _write(repo, rel, "seed\n")
    _write(repo, "README.md", "seed\n")
    _write(repo, "docs/plans/ex.md", "---\nplan_id: pln-ex-abc123\n---\n# Ex\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    run_dir = warp_run_deliverable.record_dir(repo, RUN)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({"sizing_rel": OWN}), encoding="utf-8")

    for i, rel in enumerate(declared):
        touch_record.append_event(
            touch_record.sink_path(repo / ".git" / "coordinator-sessions" / SID),
            session_id=SID, agent_id=None, verb="T", path=rel,
            content_hash=_sha("seed\n"), kind="w", timestamp=1.0 + i,
        )
    for rel in declared:
        _write(repo, rel, "drifted\n" if rel in drift else "seed\n")
    if "foo.py" not in drift:
        _write(repo, "foo.py", "seed\n# edit\n")
        touch_record.append_event(
            touch_record.sink_path(repo / ".git" / "coordinator-sessions" / SID),
            session_id=SID, agent_id=None, verb="T", path="foo.py",
            content_hash=_sha("seed\n# edit\n"), kind="w", timestamp=9.0,
        )
    for rel in (OWN, OTHER):
        if rel not in drift:
            _write(repo, rel, "seed\nown edit\n")
            touch_record.append_event(
                touch_record.sink_path(repo / ".git" / "coordinator-sessions" / SID),
                session_id=SID, agent_id=None, verb="T", path=rel,
                content_hash=_sha("seed\nown edit\n"), kind="w", timestamp=9.0,
            )

    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=tuple(declared)),),
        session_id=SID, plan_path="docs/plans/ex.md",
    )
    (repo / "run.mjs").write_text("// s\n" + render_marker(request) + "\n", encoding="utf-8", newline="")
    token = run_token.mint(repo, RUN, "run.mjs")

    outside = tmp_path / "record.md"
    outside.write_text("record\n", encoding="utf-8", newline="")
    monkeypatch.setattr(
        wave_bookkeeping, "bookkeep_wave",
        lambda *a, **k: {"sidecar_path": str(outside), "review_firings": [{"leg": "r"}]},
    )

    seen: list = []
    real = terminal_commit.reentrant_dispatch

    def spy(op, params, **kw):
        if op == "ceremony.commit_v2":
            seen.append(dict(params))
        return real(op, params, **kw)

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", spy)
    out = terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "session_id": SID, "run_id": RUN, "run_token": token,
         "inline_review": {
             "integration_stem": "rev-stem", "slices": 1, "fixes": 0, "prep_sidecar": "prep.md",
             "plan_id": "pln-ex-abc123", "wave_sidecar_paths": ["w.md"],
             "wave_reviews": reviewed()["wave_reviews"],
         }},
        repo_root=repo / ".git",
    )
    return out, seen


def test_a_rewrite_of_the_runs_own_sizing_is_reread_and_lands(tmp_path, monkeypatch):
    out, seen = _fire(tmp_path, monkeypatch, drift={OWN})
    assert out["committed"] is True, out
    assert OWN not in seen[0].get("expected_content_sha256", {})
    assert out["run_artifacts"]["reread"] == [OWN]


def test_reread_is_reported_beside_the_runs_landed_artifacts(tmp_path, monkeypatch):
    out, _ = _fire(tmp_path, monkeypatch, drift={OWN})
    assert out["committed"] is True, out
    assert out["run_artifacts"] == {
        "landed": [OWN, "state/warp/run-1/run.json"], "withheld": {}, "reread": [OWN],
    }, out


def test_reread_is_reported_when_the_run_declares_no_run_artifacts(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import halted_record_request

    monkeypatch.setattr(halted_record_request, "within_handoffs_home", lambda *a, **k: None)
    out, _ = _fire(tmp_path, monkeypatch, drift={OWN})
    assert out["committed"] is True, out
    assert out["run_artifacts"] == {"landed": [], "withheld": {}, "reread": [OWN]}, out


def test_a_product_path_drift_still_refuses(tmp_path, monkeypatch):
    out, _ = _fire(tmp_path, monkeypatch, drift={"foo.py", OWN})
    assert out["committed"] is False, out
    assert out["refused"] == "blob-drift"
    assert [d["path"] for d in out["blob_drift"]] == ["foo.py"]


def test_a_sizing_the_runs_record_does_not_name_keeps_its_expectation(tmp_path, monkeypatch):
    out, seen = _fire(tmp_path, monkeypatch, drift={OTHER})
    assert out["committed"] is False, out
    assert out["refused"] == "blob-drift"
    assert [d["path"] for d in out["blob_drift"]] == [OTHER]
