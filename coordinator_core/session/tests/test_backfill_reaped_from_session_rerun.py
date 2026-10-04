"""``backfill_reaped_from_session`` re-run against a corpus that grew between runs.

The single-run cases live in ``test_claims.py::TestBackfillReapedFromSession``;
this file pins the multi-run behaviour: a later run writes only the newly
eligible batons and leaves everything the earlier run touched byte-identical.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.session import claims, record_homes
from coordinator_core.tests.git_seed import seeded_repo

_HANDOFFS = Path(record_homes.home_dir("", "handoffs")).as_posix()


def _rel(name: str) -> str:
    return Path(record_homes.record_path("", "handoffs", name)).as_posix()

_SID_A = "cb90a56e-33f1-4992-b665-c2af3070c00c"
_SID_B = "11111111-2222-3333-4444-555555555555"
_SID_C = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

_MALFORMED_NOTE = (
    "claim-release REVERTED 2026-07-30 — the crash-orphan reaper returned "
    "this baton to the pool at 796ebf5b, but holder 517027e6 had already "
    "minted its successor..."
)


def _reaper_note(sid: str) -> str:
    return (
        f"claim released by crash-orphan reaper — holder {sid} died "
        f"without resolving; returned to pool"
    )


def _write_baton(root: Path, rel_dir: str, name: str, park_note) -> Path:
    path = root / rel_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "title: t",
        "created: 2026-07-24",
        "branch: work/t/2026-07-24",
        "predecessor: none",
        "category: infra",
        "summary: fixture",
        "status: open",
        "deployment_state: ready_to_fire",
    ]
    if park_note is not None:
        lines.append(f"park_note: {park_note}")
    lines += ["---", "body"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _field(path: Path):
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("reaped_from_session:"):
            return line.split(":", 1)[1].strip()
    return None


def test_rerun_after_corpus_growth_writes_only_new_batons(tmp_path):
    tmp_path = Path(seeded_repo(tmp_path, readme="x"))
    first = _write_baton(tmp_path, _HANDOFFS, "first.md", _reaper_note(_SID_A))
    no_note = _write_baton(tmp_path, _HANDOFFS, "no-note.md", None)
    malformed = _write_baton(
        tmp_path, _HANDOFFS, "malformed.md", _MALFORMED_NOTE
    )

    run1 = claims.backfill_reaped_from_session(tmp_path)
    assert run1["written"] == [_rel("first.md")]
    assert sorted(run1["skipped"]) == [
        _rel("malformed.md"),
        _rel("no-note.md"),
    ]
    assert run1["errors"] == []
    snapshot = {p: p.read_bytes() for p in (first, no_note, malformed)}

    second = _write_baton(tmp_path, _HANDOFFS, "second.md", _reaper_note(_SID_B))
    third = _write_baton(tmp_path, _HANDOFFS, "third.md", _reaper_note(_SID_C))
    late_no_note = _write_baton(tmp_path, _HANDOFFS, "late-no-note.md", None)

    run2 = claims.backfill_reaped_from_session(tmp_path)
    assert run2["written"] == [
        _rel("second.md"),
        _rel("third.md"),
    ]
    assert sorted(run2["skipped"]) == [
        _rel("first.md"),
        _rel("late-no-note.md"),
        _rel("malformed.md"),
        _rel("no-note.md"),
    ]
    assert run2["errors"] == []

    assert _field(second) == _SID_B
    assert _field(third) == _SID_C
    assert _field(first) == _SID_A
    assert _field(late_no_note) is None
    for path, before in snapshot.items():
        assert path.read_bytes() == before

    after_run2 = {p: p.read_bytes() for p in (first, second, third)}
    run3 = claims.backfill_reaped_from_session(tmp_path)
    assert run3["written"] == []
    assert run3["errors"] == []
    for path, before in after_run2.items():
        assert path.read_bytes() == before


def test_rerun_never_reaches_batons_archived_between_runs(tmp_path):
    tmp_path = Path(seeded_repo(tmp_path, readme="x"))
    live = _write_baton(tmp_path, _HANDOFFS, "live.md", _reaper_note(_SID_A))
    claims.backfill_reaped_from_session(tmp_path)

    archived = _write_baton(
        tmp_path, "archive/handoffs/2026-07", "gone.md", _reaper_note(_SID_B)
    )
    archived_before = archived.read_bytes()
    live_before = live.read_bytes()

    run2 = claims.backfill_reaped_from_session(tmp_path)
    assert run2["written"] == []
    assert run2["skipped"] == [_rel("live.md")]
    assert archived.read_bytes() == archived_before
    assert live.read_bytes() == live_before


def test_rerun_over_a_large_grown_corpus_is_exact(tmp_path):
    tmp_path = Path(seeded_repo(tmp_path, readme="x"))
    originals = [
        _write_baton(tmp_path, _HANDOFFS, f"orig-{i:03d}.md", _reaper_note(f"{i:08d}-0000-0000-0000-000000000000"))
        for i in range(60)
    ]
    assert len(claims.backfill_reaped_from_session(tmp_path)["written"]) == 60
    before = {p: p.read_bytes() for p in originals}

    grown = [
        _write_baton(tmp_path, _HANDOFFS, f"new-{i:03d}.md", _reaper_note(f"{i:08d}-1111-1111-1111-111111111111"))
        for i in range(60)
    ]
    run2 = claims.backfill_reaped_from_session(tmp_path)
    assert len(run2["written"]) == 60
    assert len(run2["skipped"]) == 60
    assert run2["errors"] == []
    assert all(_field(p) is not None for p in grown)
    for path, content in before.items():
        assert path.read_bytes() == content
