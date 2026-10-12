"""`_run_churn` never invokes an op: refresh mode reports the producer-not-in-claude-klabauter skip.

The skip holds whatever `since`, `system_dirs` or `excluded_dirs` the config carries.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

GATE_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "survey-consume-gate.py"
SKIP = {"skipped": True, "reason": "producer-not-in-claude-klabauter"}


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
        pytest.fail("_invoke_op reached from _run_churn")

    monkeypatch.setattr(gate, "_invoke_op", _boom)


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"excluded_dirs": None},
        {"excluded_dirs": ["state", "cross-repo"]},
        {"since": None},
        {"system_dirs": None},
    ],
)
def test_refresh_mode_is_the_producer_skip_and_invokes_no_op(gate, monkeypatch, overrides):
    _fail_if_invoked(gate, monkeypatch)

    assert gate._run_churn(_config(**overrides)) == SKIP


@pytest.mark.parametrize("mode", ["first-run", "targeted", None])
def test_outside_refresh_returns_none(gate, monkeypatch, mode):
    _fail_if_invoked(gate, monkeypatch)

    assert gate._run_churn(_config(mode=mode)) is None
