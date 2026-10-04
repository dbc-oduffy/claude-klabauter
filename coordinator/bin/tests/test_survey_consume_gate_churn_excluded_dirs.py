"""`_run_churn` never reaches `_invoke_op` without an `excluded_dirs` set.

A missing set is a discriminated skip (`missing-excludedDirs`) carrying no
producer claim; a supplied set passes through to the op unchanged.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

GATE_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "survey-consume-gate.py"


@pytest.fixture
def gate():
    spec = importlib.util.spec_from_file_location(
        "survey_consume_gate_churn_excluded_dirs", GATE_SCRIPT_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["survey_consume_gate_churn_excluded_dirs"] = mod
    spec.loader.exec_module(mod)
    return mod


def _config(**overrides):
    cfg = {
        "mode": "refresh",
        "repo_root": "/repo",
        "claude_klabauter_root": "/claude-klabauter",
        "since": "2026-01-01",
        "system_dirs": ["coordinator_core"],
    }
    cfg.update(overrides)
    return cfg


def _fail_if_invoked(gate, monkeypatch):
    def _boom(*args, **kwargs):
        pytest.fail("_invoke_op reached without excluded_dirs")

    monkeypatch.setattr(gate, "_invoke_op", _boom)


@pytest.mark.parametrize("missing", [{}, {"excluded_dirs": None}, {"excluded_dirs": []}])
def test_missing_excluded_dirs_is_a_discriminated_skip(gate, monkeypatch, missing):
    _fail_if_invoked(gate, monkeypatch)

    out = gate._run_churn(_config(**missing))

    assert out == {"skipped": True, "reason": "missing-excludedDirs"}


def test_missing_since_outranks_missing_excluded_dirs(gate, monkeypatch):
    _fail_if_invoked(gate, monkeypatch)

    out = gate._run_churn(_config(since=None))

    assert out == {"skipped": True, "reason": "missing-since-or-systemDirs"}


def test_supplied_excluded_dirs_passes_through_to_the_op(gate, monkeypatch):
    seen = {}
    payload = {"emergent": [], "excluded_by_prefilter": [], "deleted_at_head": []}

    def _capture(root, op, params):
        seen["op"] = op
        seen["params"] = params
        return 0, payload, None

    monkeypatch.setattr(gate, "_invoke_op", _capture)

    out = gate._run_churn(_config(excluded_dirs=["state", "cross-repo"]))

    assert out is payload
    assert seen["op"] == "cartography.churn"
    assert seen["params"]["excluded_dirs"] == ["state", "cross-repo"]
