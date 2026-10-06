"""dispatch.terminal_commit strands a committable chunk that imports an uncommitted file of a
stranded chunk, with that chunk, and still lands the independent chunks."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = {"integration_stem": "s", "slices": 1, "fixes": 0}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _prune_shape(repo: Path, c1_report: str, c3_body: str) -> str:
    (repo / "pkg" / "prune.py").write_text("def prune():\n    return 1\n", encoding="utf-8")
    (repo / "pkg" / "cli.py").write_text(c3_body, encoding="utf-8")
    (repo / "pkg" / "other.py").write_text("X = 2\n", encoding="utf-8")
    chunks = []
    for cid, path, report in (
        ("C1", "pkg/prune.py", c1_report), ("C2", "pkg/other.py", "DONE: ok\n"),
        ("C3", "pkg/cli.py", "DONE: ok\n"),
    ):
        (repo / f"report-{cid}.md").write_text(report, encoding="utf-8")
        chunks.append(ChunkCommit(id=cid, title=cid, paths=(path,), report=f"report-{cid}.md"))
    (repo / "run.mjs").write_text(
        "// emitted\n" + render_marker(CommitRequest(chunks=tuple(chunks))) + "\n", encoding="utf-8"
    )
    return "run.mjs"


def _call(repo: Path, script: str) -> dict:
    return terminal_commit._handler(
        {"script_path": script, "incomplete_chunks": ["C1"], "inline_review": _REVIEWED},
        repo_root=repo / ".git",
    )


@pytest.mark.parametrize("import_line", ["from pkg.prune import prune", "from . import prune", "import pkg.prune"])
def test_importer_of_stranded_definer_is_stranded_and_independent_chunk_lands(repo, import_line):
    out = _call(repo, _prune_shape(repo, "BLOCKED: no runtime\n", import_line + "\n"))
    assert out["committed"] is True, out
    assert out["chunks_committed"] == ["C2"]
    assert out["entangled"] == {"C3": ["C1"]}
    assert set(out["stranded"]) == {"C1", "C3"}
    assert out["incomplete_reasons"]["C3"] == "entangled: imports stranded C1"
    tracked = _git(["ls-files"], repo).split()
    assert "pkg/other.py" in tracked
    assert "pkg/cli.py" not in tracked and "pkg/prune.py" not in tracked


def test_importer_of_delivered_definer_commits_together(repo):
    out = _call(repo, _prune_shape(repo, "DONE: ok\n", "from pkg.prune import prune\n"))
    assert out["committed"] is True, out
    assert out["chunks_committed"] == ["C2", "C3"]
    assert out["partial_committed"] == ["C1"]
    assert "entangled" not in out
    assert {"pkg/prune.py", "pkg/cli.py"} <= set(_git(["ls-files"], repo).split())


def test_independent_chunk_with_no_import_is_not_entangled(repo):
    out = _call(repo, _prune_shape(repo, "BLOCKED: no runtime\n", "Y = 1\n"))
    assert out["committed"] is True, out
    assert "entangled" not in out
    assert set(out["stranded"]) == {"C1"}
    assert {"pkg/cli.py", "pkg/other.py"} <= set(_git(["ls-files"], repo).split())


def _emit(repo: Path, specs) -> str:
    chunks = []
    for cid, paths, prefixes, report in specs:
        (repo / f"report-{cid}.md").write_text(report, encoding="utf-8")
        chunks.append(ChunkCommit(
            id=cid, title=cid, paths=tuple(paths), prefixes=tuple(prefixes), report=f"report-{cid}.md"
        ))
    (repo / "run.mjs").write_text(
        "// emitted\n" + render_marker(CommitRequest(chunks=tuple(chunks))) + "\n", encoding="utf-8"
    )
    return "run.mjs"


def _write(repo: Path, rel: str, body: str) -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(body, encoding="utf-8")


def test_transitive_importer_chain_is_stranded(repo):
    _write(repo, "pkg/c1.py", "A = 1\n")
    _write(repo, "pkg/c3.py", "from pkg.c1 import A\n")
    _write(repo, "pkg/c4.py", "from pkg.c3 import A\n")
    _write(repo, "pkg/c2.py", "X = 2\n")
    script = _emit(repo, [
        ("C1", ["pkg/c1.py"], [], "BLOCKED: x\n"), ("C2", ["pkg/c2.py"], [], "DONE: ok\n"),
        ("C3", ["pkg/c3.py"], [], "DONE: ok\n"), ("C4", ["pkg/c4.py"], [], "DONE: ok\n"),
    ])
    out = _call(repo, script)
    assert out["committed"] is True, out
    assert out["chunks_committed"] == ["C2"]
    assert set(out["entangled"]) == {"C3", "C4"}
    assert set(out["stranded"]) == {"C1", "C3", "C4"}


def test_unchanged_stranded_file_is_not_a_target(repo):
    _write(repo, "pkg/c1.py", "A = 1\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "c1"], repo)
    _write(repo, "pkg/c3.py", "from pkg.c1 import A\n")
    script = _emit(repo, [
        ("C1", ["pkg/c1.py"], [], "BLOCKED: x\n"), ("C3", ["pkg/c3.py"], [], "DONE: ok\n"),
    ])
    out = _call(repo, script)
    assert out["committed"] is True, out
    assert "entangled" not in out
    assert "pkg/c3.py" in _git(["ls-files"], repo).split()


def test_importer_of_file_created_under_stranded_prefix_is_entangled(repo):
    _write(repo, "gen/made.py", "A = 1\n")
    _write(repo, "pkg/c3.py", "from gen.made import A\n")
    _write(repo, "pkg/c2.py", "X = 2\n")
    script = _emit(repo, [
        ("C1", [], ["gen/"], "BLOCKED: x\ncreated-under-prefix: gen/made.py\n"),
        ("C2", ["pkg/c2.py"], [], "DONE: ok\n"), ("C3", ["pkg/c3.py"], [], "DONE: ok\n"),
    ])
    out = _call(repo, script)
    assert out["committed"] is True, out
    assert out["chunks_committed"] == ["C2"]
    assert out["entangled"] == {"C3": ["C1"]}
