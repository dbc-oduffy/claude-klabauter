"""inspect_commits keeps every `git show` argv under the OS command-line cap."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from coordinator_core import consolidate_assemble as ca


def _shas(n: int) -> list[str]:
    return [f"{i:040x}" for i in range(n)]


def _fake_show(calls: list[list[str]]):
    def run_git(args: list[str], cwd: Path) -> SimpleNamespace:
        calls.append(list(args))
        out = "".join(f"commit {s}\nAuthor: x\n\n    msg\n\n" for s in args[2:])
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    return run_git


def test_thousands_of_shas_are_split_into_bounded_argv():
    shas = _shas(5000)
    calls: list[list[str]] = []
    ca.inspect_commits(_fake_show(calls), Path("."), shas)
    assert len(calls) > 1
    assert all(sum(len(a) + 1 for a in c) < 30000 for c in calls)
    assert sorted(a for c in calls for a in c[2:]) == sorted(shas)


def test_blocks_are_complete_and_ordered_across_batches():
    shas = _shas(5000)
    blocks = ca.inspect_commits(_fake_show([]), Path("."), shas)
    assert list(blocks) == shas
    assert blocks[shas[-1]].startswith(f"commit {shas[-1]}")


def test_small_input_stays_one_spawn_and_empty_spawns_none():
    calls: list[list[str]] = []
    ca.inspect_commits(_fake_show(calls), Path("."), _shas(3))
    assert len(calls) == 1
    calls.clear()
    assert ca.inspect_commits(_fake_show(calls), Path("."), []) == {}
    assert calls == []
