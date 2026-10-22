"""Tests for coordinator_core.roadmap.census_inproc; every tree is built under tmp_path."""

from __future__ import annotations

import hashlib
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


def _host_treats_cr_as_eol(sh, env, kw):
    """Git-for-Windows grep strips CR before `$`; POSIX grep (and the matcher) does not."""
    out = subprocess.run([sh, "-c", "printf 'a\r\n' | grep -c 'a$'"], capture_output=True,
                         text=True, env=env, **kw).stdout.strip()
    return out == "1"


def _host_comparable(commands, sh, env, kw):
    if _host_treats_cr_as_eol(sh, env, kw):
        return [c for c in commands if "crlf" not in c]
    return commands


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
    commands = _host_comparable(commands, sh, env, kw)
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


# ---- evaluate_many: the index world, oid-keyed match records -------------------------------

_BLOBS = {
    "src/top.py": "needle one\nplain\nNEEDLE two\nneedle needle\n",
    "src/data.txt": "nothing here\nneed\n",
    "src/pkg/mod.py": "def needle():\n    pass\nneedle\n",
    "src/pkg/util.py": "x = 1\n",
    "src/__pycache__/mod.cpython.pyc": b"\x00\x01needle\x02\xff\n",
    "docs/notes.md": "needle in notes\nsecond\n",
    "docs/crlf.md": "needle one\r\nneedle\r\nplain\r\n",
    "docs/noeol.txt": "alpha\nneedle",
    "odd/latin1.txt": b"caf\xe9 needle\n",
    "lib/vendor/skip.py": "needle\n",
    "lib/keep.py": "needle\n",
}
_SYMLINKS = {"src/link": "../docs/notes.md"}


def _oid(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class _Index:
    """An in-memory IndexFiles: tracked paths only, with a log of every read_blobs call."""

    def __init__(self, blobs=None, links=None) -> None:
        self.paths = {}
        for rel, data in (blobs if blobs is not None else _BLOBS).items():
            self.paths[rel] = (data if isinstance(data, bytes) else data.encode(), "100644")
        for rel, target in (links if links is not None else _SYMLINKS).items():
            self.paths[rel] = (target.encode(), "120000")
        self.calls: list = []

    def files(self, rel):
        return sorted((p, _oid(d), m) for p, (d, m) in self.paths.items()
                      if p == rel or p.startswith(rel + "/"))

    def read_blobs(self, oids):
        self.calls.append(list(oids))
        by_oid = {_oid(d): d for d, _ in self.paths.values()}
        return {o: by_oid[o] for o in oids if o in by_oid}

    def fetched(self) -> list:
        return [o for call in self.calls for o in call]


class _Records:
    """An in-memory MatchStore."""

    def __init__(self) -> None:
        self.rows: dict = {}

    def matched(self, key, oid):
        return self.rows.get((key, oid))

    def put_matched(self, key, counts):
        for oid, v in counts.items():
            self.rows[(key, oid)] = v


def _many(commands, index, store=None, budget=1e9):
    return census_inproc.evaluate_many(list(commands), index, store, budget)


def test_pending_is_not_none_and_is_a_distinct_sentinel():
    assert census_inproc.PENDING is not None
    assert census_inproc.PENDING != (0, "0\n", "")
    assert repr(census_inproc.PENDING) == "PENDING"


def test_pack_round_trips_every_state():
    for res in [(None, -1, 1), (False, 0, 0), (True, 7, 0), (True, -1, 2), (None, -1, 2), (True, 0, 1)]:
        v = census_inproc._pack(res)
        assert census_inproc._valid(v)
        assert census_inproc._unpack(v) == res
    assert not census_inproc._valid(-1)
    assert not census_inproc._valid("3")
    assert not census_inproc._valid(3)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_index_world_parity_with_the_host_shell(tmp_path):
    sh = shutil.which("sh")
    if not sh:
        pytest.skip("no POSIX shell")
    co = tmp_path / "co"
    for rel, data in _BLOBS.items():
        _put(co, rel, data)
    try:
        for rel, target in _SYMLINKS.items():
            os.symlink(target, co / rel)
    except (OSError, NotImplementedError):
        pytest.skip("no symlink support")
    env = {k: v for k, v in os.environ.items() if not k.startswith("BASH_FUNC_")}
    kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
    commands = _docstring_examples() + _REAL_SHAPES
    assert len(commands) >= 20 + 20
    commands = _host_comparable(commands, sh, env, kw)
    index = _Index()
    got = _many(commands, index, _Records())
    for command in commands:
        proc = subprocess.run([sh, "-c", command], cwd=co, capture_output=True, text=True,
                              env=env, **kw)
        assert got[command] is not None, command
        assert got[command] is not census_inproc.PENDING, command
        assert (got[command][0], got[command][1].strip()) == (proc.returncode,
                                                              proc.stdout.strip()), command
        assert got[command][2] == "", command


def test_grammar_examples_and_real_shapes_match_the_walk_path(tree):
    commands = _docstring_examples() + _REAL_SHAPES
    got = _many(commands, _Index())
    for command in commands:
        assert got[command] == evaluate(command, tree, None), command


@pytest.mark.parametrize("command", _DECLINED)
def test_every_declined_construct_declines_in_the_index_world_too(command):
    assert _many([command], _Index())[command] is None


def test_many_commands_over_one_tree_fetch_each_oid_once():
    index = _Index()
    commands = [
        "grep -rl needle src docs lib | wc -l",
        "grep -rc needle src docs lib | wc -l",
        "grep -rlE 'need(le|les)' src/pkg docs | wc -l",
        "grep -rli NEEDLE lib docs | wc -l",
        "grep -c needle docs/notes.md",
        "wc -l docs/notes.md",
        "grep -n needle docs/notes.md",
    ]
    got = _many(commands, index, _Records())
    assert all(isinstance(got[c], tuple) for c in commands)
    fetched = index.fetched()
    assert len(fetched) == len(set(fetched))
    assert len(index.calls) == 1


def test_a_warm_second_call_fetches_only_what_has_no_record():
    index = _Index()
    store = _Records()
    command = "grep -rl needle src/pkg lib | wc -l"
    first = _many([command], index, store)
    assert first[command] == (0, "3\n", "")
    index.calls.clear()
    assert _many([command], index, store) == first
    assert index.calls == []
    assert _many(["grep -rl pass src/pkg | wc -l"], index, store)[
        "grep -rl pass src/pkg | wc -l"] == (0, "1\n", "")
    assert len(index.fetched()) == 2


def test_restaging_k_files_fetches_exactly_k_oids():
    index = _Index()
    store = _Records()
    command = "grep -rl needle lib src/pkg docs | wc -l"
    assert _many([command], index, store)[command] == (0, "6\n", "")
    index.calls.clear()
    index.paths["lib/keep.py"] = (b"changed\n", "100644")
    index.paths["docs/notes.md"] = (b"also changed\n", "100644")
    assert _many([command], index, store)[command] == (0, "4\n", "")
    assert len(index.fetched()) == 2


def test_a_small_budget_yields_pending_and_a_second_call_finishes_without_refetching(monkeypatch):
    monkeypatch.setattr(census_inproc, "_SLICE", 2)
    index = _Index()
    store = _Records()
    command = "grep -rl needle src docs lib | wc -l"
    first = _many([command], index, store, budget=0)
    assert first[command] is census_inproc.PENDING
    assert len(index.calls) == 1 and len(index.calls[0]) == 2
    recorded = {oid for _, oid in store.rows}
    second = _many([command], index, store, budget=1e9)
    assert second[command] == (0, "8\n", "")
    refetched = [o for call in index.calls[1:] for o in call]
    assert not recorded & set(refetched)
    assert len(refetched) == len(set(refetched))


def test_progress_advances_on_every_call_even_with_zero_budget(monkeypatch):
    monkeypatch.setattr(census_inproc, "_SLICE", 1)
    index = _Index()
    store = _Records()
    command = "grep -rl needle lib docs | wc -l"
    seen = 0
    for _ in range(10):
        got = _many([command], index, store, budget=0)[command]
        assert len(store.rows) > seen or got is not census_inproc.PENDING
        seen = len(store.rows)
        if got is not census_inproc.PENDING:
            assert got == (0, "5\n", "")
            break
    else:
        pytest.fail("never finished")


def test_a_finished_command_is_answered_while_another_stays_pending(monkeypatch):
    monkeypatch.setattr(census_inproc, "_SLICE", 1)
    index = _Index()
    got = _many(["grep -c needle lib/keep.py", "grep -rl needle src docs | wc -l"],
                index, _Records(), budget=0)
    assert got["grep -c needle lib/keep.py"] == (0, "1\n", "")
    assert got["grep -rl needle src docs | wc -l"] is census_inproc.PENDING


def test_an_untracked_file_is_never_read_or_counted(tmp_path):
    index = _Index()
    _put(tmp_path, "src/untracked.py", "needle\n")
    got = _many(["grep -rl needle src/pkg | wc -l", "grep -c needle src/untracked.py",
                 "grep -rl needle src/untracked.py | wc -l"], index)
    assert got["grep -rl needle src/pkg | wc -l"] == (0, "1\n", "")
    assert got["grep -c needle src/untracked.py"] is None
    assert got["grep -rl needle src/untracked.py | wc -l"] is None
    assert _oid(b"needle\n") not in index.fetched() or "lib/keep.py" in index.paths
    assert [r for r, (d, _) in index.paths.items() if r.endswith("untracked.py")] == []


def test_two_paths_sharing_a_blob_count_twice():
    blobs = dict(_BLOBS)
    blobs["lib/copy.py"] = "needle\n"
    got = _many(["grep -rl needle lib | wc -l", "grep -rc needle lib | wc -l"],
                _Index(blobs, {}))
    assert got["grep -rl needle lib | wc -l"] == (0, "3\n", "")
    assert got["grep -rc needle lib | wc -l"] == (0, "3\n", "")


def test_wc_l_is_recorded_under_the_reserved_key_from_the_newline_count():
    index = _Index()
    store = _Records()
    got = _many(["wc -l docs/noeol.txt", "wc -l docs/crlf.md"], index, store)
    assert got["wc -l docs/noeol.txt"] == (0, "1 docs/noeol.txt\n", "")
    assert got["wc -l docs/crlf.md"] == (0, "3 docs/crlf.md\n", "")
    assert {k for k, _ in store.rows} == {"wc-l"}


def test_a_symlink_entry_is_skipped_under_r_and_declined_as_an_operand():
    index = _Index()
    got = _many(["grep -rl needle src | wc -l", "grep -rl needle src/link | wc -l",
                 "wc -l src/link"], index)
    assert got["grep -rl needle src/link | wc -l"] is None
    assert got["wc -l src/link"] is None
    assert got["grep -rl needle src | wc -l"] == (0, "3\n", "")
    assert _oid(b"../docs/notes.md") not in index.fetched()


def test_a_blob_the_index_cannot_supply_declines_the_command():
    class _Missing(_Index):
        def read_blobs(self, oids):
            out = super().read_blobs(oids)
            out.pop(_oid(b"needle\n"), None)
            return out

    got = _many(["grep -rl needle lib | wc -l", "grep -rl pass src/pkg | wc -l"], _Missing())
    assert got["grep -rl needle lib | wc -l"] is None
    assert got["grep -rl pass src/pkg | wc -l"] == (0, "1\n", "")


def test_a_corrupt_record_is_refetched_not_trusted():
    index = _Index()
    store = _Records()
    command = "grep -rl pass src/pkg | wc -l"
    assert _many([command], index, store)[command] == (0, "1\n", "")
    for k in list(store.rows):
        store.rows[k] = -5
    index.calls.clear()
    assert _many([command], index, store)[command] == (0, "1\n", "")
    assert len(index.fetched()) == 2


def test_evaluate_many_runs_without_a_store_and_never_spawns(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(os, "system", boom)
    commands = _docstring_examples() + _REAL_SHAPES
    got = _many(commands, _Index(), None)
    assert all(got[c] is not None for c in commands)


def test_duplicate_commands_collapse_and_order_is_kept():
    got = _many(["ls docs | wc -l", "ls src/pkg | wc -l", "ls docs | wc -l"], _Index())
    assert list(got) == ["ls docs | wc -l", "ls src/pkg | wc -l"]
    assert got["ls docs | wc -l"] == (0, "3\n", "")


def test_find_and_ls_filter_stages_answer_from_the_index():
    got = _many(["find src -name '*.py' | grep -v pkg | wc -l",
                 "ls src | grep -v data | wc -l"], _Index())
    assert got["find src -name '*.py' | grep -v pkg | wc -l"] == (0, "1\n", "")
    assert got["ls src | grep -v data | wc -l"] == (0, "4\n", "")
