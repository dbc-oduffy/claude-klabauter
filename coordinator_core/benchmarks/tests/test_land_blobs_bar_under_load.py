"""The declared-blob landing op held to the brightline (DR-344) with live sibling landings, not idle."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.benchmarks import fixture_settings_home
from coordinator_core.benchmarks import land_blobs_bar_profile as bar
from coordinator_core.ops.dispatch_emit import land_blobs_contract as contract

pytestmark = pytest.mark.complete

LIVE_SIBLINGS = 6


def test_ledger_flags_a_spawn_outside_it_and_passes_a_named_one() -> None:
    assert contract.unledgered([["git", "-c", "k=v", "commit-tree", "x"], ["git", "reset", "--", "p"]]) == []
    stray = [["git", "status", "--porcelain"], ["git.exe", "-C", "d", "diff"], ["python", "-c", "1"]]
    assert contract.unledgered(stray + [["git", "hash-object", "f"]]) == stray


def test_landing_is_under_the_bar_with_live_siblings_and_spawns_only_ledgered_git(tmp_path: Path) -> None:
    with fixture_settings_home(tmp_path):
        row = bar.run_live(tmp_path, LIVE_SIBLINGS, bar.DEFAULT_TRACKED, bar.DEFAULT_CHAIN, "op", verify=True)
    attribution = row.attribution
    assert attribution["live_siblings"] == LIVE_SIBLINGS
    assert len(attribution["live_sibling_outcomes"]) == LIVE_SIBLINGS
    assert attribution["outcome"] == "committed", attribution
    assert attribution["verified"] is True
    assert attribution["unledgered_spawns"] == [], attribution["spawn_argvs"]
    assert row.process_time_ms < contract.BAR_MS, row.process_time_ms
    assert bar.passed(row)
