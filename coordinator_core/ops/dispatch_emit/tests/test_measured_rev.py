"""measured_rev: candidate tree equals HEAD plus the write set, refs untouched, zero spawns."""

import json
import subprocess

import pytest

from coordinator_core.git import commit
from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.ops.dispatch_emit.ask_contract import MEASURED_REV_KEYS, measured_rev_record_path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "core.autocrlf", "false")
    (tmp_path / "keep.txt").write_text("keep\n")
    (tmp_path / "edit.txt").write_text("old\n")
    (tmp_path / "gone.txt").write_text("gone\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.txt").write_text("deep\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _tree(root, rev):
    out = _git(root, "ls-tree", "-r", rev)
    return {line.split("\t")[1]: line.split()[2] for line in out.splitlines()}


def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)


def test_tree_overlay_refs_untouched_zero_spawns(repo, monkeypatch):
    (repo / "edit.txt").write_bytes(b"new\n")
    (repo / "gone.txt").unlink()
    (repo / "fresh").mkdir()
    (repo / "fresh" / "n.txt").write_bytes(b"n\n")
    head_before = _git(repo, "rev-parse", "HEAD")
    ref_before = (repo / ".git" / "refs" / "heads" / "main").read_bytes()
    index_before = (repo / ".git" / "index").read_bytes()
    before_tree = _tree(repo, "HEAD")

    _no_spawn(monkeypatch)
    out = mr.measured_rev("run-1", ["edit.txt", "gone.txt", "fresh/n.txt"], repo_root=repo)
    monkeypatch.undo()

    assert tuple(out) == MEASURED_REV_KEYS + ("reach_dirt",)
    assert out["base_sha"] == head_before
    assert out["removed"] == ["gone.txt"]
    assert out["paths"] == ["edit.txt", "fresh/n.txt"]
    expected = dict(before_tree)
    expected.pop("gone.txt")
    expected["edit.txt"] = commit.checkin_blob_sha(repo, "edit.txt")
    expected["fresh/n.txt"] = commit.checkin_blob_sha(repo, "fresh/n.txt")
    assert _tree(repo, out["sha"]) == expected
    assert _git(repo, "rev-parse", out["sha"] + "^") == head_before

    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert (repo / ".git" / "refs" / "heads" / "main").read_bytes() == ref_before
    assert (repo / ".git" / "index").read_bytes() == index_before
    rec = json.loads(measured_rev_record_path(repo, "run-1").read_text())
    assert rec == out


def test_crlf_checkin_parity(repo):
    _git(repo, "config", "core.autocrlf", "true")
    (repo / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    out = mr.measured_rev("run-2", ["crlf.txt"], repo_root=repo)
    assert out["blobs"]["crlf.txt"] == commit.checkin_blob_sha(repo, "crlf.txt")
    assert _tree(repo, out["sha"])["crlf.txt"] == out["blobs"]["crlf.txt"]
    assert _git(repo, "cat-file", "blob", out["blobs"]["crlf.txt"]) == "a\nb"


def test_new_dir_under_missing_parent(repo):
    (repo / "x" / "y").mkdir(parents=True)
    (repo / "x" / "y" / "z.txt").write_bytes(b"z\n")
    out = mr.measured_rev("run-3", ["x/y/z.txt"], repo_root=repo)
    assert "x/y/z.txt" in _tree(repo, out["sha"])
    assert "sub/deep.txt" in _tree(repo, out["sha"])


@pytest.mark.parametrize(
    "bad",
    ["sub", "../outside.txt", "/abs.txt", "*.txt", "nope/*.txt"],
)
def test_refuses_non_file_paths(repo, bad):
    with pytest.raises(mr.MeasuredRevRefused):
        mr.measured_rev("run-4", [bad], repo_root=repo)


def test_refuses_traversal_run_id_before_writing_objects(repo):
    with pytest.raises(ValueError):
        mr.measured_rev("../escape", ["edit.txt"], repo_root=repo)


def test_handler_replies_typed_refusal_and_result(repo):
    bad = mr._handler({"run_id": "run-h", "paths": "edit.txt"}, repo)
    assert bad["refused"] == "measured-rev"
    refused = mr._handler({"run_id": "run-h", "paths": ["../x"]}, repo)
    assert refused["refused"] == "measured-rev"
    (repo / "edit.txt").write_bytes(b"new\n")
    ok = mr._handler({"run_id": "run-h", "paths": ["edit.txt"]}, repo)
    assert tuple(ok) == MEASURED_REV_KEYS + ("reach_dirt",)


def test_record_tree_is_candidate_tree(repo):
    (repo / "edit.txt").write_bytes(b"new\n")
    out = mr.measured_rev("run-t", ["edit.txt"], repo_root=repo)
    assert out["tree"] == _git(repo, "rev-parse", out["sha"] + "^{tree}")
    assert json.loads(measured_rev_record_path(repo, "run-t").read_text())["tree"] == out["tree"]


def test_no_whole_tree_export_surface():
    assert not hasattr(mr, "export_measured")
    assert not hasattr(mr, "run_git")


def test_handler_materialises_no_tree_and_spawns_nothing_even_with_export_param(repo, monkeypatch):
    (repo / "edit.txt").write_bytes(b"new\n")
    _no_spawn(monkeypatch)
    out = mr._handler({"run_id": "run-x", "paths": ["edit.txt"], "export": True}, repo)
    assert "export_dir" not in out and set(MEASURED_REV_KEYS) <= set(out)
    assert not (repo / "scratch" / "measured-rev").exists()


def test_drifted_write_set_blob_is_refused_by_the_drift_check(repo):
    (repo / "edit.txt").write_bytes(b"new\n")
    mr.measured_rev("run-d", ["edit.txt"], repo_root=repo)
    run_dir = measured_rev_record_path(repo, "run-d").parent
    assert mr.measured_rev_drift(run_dir, repo) == []
    (repo / "edit.txt").write_bytes(b"drifted\n")
    assert mr.measured_rev_drift(run_dir, repo) == ["edit.txt"]


def _reach_repo(root):
    (root / "pkg" / "tests" / "fixtures").mkdir(parents=True)
    (root / "pkg" / "tests" / "test_a.py").write_bytes(b"def test_a():\n    pass\n")
    (root / "pkg" / "tests" / "fixtures" / "data.json").write_bytes(b"{}\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "reach")


def test_reach_dirt_names_dirty_fixture_outside_landing_set(repo, monkeypatch):
    _reach_repo(repo)
    (repo / "pkg" / "tests" / "test_a.py").write_bytes(b"def test_a():\n    assert True\n")
    (repo / "pkg" / "tests" / "fixtures" / "data.json").write_bytes(b'{"dirty": 1}\n')
    head_blob = _git(repo, "rev-parse", "HEAD:pkg/tests/fixtures/data.json")
    _no_spawn(monkeypatch)
    out = mr.measured_rev("run-rd", ["pkg/tests/test_a.py"], repo_root=repo)
    monkeypatch.undo()
    assert out["reach_dirt"] == {"pkg/tests/fixtures/data.json": head_blob}
    rec = json.loads(measured_rev_record_path(repo, "run-rd").read_text())
    assert rec["reach_dirt"] == out["reach_dirt"]


def test_reach_dirt_empty_when_fixture_clean_or_in_landing_set(repo):
    _reach_repo(repo)
    test = "pkg/tests/test_a.py"
    fixture = "pkg/tests/fixtures/data.json"
    (repo / test).write_bytes(b"def test_a():\n    assert True\n")
    assert mr.measured_rev("run-c", [test], repo_root=repo)["reach_dirt"] == {}
    (repo / fixture).write_bytes(b'{"dirty": 1}\n')
    assert mr.measured_rev("run-l", [test, fixture], repo_root=repo)["reach_dirt"] == {}


def test_reach_dirt_seeds_from_landed_module_sibling_test(repo):
    _reach_repo(repo)
    (repo / "pkg" / "mod.py").write_bytes(b"X = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "mod")
    (repo / "pkg" / "mod.py").write_bytes(b"X = 2\n")
    (repo / "pkg" / "tests" / "fixtures" / "data.json").write_bytes(b'{"dirty": 1}\n')
    out = mr.measured_rev("run-m", ["pkg/mod.py"], repo_root=repo)
    assert list(out["reach_dirt"]) == []
    (repo / "pkg" / "tests" / "test_mod.py").write_bytes(b"def test_m():\n    pass\n")
    _git(repo, "add", "pkg/tests/test_mod.py")
    _git(repo, "commit", "-q", "-m", "sibling")
    out = mr.measured_rev("run-m2", ["pkg/mod.py"], repo_root=repo)
    assert list(out["reach_dirt"]) == ["pkg/tests/fixtures/data.json"]


def test_refuses_when_no_head(tmp_path):
    _git(tmp_path, "init", "-q")
    with pytest.raises(mr.MeasuredRevRefused):
        mr.measured_rev("run-5", ["a.txt"], repo_root=tmp_path)


def test_refuses_unsupported_filter(repo):
    (repo / ".gitattributes").write_text("*.pin text eol=crlf\n")
    (repo / "a.pin").write_bytes(b"x\r\n")
    with pytest.raises(mr.MeasuredRevRefused):
        mr.measured_rev("run-6", ["a.pin"], repo_root=repo)


def test_unparseable_py_is_refused_and_parseable_py_is_accepted(repo):
    (repo / "bad.py").write_bytes(b"def (:\n")
    (repo / "ok.py").write_bytes(b"x = 1\n")
    with pytest.raises(mr.MeasuredRevRefused, match="py-unparseable: bad.py"):
        mr.measured_rev("run-py", ["bad.py", "ok.py"], repo_root=repo)
    assert not measured_rev_record_path(repo, "run-py").exists()
    out = mr.measured_rev("run-py", ["ok.py"], repo_root=repo)
    assert out["paths"] == ["ok.py"]


def test_blob_equal_to_head_entry_is_not_rewritten_and_changed_blob_is(repo, monkeypatch):
    (repo / "same.txt").write_bytes(b"lf\n")
    _git(repo, "add", "same.txt")
    _git(repo, "commit", "-q", "-m", "same")
    (repo / "edit.txt").write_bytes(b"new\n")
    writes = []
    real = mr.write_object

    def spy(gitdir, kind, payload, **kw):
        writes.append((kind, payload))
        return real(gitdir, kind, payload, **kw)

    monkeypatch.setattr(mr, "write_object", spy)
    out = mr.measured_rev("run-same", ["same.txt", "edit.txt"], repo_root=repo)
    assert out["blobs"]["same.txt"] == _tree(repo, "HEAD")["same.txt"]
    blob_payloads = [p for k, p in writes if k == b"blob"]
    assert blob_payloads == [b"new\n"]


def test_unmeasurable_reach_is_a_typed_refusal_and_writes_no_record(repo, monkeypatch):
    (repo / "edit.txt").write_bytes(b"new\n")

    def boom(*a, **k):
        raise RuntimeError("status unreadable")

    monkeypatch.setattr(mr, "foreign_dirt", boom)
    with pytest.raises(mr.MeasuredRevRefused, match="test reach"):
        mr.measured_rev("run-reach", ["edit.txt"], repo_root=repo)
    assert not measured_rev_record_path(repo, "run-reach").exists()
    assert mr._handler({"run_id": "run-reach", "paths": ["edit.txt"]}, repo)["refused"] == "measured-rev"


def _stage_manifest(repo, run_id, declared, row_writes):
    run_dir = repo / "scratch" / "warp" / run_id
    run_dir.mkdir(parents=True)
    manifest = {"review_declared_paths": declared, "rows": [{"id": "C1", "writes": row_writes}]}
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_measure_unions_the_runs_own_manifest_writes_with_the_given_paths(repo, monkeypatch):
    """An empty `paths` re-measures the run's landing set: a row appended after the declared list was fixed
    is carried; a dirty path the run never wrote stays out of the record."""
    _stage_manifest(repo, "run-landing", ["edit.txt"], ["edit.txt", "sub/deep.txt", "scratch/warp/x.json"])
    (repo / "edit.txt").write_bytes(b"new\n")
    (repo / "sub" / "deep.txt").write_bytes(b"deeper\n")
    (repo / "keep.txt").write_bytes(b"foreign\n")

    _no_spawn(monkeypatch)
    out = mr.measured_rev("run-landing", [], repo_root=repo)
    monkeypatch.undo()

    assert out["paths"] == ["edit.txt", "sub/deep.txt"]
    assert "keep.txt" not in out["blobs"]


def test_a_run_without_a_record_measures_exactly_the_given_paths(repo):
    (repo / "edit.txt").write_bytes(b"new\n")

    assert mr.measured_rev("run-bare", ["edit.txt"], repo_root=repo)["paths"] == ["edit.txt"]


def test_handler_measures_the_worktree_when_ipc_passes_the_git_dir(repo):
    """IPC keys this common_dir op on `.git`; the handler must still find HEAD and measure the worktree."""
    (repo / "edit.txt").write_bytes(b"new\n")
    out = mr._handler({"run_id": "run-gitdir", "paths": ["edit.txt"]}, repo / ".git")
    assert "error" not in out, out
    assert out["base_sha"] == _git(repo, "rev-parse", "HEAD").strip()


def _cr_pinned_repo(repo):
    (repo / ".gitignore").write_text("build/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "ignore build")


def test_cr_bearing_path_is_refused_unless_ignored_or_withheld_control(repo):
    _cr_pinned_repo(repo)
    (repo / "edit.txt").write_bytes(b"new\n")
    (repo / "loose.bin").write_bytes(b"a\r\nb\x00\r\n")
    with pytest.raises(mr.MeasuredRevRefused, match="CR bytes"):
        mr.measured_rev("run-cr0", ["edit.txt", "loose.bin"], repo_root=repo)


def test_ignored_untracked_cr_binary_in_the_paths_is_never_measured(repo):
    _cr_pinned_repo(repo)
    (repo / "edit.txt").write_bytes(b"new\n")
    (repo / "build").mkdir()
    (repo / "build" / "door.bin").write_bytes(b"\x7fELF\r\n\x00\r\n")
    out = mr.measured_rev("run-cr1", ["edit.txt", "build/door.bin"], repo_root=repo)
    assert out["paths"] == ["edit.txt"]
    assert "build/door.bin" not in out["blobs"] and "build/door.bin" not in out["removed"]
    assert "build/door.bin" not in _tree(repo, out["sha"])


def test_ignored_untracked_binary_in_the_run_write_set_is_never_measured(repo, monkeypatch):
    _cr_pinned_repo(repo)
    (repo / "edit.txt").write_bytes(b"new\n")
    (repo / "build").mkdir()
    (repo / "build" / "door.bin").write_bytes(b"\x7fELF\r\n\x00\r\n")
    monkeypatch.setattr(mr, "_run_landing_paths", lambda root, run_id: ["build/door.bin", "edit.txt"])
    out = mr.measured_rev("run-cr2", [], repo_root=repo)
    assert out["paths"] == ["edit.txt"]


def test_tracked_path_stays_measured_even_if_gitignore_matches_it(repo):
    _cr_pinned_repo(repo)
    (repo / "build").mkdir()
    (repo / "build" / "t.txt").write_bytes(b"one\n")
    _git(repo, "add", "-f", "build/t.txt")
    _git(repo, "commit", "-q", "-m", "tracked-ignored")
    (repo / "build" / "t.txt").write_bytes(b"two\n")
    out = mr.measured_rev("run-cr3", ["build/t.txt"], repo_root=repo)
    assert out["paths"] == ["build/t.txt"]


def test_withheld_path_is_removed_from_every_input(repo):
    from coordinator_core.ops.dispatch_emit import run_withhold
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    _cr_pinned_repo(repo)
    (repo / "edit.txt").write_bytes(b"new\n")
    (repo / "loose.bin").write_bytes(b"a\r\nb\x00\r\n")
    home = record_dir(repo, "run-cr4")
    home.mkdir(parents=True, exist_ok=True)
    run_withhold.record(home, [{"path": "loose.bin", "reason": "per-machine product"}], "2026-11-04T00:00:00Z")
    out = mr.measured_rev("run-cr4", ["edit.txt", "loose.bin"], repo_root=repo)
    assert out["paths"] == ["edit.txt"]
    assert "loose.bin" not in out["removed"]
