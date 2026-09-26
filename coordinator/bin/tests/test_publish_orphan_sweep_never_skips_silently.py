"""coordinator/bin/tests/test_publish_orphan_sweep_never_skips_silently.py --
regression coverage for the `renamed_file_names is None` silent skip in
`publish.py`'s top-level mirror orphan sweep.

Defect (reported by DoE): near `process_target`'s `dispatch_mirror_like`
call, `sweep_top_level_orphans` was computed as `_dest_is_owned_subdir(...)
and renamed_file_names is not None` -- when the rename exemption could not
resolve (engine unavailable, ledger/store lookup raised), `renamed_file_names`
stayed `None`, the sweep silently disabled itself, and NOTHING was printed to
say so. After DoE's wiki reorg, a stale flat file survived a real (non-dry)
publish of `coordinator-claude-toplevel-wiki` (229 vs 230 files) because the
skip left no trace.

`resolve_effective_renamed_file_names` is the pure decision this proves:
- an already-resolved exemption passes through unchanged;
- an unresolved exemption on a row CONFIRMED to declare no renames has
  nothing for the sweep to protect, so it degrades to an EMPTY set and the
  sweep runs (the "run safely without it" fix, per the ask);
- an unresolved exemption on a row that DOES declare renames, or whose
  declaration could not even be checked, still cannot sweep safely -- it
  returns `None` so the caller fails closed, but (per
  `_candidate_top_level_orphans` below and `process_target`'s own WARNING
  print) never in silence.

`_candidate_top_level_orphans` is the read-only preview `process_target`
prints by name in that fail-closed case -- pure filesystem listing, no git
spawn, no delete.

Run: python -m pytest coordinator/bin/tests/test_publish_orphan_sweep_never_skips_silently.py -n 4
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parents[1]


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_orphan_sweep_never_skips_silently_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()
resolve_effective = publish.resolve_effective_renamed_file_names


def test_already_resolved_exemption_passes_through_unchanged():
    for resolved in (frozenset(), frozenset({"renamed.txt"})):
        assert resolve_effective(
            renamed_file_names=resolved,
            basename_rename_lookup_ok=False,
            declares_basename_rename=True,
        ) is resolved


def test_none_with_confirmed_no_declared_rename_degrades_to_empty_set():
    result = resolve_effective(
        renamed_file_names=None,
        basename_rename_lookup_ok=True,
        declares_basename_rename=False,
    )
    assert result == frozenset()
    assert result is not None


def test_none_with_declared_rename_stays_none_fail_closed():
    assert resolve_effective(
        renamed_file_names=None,
        basename_rename_lookup_ok=True,
        declares_basename_rename=True,
    ) is None


def test_none_with_failed_lookup_stays_none_fail_closed():
    assert resolve_effective(
        renamed_file_names=None,
        basename_rename_lookup_ok=False,
        declares_basename_rename=False,
    ) is None


def _stub_publish_sync_module():
    stub = types.ModuleType("stub_publish_sync_for_candidate_orphans")
    stub.load_ignore = lambda path: _NullIgnoreMatcher()
    stub._archived_or_orphan = lambda rel_path: False
    return stub


class _NullIgnoreMatcher:
    def matches(self, rel_path: str) -> bool:
        return False


def test_candidate_top_level_orphans_names_a_file_dropped_from_source(tmp_path):
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    (dst_dir / "stale-flat-file.md").write_text("stale")
    (dst_dir / "still-current.md").write_text("current")
    (src_dir / "still-current.md").write_text("current")

    result = publish._candidate_top_level_orphans(
        _stub_publish_sync_module(), src_dir, dst_dir
    )

    assert result == ["stale-flat-file.md"]


def test_candidate_top_level_orphans_skips_dotfiles_and_ignored(tmp_path):
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    (dst_dir / ".hidden").write_text("x")

    stub = _stub_publish_sync_module()

    class _AllMatch:
        def matches(self, rel_path: str) -> bool:
            return True

    stub.load_ignore = lambda path: _AllMatch()
    (dst_dir / "ignored.md").write_text("x")

    result = publish._candidate_top_level_orphans(stub, src_dir, dst_dir)

    assert result == []


def test_candidate_top_level_orphans_empty_when_dest_missing(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    missing_dst = tmp_path / "does-not-exist"

    result = publish._candidate_top_level_orphans(
        _stub_publish_sync_module(), src_dir, missing_dst
    )

    assert result == []
