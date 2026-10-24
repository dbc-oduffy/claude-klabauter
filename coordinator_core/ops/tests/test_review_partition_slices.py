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


def _peer_tree(repo: Path):
    """Commit base..terminal over declared paths, then leave a peer's uncommitted edit and an
    untracked file under a declared directory in the shared tree."""
    base = _git(repo, "rev-parse", "HEAD").strip()
    rels = [f"pkg{i % 8}/sub{i % 3}/mod{i}.py" for i in range(19)]
    _write(repo, rels, 30)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "work")
    terminal = _git(repo, "rev-parse", "HEAD").strip()
    (repo / rels[0]).write_text("v = 1\n" * 30 + "peer_edit = 1\n", encoding="utf-8")
    (repo / "pkg0" / "sub0").mkdir(parents=True, exist_ok=True)
    (repo / "pkg0" / "sub0" / "peer_untracked.py").write_text("p = 1\n" * 40, encoding="utf-8")
    declared = rels + ["pkg0/sub0"]
    return base, terminal, rels, declared


def test_range_mode_omits_a_peers_uncommitted_and_untracked_files(repo):
    base, terminal, rels, declared = _peer_tree(repo)
    result = partition_slices(repo, base, declared, "rng", head=terminal)
    assert result["error"] is None
    assert result["verdict"] == "PARTITION-MANDATORY"
    assert "pkg0/sub0/peer_untracked.py" not in result["product_paths"]
    assert sorted(result["product_paths"]) == sorted(rels)
    assert result["slices"]
    for s in result["slices"]:
        text = Path(s["diff_path"]).read_text(encoding="utf-8")
        assert "peer_untracked" not in text
        assert "peer_edit" not in text
        assert set(s["paths"]) <= set(rels)


def test_worktree_mode_still_reports_the_peers_files(repo):
    base, _terminal, rels, declared = _peer_tree(repo)
    result = partition_slices(repo, base, declared, "wt")
    assert result["error"] is None
    assert "pkg0/sub0/peer_untracked.py" in result["product_paths"]


def test_handler_requires_exactly_one_of_head_and_worktree(repo):
    from coordinator_core.ops.review_partition_slices import _handler  # noqa: PLC0415

    base = _git(repo, "rev-parse", "HEAD").strip()
    common = {"base": base, "paths": ["seed.py"], "slice_prefix": "p"}
    both = _handler({**common, "head": base, "worktree": True}, repo)
    neither = _handler(dict(common), repo)
    assert both.get("error") and neither.get("error")
    assert "exactly one" in both["error"] and "exactly one" in neither["error"]


def test_range_mode_numstat_failure_is_returned_as_error_not_raised(repo):
    base = _git(repo, "rev-parse", "HEAD").strip()
    result = partition_slices(repo, base, ["seed.py"], "bad", head="no-such-ref-xyz")
    assert result["error"]
    assert "numstat" in result["error"]
    assert result["slices"] == [] and result["verdict"] is None


def test_range_mode_freezes_each_partition_slice_with_only_its_own_paths(repo):
    base, terminal, rels, _declared = _peer_tree(repo)
    result = partition_slices(repo, base, rels, "own", head=terminal)
    assert result["error"] is None and len(result["slices"]) > 1
    for s in result["slices"]:
        text = Path(s["diff_path"]).read_text(encoding="utf-8")
        for other in rels:
            assert (f"b/{other}" in text) == (other in s["paths"])


def _peer_then_own(repo: Path):
    base = _git(repo, "rev-parse", "HEAD").strip()
    rels = [f"pkg{i % 8}/sub{i % 3}/mod{i}.py" for i in range(19)]
    _write(repo, rels, 30)
    _write(repo, ["pkg0/sub0/shared.py"], 40)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "peer work")
    peer = _git(repo, "rev-parse", "HEAD").strip()
    shared = repo / "pkg0" / "sub0" / "shared.py"
    shared.write_text("v = 1\n" * 40 + "own_line = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "own work")
    terminal = _git(repo, "rev-parse", "HEAD").strip()
    return base, peer, terminal, rels + ["pkg0/sub0/shared.py"]


def test_path_bases_excludes_peer_hunks_and_keeps_own(repo):
    base, peer, terminal, declared = _peer_then_own(repo)
    plain = partition_slices(repo, base, declared, "pb0", head=terminal)
    mapped = partition_slices(
        repo, base, declared, "pb1", head=terminal, path_bases={"pkg0/sub0/shared.py": peer}
    )
    assert plain["error"] is None and mapped["error"] is None
    assert mapped["loc"] == plain["loc"] - 40
    shared_diffs = [
        Path(s["diff_path"]).read_text(encoding="utf-8")
        for s in mapped["slices"]
        if "pkg0/sub0/shared.py" in s["paths"]
    ]
    assert len(shared_diffs) == 1
    assert "own_line" in shared_diffs[0] and "+v = 1" not in shared_diffs[0]
    assert sorted(p for s in mapped["slices"] for p in s["paths"]) == sorted(declared)


def test_empty_path_bases_matches_none_byte_for_byte(repo):
    base, _peer, terminal, declared = _peer_then_own(repo)
    a = partition_slices(repo, base, declared, "nb-a", head=terminal)
    b = partition_slices(repo, base, declared, "nb-b", head=terminal, path_bases={})
    assert (a["loc"], a["product_paths"], [s["paths"] for s in a["slices"]]) == (
        b["loc"], b["product_paths"], [s["paths"] for s in b["slices"]]
    )
    for sa, sb in zip(a["slices"], b["slices"]):
        assert Path(sa["diff_path"]).read_bytes() == Path(sb["diff_path"]).read_bytes()


def test_op_is_registered_with_a_handler_and_complete_registration():
    from coordinator_core.ipc import get_op_handler  # noqa: PLC0415
    import coordinator_core.ops  # noqa: F401, PLC0415
    from coordinator_core.ops._registry_map import OP_MODULE_MAP  # noqa: PLC0415

    assert get_op_handler("review.partition_slices") is not None
    assert OP_MODULE_MAP["review.partition_slices"] == "coordinator_core.ops.review_partition_slices"
