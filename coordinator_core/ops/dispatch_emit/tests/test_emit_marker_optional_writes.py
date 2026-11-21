"""The commit-request marker carries a row's ``optional_writes:`` paths as ``ChunkCommit.optional``."""


from coordinator_core.ops.dispatch_emit.commit_request import parse_marker
from coordinator_core.ops.dispatch_emit.emit import _terminal_commit_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

_REVIEWED = {
    "integration_stem": "rev-stem", "slices": 1, "fixes": 1,
    "wave_reviews": reviewed()["wave_reviews"],
}


def _chunks(body, writes=("a.py", "b.npz")):
    row = WaveRow("C2", "t", "s", list(writes), [], [], body=body)
    marker = _terminal_commit_marker(
        [row],
        {"C2": list(writes)},
        plan_path="docs/plans/p.md",
        deliverable_id=None,
        session_id=None,
        row_prefixes={},
    )
    assert marker is not None
    return parse_marker(marker).chunks


def test_optional_write_is_carried_and_leaves_gating() -> None:
    (chunk,) = _chunks("do it\noptional_writes: `b.npz`\n")
    assert chunk.optional == ("b.npz",)
    assert chunk.gating_paths() == ("a.py",)


def test_unmarked_row_keeps_every_write_gating() -> None:
    (chunk,) = _chunks("do it\n")
    assert chunk.optional == ()
    assert chunk.gating_paths() == ("a.py", "b.npz")


def test_optional_naming_an_undeclared_path_is_ignored() -> None:
    (chunk,) = _chunks("optional_writes: other.py\n")
    assert chunk.optional == ()
    assert chunk.gating_paths() == ("a.py", "b.npz")


def test_declared_write_marked_required_false_in_the_row_body_is_optional() -> None:
    (chunk,) = _chunks("phase: code\n- `b.npz` (required: false)\nb.npz is built\n")
    assert chunk.optional == ("b.npz",)
    assert chunk.gating_paths() == ("a.py",)


def test_required_false_beside_prose_naming_a_path_is_not_a_marking() -> None:
    (chunk,) = _chunks("when b.npz is absent the field required: false is unrelated\n")
    assert chunk.optional == ()


_SID = "11111111-2222-3333-4444-555555555555"
