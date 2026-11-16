"""Post-landing bookkeeping of `run_landing.land_run` (RL-07d, RL-08, RL-11, RL-12): what only the new
path promises. The old-versus-new parity cases live in `test_landing_parity.py`.

RL-11 and RL-12 cannot fold into the landing commit: each writes the landing's own sha into a plan or
sizing file, and no tree can contain the hash of the commit that holds it. They ride one in-process
follow-up commit; the trailers, receipts, tracked outcome and delivery note ride the landing commit.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import run_landing
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_landing_parity import (  # noqa: F401 -- fixtures
    PLAN,
    SIZING,
    _git,
    _head,
    _landed_names_at,
    _PLAN_TEXT,
    _plan_at,
    _plan_fire,
    _SIZING_TEXT,
    ask_repo,
    plan_repo,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _write_a(root: Path) -> None:
    (root / "a.py").write_text("a = 1\n", encoding="utf-8")


def test_the_landing_and_its_stamp_are_two_commits_and_the_receipt_rides_the_first(plan_repo):
    _write_a(plan_repo)
    before = int(_git(["rev-list", "--count", "HEAD"], plan_repo))

    out = _plan_fire("new", plan_repo)

    assert out["committed"] is True, out
    assert int(_git(["rev-list", "--count", "HEAD"], plan_repo)) == before + 2
    assert _git(["rev-parse", "HEAD~1"], plan_repo) == out["sha"] == _git(["rev-parse", out["coded_sha"] + "~1"], plan_repo)
    assert out["receipts"][0] in _landed_names_at(plan_repo, out["sha"])
    assert _landed_names_at(plan_repo, out["coded_sha"]) == {PLAN}


def test_the_bookkeeping_spawns_no_process(plan_repo, monkeypatch):
    _write_a(plan_repo)
    spawned: list = []
    real = subprocess.Popen

    class Counting(real):
        def __init__(self, *a, **kw):
            spawned.append(a[0] if a else kw.get("args"))
            super().__init__(*a, **kw)

    monkeypatch.setattr(subprocess, "Popen", Counting)

    out = _plan_fire("new", plan_repo)

    assert out["committed"] is True and out["rows_coded"], out
    assert spawned == []


def test_a_refused_landing_leaves_no_receipt_and_no_outcome_claiming_a_landing(plan_repo):
    key = "-----BEGIN " + "RSA PRIVATE KEY-----"
    (plan_repo / "creds.py").write_text(f'KEY = """{key}"""\n', encoding="utf-8")
    head = _head(plan_repo)

    out = _plan_fire("new", plan_repo, {"C1": ["creds.py"]})

    assert out["committed"] is False and out["refused"] == "secret-in-diff"
    assert _head(plan_repo) == head
    assert list((plan_repo / "state").rglob("*.md")) == []


def test_a_repo_that_has_not_onboarded_gets_no_plan_stamp_no_register_write_and_no_outcome(plan_repo):
    (plan_repo / "archive").rmdir()
    _write_a(plan_repo)

    out = _plan_fire("new", plan_repo)

    assert out["committed"] is True, out
    assert out["rows_coded"] == {} and out["coded_sha"] is None and out["coded_skipped"] == "repo-not-opted-in"
    assert _head(plan_repo) == out["sha"]
    assert _plan_at(plan_repo, "HEAD").replace("\r\n", "\n") == _PLAN_TEXT.rstrip("\n")
    assert _git(["show", f"HEAD:{SIZING}"], plan_repo).replace("\r\n", "\n") == _SIZING_TEXT.rstrip("\n")


def test_a_stamp_that_would_break_the_plan_schema_is_refused_and_the_landing_stands(plan_repo, monkeypatch):
    from coordinator_core.frontmatter import schema_validate

    _write_a(plan_repo)
    monkeypatch.setattr(schema_validate, "check_plan_tasks_source",
                        lambda text: "stamped" if "disposition: coded" in text else None)
    original = (plan_repo / PLAN).read_bytes()

    out = _plan_fire("new", plan_repo)

    assert out["committed"] is True and "stamped spine fails schema" in out["coded_stamp_error"], out
    assert _head(plan_repo) == out["sha"]
    assert (plan_repo / PLAN).read_bytes() == original


def test_a_session_landing_hold_defers_the_stamp_without_a_second_commit(plan_repo, monkeypatch):
    from coordinator_core.session import landing_hold

    _write_a(plan_repo)
    deferred: list = []
    monkeypatch.setattr(landing_hold, "is_held", lambda root, sid: True)
    monkeypatch.setattr(landing_hold, "defer", lambda root, sid, kind, params: deferred.append((kind, params)))

    out = _plan_fire("new", plan_repo)

    assert out["committed"] is True and out["coded_deferred"] == "session-landing-hold", out
    assert _head(plan_repo) == out["sha"]
    assert [kind for kind, _ in deferred] == ["coded_stamp"] and PLAN in deferred[0][1]["paths"]


def test_the_resolution_ledger_path_is_the_one_the_close_job_writes():
    from coordinator_core.ops.ceremony import close_background

    assert run_landing.RESOLUTION_LEDGER_REL == close_background.SIZING_LEDGER_REL


def test_the_bookkeeping_names_none_of_the_helpers_it_replaces():
    """DR-344: the old path's stamp, register writeback and receipt writer are read for parity, never reused."""
    source = Path(run_landing.__file__).read_text(encoding="utf-8")

    for retired in (r"_stamp_coded_commit", r"\bregister_writeback\b(?!_error)", r"write_run_receipts",
                    r"terminal_commit_closeout", r"terminal_commit_review"):
        assert re.search(retired, source) is None, retired


def test_a_review_stage_edit_for_a_path_the_landing_already_carries_is_not_residue(plan_repo):
    _write_a(plan_repo)
    review = {**reviewed(), "reverify": {"edited": ["a.py", "../escape.py", "/abs.py"]}}

    out = _plan_fire("new", plan_repo, review=review)

    assert out["committed"] is True, out
    assert "review_residue" not in out
    assert _git(["show", "-s", "--format=%(trailers:key=Review-Residue)", out["sha"]], plan_repo) == ""
