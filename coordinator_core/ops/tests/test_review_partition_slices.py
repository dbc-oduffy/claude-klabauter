"""review.partition_slices: grouping rule, gate verdict gating, in-process freeze, registry."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.review_partition_slices import MAX_SLICES, group_paths, partition_slices
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "seed.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "seed.py")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _write(repo: Path, rels: list[str], lines: int) -> None:
    for rel in rels:
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("v = 1\n" * lines, encoding="utf-8")


def test_group_paths_merges_to_at_most_six_deterministically():
    paths = [f"d{i}/s{i}/f{j}.py" for i in range(9) for j in range(2)] + ["top.py"]
    groups = group_paths(paths)
    assert 1 <= len(groups) <= MAX_SLICES
    assert sorted(p for g in groups for p in g) == sorted(paths)
    assert groups == group_paths(list(reversed(paths)))


def test_group_paths_keys_on_first_two_directory_segments():
    groups = group_paths(["a/b/c/x.py", "a/b/y.py", "a/z.py", "top.py"])
    assert ["a/b/c/x.py", "a/b/y.py"] in groups
    assert ["a/z.py"] in groups and ["top.py"] in groups


def test_nineteen_file_multi_directory_diff_partitions_into_frozen_slices(repo):
    base = _git(repo, "rev-parse", "HEAD").strip()
    rels = [f"pkg{i % 8}/sub{i % 3}/mod{i}.py" for i in range(19)]
    _write(repo, rels, 30)
    started = time.process_time()
    result = partition_slices(repo, base, rels, "run-prep")
    cpu_s = time.process_time() - started
    assert result["error"] is None
    assert result["verdict"] == "PARTITION-MANDATORY"
    assert 1 <= len(result["slices"]) <= MAX_SLICES
    assert sorted(p for s in result["slices"] for p in s["paths"]) == sorted(rels)
    for s in result["slices"]:
        assert s["slice_id"].startswith("run-prep-")
        assert Path(s["diff_path"]).read_text(encoding="utf-8").startswith("diff --git")
    assert cpu_s < 0.5


def test_small_diff_is_single_reviewer_ok_with_no_partition(repo):
    base = _git(repo, "rev-parse", "HEAD").strip()
    _write(repo, ["a/b/one.py", "c/d/two.py"], 3)
    result = partition_slices(repo, base, ["a/b/one.py", "c/d/two.py"], "run-prep")
    assert result["error"] is None
    assert result["verdict"] == "single-reviewer-ok"
    assert result["slices"] == []
    assert sorted(result["product_paths"]) == ["a/b/one.py", "c/d/two.py"]


def test_missing_inputs_are_a_structured_error(repo):
    assert partition_slices(repo, "", ["a.py"], "p")["error"]


def test_op_is_registered_with_a_handler_and_complete_registration():
    from coordinator_core.ipc import get_op_handler  # noqa: PLC0415
    import coordinator_core.ops  # noqa: F401, PLC0415
    from coordinator_core.ops._registry_map import OP_MODULE_MAP  # noqa: PLC0415

    assert get_op_handler("review.partition_slices") is not None
    assert OP_MODULE_MAP["review.partition_slices"] == "coordinator_core.ops.review_partition_slices"
