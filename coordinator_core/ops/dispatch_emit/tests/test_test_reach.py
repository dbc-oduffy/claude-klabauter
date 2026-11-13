"""reach_paths / foreign_dirt over a tmp_path git repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_reach as tr

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root, check=True, capture_output=True, creationflags=_NOWIN,
    )


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "core.autocrlf", "false")
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/mod.py", "from pkg import dep\n")
    _write(tmp_path, "pkg/dep.py", "X = 1\n")
    _write(tmp_path, "pkg/unrelated.py", "Y = 1\n")
    _write(tmp_path, "pkg/tests/conftest.py", "")
    _write(tmp_path, "pkg/tests/helper.py", "H = 1\n")
    _write(tmp_path, "pkg/tests/test_mod.py", "from pkg import mod\nfrom pkg.tests import helper\n")
    _write(tmp_path, "pkg/tests/test_other.py", "")
    _write(tmp_path, "pkg/tests/fixtures/data.json", '{"a": 1}\n')
    _write(tmp_path, "pkg/tests/fixtures/sub/more.json", '{"b": 1}\n')
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


SEED = "pkg/tests/test_mod.py"


def test_reach_covers_closure_fixtures_conftest_not_siblings(repo: Path) -> None:
    reach = tr.reach_paths(repo, [SEED])
    assert {
        SEED, "pkg/mod.py", "pkg/dep.py", "pkg/__init__.py", "pkg/tests/helper.py",
        "pkg/tests/conftest.py", "pkg/tests/fixtures/data.json", "pkg/tests/fixtures/sub/more.json",
    } <= reach
    assert "pkg/tests/test_other.py" not in reach
    assert "pkg/unrelated.py" not in reach


def test_closure_is_depth_bounded(repo: Path) -> None:
    assert "pkg/dep.py" not in tr.reach_paths(repo, [SEED], depth=1)
    assert "pkg/dep.py" in tr.reach_paths(repo, [SEED], depth=2)


def test_seeds_for_landing_adds_sibling_tests(repo: Path) -> None:
    seeds = tr.seeds_for_landing(repo, ["pkg/mod.py"])
    assert seeds == ["pkg/tests/test_mod.py"]
    assert tr.seeds_for_landing(repo, ["pkg/tests/test_other.py"]) == ["pkg/tests/test_other.py"]


def test_dirty_fixture_outside_landing_is_returned_and_clean_is_not(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reach = tr.reach_paths(repo, [SEED])
    assert tr.foreign_dirt(repo, reach, ["pkg/mod.py"]) == {}
    _write(repo, "pkg/tests/fixtures/data.json", '{"a": 2}\n')

    def boom(*a, **k):
        raise AssertionError("spawned a subprocess")

    monkeypatch.setattr(subprocess, "Popen", boom)
    dirt = tr.foreign_dirt(repo, reach, ["pkg/mod.py"])
    assert list(dirt) == ["pkg/tests/fixtures/data.json"]
    assert dirt["pkg/tests/fixtures/data.json"]
    # the same path inside the landing set is the landing's own, not foreign
    assert tr.foreign_dirt(repo, reach, ["pkg/tests/fixtures/data.json"]) == {}


def test_touched_but_identical_fixture_is_clean(repo: Path) -> None:
    reach = tr.reach_paths(repo, [SEED])
    _write(repo, "pkg/tests/fixtures/data.json", '{"a": 1}\n')
    assert tr.foreign_dirt(repo, reach, []) == {}


def test_staged_deleted_and_dirty_py_in_closure(repo: Path) -> None:
    reach = tr.reach_paths(repo, [SEED])
    _write(repo, "pkg/dep.py", "X = 2\n")
    _git(repo, "add", "pkg/dep.py")
    (repo / "pkg/tests/fixtures/sub/more.json").unlink()
    _write(repo, "pkg/tests/helper.py", "H = 2\n")
    dirt = tr.foreign_dirt(repo, reach, [])
    assert set(dirt) == {"pkg/dep.py", "pkg/tests/fixtures/sub/more.json", "pkg/tests/helper.py"}


def test_names_scan_matches_parse_index_identity_on_real_index(repo: Path) -> None:
    from coordinator_core.git.git_index import parse_index_identity

    view = tr._index_view(repo)
    ident = parse_index_identity(repo)
    assert view.starts is not None
    assert set(view.names) == set(ident)
    assert all(view.sha(p) == ident[p].sha for p in ident)


def test_names_scan_matches_parse_index_identity_on_this_repo() -> None:
    from coordinator_core.git.git_index import parse_index_identity

    root = Path(__file__).resolve().parents[4]
    if not (root / ".git").exists():
        pytest.skip("not a checkout")
    view = tr._index_view(root, exact=True)
    ident = parse_index_identity(root)
    assert view.starts is not None
    assert set(view.names) == set(ident)
    sample = sorted(ident)[:: max(len(ident) // 500, 1)]
    assert all(view.sha(p) == ident[p].sha for p in sample)


def test_exact_view_sees_restaged_content_not_a_stale_sha(repo: Path) -> None:
    reach = tr.reach_paths(repo, [SEED])
    assert tr.foreign_dirt(repo, reach, []) == {}
    _write(repo, "pkg/dep.py", "X = 3\n")
    _git(repo, "add", "pkg/dep.py")
    assert set(tr.foreign_dirt(repo, reach, [])) == {"pkg/dep.py"}


def test_unmerged_reach_path_is_refused(repo: Path) -> None:
    from coordinator_core.git.git_index import IndexParseError

    _git(repo, "checkout", "-q", "-b", "side")
    _write(repo, "pkg/dep.py", "X = 'side'\n")
    _git(repo, "commit", "-qam", "side")
    _git(repo, "checkout", "-q", "-")
    _write(repo, "pkg/dep.py", "X = 'main'\n")
    _git(repo, "commit", "-qam", "main")
    merge = subprocess.run(
        ["git", "merge", "side"], cwd=repo, capture_output=True, creationflags=_NOWIN
    )
    assert merge.returncode != 0
    with pytest.raises(IndexParseError):
        tr.foreign_dirt(repo, ["pkg/dep.py"], [])


def test_scan_imports_reads_every_statement_shape() -> None:
    src = (
        "import os, a.b as c\n"
        "from x.y import (\n    p,  # note\n    q as r,\n)\n"
        "from .rel import z\n"
        "from .. import top\n"
        "def f():\n    from lazy import w\n"
        "x = 1  # import not_a_statement\n"
    )
    recs = {(r.module, r.names, r.level) for r in tr._scan_imports(src)}
    assert {
        ("os", (), 0), ("a.b", (), 0), ("x.y", ("p", "q"), 0), ("rel", ("z",), 1),
        ("", ("top",), 2), ("lazy", ("w",), 0),
    } <= recs
    assert not any(r[0] == "not_a_statement" for r in recs)


def test_untracked_file_is_not_dirt(repo: Path) -> None:
    _write(repo, "pkg/tests/fixtures/new.json", "{}\n")
    reach = tr.reach_paths(repo, [SEED])
    assert "pkg/tests/fixtures/new.json" not in reach
    assert tr.foreign_dirt(repo, ["pkg/tests/fixtures/new.json"], []) == {}
