"""dispatch.terminal_commit lands a review-stage edit outside the declared writes as review residue,
or refuses ``review-edit-unlanded`` naming the files; an executor's undeclared write stays reported."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import review_edits, run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-000000000011"
PEER = "5e1f0000-0000-4000-8000-000000000012"
RUN = "run-review-residue"
STRAY = "elsewhere/tests/test_stray.py"
FOREIGN = "foreign/other_run.py"
EARLIER = "elsewhere/tests/earlier.py"
_REVIEWED = {
    "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
    "prep": {"slice_files": [STRAY, "elsewhere/tests/gone.py"]},
}
_UNLISTED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 1}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8", newline="")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("a\n", encoding="utf-8", newline="")
    (root / "elsewhere" / "tests").mkdir(parents=True)
    (root / STRAY).write_text("review_edit = 1\n", encoding="utf-8", newline="")
    return root


def _review_wrote(monkeypatch, writes) -> None:
    monkeypatch.setattr(review_edits, "review_writes", lambda root, sid, since: writes)


def _run(repo: Path, **params) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("src/a.py",)),))
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8", newline="")
    return terminal_commit._handler(
        {
            "script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED,
            "session_id": SELF, **params,
        },
        repo_root=repo / ".git",
    )


def _committed(repo: Path) -> list:
    out = _git(["show", "--numstat", "--format=", "HEAD"], repo)
    return sorted(ln.split("\t")[-1] for ln in out.splitlines() if ln.strip())


def _dirty(repo: Path, rel: str) -> str:
    return _git(["status", "--porcelain", "--untracked-files=all", "--", rel], repo).strip()


def test_review_edit_outside_declared_dir_lands_as_review_residue(repo, monkeypatch):
    _review_wrote(monkeypatch, {STRAY: "agent-1"})
    out = _run(repo)
    assert out["committed"] is True, out
    assert out.get("ok") is not False, out
    assert out["review_residue"] == [STRAY]
    assert _committed(repo) == sorted(["src/a.py", STRAY])
    assert f"Review-Residue: {STRAY}" in _git(["log", "-1", "--format=%B"], repo)
    assert _dirty(repo, STRAY) == ""
    assert STRAY not in out["undeclared_dirty"]


def test_unlandable_review_edit_refuses_naming_the_files(repo, monkeypatch):
    session_core.init(PEER, cwd=str(repo))
    session_scope.touch(PEER, STRAY, cwd=str(repo))
    _review_wrote(monkeypatch, {STRAY: "agent-1"})
    head = _git(["rev-parse", "HEAD"], repo)
    token = run_token.mint(repo, RUN, "run.mjs", session_id=SELF)
    out = _run(repo, run_id=RUN, run_token=token)
    assert out["committed"] is False
    assert out["refused"] == "review-edit-unlanded"
    assert out["review_edits_unlanded"] == {STRAY: review_edits.HALT_PEER_CONTESTED}
    assert STRAY in out["error"]
    assert _git(["rev-parse", "HEAD"], repo) == head
    _review_wrote(monkeypatch, {})
    retry = _run(repo, run_id=RUN, run_token=token)
    assert retry["committed"] is True, retry


def test_a_deleted_review_write_refuses(repo, monkeypatch):
    (repo / "elsewhere" / "tests" / "gone.py").write_text("x\n", encoding="utf-8", newline="")
    _git(["add", "elsewhere"], repo)
    _git(["commit", "-q", "-m", "track"], repo)
    (repo / "elsewhere" / "tests" / "gone.py").unlink()
    _review_wrote(monkeypatch, {"elsewhere/tests/gone.py": "agent-1"})
    out = _run(repo)
    assert out["refused"] == "review-edit-unlanded"
    assert out["review_edits_unlanded"] == {"elsewhere/tests/gone.py": review_edits.HALT_DELETED}


def test_an_unanswerable_source_sets_ok_false_with_the_commit_landed(repo, monkeypatch):
    _review_wrote(monkeypatch, None)
    out = _run(repo)
    assert out["committed"] is True, out
    assert out["ok"] is False
    assert out["review_edits"] is None
    assert "review-stage edits unverified" in out["error"]
    assert STRAY not in _committed(repo)


def test_an_executor_only_undeclared_file_is_still_only_reported(repo, monkeypatch):
    (repo / "src" / "stray.py").write_text("stray\n", encoding="utf-8", newline="")
    _review_wrote(monkeypatch, {STRAY: "agent-1"})
    out = _run(repo)
    assert out["committed"] is True, out
    assert "src/stray.py" in out["undeclared_dirty"]
    assert "src/stray.py" not in _committed(repo)
    assert out["review_residue"] == [STRAY]


def test_a_sibling_declared_review_write_is_neither_landed_nor_refused(repo, monkeypatch):
    _review_wrote(monkeypatch, {STRAY: "agent-1"})
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda root, rid: {STRAY: "other-run"})
    out = _run(repo)
    assert out["committed"] is True, out
    assert out.get("ok") is not False, out
    assert "review_residue" not in out
    assert STRAY not in _committed(repo)
    assert _dirty(repo, STRAY) != ""


def test_a_review_write_outside_the_runs_own_files_is_left_dirty_and_not_halted(repo, monkeypatch):
    (repo / "foreign").mkdir()
    (repo / FOREIGN).write_text("other run\n", encoding="utf-8", newline="")
    _review_wrote(monkeypatch, {STRAY: "agent-1", FOREIGN: "agent-2"})
    out = _run(repo)
    assert out["committed"] is True, out
    assert out["review_residue"] == [STRAY]
    assert FOREIGN not in _committed(repo)
    assert _dirty(repo, FOREIGN) != ""


def test_a_review_edit_to_an_earlier_checkpoints_file_lands_when_the_review_record_lists_it(repo, monkeypatch):
    (repo / EARLIER).write_text("v1\n", encoding="utf-8", newline="")
    _git(["add", EARLIER], repo)
    _git(["commit", "-q", "-m", "checkpoint(wave 1)"], repo)
    (repo / EARLIER).write_text("v = 2\n", encoding="utf-8", newline="")
    _review_wrote(monkeypatch, {EARLIER: "agent-1"})
    out = _run(repo, inline_review={**_UNLISTED, "reverify": {"edited": [EARLIER]}})
    assert out["committed"] is True, out
    assert out["review_residue"] == [EARLIER]
    assert EARLIER in _committed(repo)
    assert _dirty(repo, EARLIER) == ""


def test_an_earlier_checkpoint_file_the_review_record_omits_stays_dirty(repo, monkeypatch):
    (repo / EARLIER).write_text("v1\n", encoding="utf-8", newline="")
    _git(["add", EARLIER], repo)
    _git(["commit", "-q", "-m", "checkpoint(wave 1)"], repo)
    (repo / EARLIER).write_text("v = 3\n", encoding="utf-8", newline="")
    _review_wrote(monkeypatch, {EARLIER: "agent-2"})
    out = _run(repo, inline_review={**_UNLISTED, "reverify": {"edited": [STRAY]}})
    assert out["committed"] is True, out
    assert "review_residue" not in out
    assert EARLIER not in _committed(repo)
    assert _dirty(repo, EARLIER) != ""
