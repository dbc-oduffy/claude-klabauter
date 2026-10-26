"""A refire of a run whose terminal commit refused ``blob-drift`` declares, per path, the bytes its
own newest review read -- or, for a path the run session itself rewrote after that review, the
bytes the run left -- and still refuses a path a session outside the run rewrote."""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep
from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.ops.review_mint.execute_review import prep_slice_id_for
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SID = "c4e53fa2-2bda-51b1-833a-cf629f2b0cb6"
RUN = "run-1"
PLAN = "docs/plans/2026-10-16-refire.md"
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed

REVIEWED = reviewed()


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "a.py").write_text("seed\n", encoding="utf-8", newline="")
    (root / "README.md").write_text("seed\n", encoding="utf-8", newline="")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _base(repo: Path) -> str:
    return _git(["rev-parse", "HEAD"], repo)


def _script(repo: Path) -> str:
    request = CommitRequest(chunks=(ChunkCommit(id="C4", title="t4", paths=("a.py",)),))
    (repo / "run.mjs").write_text("// s\n" + render_marker(request) + "\n", encoding="utf-8", newline="")
    return "run.mjs"


def _write(repo: Path, rel: str, text: str) -> None:
    (repo / rel).write_text(text, encoding="utf-8", newline="")


def _record(repo: Path, rel: str, text: str, at: float, session: str = SID) -> None:
    """The run session's touch event: it wrote ``text`` to ``rel`` at ``at``."""
    sink = touch_record.sink_path(repo / ".git" / "coordinator-sessions" / session)
    touch_record.append_event(
        sink, session_id=session, agent_id=None, verb="T", path=rel,
        content_hash=_sha(text), kind="w", timestamp=at,
    )


def _prep(repo: Path, base: str, paths: tuple | list = ("a.py",)) -> dict:
    """review.prep over the worktree the way the run fires it: the real slice prefix, the real
    store, ``recorded_at`` stamped now."""
    reply = review_prep.review_prep(
        repo, base=base, paths=list(paths), plan_path=PLAN, session_id=SID,
        slice_prefix=prep_slice_id_for(PLAN, base, RUN),
    )
    assert reply["error"] is None, reply
    return reply


def _fire(repo: Path, monkeypatch, base: str) -> tuple[dict, list]:
    script = _script(repo)
    token = run_token.mint(repo, RUN, script)
    seen: list = []
    real = terminal_commit.reentrant_dispatch

    def spy(op, params, **kw):
        if op == "ceremony.commit_v2":
            seen.append(dict(params))
        return real(op, params, **kw)

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", spy)
    out = terminal_commit._handler(
        {"script_path": script, "incomplete_chunks": [], "session_id": SID, "run_id": RUN,
         "run_token": token, "inline_review": {**REVIEWED, "prep": {"run_base_sha": base}}},
        repo_root=repo / ".git",
    )
    return out, seen


def test_a_refire_overwrites_the_first_fires_stored_review(repo, monkeypatch):
    """Cause (i) ruled out: a refire in the same session under the same plan, run key and base
    re-stores the one sidecar, so the lookup stays unambiguous and the refire's review wins over
    the first fire's older session write."""
    base = _base(repo)
    _write(repo, "a.py", "A\n")
    _record(repo, "a.py", "A\n", 1000.0)
    _prep(repo, base)

    _write(repo, "a.py", "B\n")
    _prep(repo, base)

    out, seen = _fire(repo, monkeypatch, base)

    assert out["committed"] is True, out
    assert seen[0]["expected_content_sha256"] == {"a.py": _sha("B\n")}


def test_a_refire_whose_review_names_paths_beyond_the_commit_declares_its_bytes(repo, monkeypatch):
    """Drift (a), cause (ii): the review covers a path outside the commit's chunk paths, so
    ``stored_reply_for_run``'s ``within`` filter drops the whole reply and the first fire's older
    session write supplies the expectation. The refire commits the bytes its review read."""
    base = _base(repo)
    _write(repo, "a.py", "A\n")
    _record(repo, "a.py", "A\n", 1000.0)
    _prep(repo, base, paths=["a.py", "README.md"])

    _write(repo, "a.py", "B\n")
    _write(repo, "README.md", "edited\n")
    _prep(repo, base, paths=["a.py", "README.md"])

    out, seen = _fire(repo, monkeypatch, base)

    assert out["committed"] is True, out
    assert seen[0]["expected_content_sha256"]["a.py"] == _sha("B\n")


def test_a_run_owned_write_after_the_review_is_declared_at_its_bytes(repo, monkeypatch):
    """Drift (b): the review read A; the run session then wrote B itself. Its newest touch event
    postdates the review but fingerprinted the bytes from before that write (A2), and the file's
    mtime is no earlier than the event. The path commits at B, the bytes the run left."""
    base = _base(repo)
    _write(repo, "a.py", "A\n")
    _prep(repo, base)

    _write(repo, "a.py", "B\n")
    event_at = time.time() + 100.0
    _record(repo, "a.py", "A2\n", event_at)
    os.utime(repo / "a.py", (event_at + 1.0, event_at + 1.0))

    out, seen = _fire(repo, monkeypatch, base)

    assert out["committed"] is True, out
    assert seen[0]["expected_content_sha256"] == {"a.py": _sha("B\n")}


def test_a_rewrite_by_a_writer_outside_the_run_after_the_review_still_refuses(repo, monkeypatch):
    """Negative: the run's review read B; a writer that is not the run's rewrote the file to C,
    with no newer run-session write. The expectation stays B, so the commit refuses with the path
    in ``blob_drift``."""
    base = _base(repo)
    _write(repo, "a.py", "B\n")
    _prep(repo, base)

    _write(repo, "a.py", "C\n")
    os.utime(repo / "a.py", (time.time() + 100.0, time.time() + 100.0))

    out, seen = _fire(repo, monkeypatch, base)

    assert out["committed"] is False
    assert out["refused"] == "blob-drift"
    assert out["blob_drift"][0]["path"] == "a.py"
    assert seen[0]["expected_content_sha256"] == {"a.py": _sha("B\n")}
