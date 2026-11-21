"""review_token_index: a stat-keyed bloom index keeps reverify's list scan off the per-file open path."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops import review_token_index as idx
from coordinator_core.ops import review_tripwires as rt
from coordinator_core.ops.reverify_scope import select_for_paths
from coordinator_core.ops.tests.test_review_tripwires import _repo, stage

pytestmark = [pytest.mark.cadence]

EDITED = ["pkg/mod_x.py"]
FILES = {
    "pkg/mod_x.py": "X = 1\n",
    "tests/test_imports.py": "from pkg import mod_x\n",
    "tests/test_names.py": 'NAMED = ["mod_x.py"]\n',
    "tests/test_plain.py": "Y = 0\n",
    "tests/test_gone.py": "from pkg import mod_x\n",
    "tests/test_other.py": "Z = 2\n",
}


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path_factory, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path_factory.mktemp("ttix") / "index.bin"
    monkeypatch.setattr(idx, "_cache_path", lambda _root: cache)
    idx.reset_memory()
    yield cache
    idx.reset_memory()


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list:
    seen: list = []
    real = rt._read

    def counted(path, flags):
        seen.append(Path(path).name)
        return real(path, flags)

    monkeypatch.setattr(rt, "_read", counted)
    return seen


def _select(repo: Path, tokens=None, paths=EDITED) -> dict:
    """Full read when `tokens` is None, else the indexed path exactly as review_reverify drives it."""
    if tokens is None:
        return select_for_paths(repo, paths, rt._tracked_test_bytes(repo, set(paths)))
    imports: dict = {}
    scan = rt._tracked_test_bytes(repo, set(paths), tokens, imports)
    return select_for_paths(repo, paths, scan, imports=imports)


def test_second_call_with_one_changed_file_performs_exactly_one_read(tmp_path: Path, reads: list) -> None:
    repo = _repo(tmp_path, FILES)
    nothing = {b"never_present_word"}
    list(rt._tracked_test_bytes(repo, (), nothing))
    assert len(reads) == 5  # the five test modules, each read once to build
    reads.clear()
    list(rt._tracked_test_bytes(repo, (), nothing))
    assert reads == []
    (repo / "tests/test_plain.py").write_text("Y = 0\nW = 3\n", encoding="utf-8")
    list(rt._tracked_test_bytes(repo, (), nothing))
    assert reads == ["test_plain.py"]


def test_selection_matches_the_full_read_for_clean_edited_and_deleted_modules(tmp_path: Path) -> None:
    repo = _repo(tmp_path, FILES)
    tokens = rt.scan_tokens(EDITED)
    assert _select(repo, tokens) == _select(repo)
    picked = _select(repo, tokens)
    assert set(picked["owned"] + picked["tripwires"]) >= {"tests/test_imports.py", "tests/test_names.py"}

    (repo / "tests/test_other.py").write_text("from pkg import mod_x\n", encoding="utf-8")
    (repo / "tests/test_gone.py").unlink()
    assert _select(repo, tokens) == _select(repo)
    assert "tests/test_other.py" in _select(repo, tokens)["owned"]
    assert "tests/test_gone.py" not in _select(repo, tokens)["owned"]


def test_untracked_module_is_never_scanned(tmp_path: Path) -> None:
    repo = _repo(tmp_path, FILES)
    (repo / "tests/test_untracked.py").write_text("from pkg import mod_x\n", encoding="utf-8")
    tokens = rt.scan_tokens(EDITED)
    assert "tests/test_untracked.py" not in _select(repo, tokens)["owned"]
    assert _select(repo, tokens) == _select(repo)


def test_persisted_index_serves_a_cold_process_without_reads(tmp_path: Path, reads: list) -> None:
    repo = _repo(tmp_path, FILES)
    list(rt._tracked_test_bytes(repo, (), {b"never_present_word"}))
    idx.reset_memory()
    reads.clear()
    list(rt._tracked_test_bytes(repo, (), {b"never_present_word"}))
    assert reads == []


@pytest.mark.parametrize(
    "payload",
    [b"garbage", b"TTIX1\n" + b"\x00" * 64, b"TTIX2\n\xff\xff\xff\xff{}", b""],
    ids=["garbage", "foreign-version", "truncated", "empty"],
)
def test_corrupt_or_foreign_persisted_index_rebuilds(tmp_path: Path, isolated_cache: Path, reads: list, payload: bytes) -> None:
    repo = _repo(tmp_path, FILES)
    tokens = rt.scan_tokens(EDITED)
    expected = _select(repo)
    isolated_cache.parent.mkdir(parents=True, exist_ok=True)
    isolated_cache.write_bytes(payload)
    idx.reset_memory()
    reads.clear()
    assert _select(repo, tokens) == expected
    assert len(reads) >= 5


def test_bloom_admits_every_word_of_a_token_and_rules_out_absent_ones() -> None:
    bloom = idx.bloom_of(b'x = ["run_pass.py"]  # coverage-oracles')
    assert idx.admits(bloom, [idx.token_mask(b"run_pass.py")])
    assert idx.admits(bloom, [idx.token_mask(b"coverage-oracles")])
    assert not idx.admits(bloom, [idx.token_mask(b"absent_word_zzz")])
    assert idx.admits(bloom, [idx.token_mask(b"...")])  # no word: admits everything


def test_tracked_set_is_reparsed_only_when_the_git_index_moves(tmp_path: Path) -> None:
    repo = _repo(tmp_path, FILES)
    calls: list = []

    def parse(root):
        calls.append(root)
        return frozenset(FILES)

    index_file = repo / ".git" / "index"
    idx.tracked_paths(repo, parse, index_file)
    idx.tracked_paths(repo, parse, index_file)
    assert len(calls) == 1
    stage(repo, list(FILES) + [])
    index_file.write_bytes(index_file.read_bytes() + b"\x00")
    idx.tracked_paths(repo, parse, index_file)
    assert len(calls) == 2


@pytest.mark.parametrize("statement", ["from . import y\n", "from .sub_mod import y\n"])
@pytest.mark.parametrize("indexed", [False, True], ids=["full-read", "indexed"])
def test_relative_import_naming_no_leaf_is_selected(tmp_path: Path, statement: str, indexed: bool) -> None:
    files = {
        "pkg/sub/__init__.py": "",
        "pkg/sub/sub_mod.py": "y = 1\n",
        "pkg/sub/test_rel.py": statement,
        "pkg/sub/test_plain.py": "Z = 0\n",
    }
    repo = _repo(tmp_path, files)
    edited = ["pkg/sub/__init__.py"] if statement.startswith("from . import") else ["pkg/sub/sub_mod.py"]
    out = _select(repo, rt.scan_tokens(edited, leaves=False) if indexed else None, edited)
    assert "pkg/sub/test_rel.py" in out["owned"] + out["tripwires"]
    assert "pkg/sub/test_plain.py" not in out["owned"]


def test_steady_state_import_lookup_reads_and_parses_nothing(tmp_path: Path, reads: list, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.ops import reverify_scope

    repo = _repo(tmp_path, FILES)
    tokens = rt.scan_tokens(EDITED, leaves=False)
    first = _select(repo, tokens)
    reads.clear()
    monkeypatch.setattr(reverify_scope, "import_targets", lambda *a: pytest.fail("re-parsed an unchanged module"))
    assert _select(repo, tokens) == first
    assert set(reads) <= {"test_names.py"}  # only the module its basename bloom admits is opened


def test_index_key_carries_ctime_and_inode(tmp_path: Path) -> None:
    repo = _repo(tmp_path, FILES)
    list(rt._tracked_test_bytes(repo, (), {b"x"}))
    key, *_rest = next(iter(idx._MEM[str(repo).replace("\\", "/").rstrip("/")].values()))
    assert len(key) == 4
