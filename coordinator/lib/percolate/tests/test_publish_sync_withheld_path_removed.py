"""A destination file the row's allowlist now withholds leaves the mirror.

`sync_mirror` already reports the withheld destination file as removed (the
restricted source tree omits it). The cold-round filter in
`publish._removals_this_source_shipped` then drops it as "never shipped by this
repo" because the path is live in source history, so the file stays.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate import publish_sync  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[4]
_WITHHELD = "git/tests/test_withheld_flag.py"


def _load_publish():
    spec = importlib.util.spec_from_file_location(
        "publish_withheld_removal_under_test", _REPO_ROOT / "coordinator" / "bin" / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _trees(tmp_path: Path) -> "tuple[Path, Path]":
    src, dst = tmp_path / "src", tmp_path / "dst"
    for root in (src, dst):
        (root / "git" / "tests").mkdir(parents=True)
        (root / "git" / "kept.py").write_text("kept\n", encoding="utf-8")
    (dst / _WITHHELD).write_text("withheld\n", encoding="utf-8")
    return src, dst


def test_sync_reports_the_withheld_file_as_removed(tmp_path):
    src, dst = _trees(tmp_path)
    sink: "set[str]" = set()

    _synced, removed = publish_sync.sync_mirror(
        src, dst, publish_sync.load_ignore(None), True, removed_sink=sink
    )

    assert removed == 1
    assert sink == {_WITHHELD}


def test_cold_filter_keeps_an_allowlist_withheld_removal():
    publish = _load_publish()
    target = SimpleNamespace(
        name="engine-row",
        source_dir=_REPO_ROOT / "coordinator_core",
        allowlist=f"git,!{_WITHHELD}",
    )

    kept = publish._removals_this_source_shipped(target, {_WITHHELD}, frozenset())

    assert kept == {_WITHHELD}
