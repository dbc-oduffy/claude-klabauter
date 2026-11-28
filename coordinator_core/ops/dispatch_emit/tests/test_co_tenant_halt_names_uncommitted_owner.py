"""A co_tenant halt names the run whose uncommitted hunk holds the dirty peer path, never a run whose work landed
while a later run's dirt sits on its declared paths (the 92bd83 / 81929f shape)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.ops.dispatch_emit import co_tenant_owner, halted_withheld_index
from coordinator_core.win_portability import no_console_creationflags

PEER = "coordinator_core/ops/dispatch_emit/pre_halt_ancestry.py"
WAITER = "ask-20261126T100000-aaaaaa"
PARKED = "warp-park-dddddd-20261124T114131-d98cc7"
STRANDED = "warp-x-bbbbbb-20261124T175400-c1a81b"
LANDED = "warp-x-bbbbbb-20261124T220554-26f663"
LANDED_SIZING = "state/sizings/2026-11/x-bbbbbb.yaml"
PARKED_SIZING = "state/sizings/2026-11/park-dddddd.yaml"


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags(),
    )
    return done.stdout.strip()


def _record(root: Path, run_id: str, **files: dict) -> Path:
    rd = root / "state" / "warp" / run_id
    rd.mkdir(parents=True, exist_ok=True)
    for name, doc in files.items():
        (rd / f"{name.replace('_', '-')}.json").write_text(json.dumps(doc), encoding="utf-8")
    return rd


def _world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, landed_withholds_peer: bool = False, parked: bool = True) -> Path:
    """A real repo whose PEER file is dirty. LANDED halted with a.txt committed after its base and PEER declared,
    withholding only a state path; STRANDED is its earlier staged run; PARKED (when `parked`) halted withholding PEER."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    root = tmp_path / "gitrepo"
    (root / PEER).parent.mkdir(parents=True)
    _git(root.parent, "init", "-q", str(root))
    (root / "a.txt").write_text("a0", encoding="utf-8")
    (root / PEER).write_text("X = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "a.txt").write_text("a1", encoding="utf-8")
    _git(root, "commit", "-q", "-am", "work")
    (root / PEER).write_text("X = 1\nEMPTY_STAGE_RESUME_FIELD = 'x'\n", encoding="utf-8")

    withheld = [PEER] if landed_withholds_peer else ["state/warp/x/halted.json"]
    halted_withheld_index.add_run(root, STRANDED, (), sizing_rel=LANDED_SIZING, declared=(PEER,), at_ns=1_000)
    _record(root, STRANDED, run_stage={"stage": "staged", "last_activity_at": "2026-11-24T17:54:00Z"})
    halted_withheld_index.add_run(root, LANDED, withheld, sizing_rel=LANDED_SIZING, declared=("a.txt", PEER), at_ns=2_000)
    _record(
        root, LANDED, run_stage={"stage": "halted"}, manifest={"review_declared_paths": ["a.txt", PEER], "head_sha": base},
        halted={"status": "halted", "sizing_rel": LANDED_SIZING, "run_artifacts": {"withheld": {p: "kept" for p in withheld}}},
    )
    if parked:
        halted_withheld_index.add_run(root, PARKED, [PEER], sizing_rel=PARKED_SIZING, declared=(PEER,), at_ns=500)
        _record(
            root, PARKED, run_stage={"stage": "halted"},
            halted={"status": "halted", "sizing_rel": PARKED_SIZING, "run_artifacts": {"withheld": {PEER: "kept"}}},
        )
    return root


def _exc() -> SimpleNamespace:
    return SimpleNamespace(importer="land_run.py", peer=PEER, symbol="EMPTY_STAGE_RESUME_FIELD", module="", missing="symbol")


def test_halt_names_the_parked_run_holding_the_uncommitted_hunk_as_a_peer_not_an_owner(tmp_path, monkeypatch) -> None:
    root = _world(tmp_path, monkeypatch)
    assert co_tenant_owner.work_landed_at_head(root, LANDED) is True
    assert co_tenant_owner.owner_of(root, PEER, exclude_run_id=WAITER) == {"run_id": None, "state": None}
    out = co_tenant_owner.resolve(root, WAITER, _exc())
    assert out["cause"] == "co_tenant_dependency" and out["owner"] is None and PARKED in out["peer_runs"]
    assert out["symbol"] == "EMPTY_STAGE_RESUME_FIELD"


def test_halted_run_withholding_the_dirty_path_is_not_landed_and_is_still_no_owner(tmp_path, monkeypatch) -> None:
    root = _world(tmp_path, monkeypatch, landed_withholds_peer=True, parked=False)
    assert co_tenant_owner.work_landed_at_head(root, LANDED) is False
    assert co_tenant_owner.owner_of(root, PEER, exclude_run_id=WAITER)["state"] is None
    assert LANDED in co_tenant_owner.peer_hunk_runs(root, PEER, exclude_run_id=WAITER)


def test_with_no_other_holder_the_landed_chain_leaves_the_typed_unowned_cause(tmp_path, monkeypatch) -> None:
    root = _world(tmp_path, monkeypatch, parked=False)
    assert co_tenant_owner.owner_of(root, PEER, exclude_run_id=WAITER) == {"run_id": None, "state": None}
    assert co_tenant_owner.resolve(root, WAITER, _exc())["cause"] == "co_tenant_dependency"
