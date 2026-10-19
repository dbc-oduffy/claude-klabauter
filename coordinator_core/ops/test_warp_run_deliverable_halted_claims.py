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


def _record(repo, run_id, *, writes=(), doc_writes=(), status=STATUS_HALTED, manifest_text=None):
    rec = repo / "state/warp" / run_id
    rec.mkdir(parents=True)
    if manifest_text is None:
        manifest_text = json.dumps({"rows": [{"writes": list(writes)}], "doc_writes": list(doc_writes)})
    (rec / "manifest.json").write_text(manifest_text, encoding="utf-8")
    if status is not None:
        (rec / halted_run.HALTED_FILE).write_text(json.dumps({"status": status}), encoding="utf-8")
    return rec


def _claims(repo, paths, **kw):
    kw.setdefault("exclude_run", None)
    return wrd.halted_run_write_claims(repo, paths, now=NOW, **kw)


def test_declared_write_maps_to_run_id(repo):
    _record(repo, HALTED_ID, writes=[JOB])
    assert _claims(repo, [JOB, "src/other.py"]) == {JOB: [HALTED_ID]}


def test_doc_write_and_backslash_path(repo):
    _record(repo, HALTED_ID, doc_writes=["docs/a.md"])
    assert _claims(repo, ["docs\\a.md"]) == {"docs/a.md": [HALTED_ID]}


def test_prefix_declaration_matches_path_under_it(repo):
    _record(repo, HALTED_ID)
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker

    marker = render_marker(
        CommitRequest(
            chunks=(ChunkCommit(id="C", title="t", paths=(), prefixes=("tools/likeness-prototype",)),), version=1
        )
    )
    scratch = repo / "scratch/warp" / HALTED_ID
    scratch.mkdir(parents=True)
    (scratch / wrd.COMMIT_REQUEST_FILE).write_text(marker + "\n", encoding="utf-8")
    got = _claims(repo, ["tools/likeness-prototype/x.py", "tools/likeness-prototypex/y.py"])
    assert got == {"tools/likeness-prototype/x.py": [HALTED_ID]}


def test_two_halted_runs_both_named(repo):
    _record(repo, HALTED_ID, writes=[JOB])
    _record(repo, OTHER_ID, writes=[JOB])
    assert _claims(repo, [JOB]) == {JOB: sorted([HALTED_ID, OTHER_ID])}


def test_resumed_marker_yields_empty(repo):
    _record(repo, HALTED_ID, writes=[JOB], status="resumed")
    assert _claims(repo, [JOB]) == {}


def test_no_marker_yields_empty(repo):
    _record(repo, HALTED_ID, writes=[JOB], status=None)
    assert _claims(repo, [JOB]) == {}


def test_out_of_window_stamp_yields_empty(repo):
    _record(repo, OLD_ID, writes=[JOB])
    assert _claims(repo, [JOB]) == {}


def test_exclude_run_yields_empty(repo):
    _record(repo, HALTED_ID, writes=[JOB])
    assert _claims(repo, [JOB], exclude_run=HALTED_ID) == {}


def test_unreadable_manifest_yields_none(repo):
    _record(repo, HALTED_ID, manifest_text="{not json")
    assert _claims(repo, [JOB]) is None


def test_unreadable_manifest_with_nothing_declared_out_of_window_is_ignored(repo):
    _record(repo, OLD_ID, manifest_text="{not json")
    assert _claims(repo, [JOB]) == {}


def test_unstamped_name_skipped_without_read(repo, monkeypatch):
    _record(repo, "no-stamp-here", writes=[JOB])

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
    _record(repo, run_id, writes=[JOB])
    assert wrd.halted_run_write_claims(repo, [JOB], exclude_run=None) == {JOB: [run_id]}
