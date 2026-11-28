"""dispatch.record_halted_run_landed: a hand-landed predecessor, halted or still running, releases its parked stage-wait siblings."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ask_spool import record as spool
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.ops.dispatch_emit import run_token, stage_wait
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PRED = "ask-20261124T190558-28aff7"
HELD = "ask-20261124T190558-fdde95"
RESUBMITTED = "ask-20261124T190558-099879"
HELD_SIZING = "state/sizings/2026-11/2026-11-24-ab7224-2-held.yaml"
RESUBMITTED_SIZING = "state/sizings/2026-11/2026-11-24-ab7224-3-resub.yaml"
PRODUCT = "src/scaffold.py"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )
    return done.stdout.strip()


@pytest.fixture(autouse=True)
def _no_launch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setattr(op, "_launch", lambda root, run_id: True)


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / PRODUCT).parent.mkdir(parents=True)
    (root / PRODUCT).write_text("x", encoding="utf-8")
    _git(root, "add", "--", PRODUCT)
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "hand landed")
    sha = _git(root, "rev-parse", "HEAD")
    status = root / c.status_rel(PRED)
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text(json.dumps({"parent_id": "P", "status": "running"}), encoding="utf-8")
    record = root / "state" / "warp" / PRED
    record.mkdir(parents=True)
    (record / "manifest.json").write_text(json.dumps({"review_declared_paths": [PRODUCT]}), encoding="utf-8")
    run_token.mint(root, PRED, "scratch/x.py")
    return root, sha


def _status(root: Path) -> str:
    return json.loads((root / c.status_rel(PRED)).read_text(encoding="utf-8"))["status"]


def _park(root: Path, run_id: str, sizing: str) -> None:
    stage_wait.park(root, run_id, [PRED], [], session_id="s1", sizing_rel=sizing)


def _spooled(root: Path) -> list:
    return spool.list_pending(str(root))


def test_running_predecessor_landed_by_hand_releases_parked_sibling(repo) -> None:
    root, sha = repo
    assert run_token.is_live(root, PRED)
    _park(root, HELD, HELD_SIZING)
    out = op._handler({"run_id": PRED, "sha": sha}, root)
    assert out["ok"] and out["written"] is True, out
    assert _status(root) == "landed"
    assert out["stage_waiters"] == {"released": [HELD]}
    assert not run_token.is_live(root, PRED)
    assert stage_wait.read(root, HELD)["started"] is True
    assert len(_spooled(root)) == 1


def test_running_predecessor_without_landed_work_still_refuses(repo) -> None:
    root, _ = repo
    (root / "other.py").write_text("y", encoding="utf-8")
    _git(root, "add", "--", "other.py")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "unrelated")
    unrelated = _git(root, "rev-parse", "HEAD")
    _park(root, HELD, HELD_SIZING)
    out = op._handler({"run_id": PRED, "sha": unrelated}, root)
    assert not out["ok"] and "not halted" in out["error"]
    assert _status(root) == "running"
    assert stage_wait.read(root, HELD)["started"] is False
    assert _spooled(root) == []


def test_sibling_resubmitted_under_a_newer_run_is_skipped(repo) -> None:
    root, sha = repo
    _park(root, HELD, HELD_SIZING)
    _park(root, RESUBMITTED, RESUBMITTED_SIZING)
    newer = root / "state" / "warp" / "warp-2026-11-24-ab7224-3-resub-20261124T194117-9817f9"
    newer.mkdir(parents=True)
    (newer / "run-stage.json").write_text(json.dumps({"stage": "running"}), encoding="utf-8")
    out = op._handler({"run_id": PRED, "sha": sha}, root)
    assert out["ok"], out
    assert out["stage_waiters"]["released"] == [HELD]
    assert [s["run_id"] for s in out["stage_waiters"]["skipped"]] == [RESUBMITTED]
    assert stage_wait.read(root, RESUBMITTED) is None
    assert stage_wait.read(root, HELD)["started"] is True
    assert len(_spooled(root)) == 1


def test_sibling_with_supersession_marker_is_skipped(repo) -> None:
    root, sha = repo
    _park(root, HELD, HELD_SIZING)
    marker = root / c.superseded_rel(HELD)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({c.SUPERSEDED_BY_KEY: "ask-20261124T194117-9817f9"}), encoding="utf-8")
    out = op._handler({"run_id": PRED, "sha": sha}, root)
    assert out["ok"], out
    assert out["stage_waiters"]["skipped"] == [{"run_id": HELD, "superseded_by": "ask-20261124T194117-9817f9"}]
    assert "released" not in out["stage_waiters"]
    assert _spooled(root) == []


def test_repeat_call_after_landing_is_idempotent(repo) -> None:
    root, sha = repo
    _park(root, HELD, HELD_SIZING)
    first = op._handler({"run_id": PRED, "sha": sha}, root)
    second = op._handler({"run_id": PRED, "sha": sha}, root)
    assert first["ok"] and second["ok"] and second["written"] is False
    assert "stage_waiters" not in second
    assert len(_spooled(root)) == 1
