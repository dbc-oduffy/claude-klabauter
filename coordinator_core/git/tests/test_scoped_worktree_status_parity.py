"""scoped_worktree_status equals the (changed, ignored) parsed from real
`git status --porcelain=v2 --ignored=matching` for `_worktree_snapshot`'s argv, or is None
(the caller spawns). One fixture per gap-register row plus a combined ask-run shape. A fixture
that must answer in process asserts non-None so a gate that swallows it fails loudly."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import scoped_worktree_status as sws
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )
    return done.stdout


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in sws._GATED_ENV:
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "core.filemode", "false")
    return root


def _put(root: Path, rel: str, text: str = "x\n") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _commit(root: Path, *rels: str) -> None:
    _git(root, "add", "-f", "--", *rels)
    _git(root, "commit", "-q", "-m", "c")


def _settle(root: Path) -> None:
    """Age every worktree file past the index mtime so no entry is racily clean."""
    import os
    import time

    past = time.time() - 30
    for path in root.rglob("*"):
        if ".git" in path.parts or not path.is_file():
            continue
        os.utime(path, (past, past))
    _git(root, "update-index", "--refresh", "-q")


def _real(root: Path, literals: list, roots: list, scope_specs: list):
    literals = list(dict.fromkeys(literals))
    roots = [r.rstrip("/") for r in dict.fromkeys(roots)]
    specs = [f":(literal){p}" for p in literals] + [f":(literal){r}/" for r in roots] + list(scope_specs)
    out = subprocess.run(
        ["git", "-C", str(root), "--no-optional-locks", "status", "--porcelain=v2", "-z",
         "--no-renames", "--untracked-files=all", "--ignored=matching", "--", *specs],
        capture_output=True, **no_console_creationflags(),
    ).stdout
    changed: set = set()
    ignored: list = []
    for raw in out.split(b"\0"):
        if len(raw) < 3:
            continue
        kind = raw[:1]
        fields = {b"1": 8, b"u": 10, b"?": 1, b"!": 1}.get(kind)
        if fields is None:
            continue
        path = raw.split(b" ", fields)[fields].decode("utf-8", "surrogateescape")
        (ignored.append(path) if kind == b"!" else changed.add(path))
    return changed, ignored


def _check(root, literals=(), roots=(), specs=(), *, answers=True):
    literals, roots, specs = list(literals), list(roots), list(specs)
    mine = sws.scoped_worktree_status(root, literals, roots, specs)
    if mine is None:
        assert not answers, "expected an in-process answer, got a fallback"
        return None
    real = _real(root, literals, roots, specs)
    assert (mine[0], sorted(mine[1])) == (real[0], sorted(real[1]))
    return mine


def test_modified_staged_deleted_untracked(repo):
    for rel in ("a/m.txt", "a/s.txt", "a/d.txt", "a/ok.txt", "b/o.txt"):
        _put(repo, rel)
    _commit(repo, "a/m.txt", "a/s.txt", "a/d.txt", "a/ok.txt", "b/o.txt")
    _settle(repo)
    _put(repo, "a/m.txt", "changed content\n")
    _put(repo, "a/s.txt", "staged content\n")
    _git(repo, "add", "a/s.txt")
    (repo / "a/d.txt").unlink()
    _put(repo, "a/new.txt")
    _put(repo, "b/o.txt", "other\n")
    got = _check(repo, ["a/m.txt", "a/new.txt", "a/ok.txt"], ["a"], [":(glob)b/*"])
    assert {"a/m.txt", "a/s.txt", "a/d.txt", "a/new.txt", "b/o.txt"} <= got[0]
    assert "a/ok.txt" not in got[0]


def test_staged_then_reverted_and_staged_delete(repo):
    _put(repo, "a/r.txt")
    _put(repo, "a/g.txt")
    _commit(repo, "a/r.txt", "a/g.txt")
    _settle(repo)
    _put(repo, "a/r.txt", "temp\n")
    _git(repo, "add", "a/r.txt")
    _put(repo, "a/r.txt", "x\n")
    _git(repo, "rm", "-q", "--cached", "a/g.txt")
    _check(repo, ["a/r.txt", "a/g.txt"], ["a"], [":(glob)a/*"])


def test_nested_gitignore_negation_and_dir_only(repo):
    _put(repo, ".gitignore", "*.log\nbuild/\n!keep.log\n")
    _put(repo, "a/.gitignore", "!pin.log\nsecret.txt\n")
    _put(repo, "a/seed.txt")
    _commit(repo, ".gitignore", "a/.gitignore", "a/seed.txt")
    _settle(repo)
    for rel in ("a/x.log", "a/pin.log", "keep.log", "a/secret.txt", "a/build/o.bin", "a/plain.txt",
                "build", "a/n/deep.log"):
        _put(repo, rel)
    got = _check(repo, ["a/plain.txt"], ["a"], [":(glob)*"])
    assert "a/plain.txt" in got[0]
    assert "a/build/" in got[1]


def test_info_exclude_and_core_excludes_file(repo, tmp_path):
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git/info/exclude").write_text("*.tmp\n", encoding="utf-8")
    glob = tmp_path / "global-ignore"
    glob.write_text("*.cache\n", encoding="utf-8")
    _git(repo, "config", "core.excludesFile", str(glob).replace("\\", "/"))
    _put(repo, "a/seed.txt")
    _commit(repo, "a/seed.txt")
    for rel in ("a/x.tmp", "a/y.cache", "a/z.txt"):
        _put(repo, rel)
    got = _check(repo, [], ["a"], [])
    assert got[1] and "a/z.txt" in got[0]


def test_negation_cannot_reinclude_under_an_excluded_parent(repo):
    _put(repo, ".gitignore", "out/\n!out/keep.txt\n")
    _put(repo, "seed.txt")
    _commit(repo, ".gitignore", "seed.txt")
    _put(repo, "out/keep.txt")
    _check(repo, ["out/keep.txt"], [], [":(glob)*"])


def test_literal_inside_an_ignored_directory_reports_the_directory(repo):
    _put(repo, ".gitignore", "cache/\n")
    _put(repo, "seed.txt")
    _commit(repo, ".gitignore", "seed.txt")
    _put(repo, "cache/a.txt")
    _check(repo, ["cache/a.txt", "cache/missing.txt"], [], [])


def test_crlf_touch_under_autocrlf_true(repo):
    _git(repo, "config", "core.autocrlf", "true")
    _put(repo, "a/c.txt", "one\ntwo\n")
    _commit(repo, "a/c.txt")
    _settle(repo)
    _check(repo, ["a/c.txt"], ["a"], [])
    _put(repo, "a/c.txt", "one\r\ntwo\r\n")
    _check(repo, ["a/c.txt"], ["a"], [])


def test_crlf_text_auto_clean_and_modified_agree_with_git(repo):
    _git(repo, "config", "core.autocrlf", "true")
    _put(repo, ".gitattributes", "* text=auto\n")
    _put(repo, "a/c.txt", "one\ntwo\nthree\n")
    _commit(repo, ".gitattributes", "a/c.txt")
    _settle(repo)
    _put(repo, "a/c.txt", "one\r\ntwo\r\nthree\r\n")
    # Real git is the oracle: it lists a CRLF worktree file over an LF index blob as `.M` even
    # though the content is equal after the clean filter; _check pins the snapshot to that.
    _check(repo, ["a/c.txt"], ["a"], [], answers=False)
    _put(repo, "a/c.txt", "one\r\nTWO\r\nthree\r\n")
    changed = _check(repo, ["a/c.txt"], ["a"], [], answers=False)
    if changed is not None:
        assert "a/c.txt" in changed[0]


def test_clean_filter_and_text_attribute_fall_back_or_agree(repo):
    _put(repo, ".gitattributes", "*.dat filter=nope\n*.txt text eol=crlf\n")
    _put(repo, "a/f.dat", "1\n")
    _put(repo, "a/t.txt", "1\n")
    _commit(repo, ".gitattributes", "a/f.dat", "a/t.txt")
    _settle(repo)
    _put(repo, "a/f.dat", "2\n")
    _put(repo, "a/t.txt", "2\n")
    _check(repo, [], ["a"], [], answers=False)


def test_assume_unchanged_and_skip_worktree_fall_back(repo):
    _put(repo, "a/au.txt")
    _put(repo, "a/sw.txt")
    _commit(repo, "a/au.txt", "a/sw.txt")
    _settle(repo)
    _git(repo, "update-index", "--assume-unchanged", "a/au.txt")
    _git(repo, "update-index", "--skip-worktree", "a/sw.txt")
    _put(repo, "a/au.txt", "edited\n")
    assert _check(repo, [], ["a"], [], answers=False) is None


def test_submodule_gitlink_and_gitmodules_fall_back(repo, tmp_path):
    _put(repo, "seed.txt")
    _commit(repo, "seed.txt")
    sub = tmp_path / "sub"
    sub.mkdir()
    _git(sub, "init", "-q")
    _git(sub, "config", "user.email", "t@example.com")
    _git(sub, "config", "user.name", "t")
    (sub / "f").write_text("1", encoding="utf-8")
    _git(sub, "add", "f")
    _git(sub, "commit", "-q", "-m", "s")
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "mod")
    assert _check(repo, ["seed.txt"], ["mod"], [], answers=False) is None


def test_unmerged_and_intent_to_add_fall_back(repo):
    _put(repo, "a/u.txt", "base\n")
    _commit(repo, "a/u.txt")
    base = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    _git(repo, "checkout", "-q", "-b", "side")
    _put(repo, "a/u.txt", "side\n")
    _git(repo, "commit", "-q", "-am", "side")
    _git(repo, "checkout", "-q", base)
    _put(repo, "a/u.txt", "main\n")
    _git(repo, "commit", "-q", "-am", "main")
    subprocess.run(["git", "-C", str(repo), "merge", "side"], capture_output=True, **no_console_creationflags())
    assert _check(repo, ["a/u.txt"], ["a"], [], answers=False) is None
    _git(repo, "merge", "--abort")
    _put(repo, "a/i.txt")
    _git(repo, "add", "-N", "a/i.txt")
    assert _check(repo, ["a/i.txt"], ["a"], [], answers=False) is None


@pytest.mark.parametrize("setting", ["true", "false"])
def test_ignorecase_both_settings(repo, setting):
    _git(repo, "config", "core.ignorecase", setting)
    _put(repo, ".gitignore", "*.LOG\n")
    _put(repo, "a/seed.txt")
    _commit(repo, ".gitignore", "a/seed.txt")
    _put(repo, "a/x.log", "1\n")
    _check(repo, [], ["a"], [], answers=False)


def test_symlink_and_filemode_fall_back_or_agree(repo):
    _put(repo, "a/f.sh", "#!/bin/sh\n")
    _commit(repo, "a/f.sh")
    _settle(repo)
    _git(repo, "config", "core.filemode", "true")
    _check(repo, ["a/f.sh"], ["a"], [], answers=False)


@pytest.mark.parametrize(
    "specs",
    [[":/"], [":(glob)a/**"], [":(glob)a/*.py"], [":(icase)a/x"]],
)
def test_unsupported_scope_specs_fall_back(repo, specs):
    _put(repo, "a/x.py")
    _commit(repo, "a/x.py")
    assert sws.scoped_worktree_status(repo, ["a/x.py"], [], specs) is None


def test_racily_clean_entry_falls_back_or_agrees(repo):
    _put(repo, "a/r.txt", "aaaa\n")
    _commit(repo, "a/r.txt")
    _put(repo, "a/r.txt", "bbbb\n")
    _check(repo, ["a/r.txt"], ["a"], [], answers=False)


def test_linked_worktree_agrees(repo, tmp_path):
    _put(repo, "a/f.txt")
    _commit(repo, "a/f.txt")
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", str(linked), "-b", "lnk")
    _settle(linked)
    _put(linked, "a/new.txt")
    _check(linked, ["a/f.txt", "a/new.txt"], ["a"], [":(glob)*"])


def test_git_dir_env_falls_back(repo, monkeypatch):
    _put(repo, "a/f.txt")
    _commit(repo, "a/f.txt")
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    assert sws.scoped_worktree_status(repo, ["a/f.txt"], [], []) is None


def test_injected_config_gates_unless_every_key_is_inert(repo, monkeypatch):
    _put(repo, "a/f.txt")
    _commit(repo, "a/f.txt")
    _put(repo, "a/new.txt")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "help.format")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "web")
    _check(repo, ["a/f.txt", "a/new.txt"], ["a"], [])
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.excludesFile")
    assert sws.scoped_worktree_status(repo, ["a/f.txt"], ["a"], []) is None


def test_combined_ask_run_shape(repo):
    _put(repo, ".gitignore", "*.pyc\n__pycache__/\n.coordinator-local/\n")
    for rel in ("plan.md", "src/mod.py", "src/old.py", "state/warp/run1/a.json", "state/other.md"):
        _put(repo, rel)
    _commit(repo, ".gitignore", "plan.md", "src/mod.py", "src/old.py", "state/warp/run1/a.json",
            "state/other.md")
    _settle(repo)
    _put(repo, "src/mod.py", "changed\n")
    (repo / "src/old.py").unlink()
    _put(repo, "src/new.py")
    _put(repo, "src/__pycache__/new.pyc")
    _put(repo, "state/warp/run1/b.json")
    _put(repo, "state/warp/run1/skip.pyc")
    _put(repo, "state/warp/run2/c.json")
    _put(repo, ".coordinator-local/rep.md")
    got = _check(
        repo,
        ["plan.md", "src/mod.py", "src/new.py", "src/old.py", ".coordinator-local/rep.md"],
        ["state/warp"],
        [":(glob)src/*", ":(glob)state/*", ":(glob)*"],
    )
    assert {"src/mod.py", "src/old.py", "src/new.py", "state/warp/run1/b.json"} <= got[0]
    assert "src/__pycache__/" in got[1] and ".coordinator-local/" in got[1]
