"""A never-tracked, git-ignored run-record file is not owed as a withheld path."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import halted_run_landed as hrl
from coordinator_core.ops.dispatch_emit.tests.test_halted_run_landed_voided_paths import _head_tree

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

CLAIM = "state/warp/run-x/stage-claim.json"


def test_ignored_untracked_file_is_not_owed_but_unignored_and_tracked_stay_owed(tmp_path):
    common, load_dir, base = _head_tree(
        tmp_path,
        {".gitignore": "state/warp/*/stage-claim.json\n", "pkg/tracked.py": "a\n"},
    )
    (tmp_path / CLAIM).parent.mkdir(parents=True)
    (tmp_path / CLAIM).write_text("{}", encoding="utf-8")
    (tmp_path / "state/warp/run-x/other.json").write_text("{}", encoding="utf-8")
    (tmp_path / "pkg/tracked.py").write_text("edited\n", encoding="utf-8")
    got = hrl._clean_at_head(
        tmp_path, common, load_dir, [CLAIM, "state/warp/run-x/other.json", "pkg/tracked.py"], base
    )
    assert got == {CLAIM}
