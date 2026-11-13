"""Replay of the fbcfb6 halt: a repo-wide gate node, green at the run base, whose failure text names only
dirty paths outside the run footprint is `foreign_red` (not `unattributed`) and feeds `classify_reds` no
fix-leg red; naming a footprint path, or sitting in a footprint test file, keeps it blocking. The
matcher is the one `retest_targeted` uses."""

from __future__ import annotations

from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.dispatch_emit import red_attribution, retest_targeted
from coordinator_core.ops.dispatch_emit.self_correction import classify_reds
from coordinator_core.ops.review_test_blocker import from_reverify
from coordinator_core.ops.tests.test_review_reverify import (  # noqa: F401  (world is a fixture)
    BASE, PREFIX, SESSION, world,
)
from coordinator_core.ops.tests.test_review_reverify_sibling_owned import _blob_sha
from coordinator_core.ops.dispatch_emit import land_run

GATE = "coordinator_core/tests/test_raw_writes_have_a_disposition.py::test_the_live_gate_is_green"
PEER_FILES = [
    "coordinator_core/ops/dispatch_emit/land_resume.py",
    "coordinator_core/ops/dispatch_emit/tree_write_fence.py",
    "coordinator_core/warm/client.py",
]
HALT_TEXT = (
    "AssertionError: The disposition gate is RED. undispositioned: "
    + "; ".join(PEER_FILES)
)


def _peer_dirt(root, monkeypatch):
    for rel in PEER_FILES:
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"# peer edit\n")
    monkeypatch.setattr(rr, "_head_readable", lambda _r: True)
    monkeypatch.setattr(rr.git_state, "head_blobs", lambda r, paths: {
        p: (0o100644, "0" * 40 if p in PEER_FILES else _blob_sha((r / p).read_bytes())) for p in paths
    })
    monkeypatch.setattr(land_run, "foreign_halted_withheld", lambda *_a, **_k: {})


def _reverify(root, text):
    return rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=[GATE],
        run_id="me", failing_output={GATE: text}, failing_at_base=[],
    )


def test_halt_shape_names_only_peer_paths_is_foreign_red_and_no_fix_leg(world, monkeypatch):
    root, _orig, _declared = world
    _peer_dirt(root, monkeypatch)
    out = _reverify(root, HALT_TEXT)
    assert out["foreign_red"] == {GATE: sorted(PEER_FILES)}
    assert out["unattributed"] == [] and out["sibling_owned"] == []
    assert from_reverify({**out, "failing": [GATE]}) == []
    split = classify_reds([GATE], ["pkg/mod.py"], foreign_red=out["foreign_red"], passed_at_base=[GATE])
    assert not split.owned and split.foreign == (GATE,)


def test_backslash_paths_in_the_failure_text_still_attribute(world, monkeypatch):
    root, _orig, _declared = world
    _peer_dirt(root, monkeypatch)
    out = _reverify(root, HALT_TEXT.replace("/", "\\"))
    assert list(out["foreign_red"]) == [GATE]


def test_naming_a_footprint_path_keeps_the_node_blocking(world, monkeypatch):
    root, _orig, _declared = world
    _peer_dirt(root, monkeypatch)
    out = _reverify(root, HALT_TEXT + "; pkg/mod.py")
    assert out["foreign_red"] == {} and out["unattributed"] == [GATE]
    assert from_reverify({**out, "failing": [GATE]}) != []


def test_a_node_in_a_footprint_test_file_keeps_blocking(world, monkeypatch):
    root, _orig, _declared = world
    _peer_dirt(root, monkeypatch)
    node = "tests/test_mod.py::test_v"
    out = rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=[node],
        run_id="me", failing_output={node: HALT_TEXT}, failing_at_base=[],
    )
    assert out["foreign_red"] == {} and out["unattributed"] == [node]


def test_one_matcher_serves_the_retest_and_the_reverify(tmp_path):
    assert retest_targeted.red_attribution is red_attribution
    log = tmp_path / "t.log"
    log.write_text(f"___ test_x ___\nboom {PEER_FILES[0]}\n=== 1 failed ===\n", encoding="utf-8")
    node = "t/test_a.py::test_x"
    assert retest_targeted._names_own_path(log, [node], PEER_FILES[:1]) == {node}
    assert retest_targeted._names_own_path(log, [node], ["pkg/mod.py"]) == set()
