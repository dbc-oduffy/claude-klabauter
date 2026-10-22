"""coordinator/bin/tests/test_census_revalidate_index_key.py -- census operands keyed from the index.

Subject: `revalidate_set`, `input_key` and `_TreeView` in `coordinator/bin/mise-census-revalidate.py`
under EM note N2: an operand is fingerprinted from the git index, never walked, and an operand the
index does not track is UNDECIDABLE and never run. Repos are built under tmp_path with `git init`.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.roadmap.census_store import CensusStore
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

TOOL = Path(__file__).resolve().parents[1] / "mise-census-revalidate.py"


@pytest.fixture(scope="module")
def mcr():
    spec = importlib.util.spec_from_file_location("mise_census_revalidate_index_key", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@t.example"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / "f.txt").write_text("alpha\nalpha\n", encoding="utf-8")
    (root / "d").mkdir()
    (root / "d" / "a.txt").write_text("needle\n", encoding="utf-8")
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "scratch").mkdir()
    (root / "scratch" / "tracked.txt").write_text("alpha\n", encoding="utf-8")
    _git(["add", "f.txt", "d/a.txt", "scratch/tracked.txt"], root)
    _git(["commit", "-qm", "seed"], root)
    (root / "scratch" / "note.txt").write_text("alpha\n", encoding="utf-8")
    return root


def _plan(root: Path, name: str, entries: list[tuple[str, str]]) -> Path:
    lines = ["---", "status: approved", "census:"]
    for command, result in entries:
        lines += ["  - question: q", f"    command: {command!r}", f"    result: {result!r}"]
    lines += ["---", "", "body", ""]
    path = root / "docs" / "plans" / f"{name}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    time.sleep(0.02)
    return path


def _run(mcr, plans, repo, store, **kw):
    return mcr.revalidate_set(
        plans, repo, 30, deadline=time.monotonic() + 600, head="H1", store=store, **kw
    )


def test_an_untracked_operand_is_undecidable_unrun_unwalked_and_listed(mcr, repo, monkeypatch):
    plan = _plan(repo, "u", [("grep -c alpha scratch/note.txt", "1")])
    popens: list = []
    real = subprocess.Popen

    class Counting(real):
        def __init__(self, *a, **k):
            popens.append(a)
            super().__init__(*a, **k)

    real_scandir = os.scandir

    def scandir(path=".", *a, **k):
        # git_state's index read globs the git dir's own sharedindex files; no operand is listed
        if isinstance(path, int):
            return real_scandir(path, *a, **k)
        if ".git" not in Path(os.fspath(path)).parts:
            raise AssertionError(f"the census must not list a directory: {path}")
        return real_scandir(path, *a, **k)

    def walk(*a, **k):
        raise AssertionError("the census must not walk a tree")

    monkeypatch.setattr(subprocess, "Popen", Counting)
    monkeypatch.setattr(mcr.os, "scandir", scandir)
    monkeypatch.setattr(mcr.os, "walk", walk)
    spawned: list = []
    monkeypatch.setattr(mcr, "_spawn", lambda *a: spawned.append(a) or (0, "1\n", ""))
    reply = _run(mcr, [plan], repo, CensusStore())
    row = reply["plans"]["docs/plans/u.md"]["entries"][0]
    assert row["state"] == mcr.UNDECIDABLE
    assert row["basis"] == mcr._UNTRACKED_OPERAND.format(rel="scratch/note.txt")
    assert "is not tracked" in row["basis"] and "untracked_operand" not in row
    assert spawned == [] and reply["spawns"] == 0
    assert not [p for p in popens if "cat-file" in p[0]], "no blob was asked for"
    assert reply["untracked"] == [
        {"plan": "docs/plans/u.md", "index": 0, "operand": "scratch/note.txt"}
    ]


def test_a_live_state_root_is_untracked_even_when_it_holds_tracked_files(mcr, repo):
    plan = _plan(repo, "u", [("ls scratch | wc -l", "2"), ("grep -c alpha scratch/tracked.txt", "1")])
    reply = _run(mcr, [plan], repo, CensusStore())
    rows = reply["plans"]["docs/plans/u.md"]["entries"]
    assert rows[0]["state"] == mcr.UNDECIDABLE and "scratch" in rows[0]["basis"]
    assert rows[1]["state"] == mcr.MATCH
    assert [u["operand"] for u in reply["untracked"]] == ["scratch"]


def test_staging_a_change_under_a_directory_operand_moves_its_key_untracked_does_not(mcr, repo):
    def key():
        return mcr.input_key({"command": "grep -rn needle d"}, repo, head="H1")

    base = key()
    assert base is not None
    (repo / "d" / "untracked.txt").write_text("needle\n", encoding="utf-8")
    assert key() == base
    (repo / "d" / "a.txt").write_text("needle\nneedle\n", encoding="utf-8")
    assert key() == base
    _git(["add", "d/a.txt"], repo)
    assert key() not in (None, base)


def test_a_second_call_at_an_unchanged_index_parses_nothing_and_screens_nothing(
    mcr, repo, monkeypatch
):
    plan = _plan(repo, "m", [("grep -c alpha f.txt", "2"), ("grep -rn needle d", "1 hit")])
    store = CensusStore()
    first = _run(mcr, [plan], repo, store)
    assert [e["state"] for e in first["plans"]["docs/plans/m.md"]["entries"]] == [
        mcr.MATCH, mcr.UNDECIDABLE]
    from coordinator_core.git import git_state

    parses: list = []
    real_read = git_state.read_index
    monkeypatch.setattr(
        git_state, "read_index", lambda *a, **k: parses.append(1) or real_read(*a, **k)
    )
    screened: list = []
    real_screen = mcr.screen
    monkeypatch.setattr(
        mcr, "screen", lambda *a, **k: screened.append(a[0]) or real_screen(*a, **k)
    )
    second = _run(mcr, [plan], repo, store)
    assert parses == [] and screened == []
    assert second["memo_hits"] >= 1 and second["spawns"] == 0


def test_the_tool_source_lists_no_directory():
    source = TOOL.read_text(encoding="utf-8")
    assert source.count("os.scandir") == 0
    assert "os.walk" not in source
    assert "def _children(self, rel" not in source and "def path_id" not in source


def test_the_screen_memo_spares_a_second_store_load_the_screen(mcr, repo, monkeypatch):
    plan = _plan(repo, "m", [("grep -c alpha f.txt", "2")])
    store = CensusStore.load(repo)
    _run(mcr, [plan], repo, store)
    assert store.screened("grep -c alpha f.txt") == (None,)
    screened: list = []
    real_screen = mcr.screen
    monkeypatch.setattr(
        mcr, "screen", lambda *a, **k: screened.append(a[0]) or real_screen(*a, **k)
    )
    _run(mcr, [plan], repo, CensusStore.load(repo))
    assert screened == []


def test_a_python_head_is_screened_each_call_against_the_index_with_no_git_spawn(
    mcr, repo, monkeypatch
):
    (repo / "tool.py").write_text("print(1)\n", encoding="utf-8")
    _git(["add", "tool.py"], repo)
    plan = _plan(repo, "p", [("python tool.py", "1"), ("python nothere.py", "1")])
    store = CensusStore()
    spawned: list = []
    monkeypatch.setattr(mcr, "_spawn", lambda c, r, t: spawned.append(c) or (0, "1\n", ""))
    reply = _run(mcr, [plan], repo, store)
    rows = reply["plans"]["docs/plans/p.md"]["entries"]
    assert rows[0]["state"] == mcr.MATCH
    assert rows[1]["state"] == mcr.REFUSED and "not a git-tracked file" in rows[1]["detail"]
    assert store.screened("python tool.py") is None, "a python head is never memoised"
    assert os.path.exists(repo / "tool.py")
