"""An expired land-resume entry of a green run hands off one land call on its halt marker; red stays expired."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_resume as lr
from coordinator_core.warm import breadcrumb

RUN = "warp-green-1"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "rt"))
    return tmp_path / "engine"


def _run_dir(repo: Path, *, green: bool, status: str = "halted") -> Path:
    rd = repo / "state" / "warp" / RUN
    rd.mkdir(parents=True)
    marker = {
        "halted": "land-deadline",
        "status": status,
        "reason": "land_byte_gate deadline",
        "next_action": {"kind": "exit", "detail": "old"},
        "run_artifacts": {"withheld": {"b.py": "held", "a.py": "held"}},
    }
    (rd / "halted.json").write_text(json.dumps(marker), encoding="utf-8")
    verdict = "green" if green else "red"
    (rd / "overlay-verdict.json").write_text(json.dumps({"verdict": verdict}), encoding="utf-8")
    return rd


def _expire(root: Path, repo_root: str, leg: str = "land_byte_gate.admit") -> None:
    params = {"root": repo_root, "mode": "product", "run_id": RUN}
    assert lr.enqueue(root, RUN, params, leg)
    entry = lr.load(root, RUN)
    entry["enqueued_at"] = time.time() - lr.MAX_AGE_SECS - 10
    lr._write(lr._path(root, RUN, lr.PENDING_SUFFIX), entry)


def _marker(rd: Path) -> dict:
    return json.loads((rd / "halted.json").read_text(encoding="utf-8"))


def _done(root: Path) -> dict:
    return json.loads(lr._path(root, RUN, lr.DONE_SUFFIX).read_text(encoding="utf-8"))


def test_green_expired_entry_hands_off_one_land_call(root: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    rd = _run_dir(repo, green=True)
    _expire(root, str(repo))
    assert lr.next_due(root, time.time()) is None
    action = _marker(rd)["next_action"]
    assert action["kind"] == lr.HANDOFF_KIND
    assert action["run_id"] == RUN
    assert action["last_leg"] == "land_byte_gate.admit"
    assert action["withheld_paths"] == ["a.py", "b.py"]
    assert action["call"].count("dispatch.land_run") == 1 and RUN in action["call"]
    assert _marker(rd)["status"] == "halted"
    assert _done(root)["outcome"] == lr.OUTCOME_HANDED_OFF


def test_red_expired_entry_finishes_expired_without_next_action(root: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    rd = _run_dir(repo, green=False)
    before = (rd / "halted.json").read_bytes()
    _expire(root, str(repo))
    lr.next_due(root, time.time())
    assert (rd / "halted.json").read_bytes() == before
    assert _done(root)["outcome"] == lr.OUTCOME_EXPIRED


def test_green_entry_with_empty_repo_root_finishes_expired(root: Path) -> None:
    _expire(root, "")
    lr.next_due(root, time.time())
    assert _done(root)["outcome"] == lr.OUTCOME_EXPIRED


def test_green_entry_with_missing_run_dir_finishes_expired(root: Path, tmp_path: Path) -> None:
    _expire(root, str(tmp_path / "nowhere"))
    lr.next_due(root, time.time())
    assert _done(root)["outcome"] == lr.OUTCOME_EXPIRED


def test_non_halted_marker_finishes_expired(root: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    rd = _run_dir(repo, green=True, status="landed")
    before = (rd / "halted.json").read_bytes()
    _expire(root, str(repo))
    lr.next_due(root, time.time())
    assert (rd / "halted.json").read_bytes() == before
    assert _done(root)["outcome"] == lr.OUTCOME_EXPIRED


def test_unreadable_marker_finishes_expired_bytes_unchanged(root: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    rd = _run_dir(repo, green=True)
    (rd / "halted.json").write_bytes(b"{not json")
    _expire(root, str(repo))
    lr.next_due(root, time.time())
    assert (rd / "halted.json").read_bytes() == b"{not json"
    assert _done(root)["outcome"] == lr.OUTCOME_EXPIRED


def test_marker_write_failure_finishes_expired_and_does_not_raise(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coordinator_core import atomic_replace

    repo = tmp_path / "repo"
    rd = _run_dir(repo, green=True)
    before = (rd / "halted.json").read_bytes()

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(atomic_replace, "atomic_write_bytes", boom)
    _expire(root, str(repo))
    assert lr.next_due(root, time.time()) is None
    assert (rd / "halted.json").read_bytes() == before
    assert _done(root)["outcome"] == lr.OUTCOME_EXPIRED


def test_expired_green_entry_ahead_of_due_entry_hands_off_and_claims_the_due_one(
    root: Path, tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    rd = _run_dir(repo, green=True)
    _expire(root, str(repo))
    later = "warp-zz-due"
    assert lr.enqueue(root, later, {"root": str(repo), "mode": "product", "run_id": later}, "land_byte_gate.admit")
    claimed = lr.next_due(root, time.time())
    assert claimed is not None and claimed.name == later + lr.CLAIMED_SUFFIX
    assert _marker(rd)["next_action"]["kind"] == lr.HANDOFF_KIND
    assert _done(root)["outcome"] == lr.OUTCOME_HANDED_OFF


def test_unexpired_entry_is_claimed_unchanged(root: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    rd = _run_dir(repo, green=True)
    before = (rd / "halted.json").read_bytes()
    assert lr.enqueue(root, RUN, {"root": str(repo), "mode": "product", "run_id": RUN}, "land_byte_gate.admit")
    claimed = lr.next_due(root, time.time())
    assert claimed is not None and claimed.name == RUN + lr.CLAIMED_SUFFIX
    assert (rd / "halted.json").read_bytes() == before
