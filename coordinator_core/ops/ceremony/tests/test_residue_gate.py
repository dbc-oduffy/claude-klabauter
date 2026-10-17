import pytest

from coordinator_core.ops.ceremony.commit_gates import (
    is_never_commit,
    residue_gate,
    split_process_paths,
)

MIXED = ["src/app.py", "state/handoffs/x.md"]


def test_never_commit_refused(tmp_path):
    out = residue_gate(tmp_path, ["src/app.py", "scratch/x.py"])
    assert not out.passed and "scratch/x.py" in out.diagnostics[0]


def _repo(tmp_path, gitignore):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    (tmp_path / ".gitignore").write_text(gitignore)


@pytest.mark.spawns_process
def test_ignored_scratch_passes(tmp_path):
    _repo(tmp_path, "scratch/\n")
    assert residue_gate(tmp_path, ["scratch/a.py", "scratch\\b.py"]).passed


@pytest.mark.spawns_process
def test_unignored_scratch_still_refused_in_repo(tmp_path):
    _repo(tmp_path, "other/\n")
    out = residue_gate(tmp_path, ["scratch/a.py"])
    assert not out.passed and "scratch/a.py" in out.diagnostics[0]


@pytest.mark.spawns_process
def test_tracked_scratch_matching_gitignore_still_refused(tmp_path):
    import subprocess

    _repo(tmp_path, "")
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "a.py").write_text("x")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(["git", "add", "-f", "scratch/a.py"], cwd=tmp_path, check=True, creationflags=flags)
    (tmp_path / ".gitignore").write_text("scratch/\n")
    out = residue_gate(tmp_path, ["scratch/a.py"])
    assert not out.passed and "scratch/a.py" in out.diagnostics[0]


@pytest.mark.spawns_process
def test_mixed_ignored_and_unignored_refuses_only_unignored(tmp_path):
    _repo(tmp_path, "scratch/ign/\n")
    out = residue_gate(tmp_path, ["scratch/ign/a.py", "scratch/keep.py"])
    assert not out.passed
    assert "scratch/keep.py" in out.diagnostics[0] and not any("ign/a.py" in d for d in out.diagnostics)


def test_is_never_commit_matches_scratch_under_either_separator():
    assert is_never_commit("scratch/x.py")
    assert is_never_commit("scratch\\sub\\x.py")
    assert not is_never_commit("src/scratch/x.py")
    assert not is_never_commit("docs/scratch.md")


def test_coordinator_local_is_partitioned_not_refused(tmp_path):
    paths = ["src/app.py", ".coordinator-local/review/sidecar.md"]
    assert residue_gate(tmp_path, paths).passed
    assert split_process_paths(tmp_path, paths) == (
        ["src/app.py"], [".coordinator-local/review/sidecar.md"]
    )


def test_process_paths_not_refused(tmp_path):
    assert residue_gate(tmp_path, MIXED).passed


def test_split_mixed_preserves_order(tmp_path):
    paths = ["a.py", "state/x.md", "b.py", "docs/plans/p.md", ".coordinator-local/r.md", "archive/z"]
    product, process = split_process_paths(tmp_path, paths)
    assert product == ["a.py", "b.py"]
    assert process == ["state/x.md", "docs/plans/p.md", ".coordinator-local/r.md", "archive/z"]


def test_split_pure_classes(tmp_path):
    assert split_process_paths(tmp_path, ["a.py", "b.py"]) == (["a.py", "b.py"], [])
    assert split_process_paths(tmp_path, ["state/a.md"]) == ([], ["state/a.md"])
    assert split_process_paths(tmp_path, []) == ([], [])


def test_split_backslash_paths(tmp_path):
    product, process = split_process_paths(tmp_path, ["src\\app.py", "state\\handoffs\\x.md"])
    assert product == ["src\\app.py"]
    assert process == ["state\\handoffs\\x.md"]


def test_split_doe_sentinel_returns_all_product(tmp_path):
    (tmp_path / ".coordinator-dev-repo").write_text("")
    assert split_process_paths(tmp_path, MIXED) == (MIXED, [])
