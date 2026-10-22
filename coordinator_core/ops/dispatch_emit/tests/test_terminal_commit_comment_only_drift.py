"""dispatch.terminal_commit re-lands once over a drift proven comment-only against the review's
pre-review snapshot, and still refuses any drift it cannot prove."""

from __future__ import annotations

import hashlib
import json
import shutil

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_refuses_blob_drift import (  # noqa: F401 -- fixtures
    BASE,
    SID,
    _WITH_BASE,
    _fire,
    _git,
    _record_at,
    _review,
    _sha,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PREFIX = f"manifest-run-1-{BASE[:12]}-prep"
_SIDECAR_REL = f".coordinator-local/subagent-share/{SID}/{_PREFIX}.review-prep.md"
_REVIEWED = {**_WITH_BASE, "prep_sidecar": _SIDECAR_REL}
_SOURCE = "# Reviewed-by: kira\nx = 1\n"


def _snapshot(repo, blobs: dict) -> None:
    snap = repo / ".coordinator-local" / "subagent-share" / SID / f"{_PREFIX}.pre-review"
    snap.mkdir(parents=True, exist_ok=True)
    files = {}
    for path, text in blobs.items():
        raw = hashlib.sha256(text.encode("utf-8")).hexdigest()
        (snap / raw).write_bytes(text.encode("utf-8"))
        files[path] = raw
    (snap / "manifest.json").write_text(json.dumps({"files": files}), encoding="utf-8")


def _reviewed(repo, reviewed: dict, current: dict) -> None:
    for path, text in current.items():
        (repo / path).write_text(text, encoding="utf-8", newline="")
    _review(repo, {p: _sha(t) for p, t in reviewed.items()}, 2000.0)
    _snapshot(repo, reviewed)


class _Counting:
    def __init__(self, monkeypatch):
        self.calls = 0
        real = terminal_commit.reentrant_dispatch

        def spy(op, params, **kw):
            if op == "ceremony.commit_v2":
                self.calls += 1
            return real(op, params, **kw)

        monkeypatch.setattr(terminal_commit, "reentrant_dispatch", spy)


def test_comment_only_edit_after_review_commits_with_trailer(repo, monkeypatch):
    _reviewed(repo, {"a.py": _SOURCE}, {"a.py": "x = 1\n"})
    out, _ = _fire(repo, monkeypatch, _REVIEWED)
    assert out["committed"] is True, out
    assert [d["path"] for d in out["comment_only_drift"]] == ["a.py"]
    message = _git(["log", "-1", "--format=%B"], repo)
    assert f"Comment-Only-Drift: a.py {_sha(_SOURCE)[:12]}->{_sha('x = 1' + chr(10))[:12]}" in message


def test_one_token_code_edit_refuses(repo, monkeypatch):
    _reviewed(repo, {"a.py": _SOURCE}, {"a.py": "# Reviewed-by: kira\nx = 2\n"})
    head = _git(["rev-parse", "HEAD"], repo)
    out, _ = _fire(repo, monkeypatch, _REVIEWED)
    assert out["committed"] is False
    assert out["refused"] == "blob-drift"
    assert "comment_only_drift" not in out
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_mixed_drift_refuses_and_remedy_names_only_the_real_path(repo, monkeypatch):
    reviewed = {"a.py": _SOURCE, "b.py": "y = 1\n"}
    _reviewed(repo, reviewed, {"a.py": "x = 1\n", "b.py": "y = 2\n"})
    _record_at(repo, "b.py", _sha("y = 1\n"), 1000.0)
    _record_at(repo, "a.py", _sha(_SOURCE), 1000.0)
    from coordinator_core.ops.dispatch_emit import commit_request as cr

    request = cr.CommitRequest(chunks=(cr.ChunkCommit(id="C4", title="t4", paths=("a.py", "b.py")),))
    (repo / "run.mjs").write_text("// s\n" + cr.render_marker(request) + "\n", encoding="utf-8", newline="")
    counter = _Counting(monkeypatch)
    out = _fire_two(repo, monkeypatch)
    assert out["refused"] == "blob-drift"
    assert [d["path"] for d in out["comment_only_drift"]] == ["a.py"]
    withhold_call = out["error"].split("Withhold call:", 1)[1]
    assert "b.py" in withhold_call
    assert "a.py" not in withhold_call
    assert counter.calls == 1


def _fire_two(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_token

    token = run_token.mint(repo, "run-1", "run.mjs")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED,
         "session_id": SID, "run_id": "run-1", "run_token": token},
        repo_root=repo / ".git",
    )


def test_deleted_snapshot_is_unprovable_and_refuses(repo, monkeypatch):
    _reviewed(repo, {"a.py": _SOURCE}, {"a.py": "x = 1\n"})
    shutil.rmtree(repo / ".coordinator-local" / "subagent-share" / SID / f"{_PREFIX}.pre-review")
    out, _ = _fire(repo, monkeypatch, _REVIEWED)
    assert out["refused"] == "blob-drift"
    assert "comment_only_drift" not in out


def test_commit_v2_is_called_twice_on_the_tolerated_path(repo, monkeypatch):
    _reviewed(repo, {"a.py": _SOURCE}, {"a.py": "x = 1\n"})
    counter = _Counting(monkeypatch)
    out, _ = _fire(repo, monkeypatch, _REVIEWED)
    assert out["committed"] is True, out
    assert counter.calls == 2


def test_commit_v2_is_called_once_on_real_drift(repo, monkeypatch):
    _reviewed(repo, {"a.py": _SOURCE}, {"a.py": "x = 3\n"})
    counter = _Counting(monkeypatch)
    out, _ = _fire(repo, monkeypatch, _REVIEWED)
    assert out["refused"] == "blob-drift"
    assert counter.calls == 1
