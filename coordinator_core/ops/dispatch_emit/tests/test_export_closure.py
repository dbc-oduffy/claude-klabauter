"""export_closure.test_closure over a tmp_path git repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import export_closure as ec

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SEED = "pkg/tests/sub/test_mod.py"


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
    _write(tmp_path, "pyproject.toml", "[tool.pytest.ini_options]\n")
    _write(tmp_path, "tox.ini", "")
    _write(tmp_path, "conftest.py", "")
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/mod.py", "from pkg import dep\n")
    _write(tmp_path, "pkg/dep.py", "X = 1\n")
    _write(tmp_path, "pkg/unrelated.py", "Y = 1\n")
    _write(tmp_path, "other/unrelated_too.py", "Z = 1\n")
    _write(tmp_path, "pkg/tests/conftest.py", "")
    _write(tmp_path, "pkg/tests/sub/conftest.py", "")
    _write(tmp_path, "pkg/tests/sub/test_mod.py", "from pkg import mod\n")
    _write(tmp_path, "pkg/tests/sub/test_sibling.py", "")
    _write(tmp_path, "pkg/tests/test_up.py", "")
    _write(tmp_path, "pkg/svc.py", "")
    _write(tmp_path, "pkg/tests/test_svc.py", "")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def test_argv_file_gives_chain_and_excludes_unrelated(repo: Path) -> None:
    c = ec.test_closure(repo, [], ["python", "-m", "pytest", "-q", SEED])
    assert c.paths is not None and c.seeds == (SEED,) and c.note == ""
    assert {
        SEED, "pkg/mod.py", "pkg/dep.py", "pkg/__init__.py",
        "pkg/tests/conftest.py", "pkg/tests/sub/conftest.py", "conftest.py",
        "pyproject.toml", "tox.ini",
    } <= c.paths
    assert not {"pkg/unrelated.py", "other/unrelated_too.py", "pkg/tests/sub/test_sibling.py",
                "pkg/tests/test_up.py"} & c.paths


def test_node_id_and_backslash_token_resolve_to_the_file(repo: Path) -> None:
    c = ec.test_closure(repo, [], ["pytest", "pkg\\tests\\sub\\test_mod.py::test_a[x::y]"])
    assert c.seeds == (SEED,)


def test_declared_test_and_sibling_seed_and_declared_path(repo: Path) -> None:
    c = ec.test_closure(repo, ["pkg/svc.py", "pkg/unrelated.py"], ["pytest"])
    assert c.paths is not None and c.seeds == ("pkg/tests/test_svc.py",)
    assert "pkg/unrelated.py" in c.paths and "pkg/svc.py" in c.paths
    c2 = ec.test_closure(repo, ["pkg/tests/test_up.py"], [])
    assert c2.seeds == ("pkg/tests/test_up.py",) and "pkg/tests/conftest.py" in c2.paths


def test_untracked_root_config_is_not_added(repo: Path) -> None:
    c = ec.test_closure(repo, [], [SEED])
    assert "setup.cfg" not in c.paths and "pytest.ini" not in c.paths


def test_argv_naming_no_test_path_gives_none(repo: Path) -> None:
    for argv in (["python", "-m", "pytest", "-q"], ["pytest", "no/such/test_x.py"]):
        c = ec.test_closure(repo, [], argv)
        assert c.paths is None and c.seeds == () and "full fill" in c.note
    c = ec.test_closure(repo, ["pkg/unrelated.py"], ["pytest"])
    assert c.paths is None


def test_directory_argv_gives_none_even_with_a_declared_test(repo: Path) -> None:
    c = ec.test_closure(repo, [SEED], ["pytest", "pkg/tests"])
    assert c.paths is None and "directory" in c.note


def test_closure_is_frozen(repo: Path) -> None:
    c = ec.test_closure(repo, [], [SEED])
    with pytest.raises(AttributeError):
        c.paths = None  # type: ignore[misc]


def test_conftest_imports_and_sys_path_bare_imports_are_in_the_closure(repo: Path) -> None:
    _write(repo, "conftest.py", "import sys\nsys.path.insert(0, 'tools/lib')\nimport trampoline\nfrom pkg import boot\n")
    _write(repo, "pkg/boot.py", "X = 1\n")
    _write(repo, "tools/lib/trampoline.py", "import helper\n")
    _write(repo, "tools/lib/helper.py", "")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "lib")
    c = ec.test_closure(repo, [], [SEED])
    assert c.paths is not None
    assert {"pkg/boot.py", "tools/lib/trampoline.py", "tools/lib/helper.py"} <= c.paths
    assert "pkg/unrelated.py" not in c.paths


def test_conftest_pytest_plugins_and_their_imports_are_in_the_closure(repo: Path) -> None:
    _write(repo, "conftest.py", 'pytest_plugins = [\n    "pkg.testing.watchdog",\n    "absent.plugin",\n]\n')
    _write(repo, "pkg/testing/__init__.py", "")
    _write(repo, "pkg/testing/watchdog.py", "from pkg import dep\nfrom pkg.testing import timer\n")
    _write(repo, "pkg/testing/timer.py", "")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "plugins")
    c = ec.test_closure(repo, [], [SEED])
    assert c.paths is not None
    assert {"pkg/testing/__init__.py", "pkg/testing/watchdog.py", "pkg/testing/timer.py"} <= c.paths
    assert "pkg/unrelated.py" not in c.paths


def test_conftest_quoted_module_and_its_whole_import_chain_are_in_the_closure(repo: Path) -> None:
    # Witness 6b3402: coordinator_core/conftest.py imports "coordinator_core.sizing_assemble" by
    # importlib, and the depth-capped walk dropped its imports, so the mirror collected 0 tests.
    _write(repo, "conftest.py", 'import importlib\nfor _n in ("pkg.engine",):\n    importlib.import_module(_n)\n')
    _write(repo, "pkg/engine/__init__.py", "from pkg import hop1\n")
    for i in range(1, 6):
        _write(repo, f"pkg/hop{i}.py", f"from pkg import hop{i + 1}\n" if i < 5 else "X = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "deep")
    c = ec.test_closure(repo, [], [SEED])
    assert c.paths is not None
    assert {"pkg/engine/__init__.py", *(f"pkg/hop{i}.py" for i in range(1, 6))} <= c.paths
    assert "pkg/unrelated.py" not in c.paths


def test_bare_dotted_submodule_and_every_package_init_are_in_the_closure(repo: Path) -> None:
    # Witness example-game-repo 036bcf: a conftest under a hyphenated sys.path root imports
    # `from likeness.pipeline import native_postcreate_stage`; the mirror got neither the submodule nor
    # pipeline/__init__.py, so the package resolved as a namespace and 0 tests collected.
    lib = "tools/like-proto"
    _write(repo, f"{lib}/tests/conftest.py", "from likeness.pipeline import native_stage as ns\n")
    _write(repo, f"{lib}/tests/test_x.py", "")
    _write(repo, f"{lib}/likeness/__init__.py", "")
    _write(repo, f"{lib}/likeness/pipeline/__init__.py", "")
    _write(repo, f"{lib}/likeness/pipeline/native_stage.py", "")
    _write(repo, f"{lib}/likeness/pipeline/driver.py", "")
    _write(repo, f"{lib}/likeness/pipeline/unrelated.py", "")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "lib")
    c = ec.test_closure(repo, [f"{lib}/likeness/pipeline/driver.py"], ["pytest", f"{lib}/tests/test_x.py"])
    assert c.paths is not None
    assert {
        f"{lib}/likeness/__init__.py", f"{lib}/likeness/pipeline/__init__.py",
        f"{lib}/likeness/pipeline/native_stage.py", f"{lib}/likeness/pipeline/driver.py",
    } <= c.paths
    assert f"{lib}/likeness/pipeline/unrelated.py" not in c.paths


def test_pytest_plugins_reads_a_bare_string_and_ignores_other_names() -> None:
    assert ec._pytest_plugins('pytest_plugins = "a.b"\nother = ["c.d"]\n') == ["a.b"]
    assert ec._pytest_plugins("x = 1\n") == []
    assert ec._pytest_plugins("pytest_plugins = (\n") == []
