"""`review.reverify` revert mode classes a failing node sibling-owned only when every caller-named cause
file is outside the run footprint and withheld by a readable foreign halt record; otherwise it blocks.

Trap: `land_run.foreign_halted_withheld` and the HEAD blob lookup are stubbed here, so halt-record parsing
is pinned by `land_run`'s own tests, not by this file."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.session.machinery_paths import share_dir
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.tests.test_review_reverify import (  # noqa: F401  (world is a fixture)
    BASE, PREFIX, SESSION, world,
)

NODE = "tests/test_plain.py::test_p"
SIBLING_FILE = "objective_falsifiers/ladder_xxl.py"


def _revert(root, cause_files):
    return rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=[NODE],
        run_id="me", cause_files=cause_files,
    )


def test_cause_file_outside_footprint_held_by_a_readable_halt_is_sibling_owned(world, monkeypatch):
    root, _orig, _declared = world
    monkeypatch.setattr(land_run, "foreign_halted_withheld", lambda _r, _id, _p, paths: {p: "sibling" for p in paths})
    out = _revert(root, {NODE: [SIBLING_FILE]})
    assert out["sibling_owned"] == [NODE]
    assert out["unattributed"] == []


def test_unreadable_halt_record_keeps_the_node_blocking(world, monkeypatch):
    root, _orig, _declared = world
    monkeypatch.setattr(
        land_run, "foreign_halted_withheld",
        lambda _r, _id, _p, paths: {p: land_run.UNREADABLE_HALT_RECORD for p in paths},
    )
    out = _revert(root, {NODE: [SIBLING_FILE]})
    assert out["sibling_owned"] == [] and out["unattributed"] == [NODE]


def test_any_in_footprint_cause_file_keeps_the_node_blocking(world, monkeypatch):
    root, _orig, _declared = world
    monkeypatch.setattr(land_run, "foreign_halted_withheld", lambda _r, _id, _p, paths: {p: "sibling" for p in paths})
    out = _revert(root, {NODE: [SIBLING_FILE, "pkg/mod.py"]})
    assert out["sibling_owned"] == [] and out["unattributed"] == [NODE]


def test_a_cause_file_no_halt_record_claims_keeps_the_node_blocking(world, monkeypatch):
    root, _orig, _declared = world
    monkeypatch.setattr(land_run, "foreign_halted_withheld", lambda *_a, **_k: {})
    out = _revert(root, {NODE: [SIBLING_FILE]})
    assert out["sibling_owned"] == [] and out["unattributed"] == [NODE]


def test_no_cause_files_never_classes_sibling_owned_and_never_asks_the_halt_records(world, monkeypatch):
    root, _orig, _declared = world

    def boom(*_a, **_k):
        raise AssertionError("halt records consulted without cause files")

    monkeypatch.setattr(land_run, "foreign_halted_withheld", boom)
    out = _revert(root, None)
    assert out["sibling_owned"] == [] and out["unattributed"] == [NODE]


def test_a_cause_file_in_the_manifest_keeps_the_node_blocking(world, monkeypatch):
    root, _orig, _declared = world
    monkeypatch.setattr(land_run, "foreign_halted_withheld", lambda _r, _id, _p, paths: {p: "sibling" for p in paths})
    out = _revert(root, {NODE: [SIBLING_FILE, "pkg/other.py"]})
    assert out["sibling_owned"] == [] and out["unattributed"] == [NODE]


def test_handler_rejects_malformed_cause_files(world):
    root, _orig, _declared = world
    out = rr._handler(
        {"base": BASE, "slice_prefix": PREFIX, "session_id": SESSION, "failing": [NODE], "cause_files": [1]}, root
    )
    assert "cause_files" in out["error"]


PARAM_NODE = "tests/test_plain.py::test_no_handler_silently_passes[ladder_xxl.py]"
TRACE = f"tests/test_plain.py:9: in test_no_handler_silently_passes\n    {SIBLING_FILE}:4: SyntaxError\n"


def _blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _dirty_sibling(root, monkeypatch, *, held=True, record=None):
    """A dirty edit to the sibling's file, a HEAD blob that differs, and a halt record claiming it."""
    f = root / SIBLING_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"broken(\n")
    monkeypatch.setattr(rr, "_head_readable", lambda _r: True)
    monkeypatch.setattr(rr.git_state, "head_blobs", lambda r, paths: {
        p: (0o100644, "0" * 40 if p == SIBLING_FILE else _blob_sha((r / p).read_bytes())) for p in paths
    })
    claims = (lambda _r, _id, _p, paths: {p: record or "sibling" for p in paths}) if held else (lambda *_a, **_k: {})
    monkeypatch.setattr(land_run, "foreign_halted_withheld", claims)


def _derived(root, output):
    return rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=[PARAM_NODE],
        run_id="me", failing_output={PARAM_NODE: output} if output is not None else None,
    )


def test_end_to_end_a_dirty_sibling_file_named_in_the_failure_output_is_sibling_owned(world, monkeypatch):
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch)
    out = _derived(root, TRACE)
    assert out["sibling_owned"] == [PARAM_NODE] and out["unattributed"] == []
    from coordinator_core.ops.review_test_blocker import from_reverify

    assert from_reverify({**out, "failing": [PARAM_NODE]}) == []


def test_end_to_end_the_parametrize_id_resolves_against_a_slashed_path_dir(world, monkeypatch):
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch)
    out = _derived(root, "see objective_falsifiers/other.py and ladder_xxl.py failed")
    assert out["sibling_owned"] == [PARAM_NODE]


def test_end_to_end_removing_the_halt_record_makes_it_a_blocker_again(world, monkeypatch):
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch, held=False)
    out = _derived(root, TRACE)
    assert out["sibling_owned"] == [] and out["unattributed"] == [PARAM_NODE]


def test_end_to_end_an_unreadable_halt_record_makes_it_a_blocker(world, monkeypatch):
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch, record=land_run.UNREADABLE_HALT_RECORD)
    out = _derived(root, TRACE)
    assert out["sibling_owned"] == [] and out["unattributed"] == [PARAM_NODE]


def test_end_to_end_a_clean_file_or_no_named_path_stays_a_blocker(world, monkeypatch):
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch)
    monkeypatch.setattr(rr.git_state, "head_blobs", lambda r, paths: {
        p: (0o100644, _blob_sha((r / p).read_bytes())) for p in paths
    })
    assert _derived(root, TRACE)["sibling_owned"] == []
    _dirty_sibling(root, monkeypatch)
    assert _derived(root, "AssertionError: nothing path-like here")["sibling_owned"] == []


def test_end_to_end_a_named_file_in_the_run_footprint_stays_a_blocker(world, monkeypatch):
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch)
    (root / "pkg/mod.py").write_bytes(b"VALUE = 2\n")
    monkeypatch.setattr(rr.git_state, "head_blobs", lambda _r, paths: {p: (0o100644, "0" * 40) for p in paths})
    out = _derived(root, TRACE + "pkg/mod.py:1: in <module>\n")
    assert out["sibling_owned"] == []


def test_a_bare_token_with_two_dirty_basename_matches_stays_a_blocker(world, monkeypatch):
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch)
    paths = frozenset({SIBLING_FILE, "other_dir/ladder_xxl.py"})
    monkeypatch.setattr(rr.git_state, "read_index_paths", lambda _r: paths)
    (root / "other_dir").mkdir()
    (root / "other_dir/ladder_xxl.py").write_bytes(b"x = 1\n")
    monkeypatch.setattr(rr.git_state, "head_blobs", lambda _r, ps: {p: (0o100644, "0" * 40) for p in ps})
    out = _derived(root, "AssertionError: ladder_xxl.py")
    assert out["sibling_owned"] == [] and out["unattributed"] == [PARAM_NODE]


# The reported shape: a real repo, a real on-disk halt record, nothing monkeypatched but the run clock.
REAL_NODE = "naked-vs-coordinator/tests/test_falsifier_instrument_errors.py::test_no_handler_silently_passes[ladder_xxl.py]"
REAL_TEST = "naked-vs-coordinator/tests/test_falsifier_instrument_errors.py"
HALTED_RUN = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-3e1743"


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()


def _put(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def real_world(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _put(root, "pkg/mod.py", "VALUE = 1\n")
    _put(root, REAL_TEST, "def test_no_handler_silently_passes():\n    assert True\n")
    _put(root, SIBLING_FILE, "ok = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    head = _git(root, "rev-parse", "HEAD")
    snap = Path(share_dir(str(root), SESSION)) / f"{PREFIX}.pre-review"
    snap.mkdir(parents=True)
    data = b"VALUE = 1\n"
    sha = hashlib.sha256(data).hexdigest()
    (snap / sha).write_bytes(data)
    (snap / "manifest.json").write_text(
        json.dumps({"base": head, "recorded_at": "2026-10-19T00:00:00Z", "paths": ["pkg/mod.py"],
                    "files": {"pkg/mod.py": sha}}), encoding="utf-8")
    _put(root, SIBLING_FILE, "broken(\n")
    return root, head


def _halt(root, withheld):
    rec = f"state/warp/{HALTED_RUN}"
    _put(root, f"{rec}/halted.json", json.dumps({"status": "halted", "run_artifacts": {"withheld": withheld}}))
    _put(root, f"{rec}/manifest.json", json.dumps({"rows": [], "doc_writes": []}))


def _real(root, head):
    return rr.review_reverify(root, base=head, slice_prefix=PREFIX, session_id=SESSION, failing=[REAL_NODE], run_id="me")


@pytest.mark.spawns_process
def test_reported_shape_is_sibling_owned_with_a_real_halt_record(real_world):
    from coordinator_core.ops.review_test_blocker import from_reverify

    root, head = real_world
    _halt(root, {SIBLING_FILE: "halt-record-only"})
    out = _real(root, head)
    assert out["sibling_owned"] == [REAL_NODE] and out["unattributed"] == []
    assert from_reverify({**out, "failing": [REAL_NODE]}) == []


@pytest.mark.spawns_process
def test_reported_shape_blocks_without_a_claiming_halt_record(real_world):
    root, head = real_world
    assert _real(root, head)["unattributed"] == [REAL_NODE]
    _halt(root, {"some/other.py": "halt-record-only"})
    assert _real(root, head)["sibling_owned"] == []


@pytest.mark.spawns_process
def test_reported_shape_blocks_on_an_unreadable_halt_record(real_world):
    root, head = real_world
    _put(root, f"state/warp/{HALTED_RUN}/halted.json", "{not json")
    out = _real(root, head)
    assert out["sibling_owned"] == [] and out["unattributed"] == [REAL_NODE]


def test_handler_threads_failing_output_to_the_deriver(world, monkeypatch):
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch)
    out = rr._handler(
        {"base": BASE, "slice_prefix": PREFIX, "session_id": SESSION, "failing": [PARAM_NODE],
         "run_id": "me", "failing_output": {PARAM_NODE: TRACE}}, root,
    )
    assert out["sibling_owned"] == [PARAM_NODE]
    bad = rr._handler(
        {"base": BASE, "slice_prefix": PREFIX, "session_id": SESSION, "failing": [PARAM_NODE], "failing_output": [1]},
        root,
    )
    assert "failing_output" in bad["error"]
