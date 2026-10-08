"""coordinator/bin/tests/test_publish_staging_survives_same_mirror_sweep.py —
a later row's stale-staging sweep must not reap an earlier row's live staging
tree for the same mirror.

`shutil.copytree` stamps the dest root's mtime onto the fresh staging dir; with
an hours-old mirror root the age-keyed sweep read the tree as orphaned and
deleted it before the round's throwaway overlay read it (FileNotFoundError in
`_overlay_root`, every row unpublished).

Run: python -m pytest coordinator/bin/tests/test_publish_staging_survives_same_mirror_sweep.py -q
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import time
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_staging_survives_same_mirror_sweep_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _old_mirror(tmp_path: Path) -> Path:
    dest = tmp_path / "mirror"
    (dest / ".git").mkdir(parents=True)
    (dest / "a.txt").write_text("a", encoding="utf-8")
    old = time.time() - 3 * 3600
    os.utime(dest, (old, old))
    return dest


def test_fresh_staging_dir_is_not_stamped_with_the_dest_mtime(tmp_path):
    staging = publish._create_publish_staging_dir(_old_mirror(tmp_path))
    assert time.time() - staging.stat().st_mtime < 600


def test_sweep_never_reaps_a_staging_dir_this_process_minted(tmp_path):
    dest = _old_mirror(tmp_path)
    staging = publish._create_publish_staging_dir(dest)
    old = time.time() - 3 * 3600
    os.utime(staging, (old, old))
    publish._sweep_stale_publish_staging_dirs(dest, publish.RunTotals(), out=io.StringIO())
    assert staging.is_dir()


def test_sweep_still_reaps_an_orphan_from_another_run(tmp_path):
    dest = _old_mirror(tmp_path)
    orphan = dest.parent / f".{dest.name}.publish-staging-orphan"
    orphan.mkdir()
    old = time.time() - 3 * 3600
    os.utime(orphan, (old, old))
    publish._sweep_stale_publish_staging_dirs(dest, publish.RunTotals(), out=io.StringIO())
    assert not orphan.exists()
