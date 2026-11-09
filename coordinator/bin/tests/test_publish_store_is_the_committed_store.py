"""A publish round reads its percolate store at the source commit, not the working tree.

A rename rule committed at `--commit` but absent from the working copy otherwise
ships the file under its unrenamed (codename-bearing) name.

Run: python -m pytest coordinator/bin/tests/test_publish_store_is_the_committed_store.py -q
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
        "publish_store_is_committed_store_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=True,
        text=True,
        creationflags=_NO_WINDOW,
    ).stdout.strip()


@pytest.fixture
def setup_dir(tmp_path):
    root = tmp_path / "src"
    store = root / "setup" / "percolate-hooks" / "percolate-store.yaml"
    store.parent.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    store.write_text("rules: [old]\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "old")
    store.write_text("rules: [old, new]\n", encoding="utf-8")
    _git(root, "commit", "-q", "-am", "new")
    return root / "setup"


def test_store_is_read_at_the_source_ref_when_the_working_copy_lags(setup_dir, monkeypatch):
    head = _git(setup_dir.parent, "rev-parse", "HEAD")
    (setup_dir / "percolate-hooks" / "percolate-store.yaml").write_text(
        "rules: [old]\n", encoding="utf-8"
    )
    monkeypatch.setattr(publish, "_source_ref_override", head)
    located = publish.locate_percolate_store(setup_dir)
    assert located.name == "percolate-store.yaml"
    assert located.read_text(encoding="utf-8") == "rules: [old, new]\n"


def test_store_is_the_working_copy_when_it_matches_the_ref(setup_dir, monkeypatch):
    monkeypatch.setattr(publish, "_source_ref_override", "")
    located = publish.locate_percolate_store(setup_dir)
    assert located == setup_dir / "percolate-hooks" / "percolate-store.yaml"


def test_archive_scope_loads_the_committed_registry(setup_dir, monkeypatch, tmp_path):
    """A row the pinned ref has but the working copy dropped is still published, so the archive
    scope must load rows from the same committed registry main does, or its root is never extracted."""
    root = setup_dir.parent
    committed = tmp_path / "committed" / "publish-targets.portable"
    seen = {}

    def fake_load_rows(_setup_dir, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(publish, "_REPO_ROOT", root)
    monkeypatch.setattr(publish, "_bootstrap_engine", lambda: None)
    monkeypatch.setattr(publish, "_resolve_percolate_root_and_rung", lambda override=None: (root, None))
    monkeypatch.setattr(publish, "_committed_portable_targets_file", lambda _d: committed)
    monkeypatch.setattr(publish, "_load_rows", fake_load_rows)
    monkeypatch.setattr(publish, "_all_inject_srcs_resolved", lambda *_a: [])
    monkeypatch.setattr(publish, "_REQUIRED_PATHSPEC_CACHE", {})
    with pytest.raises(publish.GitMaterializeError):
        publish._required_pathspec_for_toplevel(root, _git(root, "rev-parse", "HEAD"))
    assert seen["portable_targets_file"] == committed
