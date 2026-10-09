"""dispatch.terminal_commit names the declared paths of every incomplete chunk in `stranded`,
and the undeclared dirty files beside its declared writes in `undeclared_dirty`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


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


def _script(repo: Path, request: CommitRequest) -> str:
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return "run.mjs"


_REVIEWED = {"integration_stem": "s", "slices": 1, "fixes": 0}


def _call(repo: Path, script: str, incomplete: list) -> dict:
    return terminal_commit._handler(
        {"script_path": script, "incomplete_chunks": incomplete, "inline_review": _REVIEWED},
        repo_root=repo / ".git",
    )


def _two_chunks(repo: Path) -> str:
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    return _script(
        repo,
        CommitRequest(
            chunks=(
                ChunkCommit(id="C1", title="t1", paths=("a.py",)),
                ChunkCommit(id="C2", title="t2", paths=("b.py", "b2.py")),
            )
        ),
    )


def test_incomplete_chunk_paths_are_named(repo):
    out = _call(repo, _two_chunks(repo), ["C2"])
    assert out["committed"] is True
    assert out["stranded"] == {"C2": ["b.py", "b2.py"]}


def test_all_done_run_strands_nothing(repo):
    out = _call(repo, _two_chunks(repo), [])
    assert out["stranded"] == {}


def test_stranded_is_present_on_a_refusal(repo):
    out = _call(repo, "run.mjs" if (repo / "run.mjs").exists() else "missing.mjs", [])
    assert out["stranded"] == {}


def test_bare_script_path_refuses_naming_the_missing_params_and_omits_stranded(repo):
    script = _two_chunks(repo)
    out = terminal_commit._handler({"script_path": script}, repo_root=repo / ".git")
    assert out["committed"] is False
    assert out["refused"] == "missing-run-outcome"
    assert out["missing"] == ["incomplete_chunks", "inline_review"]
    assert "task_output_path" in out["error"] and script in out["error"]
    assert "stranded" not in out


def test_unreviewed_refusal_omits_stranded(repo):
    script = _two_chunks(repo)
    out = terminal_commit._handler(
        {"script_path": script, "incomplete_chunks": ["C2"]}, repo_root=repo / ".git"
    )
    assert out["refused"] == "unreviewed"
    assert "stranded" not in out


def _inline_call(repo: Path, script: str) -> dict:
    return _call(repo, script, [])


def test_undeclared_file_beside_a_declared_write_is_reported_not_committed(repo):
    script = _two_chunks(repo)
    (repo / "a_test.py").write_text("t\n", encoding="utf-8")
    (repo / "sub").mkdir()
    (repo / "sub" / "deep.py").write_text("d\n", encoding="utf-8")
    out = _inline_call(repo, script)
    assert out["committed"] is True
    assert "a_test.py" in out["undeclared_dirty"]
    assert "a.py" not in out["undeclared_dirty"]
    assert "sub/deep.py" not in out["undeclared_dirty"]
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=str(repo), capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.split()
    assert "a_test.py" not in tracked


def test_undeclared_file_under_a_declared_prefix_is_reported(repo):
    (repo / "gen").mkdir()
    (repo / "gen" / "x.py").write_text("x\n", encoding="utf-8")
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "report.md").write_text("created-under-prefix: none\n", encoding="utf-8")
    script = _script(
        repo,
        CommitRequest(
            chunks=(
                ChunkCommit(
                    id="C1", title="t", paths=("a.py",), prefixes=("gen",), report="report.md"
                ),
            )
        ),
    )
    out = _inline_call(repo, script)
    assert "gen/x.py" in out["undeclared_dirty"]


def test_only_the_emitted_script_is_dirty_when_every_write_is_declared(repo):
    out = _inline_call(repo, _two_chunks(repo))
    assert out["undeclared_dirty"] == ["run.mjs"]


def test_no_marker_carries_no_undeclared_report(repo):
    (repo / "run.mjs").write_text("// no marker\n", encoding="utf-8")
    out = _inline_call(repo, "run.mjs")
    assert "undeclared_dirty" not in out
