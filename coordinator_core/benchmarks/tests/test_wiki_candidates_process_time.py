"""Pins for the wiki-candidates bench: spawn kinds, baseline loader, verdict, exit codes."""

from __future__ import annotations


import pytest

from coordinator_core.benchmarks import wiki_candidates_process_time as bench

_SUFFIX = "wiki_update_candidates"
_OP = f"ceremony.{_SUFFIX}"


@pytest.mark.parametrize("argv,kind", [
    (("git", "log", "--oneline"), "log"),
    (("/usr/bin/git", "-c", "core.x=1", "--no-optional-locks", "cat-file", "--batch"), "cat-file"),
    (("git", "-C", "/r", "grep", "-F"), "grep"),
    (("git.exe", "rev-list", "HEAD"), "rev-list"),
    (("python3", "x.py"), "python3"),
    ((), "?"),
])
def test_spawn_kind_names_the_git_subcommand(argv, kind) -> None:
    assert bench.spawn_kind(argv) == kind


def test_op_suffix_refuses_a_non_ceremony_op() -> None:
    assert bench.op_suffix("ceremony.wiki_candidates") == "wiki_candidates"
    for bad in ("wiki_candidates", "plan.status", "ceremony."):
        with pytest.raises(ValueError):
            bench.op_suffix(bad)



def test_exit_codes_over_under_and_unmeasured() -> None:
    assert bench.exit_code({"pass": True, "unmeasured": False}) == 0
    assert bench.exit_code({"pass": False, "unmeasured": False}) == 1
    assert bench.exit_code({"pass": False, "unmeasured": True}) == 2
    assert bench.max_total_ms([{"total_ms": 10.0}, {"total_ms": 30.0}]) == 30.0
    assert bench.max_total_ms([{"total_ms": 10.0}, {"total_ms": None}]) is None
