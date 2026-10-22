"""landing_export: HEAD plus the run's own landing paths, overlaid on a run-base export."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_export as le

pytestmark = pytest.mark.spawns_process


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


_TEMPLATE: list = []


def _build_template(root):
    root.mkdir()
    _git(root, "init", "-q")
    (root / "pkg").mkdir()
    (root / "pkg" / "own.py").write_bytes(b"own v1\r\n")
    (root / "pkg" / "peer.py").write_bytes(b"peer v1\n")
    (root / "pkg" / "gone.py").write_bytes(b"gone\n")
    (root / "old.txt").write_bytes(b"old\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base_sha = _git(root, "rev-parse", "HEAD")
    base = root / "scratch" / "base"
    for rel in ("pkg/own.py", "pkg/peer.py", "pkg/gone.py", "old.txt"):
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_bytes((root / rel).read_bytes())
    (root / "pkg" / "peer.py").write_bytes(b"peer v2 committed\n")
    (root / "new.txt").write_bytes(b"new at head\n")
    (root / "old.txt").unlink()
    _git(root, "add", "-A", "--", "pkg", "new.txt", "old.txt")
    _git(root, "commit", "-q", "-m", "head")
    return base_sha


@pytest.fixture
def repo(tmp_path):
    """Copy of a once-built base+head repo; the git build spawns run once per module."""
    import shutil
    import tempfile

    if not _TEMPLATE:
        tpl = Path(tempfile.mkdtemp(prefix="landexp-tpl-")) / "repo"
        _TEMPLATE.append((tpl, _build_template(tpl)))
    tpl, base_sha = _TEMPLATE[0]
    root = tmp_path / "repo"
    shutil.copytree(tpl, root)
    return root, base_sha, root / "scratch" / "base"


def _build(root, base_sha, base, paths, name="out"):
    return le.build_landing_export(
        root, root / "scratch" / name, paths, base_export=base, base_sha=base_sha
    )


def test_head_bytes_for_untouched_dirty_file_and_worktree_bytes_for_landing(repo):
    root, base_sha, base = repo
    (root / "pkg" / "peer.py").write_bytes(b"PEER DIRT\n")
    (root / "pkg" / "own.py").write_bytes(b"own v2 worktree\r\n")
    res = _build(root, base_sha, base, ["pkg/own.py"])
    out = Path(res.path)
    assert (out / "pkg" / "peer.py").read_bytes() == b"peer v2 committed\n"
    assert (out / "pkg" / "own.py").read_bytes() == b"own v2 worktree\r\n"
    assert (out / "new.txt").read_bytes() == b"new at head\n"
    assert not (out / "old.txt").exists()
    assert res.overlaid == ("pkg/own.py",)
    assert res.head_sha == _git(root, "rev-parse", "HEAD")


def test_deleted_landing_path_is_absent(repo):
    root, base_sha, base = repo
    (root / "pkg" / "gone.py").unlink()
    res = _build(root, base_sha, base, ["pkg/gone.py"])
    assert not (Path(res.path) / "pkg" / "gone.py").exists()
    assert res.removed == ("pkg/gone.py",)


def test_untracked_absent_landing_path_is_neither_overlaid_nor_removed(repo):
    root, base_sha, base = repo
    res = _build(root, base_sha, base, ["pkg/never.py"])
    assert res.overlaid == () and res.removed == ()


def test_out_dir_outside_scratch_refused(repo, tmp_path):
    root, base_sha, base = repo
    with pytest.raises(le.LandingExportError):
        le.build_landing_export(root, tmp_path / "elsewhere", ["pkg/own.py"], base_export=base, base_sha=base_sha)
    with pytest.raises(le.LandingExportError):
        le.build_landing_export(root, root / "out", ["pkg/own.py"], base_export=base, base_sha=base_sha)
    assert base.is_dir()


def test_dotdot_and_outside_repo_paths_refused(repo, tmp_path):
    root, base_sha, base = repo
    for bad in ("../x.py", "pkg/../../x.py", str(tmp_path / "other.py"), ".git/config"):
        with pytest.raises(le.LandingExportError):
            _build(root, base_sha, base, [bad])
    assert base.is_dir()


def test_missing_base_export_refused(repo):
    root, base_sha, _ = repo
    with pytest.raises(le.LandingExportError):
        le.build_landing_export(root, root / "scratch" / "o", ["pkg/own.py"])


def test_export_spawns_nothing(repo, monkeypatch):
    root, base_sha, base = repo
    calls = []
    real = subprocess.Popen.__init__

    def counting(self, *a, **k):
        calls.append(a)
        real(self, *a, **k)

    monkeypatch.setattr(subprocess.Popen, "__init__", counting)
    _build(root, base_sha, base, ["pkg/own.py"])
    assert calls == []


def test_peer_dirty_file_red_in_worktree_is_green_in_export(repo):
    root, base_sha, base = repo
    (root / "pkg" / "peer.py").write_bytes(b"VALUE = 'dirt'\n")
    (root / "pkg" / "test_peer_gate.py").write_bytes(
        b"import peer\n\n\ndef test_value():\n    assert peer.VALUE == 'ok'\n"
    )
    _git(root, "add", "pkg/test_peer_gate.py")
    (root / "pkg" / "peer.py").write_bytes(b"VALUE = 'ok'\n")
    _git(root, "add", "pkg/peer.py")
    _git(root, "commit", "-q", "-m", "gate")
    (root / "pkg" / "peer.py").write_bytes(b"VALUE = 'dirt'\n")

    def run(cwd):
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_peer_gate.py"],
            cwd=cwd / "pkg",
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).returncode

    assert run(root) != 0
    res = _build(root, base_sha, base, ["pkg/own.py"])
    assert run(Path(res.path)) == 0


def test_cli_prints_one_json_line_and_refuses_with_exit_2(repo, monkeypatch, capsys):
    root, base_sha, base = repo
    monkeypatch.chdir(root)
    assert le.main(["scratch/cli", "pkg/own.py", "--base-export", str(base), "--base-sha", base_sha]) == 0
    line = capsys.readouterr().out.strip()
    assert json.loads(line)["overlaid"] == ["pkg/own.py"]
    assert le.main(["/elsewhere", "pkg/own.py", "--base-export", str(base), "--base-sha", base_sha]) == 2
    assert "refused" in capsys.readouterr().out
