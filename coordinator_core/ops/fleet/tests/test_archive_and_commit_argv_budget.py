"""
coordinator_core.ops.fleet.tests.test_archive_and_commit_argv_budget

The batched drift-check and restage spawns (amplification burn-down C4,
2026-08-19) pass every candidate path in ONE argv. Windows caps a
CreateProcess command line at 32767 characters TOTAL, and the paths are
ABSOLUTE, so a large enough sweep overflows it: a real 335-memo inbox
measured 35,468 characters and died with `FileNotFoundError: [WinError 206]
The filename or extension is too long`. The op surfaced that as
`-32603 Internal error` and `sweep-actioned-memos.py` reported `0` archived
at exit 0 -- indistinguishable, to /workstream-complete Step 2.65's numeric
guard, from a healthy "nothing to do" run.

The failure mode is triggered BY BACKLOG SIZE, so the sweep broke exactly
when it was most needed and an inbox past the threshold could never
mechanically shrink again. These tests pin the argv budget that fixes it.

Pure-logic, no spawn: chunking is a list operation, and pinning it here
keeps the check on the fast tier rather than behind this family's
real-git `cadence`/`spawns_process` module.
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.fleet._common import (
    _ARGV_PATHSPEC_BUDGET,
    _argv_path_chunks,
)

_WINDOWS_CMDLINE_CAP = 32767


def _argv_chars(chunk: list) -> int:
    return sum(len(token) + 1 for token in chunk)


def test_budget_leaves_headroom_under_the_windows_cap() -> None:
    assert _ARGV_PATHSPEC_BUDGET < _WINDOWS_CMDLINE_CAP
    assert _WINDOWS_CMDLINE_CAP - _ARGV_PATHSPEC_BUDGET > 4000


def test_every_chunk_fits_the_budget_at_the_observed_failing_scale() -> None:
    paths = [
        Path(r"X:\claude-klabauter\cross-repo\inbox")
        / f"2026-08-{(i % 28) + 1:02d}-doe-claude-em-a-memo-with-a-representative-slug-{i}.md"
        for i in range(335)
    ]
    unchunked = _argv_chars([str(p) for p in paths])
    assert unchunked > _WINDOWS_CMDLINE_CAP, (
        "fixture no longer reproduces the overflow it exists to pin; "
        f"got {unchunked} chars"
    )

    chunks = _argv_path_chunks(paths)
    assert len(chunks) > 1
    for chunk in chunks:
        assert _argv_chars(chunk) <= _ARGV_PATHSPEC_BUDGET


def test_chunking_preserves_every_path_in_order() -> None:
    paths = [Path(f"/repo/cross-repo/inbox/{i:04d}-{'x' * 60}.md") for i in range(500)]
    flattened = [token for chunk in _argv_path_chunks(paths) for token in chunk]
    assert flattened == [str(p) for p in paths]


def test_small_batch_stays_a_single_chunk() -> None:
    paths = [Path(f"/repo/cross-repo/inbox/{i}.md") for i in range(12)]
    assert len(_argv_path_chunks(paths)) == 1


def test_empty_input_produces_no_chunks() -> None:
    assert _argv_path_chunks([]) == []


def test_single_over_budget_path_is_not_dropped() -> None:
    monster = Path("/repo/" + ("d" * (_ARGV_PATHSPEC_BUDGET + 500)) + ".md")
    chunks = _argv_path_chunks([monster])
    assert chunks == [[str(monster)]]
