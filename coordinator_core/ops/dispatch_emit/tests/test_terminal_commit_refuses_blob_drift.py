"""dispatch.terminal_commit's commit leg declares, per path, the newer of the run session's last
recorded write and the run's newest review to ``ceremony.commit_v2`` (the delivery note: its own
post-write bytes) and refuses a path a peer rewrote after that."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.ops.dispatch_emit.tests import test_ask_run_product_commit as ask
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import repo as ask_repo  # noqa: F401 -- fixture
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SID = "c4e53fa2-2bda-51b1-833a-cf629f2b0cb6"
REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
_TOKENS: dict = {}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _script(repo: Path) -> str:
    request = CommitRequest(chunks=(ChunkCommit(id="C4", title="t4", paths=("a.py",)),))
    (repo / "run.mjs").write_text("// s\n" + render_marker(request) + "\n", encoding="utf-8")
    return "run.mjs"


def _record(repo: Path, rel: str, digest) -> None:
    sink = touch_record.sink_path(repo / ".git" / "coordinator-sessions" / SID)
    touch_record.append_event(
        sink, session_id=SID, agent_id=None, verb="T", path=rel, content_hash=digest, kind="w",
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


BASE = "a" * 40


def _review(repo: Path, reviewed: dict, at: float) -> None:
    """Store the run's review.prep reply the way review_prep does: a sidecar under the session's
    share dir naming the bytes it read (``reviewed_sha256``) at ``recorded_at``."""
    share = repo / ".coordinator-local" / "subagent-share" / SID
    share.mkdir(parents=True, exist_ok=True)
    fm = {
        "agent_type": "engine:prep-freeze", "plan_id": "p", "share_dir": "x", "run_base_sha": BASE,
        "verdict": "OK", "basis": "worktree", "whole_diff_path": "d", "product_files": len(reviewed),
        "foreign_claims": [], "slices": [{"id": "s", "files": sorted(reviewed)}],
        "whole_diff_sidecars": {}, "sidecar_path": "x", "head": None, "paths": sorted(reviewed),
        "recorded_at": datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "reviewed_sha256": reviewed,
    }
    (share / f"manifest-run-1-{BASE[:12]}-prep.review-prep.md").write_text(
        f"---\n{yaml.safe_dump(fm)}---\n\nbody\n", encoding="utf-8"
    )


def _record_at(repo: Path, rel: str, digest, at: float) -> None:
    sink = touch_record.sink_path(repo / ".git" / "coordinator-sessions" / SID)
    touch_record.append_event(
        sink, session_id=SID, agent_id=None, verb="T", path=rel, content_hash=digest, kind="w",
        timestamp=at,
    )


def _fire(repo: Path, monkeypatch, inline_review: dict = REVIEWED) -> tuple[dict, list]:
    from coordinator_core.ops.dispatch_emit import run_token

    script = _script(repo)
    token = _TOKENS.get(repo) or _TOKENS.setdefault(repo, run_token.mint(repo, "run-1", script))
    seen: list = []
    real = terminal_commit.reentrant_dispatch

    def spy(op, params, **kw):
        if op == "ceremony.commit_v2":
            seen.append(dict(params))
        return real(op, params, **kw)

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", spy)
    out = terminal_commit._handler(
        {"script_path": script, "incomplete_chunks": [], "inline_review": inline_review,
         "session_id": SID, "run_id": "run-1", "run_token": token},
        repo_root=repo / ".git",
    )
    return out, seen


def test_drift_since_the_runs_last_recorded_write_refuses(repo, monkeypatch):
    (repo / "a.py").write_text("B\n", encoding="utf-8")
    _record(repo, "a.py", _sha("A\n"))
    head = _git(["rev-parse", "HEAD"], repo)

    out, seen = _fire(repo, monkeypatch)

    assert out["committed"] is False
    assert out["refused"] == "blob-drift"
    assert out["blob_drift"][0]["path"] == "a.py"
    assert _git(["rev-parse", "HEAD"], repo) == head
    assert not out.get("receipts")
    assert seen[0]["expected_content_sha256"] == {"a.py": _sha("A\n")}


def test_a_refused_run_leaves_no_receipts_and_can_refire(repo, monkeypatch):
    (repo / "a.py").write_text("B\n", encoding="utf-8")
    _record(repo, "a.py", _sha("A\n"))
    out, _ = _fire(repo, monkeypatch)
    assert out["committed"] is False

    (repo / "a.py").write_text("A\n", encoding="utf-8")
    out, _ = _fire(repo, monkeypatch)
    assert out["committed"] is True, out


def test_matching_bytes_commit(repo, monkeypatch):
    (repo / "a.py").write_text("A\n", encoding="utf-8")
    _record(repo, "a.py", _sha("A\n"))
    out, seen = _fire(repo, monkeypatch)
    assert out["committed"] is True, out
    assert seen[0]["expected_content_sha256"] == {"a.py": _sha("A\n")}


def test_a_hashless_newest_event_carries_no_expectation(repo, monkeypatch):
    (repo / "a.py").write_text("B\n", encoding="utf-8")
    _record(repo, "a.py", _sha("A\n"))
    _record(repo, "a.py", None)
    out, seen = _fire(repo, monkeypatch)
    assert out["committed"] is True, out
    assert "expected_content_sha256" not in seen[0]


def test_only_committed_declared_paths_enter_the_expectation_map(repo, monkeypatch):
    (repo / "a.py").write_text("A\n", encoding="utf-8")
    _record(repo, "a.py", _sha("A\n"))
    _record(repo, "README.md", _sha("not-the-readme\n"))
    out, seen = _fire(repo, monkeypatch)
    assert out["committed"] is True, out
    declared = set(seen[0]["expected_content_sha256"])
    assert declared == {"a.py"}
    assert declared.isdisjoint(out["receipts"])


_WITH_BASE = {**REVIEWED, "prep": {"run_base_sha": BASE}}


def test_a_review_of_the_current_bytes_supersedes_an_older_session_write(repo, monkeypatch):
    (repo / "a.py").write_text("B\n", encoding="utf-8")
    _record_at(repo, "a.py", _sha("A\n"), 1000.0)
    _review(repo, {"a.py": _sha("B\n")}, 2000.0)
    out, seen = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["committed"] is True, out
    assert seen[0]["expected_content_sha256"] == {"a.py": _sha("B\n")}


def test_a_rewrite_after_the_review_still_refuses_with_the_reviewed_hash(repo, monkeypatch):
    (repo / "a.py").write_text("C\n", encoding="utf-8")
    _record_at(repo, "a.py", _sha("A\n"), 1000.0)
    _review(repo, {"a.py": _sha("B\n")}, 2000.0)
    out, seen = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["committed"] is False
    assert out["refused"] == "blob-drift"
    assert seen[0]["expected_content_sha256"] == {"a.py": _sha("B\n")}
    assert out["blob_drift"][0]["path"] == "a.py"


def test_a_session_write_after_the_review_wins(repo, monkeypatch):
    (repo / "a.py").write_text("B\n", encoding="utf-8")
    _record_at(repo, "a.py", _sha("A\n"), 3000.0)
    _review(repo, {"a.py": _sha("B\n")}, 2000.0)
    out, seen = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["refused"] == "blob-drift"
    assert seen[0]["expected_content_sha256"] == {"a.py": _sha("A\n")}


def test_on_an_equal_timestamp_the_session_write_wins(repo, monkeypatch):
    (repo / "a.py").write_text("B\n", encoding="utf-8")
    _record_at(repo, "a.py", _sha("A\n"), 2000.0)
    _review(repo, {"a.py": _sha("B\n")}, 2000.0)
    out, seen = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["refused"] == "blob-drift"
    assert seen[0]["expected_content_sha256"] == {"a.py": _sha("A\n")}


def test_an_unparseable_review_stamp_is_no_review_basis(repo, monkeypatch):
    (repo / "a.py").write_text("B\n", encoding="utf-8")
    _record_at(repo, "a.py", _sha("A\n"), 1000.0)
    _review(repo, {"a.py": _sha("B\n")}, 2000.0)
    sidecar = next((repo / ".coordinator-local").rglob("*-prep.review-prep.md"))
    text = sidecar.read_text(encoding="utf-8")
    assert "Z'" in text
    sidecar.write_text(text.replace("Z'", "Z-garbage'"), encoding="utf-8")
    out, seen = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["refused"] == "blob-drift"
    assert seen[0]["expected_content_sha256"] == {"a.py": _sha("A\n")}


NOTE = "docs/delivery/run1.md"


def test_the_delivery_note_is_declared_at_the_bytes_its_verdicts_append_left(ask_repo, monkeypatch):
    ask._write(ask_repo, NOTE, "# delivered\n")
    ask._request(ask_repo, docs=(NOTE,))
    touch_record.append_event(
        touch_record.sink_path(ask_repo / ".git" / "coordinator-sessions" / ask._SID),
        session_id=ask._SID, agent_id=None, verb="T", path=NOTE,
        content_hash=_sha("# delivered\n"), kind="w", timestamp=1000.0,
    )
    calls = ask._count_commit_v2(monkeypatch)
    out = ask._call(ask_repo)
    assert out["committed"] is True, out
    final = (ask_repo / NOTE).read_bytes()
    assert b"## Verdicts" in final
    assert calls[0]["expected_content_sha256"][NOTE] == hashlib.sha256(final).hexdigest()


def test_a_foreign_edit_of_the_note_after_the_expectation_still_refuses(ask_repo, monkeypatch):
    ask._write(ask_repo, NOTE, "# delivered\n")
    ask._request(ask_repo, docs=(NOTE,))
    real = terminal_commit.reentrant_dispatch

    def foreign_edit_then_commit(op, params, **kw):
        if op == "ceremony.commit_v2":
            (ask_repo / NOTE).write_text("# foreign\n", encoding="utf-8")
        return real(op, params, **kw)

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", foreign_edit_then_commit)
    out = ask._call(ask_repo)
    assert out["committed"] is False
    assert out["refused"] == "blob-drift"
    assert out["blob_drift"][0]["path"] == NOTE
