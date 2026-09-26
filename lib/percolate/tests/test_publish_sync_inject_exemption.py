"""Regression tests for `sync_mirror`/`sync_flat_mirror`'s Phase-2 orphan sweep
exempting `inject`-copied paths and `basename_rename` destinations.

DoE-thread item 26 (docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-doe-thread.md
`C19`): an `inject` entry (coordinator_core/percolate/inject.py `run_inject`)
copies content into the destination with no source-dir analog by construction --
Phase 2 has no other way to tell "injected" apart from "genuinely stray", both
being present at dst and absent from src. Left unexempted, Phase 2 deletes the
injected content the very next pass, and a row whose renamed destination is not
top-level (`sync_flat_mirror` carried no rename exemption at all before this
change) sees the published rename reaped and re-added under its pre-rename
source name.

Loaded via `coordinator/lib` on `sys.path` rather than a bare
`spec_from_file_location`: `publish_sync.py` does `from .ignore import ...`,
which only resolves when the module is loaded as part of its `percolate`
package -- same idiom as the sibling `test_publish_sync_nested_rename_exemption.py`.

Negative-spec: no persona names, no codenames, no consumer-home path literals;
all fixture content is synthetic. No `git init` and no subprocess -- both
functions under test only read and write files.
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


class TestSyncMirrorInjectExemption:
    def _seed(self, tmp_path: Path) -> "tuple[Path, Path]":
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        (src / "widgets").mkdir(parents=True)
        (dst / "widgets").mkdir(parents=True)
        (src / "widgets" / "core.py").write_text("core\n", encoding="utf-8")
        (dst / "widgets" / "core.py").write_text("core\n", encoding="utf-8")
        # Injected: no source-dir analog by construction.
        (dst / "widgets" / "vendored.json").write_text("{}\n", encoding="utf-8")
        # Renamed: published under a name the source never carries.
        (dst / "widgets" / "test_published_name.py").write_text(
            "payload\n", encoding="utf-8"
        )
        # Genuinely stray, exempted by neither set.
        (dst / "widgets" / "test_retired.py").write_text("retired\n", encoding="utf-8")
        return src, dst

    def test_an_injected_file_survives_the_sweep(self, tmp_path):
        src, dst = self._seed(tmp_path)

        publish_sync.sync_mirror(
            src,
            dst,
            _no_ignore(),
            False,
            injected_paths=frozenset({"widgets/vendored.json"}),
            renamed_file_names=frozenset({"test_published_name.py"}),
        )

        assert (dst / "widgets" / "vendored.json").is_file()

    def test_a_rename_destination_survives_the_sweep(self, tmp_path):
        src, dst = self._seed(tmp_path)

        publish_sync.sync_mirror(
            src,
            dst,
            _no_ignore(),
            False,
            injected_paths=frozenset({"widgets/vendored.json"}),
            renamed_file_names=frozenset({"test_published_name.py"}),
        )

        assert (dst / "widgets" / "test_published_name.py").is_file()

    def test_a_genuine_orphan_is_still_reaped(self, tmp_path):
        src, dst = self._seed(tmp_path)

        publish_sync.sync_mirror(
            src,
            dst,
            _no_ignore(),
            False,
            injected_paths=frozenset({"widgets/vendored.json"}),
            renamed_file_names=frozenset({"test_published_name.py"}),
        )

        assert not (dst / "widgets" / "test_retired.py").exists()

    def test_the_injected_exemption_matches_the_full_qualified_path_not_basename(
        self, tmp_path
    ):
        src, dst = self._seed(tmp_path)
        (dst / "widgets" / "other").mkdir()
        (dst / "widgets" / "other" / "vendored.json").write_text(
            "{}\n", encoding="utf-8"
        )

        publish_sync.sync_mirror(
            src,
            dst,
            _no_ignore(),
            False,
            injected_paths=frozenset({"widgets/vendored.json"}),
        )

        # Same basename, different qualified path -- not the declared inject
        # entry, so it is not exempted and is reaped as a genuine orphan.
        assert not (dst / "widgets" / "other" / "vendored.json").exists()

    def test_an_unset_injected_paths_preserves_the_prior_reap(self, tmp_path):
        src, dst = self._seed(tmp_path)

        publish_sync.sync_mirror(src, dst, _no_ignore(), False)

        assert not (dst / "widgets" / "vendored.json").exists()


class TestSyncFlatMirrorInjectAndRenameExemption:
    def _seed(self, tmp_path: Path) -> "tuple[Path, Path]":
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir(parents=True)
        dst.mkdir(parents=True)
        (src / "top.py").write_text("top\n", encoding="utf-8")
        (dst / "top.py").write_text("top\n", encoding="utf-8")
        (dst / "vendored.json").write_text("{}\n", encoding="utf-8")
        (dst / "test_published_name.py").write_text("payload\n", encoding="utf-8")
        (dst / "test_retired.py").write_text("retired\n", encoding="utf-8")
        return src, dst

    def test_an_injected_file_survives_the_sweep(self, tmp_path):
        src, dst = self._seed(tmp_path)

        publish_sync.sync_flat_mirror(
            src,
            dst,
            _no_ignore(),
            False,
            injected_paths=frozenset({"vendored.json"}),
            renamed_file_names=frozenset({"test_published_name.py"}),
        )

        assert (dst / "vendored.json").is_file()

    def test_a_rename_destination_survives_the_sweep(self, tmp_path):
        src, dst = self._seed(tmp_path)

        publish_sync.sync_flat_mirror(
            src,
            dst,
            _no_ignore(),
            False,
            injected_paths=frozenset({"vendored.json"}),
            renamed_file_names=frozenset({"test_published_name.py"}),
        )

        assert (dst / "test_published_name.py").is_file()

    def test_a_genuine_orphan_is_still_reaped(self, tmp_path):
        src, dst = self._seed(tmp_path)

        publish_sync.sync_flat_mirror(
            src,
            dst,
            _no_ignore(),
            False,
            injected_paths=frozenset({"vendored.json"}),
            renamed_file_names=frozenset({"test_published_name.py"}),
        )

        assert not (dst / "test_retired.py").exists()

    def test_unset_exemptions_preserve_the_prior_reap(self, tmp_path):
        src, dst = self._seed(tmp_path)

        publish_sync.sync_flat_mirror(src, dst, _no_ignore(), False)

        assert not (dst / "vendored.json").exists()
        assert not (dst / "test_published_name.py").exists()
