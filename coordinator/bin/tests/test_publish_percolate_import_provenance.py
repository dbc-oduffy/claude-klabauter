"""coordinator/bin/tests/test_publish_percolate_import_provenance.py — the
inverse of the retired AC9 gate (DR-390).

`_import_claude_klabauter_percolate` (coordinator/bin/publish.py) once refused the whole
publish when any file in its percolate-transform import set carried an
uncommitted edit. That refusal is FORBIDDEN (PM ruling, 2026-08-30): this tree
is shared by dozens of concurrent sessions and is near-permanently dirty, so a
source-dirty refusal blocks publishes on edits the publisher did not make and
cannot commit or revert.

Provenance is answered by recording, never by refusing: `percolate/round.py`
§ `_dirty_content_digest` suffixes the round's engine identity with
`+dirty-<8hex>`, so bytes produced by an uncommitted transform are stamped as
such rather than never produced at all.

Negative spec, pinned below: no source-tree dirty probe may return to this
driver's import path.

Run: python -m pytest coordinator/bin/tests/test_publish_percolate_import_provenance.py -q
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_percolate_import_provenance_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def test_no_source_dirty_gate_symbols_survive() -> None:
    """The retired gate's own names must not come back under a new caller."""
    for name in ("_assert_percolate_transform_set_clean", "_PERCOLATE_TRANSFORM_SET_PATHS"):
        assert not hasattr(publish, name), (
            f"{name} is back in publish.py — a source-tree dirty refusal is forbidden (DR-390)"
        )


def test_import_path_runs_no_source_dirty_probe() -> None:
    """`_import_claude_klabauter_percolate`'s body must carry no cleanliness probe of the
    engine source tree. `_git_is_clean` at the DESTINATION is a different
    question (published-mirror drift) and stays."""
    source = (_BIN_DIR / "publish.py").read_text(encoding="utf-8")
    start = source.index("def _import_claude_klabauter_percolate(")
    end = source.index("\ndef ", start + 1)
    body = source[start:end]
    for banned in ("status_porcelain_scoped", "_dirty_paths_under", "_git_is_clean", "--porcelain"):
        assert banned not in body, (
            f"_import_claude_klabauter_percolate probes source cleanliness via {banned!r} — forbidden (DR-390)"
        )
