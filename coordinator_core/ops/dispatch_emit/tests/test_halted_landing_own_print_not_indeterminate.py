"""A halted landing credits a path whose worktree blob is the run's own recorded print; a differing blob stays withheld."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha
from coordinator_core.ops.dispatch_emit.tests import test_land_run_halt_record as lrh
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    LEDGER,
    PASSING,
    RUN,
    _call,
    _marker_script,
    _write_record,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

INDETERMINATE = "peer-claim-indeterminate"
BODY = "d = 1\n"


@pytest.fixture
def unanswerable(monkeypatch):
    """The claim lookup answers 'indeterminate' for every path it is asked about."""

    def fake(root, paths, holds=None, own_sid=None, abandoned_holds=None, held_out=None):
        return [f"claim state indeterminate for {p!r}" for p in paths]

    monkeypatch.setattr(commit_v2, "_peer_claim_warnings", fake)


def _print(root, rel: str, content: str) -> None:
    rec = root / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "withheld-prints.json").write_text(json.dumps({rel: blob_sha(content.encode("utf-8"))}), encoding="utf-8")


def _record_commit(repo, printed: str) -> dict:
    _write_record(repo, RUN)
    (repo / "done.py").write_bytes(BODY.encode("utf-8"))
    _marker_script(repo, [("C1", ["done.py"])], {"C1": "DONE: C1\n"})
    _print(repo, "done.py", printed)
    return _call(repo, LEDGER, **PASSING)


def _run_halt(repo, printed: str) -> dict:
    _write_record(repo, RUN)
    (repo / "done.py").write_bytes(BODY.encode("utf-8"))
    _marker_script(repo, [("C1", ["done.py"])], {"C1": "DONE: C1\n"})
    _print(repo, "done.py", printed)
    params = {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: LEDGER, **PASSING}
    return lrh.land_run_halt.land(repo, repo, params)


LANDERS = pytest.mark.parametrize("lander", [_record_commit, _run_halt], ids=["halted_record_commit", "land_run_halt"])


@LANDERS
def test_own_print_match_is_not_withheld_as_indeterminate(repo, unanswerable, lander):
    out = lander(repo, BODY)

    assert out["withheld"].get("done.py") != INDETERMINATE


@LANDERS
def test_mismatched_print_stays_withheld_as_indeterminate(repo, unanswerable, lander):
    out = lander(repo, "d = 2\n")

    assert out["withheld"]["done.py"] == INDETERMINATE


def test_own_print_credited_is_exact_per_path(tmp_path):
    from coordinator_core.ops.dispatch_emit.halted_record_commit import own_print_credited

    (tmp_path / "a.py").write_bytes(BODY.encode("utf-8"))
    (tmp_path / "b.py").write_bytes(b"other\n")
    rec = tmp_path / "state" / "warp" / "r"
    rec.mkdir(parents=True)
    prints = {"a.py": blob_sha(BODY.encode("utf-8")), "b.py": blob_sha(b"stale\n"), "gone.py": blob_sha(b"x")}
    (rec / "withheld-prints.json").write_text(json.dumps(prints), encoding="utf-8")

    assert own_print_credited(tmp_path, "state/warp/r", ["a.py", "b.py", "gone.py", "unprinted.py"]) == {"a.py"}
    assert own_print_credited(tmp_path, "state/warp/none", ["a.py"]) == set()
