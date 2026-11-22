"""co_tenant_owner: owner attribution and the park / typed-cause resolution over real run-token, index and halt records."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.ops.dispatch_emit import co_tenant_owner, live_run_index, run_token, stage_wait

PEER = "coordinator_core/ops/dispatch_emit/run_order.py"
WAITER = "ask-20261121T100000-aaaaaa"
OWNER = "ask-20261121T101000-bbbbbb"
KIN = "ask-20261121T102000-cccccc"
SIZING = "state/sizings/2026-11/x-aaaaaa.yaml"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _exc(peer: str = PEER):
    return SimpleNamespace(importer="a.py", peer=peer, symbol="run_order", module="coordinator_core.run_order", missing="module")


def _live(repo: Path, run_id: str, footprint=(PEER,), sizing: str = "") -> None:
    run_token.mint(repo, run_id, "s.mjs", marker=True, session_id=f"sess-{run_id}", sizing_rel=sizing or None)
    live_run_index.stage(repo, run_id, list(footprint), time.time())


def _halt(repo: Path, run_id: str, withheld=(PEER,), call: str = "coordinator-invoke engine-ask --resume") -> None:
    rd = repo / "state" / "warp" / run_id
    rd.mkdir(parents=True)
    marker = {"status": "halted", "run_artifacts": {"withheld": {p: "kept" for p in withheld}}}
    if call:
        marker["next_action"] = {"call": call}
    (rd / "halted.json").write_text(json.dumps(marker), encoding="utf-8")


def test_live_owner_found_by_footprint(repo) -> None:
    _live(repo, OWNER)
    assert co_tenant_owner.owner_of(repo, PEER, exclude_run_id=WAITER) == {"run_id": OWNER, "state": "live"}


def test_owner_of_path_a_run_does_not_hold_is_none(repo) -> None:
    _live(repo, OWNER, footprint=("other.py",))
    assert co_tenant_owner.owner_of(repo, PEER, exclude_run_id=WAITER) == {"run_id": None, "state": None}


def test_run_with_expired_token_is_not_a_live_owner(repo) -> None:
    _live(repo, OWNER)
    run_token.discard(repo, OWNER)
    assert co_tenant_owner.owner_of(repo, PEER, exclude_run_id=WAITER)["state"] is None


def test_self_and_chain_kin_are_never_the_owner(repo) -> None:
    _live(repo, WAITER, sizing=SIZING)
    _live(repo, KIN, sizing=SIZING)
    assert co_tenant_owner.owner_of(repo, PEER, exclude_run_id=WAITER) == {"run_id": None, "state": None}
    _live(repo, OWNER, sizing="state/sizings/2026-11/y-bbbbbb.yaml")
    assert co_tenant_owner.owner_of(repo, PEER, exclude_run_id=WAITER)["run_id"] == OWNER


def test_live_run_parked_behind_the_waiter_is_not_its_owner(repo) -> None:
    _live(repo, OWNER)
    stage_wait.park(repo, OWNER, [WAITER], [PEER])
    assert co_tenant_owner.owner_of(repo, PEER, exclude_run_id=WAITER) == {"run_id": None, "state": None}


def test_halted_owner_found_from_withheld_paths(repo) -> None:
    _halt(repo, OWNER)
    assert co_tenant_owner.owner_of(repo, PEER, exclude_run_id=WAITER) == {"run_id": OWNER, "state": "halted"}


def test_resumed_marker_is_not_a_halted_owner(repo) -> None:
    _halt(repo, OWNER)
    marker = repo / "state" / "warp" / OWNER / "halted.json"
    marker.write_text(json.dumps({"status": "resumed", "run_artifacts": {"withheld": {PEER: "kept"}}}), encoding="utf-8")
    assert co_tenant_owner.owner_of(repo, PEER, exclude_run_id=WAITER)["state"] is None


def test_live_owner_wins_over_halted(repo) -> None:
    _halt(repo, KIN.replace("cccccc", "dddddd"))
    _live(repo, OWNER)
    assert co_tenant_owner.owner_of(repo, PEER, exclude_run_id=WAITER)["state"] == "live"


def test_resolve_live_owner_parks_with_session_and_sizing(repo) -> None:
    run_token.mint(repo, WAITER, "w.mjs", marker=True, session_id="waiter-sess", sizing_rel=SIZING)
    _live(repo, OWNER, sizing="state/sizings/2026-11/y-bbbbbb.yaml")
    out = co_tenant_owner.resolve(repo, WAITER, _exc())
    assert out == {
        "parked": "co-tenant-wait", "owner": OWNER,
        "module": "coordinator_core.run_order", "symbol": "run_order", "importer": "a.py",
    }
    record = stage_wait.read(repo, WAITER)
    assert record["blockers"] == [OWNER]
    assert (record["session_id"], record["sizing_rel"], record["reason"]) == ("waiter-sess", SIZING, "co-tenant-wait")
    assert [w["run_id"] for w in stage_wait.wake_on_end(repo, OWNER)] == [WAITER]


def test_resolve_halted_owner_returns_typed_cause_and_no_park(repo) -> None:
    _halt(repo, OWNER, call="coordinator-invoke engine-ask X")
    out = co_tenant_owner.resolve(repo, WAITER, _exc())
    assert out["cause"] == "co_tenant_dependency_on_halted_run"
    assert out["owner"] == OWNER and out["owner_refire"] == "coordinator-invoke engine-ask X"
    assert "parked" not in out
    assert stage_wait.read(repo, WAITER) is None


def test_resolve_halted_owner_without_refire_names_none(repo) -> None:
    _halt(repo, OWNER, call="")
    assert co_tenant_owner.resolve(repo, WAITER, _exc())["owner_refire"] is None


def test_resolve_without_owner_refuses_typed_and_parks_nothing(repo) -> None:
    out = co_tenant_owner.resolve(repo, WAITER, _exc())
    assert out["cause"] == "co_tenant_dependency" and out["owner"] is None
    assert out["module"] == "coordinator_core.run_order"
    assert stage_wait.read(repo, WAITER) is None


def test_owner_consumed_between_attribution_and_park_leaves_no_orphan(repo, monkeypatch) -> None:
    _live(repo, OWNER)
    real_park = stage_wait.park

    def park_then_owner_lands(*args, **kwargs):
        record = real_park(*args, **kwargs)
        run_token.consume(repo, OWNER)
        return record

    monkeypatch.setattr(stage_wait, "park", park_then_owner_lands)
    out = co_tenant_owner.resolve(repo, WAITER, _exc())
    assert out["cause"] == "co_tenant_dependency" and out["owner"] is None
    assert stage_wait.read(repo, WAITER) is None
