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

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]

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


def test_pre_run_dirt_the_run_rewrote_is_adopted_as_its_own(repo):
    _put(repo, _rows({60: "DIRT"}))
    base_b = wb.capture(repo, [P])[P]
    _put(repo, _rows({60: "B-EDIT"}))

    got = hunk_stage.split(repo, P, own_run_id=RUN_B, own_baseline_blob=base_b, foreign=[])

    assert got.overlaps == []
    assert _staged_text(repo, got.staged_blob) == _rows({60: "B-EDIT"})


def test_pre_run_dirt_beside_our_hunk_in_one_block_still_refuses_unattributed(repo):
    _put(repo, _rows({60: "DIRT"}))
    base_b = wb.capture(repo, [P])[P]
    text = _rows({60: "DIRT"}).replace("x_61 = 61\n", "x_61 = 'B-1'\nx_61b = 'B-2'\n")
    _put(repo, text)

    got = hunk_stage.split(repo, P, own_run_id=RUN_B, own_baseline_blob=base_b, foreign=[])

    assert got.staged_blob is None
    assert [run for run, _ in got.overlaps] == [None]


def test_dirt_a_foreign_run_owns_that_we_rewrote_still_refuses_naming_it(repo):
    _put(repo, _rows({60: "A-EDIT"}))
    base_a = wb.capture(repo, [P])[P]
    base_b = wb.capture(repo, [P])[P]
    _put(repo, _rows({60: "B-EDIT"}))

    got = hunk_stage.split(
        repo, P, own_run_id=RUN_B, own_baseline_blob=base_b, foreign=[(RUN_A, base_a)]
    )

    assert got.staged_blob is None
    assert [run for run, _ in got.overlaps] == [RUN_A]


def test_an_insertion_beside_pre_run_inserted_dirt_stages_only_our_lines(repo):
    head = _rows({})
    _put(repo, head + "dirt_1 = 1\ndirt_2 = 2\n")
    base_b = wb.capture(repo, [P])[P]
    _put(repo, head + "dirt_1 = 1\ndirt_2 = 2\nown_1 = 1\n")

    got = hunk_stage.split(repo, P, own_run_id=RUN_B, own_baseline_blob=base_b, foreign=[])

    assert got.overlaps == []
    assert _staged_text(repo, got.staged_blob) == head + "own_1 = 1\n"


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


def _count_opcodes(monkeypatch):
    from coordinator_core.git import since_base_overlap as sbo

    calls = []
    real = sbo._opcodes

    def counting(a, b):
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(sbo, "_opcodes", counting)
    return calls


def test_a_new_file_stages_whole_with_no_line_diff(repo, monkeypatch):
    new = "pkg/new.json"
    from coordinator_core.git import git_objects

    base = git_objects.write_object(resolve_git_common_dir(repo), b"blob", b"")
    body = "".join(f'  "k{i}": 0,\n' for i in range(2000))
    (repo / new).write_bytes(body.encode())
    calls = _count_opcodes(monkeypatch)

    got = hunk_stage.split(repo, new, own_run_id=RUN_A, own_baseline_blob=base, foreign=[])

    assert calls == []
    assert got.overlaps == []
    assert _staged_text(repo, got.staged_blob) == body
    assert got.own_headers == ["@@ -0,0 +1,2000 @@"]


def test_an_untracked_file_with_a_draft_baseline_stages_only_the_runs_lines(repo):
    new = "pkg/draft.txt"
    from coordinator_core.git import git_objects

    common = resolve_git_common_dir(repo)
    base = git_objects.write_object(common, b"blob", b"draft-1\ndraft-2\n")
    (repo / new).write_bytes(b"draft-1\ndraft-2\nrun-1\n")

    got = hunk_stage.split(repo, new, own_run_id=RUN_A, own_baseline_blob=base, foreign=[])

    assert got.overlaps == []
    assert _staged_text(repo, got.staged_blob) == "run-1\n"


def test_own_baseline_equal_to_head_with_no_foreign_stages_whole_with_no_line_diff(repo, monkeypatch):
    base = wb.capture(repo, [P])[P]
    _put(repo, _rows({5: "A", 115: "B"}))
    calls = _count_opcodes(monkeypatch)

    got = hunk_stage.split(repo, P, own_run_id=RUN_A, own_baseline_blob=base, foreign=[])

    assert calls == []
    assert _staged_text(repo, got.staged_blob) == _rows({5: "A", 115: "B"})


def _commit_flat(repo) -> str:
    flat = "".join("  0,\n" for _ in range(1000))
    (repo / P).write_bytes(flat.encode())
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "flat")
    return flat


def test_a_coarse_rewrite_with_no_foreign_hunk_is_attributed_whole(repo):
    flat = _commit_flat(repo)
    base = wb.capture(repo, [P])[P]
    rows = flat.splitlines(keepends=True)
    for k in range(100, 900):
        rows[k] = "  1,\n"
    (repo / P).write_bytes("".join(rows).encode())

    got = hunk_stage.split(
        repo, P, own_run_id=RUN_B, own_baseline_blob=base, foreign=[(RUN_A, base)]
    )

    assert got.overlaps == []
    assert _staged_text(repo, got.staged_blob) == "".join(rows)


def test_a_foreign_hunk_sharing_a_coarse_block_refuses_rather_than_line_matching(repo):
    flat = _commit_flat(repo)
    base_a = wb.capture(repo, [P])[P]
    rows = flat.splitlines(keepends=True)
    rows[50] = "  A,\n"
    (repo / P).write_bytes("".join(rows).encode())
    base_b = wb.capture(repo, [P])[P]
    for k in range(100, 900):
        rows[k] = "  1,\n"
    (repo / P).write_bytes("".join(rows).encode())

    got = hunk_stage.split(
        repo, P, own_run_id=RUN_B, own_baseline_blob=base_b, foreign=[(RUN_A, base_a)]
    )

    assert got.staged_blob is None
    assert got.overlaps


def _splice(text: str, at: int, new: list[str]) -> str:
    rows = text.splitlines(keepends=True)
    return "".join(rows[:at] + new + rows[at:])


def test_a_pure_addition_after_the_runs_shifting_hunks_stages_beside_pre_run_inserted_dirt(repo):
    head = _rows({})
    dirt = ["dirt_1 = 1\n", "dirt_2 = 2\n"]
    _put(repo, _splice(head, 100, dirt))
    base_b = wb.capture(repo, [P])[P]
    shifted = _splice(_splice(head, 100, dirt), 30, ["early_1 = 1\n", "early_2 = 2\n"])
    own = [f"own_{i} = {i}\n" for i in range(5)]
    _put(repo, _splice(shifted, 102, own))

    got = hunk_stage.split(repo, P, own_run_id=RUN_B, own_baseline_blob=base_b, foreign=[])

    assert got.overlaps == []
    assert got.staged_blob is not None
    want = _splice(_splice(head, 100, own), 30, ["early_1 = 1\n", "early_2 = 2\n"])
    assert _staged_text(repo, got.staged_blob) == want
    assert any(h.startswith("@@ -100,0 ") for h in got.own_headers)


def test_a_pure_addition_beside_pre_run_dirt_that_rewrites_a_line_still_refuses(repo):
    _put(repo, _rows({101: "DIRT"}))
    base_b = wb.capture(repo, [P])[P]
    text = _rows({101: "DIRT"}).replace("x_100 = 100\n", "x_100 = 100\nown_1 = 1\nown_2 = 2\n")
    _put(repo, text)

    got = hunk_stage.split(repo, P, own_run_id=RUN_B, own_baseline_blob=base_b, foreign=[])

    assert got.staged_blob is None
    assert [run for run, _ in got.overlaps] == [None]
