from coordinator_core.ops.ceremony.commit_gates import (
    is_never_commit,
    residue_gate,
    split_process_paths,
)

MIXED = ["src/app.py", "state/handoffs/x.md"]


def test_never_commit_refused(tmp_path):
    out = residue_gate(tmp_path, ["src/app.py", "scratch/x.py"])
    assert not out.passed and "scratch/x.py" in out.diagnostics[0]


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
