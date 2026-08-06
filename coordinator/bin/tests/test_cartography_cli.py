"""test_cartography_cli — pytest tests for coordinator/bin/cartography.py.

Spec backlink: A8 (cartography operator-seam spinoff, 2026-08-06 wave)

Coverage:
  op enumeration:
    test_cartography_ops_derived_from_registry_map_not_hardcoded
  op-name resolution:
    test_resolve_op_name_accepts_bare_suffix
    test_resolve_op_name_accepts_fully_qualified
    test_resolve_op_name_unknown_exits_1
  CLI surface (in-process argv):
    test_main_list_prints_known_ops_and_exits_0
    test_main_missing_op_exits_1
    test_main_missing_target_root_exits_1
    test_main_malformed_params_json_exits_1
    test_main_params_non_object_exits_1
  end-to-end (subprocess, real coordinator_core.invoke spawn):
    test_end_to_end_file_index_against_tmp_git_repo
    test_end_to_end_unknown_op_exits_1
    test_end_to_end_tree_against_tmp_git_repo
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _BIN_DIR.parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "cartography_cli",
        _BIN_DIR / "cartography.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


# ---------------------------------------------------------------------------
# op enumeration
# ---------------------------------------------------------------------------


def test_cartography_ops_derived_from_registry_map_not_hardcoded() -> None:
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    expected = sorted(op for op in OP_MODULE_MAP if op.startswith("cartography."))
    assert _mod._cartography_ops() == expected
    # The eight ops named in the brief must all be present (a ninth,
    # op_edges, may or may not be, depending on concurrent-wave state).
    for suffix in (
        "tree",
        "file_index",
        "churn",
        "symbols",
        "edges",
        "count_references",
        "stack",
        "chunk_table",
    ):
        assert f"cartography.{suffix}" in expected


# ---------------------------------------------------------------------------
# op-name resolution
# ---------------------------------------------------------------------------


def test_resolve_op_name_accepts_bare_suffix() -> None:
    known = ["cartography.file_index", "cartography.tree"]
    assert _mod._resolve_op_name("file_index", known) == "cartography.file_index"


def test_resolve_op_name_accepts_fully_qualified() -> None:
    known = ["cartography.file_index", "cartography.tree"]
    assert _mod._resolve_op_name("cartography.tree", known) == "cartography.tree"


def test_resolve_op_name_unknown_exits_1() -> None:
    known = ["cartography.file_index", "cartography.tree"]
    with pytest.raises(SystemExit) as exc:
        _mod._resolve_op_name("bogus", known)
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# CLI surface (in-process)
# ---------------------------------------------------------------------------


def test_main_list_prints_known_ops_and_exits_0(capsys) -> None:
    rc = _mod.main(["--list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "cartography.file_index" in out
    assert "cartography.tree" in out


def test_main_missing_op_exits_1(capsys) -> None:
    rc = _mod.main([])
    assert rc == 1
    assert "an <op> is required" in capsys.readouterr().err


def test_main_missing_target_root_exits_1(capsys) -> None:
    rc = _mod.main(["file_index"])
    assert rc == 1
    assert "--target-root is required" in capsys.readouterr().err


def test_main_malformed_params_json_exits_1(capsys) -> None:
    rc = _mod.main(["file_index", "--target-root", "/tmp", "--params", "not-json"])
    assert rc == 1
    assert "--params must be valid JSON" in capsys.readouterr().err


def test_main_params_non_object_exits_1(capsys) -> None:
    rc = _mod.main(["file_index", "--target-root", "/tmp", "--params", "[1, 2]"])
    assert rc == 1
    assert "must decode to a JSON object" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# end-to-end (real subprocess spawn of coordinator_core.invoke)
# ---------------------------------------------------------------------------


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, creationflags=_NO_WINDOW)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@example.com"],
        check=True,
        creationflags=_NO_WINDOW,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
        creationflags=_NO_WINDOW,
    )
    (repo / "a.py").write_text("print(1)\n")
    subprocess.run(["git", "-C", str(repo), "add", "a.py"], check=True, creationflags=_NO_WINDOW)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "init"],
        check=True,
        creationflags=_NO_WINDOW,
    )
    return repo


def test_end_to_end_file_index_against_tmp_git_repo(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    proc = subprocess.run(
        [
            sys.executable,
            str(_BIN_DIR / "cartography.py"),
            "file_index",
            "--target-root",
            str(repo),
            "--repo",
            str(_REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        check=False,
        creationflags=_NO_WINDOW,
    )

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["file_count"] == 1
    assert "a.py" in result["index"]


def test_end_to_end_tree_against_tmp_git_repo(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    proc = subprocess.run(
        [
            sys.executable,
            str(_BIN_DIR / "cartography.py"),
            "cartography.tree",
            "--target-root",
            str(repo),
            "--repo",
            str(_REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        check=False,
        creationflags=_NO_WINDOW,
    )

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert "a.py" in result["files"]


def test_end_to_end_unknown_op_exits_1(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    proc = subprocess.run(
        [
            sys.executable,
            str(_BIN_DIR / "cartography.py"),
            "not-a-real-op",
            "--target-root",
            str(repo),
            "--repo",
            str(_REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        check=False,
        creationflags=_NO_WINDOW,
    )

    assert proc.returncode == 1
    assert "unknown op" in proc.stderr
