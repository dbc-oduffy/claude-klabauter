"""publish.py --dest-root-override/--dest-root-live: rows re-root onto a scratch
clone at every load site, the mirror-key lookup follows the clone back to its
live key, and the override never outlives `main()`.

Hermetic: tmp_path repos and a tmp machine-local registry dir only.

Run: python -m pytest coordinator/bin/tests/test_publish_dest_root_override.py -q -p no:cacheprovider
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_KEY = "some-engine-mirror"


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_dest_root_override_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, check=True, creationflags=_NO_WINDOW
    )


def _init_repo(root: Path, branch: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", branch, cwd=root)
    _git("config", "user.email", "dest-root-override-test@claude-klabauter.test", cwd=root)
    _git("config", "user.name", "Dest Root Override Test", cwd=root)
    _git("config", "commit.gpgsign", "false", cwd=root)
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", ".gitkeep", cwd=root)
    _git("commit", "-m", "chore: init", cwd=root)


def _registry(tmp_path: Path, live: Path, monkeypatch) -> None:
    reg = tmp_path / "registry"
    reg.mkdir()
    (reg / "registry.toml").write_text(
        f'[publish.mirrors]\n"{_KEY}.path" = "{live.as_posix()}"\n', encoding="utf-8"
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))


def _override(live: Path, clone: Path):
    return publish.DestRootOverride.of(live, clone)


def _rows(live: Path, other: Path) -> list:
    return [
        f"top|mirror|/src/a|{live}",
        f"lib|mirror|/src/b|{live}/coordinator_core|x",
        f"foreign|mirror|/src/c|{other}",
    ]


@pytest.fixture(autouse=True)
def _clear_override():
    publish._DEST_ROOT_OVERRIDE = None
    yield
    publish._DEST_ROOT_OVERRIDE = None


def test_one_flag_alone_is_a_usage_error_and_syncs_nothing(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("must refuse before resolving anything")

    monkeypatch.setattr(publish, "_resolve_percolate_root_and_rung", boom)
    monkeypatch.setattr(publish, "load_targets", boom)
    for argv in (
        ["--dest-root-override", str(tmp_path / "clone")],
        ["--dest-root-live", str(tmp_path / "live")],
    ):
        assert publish.main(argv) == 2
        assert "together" in capsys.readouterr().err
    assert publish._DEST_ROOT_OVERRIDE is None


def test_nested_roots_are_a_usage_error(tmp_path, capsys):
    live = tmp_path / "live"
    rc = publish.main(
        ["--dest-root-live", str(live), "--dest-root-override", str(live / "inner")]
    )
    assert rc == 2
    assert "nested" in capsys.readouterr().err
    assert publish._DEST_ROOT_OVERRIDE is None


def test_load_rows_reroots_with_override_and_is_identity_without(tmp_path, monkeypatch):
    live, clone, other = tmp_path / "live", tmp_path / "clone", tmp_path / "other"
    raw = _rows(live, other)
    monkeypatch.setattr(publish, "load_targets", lambda setup_dir, **kw: list(raw))

    assert publish._load_rows(tmp_path, target_filter="") == raw

    publish._DEST_ROOT_OVERRIDE = _override(live, clone)
    got = publish._load_rows(tmp_path, target_filter="")
    real_clone = os.path.realpath(clone)
    assert got[0] == f"top|mirror|/src/a|{real_clone}"
    assert got[1] == f"lib|mirror|/src/b|{real_clone}/coordinator_core|x"
    assert got[2] == raw[2]


def test_every_load_site_goes_through_load_rows():
    import ast

    tree = ast.parse((_BIN_DIR / "publish.py").read_text(encoding="utf-8"))
    callers = [
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "load_targets"
    ]
    assert callers == ["_load_rows"]


def test_mirror_key_follows_the_clone_only_under_the_override(tmp_path, monkeypatch):
    live, clone = tmp_path / "live", tmp_path / "clone"
    live.mkdir()
    clone.mkdir()
    _registry(tmp_path, live, monkeypatch)

    assert publish._publish_mirror_key_for_repo_root(clone) is None
    publish._DEST_ROOT_OVERRIDE = _override(live, clone)
    assert publish._publish_mirror_key_for_repo_root(clone) == _KEY
    assert publish._publish_mirror_key_for_repo_root(live) == _KEY


def test_engine_branch_guard_refuses_clone_on_main_and_passes_candidate(
    tmp_path, monkeypatch, capsys
):
    live, clone = tmp_path / "live", tmp_path / "clone"
    live.mkdir()
    _init_repo(clone, "main")
    _registry(tmp_path, live, monkeypatch)
    monkeypatch.setattr(
        publish, "_engine_declaring_mirror_keys", lambda setup_dir: frozenset({_KEY})
    )
    target = publish.ResolvedTarget(
        name="top", mode="mirror", source_dir=tmp_path / "src", dest_dir=clone
    )

    # Without the override the clone is "not a registered mirror": the guard is blind.
    assert publish.assert_dest_on_declared_ref(target, publish.RunTotals()) is True

    publish._DEST_ROOT_OVERRIDE = _override(live, clone)
    assert publish.assert_dest_on_declared_ref(target, publish.RunTotals()) is False
    assert "candidate" in capsys.readouterr().err

    _git("checkout", "-b", "candidate", cwd=clone)
    assert publish.assert_dest_on_declared_ref(target, publish.RunTotals()) is True


def test_lock_root_of_a_rerooted_row_is_the_clone(tmp_path, monkeypatch):
    live, clone = tmp_path / "live", tmp_path / "clone"
    live.mkdir()
    _init_repo(clone, "candidate")
    raw = [f"top|mirror|{tmp_path / 'src'}|{live}"]
    monkeypatch.setattr(publish, "load_targets", lambda setup_dir, **kw: list(raw))
    publish._DEST_ROOT_OVERRIDE = _override(live, clone)

    row = publish.parse_target_row(publish._load_rows(tmp_path)[0])
    assert os.path.realpath(row.dest_dir) == os.path.realpath(clone)
    assert os.path.realpath(publish._dest_repo_root(row.dest_dir)) == os.path.realpath(clone)


def test_override_is_set_during_main_and_reset_on_exception(tmp_path, monkeypatch):
    live, clone = tmp_path / "live", tmp_path / "clone"
    seen = {}

    def raising(*a, **k):
        seen["during"] = publish._DEST_ROOT_OVERRIDE
        raise RuntimeError("stop here")

    monkeypatch.setattr(publish, "_resolve_percolate_root_and_rung", raising)
    with pytest.raises(RuntimeError, match="stop here"):
        publish.main(["--dest-root-live", str(live), "--dest-root-override", str(clone)])
    assert seen["during"] is not None
    assert Path(seen["during"].clone_root) == Path(os.path.realpath(clone))
    assert publish._DEST_ROOT_OVERRIDE is None

    seen.clear()
    with pytest.raises(RuntimeError, match="stop here"):
        publish.main([])
    assert seen["during"] is None
