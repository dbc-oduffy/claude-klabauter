"""review_tripwires: literal-name scan of test modules."""

from __future__ import annotations

import hashlib
import os
import struct
from pathlib import Path

import pytest

from coordinator_core.git.index_write import _build_entry
from coordinator_core.ops import review_tripwires as rt
from coordinator_core.ops.review_tripwires import literal_namers, names_in

pytestmark = [pytest.mark.cadence]

EDITED = "ops/coverage-oracles/run_pass.py"


def blob_sha(data: bytes) -> str:
    """The git blob id of `data`: the facts store keys on it, so a fixture index needs the true one."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def stage(repo: Path, rels) -> None:
    """Write a v2 `.git/index` tracking `rels`, in process (no git spawn)."""
    entries = b""
    past = os.stat(repo).st_mtime_ns // 1_000_000_000 - 60
    for rel in rels:
        os.utime(repo / rel, (past, past))
    for rel in sorted(rels):
        entries += _build_entry(rel.encode(), 0o100644, blob_sha((repo / rel).read_bytes()), os.stat(repo / rel))
    raw = b"DIRC" + struct.pack(">II", 2, len(rels)) + entries + b"\x00" * 20
    (repo / ".git").mkdir(exist_ok=True)
    (repo / ".git" / "index").write_bytes(raw)


def _repo(tmp_path: Path, files: dict, *, track: bool = True) -> Path:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    if track:
        stage(tmp_path, list(files))
    return tmp_path


def _selected(tmp_path, literal, edited=EDITED, **kw) -> list[str]:
    repo = _repo(tmp_path, {"tests/test_t.py": f"X = [{literal}]\n"})
    return list(literal_namers(repo, [edited], **kw))


def test_bare_basename_literal_selected(tmp_path: Path) -> None:
    assert _selected(tmp_path, '"run_pass.py"') == ["tests/test_t.py"]


def test_component_suffix_and_full_path_selected(tmp_path: Path) -> None:
    assert _selected(tmp_path, '"coverage-oracles/run_pass.py"') == ["tests/test_t.py"]


def test_full_path_selected(tmp_path: Path) -> None:
    assert _selected(tmp_path, f'"{EDITED}"') == ["tests/test_t.py"]


def test_longer_basename_not_selected(tmp_path: Path) -> None:
    assert _selected(tmp_path, '"test_run_pass.py"') == []


def test_other_directory_not_selected(tmp_path: Path) -> None:
    assert _selected(tmp_path, '"other/run_pass.py"') == []


def test_backslash_literal_selected(tmp_path: Path) -> None:
    assert _selected(tmp_path, '"ops\\\\coverage-oracles\\\\run_pass.py"') == [
        "tests/test_t.py"
    ]


def test_bare_generic_basename_not_selected(tmp_path: Path) -> None:
    assert _selected(tmp_path, '"__init__.py"', edited="x/indexer_arm/__init__.py") == []


def test_qualified_generic_basename_selected(tmp_path: Path) -> None:
    assert _selected(
        tmp_path, '"indexer_arm/__init__.py"', edited="x/indexer_arm/__init__.py"
    ) == ["tests/test_t.py"]


def test_non_py_edited_path_selected(tmp_path: Path) -> None:
    assert _selected(tmp_path, '"data.json"', edited="fixtures/data.json") == [
        "tests/test_t.py"
    ]


def test_exclude_honoured(tmp_path: Path) -> None:
    assert _selected(tmp_path, '"run_pass.py"', exclude={"tests/test_t.py"}) == []


def test_non_test_module_not_scanned(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"lib/helper.py": 'X = "run_pass.py"\n'})
    assert literal_namers(repo, [EDITED]) == {}


def test_suffix_named_test_module_scanned(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"t/run_test.py": 'X = "run_pass.py"\n'})
    assert list(literal_namers(repo, [EDITED])) == ["t/run_test.py"]


def test_empty_named_returns_empty(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"tests/test_t.py": '"run_pass.py"\n'})
    assert literal_namers(repo, []) == {}


def test_maps_to_sorted_named_paths(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"tests/test_t.py": '["b.py", "a.py"]\n'})
    assert literal_namers(repo, ["z/b.py", "z/a.py", "z/c.py"]) == {
        "tests/test_t.py": ["z/a.py", "z/b.py"]
    }


def _names_reference(data: bytes, named) -> list:
    """The unprobed definition: every occurrence of every basename, judged by its literal."""
    from coordinator_core.ops import review_tripwires as rt

    hits = set()
    for base, paths in rt._by_basename(named).items():
        at = data.find(base)
        while at >= 0:
            raw = rt._literal_around(data, at, len(base))
            if raw is not None:
                lit = rt._norm(raw)
                if lit and not ("/" not in lit and lit in rt._GENERIC_BASENAMES):
                    hits.update(o for o, p in paths if p == lit or p.endswith("/" + lit))
            at = data.find(base, at + 1)
    return sorted(hits)


_PROBE_BODIES = [
    b"x = 'run_pass.py'",
    b'x = "run_pass.py"',
    b"x = 'run_pass.py\"",
    b"x = \"a'run_pass.py\"",
    b"x = 'xrun_pass.py'",
    b"x = 'q/run_pass.py' + \"z\\\\run_pass.py\"",
    b"x = './run_pass.py'",
    b"x = 'ops/coverage-oracles/run_pass.py'\ny = 'oracles/run_pass.py'",
    b"run_pass.py'",
    b"'run_pass.py",
    b"x = 'a_b.py' 'b.py'",
    b"x = 'a_b.py'\n'b.py'\n\"__init__.py\" 'z/__init__.py'",
    b"x = '" + b"d/" * 200 + b"run_pass.py'",
]


@pytest.mark.parametrize("named", [
    [EDITED, "q/other.py", "z/b.py", "m/a_b.py", "x/__init__.py", "y/z/__init__.py"],
    [EDITED, "fixtures/data.json"],
    ["ops/run_pass.py", "run_pass.py"],
])
@pytest.mark.parametrize("data", _PROBE_BODIES)
def test_probed_scan_equals_every_occurrence_definition(data, named) -> None:
    assert names_in(data, named) == _names_reference(data, named)


def test_names_in_single_file() -> None:
    assert names_in(b"x = 'run_pass.py'", [EDITED, "q/other.py"]) == [EDITED]
    assert names_in(b"x = 1", [EDITED]) == []


def test_testpaths_bound_the_walk_tripwire(tmp_path: Path) -> None:
    body = 'X = "run_pass.py"\n'
    repo = _repo(
        tmp_path,
        {
            "pyproject.toml": (
                '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
                'norecursedirs = ["_archived"]\n'
            ),
            "tests/test_a.py": body,
            "other/test_b.py": body,
            "tests/_archived/test_c.py": body,
            "tests/.hidden/test_d.py": body,
        },
    )
    assert list(literal_namers(repo, [EDITED])) == ["tests/test_a.py"]


def test_stray_apostrophe_does_not_hide_literal_tripwire(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {"tests/test_t.py": "# don't\nA = 1\nFILES = [\"run_pass.py\"]\n"},
    )
    assert list(literal_namers(repo, [EDITED])) == ["tests/test_t.py"]


def test_scan_writes_only_the_facts_store_and_reads_no_index_tripwire(tmp_path: Path) -> None:
    from coordinator_core.ops import review_tripwires as rt

    for attr in ("git_index", "machinery_paths", "json", "hashlib"):
        assert not hasattr(rt, attr)
    repo = _repo(tmp_path, {"tests/test_t.py": 'X = "run_pass.py"\n'})
    before = sorted(str(p) for p in repo.rglob("*"))
    assert list(literal_namers(repo, [EDITED])) == ["tests/test_t.py"]
    written = sorted(set(str(p) for p in repo.rglob("*")) - set(before))
    assert written == [str(repo / ".git" / rt._FACTS_FILE)]


def _opened_tests(monkeypatch: pytest.MonkeyPatch) -> list:
    from coordinator_core.ops import review_tripwires as rt

    opened: list = []
    real_open = os.open

    def counting_open(path, *a, **kw) -> int:
        if os.path.basename(str(path)).startswith("test_"):
            opened.append(os.path.basename(str(path)))
        return real_open(path, *a, **kw)

    monkeypatch.setattr(rt.os, "open", counting_open)
    return opened


def _many(n: int = 30) -> dict:
    files = {f"tests/test_other_{i:02d}.py": f"X = {i}\n" for i in range(n)}
    files["tests/test_named.py"] = 'NAME = "run_pass.py"\n'
    return files


def test_warm_namers_call_reads_only_the_naming_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path, _many())
    assert list(literal_namers(repo, [EDITED])) == ["tests/test_named.py"]
    opened = _opened_tests(monkeypatch)
    assert list(literal_namers(repo, [EDITED])) == ["tests/test_named.py"]
    assert opened == ["test_named.py"]


def test_a_module_edited_after_staging_is_scanned_not_trusted_to_the_store(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _many(3))
    assert list(literal_namers(repo, [EDITED])) == ["tests/test_named.py"]
    (repo / "tests" / "test_other_01.py").write_text('X = "run_pass.py"\n' * 3, encoding="utf-8")
    (repo / "tests" / "test_named.py").write_text("X = 1\n", encoding="utf-8")
    assert list(literal_namers(repo, [EDITED])) == ["tests/test_other_01.py"]


def test_an_unreadable_store_is_a_cold_scan_not_an_error(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _many(3))
    assert list(literal_namers(repo, [EDITED])) == ["tests/test_named.py"]
    (repo / ".git" / rt._FACTS_FILE).write_bytes(b"not marshal")
    assert list(literal_namers(repo, [EDITED])) == ["tests/test_named.py"]


def test_a_basename_the_tail_prefilter_cannot_cover_is_still_found(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"tests/test_t.py": 'X = "Makefile"\n', "tests/test_u.py": "X = 1\n"})
    assert list(literal_namers(repo, ["build/Makefile"])) == ["tests/test_t.py"]
    assert list(literal_namers(repo, ["build/Makefile"])) == ["tests/test_t.py"]


def test_the_fast_index_reader_admits_the_shortest_test_names(tmp_path: Path) -> None:
    body = "X = 'run_pass.py'"
    repo = _repo(tmp_path, {"test_.py": body, "_test.py": body})
    assert sorted(literal_namers(repo, [EDITED])) == ["_test.py", "test_.py"]
