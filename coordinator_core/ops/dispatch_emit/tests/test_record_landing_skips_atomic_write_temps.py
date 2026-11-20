"""record_dir_changes never returns an atomic-write temp, present or deleted."""

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
