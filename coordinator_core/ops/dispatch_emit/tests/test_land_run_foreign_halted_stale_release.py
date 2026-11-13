"""land_run.foreign_halted_withheld at default arguments: a halted foreign run's withheld path releases once the
path is clean in the worktree (committed by someone else) and stays held while the run's bytes are uncommitted."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.tests.test_land_run_declared_write_left_dirty import (  # noqa: F401
    CODE, _git, _write, repo,
)
from coordinator_core.ops.dispatch_emit.tests.test_land_run_stale_halted_holder import _halted_977c47

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

LANDER = "ask-20261030T223203-aaaaaa"
OTHER = "coordinator_core/other.py"


def test_a_withheld_path_committed_by_someone_else_is_not_withheld(repo):
    _halted_977c47(repo, 300)
    _git(repo, "add", CODE)
    _git(repo, "commit", "-q", "-m", "someone else landed it")
    assert land_run.foreign_halted_withheld(repo, LANDER, None, [CODE]) == {}


def test_a_withheld_path_with_uncommitted_bytes_still_protects_it(repo):
    holder = _halted_977c47(repo, 300)
    assert land_run.foreign_halted_withheld(repo, LANDER, None, [CODE]) == {CODE: holder}


def test_a_withheld_path_restored_to_head_is_not_withheld(repo):
    _halted_977c47(repo, 300)
    _git(repo, "checkout", "--", CODE)
    assert land_run.foreign_halted_withheld(repo, LANDER, None, [CODE]) == {}


def test_release_is_per_path_a_clean_sibling_releases_while_the_dirty_one_holds(repo):
    holder = _halted_977c47(repo, 300)
    _write(repo, OTHER, "x = 0\n")
    _git(repo, "add", OTHER)
    _git(repo, "commit", "-q", "-m", "other")
    halted = repo / "state/warp" / holder / "halted.json"
    marker = json.loads(halted.read_text(encoding="utf-8"))
    marker["run_artifacts"]["withheld"][OTHER] = "halt-record-only"
    halted.write_text(json.dumps(marker), encoding="utf-8")
    assert land_run.foreign_halted_withheld(repo, LANDER, None, [CODE, OTHER]) == {CODE: holder}


def test_a_staged_but_uncommitted_withheld_path_still_protects_it(repo):
    holder = _halted_977c47(repo, 300)
    _git(repo, "add", CODE)
    assert land_run.foreign_halted_withheld(repo, LANDER, None, [CODE]) == {CODE: holder}


def test_an_expired_halt_releases_even_while_its_bytes_are_still_dirty(repo):
    _halted_977c47(repo, 3 * 3600)
    assert land_run.foreign_halted_withheld(repo, LANDER, None, [CODE]) == {}
