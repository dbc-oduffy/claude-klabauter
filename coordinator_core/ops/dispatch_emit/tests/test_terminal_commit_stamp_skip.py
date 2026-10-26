"""dispatch.terminal_commit stamps run-stage.json `landed` only when the run has a record dir and a
path outside it lands; a run with no record dir, or one landing only record-dir paths, is not stamped."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-stampskip"
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed

_REVIEWED = reviewed()


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8", newline="")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("a\n", encoding="utf-8", newline="")
    return root


def _record_file(repo: Path) -> str:
    rec = repo / ask_contract.RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True)
    (rec / "note.md").write_text("n\n", encoding="utf-8", newline="")
    return f"{ask_contract.RECORD_DIR_ROOT}/{RUN}/note.md"


def _run(repo: Path, *paths: str) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=tuple(paths)),))
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8", newline="")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "run_id": RUN, "incomplete_chunks": [], "inline_review": _REVIEWED},
        repo_root=repo / ".git",
    )


def _names(repo: Path, sha: str) -> list:
    return sorted(ln for ln in _git(["show", "--name-only", "--format=", sha], repo).splitlines() if ln.strip())


def test_no_record_dir_lands_without_a_stamp(repo):
    out = _run(repo, "src/a.py")
    assert out["committed"] is True, out
    assert _names(repo, out["sha"]) == ["src/a.py"]
    assert ask_contract.read_run_stage(repo, RUN) is None
    assert not (repo / ask_contract.RECORD_DIR_ROOT / RUN).exists()


def test_only_record_dir_paths_landing_leaves_the_stage_unstamped(repo):
    note = _record_file(repo)
    out = _run(repo, note)
    assert out["committed"] is True, out
    assert ask_contract.RUN_STAGE_FILE not in " ".join(_names(repo, out["sha"]))
    assert ask_contract.read_run_stage(repo, RUN) is None


def test_a_path_outside_the_record_dir_landing_stamps_landed(repo):
    note = _record_file(repo)
    out = _run(repo, "src/a.py", note)
    assert out["committed"] is True, out
    assert (ask_contract.read_run_stage(repo, RUN) or {}).get("stage") == "landed"
