"""A wave checkpoint through `ceremony.commit_v2` after HEAD moved past `Checkpoint-Base`.

Fixture: a root commit, the run's base commit (it edits the declared path), and a push-ladder
shaped HEAD: a merge whose first parent is a line that never contained the base, then a plain
commit, both touching other paths only.

Which refusal each symptom hits:
  * Symptom 1 (moved HEAD refused): `checkpoint_guard.checkpoint_refusal` -> `_moved_outside_run`
    walks first-parent from HEAD, never meets the base across the merge, and reads the merge's
    diff against its first parent as an outside change to the declared path. The error is
    "writes changed outside this run". Measured: the merge test below reproduces it.
  * Symptom 2 ("nothing to commit -- all N path(s) already match HEAD"): `commit.py`'s per-path
    delta pass (`no_delta` equal to every assembled path -> `NothingToCommit`) fires when the
    `checkpoint_own_hunks` phase supplies a blob equal to HEAD's bytes for a path whose worktree
    differs. Unproven: no real hunk split was found that reduces to HEAD bytes, so the test
    stubs `checkpoint_own_blobs` to the reduced result the plan's Problem describes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import checkpoint_hunks
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit import peer_hunk_gate, run_hunk_claims, run_write_prints
from coordinator_core.ops.dispatch_emit.run_hunk_claims import HunkSplit
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "ckm-self"
PLAN = "state/mise-inventory/run.spine.md"
DECLARED = "tools/likeness/driver.py"
ROOT_TEXT = "".join(f"line{i}\n" for i in range(1, 11))
BASE_TEXT = ROOT_TEXT.replace("line2\n", "base2\n")
OWN_TEXT = BASE_TEXT.replace("line5\n", "OWN5\n")


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _call(repo: Path, params: dict) -> dict:
    return commit_v2._handler(params, repo_root=repo / ".git")


def _commit_file(repo: Path, rel: str, text: str, msg: str) -> str:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())
    _git(["add", rel], repo)
    _git(["commit", "-q", "-m", msg], repo)
    return _git(["rev-parse", "HEAD"], repo).strip()


def _message(base: str) -> str:
    return f"checkpoint(wave 1): 1 rows — r1\n\nCheckpoint-Plan: {PLAN}\nCheckpoint-Base: {base}\n"


def _params(base: str, paths: list) -> dict:
    return {
        "paths": paths, "skip_missing": True, "gone_tracked_as_deleted": True,
        "message": _message(base),
    }


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    _commit_file(root, DECLARED, ROOT_TEXT, "root")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(checkpoint_hunks, "checkpoint_run_id", lambda root, message, sid: "run1")
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [])
    monkeypatch.setattr(run_write_prints, "read", lambda r, run: {DECLARED: "recorded"})
    return root


def _move_head_past_base(repo: Path) -> str:
    """Base commit on the run's line, then HEAD = merge(other line, run's line) + a plain commit,
    each touching only other paths. Returns the base sha."""
    root_sha = _git(["rev-parse", "HEAD"], repo).strip()
    _git(["checkout", "-q", "-b", "upstream", root_sha], repo)
    _commit_file(repo, "other/upstream.txt", "u1\n", "upstream line")
    _git(["checkout", "-q", "-b", "run", root_sha], repo)
    base = _commit_file(repo, DECLARED, BASE_TEXT, "run base")
    _git(["checkout", "-q", "upstream"], repo)
    _git(["merge", "-q", "--no-ff", "-m", "push-ladder merge", "run"], repo)
    _commit_file(repo, "other/later.txt", "l1\n", "plain commit on HEAD")
    return base


def _own_edit(repo: Path) -> None:
    (repo / DECLARED).write_bytes(OWN_TEXT.encode())


def _claim_all_hunks(monkeypatch) -> None:
    monkeypatch.setattr(
        run_hunk_claims, "own_hunks", lambda r, run, path, hunks, holders: HunkSplit(own=list(hunks)),
    )


def test_checkpoint_lands_declared_path_after_head_moved_through_a_merge(repo, monkeypatch):
    base = _move_head_past_base(repo)
    _own_edit(repo)
    _claim_all_hunks(monkeypatch)
    assert _git(["log", "--format=%H", f"{base}..HEAD", "--", DECLARED], repo).strip() == ""

    result = _call(repo, _params(base, [DECLARED]))

    assert result["committed"] is True, result
    assert _git(["show", f"HEAD:{DECLARED}"], repo) == OWN_TEXT


def test_checkpoint_lands_declared_path_after_head_moved_by_plain_commits(repo, monkeypatch):
    base = _commit_file(repo, DECLARED, BASE_TEXT, "run base")
    _commit_file(repo, "other/a.txt", "a\n", "peer a")
    _commit_file(repo, "other/b.txt", "b\n", "peer b")
    _own_edit(repo)
    _claim_all_hunks(monkeypatch)

    result = _call(repo, _params(base, [DECLARED]))

    assert result["committed"] is True, result
    assert _git(["show", f"HEAD:{DECLARED}"], repo) == OWN_TEXT


def test_moved_head_still_refuses_a_revert_of_the_declared_path(repo, monkeypatch):
    base = _commit_file(repo, DECLARED, BASE_TEXT, "run base")
    _commit_file(repo, DECLARED, ROOT_TEXT, "peer reverts the declared path")
    _own_edit(repo)
    _claim_all_hunks(monkeypatch)
    head = _git(["rev-parse", "HEAD"], repo).strip()

    result = _call(repo, _params(base, [DECLARED]))

    assert result.get("committed") is not True
    assert "writes changed outside this run" in str(result)
    assert _git(["rev-parse", "HEAD"], repo).strip() == head


def test_no_delta_refusal_when_declared_path_truly_equals_head(repo):
    base = _commit_file(repo, DECLARED, BASE_TEXT, "run base")
    _commit_file(repo, "other/a.txt", "a\n", "peer a")
    head = _git(["rev-parse", "HEAD"], repo).strip()
    assert (repo / DECLARED).read_bytes() == BASE_TEXT.encode()

    result = _call(repo, _params(base, [DECLARED]))

    assert result.get("committed") is not True
    assert "already match HEAD" in str(result)
    assert _git(["rev-parse", "HEAD"], repo).strip() == head


def test_reduced_to_head_path_is_never_reported_as_matching_head(repo, monkeypatch):
    base = _commit_file(repo, DECLARED, BASE_TEXT, "run base")
    _commit_file(repo, "other/a.txt", "a\n", "peer a")
    _own_edit(repo)
    head_blob = _git(["rev-parse", f"HEAD:{DECLARED}"], repo).strip()
    monkeypatch.setattr(
        checkpoint_hunks, "checkpoint_own_blobs",
        lambda root, run_id, paths, message="", reasons=None: ({DECLARED: head_blob}, []),
    )

    result = _call(repo, _params(base, [DECLARED]))

    assert "already match HEAD" not in str(result), result
    assert result.get("committed") is True or DECLARED in (result.get("checkpoint_withheld_paths") or [])
    assert (repo / DECLARED).read_bytes() == OWN_TEXT.encode()
