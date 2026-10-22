"""Tests for coordinator_core.roadmap.census_inproc; every tree is built under tmp_path."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from coordinator_core.roadmap import census_inproc
from coordinator_core.roadmap.census_inproc import evaluate
from coordinator_core.roadmap.census_store import CensusStore

_OLD_NS = 1_600_000_000 * 10**9


class _Tree:
    """A FileSource over a real directory that counts the bodies it reads."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.reads: list = []

    def walk(self, rel):
        base = self.root / rel
        try:
            st = os.lstat(base)
        except OSError:
            return None
        if stat.S_ISREG(st.st_mode):
            return [(rel, st)]
        if not stat.S_ISDIR(st.st_mode):
            return None
        out = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for name in filenames:
                path = Path(dirpath) / name
                lst = os.lstat(path)
                if stat.S_ISREG(lst.st_mode):
                    out.append((path.relative_to(self.root).as_posix(), lst))
        return out

    def read(self, rel):
        self.reads.append(rel)
        try:
            return (self.root / rel).read_bytes()
        except OSError:
            return None


def _put(root: Path, rel: str, data) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    os.utime(path, ns=(_OLD_NS, _OLD_NS))


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _put(root, "src/top.py", "needle one\nplain\nNEEDLE two\nneedle needle\n")
    _put(root, "src/data.txt", "nothing here\nneed\n")
    _put(root, "src/pkg/mod.py", "def needle():\n    pass\nneedle\n")
    _put(root, "src/pkg/util.py", "x = 1\n")
    _put(root, "src/__pycache__/mod.cpython.pyc", b"\x00\x01needle\x02\xff\n")
    _put(root, "docs/notes.md", "needle in notes\nsecond\n")
    _put(root, "docs/crlf.md", "needle one\r\nneedle\r\nplain\r\n")
    _put(root, "docs/noeol.txt", "alpha\nneedle")
    _put(root, "odd/latin1.txt", b"caf\xe9 needle\n")
    _put(root, "lib/vendor/skip.py", "needle\n")
    _put(root, "lib/keep.py", "needle\n")
    try:
        os.symlink("../docs/notes.md", root / "src" / "link")
    except (OSError, NotImplementedError):
        pass
    (root / ".git").mkdir()
    _put(root, ".git/config", "needle\n")
    return _Tree(root)


def _docstring_examples() -> list:
    return re.findall(r"^    \$ (.+)$", census_inproc.__doc__, re.MULTILINE)


_REAL_SHAPES = [
    "grep -c 'def needle' src/pkg/mod.py",
    'grep -c "needle" src/top.py',
    "grep -n 'needle' src/pkg/mod.py | wc -l",
    "grep -rn --include=*.py 'needle' src | wc -l",
    'grep -rl --include="*.md" needle docs | wc -l',
    "find docs -name '*.md' | wc -l",
    "find src docs -type f | wc -l",
    "ls docs | wc -l",
    "ls src/pkg | wc -l",
    "grep -rl needle src docs lib | grep -v '/tests/' | wc -l",
    "grep -rl needle src docs lib | grep -v -F 'vendor' | wc -l",
    "grep -rlE 'needle|NEEDLE' src docs | wc -l",
    "grep -c '^needle' docs/notes.md",
    "grep -c 'needle$' docs/noeol.txt",
    "grep -c '[0-9]' src/top.py",
    "grep -rc 'needle' lib | wc -l",
    "grep -c -i 'NEEDLE' docs/crlf.md",
    "wc -l docs/noeol.txt",
    "grep -rl 'ne\\{2\\}dle' src | wc -l",
    "grep -rlw needle src/pkg | wc -l",
    'grep -rl "need\\(le\\)" src | wc -l',
    "grep -n needle docs/crlf.md",
    "grep -rl needle src/__pycache__ | wc -l",
]


def test_grammar_examples_and_real_shapes_are_answered(tree):
    for command in _docstring_examples() + _REAL_SHAPES:
        assert evaluate(command, tree, None) is not None, command


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_parity_with_the_host_shell(tree):
    sh = shutil.which("sh")
    if not sh:
        pytest.skip("no POSIX shell")
    env = {k: v for k, v in os.environ.items() if not k.startswith("BASH_FUNC_")}
    kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
    commands = _docstring_examples() + _REAL_SHAPES
    assert len(commands) >= 20 + 20
    for command in commands:
        proc = subprocess.run([sh, "-c", command], cwd=tree.root, capture_output=True,
                              text=True, env=env, **kw)
        got = evaluate(command, tree, None)
        assert got is not None, command
        assert (got[0], got[1].strip()) == (proc.returncode, proc.stdout.strip()), command
        assert got[2] == "", command


_DECLINED = [
    r"grep -c '\(a\)\1' src/top.py",
    "grep -c '[[:upper:]]' src/top.py",
    r"grep -c '\<needle' src/top.py",
    r"grep -c 'needle\>' src/top.py",
    r"grep -c 'a\+' src/top.py",
    r"grep -c 'a\?' src/top.py",
    "grep -c '*needle' src/top.py",
    "grep -c 'a**' src/top.py",
    "grep -c 'a^b' src/top.py",
    "grep -c 'a$b' src/top.py",
    "grep -cE 'a{x' src/top.py",
    "grep -cE '()' src/top.py",
    "grep -cE '(a' src/top.py",
    "grep -cE 'a|' src/top.py",
    "grep -c '[[.a.]]' src/top.py",
    "grep -c '[z-a]' src/top.py",
    "grep -c '' src/top.py",
    "grep -c 'café' src/top.py",
    "grep -rl needle . | wc -l",
    "grep -rl needle /abs | wc -l",
    "grep -rl needle ../x | wc -l",
    "grep -rl needle src/*.py | wc -l",
    "grep -rl needle $HOME | wc -l",
    "grep -rl needle ~/x | wc -l",
    "grep -rl needle nowhere | wc -l",
    "grep -c needle nowhere",
    "grep -rl needle src && true",
    "grep -rl needle src; ls src | wc -l",
    "grep -rl needle src > /dev/null",
    "grep -rl needle src 2>&1 | wc -l",
    "grep -rR needle src | wc -l",
    "grep -Rl needle src | wc -l",
    "grep -rl -e needle src | wc -l",
    "grep -rlm1 needle src | wc -l",
    "grep -rl needle src | grep -c x",
    "grep -rl needle src | sort | wc -l",
    "grep -rl needle src | grep -i x | wc -l",
    "grep -rl needle src | grep x file | wc -l",
    "grep -rn needle src | wc -l",
    "grep -rn needle src | grep -v x | wc -l",
    "grep -n needle src/__pycache__/mod.cpython.pyc",
    "grep -n needle odd/latin1.txt",
    "grep -c needle odd/latin1.txt",
    "grep -rl needle odd | wc -l",
    "grep -rl needle src odd | wc -l",
    "grep -rl needle src/link | wc -l",
    "grep -rl needle docs/notes.md/x | wc -l",
    "grep -rlo 'ne*dle' src | wc -l",
    "grep -ro 'a*' src/pkg | wc -l",
    "grep -ro 'needle\\|plain' src/pkg | wc -l",
    "grep -ov needle src/top.py",
    "grep -cl needle src/top.py",
    "grep -rl --include=*.py --exclude=*.pyc needle src | wc -l",
    "grep -rl --include=a/b needle src | wc -l",
    "grep -rl --exclude-dir=src needle src | wc -l",
    "grep -rlF -E needle src | wc -l",
    "grep -r needle src",
    "grep needle src/top.py src/data.txt",
    "grep needle",
    "grep -rlE 'a{2' src | wc -l",
    "find . -type f | wc -l",
    "find src -maxdepth 1 | wc -l",
    "find src -name *.py | wc -l",
    "find src -type l | wc -l",
    "find nowhere | wc -l",
    "find src/ | wc -l; true",
    "ls -la src | wc -l",
    "ls nowhere | wc -l",
    "ls src docs | wc -l",
    "wc -l src/top.py docs/notes.md",
    "wc -l nowhere",
    "wc -c src/top.py",
    "wc -l",
    "cat src/top.py | wc -l",
    "echo hi | wc -l",
    "python -c 'print(1)'",
    "",
    "   ",
]


@pytest.mark.parametrize("command", _DECLINED)
def test_every_declined_construct_returns_none(tree, command):
    assert evaluate(command, tree, None) is None


def test_character_dependent_regex_declines_only_when_a_file_holds_non_ascii(tree):
    assert evaluate("grep -rl 'ne.dle' src | wc -l", tree, None) is None
    assert evaluate("grep -rl 'ne.dle' src/pkg | wc -l", tree, None) == (0, "1\n", "")
    assert evaluate("grep -c 'caf.' odd/latin1.txt", tree, None) is None
    assert evaluate("grep -rlw needle src | wc -l", tree, None) is None
    assert evaluate("grep -rlw needle src/pkg | wc -l", tree, None) == (0, "1\n", "")


def test_absent_operand_is_answered_only_under_a_quiet_stderr(tree):
    assert evaluate("grep -rl needle src nowhere 2>/dev/null | wc -l", tree, None) == (0, "3\n", "")
    assert evaluate("find nowhere 2>/dev/null | wc -l", tree, None) == (0, "0\n", "")
    assert evaluate("ls nowhere 2>/dev/null | wc -l", tree, None) == (0, "0\n", "")


def test_nul_binary_counts_under_l_and_c_but_not_under_a_printing_mode(tree):
    assert evaluate("grep -rl needle src/__pycache__ | wc -l", tree, None) == (0, "1\n", "")
    assert evaluate("grep -rc needle src/__pycache__ | wc -l", tree, None) == (0, "1\n", "")
    assert evaluate("grep -rn needle src/__pycache__ | wc -l", tree, None) is None
    assert evaluate("grep -rn needle src/pkg | wc -l", tree, None) == (0, "2\n", "")


def test_symlink_under_a_recursive_walk_is_skipped(tree):
    if not (tree.root / "src" / "link").is_symlink():
        pytest.skip("no symlink support")
    assert evaluate("grep -rl needle src | wc -l", tree, None) == (0, "3\n", "")


def test_git_directory_is_never_walked_by_grep_but_declined_when_an_operand_holds_one(tree):
    _put(tree.root, "lib/.git/HEAD", "needle\n")
    assert evaluate("grep -rl needle lib | wc -l", tree, None) is None


def test_quiet_redirect_must_be_the_exact_form(tree):
    assert evaluate("grep -rl needle src 2>/dev/null | wc -l", tree, None) is not None
    assert evaluate("grep -rl needle src 2> /dev/null | wc -l", tree, None) is None
    assert evaluate("grep -rl '2>/dev/null' src | wc -l", tree, None) is None


def test_warm_second_evaluate_reads_zero_bodies_and_an_edit_rereads_one_file(tree):
    store = CensusStore()
    command = "grep -rl needle lib src/pkg docs | wc -l"
    first = evaluate(command, tree, store)
    assert first == (0, "6\n", "")
    assert len(tree.reads) == 7
    tree.reads.clear()
    assert evaluate(command, tree, store) == first
    assert tree.reads == []
    _put(tree.root, "lib/keep.py", "changed\n")
    os.utime(tree.root / "lib/keep.py", ns=(_OLD_NS + 10**9, _OLD_NS + 10**9))
    assert evaluate(command, tree, store) == (0, "5\n", "")
    assert tree.reads == ["lib/keep.py"]


def test_a_second_pattern_reads_each_file_once_and_shares_the_stat_record(tree):
    store = CensusStore()
    evaluate("grep -rl needle src/pkg | wc -l", tree, store)
    tree.reads.clear()
    evaluate("grep -rl pass src/pkg | wc -l", tree, store)
    assert sorted(tree.reads) == ["src/pkg/mod.py", "src/pkg/util.py"]
    assert len(store._matches) == 2
    assert all(len(rec[4]) == 2 for rec in store._matches.values())


def test_racy_file_is_reread_every_time(tree):
    path = tree.root / "lib" / "keep.py"
    future = 4_000_000_000 * 10**9
    os.utime(path, ns=(future, future))
    store = CensusStore()
    command = "grep -c needle lib/keep.py"
    assert evaluate(command, tree, store) == (0, "1\n", "")
    tree.reads.clear()
    evaluate(command, tree, store)
    assert tree.reads == ["lib/keep.py"]


def test_results_do_not_leak_between_patterns_flags_or_dialects(tree):
    store = CensusStore()
    assert evaluate("grep -c needle src/top.py", tree, store) == (0, "2\n", "")
    assert evaluate("grep -ci needle src/top.py", tree, store) == (0, "3\n", "")
    assert evaluate("grep -c -v needle src/top.py", tree, store) == (0, "2\n", "")
    assert evaluate("grep -cw need src/top.py", tree, store) == (1, "0\n", "")
    assert evaluate("grep -c 'ne\\{2\\}dle' src/top.py", tree, store) == (0, "2\n", "")
    assert evaluate("grep -cE 'ne{2}dle' src/top.py", tree, store) == (0, "2\n", "")
    assert evaluate("grep -cF 'ne{2}dle' src/top.py", tree, store) == (1, "0\n", "")
    assert evaluate("grep -c 'ne{2}dle' src/top.py", tree, store) == (1, "0\n", "")


def test_evaluate_never_writes_the_tree(tree):
    before = sorted((p.relative_to(tree.root).as_posix(), p.stat().st_mtime_ns)
                    for p in tree.root.rglob("*") if p.is_file() and not p.is_symlink())
    for command in _docstring_examples() + _REAL_SHAPES:
        evaluate(command, tree, CensusStore())
    after = sorted((p.relative_to(tree.root).as_posix(), p.stat().st_mtime_ns)
                   for p in tree.root.rglob("*") if p.is_file() and not p.is_symlink())
    assert before == after


def test_evaluate_never_spawns(tree, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(os, "system", boom)
    for command in _docstring_examples() + _REAL_SHAPES:
        assert evaluate(command, tree, None) is not None


def test_evaluate_swallows_a_file_source_that_raises(tree):
    class _Broken(_Tree):
        def walk(self, rel):
            raise RuntimeError("boom")

    assert evaluate("grep -rl needle src | wc -l", _Broken(tree.root), None) is None


def test_translator_closed_subset():
    translate = census_inproc._translate
    assert translate("a.c", "B").src == "a.c"
    assert translate("a\\|b", "B").alternated
    assert translate("a|b", "B").src == "a\\|b"
    assert translate("a|b", "E").alternated
    assert translate("a+", "B").src == "a\\+"
    assert translate("[[:alpha:]_]x", "B").src == "[a-zA-Z_]x"
    assert translate("[^a]", "B").sensitive
    assert translate("a.b", "F").src == "a\\.b"
    assert translate("\\(ab\\)*", "B").quantified
    assert translate("(ab)*c", "E").src == "(?:ab)*c"
    for bad in ("\\1", "\\<a", "*a", "a\\", "[abc", "[]", "a\\{2", "\\("):
        assert translate(bad, "B") is None, bad
    assert translate("(a*)*", "E") is None
