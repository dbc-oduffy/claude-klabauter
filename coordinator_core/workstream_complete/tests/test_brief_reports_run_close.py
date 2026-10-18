"""brief() reports gates.run_close and stamps discharged_by on every directive."""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest

import coordinator_core.workstream_complete as wsc
from coordinator_core.contract.decision_object.judgment import build_disposition, build_judgment_point
from coordinator_core.ops.ceremony.wsc_disposition import SINGLE_SESSION
from coordinator_core.ops.dispatch_emit import run_close as rc
from coordinator_core.ops.dispatch_emit.run_close import LegResult
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

SID = "22222222-2222-2222-2222-222222222222"
RUN = "run-ws2"
PLAN = "docs/plans/p.md"
POST_CLOSE = ("d-check-machine-local-regeneratability", "d-structural-index-refresh", "d-ceremony-reindex")
SWEEPS = ("d-sweep-terminal-handoffs", "d-sweep-terminal-sizings")
REVIEW = ("d-run-review-brightline-gate", "d-close-coverage-advisory")


def _git(root, *args):
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, **no_console_creationflags())


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "seed")
    (root / ".git" / "coordinator-sessions" / SID).mkdir(parents=True)
    monkeypatch.setattr(
        wsc,
        "compute_session_shape_gate",
        lambda r, history=None: wsc.SessionShapeGate(
            sid=SID, disposition=SINGLE_SESSION, consumed_handoff="", diagnostics=[],
            consumed_handoff_paths=(), detection={},
        ),
    )
    return root


def _plan(root):
    path = root / PLAN
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nstatus: executing\nreview_stamp: {verdict: PASS}\n---\n\n# p\n", encoding="utf-8")


def _record(root, session=SID):
    legs = {leg: LegResult("ran", "") for leg in ("review", "baton-drain", "post-close")}
    return rc.write_close_job(
        root, RUN, [], datetime.now(timezone.utc) + timedelta(minutes=5), session_id=session,
        plan_rel=PLAN, claimed_batons=(), completion_entry=None, settled=legs,
    )


def _brief(root):
    return wsc.brief({"stage_paths": []}, repo_root=root)


def _stamps(env):
    return {d["id"]: d[rc.DISCHARGED_BY_KEY] for d in env["directives"]}


def test_plan_run_record_discharges_the_legs_it_paid(repo):
    _plan(repo)
    _record(repo)
    env = _brief(repo)
    payload = env["gates"]["run_close"]
    assert payload["run_ids"] == [RUN] and payload["applies"] is True
    stamps = _stamps(env)
    assert all(rc.DISCHARGED_BY_KEY in d for d in env["directives"])
    for did in (*POST_CLOSE, *SWEEPS, *REVIEW):
        assert stamps[did] == RUN, did
    assert payload["discharged"] == {did: RUN for did in stamps if stamps[did] == RUN}


def test_a_foreign_sessions_record_discharges_nothing(repo):
    _plan(repo)
    _record(repo, session="another-session")
    env = _brief(repo)
    assert env["gates"]["run_close"]["applies"] is False
    assert set(_stamps(env).values()) == {None}


def test_run_less_entry_discharges_complete_entry_by_its_path(repo):
    folder = repo / "archive" / "completed" / "202610"
    folder.mkdir(parents=True)
    entry = folder / f"20261013-adhoc-{SID[-6:]}.md"
    entry.write_text(
        f"---\nauthored_by: {SID}\ntitle: Shipped a thing\nnature: shipped\n---\n\nProse.\n", encoding="utf-8"
    )
    rel = f"archive/completed/202610/{entry.name}"
    env = _brief(repo)
    payload = env["gates"]["run_close"]
    assert payload["run_ids"] == [rel]
    assert payload["discharged"] == {"d-complete-entry": rel}
    assert _stamps(env)["d-complete-entry"] == rel
    assert {v for k, v in _stamps(env).items() if k != "d-complete-entry"} == {None}


def test_judgment_point_gating_only_discharged_directives_is_reported_not_asked(repo, monkeypatch):
    _plan(repo)
    original = wsc._build_preserved_judgment_points

    def with_extra(*a, **kw):
        jp = build_judgment_point(
            {"disposition": "reindex", "rationale": "the reindex is cheap"},
            id="jp-reindex-question",
            question="Reindex?",
            dispositions=[build_disposition("reindex", ["d-ceremony-reindex"]), build_disposition("skip", [])],
            evidence="fixture",
            reason="fixture",
            reportable=True,
        )
        return [*original(*a, **kw), jp]

    monkeypatch.setattr(wsc, "_build_preserved_judgment_points", with_extra)
    assert "jp-reindex-question" in [j["id"] for j in _brief(repo)["judgment_points"]]
    _record(repo)
    assert "jp-reindex-question" not in [j["id"] for j in _brief(repo)["judgment_points"]]


def test_record_older_than_a_later_claim_is_still_read(repo):
    _plan(repo)
    _record(repo)
    claim = repo / ".git" / "coordinator-sessions" / SID
    time.sleep(0.05)
    (claim / "memo-claim").write_text("later claim\n", encoding="utf-8")
    now = time.time()
    os.utime(claim, (now, now))
    assert _brief(repo)["gates"]["run_close"]["run_ids"] == [RUN]


def test_no_absolute_path_reaches_gates_or_stamps(repo):
    _plan(repo)
    _record(repo)
    env = _brief(repo)
    blob = repr(env["gates"]["run_close"]) + repr(_stamps(env))
    assert str(repo) not in blob and "\\" not in blob
