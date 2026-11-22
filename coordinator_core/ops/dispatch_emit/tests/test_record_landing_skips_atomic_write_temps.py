"""record_dir_changes never returns an atomic-write temp or an in-progress marker as present."""

from coordinator_core.ops.dispatch_emit.worktree_snapshot import record_dir_changes

RECORD = "state/warp/run-1"


def test_atomic_write_temps_are_skipped_real_files_kept(tmp_path):
    d = tmp_path / RECORD
    d.mkdir(parents=True)
    (d / "report.md").write_text("x")
    (d / ".atomic-write.report.md.123.abc").write_text("tmp")
    changed = {
        f"{RECORD}/report.md",
        f"{RECORD}/.atomic-write.report.md.123.abc",
        f"{RECORD}/.atomic-write.gone.md.9.zzz",
        f"{RECORD}/gone.md",
    }
    present, deleted = record_dir_changes(tmp_path, RECORD, changed)
    assert present == [f"{RECORD}/report.md"]
    assert deleted == [f"{RECORD}/gone.md"]


def test_in_progress_markers_are_not_recorded_but_their_deletion_is(tmp_path):
    d = tmp_path / RECORD
    d.mkdir(parents=True)
    for name in ("report.md", "resume.building", "x.tmp", "y.lock"):
        (d / name).write_text("x")
    changed = {f"{RECORD}/{n}" for n in ("report.md", "resume.building", "x.tmp", "y.lock")}
    changed.add(f"{RECORD}/old.building")
    present, deleted = record_dir_changes(tmp_path, RECORD, changed)
    assert present == [f"{RECORD}/report.md"]
    assert deleted == [f"{RECORD}/old.building"]
