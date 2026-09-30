"""coordinator/bin/tests/test_publish_staging_progress.py -- the staging copy
publishes its own numerator and denominator.

`percolate-round.py` captures `publish.py`'s output until exit, so a long
staging copy was indistinguishable from a hung one. Observers estimated the
denominator from unrelated artifacts (whole-tree count, a prior round's
manifest) and were wrong by up to 6.4x. The declared count must be the exact
number of files `copytree` hands to its copy function, i.e. the same top-level
exclusion as `_create_publish_staging_dir`'s `_ignore`.

Run: python -m pytest coordinator/bin/tests/test_publish_staging_progress.py -x -q
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_staging_progress_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _seed(root: Path) -> Path:
    dest = root / "dest"
    (dest / "pkg" / "sub").mkdir(parents=True)
    (dest / "pkg" / "a.py").write_text("a\n", encoding="utf-8")
    (dest / "pkg" / "sub" / "b.py").write_text("b\n", encoding="utf-8")
    (dest / "top.txt").write_text("t\n", encoding="utf-8")
    (dest / ".git").mkdir()
    (dest / ".git" / "HEAD").write_text("ref\n", encoding="utf-8")
    env = dest / ".fleet-env.gen-1-abc" / "Lib"
    env.mkdir(parents=True)
    for i in range(7):
        (env / f"m{i}.py").write_text("x\n", encoding="utf-8")
    return dest


def test_declared_excludes_git_and_fleet_env(tmp_path):
    dest = _seed(tmp_path)
    unstaged = publish._fleet_env_unstaged_names(dest)
    assert publish._count_stageable_files(dest, unstaged) == 3


def test_progress_file_reports_declared_equal_to_staged(tmp_path, monkeypatch, capsys):
    dest = _seed(tmp_path)
    sink = tmp_path / "progress.json"
    monkeypatch.setattr(publish, "_staging_progress_file", sink)
    staging = publish._create_publish_staging_dir(dest)
    data = json.loads(sink.read_text(encoding="utf-8"))
    shutil.rmtree(staging)
    assert data["phase"] == "staged"
    assert data["declared"] == 3
    assert data["staged"] == data["declared"]
    assert isinstance(data["pid"], int)
    assert "copying 3 files" in capsys.readouterr().err


def test_no_sink_still_states_the_denominator_on_stderr(tmp_path, monkeypatch, capsys):
    dest = _seed(tmp_path)
    monkeypatch.setattr(publish, "_staging_progress_file", None)
    staging = publish._create_publish_staging_dir(dest)
    assert "copying 3 files" in capsys.readouterr().err
    shutil.rmtree(staging)
