"""`review.reverify` carries a failing node whose cause files are outside the run footprint and dirty
versus HEAD as `foreign_red` evidence naming each culprit path, whether or not a halt record holds it;
the blocker mirrors then exclude it. An in-footprint, clean or unreadable-record cause still blocks."""

from __future__ import annotations

from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.dispatch_emit import ask_compose, land_run
from coordinator_core.ops.review_test_blocker import from_reverify
from coordinator_core.ops.tests.test_review_reverify import (  # noqa: F401  (world is a fixture)
    BASE, PREFIX, SESSION, world,
)
from coordinator_core.ops.tests.test_review_reverify_sibling_owned import (
    PARAM_NODE, SIBLING_FILE, TRACE, _blob_sha, _dirty_sibling,
)


def _default(root):
    return rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=[PARAM_NODE],
        run_id="me", failing_output={PARAM_NODE: TRACE},
    )


def test_unheld_dirty_out_of_footprint_cause_is_foreign_red_not_a_blocker(world, monkeypatch):
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch, held=False)
    out = _default(root)
    assert out["foreign_red"] == {PARAM_NODE: [SIBLING_FILE]}
    assert out["sibling_owned"] == [] and out["unattributed"] == []
    assert from_reverify({**out, "failing": [PARAM_NODE]}) == []


def test_halt_held_cause_stays_sibling_owned_not_foreign_red(world, monkeypatch):
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch, held=True)
    out = _default(root)
    assert out["sibling_owned"] == [PARAM_NODE] and out["foreign_red"] == {}


def test_in_footprint_cause_still_blocks(world, monkeypatch):
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch, held=False)
    (root / "pkg/mod.py").write_bytes(b"VALUE = 2\n")
    monkeypatch.setattr(rr.git_state, "head_blobs", lambda _r, paths: {p: (0o100644, "0" * 40) for p in paths})
    out = rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=[PARAM_NODE],
        run_id="me", cause_files={PARAM_NODE: [SIBLING_FILE, "pkg/mod.py"]},
    )
    assert out["foreign_red"] == {} and out["unattributed"] == [PARAM_NODE]


def test_clean_cause_file_still_blocks(world, monkeypatch):
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch, held=False)
    monkeypatch.setattr(rr.git_state, "head_blobs", lambda r, paths: {
        p: (0o100644, _blob_sha((r / p).read_bytes())) for p in paths
    })
    out = _default(root)
    assert out["foreign_red"] == {} and out["unattributed"] == [PARAM_NODE]


def test_unreadable_halt_record_still_blocks(world, monkeypatch):
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch, record=land_run.UNREADABLE_HALT_RECORD)
    out = _default(root)
    assert out["foreign_red"] == {} and out["unattributed"] == [PARAM_NODE]


def test_composed_js_excludes_foreign_red():
    assert "Object.keys(_tSrc?.foreign_red ?? {})" in ask_compose._test_blockers_js("_r", "_t")
