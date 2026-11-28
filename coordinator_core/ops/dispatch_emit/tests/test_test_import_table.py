"""The resident test-line table: parity with `git grep -E`, staleness key, off-hot-path refresh."""
import ast
import subprocess
import threading
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import dispatch_row_lint, test_import_table as table
from coordinator_core.ops.dispatch_emit.test_caller_widen import (
    GOLDEN_GREP,
    TEST_PATHSPECS,
    grep_pattern,
)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

FILES = {
    "pkg/sub/mod.py": "x = 1\n",
    "pkg/tests/test_a.py": "from pkg.sub.mod import f\nprint('x')\n",
    "pkg/tests/test_b.py": "import pkg.sub.mod\n    import pkg.sub.mod as m\n",
    "pkg/tests/test_c.py": "from pkg.sub import mod\n",
    "pkg/tests/test_d.py": "from pkg.sub.modx import f\nopen('mod.py')\n",
    "pkg/tests/test_e.py": "path = 'pkg/sub/mod.py'\nname = 'xmod.py'\nzmod.py\n-mod.py\n",
    "pkg/tests/test_golden.py": "G = 'out.golden.txt'\nH = 'fixtures/a-b_c.golden.json'\n",
    "pkg/x_test.py": "from pkg.sub.mod import g\r\nimport os\r\n",
    "test_top.py": "import pkg.sub.mod\n",
    "pkg/src.py": "import pkg.sub.mod\n",
    "pkg/tests/notes.txt": "import pkg.sub.mod\n",
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
         "-c", "core.autocrlf=false", *args],
        cwd=root, capture_output=True, text=True, check=True, creationflags=_NO_WINDOW,
    ).stdout


@pytest.fixture(autouse=True)
def _clean_state():
    table._TABLES.clear()
    table._THREADS.clear()
    yield
    for t in list(table._THREADS.values()):
        t.join(5)
    table._TABLES.clear()
    table._THREADS.clear()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q")
    for rel, text in FILES.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(text.encode())
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _patterns() -> dict:
    mod = "pkg/sub/mod.py"
    return {
        "grep_pattern": grep_pattern([mod]),
        "exercising_pattern": dispatch_row_lint.exercising_pattern([mod, "pkg/src.py"]),
        "golden": GOLDEN_GREP,
        "composed": "|".join(
            [grep_pattern([mod]), dispatch_row_lint.exercising_pattern([mod]), GOLDEN_GREP]
        ),
    }


@pytest.mark.parametrize("shape", ["grep_pattern", "exercising_pattern", "golden", "composed"])
def test_grep_equals_git_grep(repo: Path, shape: str) -> None:
    pattern = _patterns()[shape]
    expected = _git(repo, "grep", "-E", "-e", pattern, "--", *TEST_PATHSPECS)
    table.rebuild(repo)
    got = table.grep(pattern, TEST_PATHSPECS, repo)
    assert expected.strip(), "fixture must exercise the pattern"
    assert got == expected.rstrip("\n")


def test_pattern_without_selective_literal_scans_whole_corpus(repo: Path) -> None:
    pattern = "i.p.rt.p"
    assert table._compile(pattern).needles is None
    expected = _git(repo, "grep", "-E", "-e", pattern, "--", *TEST_PATHSPECS)
    table.rebuild(repo)
    assert expected.strip()
    assert table.grep(pattern, TEST_PATHSPECS, repo) == expected.rstrip("\n")


def test_composed_pattern_prefilters_on_literals() -> None:
    needles = table._compile(_patterns()["composed"]).needles
    assert needles and "mod.py" in needles and ".golden." in needles


def test_no_table_or_foreign_pathspec_is_none(repo: Path) -> None:
    assert table.grep("import", TEST_PATHSPECS, repo) is None
    table.rebuild(repo)
    assert table.grep("import", ("*.py",), repo) is None
    assert table.grep("(unclosed", TEST_PATHSPECS, repo) is None
    assert table.grep("zzz-no-such-line", TEST_PATHSPECS, repo) == ""


def test_non_test_and_binary_files_are_not_indexed(repo: Path) -> None:
    (repo / "pkg" / "tests" / "test_bin.py").write_bytes(b"import pkg.sub.mod\0\n")
    _git(repo, "add", "-A")
    table.rebuild(repo)
    out = table.grep(grep_pattern(["pkg/sub/mod.py"]), TEST_PATHSPECS, repo)
    assert "pkg/src.py" not in out and "notes.txt" not in out and "test_bin.py" not in out


def test_rebuild_reuses_unchanged_entries_and_rereads_changed(repo: Path) -> None:
    table.rebuild(repo)
    root = table._norm(repo)
    before = dict(table._TABLES[root].entries)
    (repo / "pkg/tests/test_a.py").write_bytes(b"import pkg.sub.other_thing_longer\n")
    table.rebuild(repo)
    after = table._TABLES[root].entries
    assert after["pkg/tests/test_a.py"] is not before["pkg/tests/test_a.py"]
    assert after["pkg/tests/test_a.py"].text == "import pkg.sub.other_thing_longer"
    assert after["pkg/tests/test_b.py"] is before["pkg/tests/test_b.py"]


def test_lean_index_read_matches_the_full_parse_for_test_paths(repo: Path) -> None:
    root = table._norm(repo)
    full = {p: i for p, i in table._index_identities(root).items() if table._is_test_path(p)}
    assert full
    assert table._test_index_identities(root) == full
    assert "pkg/src.py" not in full


def test_lean_index_read_declines_a_v4_index(repo: Path) -> None:
    _git(repo, "update-index", "--index-version", "4")
    assert table._test_index_identities(table._norm(repo)) is None
    table.rebuild(repo)
    assert "pkg/tests/test_a.py" in table._TABLES[table._norm(repo)].entries


def test_key_moves_on_commit_and_index_change(repo: Path) -> None:
    k0 = table.key(repo)
    assert k0[0] and k0[1]
    (repo / "pkg/tests/test_new.py").write_bytes(b"import pkg.sub.mod\n")
    _git(repo, "add", "-A")
    k1 = table.key(repo)
    assert k1[0] == k0[0] and k1[1] != k0[1]
    _git(repo, "commit", "-q", "-m", "two")
    assert table.key(repo)[0] != k0[0]


def test_schedule_refresh_builds_once_then_follows_key(repo: Path) -> None:
    thread = table.schedule_refresh(repo)
    assert thread is not None and thread.daemon
    thread.join(10)
    assert table.grep("import", TEST_PATHSPECS, repo)
    assert table.schedule_refresh(repo) is None

    (repo / "pkg/tests/test_new.py").write_bytes(b"import pkg.sub.mod\n")
    _git(repo, "add", "-A")
    # stale table still serves until the rebuild lands
    assert "test_new.py" not in table.grep("import", TEST_PATHSPECS, repo)
    thread = table.schedule_refresh(repo)
    assert thread is not None
    thread.join(10)
    assert "pkg/tests/test_new.py:import pkg.sub.mod" in table.grep("import", TEST_PATHSPECS, repo)
    assert table.schedule_refresh(repo) is None


def test_schedule_refresh_starts_at_most_one_thread(repo: Path, monkeypatch) -> None:
    gate = threading.Event()
    monkeypatch.setattr(table, "rebuild", lambda root: gate.wait(10))
    first = table.schedule_refresh(repo)
    assert first is not None
    assert table.schedule_refresh(repo) is None
    gate.set()
    first.join(10)
    assert table._THREADS == {}


def test_failed_rebuild_keeps_prior_table_and_frees_the_slot(repo: Path, monkeypatch) -> None:
    def boom(root):
        raise RuntimeError("x")

    monkeypatch.setattr(table, "rebuild", boom)
    thread = table.schedule_refresh(repo)
    thread.join(10)
    assert table.grep("import", TEST_PATHSPECS, repo) is None
    assert table._THREADS == {}


def test_module_imports_no_subprocess() -> None:
    tree = ast.parse(Path(table.__file__).read_text(encoding="utf-8"))
    names = {
        a.name.split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.Import)
        for a in n.names
    } | {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert "subprocess" not in names
