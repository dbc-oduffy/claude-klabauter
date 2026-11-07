"""Tests for the reverify scope selector and the pre_existing split."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.reverify_scope import _SUITE_BOUND, select_tests, split_pre_existing


pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root, capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["pkg"]\n')
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/alpha.py", "X = 1\n")
    _write(tmp_path, "pkg/beta.py", "Y = 1\n")
    _write(tmp_path, "pkg/tests/__init__.py", "")
    _write(tmp_path, "pkg/tests/test_alpha.py", "def test_a():\n    pass\n")
    _write(tmp_path, "pkg/tests/test_uses_beta.py", "from pkg import beta\n\ndef test_b():\n    assert beta.Y\n")
    _write(tmp_path, "pkg/tests/test_other.py", "def test_o():\n    pass\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _commit(root: Path) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "change")


def test_only_test_file_changed_selects_that_file_alone(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "pkg/tests/test_other.py", "def test_o():\n    assert True\n")
    _commit(repo)
    out = select_tests(repo, base)
    assert out["tests"] == ["pkg/tests/test_other.py"]
    assert out["changed"] == ["pkg/tests/test_other.py"]
    assert out["fully_mapped"] is True


def test_changed_source_selects_mapped_test_not_unrelated(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "pkg/alpha.py", "X = 2\n")
    _commit(repo)
    out = select_tests(repo, base)
    assert out["tests"] == ["pkg/tests/test_alpha.py"]
    assert out["fully_mapped"] is True


def test_unmapped_source_selects_importing_test_via_closure(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "pkg/beta.py", "Y = 2\n")
    _commit(repo)
    out = select_tests(repo, base)
    assert out["tests"] == ["pkg/tests/test_uses_beta.py"]
    assert out["fully_mapped"] is True


def test_a_non_test_importers_own_tests_are_not_selected(repo: Path) -> None:
    _write(repo, "pkg/gamma.py", "def load(root, path):\n    return path\n")
    _write(repo, "pkg/delta.py", "from pkg import gamma\n\ndef prune(p):\n    return gamma.load(p)\n")
    _write(repo, "pkg/tests/test_delta.py", "def test_d():\n    pass\n")
    _write(repo, "pkg/tests/test_gamma.py", "def test_g():\n    pass\n")
    _commit(repo)
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "pkg/gamma.py", "def load(root, path):\n    return root, path\n")
    _commit(repo)
    out = select_tests(repo, base)
    assert out["tests"] == ["pkg/tests/test_gamma.py"]
    assert out["fully_mapped"] is True
    assert out["notice"] == ""


def _wide_graph(repo: Path, importers: int, owners: int) -> str:
    """A hub module imported by ``importers`` modules each with a mapped test, plus ``owners`` tests importing it."""
    _write(repo, "pkg/hub.py", "V = 1\n")
    _write(repo, "pkg/tests/test_hub.py", "def test_h():\n    pass\n")
    for i in range(importers):
        _write(repo, f"pkg/user_{i:03d}.py", "from pkg import hub\n")
        _write(repo, f"pkg/tests/test_user_{i:03d}.py", "def test_u():\n    pass\n")
    for i in range(owners):
        _write(repo, f"pkg/tests/test_owner_{i:03d}.py", "from pkg import hub\n\ndef test_o():\n    assert hub.V\n")
    _commit(repo)
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "pkg/hub.py", "V = 2\n")
    _commit(repo)
    return base


def test_a_wide_import_graph_selects_only_the_hubs_direct_test_owners(repo: Path) -> None:
    base = _wide_graph(repo, importers=80, owners=3)
    out = select_tests(repo, base)
    assert out["tests"] == [
        "pkg/tests/test_hub.py",
        "pkg/tests/test_owner_000.py",
        "pkg/tests/test_owner_001.py",
        "pkg/tests/test_owner_002.py",
    ]
    assert out["notice"] == ""


def test_a_suite_sized_owner_set_narrows_with_a_notice(repo: Path) -> None:
    base = _wide_graph(repo, importers=5, owners=_SUITE_BOUND + 20)
    out = select_tests(repo, base)
    assert len(out["tests"]) == _SUITE_BOUND
    assert "pkg/tests/test_hub.py" in out["tests"]
    assert "narrowed" in out["notice"] and f"{_SUITE_BOUND}-owner-file bound" in out["notice"]
    assert "dropped 21 owner tests" in out["notice"]
    assert out["fully_mapped"] is True


def test_touched_tests_are_never_dropped_by_the_bound(repo: Path) -> None:
    n = _SUITE_BOUND + 5
    for i in range(n):
        _write(repo, f"pkg/tests/test_t_{i:03d}.py", "def test_t():\n    pass\n")
    _commit(repo)
    base = _git(repo, "rev-parse", "HEAD")
    for i in range(n):
        _write(repo, f"pkg/tests/test_t_{i:03d}.py", "def test_t():\n    assert True\n")
    _commit(repo)
    out = select_tests(repo, base)
    assert len(out["tests"]) == n
    assert out["notice"] == ""


def test_unrelated_module_mentioning_leaf_name_is_not_an_importer(repo: Path) -> None:
    _write(repo, "pkg/gamma.py", "Z = 1\n")
    _write(repo, "pkg/delta.py", "# gamma is only mentioned here\n")
    _write(repo, "pkg/tests/test_delta.py", "def test_d():\n    pass\n")
    _write(repo, "pkg/tests/test_gamma.py", "def test_g():\n    pass\n")
    _commit(repo)
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "pkg/gamma.py", "Z = 2\n")
    _commit(repo)
    assert select_tests(repo, base)["tests"] == ["pkg/tests/test_gamma.py"]


_ORACLE_HEAD = "6011210b7d"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_live_run_writes_commit_selection_is_bounded_in_one_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    from coordinator_core.ops import reverify_scope

    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{_ORACLE_HEAD}^{{commit}}"], cwd=_REPO_ROOT, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if probe.returncode != 0:
        pytest.skip(f"{_ORACLE_HEAD} not in this checkout")
    spawns: list = []
    real = reverify_scope.run_git

    def counting(argv, *a, **kw):
        spawns.append(list(argv))
        return real(argv, *a, **kw)

    monkeypatch.setattr(reverify_scope, "run_git", counting)
    start = time.process_time()
    out = select_tests(_REPO_ROOT, f"{_ORACLE_HEAD}~1", _ORACLE_HEAD)
    elapsed_ms = (time.process_time() - start) * 1000
    touched = [p for p in out["changed"] if p.endswith(".py") and p.rsplit("/", 1)[-1].startswith("test_")]
    assert set(touched) <= set(out["tests"])
    assert len(out["tests"]) <= len(touched) + _SUITE_BOUND
    assert len([s for s in spawns if s[0] == "grep"]) == 1
    assert [s[0] for s in spawns] == ["diff", "ls-files", "grep"]
    assert elapsed_ms < 500, f"select_tests took {elapsed_ms:.0f}ms process time"


def test_source_with_no_covering_test_is_not_fully_mapped(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "pkg/gamma.py", "Z = 1\n")
    _commit(repo)
    out = select_tests(repo, base)
    assert out["tests"] == []
    assert out["fully_mapped"] is False


def test_deleted_file_is_dropped(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "pkg/tests/test_other.py").unlink()
    _commit(repo)
    out = select_tests(repo, base)
    assert out["changed"] == []
    assert out["tests"] == []


def test_unknown_base_raises(repo: Path) -> None:
    with pytest.raises(ValueError):
        select_tests(repo, None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        select_tests(repo, "deadbeef" * 5)


def test_pre_existing_red_is_not_charged() -> None:
    out = split_pre_existing(["a::t1", "b::t2"], ["b::t2", "z::t9"])
    assert out == {"failing": ["a::t1"], "pre_existing": ["b::t2"]}


def test_split_with_no_base_failures_charges_all() -> None:
    assert split_pre_existing(["a::t1"], []) == {"failing": ["a::t1"], "pre_existing": []}
