"""Tests for coordinator/bin/release-cut.py, built on a tmp_path repo."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "release-cut.py"
_spec = importlib.util.spec_from_file_location("release_cut", _SCRIPT)
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)

_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _g(root: Path, *args: str) -> str:
    import os
    cp = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                        env={**os.environ, **_ENV})
    assert cp.returncode == 0, cp.stderr
    return cp.stdout.strip()


def _commit(root: Path, files: dict[str, str], msg: str) -> str:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8", newline="\n")
    _g(root, "add", "-A")
    _g(root, "commit", "-q", "-m", msg)
    return _g(root, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    root = tmp_path / "repo"
    root.mkdir()
    _g(root, "init", "-q", "-b", "main")
    base = _commit(root, {"a.txt": "1\n2\n3\n", "b.txt": "b\n", "docs/x.md": "d\n"}, "base")
    c1 = _commit(root, {"a.txt": "1\nX\n3\n", "docs/x.md": "d2\n"}, "c1")
    c2 = _commit(root, {"b.txt": "b2\n"}, "c2")
    c3 = _commit(root, {"a.txt": "1\nY\n3\n"}, "c3")
    _g(root, "branch", "release/base", base)
    return {"root": root, "base": base, "c1": c1, "c2": c2, "c3": c3}


def _manifest(tmp_path: Path, **kw) -> Path:
    data = {"name": "t1", "base": "release/base", "picks": [], "together": []}
    data.update(kw)
    p = tmp_path / "m.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _run(repo, m: Path) -> int:
    return rc.main([str(m), "--repo-root", str(repo["root"])])


def _ref_exists(root: Path) -> bool:
    cp = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "refs/heads/release/t1"],
                        cwd=root, capture_output=True)
    return cp.returncode == 0


def test_partly_present_group_refused_naming_missing_member(repo, tmp_path, capsys):
    m = _manifest(tmp_path, picks=[repo["c2"]], together=[[repo["c2"][:10], repo["c1"][:10]]])
    assert _run(repo, m) == 1
    err = capsys.readouterr().err
    assert repo["c1"][:10] in err and "missing member" in err
    assert not _ref_exists(repo["root"])


def test_group_with_both_members_or_neither_is_accepted(repo, tmp_path):
    both = _manifest(tmp_path, picks=[repo["c1"], repo["c2"]], together=[[repo["c1"], repo["c2"]]])
    assert _run(repo, both) == 0
    _g(repo["root"], "update-ref", "-d", "refs/heads/release/t1")
    neither = _manifest(tmp_path, picks=[repo["c2"]], together=[[repo["c1"], repo["c3"]]])
    assert _run(repo, neither) == 0


def test_unresolvable_group_member_refused(repo, tmp_path, capsys):
    m = _manifest(tmp_path, picks=[repo["c2"]], together=[[repo["c2"], "deadbeef00"]])
    assert _run(repo, m) == 1
    assert "deadbeef00" in capsys.readouterr().err
    assert not _ref_exists(repo["root"])


def test_failed_apply_names_pick_and_writes_no_ref(repo, tmp_path, capsys):
    m = _manifest(tmp_path, picks=[repo["c2"], repo["c3"]])
    assert _run(repo, m) == 1
    err = capsys.readouterr().err
    assert f"pick {repo['c3']} failed to apply" in err
    assert not _ref_exists(repo["root"])


def test_valid_cut_writes_ref_with_old_value_and_leaves_shared_state(repo, tmp_path, capsys, monkeypatch):
    root = repo["root"]
    (root / "untracked.txt").write_text("dirt\n")
    index_before = (root / ".git" / "index").read_bytes()
    head_before = _g(root, "rev-parse", "HEAD")
    status_before = _g(root, "status", "--porcelain")

    calls: list[tuple] = []
    real = rc._git

    def spy(r, *args, **kw):
        calls.append(args)
        return real(r, *args, **kw)

    monkeypatch.setattr(rc, "_git", spy)
    m = _manifest(tmp_path, picks=[repo["c1"], repo["c2"], repo["c3"]])
    assert _run(repo, m) == 0
    out = capsys.readouterr().out

    commit = _g(root, "rev-parse", "refs/heads/release/t1")
    assert _g(root, "rev-parse", f"{commit}^") == repo["base"]
    assert _g(root, "show", f"{commit}:a.txt") == "1\nY\n3"
    assert _g(root, "show", f"{commit}:b.txt") == "b2"
    assert _g(root, "show", f"{commit}:docs/x.md") == "d"
    assert "git clone --single-branch -b release/t1" in out

    ur = [c for c in calls if c[0] == "update-ref"]
    assert len(ur) == 1 and ur[0][-1] == "0" * 40
    assert not any(c[0] in ("push", "fetch", "checkout", "cherry-pick", "worktree", "init") for c in calls)
    assert (root / ".git" / "index").read_bytes() == index_before
    assert _g(root, "rev-parse", "HEAD") == head_before
    assert _g(root, "status", "--porcelain") == status_before
    assert not list((root / ".git").glob("release-cut-*"))


def test_existing_ref_refused_unless_replace_and_old_value_passed(repo, tmp_path, monkeypatch):
    m = _manifest(tmp_path, picks=[repo["c2"]])
    assert _run(repo, m) == 0
    first = _g(repo["root"], "rev-parse", "refs/heads/release/t1")
    assert _run(repo, m) == 1
    assert _g(repo["root"], "rev-parse", "refs/heads/release/t1") == first
    calls: list[tuple] = []
    real = rc._git
    monkeypatch.setattr(rc, "_git", lambda r, *a, **k: (calls.append(a), real(r, *a, **k))[1])
    assert rc.main([str(m), "--repo-root", str(repo["root"]), "--replace"]) == 0
    assert [c for c in calls if c[0] == "update-ref"][0][-1] == first


def test_default_invocation_as_subprocess(repo, tmp_path):
    import os
    m = _manifest(tmp_path, picks=[repo["c2"]])
    cp = subprocess.run([sys.executable, str(_SCRIPT), str(m), "--repo-root", str(repo["root"])],
                        capture_output=True, text=True, env={**os.environ, **_ENV})
    assert cp.returncode == 0, cp.stderr
    assert "clean-clone:" in cp.stdout
    assert _ref_exists(repo["root"])
