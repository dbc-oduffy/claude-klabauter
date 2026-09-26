"""Regression tests for `sync_mirror`'s rename exemption reaching NESTED files.

The gap (2026-08-26): the rename exemption landed on the top-level orphan sweep
(`_sweep_mirror_top_level_orphans`) and stopped there. `sync_mirror`'s per-plugin
phase-2 delete loop kept reaping unconditionally, so a row whose renamed files live in
a subdirectory saw the two legs disagree: top-level renames exempt and silent, nested
ones printed as `REMOVE: <published-name> (not in source)` and re-added under their
pre-rename source names. Read as a bug in the wild -- a preview that looked like the
rename running BACKWARDS, filed against the store's `basename_rename` map, which was
firing correctly all along.

Convergence was never at risk either way (the content-transform pass renames the files
again immediately after sync). What the exemption buys is a preview a reader can trust
and a round that does not delete-then-recreate a file per renamed basename.

Loaded via `coordinator/lib` on `sys.path` rather than a bare `spec_from_file_location`:
`publish_sync.py` does `from .ignore import ...`, which only resolves when the module is
loaded as part of its `percolate` package -- same idiom as the sibling
`test_publish_sync_top_level_orphan_sweep.py`.

Negative-spec: no persona names, no codenames, no consumer-home path literals; all
fixture content is synthetic. No `git init` and no subprocess -- `sync_mirror` only
reads and writes files.
"""

from __future__ import annotations

import sys
from pathlib import Path

_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate import publish_sync  # noqa: E402


def _no_ignore():
    return publish_sync.load_ignore(None)


def _seed(tmp_path: Path) -> "tuple[Path, Path]":
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    (src / "tests").mkdir(parents=True)
    (dst / "tests").mkdir(parents=True)
    (src / "top.py").write_text("top\n", encoding="utf-8")
    (dst / "top.py").write_text("top\n", encoding="utf-8")
    (src / "tests" / "test_source_name.py").write_text("payload\n", encoding="utf-8")
    (dst / "tests" / "test_published_name.py").write_text("payload\n", encoding="utf-8")
    (dst / "tests" / "test_retired.py").write_text("retired\n", encoding="utf-8")
    return src, dst


class TestNestedRenameExemption:
    def test_a_nested_published_rename_target_survives_the_sweep(self, tmp_path):
        src, dst = _seed(tmp_path)

        publish_sync.sync_mirror(
            src,
            dst,
            _no_ignore(),
            False,
            renamed_file_names=frozenset({"test_published_name.py"}),
        )

        assert (dst / "tests" / "test_published_name.py").is_file()

    def test_a_genuine_nested_orphan_is_still_reaped(self, tmp_path):
        src, dst = _seed(tmp_path)

        publish_sync.sync_mirror(
            src,
            dst,
            _no_ignore(),
            False,
            renamed_file_names=frozenset({"test_published_name.py"}),
        )

        assert not (dst / "tests" / "test_retired.py").exists()

    def test_the_source_file_is_still_copied_under_its_source_name(self, tmp_path):
        src, dst = _seed(tmp_path)

        publish_sync.sync_mirror(
            src,
            dst,
            _no_ignore(),
            False,
            renamed_file_names=frozenset({"test_published_name.py"}),
        )

        assert (dst / "tests" / "test_source_name.py").is_file()

    def test_an_unknown_exemption_set_preserves_the_prior_reap(self, tmp_path):
        src, dst = _seed(tmp_path)

        publish_sync.sync_mirror(src, dst, _no_ignore(), False)

        assert not (dst / "tests" / "test_published_name.py").exists()
        assert not (dst / "tests" / "test_retired.py").exists()

    def test_the_exemption_matches_on_basename_not_on_relative_path(self, tmp_path):
        src, dst = _seed(tmp_path)
        (src / "tests" / "deep").mkdir()
        (dst / "tests" / "deep").mkdir()
        (dst / "tests" / "deep" / "test_published_name.py").write_text(
            "payload\n", encoding="utf-8"
        )
        (src / "tests" / "deep" / "keep.py").write_text("keep\n", encoding="utf-8")

        publish_sync.sync_mirror(
            src,
            dst,
            _no_ignore(),
            False,
            renamed_file_names=frozenset({"test_published_name.py"}),
        )

        assert (dst / "tests" / "deep" / "test_published_name.py").is_file()
