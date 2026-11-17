"""A {sizing} refire never adopts a mint token whose run dir is absent: it consumes it and mints fresh."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests.test_submit_engine_ask import _Emit, _hso, _payload, _repo
from coordinator_core.ops.dispatch_emit import run_token

pytestmark = pytest.mark.complete

SIZING = "state/sizings/2026-11/2026-11-16-750ed8-1-distill-launch-args-scriptpath.yaml"
DEAD_SESSION = "a7c4697f-945a-4fff-9d5c-ee4f41cc945a"
STRAY_IDS = (
    "ask-20261116T120100-aaaaa1",
    "ask-20261116T120200-aaaaa2",
    "ask-20261116T120300-aaaaa3",
)
HELD_ID = "ask-20261116T120400-bbbbb1"
FRESH_ID = "ask-20261013T000000-abcdef"  # the id `_Emit` mints


def _mint_dead(root: Path, run_id: str) -> None:
    run_token.mint(root, run_id, f"scratch/warp/{run_id}.workflow.mjs", session_id=DEAD_SESSION, sizing_rel=SIZING)


def _age(root: Path, run_id: str, seconds: float) -> None:
    path = run_token._record_path(root, run_id)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["minted_at"] = time.time() - seconds
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")


def _live_ids(root: Path) -> set:
    return {r["run_id"] for r in run_token.live_records(root)}


def test_refire_discards_dead_session_dirless_tokens_and_mints_fresh_in_the_same_call(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    for run_id in STRAY_IDS:
        _mint_dead(root, run_id)
        _age(root, run_id, 3600)
        assert not (root / "state" / "warp" / run_id).exists()
    emit = _Emit()

    out = mod.evaluate(_payload(root, {"sizing": SIZING}), emit=emit)

    assert _hso(out)["permissionDecision"] == "allow"
    assert len(emit.calls) == 1
    assert "defer" not in json.dumps(out) and not any(i in json.dumps(out) for i in STRAY_IDS)
    assert _live_ids(root) == {FRESH_ID}


def test_a_dirless_token_of_another_sizing_is_left_alone(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    other = "ask-20261116T120500-ccccc1"
    run_token.mint(root, other, f"scratch/warp/{other}.workflow.mjs", session_id=DEAD_SESSION,
                   sizing_rel="state/sizings/2026-11/2026-11-16-unrelated.yaml")
    _age(root, other, 3600)

    mod.evaluate(_payload(root, {"sizing": SIZING}), emit=_Emit())

    assert other in _live_ids(root)


def test_a_young_dirless_token_is_kept_inside_the_grace(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _mint_dead(root, HELD_ID)

    assert mod._runner_less_run(root, SIZING, None) is None
    assert HELD_ID in _live_ids(root)


def test_a_minted_run_with_a_dir_past_grace_still_resumes_via_the_minted_start_envelope(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _mint_dead(root, HELD_ID)
    _age(root, HELD_ID, 3600)
    rd = root / "state" / "warp" / HELD_ID
    rd.mkdir(parents=True)
    (rd / "run-stage.json").write_text(json.dumps({"stage": "minted"}), encoding="utf-8")
    emit = _Emit()

    out = mod.evaluate(_payload(root, {"sizing": SIZING}), emit=emit)

    assert emit.calls == []
    assert HELD_ID in json.dumps(out)
    assert HELD_ID in _live_ids(root)


def test_a_dirless_token_is_never_the_adopted_run_beside_a_held_one(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _mint_dead(root, STRAY_IDS[0])
    _age(root, STRAY_IDS[0], 3600)
    _mint_dead(root, HELD_ID)
    _age(root, HELD_ID, 3600)
    rd = root / "state" / "warp" / HELD_ID
    rd.mkdir(parents=True)
    (rd / "run-stage.json").write_text(json.dumps({"stage": "minted"}), encoding="utf-8")

    assert mod._runner_less_run(root, SIZING, None) == HELD_ID
    assert HELD_ID in _live_ids(root)
