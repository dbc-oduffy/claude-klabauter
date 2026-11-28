"""A refire whose test leg ran green after the row's newest write passes the ledger test-freshness gate."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from coordinator_core.git import git_state
from coordinator_core.ops import test_leg_run as tlr
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path
from coordinator_core.ops.dispatch_emit.ledger_test_freshness import STALE_TESTS_REASON, stale_tests_refusal

RUN = "ask-20261125T000000-refire"
HEAD = "a" * 40
GREEN = {"status": "pass", "status_after": "pass", "tests_run": 3, "over_budget": False}


def _repo(tmp_path: Path) -> Path:
    repo = (tmp_path / "repo").resolve()
    (repo / ".git").mkdir(parents=True)
    return repo


def _age(path: Path, seconds_from_now: float) -> None:
    when = time.time() + seconds_from_now
    os.utime(path, (when, when))


def _fire_leg(repo: Path, monkeypatch, verdict: dict) -> Path:
    """The real leg writer (`test_leg_run.main`) with only pytest replaced, writing the run's own record."""
    monkeypatch.setattr(git_state, "head_sha", lambda *_a, **_k: HEAD)
    monkeypatch.setattr(tlr, "run", lambda *_a, **_k: dict(verdict))
    out = measured_rev_record_path(repo, RUN).parent / "test-leg.json"
    tlr.main(["--repo-root", str(repo), "--run-id", RUN, "--out", str(out), "--", "tests/test_x.py"])
    return out


def _refire_fixture(repo: Path) -> Path:
    """An earlier fire's edit and its unstamped leg record, as the pre-fix writer left them."""
    write = repo / "a.py"
    write.write_text("x = 1\n", encoding="utf-8")
    _age(write, -3600)
    stale_leg = measured_rev_record_path(repo, RUN).parent / "test-leg.json"
    stale_leg.parent.mkdir(parents=True, exist_ok=True)
    stale_leg.write_text(json.dumps({"status": "pass", "tests_run": 17}), encoding="utf-8")
    _age(stale_leg, -3500)
    return write


def test_a_green_refire_leg_after_the_newest_write_passes_the_gate(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _refire_fixture(repo)
    assert stale_tests_refusal(repo, ["a.py"], {}, RUN) is not None
    out = _fire_leg(repo, monkeypatch, GREEN)
    assert json.loads(out.read_text(encoding="utf-8"))["head_sha"] == HEAD
    assert stale_tests_refusal(repo, ["a.py"], {}, RUN) is None


def test_an_edit_after_the_refire_leg_still_refuses(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    write = _refire_fixture(repo)
    _fire_leg(repo, monkeypatch, GREEN)
    write.write_text("x = 2\n", encoding="utf-8")
    _age(write, 3600)
    reason = stale_tests_refusal(repo, ["a.py"], {}, RUN)
    assert reason is not None and reason.startswith(STALE_TESTS_REASON)


def test_a_stamped_red_refire_leg_does_not_pass_the_gate(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _refire_fixture(repo)
    _fire_leg(repo, monkeypatch, {"status": "fail", "status_after": "fail", "tests_run": 3, "over_budget": False,
                                  "failing": ["tests/test_x.py::test_y"]})
    reason = stale_tests_refusal(repo, ["a.py"], {}, RUN)
    assert reason is not None and reason.startswith(STALE_TESTS_REASON)
