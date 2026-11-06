"""dispatch.record_halted_run_landed flags a landing with no review record and no delivery verdict."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract, halted_run
from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RID = "warp-2026-11-06-solo-20261106T000000-abcdef"
UUID = "123e4567-e89b-12d3-a456-426614174000"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _no_launch(monkeypatch):
    monkeypatch.setattr(op, "_launch", lambda root, run_id: True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "one").write_text("one", encoding="utf-8")
    _git(tmp_path, "add", "--", "one")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "one")
    return tmp_path, _git(tmp_path, "rev-parse", "HEAD")


def _halt(root: Path) -> Path:
    d = root / "state" / "warp" / RID
    d.mkdir(parents=True, exist_ok=True)
    marker = d / "halted.json"
    marker.write_text(json.dumps({"status": "halted", "halted": "x", "sizing_rel": None}), encoding="utf-8")
    return marker


def _land(root: Path, sha: str) -> dict:
    out = op._handler({"run_id": RID, "sha": sha, "session_id": UUID}, root)
    assert out["ok"], out
    return out


def test_no_review_record_and_no_verdict_marks_landed_without_verdict(repo):
    root, sha = repo
    marker = _halt(root)
    out = _land(root, sha)
    assert out["landed_without_verdict"] is True
    assert json.loads(marker.read_text(encoding="utf-8"))["landed_without_verdict"] is True


def test_review_record_clears_the_flag_and_leaves_no_marker_key(repo):
    root, sha = repo
    marker = _halt(root)
    sidecar = root / ".coordinator-local" / "subagent-share" / UUID / "wave-review.md"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("review", encoding="utf-8")
    ask_contract.write_run_outcome(root, RID, {"inline_review": {"integration_stem": "wave-review"}})
    out = _land(root, sha)
    assert out["landed_without_verdict"] is False
    assert "landed_without_verdict" not in json.loads(marker.read_text(encoding="utf-8"))


def test_delivery_verdict_in_outcome_alone_clears_the_flag(repo):
    root, sha = repo
    marker = _halt(root)
    ask_contract.write_run_outcome(root, RID, {"inline_review": {"delivery": {"verdict": "PASS"}}})
    out = _land(root, sha)
    assert out["landed_without_verdict"] is False
    assert "landed_without_verdict" not in json.loads(marker.read_text(encoding="utf-8"))


def test_mark_landed_default_writes_no_key(tmp_path):
    (tmp_path / halted_run.HALTED_FILE).write_text(json.dumps({"status": "halted"}), encoding="utf-8")
    assert halted_run.mark_landed(tmp_path, "a" * 40)
    assert "landed_without_verdict" not in json.loads((tmp_path / halted_run.HALTED_FILE).read_text(encoding="utf-8"))
