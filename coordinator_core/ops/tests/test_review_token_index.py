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


def test_eviction_import_index_reloads_and_only_reads_changed_files(tmp_path):
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("from pkg import mod_x\n", encoding="utf-8")
    second.write_text("import peer\n", encoding="utf-8")
    reads = []

    def read(path):
        reads.append(Path(path).name)
        return Path(path).read_bytes()

    rels = ["first.py", "second.py"]
    expected = idx.refresh_import_targets(str(tmp_path), rels, read)
    assert "pkg/mod_x.py" in expected["first.py"]
    assert "peer.py" in expected["second.py"]
    idx.reset_memory()
    reads.clear()
    assert idx.refresh_import_targets(str(tmp_path), rels, read) == expected
    assert reads == []

    second.write_text("import replacement\n", encoding="utf-8")
    assert "replacement.py" in idx.refresh_import_targets(str(tmp_path), rels, read)["second.py"]
    assert reads == ["second.py"]


def test_second_call_with_one_changed_file_performs_exactly_one_read(tmp_path: Path, reads: list) -> None:
    repo = _repo(tmp_path, FILES)
    nothing = {b"never_present.py"}
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
    list(rt._tracked_test_bytes(repo, (), {b"never_present.py"}))
    idx.reset_memory()
    reads.clear()
    list(rt._tracked_test_bytes(repo, (), {b"never_present.py"}))
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


def test_bloom_admits_a_quoted_component_and_rules_out_absent_ones() -> None:
    bloom = idx.bloom_of(b'x = ["run_pass.py", "a/coverage-oracles.md"]  # nothing.else')
    assert idx.admits(bloom, [idx.token_mask(b"run_pass.py")])
    assert idx.admits(bloom, [idx.token_mask(b"coverage-oracles.md")])
    assert not idx.admits(bloom, [idx.token_mask(b"absent_word.zzz")])
    assert not idx.admits(bloom, [idx.token_mask(b"nothing.else")])  # a comment word is no literal component
    assert idx.admits(bloom, [idx.token_mask(b"a b.py")])  # not a component: admits everything
    assert idx.token_mask(b"Makefile") == 0  # no dot: indexed for walkers only


def test_only_a_walker_indexes_dotless_components() -> None:
    plain = idx.bloom_of(b'P = "pkg/sub_dir"')
    walker = idx.bloom_of(b'for x in Path("pkg/sub_dir").rglob("*"): pass')
    mask = idx.token_mask(b"sub_dir", walk=True)
    assert not idx.admits(plain, [mask])
    assert idx.admits(walker, [mask])


def test_a_long_unclosed_run_scans_in_linear_time() -> None:
    import time

    start = time.perf_counter()
    idx.bloom_of(b"a" * 400_000 + b" " + b"b." * 100_000)
    assert time.perf_counter() - start < 2.0


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


def test_filtered_tracked_set_is_persisted_so_a_cold_process_skips_the_index_parse(tmp_path: Path) -> None:
    repo = _repo(tmp_path, FILES)
    calls: list = []

    def parse(root):
        calls.append(root)
        return frozenset(FILES)

    index_file = repo / ".git" / "index"
    keep = lambda rel: rel.startswith("tests/")  # noqa: E731
    first = idx.tracked_paths(repo, parse, index_file, keep)
    idx.reset_memory()
    assert idx.tracked_paths(repo, parse, index_file, keep) == first
    assert len(calls) == 1
    assert all(r.startswith("tests/") for r in first)


def test_a_second_builder_waits_for_the_first_and_then_reloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    import time

    held = idx._BuildLock("k")
    assert held.acquire() and held.held
    other = idx._BuildLock("k")
    threading.Timer(0.2, held.release).start()
    start = time.monotonic()
    assert other.acquire() is False  # waited out the first build; it must diff against that build's file
    assert time.monotonic() - start >= 0.15
    assert other.acquire() is True
    other.release()


def test_warm_index_builds_so_the_op_scan_reads_only_admitted_modules(tmp_path: Path, reads: list) -> None:
    repo = _repo(tmp_path, FILES)
    rt.warm_index(repo)
    idx.reset_memory()
    reads.clear()
    _select(repo, rt.scan_tokens(EDITED, leaves=False))
    assert set(reads) <= {"test_names.py"}


def test_a_reverify_scan_of_repo_x_makes_x_the_repo_prewarmed_next_boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = _repo(tmp_path / "other", FILES)
    repo_x = _repo(tmp_path / "x", FILES)
    fallback = _repo(tmp_path / "engine", FILES)
    assert idx.recent_roots() == []
    _select(other, rt.scan_tokens(EDITED, leaves=False))
    _select(repo_x, rt.scan_tokens(EDITED, leaves=False))
    assert idx.recent_roots()[0] == str(repo_x).replace("\\", "/").rstrip("/")
    warmed: list = []
    monkeypatch.setattr(rt, "warm_index", lambda root: warmed.append(Path(root)))
    rt.warm_recent_indexes(fallback)
    assert warmed[-1] == repo_x and other in warmed and fallback not in warmed  # newest warmed last


def test_recent_roots_keep_four_and_fall_back_to_the_engine_clone_when_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(6):
        idx.record_root(str(tmp_path / f"r{i}"))
    assert idx.recent_roots() == [str(tmp_path / f"r{i}") for i in (5, 4, 3, 2)]
    idx.record_root(str(tmp_path / "r4"))
    assert idx.recent_roots()[:2] == [str(tmp_path / "r4"), str(tmp_path / "r5")]
    warmed: list = []
    monkeypatch.setattr(rt, "warm_index", lambda root: warmed.append(Path(root)))
    monkeypatch.setattr(idx, "recent_roots", lambda limit=4: [])
    rt.warm_recent_indexes(tmp_path)
    assert warmed == [tmp_path]
