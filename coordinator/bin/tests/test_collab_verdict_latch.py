"""Tests for the collaboration verdict latch: merge_verdict truth table and corrupt handling."""
import importlib.util
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parents[2] / "lib"


def _load(name):
    spec = importlib.util.spec_from_file_location(name + "_latch", _LIB / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


cd = _load("collab_detect")
V = _load("collab_gate").Verdict


def v(mode, source="detected", n=1, at="2026-01-01T00:00:00Z"):
    return V(mode=mode, source=source, author_count=n, checked_at=at)


@pytest.mark.parametrize("prev_source", ["detected", "override", "unresolved", "corrupt"])
def test_solo_to_multi_transitions_freely(prev_source):
    obs = v("multi", n=2)
    assert cd.merge_verdict(v("solo", prev_source), obs) == obs


def test_multi_is_absorbing_under_auto():
    prev = v("multi", n=2)
    assert cd.merge_verdict(prev, v("solo", n=1)) == prev
    assert cd.merge_verdict(prev, v("solo", n=1), "auto") == prev
    assert cd.merge_verdict(prev, v("solo", n=1), "garbage") == prev


def test_multi_override_previous_is_also_absorbing():
    prev = v("multi", "override", n=2)
    assert cd.merge_verdict(prev, v("solo")) == prev


def test_explicit_solo_override_lowers_multi():
    out = cd.merge_verdict(v("multi", n=2), v("solo", n=1), "solo")
    assert (out.mode, out.source) == ("solo", "override")


def test_solo_override_beats_observed_multi():
    out = cd.merge_verdict(v("solo"), v("multi", n=3), "solo")
    assert (out.mode, out.source) == ("solo", "override")


def test_multi_override_raises_solo():
    out = cd.merge_verdict(v("solo"), v("solo"), "multi")
    assert (out.mode, out.source) == ("multi", "override")


@pytest.mark.parametrize("prev_source", ["unresolved", "corrupt"])
def test_unresolved_or_corrupt_previous_never_lowers_multi(prev_source):
    obs = v("multi", n=2)
    assert cd.merge_verdict(v("solo", prev_source, n=0, at=""), obs).mode == "multi"


def test_solo_stays_solo():
    obs = v("solo")
    assert cd.merge_verdict(v("solo"), obs) == obs


def test_corrupt_previous_is_loud_and_distinct_from_unresolved():
    with pytest.raises(cd.CorruptVerdictError):
        cd.require_readable(v("solo", "corrupt", n=0, at=""))
    unresolved = v("solo", "unresolved", n=0, at="")
    assert cd.require_readable(unresolved) is unresolved


def test_merge_does_not_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(cd.subprocess, "run", boom)
    prev = v("multi")
    assert cd.merge_verdict(prev, v("solo")) == prev
