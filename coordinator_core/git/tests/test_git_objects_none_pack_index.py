"""A merged-table hit whose pack index slot is None (unreadable index) reads as absent, not AttributeError."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.git import git_objects as go


def _source(tmp_path: Path, slots: list) -> go._LookupSource:
    indexes = [(Path(f"pack-{i}.idx"), Path(f"pack-{i}.pack"), pidx) for i, pidx in enumerate(slots)]
    return go._LookupSource(tmp_path, indexes)


def test_merged_hit_on_none_pack_index_is_absent(tmp_path):
    target = bytes.fromhex("ab" * 20)
    src = _source(tmp_path, [None] * (go._UNCOVERED_LAZY_MAX + 1))
    src._probes = go._PROBES_BEFORE_MERGE
    src._merged = {target: 0}
    src._sid_pos = {0: 0}
    assert src.find("k", "ab" * 20, target) is None


def test_merged_hit_on_readable_pack_index_still_resolves(tmp_path):
    target = bytes.fromhex("ab" * 20)

    class _Pidx:
        offsets = [1234]

    slots: list = [None] * (go._UNCOVERED_LAZY_MAX + 1)
    slots[3] = _Pidx()
    src = _source(tmp_path, slots)
    src._probes = go._PROBES_BEFORE_MERGE
    src._merged = {target: 0}
    src._sid_pos = {0: 3}
    assert src.find("k", "ab" * 20, target) == (3, 1234)
