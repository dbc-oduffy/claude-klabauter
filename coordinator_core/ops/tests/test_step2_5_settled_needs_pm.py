"""`--settled` makes an ambiguous-only NEEDS-PM pass; unsettled and non-ambiguous still stop."""

from __future__ import annotations

import pytest

from coordinator_core.ops import workday_complete_step2_5_dirty_tree as mod


def _classification(*, source=0, peer=0, ambiguous=0):
    counters = mod._Counters()
    counters.source = source
    counters.claim_peer = peer
    counters.ambiguous = ambiguous
    return mod.DirtyTreeClassification(
        needs_pm=True, error=None, counters=counters, acc=mod._Accumulators()
    )


def _run(monkeypatch, classification, argv):
    monkeypatch.setattr(mod, "classify_dirty_tree", lambda cwd=None: classification)
    monkeypatch.setattr(mod, "show_toplevel", lambda cwd=None: "/repo")
    return mod.main(argv)


def test_unsettled_ambiguous_still_stops(monkeypatch):
    assert _run(monkeypatch, _classification(ambiguous=3), []) == 2


def test_settled_ambiguous_passes(monkeypatch, capsys):
    assert _run(monkeypatch, _classification(ambiguous=3), ["--settled"]) == 0
    assert "NEEDS-PM" in capsys.readouterr().out


@pytest.mark.parametrize("kw", [{"source": 1}, {"peer": 1}])
def test_settled_does_not_cover_source_or_peer(monkeypatch, kw):
    assert _run(monkeypatch, _classification(ambiguous=3, **kw), ["--settled"]) == 2
