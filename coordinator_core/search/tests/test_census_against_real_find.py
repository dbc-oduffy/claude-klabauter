"""Tests for `coordinator_core.search.census` that assert against REAL `find`.

AC13's differential oracle for C1 (docs/plans/2026-08-21-the-advisory-band-gets-
smaller-cheaper-and-honest.md): the in-process evaluator and the real `find` pipeline
run over the SAME fixture trees, and the two are asserted identical (sorted, per
`census.py`'s own documented choice to leave sorting to the caller, matching
`guard_head_tail_rewrite`'s SORTED-for-determinism policy) -- never a faked oracle.
Methodology transfers from `coordinator_core/git/tests/test_git_state_against_real_
git.py`: every test here spawns a real `find` binary through a module-level helper,
so this file carries the module-level `pytestmark` the spawn ratchet requires (a
per-function mark is inert to it -- see AC11), tiered onto `cadence` on its own,
away from the plain-Python parser tests in `test_census_declines.py`-shaped
counterparts colocated with this module's own recognition unit tests.

Corpus, minimum per AC13: traversal order (sorted comparison, not raw order --
`find`'s own order is not guaranteed reproducible, same choice the generator this
module replaces already made); a bare path; `-name` glob semantics; `-type f`;
hidden files; a `-type d` shape that must DECLINE (Unanswerable), not answer; an
`-exec`/redirection/substitution shape that must also decline; and an empty result.

Negative spec: nothing here may be de-tiered by faking `find`. A test that stops
needing the real binary belongs beside `census.py`'s own parser unit tests, not here.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from coordinator_core.search import census  # noqa: E402
from coordinator_core.search.engine import Unanswerable  # noqa: E402
from coordinator_core.win_portability import no_console_creationflags  # noqa: E402

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_HAS_FIND = shutil.which("find") is not None


def _real_find(args, cwd):
    proc = subprocess.run(
        ["find"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    return sorted(line for line in proc.stdout.splitlines() if line)


def _fixture_tree(tmp_path):
    (tmp_path / "a.txt").write_text("a\n")
    (tmp_path / "b.txt").write_text("b\n")
    (tmp_path / "c.log").write_text("c\n")
    (tmp_path / ".hidden.txt").write_text("hidden\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "d.txt").write_text("d\n")
    (sub / "e.log").write_text("e\n")
    empty = tmp_path / "empty"
    empty.mkdir()
    return tmp_path


@pytest.mark.skipif(not _HAS_FIND, reason="real `find` binary not on PATH")
class TestCensusMatchesRealFind:
    def test_bare_path_all_entries_matches(self, tmp_path):
        _fixture_tree(tmp_path)
        spec = census.parse_find_census_segment(["find", "."])
        got = sorted(census.run(spec, cwd=tmp_path))
        want = _real_find(["."], cwd=tmp_path)
        assert got == want

    def test_name_glob_matches(self, tmp_path):
        _fixture_tree(tmp_path)
        spec = census.parse_find_census_segment(["find", ".", "-name", "*.txt"])
        got = sorted(census.run(spec, cwd=tmp_path))
        want = _real_find([".", "-name", "*.txt"], cwd=tmp_path)
        assert got == want
        assert got  # non-empty -- the match actually exercises something

    def test_type_f_matches(self, tmp_path):
        _fixture_tree(tmp_path)
        spec = census.parse_find_census_segment(["find", ".", "-type", "f"])
        got = sorted(census.run(spec, cwd=tmp_path))
        want = _real_find([".", "-type", "f"], cwd=tmp_path)
        assert got == want

    def test_name_and_type_f_combined_matches(self, tmp_path):
        _fixture_tree(tmp_path)
        spec = census.parse_find_census_segment(
            ["find", ".", "-name", "*.txt", "-type", "f"]
        )
        got = sorted(census.run(spec, cwd=tmp_path))
        want = _real_find([".", "-name", "*.txt", "-type", "f"], cwd=tmp_path)
        assert got == want

    def test_hidden_files_included_like_real_find(self, tmp_path):
        _fixture_tree(tmp_path)
        spec = census.parse_find_census_segment(["find", ".", "-name", ".hidden.txt"])
        got = sorted(census.run(spec, cwd=tmp_path))
        want = _real_find([".", "-name", ".hidden.txt"], cwd=tmp_path)
        assert got == want
        assert got

    def test_empty_result_matches(self, tmp_path):
        _fixture_tree(tmp_path)
        spec = census.parse_find_census_segment(["find", ".", "-name", "*.nope"])
        got = sorted(census.run(spec, cwd=tmp_path))
        want = _real_find([".", "-name", "*.nope"], cwd=tmp_path)
        assert got == want == []

    def test_named_subdirectory_matches(self, tmp_path):
        _fixture_tree(tmp_path)
        spec = census.parse_find_census_segment(["find", "sub", "-type", "f"])
        got = sorted(census.run(spec, cwd=tmp_path))
        want = _real_find(["sub", "-type", "f"], cwd=tmp_path)
        assert got == want


class TestCensusDeclinesUncertifiedShapes:
    """Corpus-uncovered per C0's spike -- must stay `Unanswerable` (AC4/AC13),
    never guess at an equivalent."""

    def test_type_d_declines(self, tmp_path):
        _fixture_tree(tmp_path)
        with pytest.raises(Unanswerable):
            census.parse_find_census_segment(["find", ".", "-type", "d"])

    def test_exec_declines(self, tmp_path):
        with pytest.raises(Unanswerable):
            census.parse_find_census_segment(["find", ".", "-exec", "rm", "{}", ";"])

    def test_redirection_declines(self, tmp_path):
        with pytest.raises(Unanswerable):
            census.parse_find_census_segment(["find", ".", "2>/dev/null"])

    def test_substitution_declines(self, tmp_path):
        with pytest.raises(Unanswerable):
            census.parse_find_census_segment(["find", "$(pwd)"])

    def test_unrecognized_predicate_declines(self, tmp_path):
        with pytest.raises(Unanswerable):
            census.parse_find_census_segment(["find", ".", "-maxdepth", "1"])

    def test_multiple_name_declines(self, tmp_path):
        with pytest.raises(Unanswerable):
            census.parse_find_census_segment(
                ["find", ".", "-name", "*.txt", "-name", "*.log"]
            )

    def test_nonexistent_path_declines(self, tmp_path):
        spec = census.parse_find_census_segment(["find", "does-not-exist"])
        with pytest.raises(Unanswerable):
            census.run(spec, cwd=tmp_path)

    def test_walk_budget_exceeded_declines(self, tmp_path, monkeypatch):
        _fixture_tree(tmp_path)
        monkeypatch.setattr(census, "WALK_BUDGET_ENTRIES", 0)
        spec = census.parse_find_census_segment(["find", "."])
        with pytest.raises(Unanswerable):
            census.run(spec, cwd=tmp_path)
