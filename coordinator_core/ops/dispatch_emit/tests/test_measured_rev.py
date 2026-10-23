"""measured_rev: candidate tree equals HEAD plus the write set, refs untouched, zero spawns."""

import json
import subprocess

import pytest

from coordinator_core.git import commit
from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.ops.dispatch_emit.ask_contract import MEASURED_REV_KEYS, measured_rev_record_path


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
    (repo / "edit.txt").write_text("new\n")
    (repo / "gone.txt").unlink()
    (repo / "fresh").mkdir()
    (repo / "fresh" / "n.txt").write_text("n\n")
    head_before = _git(repo, "rev-parse", "HEAD")
    ref_before = (repo / ".git" / "refs" / "heads" / "main").read_bytes()
    index_before = (repo / ".git" / "index").read_bytes()
    before_tree = _tree(repo, "HEAD")

    _no_spawn(monkeypatch)
    out = mr.measured_rev("run-1", ["edit.txt", "gone.txt", "fresh/n.txt"], repo_root=repo)
    monkeypatch.undo()

    assert tuple(out) == MEASURED_REV_KEYS
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
    (repo / "x" / "y" / "z.txt").write_text("z\n")
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
    (repo / "edit.txt").write_text("new\n")
    ok = mr._handler({"run_id": "run-h", "paths": ["edit.txt"]}, repo)
    assert tuple(ok) == MEASURED_REV_KEYS


def test_refuses_when_no_head(tmp_path):
    _git(tmp_path, "init", "-q")
    with pytest.raises(mr.MeasuredRevRefused):
        mr.measured_rev("run-5", ["a.txt"], repo_root=tmp_path)


def test_refuses_unsupported_filter(repo):
    (repo / ".gitattributes").write_text("*.pin text eol=crlf\n")
    (repo / "a.pin").write_bytes(b"x\r\n")
    with pytest.raises(mr.MeasuredRevRefused):
        mr.measured_rev("run-6", ["a.pin"], repo_root=repo)
