"""dispatch.land_run: the kept-review chunk lands as bookkeeping, and a landing refuses while the run's
recorded post-checkpoint hunks are dirty, outside the landing set and not withheld."""

from __future__ import annotations

import re
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import kept_work
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.credit_gate import KEPT_REVIEW_CHUNK_ID
from coordinator_core.ops.dispatch_emit.tests.test_land_run_landing_remedies import (  # noqa: F401 -- fixture
    _author,
    _declare,
    _dirty,
    _git,
    _land,
    _tree,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "plan-20261024T100000-bbbbbb"
REFUSAL = "own-post-checkpoint-hunks-unclaimed"


def _record(root, run_id: str, hunks: dict) -> None:
    kept_work.write(root / RECORD_DIR_ROOT / run_id, {}, None, post_checkpoint=hunks)


def _land_run(root, **extra) -> dict:
    return _land(root, run_id=RUN, **extra)


def _blob(root, rel: str) -> str:
    return kept_work.fingerprint(root, rel)


def test_kept_review_chunk_lands_both_paths_uncredited_and_named(repo):
    _author(repo, "a.py", "a = 1\n")
    _author(repo, "b.py", "b = 1\n")
    _author(repo, "p.py", "p = 1\n")
    _declare(repo, [("C1", ["p.py"]), (KEPT_REVIEW_CHUNK_ID, ["a.py", "b.py"])])
    _record(repo, RUN, {"a.py": _blob(repo, "a.py"), "b.py": _blob(repo, "b.py")})

    out = _land_run(repo)

    assert out["committed"] is True, out
    assert {"a.py", "b.py", "p.py"} <= set(_tree(repo))
    message = _git(["log", "-1", "--format=%B"], repo)
    assert re.search(rf"^Lands-Chunks: .*{KEPT_REVIEW_CHUNK_ID}", message, re.M)


def test_a_recorded_dirty_hunk_the_request_omits_refuses_and_commits_nothing(repo):
    _author(repo, "a.py", "a = 1\n")
    _author(repo, "p.py", "p = 1\n")
    _declare(repo, [("C1", ["p.py"])])
    _record(repo, RUN, {"a.py": _blob(repo, "a.py")})
    head = _git(["rev-parse", "HEAD"], repo).strip()

    out = _land_run(repo)

    assert out["committed"] is False and out["refused"] == REFUSAL, out
    assert out["paths"] == ["a.py"] and "a.py" in out["error"] and RUN in out["error"]
    assert _git(["rev-parse", "HEAD"], repo).strip() == head


def test_the_resumed_runs_record_is_read_too(repo):
    resumed = "plan-20261023T100000-cccccc"
    _author(repo, "a.py", "a = 1\n")
    _author(repo, "p.py", "p = 1\n")
    _declare(repo, [("C1", ["p.py"])])
    _record(repo, resumed, {"a.py": _blob(repo, "a.py")})
    (repo / RECORD_DIR_ROOT / RUN).mkdir(parents=True, exist_ok=True)
    review = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0, "resumed_run_id": resumed}

    out = _land_run(repo, inline_review=review)

    assert out["refused"] == REFUSAL and out["paths"] == ["a.py"], out
    assert resumed in out["error"]


def test_a_withheld_recorded_hunk_commits_and_stays_dirty(repo):
    _author(repo, "a.py", "a = 1\n")
    _author(repo, "p.py", "p = 1\n")
    _declare(repo, [("C1", ["p.py"])])
    _record(repo, RUN, {"a.py": _blob(repo, "a.py")})

    out = _land_run(repo, withhold_paths=[{"path": "a.py", "reason": "peer owns it"}])

    assert out["committed"] is True, out
    assert "p.py" in _tree(repo) and "a.py" not in _tree(repo)
    assert "a.py" in _dirty(repo)


def test_a_recorded_hunk_already_at_head_does_not_refuse(repo):
    _author(repo, "a.py", "a = 1\n")
    _git(["add", "a.py"], repo)
    _git(["commit", "-q", "-m", "a landed already"], repo)
    _author(repo, "p.py", "p = 1\n")
    _declare(repo, [("C1", ["p.py"])])
    _record(repo, RUN, {"a.py": _blob(repo, "a.py")})

    out = _land_run(repo)

    assert out["committed"] is True, out
    assert "p.py" in _tree(repo)


def test_a_run_with_no_post_checkpoint_record_lands_unchanged(repo):
    _author(repo, "p.py", "p = 1\n")
    _declare(repo, [("C1", ["p.py"])])

    out = _land_run(repo)

    assert out["committed"] is True, out


def test_the_record_adds_no_process(repo, monkeypatch):
    real = subprocess.Popen
    calls = []

    class Spy(real):
        def __init__(self, *a, **k):
            calls.append(a[0] if a else k.get("args"))
            super().__init__(*a, **k)

    _author(repo, "p.py", "p = 1\n")
    _declare(repo, [("C1", ["p.py"])])
    monkeypatch.setattr(subprocess, "Popen", Spy)
    assert _land_run(repo)["committed"] is True
    base = len(calls)

    _author(repo, "a.py", "a = 1\n")
    _author(repo, "b.py", "b = 1\n")
    _declare(repo, [("C2", ["b.py"]), (KEPT_REVIEW_CHUNK_ID, ["a.py"])])
    _record(repo, RUN, {"a.py": _blob(repo, "a.py")})
    calls.clear()
    out = _land_run(repo)

    assert out["committed"] is True, out
    assert len(calls) <= base
