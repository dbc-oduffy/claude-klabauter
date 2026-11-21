"""Triage verdicts: isolation rerun for test rows, HEAD re-verify for non-test rows."""

from __future__ import annotations


import pytest

from coordinator_core.grind_triage import triage as triage_mod
from coordinator_core.grind_triage.contract import TriageRow, Verdict
from pathlib import Path


def _by_key(results):
    return {r.key: r for r in results}


def test_unreadable_head_survives(tmp_path: Path) -> None:
    rows = [TriageRow(key="x", title="x", is_test=False, target="a.py")]
    res = _by_key(triage_mod.triage(rows, tmp_path))
    assert res["x"].verdict is Verdict.SURVIVOR
    assert res["x"].evidence == "HEAD unreadable"
