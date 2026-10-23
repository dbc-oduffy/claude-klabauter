"""process_artifact_policy: opt-in predicate, withhold filter, local exclude."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import process_artifact_policy as pap
from coordinator_core.ops.dispatch_emit.run_artifacts import RunArtifacts

pytestmark = pytest.mark.foreign_repo


def _artifacts(root):
    return RunArtifacts(
        run_id="r1",
        by_class={"record": ("state/warp/r1/a.json",), "plan": ("docs/plans/p.md",)},
        record_dirs=("state/warp/r1",),
        root=root,
    )


def test_predicate_both_ways(tmp_path):
    assert pap.hosts_coordinator_state(tmp_path) is False
    (tmp_path / "archive").mkdir()
    assert pap.hosts_coordinator_state(tmp_path) is True


def test_filter_withholds_when_not_opted_in(tmp_path):
    arts = _artifacts(tmp_path)
    kept, withheld = pap.withhold_process_artifacts(tmp_path, arts)
    assert kept.paths() == ()
    assert kept.run_id == "r1" and kept.record_dirs == arts.record_dirs and kept.root == tmp_path
    assert withheld == {
        "docs/plans/p.md": pap.WITHHELD_REASON,
        "state/warp/r1/a.json": pap.WITHHELD_REASON,
    }


def test_filter_passes_through_when_opted_in(tmp_path):
    (tmp_path / "archive").mkdir()
    arts = _artifacts(tmp_path)
    kept, withheld = pap.withhold_process_artifacts(tmp_path, arts)
    assert kept is arts and withheld == {}


def test_exclude_is_idempotent_and_anchored(tmp_path):
    (tmp_path / ".git").mkdir()
    first = pap.exclude_locally(tmp_path, ["docs/plans/p.md"], ["state/warp/r1"])
    assert first == ["/docs/plans/p.md", "/state/warp/r1/"]
    assert pap.exclude_locally(tmp_path, ["docs/plans/p.md"], ["state/warp/r1"]) == []
    raw = (tmp_path / ".git" / "info" / "exclude").read_bytes()
    assert raw == b"/docs/plans/p.md\n/state/warp/r1/\n"


def test_exclude_appends_after_unterminated_line(tmp_path):
    info = tmp_path / ".git" / "info"
    info.mkdir(parents=True)
    (info / "exclude").write_bytes(b"*.pyc")
    assert pap.exclude_locally(tmp_path, ["a.md"]) == ["/a.md"]
    assert (info / "exclude").read_bytes() == b"*.pyc\n/a.md\n"


def test_exclude_uses_common_dir_of_a_linked_worktree(tmp_path):
    common = tmp_path / "main" / ".git"
    private = common / "worktrees" / "wt"
    private.mkdir(parents=True)
    (private / "commondir").write_text("../..\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {private}\n", encoding="utf-8")
    assert pap.exclude_locally(wt, ["x.md"]) == ["/x.md"]
    assert (common / "info" / "exclude").read_text(encoding="utf-8") == "/x.md\n"
    assert not (private / "info").exists()


def test_exclude_never_raises(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "info").write_text("a file, not a dir", encoding="utf-8")
    assert pap.exclude_locally(tmp_path, ["x.md"]) == []
