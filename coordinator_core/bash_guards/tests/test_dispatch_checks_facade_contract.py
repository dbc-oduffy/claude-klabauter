"""Facade contract for ``dispatch_checks``: every baseline name stays importable
and, once names move to ``_dc_*`` modules, stays the same object as its home.

The fixture is generated once at the pre-split baseline and never regenerated.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _alternative_liveness
from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.bash_guards import roster

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "dispatch_checks_facade_contract.json").read_text(
        encoding="utf-8"
    )
)
_DC_PREFIX = "coordinator_core.bash_guards._dc_"


def test_every_baseline_name_is_gettable_on_the_facade():
    missing = [n for n in _FIXTURE["names"] if not hasattr(dispatch_checks, n)]
    assert not missing, f"names missing from the dispatch_checks facade: {missing}"


def test_facade_objects_are_their_home_module_objects():
    mismatched = []
    for name in _FIXTURE["names"]:
        obj = getattr(dispatch_checks, name, None)
        if not (inspect.isfunction(obj) or inspect.isclass(obj)):
            continue
        home = getattr(obj, "__module__", "") or ""
        if not home.startswith(_DC_PREFIX):
            continue
        if getattr(sys.modules[home], name, None) is not obj:
            mismatched.append((name, home))
    assert not mismatched, f"facade object is not the home-module object: {mismatched}"


def test_roster_script_tails_match_the_baseline():
    actual = {e.id: e.script for e in roster.guard_roster()}
    expected = _FIXTURE["roster_tails"]
    changed = {
        k: (expected[k], actual.get(k))
        for k in expected
        if actual.get(k) != expected[k]
    }
    assert not changed, f"roster script tails drifted from the baseline: {changed}"


def test_dispatch_check_discovery_matches_the_baseline():
    assert _alternative_liveness.discover_dispatch_check_names() == _FIXTURE[
        "dispatch_check_names"
    ]


@pytest.mark.parametrize("key", ["names", "dispatch_check_names"])
def test_fixture_lists_are_sorted(key):
    assert _FIXTURE[key] == sorted(_FIXTURE[key])
