"""Parity of `IgnoreMatcher.report` against the `!` records of real
`git status --porcelain=v2 -z --no-renames --untracked-files=all --ignored=matching`.

Each fixture is a tmp_path repo; candidates are every untracked file in the worktree. A matcher
`None` is a declared gap and is counted, never a pass for a case that must be handled.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from coordinator_core.git.ignore_match import IgnoreMatcher

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@pytest.fixture(autouse=True)
def _isolated_git_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(var, str(home) if var != "XDG_CONFIG_HOME" else str(home / "xdg"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        check=True,
        creationflags=_NO_WINDOW,
    )
    return done.stdout.decode("utf-8", "replace")


def _make_repo(root: Path, files: Dict[str, str], ignore_case: bool = False) -> None:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.ignorecase", "true" if ignore_case else "false")
    _git(root, "config", "core.autocrlf", "false")
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


def _real_ignored(root: Path) -> List[str]:
    raw = _git(
        root, "status", "--porcelain=v2", "-z", "--no-renames",
        "--untracked-files=all", "--ignored=matching",
    )
    return [rec[2:] for rec in raw.split("\0") if rec.startswith("! ")]


def _untracked_files(root: Path) -> List[str]:
    tracked = set(_git(root, "ls-files", "-z").split("\0"))
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            rel = (Path(dirpath) / name).relative_to(root).as_posix()
            if rel not in tracked:
                found.append(rel)
    return found


def _assert_parity(root: Path, tracked_dirs=()) -> None:
    candidates = [(rel, False) for rel in _untracked_files(root)]
    mine = IgnoreMatcher(root).report(candidates, tracked_dirs=tracked_dirs)
    assert mine is not None, "matcher declared a gap on a case that must be handled"
    assert sorted(mine) == sorted(_real_ignored(root))


def test_root_gitignore_basic_and_wildcards(tmp_path):
    root = tmp_path / "r"
    _make_repo(root, {
        ".gitignore": "*.log\nbuild/\n/top.txt\n*.o\n!keep.o\nq?.tmp\n[ab].dat\n",
        "x.log": "", "sub/y.log": "", "top.txt": "", "sub/top.txt": "",
        "build/a.c": "", "build/deep/b.c": "", "sub/build/c.c": "",
        "keep.o": "", "z.o": "", "q1.tmp": "", "q10.tmp": "", "a.dat": "", "c.dat": "",
        "src/main.c": "",
    })
    _assert_parity(root)


def test_nested_gitignore_deepest_wins(tmp_path):
    root = tmp_path / "r"
    _make_repo(root, {
        ".gitignore": "*.gen\n",
        "a/.gitignore": "!keep.gen\n/only-here.txt\nlocal/\n",
        "a/keep.gen": "", "a/other.gen": "", "a/only-here.txt": "",
        "a/b/only-here.txt": "", "a/local/f": "", "a/b/local/g": "",
        "a/b/.gitignore": "*.txt\n!x.txt\n",
        "a/b/y.txt": "", "a/b/x.txt": "", "a/b/c/z.txt": "", "top.gen": "",
    })
    _assert_parity(root)


def test_info_exclude(tmp_path):
    root = tmp_path / "r"
    _make_repo(root, {"a.secret": "", "d/b.secret": "", "d/ok.txt": "", "priv/x": ""})
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "exclude").write_text("*.secret\npriv/\n", encoding="utf-8")
    _assert_parity(root)


def test_gitignore_beats_info_exclude(tmp_path):
    root = tmp_path / "r"
    _make_repo(root, {".gitignore": "!a.secret\n", "a.secret": "", "b.secret": ""})
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "exclude").write_text("*.secret\n", encoding="utf-8")
    _assert_parity(root)


def test_core_excludes_file_absolute(tmp_path):
    root = tmp_path / "r"
    _make_repo(root, {"a.glob": "", "d/b.glob": "", "keep.txt": ""})
    excludes = tmp_path / "global-ignore"
    excludes.write_text("*.glob\n", encoding="utf-8")
    _git(root, "config", "core.excludesFile", excludes.as_posix())
    _assert_parity(root)


def test_core_excludes_file_default_xdg(tmp_path, monkeypatch):
    root = tmp_path / "r"
    xdg = tmp_path / "xdgcfg"
    (xdg / "git").mkdir(parents=True)
    (xdg / "git" / "ignore").write_text("*.xdg\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    _make_repo(root, {"a.xdg": "", "b.txt": ""})
    _assert_parity(root)


def test_core_excludes_file_relative_is_a_gap(tmp_path):
    root = tmp_path / "r"
    _make_repo(root, {"a.glob": ""})
    _git(root, "config", "core.excludesFile", "relative/ignore")
    assert IgnoreMatcher(root).ignored("a.glob", False) is None


def test_config_include_is_a_gap(tmp_path):
    root = tmp_path / "r"
    _make_repo(root, {"a.txt": ""})
    with open(root / ".git" / "config", "a", encoding="utf-8") as cfg:
        cfg.write('[include]\n\tpath = other\n')
    assert IgnoreMatcher(root).ignored("a.txt", False) is None


def test_negation_cannot_reinclude_under_excluded_parent(tmp_path):
    root = tmp_path / "r"
    _make_repo(root, {
        ".gitignore": "out/\n!out/keep.txt\nlog*\n!logs/\n",
        "out/keep.txt": "", "out/other.txt": "", "logs/a.txt": "", "logfile": "",
    })
    _assert_parity(root)


def test_negation_reincludes_when_parent_not_excluded(tmp_path):
    root = tmp_path / "r"
    _make_repo(root, {
        ".gitignore": "out/*\n!out/keep.txt\n",
        "out/keep.txt": "", "out/other.txt": "", "out/sub/z.txt": "",
    })
    _assert_parity(root)


def test_directory_only_patterns(tmp_path):
    root = tmp_path / "r"
    _make_repo(root, {
        ".gitignore": "cache/\nfoo/\n",
        "cache": "a file named cache", "d/cache/f": "", "foo/bar": "",
        "d/foo": "a file named foo", "d/e/foo/x": "",
    })
    _assert_parity(root)


def test_anchoring_leading_middle_trailing_slash(tmp_path):
    root = tmp_path / "r"
    _make_repo(root, {
        ".gitignore": "/lead.txt\nmid/dle.txt\n/anch/\nsub/trail/\n",
        "lead.txt": "", "x/lead.txt": "", "mid/dle.txt": "", "x/mid/dle.txt": "",
        "anch/f": "", "x/anch/f": "", "sub/trail/f": "", "y/sub/trail/f": "",
    })
    _assert_parity(root)


def test_doublestar_forms(tmp_path):
    root = tmp_path / "r"
    _make_repo(root, {
        ".gitignore": "**/deep.txt\na/**/z.txt\nlogs/**\n",
        "deep.txt": "", "p/q/deep.txt": "", "a/z.txt": "", "a/b/c/z.txt": "", "b/z.txt": "",
        "logs/one": "", "logs/two/three": "", "other/logs/one": "",
    })
    _assert_parity(root)


def test_escapes_comments_and_trailing_spaces(tmp_path):
    root = tmp_path / "r"
    _make_repo(root, {
        ".gitignore": "\\#hash.txt\n\\!bang.txt\n# a comment\ntrail.txt   \nsp\\ \n",
        "#hash.txt": "", "!bang.txt": "", "trail.txt": "", "sp ": "", "sp": "", "other": "",
    })
    _assert_parity(root)


def test_character_classes_and_ranges(tmp_path):
    root = tmp_path / "r"
    _make_repo(root, {
        ".gitignore": "f[0-9].txt\ng[!a-c].txt\nh[]x].txt\n",
        "f1.txt": "", "fa.txt": "", "gd.txt": "", "ga.txt": "", "h].txt": "", "hx.txt": "",
    })
    _assert_parity(root)


def test_star_does_not_cross_slash(tmp_path):
    root = tmp_path / "r"
    _make_repo(root, {
        ".gitignore": "a/*.txt\n*/mid.txt\n",
        "a/x.txt": "", "a/b/x.txt": "", "q/mid.txt": "", "q/r/mid.txt": "",
    })
    _assert_parity(root)


def test_ignored_dir_collapses_even_with_unignored_looking_children(tmp_path):
    root = tmp_path / "r"
    _make_repo(root, {
        ".gitignore": "node_modules/\n",
        "node_modules/a/b/c.js": "", "node_modules/d.js": "", "src/app.js": "",
    })
    _assert_parity(root)


def test_tracked_file_matching_pattern_is_not_reported(tmp_path):
    root = tmp_path / "r"
    _make_repo(root, {".gitignore": "*.log\n", "forced.log": "", "other.log": ""})
    _git(root, "add", "-f", "forced.log", ".gitignore")
    _assert_parity(root)


def test_ignored_dir_holding_tracked_file_is_a_gap_when_declared(tmp_path):
    root = tmp_path / "r"
    _make_repo(root, {".gitignore": "gen/\n", "gen/tracked.txt": "", "gen/untracked.txt": ""})
    _git(root, "add", "-f", "gen/tracked.txt")
    matcher = IgnoreMatcher(root)
    assert matcher.report([("gen/untracked.txt", False)], tracked_dirs=["gen"]) is None


def test_ignore_case_true_matches_case_insensitively(tmp_path):
    root = tmp_path / "r"
    _make_repo(root, {
        ".gitignore": "*.LOG\nBuild/\n",
        "a.log": "", "b.Log": "", "build/f": "", "BUILD/g": "", "keep.txt": "",
    }, ignore_case=True)
    if sorted(os.listdir(root)).count("build") + sorted(os.listdir(root)).count("BUILD") != 1:
        pytest.skip("case-sensitive filesystem cannot hold the case-folding fixture")
    _assert_parity(root)


def test_ignore_case_false_is_case_sensitive(tmp_path):
    root = tmp_path / "r"
    _make_repo(root, {".gitignore": "*.LOG\n", "a.log": "", "b.LOG": ""}, ignore_case=False)
    _assert_parity(root)


def test_posix_class_and_trailing_backslash_are_gaps(tmp_path):
    root = tmp_path / "r"
    _make_repo(root, {".gitignore": "[[:digit:]].txt\n", "1.txt": ""})
    assert IgnoreMatcher(root).ignored("1.txt", False) is None
    (root / ".gitignore").write_text("odd\\", encoding="utf-8")
    assert IgnoreMatcher(root).ignored("odd", False) is None


def test_combined_fixture(tmp_path):
    root = tmp_path / "r"
    _make_repo(root, {
        ".gitignore": "*.log\n/dist/\ntmp/\n!important.log\n",
        "a/.gitignore": "*.cache\n!pin.cache\nnested/\n",
        "a/x.cache": "", "a/pin.cache": "", "a/nested/f": "", "a/b/nested/g": "",
        "a/b/y.cache": "", "important.log": "", "run.log": "", "dist/o": "", "dist/p/q": "",
        "tmp/t": "", "src/tmp/u": "", "src/keep.c": "", "z/deep/w.log": "",
    })
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "exclude").write_text("*.scratch\n", encoding="utf-8")
    for rel in ("s.scratch", "a/s.scratch"):
        (root / rel).write_text("", encoding="utf-8")
    _assert_parity(root)


def test_matcher_spawns_no_process(tmp_path, monkeypatch):
    root = tmp_path / "r"
    _make_repo(root, {".gitignore": "*.log\n", "a.log": "", "b.txt": ""})
    calls: List[object] = []
    real_popen = subprocess.Popen

    def counting_popen(*args, **kwargs):
        calls.append(args)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", counting_popen)
    matcher = IgnoreMatcher(root)
    assert matcher.report([("a.log", False), ("b.txt", False)]) == ["a.log"]
    assert calls == []
