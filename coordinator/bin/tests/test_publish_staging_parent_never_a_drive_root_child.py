"""A mirror sitting directly on a drive root must stage under its own `.git`,
never in a new directory beside it on the anchor.

The anchor is never touched: these call the pure resolver against the real
drive root of tmp_path and stub only the `.git` existence check, so no test
creates anything outside tmp_path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parents[1]


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_staging_parent_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _drive_root_dest(tmp_path: Path) -> Path:
    anchor = Path(tmp_path.anchor)
    return anchor / "no-such-mirror-for-staging-test"


def test_drive_root_mirror_stages_inside_its_own_git_dir(tmp_path, monkeypatch):
    dest = _drive_root_dest(tmp_path)
    git_dir = dest / ".git"
    real_is_dir = Path.is_dir
    monkeypatch.setattr(Path, "is_dir", lambda p: p == git_dir or real_is_dir(p))

    parent = publish._publish_staging_parent(dest)

    assert parent.parent == git_dir
    assert parent.parent.parent == dest
    assert parent.parent.parent.parent == dest.parent, "staging must sit below the mirror"


def test_drive_root_mirror_without_git_dir_is_refused(tmp_path):
    with pytest.raises(RuntimeError, match="no .git"):
        publish._publish_staging_parent(_drive_root_dest(tmp_path))


def test_nested_mirror_keeps_its_parent(tmp_path):
    dest = tmp_path / "mirror"
    assert publish._publish_staging_parent(dest) == tmp_path
