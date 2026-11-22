"""rulings.gate dispatched through coordinator_core.ipc at default arguments."""
from __future__ import annotations

import asyncio
import subprocess

import pytest

from coordinator_core import ipc
from coordinator_core.ops.rulings import gate  # noqa: F401  (registers the op)
from coordinator_core.rulings import store
from coordinator_core.rulings.contract import Ruling


@pytest.fixture
def root(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    store.append(
        tmp_path,
        Ruling(
            id="no-pending-pm",
            pm_turn="state/sizings/x.yaml R2",
            scope=("plan.prep_gate",),
            text="a pending PM ruling is not a state",
            recorded="2026-11-21",
        ),
    )
    monkeypatch.setattr(ipc, "_STAMP_GATE_ARMED", False)
    # Scope table entry is the op-catalog leg's; setitem is a no-op once it lands.
    monkeypatch.setitem(ipc._OP_KEY_SCOPE, "rulings.gate", "common_dir")

    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    return tmp_path


def _call(root, params):
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "rulings.gate",
        "params": params,
        "_origin_worktree": str(root),
    }
    return asyncio.run(ipc.dispatch_message(msg))


DEFERRING = "plan.prep_gate scope is pending a PM ruling on thresholds?"


def test_defer_refuses_and_names_route(root):
    res = _call(root, {"body": DEFERRING})["result"]
    assert res["verdict"] == "refuse"
    assert res["deferred"] == ["no-pending-pm"]
    assert res["route"] == "coordinator:apm"


def test_newer_pm_turn_passes(root):
    (root / "turn.md").write_text("pm", encoding="utf-8")
    body = DEFERRING + "\npm_turn: turn.md 2026-11-22 no-pending-pm\n"
    assert _call(root, {"body": body})["result"]["verdict"] == "pass"


def test_contradicting_body_refuses(root):
    res = _call(root, {"body": "We withdraw no-pending-pm."})["result"]
    assert res["verdict"] == "refuse"
    assert res["contradicted"] == ["no-pending-pm"]


def test_path_param_reads_one_file_and_escape_is_refused(root, tmp_path):
    (root / "plan.md").write_text(DEFERRING, encoding="utf-8")
    assert _call(root, {"path": "plan.md"})["result"]["deferred"] == ["no-pending-pm"]
    assert "error" in _call(root, {"path": "../outside.md"})


def test_exactly_one_of_body_or_path(root):
    assert "error" in _call(root, {})
    assert "error" in _call(root, {"body": "x", "path": "plan.md"})
    assert "error" in _call(root, {"body": "x", "extra": 1})


def test_reraising_a_settled_licence_gate_is_refused(root):
    store.append(
        root,
        Ruling(
            id="private-repo-stop-relitigating",
            pm_turn="state/sizings/x.yaml R5",
            scope=("licence", "license", "publication", "private"),
            text="this is a private repo; stop relitigating",
            recorded="2026-11-21",
        ),
    )
    raised = _call(root, {"body": "Whether a licence gate applies is pending a PM ruling: public or private?"})
    assert raised["result"]["deferred"] == ["private-repo-stop-relitigating"]
    unrelated = _call(root, {"body": "Logo colour is pending a PM ruling: red or blue?"})
    assert unrelated["result"]["verdict"] == "pass"


def test_empty_register_passes_a_named_question(root):
    for f in (root / "state" / "pm-rulings").glob("*.yaml"):
        f.unlink()
    assert _call(root, {"body": "Logo colour pending a PM ruling: red or blue?"})["result"][
        "verdict"
    ] == "pass"
