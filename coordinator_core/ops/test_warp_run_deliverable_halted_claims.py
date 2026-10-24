"""Tests for warp_run_deliverable.halted_run_write_claims."""

from __future__ import annotations

import json
import time
from calendar import timegm

import pytest

from coordinator_core.ops import warp_run_deliverable as wrd
from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import STATUS_HALTED

NOW = float(timegm((2026, 10, 19, 12, 0, 0, 0, 0, 0)))
HALTED_ID = "ask-20261019T100000-aaaaaa"
OTHER_ID = "ask-20261019T110000-bbbbbb"
OLD_ID = "ask-20261001T100000-cccccc"
JOB = "tools/runpod-jobs/job.py"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _record(repo, run_id, *, writes=(), withheld=(), status=STATUS_HALTED, manifest_text=None):
    rec = repo / "state/warp" / run_id
    rec.mkdir(parents=True)
    if manifest_text is None:
        manifest_text = json.dumps({"rows": [{"writes": list(writes)}], "doc_writes": []})
    (rec / "manifest.json").write_text(manifest_text, encoding="utf-8")
    if status is not None:
        marker = {"status": status}
        if withheld:
            marker["run_artifacts"] = {"withheld": {p: "peer-held" for p in withheld}}
        (rec / halted_run.HALTED_FILE).write_text(json.dumps(marker), encoding="utf-8")
    return rec


def _claims(repo, paths, **kw):
    kw.setdefault("exclude_run", None)
    return wrd.halted_run_write_claims(repo, paths, now=NOW, **kw)


def test_withheld_write_maps_to_run_id(repo):
    _record(repo, HALTED_ID, writes=[JOB], withheld=[JOB])
    assert _claims(repo, [JOB, "src/other.py"]) == {JOB: [HALTED_ID]}


def test_declared_but_not_withheld_path_is_not_held(repo):
    _record(repo, HALTED_ID, writes=[JOB, "src/other.py"], withheld=["src/other.py"])
    assert _claims(repo, [JOB, "src/other.py"]) == {"src/other.py": [HALTED_ID]}


def test_halt_without_any_withheld_holds_nothing(repo):
    _record(repo, HALTED_ID, writes=[JOB])
    assert _claims(repo, [JOB]) == {}


def test_backslash_path_is_normalized(repo):
    _record(repo, HALTED_ID, withheld=["docs/a.md"])
    assert _claims(repo, ["docs\\a.md"]) == {"docs/a.md": [HALTED_ID]}


def test_two_halted_runs_both_named(repo):
    _record(repo, HALTED_ID, withheld=[JOB])
    _record(repo, OTHER_ID, withheld=[JOB])
    assert _claims(repo, [JOB]) == {JOB: sorted([HALTED_ID, OTHER_ID])}


def test_resumed_marker_yields_empty(repo):
    _record(repo, HALTED_ID, withheld=[JOB], status="resumed")
    assert _claims(repo, [JOB]) == {}


def test_no_marker_yields_empty(repo):
    _record(repo, HALTED_ID, writes=[JOB], status=None)
    assert _claims(repo, [JOB]) == {}


def test_out_of_window_stamp_yields_empty(repo):
    _record(repo, OLD_ID, withheld=[JOB])
    assert _claims(repo, [JOB]) == {}


def test_exclude_run_yields_empty(repo):
    _record(repo, HALTED_ID, withheld=[JOB])
    assert _claims(repo, [JOB], exclude_run=HALTED_ID) == {}


def test_unreadable_manifest_does_not_hide_a_withheld_hold(repo):
    _record(repo, HALTED_ID, withheld=[JOB], manifest_text="{not json")
    assert _claims(repo, [JOB]) == {JOB: [HALTED_ID]}


def test_unstamped_name_skipped_without_read(repo, monkeypatch):
    _record(repo, "no-stamp-here", withheld=[JOB])

    def boom(*a, **k):
        raise AssertionError("read an unstamped record dir")

    monkeypatch.setattr(wrd, "_halted", boom)
    assert _claims(repo, [JOB]) == {}


def test_no_candidates_or_no_state_dir(repo):
    assert _claims(repo, []) == {}
    assert _claims(repo, [JOB]) == {}


def test_default_now_uses_wall_clock(repo):
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    run_id = f"ask-{stamp}-abcdef"
    _record(repo, run_id, withheld=[JOB])
    assert wrd.halted_run_write_claims(repo, [JOB], exclude_run=None) == {JOB: [run_id]}
