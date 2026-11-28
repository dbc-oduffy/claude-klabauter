"""A halted or dead co-tenant parked behind the asking run never withholds its paths; a live holder still does."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import co_tenant_owner, run_token, stage_wait

pytestmark = pytest.mark.complete

PATH = "coordinator_core/ops/dispatch_emit/run_token.py"
TEST_PATH = "coordinator_core/ops/dispatch_emit/tests/test_run_token.py"
LIVE_ASKER = "warp-2026-11-27-halted-landing-writes-never-wedge-engine-f90ccf-20261128T013844-8c56b9"
DEAD_HOLDER = "warp-2026-11-27-fixture-97528e-20261127T235856-f125e5"
OTHER = "warp-2026-11-27-fixture-aaaaaa-20261127T100000-abcdef"
SIZING = "state/sizings/2026-11/x-97528e.yaml"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _halt(repo: Path, run_id: str, withheld=(PATH, TEST_PATH)) -> None:
    rd = repo / "state" / "warp" / run_id
    rd.mkdir(parents=True)
    marker = {
        "status": "halted", "halted": "co_tenant_wait", "sizing_rel": SIZING,
        "run_artifacts": {"withheld": {p: "kept" for p in withheld}},
        "next_action": {"call": "coordinator-invoke engine-ask --resume"},
    }
    (rd / "halted.json").write_text(json.dumps(marker), encoding="utf-8")


def test_a_dead_halted_holder_parked_behind_the_asking_run_is_not_its_owner(repo):
    _halt(repo, DEAD_HOLDER)
    stage_wait.park(repo, DEAD_HOLDER, [LIVE_ASKER], [PATH, TEST_PATH])

    for path in (PATH, TEST_PATH):
        assert co_tenant_owner.owner_of(repo, path, exclude_run_id=LIVE_ASKER) == {"run_id": None, "state": None}, path


def test_a_halted_holder_not_parked_behind_the_asking_run_is_attributed_never_its_owner(repo):
    _halt(repo, DEAD_HOLDER)

    assert co_tenant_owner.owner_of(repo, PATH, exclude_run_id=LIVE_ASKER) == {"run_id": None, "state": None}
    assert co_tenant_owner.peer_hunk_runs(repo, PATH, exclude_run_id=LIVE_ASKER) == (DEAD_HOLDER,)


def test_a_halted_holder_parked_behind_another_run_is_attributed_never_the_owner(repo):
    _halt(repo, DEAD_HOLDER)
    stage_wait.park(repo, DEAD_HOLDER, [OTHER], [PATH])

    assert co_tenant_owner.owner_of(repo, PATH, exclude_run_id=LIVE_ASKER) == {"run_id": None, "state": None}
    assert co_tenant_owner.peer_hunk_runs(repo, PATH, exclude_run_id=LIVE_ASKER) == (DEAD_HOLDER,)


def test_a_live_holder_still_withholds_its_path(repo):
    run_token.mint(repo, OTHER, "s.mjs", marker=True, session_id=f"sess-{OTHER}", sizing_rel=None)
    from coordinator_core.ops.dispatch_emit import live_run_index
    import time

    live_run_index.stage(repo, OTHER, [PATH], time.time())

    assert co_tenant_owner.owner_of(repo, PATH, exclude_run_id=LIVE_ASKER) == {"run_id": OTHER, "state": "live"}


def test_a_live_holder_with_a_stale_halt_marker_still_withholds_its_path(repo):
    from coordinator_core.ops.dispatch_emit import live_run_index
    import time

    _halt(repo, DEAD_HOLDER)
    run_token.mint(repo, DEAD_HOLDER, "s.mjs", marker=True, session_id=f"sess-{DEAD_HOLDER}", sizing_rel=None)
    live_run_index.stage(repo, DEAD_HOLDER, [PATH], time.time())

    assert co_tenant_owner.owner_of(repo, PATH, exclude_run_id=LIVE_ASKER) == {"run_id": DEAD_HOLDER, "state": "live"}
