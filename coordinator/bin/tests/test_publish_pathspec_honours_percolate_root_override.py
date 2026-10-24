"""`_required_pathspec_for_toplevel` resolves the percolate root from
`--percolate-root` (`_PERCOLATE_ROOT_OVERRIDE`), not the cwd's git root, so a
round launched from another checkout still scopes the archive to the clone's roots.

Run: python -m pytest coordinator/bin/tests/test_publish_pathspec_honours_percolate_root_override.py -q
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_pathspec_percolate_root_override_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


@pytest.fixture(autouse=True)
def _clear_state():
    publish._bootstrap_engine()
    publish._REQUIRED_PATHSPEC_CACHE.clear()
    publish._PERCOLATE_ROOT_OVERRIDE = None
    yield
    publish._REQUIRED_PATHSPEC_CACHE.clear()
    publish._PERCOLATE_ROOT_OVERRIDE = None


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
    )
    return result.stdout.strip()


def _make_repo(base: Path, name: str, root_name: str) -> "tuple[Path, str]":
    repo = base / name
    (repo / "setup").mkdir(parents=True)
    (repo / root_name).mkdir()
    (repo / root_name / "payload.txt").write_text("x\n", encoding="utf-8")
    (repo / "setup" / "publish-targets.portable").write_text(
        f"row-{root_name}|flat-mirror|{repo / root_name}|{base / 'dest' / root_name}\n",
        encoding="utf-8",
    )
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_override_scopes_pathspec_to_the_clone_not_the_cwd(tmp_path, monkeypatch):
    clone, clone_sha = _make_repo(tmp_path, "clone", "clone-root")
    authoring, _ = _make_repo(tmp_path, "authoring", "authoring-root")
    monkeypatch.setattr(publish, "_REPO_ROOT", clone)
    monkeypatch.chdir(authoring)
    monkeypatch.setattr(publish, "_PERCOLATE_ROOT_OVERRIDE", str(clone))

    pathspec = publish._required_pathspec_for_toplevel(clone, clone_sha)

    assert ":(literal)clone-root" in pathspec
    assert ":(literal)authoring-root" not in pathspec


def test_override_naming_another_checkout_is_followed_not_the_cwd(tmp_path, monkeypatch):
    clone, clone_sha = _make_repo(tmp_path, "clone", "clone-root")
    authoring, _ = _make_repo(tmp_path, "authoring", "authoring-root")
    monkeypatch.setattr(publish, "_REPO_ROOT", clone)
    monkeypatch.chdir(clone)
    monkeypatch.setattr(publish, "_PERCOLATE_ROOT_OVERRIDE", str(authoring))

    with pytest.raises(publish.GitMaterializeError):
        publish._required_pathspec_for_toplevel(clone, clone_sha)
