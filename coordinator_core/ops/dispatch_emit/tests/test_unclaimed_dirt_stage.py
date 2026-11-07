"""unclaimed_dirt_stage.own_only_blobs: a landing path's own hunks over HEAD, peers' dirt left out."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import read_object
from coordinator_core.ops.dispatch_emit import unclaimed_dirt_stage as uds
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]

P = "pkg/shared.py"
LINES = 120
RUN = "ask-20261031T100000-aaaaaa"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _rows(edits: dict) -> str:
    rows = [f"x_{i} = {i}" for i in range(1, LINES + 1)]
    for n, tag in edits.items():
        rows[n - 1] = f"x_{n} = '{tag}'"
    return "\n".join(rows) + "\n"


def _put(root: Path, text: str, eol: str = "\n") -> None:
    (root / P).write_bytes(text.replace("\n", eol).encode("utf-8"))


def _text(root: Path, sha: str) -> str:
    obj = read_object(resolve_git_common_dir(root), sha)
    assert obj is not None
    return obj[1].decode("utf-8")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")
    _put(root, _rows({}))
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def test_pre_run_dirt_beside_own_hunk_is_left_out_of_the_blob(repo):
    _put(repo, _rows({5: "DIRT"}))
    base = wb.capture(repo, [P])
    _put(repo, _rows({5: "DIRT", 115: "OWN"}))

    blobs, refusals = uds.own_only_blobs(repo, RUN, [P], base)

    assert refusals == []
    assert _text(repo, blobs[P]) == _rows({115: "OWN"})


def test_pre_run_dirt_the_run_rewrote_is_stageable_as_is(repo):
    _put(repo, _rows({60: "DIRT"}))
    base = wb.capture(repo, [P])
    _put(repo, _rows({60: "OWN"}))

    assert uds.own_only_blobs(repo, RUN, [P], base) == ({}, [])


def test_dirt_sharing_a_block_with_own_hunk_refuses_naming_the_header(repo):
    _put(repo, _rows({60: "DIRT"}))
    base = wb.capture(repo, [P])
    _put(repo, _rows({60: "DIRT"}).replace("x_61 = 61\n", "x_61 = 'O-1'\nx_61b = 'O-2'\n"))

    blobs, refusals = uds.own_only_blobs(repo, RUN, [P], base)

    assert blobs == {}
    assert len(refusals) == 1
    path, run, header = refusals[0]
    assert path == P and run is None and header.startswith("@@ ")


def test_wholly_own_file_is_omitted(repo):
    base = wb.capture(repo, [P])
    _put(repo, _rows({5: "OWN", 115: "OWN"}))

    assert uds.own_only_blobs(repo, RUN, [P], base) == ({}, [])


def test_no_baseline_is_omitted(repo):
    _put(repo, _rows({5: "X"}))

    assert uds.own_only_blobs(repo, RUN, [P], {}) == ({}, [])
    assert uds.own_only_blobs(repo, RUN, [P], {P: None}) == ({}, [])


def test_crlf_worktree_wholly_own_is_omitted_and_mixed_yields_lf_blob(repo):
    _git(repo, "config", "core.autocrlf", "true")
    base = wb.capture(repo, [P])
    _put(repo, _rows({115: "OWN"}), "\r\n")
    assert uds.own_only_blobs(repo, RUN, [P], base) == ({}, [])

    repo2_text = _rows({5: "DIRT"})
    _put(repo, repo2_text)
    _git(repo, "checkout", "--", P)
    _put(repo, repo2_text, "\r\n")
    base = wb.capture(repo, [P])
    _put(repo, _rows({5: "DIRT", 115: "OWN"}), "\r\n")

    blobs, refusals = uds.own_only_blobs(repo, RUN, [P], base)

    assert refusals == []
    assert _text(repo, blobs[P]) == _rows({115: "OWN"})


def test_never_raises_an_exception_refuses_the_path(repo, monkeypatch):
    base = wb.capture(repo, [P])
    _put(repo, _rows({5: "X"}))
    from coordinator_core.ops.dispatch_emit import hunk_stage

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(hunk_stage, "split", boom)

    assert uds.own_only_blobs(repo, RUN, [P], base) == ({}, [(P, None, "unreadable")])


def test_zero_subprocess_calls(repo, monkeypatch):
    _put(repo, _rows({5: "DIRT"}))
    base = wb.capture(repo, [P])
    _put(repo, _rows({5: "DIRT", 115: "OWN"}))
    spawned = []
    real = subprocess.Popen

    class _Counting(real):
        def __init__(self, *args, **kw):
            spawned.append(args[:1])
            super().__init__(*args, **kw)

    monkeypatch.setattr(subprocess, "Popen", _Counting)

    uds.own_only_blobs(repo, RUN, [P], base)

    assert spawned == []
