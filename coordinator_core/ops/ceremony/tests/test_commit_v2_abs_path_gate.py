"""`ceremony.commit_v2` refuses a plan, brief or sizing that ADDS an absolute machine path, in-process.

Every refusal asserts HEAD is unmoved. Path literals are built from parts so this file carries none.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_gates, commit_v2

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_DRIVE = "C" + ":" + "\\" + "Users" + "\\" + "jdoe_dev" + "\\" + "repo"
_POSIX = "/" + "Users" + "/" + "jdoe_dev" + "/" + "repo"
_UNC = "\\\\" + "fileserver01" + "\\" + "projects" + "\\" + "dir"


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _head(repo: Path) -> str:
    return _git(["rev-parse", "HEAD"], repo).strip()


def _write(repo: Path, rel: str, text: str) -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(["init", "-q"], r)
    _git(["config", "user.email", "t@t.example"], r)
    _git(["config", "user.name", "t"], r)
    _write(r, "docs/plans/p.md", "# plan\nclean line\n")
    _write(r, "state/sizings/s.yaml", "a: 1\n")
    _write(r, "state/warp/w/briefs/C1.md", "brief\n")
    _write(r, "src/code.md", "doc\n")
    _git(["add", "-A"], r)
    _git(["commit", "-q", "-m", "seed"], r)
    return r


def _commit(repo: Path, rel: str) -> dict:
    return commit_v2._handler({"paths": [rel], "message": "edit\n"}, repo_root=repo / ".git")


@pytest.mark.parametrize(
    "rel, literal",
    [
        ("docs/plans/p.md", _DRIVE),
        ("docs/plans/p.md", _POSIX),
        ("docs/plans/p.md", _UNC),
        ("state/sizings/s.yaml", _DRIVE),
        ("state/warp/w/briefs/C1.md", _POSIX),
    ],
)
def test_added_absolute_path_is_refused_with_a_one_line_fix(repo, rel, literal):
    _write(repo, rel, (repo / rel).read_text(encoding="utf-8") + f"see {literal}\n")
    before = _head(repo)

    result = _commit(repo, rel)

    assert result["committed"] is False, result
    assert "abs_path_citation_gate" in result["error"]
    assert f"{rel}:" in result["error"]
    assert "abs-path-ok" in result["error"]
    assert _head(repo) == before


def test_preexisting_path_not_reflagged_but_a_new_duplicate_is(repo):
    _write(repo, "docs/plans/p.md", f"old {_POSIX}\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "had it"], repo)

    _write(repo, "docs/plans/p.md", f"old {_POSIX}\nmore prose\n")
    assert _commit(repo, "docs/plans/p.md")["committed"] is True

    _write(repo, "docs/plans/p.md", f"old {_POSIX}\nmore prose\nnew {_POSIX}\n")
    before = _head(repo)
    result = _commit(repo, "docs/plans/p.md")
    assert result["committed"] is False
    assert _head(repo) == before


def test_marked_line_and_ungated_path_pass(repo):
    _write(repo, "docs/plans/p.md", f"quoted {_POSIX} abs-path-ok: documenting the refusal\n")
    assert _commit(repo, "docs/plans/p.md")["committed"] is True

    _write(repo, "src/code.md", f"see {_POSIX}\n")
    assert _commit(repo, "src/code.md")["committed"] is True


def test_gate_spawns_nothing_and_skips_unrelated_paths_without_reading(repo, monkeypatch):
    reads = []

    def read_source(rel):
        reads.append(rel)
        return (repo / rel).read_bytes()

    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)

    out = commit_gates.abs_path_citation_gate(repo, ["src/a.py", "state/x.json"], read_source)
    assert out.passed and out.skipped and reads == []

    _write(repo, "docs/plans/p.md", f"x {_DRIVE}\n")
    out = commit_gates.abs_path_citation_gate(repo, ["docs/plans/p.md"], read_source)
    assert not out.passed
    assert out.diagnostics[0].startswith("docs/plans/p.md:1 ")


def test_url_text_is_not_hot_but_forward_slash_drive_path_is():
    marked = commit_gates._ABS_PATH_DRIVE_FWD_RE
    assert not marked.search("see https://example.com/x and ftp://h/y")
    assert marked.search("path " + "C" + ":/" + "Users/x")
    assert not any(m in "see https://example.com/x" for m in commit_gates._ABS_PATH_CHEAP_MARKERS)
