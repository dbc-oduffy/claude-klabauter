"""workflow.revise run_live: a sibling run's token minted before its run was superseded or halted holds no driver."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from coordinator_core import warp_live
from coordinator_core.ops.dispatch_emit import halted_run, run_token
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

RUN = "ask-20261119T201243-c4821f"
SIBLING = "warp-2026-11-19-x-20261119T214108-47b8c9"
REL = "state/sizings/2026-11-19-x.yaml"
SCRIPT = f"scratch/warp/{SIBLING}.workflow.mjs"


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    sizing = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL, ask_text="a raw ask")
    return tmp_path


def _sibling_token(root: Path) -> None:
    run_token.mint(root, SIBLING, SCRIPT, session_id="s1")
    assert run_token.bind_sizing(root, SIBLING, REL)


def _end(root: Path, how: str) -> None:
    rd = record_dir(root, SIBLING)
    halted_run.write_halt_marker(rd, halted="ledger-check", sizing_rel=REL)
    if how == "superseded":
        assert halted_run.mark_superseded_by_refire(rd, prior_engine=None, engine=None, at="2026-11-20T14:50:01+00:00")
    # The marker is written after the token: pin that ordering whatever the clock resolution.
    stamp = run_token.minted_at(root, SIBLING) + 5
    os.utime(rd / halted_run.HALTED_FILE, (stamp, stamp))


def _revise(root: Path) -> dict:
    return rl._handler({"run_id": RUN, "verbatim": "also cover Windows"}, repo_root=root)


@pytest.fixture(autouse=True)
def _runner_not_disproven(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(warp_live, "runner_unproven", lambda _r, _id: False)


@pytest.mark.parametrize("how", ["superseded", "halted"])
def test_sibling_token_minted_before_its_run_ended_is_not_live(tmp_path: Path, how: str) -> None:
    root = _repo(tmp_path)
    _sibling_token(root)
    _end(root, how)
    assert _revise(root)["run_live"] is False


def test_sibling_token_of_a_run_never_ended_stays_live(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _sibling_token(root)
    assert _revise(root)["run_live"] is True


def test_sibling_token_re_minted_after_the_marker_stays_live(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _sibling_token(root)
    _end(root, "superseded")
    rd = record_dir(root, SIBLING)
    old = run_token.minted_at(root, SIBLING) - 60
    os.utime(rd / halted_run.HALTED_FILE, (old, old))
    assert _revise(root)["run_live"] is True
