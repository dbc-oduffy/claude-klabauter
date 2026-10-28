"""hunk_stage.split: a worktree file two runs dirtied, divided into this run's hunks and the rest.

Each case builds a tmp git repo, records run baselines through ``write_baseline.capture`` and
asserts the split's staged blob, own headers and overlaps; zero spawns throughout.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import read_object
from coordinator_core.ops.dispatch_emit import hunk_stage
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

P = "pkg/shared.py"
LINES = 120
RUN_A = "ask-20261027T100000-aaaaaa"
RUN_B = "ask-20261027T110000-bbbbbb"


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


def _staged_text(root: Path, sha: str) -> str:
    obj = read_object(resolve_git_common_dir(root), sha)
    assert obj is not None and obj[0] == "blob", sha
    return obj[1].decode("utf-8")


def _headers_cover(headers, line: int) -> bool:
    import re

    for h in headers:
        m = re.match(r"@@ -(\d+)(?:,(\d+))? ", h)
        start, n = int(m.group(1)), int(m.group(2) or 1)
        if start <= line < start + max(n, 1):
            return True
    return False


def test_disjoint_split_stages_the_own_hunk_only_when_the_foreign_run_predates_ours(repo):
    base_a = wb.capture(repo, [P])[P]
    _put(repo, _rows({5: "A-EDIT"}))
    base_b = wb.capture(repo, [P])[P]
    _put(repo, _rows({5: "A-EDIT", 115: "B-EDIT"}))

    got = hunk_stage.split(
        repo, P, own_run_id=RUN_B, own_baseline_blob=base_b, foreign=[(RUN_A, base_a)]
    )

    assert got.overlaps == []
    assert got.staged_blob is not None
    assert _staged_text(repo, got.staged_blob) == _rows({115: "B-EDIT"})
    assert _headers_cover(got.own_headers, 115) and not _headers_cover(got.own_headers, 5)


def test_disjoint_split_stages_the_own_hunk_only_when_the_foreign_run_postdates_ours(repo):
    base_b = wb.capture(repo, [P])[P]
    _put(repo, _rows({115: "B-EDIT"}))
    base_a = wb.capture(repo, [P])[P]
    _put(repo, _rows({5: "A-EDIT", 115: "B-EDIT"}))

    got = hunk_stage.split(
        repo, P, own_run_id=RUN_B, own_baseline_blob=base_b, foreign=[(RUN_A, base_a)]
    )

    assert got.overlaps == []
    assert got.staged_blob is not None
    assert _staged_text(repo, got.staged_blob) == _rows({115: "B-EDIT"})


def test_a_foreign_hunk_on_the_same_lines_refuses_naming_the_run_and_header(repo):
    base_a = wb.capture(repo, [P])[P]
    _put(repo, _rows({60: "A-EDIT"}))
    base_b = wb.capture(repo, [P])[P]
    _put(repo, _rows({60: "B-EDIT"}))

    got = hunk_stage.split(
        repo, P, own_run_id=RUN_B, own_baseline_blob=base_b, foreign=[(RUN_A, base_a)]
    )

    assert got.staged_blob is None
    assert [run for run, _ in got.overlaps] == [RUN_A]
    assert _headers_cover([h for _, h in got.overlaps], 60)


def test_a_postdating_foreign_run_that_reedits_our_line_refuses_naming_it(repo):
    base_b = wb.capture(repo, [P])[P]
    _put(repo, _rows({60: "B-EDIT"}))
    base_a = wb.capture(repo, [P])[P]
    _put(repo, _rows({60: "A-EDIT"}))

    got = hunk_stage.split(
        repo, P, own_run_id=RUN_B, own_baseline_blob=base_b, foreign=[(RUN_A, base_a)]
    )

    assert got.staged_blob is None
    assert [run for run, _ in got.overlaps] == [RUN_A]
    assert _headers_cover([h for _, h in got.overlaps], 60)


def test_pre_run_dirt_under_our_hunk_refuses_unattributed(repo):
    _put(repo, _rows({60: "DIRT"}))
    base_b = wb.capture(repo, [P])[P]
    _put(repo, _rows({60: "B-EDIT"}))

    got = hunk_stage.split(repo, P, own_run_id=RUN_B, own_baseline_blob=base_b, foreign=[])

    assert got.staged_blob is None
    assert [run for run, _ in got.overlaps] == [None]


def test_an_adjacent_foreign_hunk_is_not_an_overlap(repo):
    base_a = wb.capture(repo, [P])[P]
    _put(repo, _rows({60: "A-EDIT"}))
    base_b = wb.capture(repo, [P])[P]
    _put(repo, _rows({60: "A-EDIT", 61: "B-EDIT"}))

    got = hunk_stage.split(
        repo, P, own_run_id=RUN_B, own_baseline_blob=base_b, foreign=[(RUN_A, base_a)]
    )

    assert got.overlaps == []
    assert got.staged_blob is not None
    assert _staged_text(repo, got.staged_blob) == _rows({61: "B-EDIT"})


def test_a_crlf_worktree_stages_checkin_form_bytes(repo):
    _git(repo, "config", "core.autocrlf", "true")
    _put(repo, _rows({}), "\r\n")
    base_a = wb.capture(repo, [P])[P]
    _put(repo, _rows({5: "A-EDIT"}), "\r\n")
    base_b = wb.capture(repo, [P])[P]
    _put(repo, _rows({5: "A-EDIT", 115: "B-EDIT"}), "\r\n")

    got = hunk_stage.split(
        repo, P, own_run_id=RUN_B, own_baseline_blob=base_b, foreign=[(RUN_A, base_a)]
    )

    assert got.staged_blob is not None
    assert _staged_text(repo, got.staged_blob) == _rows({115: "B-EDIT"})


def test_split_spawns_nothing(repo, monkeypatch):
    _put(repo, _rows({5: "A-EDIT"}))
    base = wb.capture(repo, [P])[P]
    _put(repo, _rows({5: "A-EDIT", 115: "B-EDIT"}))
    spawned = []
    real = subprocess.Popen

    class _Counting(real):
        def __init__(self, *args, **kwargs):
            spawned.append(args[:1])
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _Counting)

    hunk_stage.split(repo, P, own_run_id=RUN_B, own_baseline_blob=base, foreign=[(RUN_A, base)])

    assert spawned == []
