"""A halted earlier attempt of the landing run's own sizing is kin in `_split_free_targets`; an unrelated halted run still holds."""
from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import halted_hold_release, halted_withheld_index, land_run, peer_hunk_gate
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

SIZING = "state/sizings/2026-11/a.yaml"
TARGET = "coordinator_core/mod.py"


def _bind(root, rid, **fields):
    d = rl.run_dir(root, rid)
    d.mkdir(parents=True, exist_ok=True)
    (d / rl.RUN_FILE).write_text(json.dumps(fields), encoding="utf-8")


@pytest.fixture
def split(tmp_path, monkeypatch):
    def run(halted_by: str, **kw):
        monkeypatch.setattr(halted_withheld_index, "withheld_by_path", lambda root: {TARGET: halted_by})
        monkeypatch.setattr(halted_hold_release, "still_held_by_index", lambda root, holders, *a, **k: dict(holders))
        monkeypatch.setattr(land_run, "_peer_held", lambda *a, **k: {})
        monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [])
        return land_run._split_free_targets(
            tmp_path, {TARGET}, barred=(), session_id="s", rid="r-new", script_path=None, plan_rel=None,
            run_base=None, minted_before=None, own_run_ids=("r-new", None), **kw,
        )

    return run


def test_same_sizing_predecessor_is_kin(tmp_path, split):
    _bind(tmp_path, "r-new", sizing_rel=SIZING)
    _bind(tmp_path, "r-old", sizing_rel=SIZING)
    assert split("r-old") == ([TARGET], {})


def test_resumed_from_predecessor_is_kin(tmp_path, split):
    _bind(tmp_path, "r-new", resumed_from="r-old")
    _bind(tmp_path, "r-old")
    assert split("r-old") == ([TARGET], {})


def test_unrelated_sizing_still_holds(tmp_path, split):
    _bind(tmp_path, "r-new", sizing_rel=SIZING)
    _bind(tmp_path, "r-other", sizing_rel="state/sizings/2026-11/b.yaml")
    assert split("r-other") == ([], {TARGET: "r-other"})


def test_unbound_halted_run_still_holds(tmp_path, split):
    _bind(tmp_path, "r-new", sizing_rel=SIZING)
    assert split("r-ghost") == ([], {TARGET: "r-ghost"})


def test_kin_is_not_advisory_but_unrelated_is(tmp_path, split):
    _bind(tmp_path, "r-new", sizing_rel=SIZING)
    _bind(tmp_path, "r-other", sizing_rel="state/sizings/2026-11/b.yaml")
    advisory: dict = {}
    assert split("r-other", halted_advisory=advisory) == ([TARGET], {})
    assert advisory == {TARGET: "r-other"}
    _bind(tmp_path, "r-old", sizing_rel=SIZING)
    advisory = {}
    assert split("r-old", halted_advisory=advisory) == ([TARGET], {})
    assert advisory == {}
