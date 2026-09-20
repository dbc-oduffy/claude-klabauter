"""AC7 — ``state/handoffs/`` and ``archive/handoffs/2026-08/`` share no
colliding name with differing bytes.

Origin: docs/plans/2026-08-25-the-close-ceremony-inside-the-brightline.md,
chunk C4(a). 17 names were measured (2026-08-25) to exist in BOTH
``state/handoffs/`` and ``archive/handoffs/2026-08/`` with DIFFERENT bytes —
zero identical. The in-process archive sweep re-attempts all 17 on every
``run_commit_pipeline`` call, fleet-wide, and can never succeed: a
destination occupied by a different file is not a condition retrying fixes.

This is a STATE INVARIANT test, not a mechanism pin. It does not assert
sweep behaviour (that lives on the commit hot path, which this chunk is
constrained not to write) — it pins the fact-on-disk: no live collision with
differing bytes exists, exercised against a synthesised collided pair so the
detector itself is proven before it is trusted against the real trees.

NON-DESTRUCTIVE BY CONSTRUCTION: this test never deletes, overwrites, or
moves any file under ``state/handoffs/`` or ``archive/handoffs/2026-08/`` —
both are fleet-shared, ~50 concurrent sessions may hold either as a live
baton. It only reads.
"""

from __future__ import annotations

from pathlib import Path

STATE_HANDOFFS_REL = "state/handoffs"
ARCHIVE_HANDOFFS_REL = "archive/handoffs/2026-08"


def _colliding_names_with_differing_bytes(dir_a: Path, dir_b: Path) -> list[str]:
    """Names present as a regular file in both dirs with different bytes.

    Read-only: never writes, deletes, or moves anything under either dir.
    """
    if not dir_a.is_dir() or not dir_b.is_dir():
        return []
    names_a = {p.name for p in dir_a.iterdir() if p.is_file()}
    names_b = {p.name for p in dir_b.iterdir() if p.is_file()}
    colliding = []
    for name in sorted(names_a & names_b):
        if (dir_a / name).read_bytes() != (dir_b / name).read_bytes():
            colliding.append(name)
    return colliding


def test_detector_flags_a_synthesised_collided_pair(tmp_path):
    dir_a = tmp_path / "state-handoffs"
    dir_b = tmp_path / "archive-handoffs"
    dir_a.mkdir()
    dir_b.mkdir()

    (dir_a / "clean-baton.md").write_bytes(b"same bytes")
    (dir_b / "clean-baton.md").write_bytes(b"same bytes")

    (dir_a / "colliding-baton.md").write_bytes(b"live edit")
    (dir_b / "colliding-baton.md").write_bytes(b"archived predecessor")

    assert _colliding_names_with_differing_bytes(dir_a, dir_b) == ["colliding-baton.md"]


def test_detector_is_empty_when_no_collision_exists(tmp_path):
    dir_a = tmp_path / "state-handoffs"
    dir_b = tmp_path / "archive-handoffs"
    dir_a.mkdir()
    dir_b.mkdir()

    (dir_a / "only-here.md").write_bytes(b"a")
    (dir_b / "only-there.md").write_bytes(b"b")

    assert _colliding_names_with_differing_bytes(dir_a, dir_b) == []


def test_no_live_colliding_name_with_differing_bytes():
    """The real-repo invariant: as of this test run, ``state/handoffs/`` and
    ``archive/handoffs/2026-08/`` share no colliding name with differing
    bytes.

    Read-only against fleet-shared state — see module docstring's
    NON-DESTRUCTIVE BY CONSTRUCTION note. If this ever goes red, the fix is
    a non-destructive MOVE of the non-authoritative copy to a distinct,
    dated name per the chunk's hard constraints — never a delete or
    overwrite, and never inside this test.
    """
    repo_root = Path(__file__).resolve()
    for candidate in Path(__file__).resolve().parents:
        if (candidate / STATE_HANDOFFS_REL).is_dir() or (candidate / ARCHIVE_HANDOFFS_REL).is_dir():
            repo_root = candidate
            break

    state_dir = repo_root / STATE_HANDOFFS_REL
    archive_dir = repo_root / ARCHIVE_HANDOFFS_REL

    colliding = _colliding_names_with_differing_bytes(state_dir, archive_dir)

    assert colliding == [], (
        "state/handoffs/ and archive/handoffs/2026-08/ share colliding "
        f"name(s) with differing bytes: {colliding} — reconcile by MOVING "
        "the non-authoritative copy to a distinct, dated name; never "
        "delete or overwrite either copy."
    )
