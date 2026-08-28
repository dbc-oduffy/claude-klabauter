"""Tests for `coordinator_core.op_census.cross_repo_consumers` — AC1-AC3.

Runs against a temp-dir memo corpus, never the live `cross-repo/` tree, so
this suite does not move when a memo is filed or archived.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.op_census import cross_repo_consumers as crc
from coordinator_core.ops import _registry_map


def _write(root: Path, box: str, name: str, text: str) -> Path:
    box_dir = root / "cross-repo" / box
    box_dir.mkdir(parents=True, exist_ok=True)
    memo_path = box_dir / name
    memo_path.write_text(text, encoding="utf-8")
    return memo_path


def test_matches_dotted_op_name_in_inbox(tmp_path: Path) -> None:
    _write(tmp_path, "inbox", "one.md", "This memo names `records.history` directly.")

    hits = crc.scan_cross_repo_consumers(["records.history"], repo_root=tmp_path)

    assert len(hits["records.history"]) == 1
    hit = hits["records.history"][0]
    assert hit.memo_path == "cross-repo/inbox/one.md"
    assert hit.box == "inbox"
    assert hit.shape == crc.SHAPE_OP_NAME


def test_matches_module_path_shape(tmp_path: Path) -> None:
    module = _registry_map.OP_MODULE_MAP["records.history"]
    module_path = module.replace(".", "/") + ".py"
    _write(tmp_path, "inbox", "two.md", f"See `{module_path}` for the caller.")

    hits = crc.scan_cross_repo_consumers(["records.history"], repo_root=tmp_path)

    assert len(hits["records.history"]) == 1
    assert hits["records.history"][0].shape == crc.SHAPE_MODULE_PATH


def test_op_module_map_none_value_contributes_only_dotted_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2's None arm is a test case, not a defensive branch: a fake op name
    mapped to `None` must still match on its dotted name and must not raise
    while deriving the (absent) module-path shape."""
    monkeypatch.setitem(_registry_map.OP_MODULE_MAP, "fake.none_mapped_op", None)
    _write(tmp_path, "inbox", "three.md", "This memo names `fake.none_mapped_op`.")

    hits = crc.scan_cross_repo_consumers(["fake.none_mapped_op"], repo_root=tmp_path)

    assert len(hits["fake.none_mapped_op"]) == 1
    assert hits["fake.none_mapped_op"][0].shape == crc.SHAPE_OP_NAME


def test_archive_not_scanned_by_default(tmp_path: Path) -> None:
    _write(tmp_path, "archive", "archived.md", "This memo names `records.history`.")

    hits = crc.scan_cross_repo_consumers(["records.history"], repo_root=tmp_path)

    assert hits["records.history"] == []


def test_archive_scanned_when_requested(tmp_path: Path) -> None:
    _write(tmp_path, "archive", "archived.md", "This memo names `records.history`.")

    hits = crc.scan_cross_repo_consumers(
        ["records.history"], include_archive=True, repo_root=tmp_path
    )

    assert len(hits["records.history"]) == 1
    assert hits["records.history"][0].box == "archive"


def test_missing_cross_repo_directory_returns_empty_result(tmp_path: Path) -> None:
    hits = crc.scan_cross_repo_consumers(["records.history", "ping"], repo_root=tmp_path)

    assert hits == {"records.history": [], "ping": []}


def test_no_match_yields_empty_list(tmp_path: Path) -> None:
    _write(tmp_path, "inbox", "unrelated.md", "Nothing here names any op.")

    hits = crc.scan_cross_repo_consumers(["records.history"], repo_root=tmp_path)

    assert hits["records.history"] == []


def test_one_pass_tests_every_name_against_each_memo(tmp_path: Path) -> None:
    """A single memo naming two of three requested ops must be read once and
    still contribute a hit to each name it names."""
    _write(
        tmp_path,
        "inbox",
        "multi.md",
        "This memo names `records.history` and also `ping`.",
    )

    hits = crc.scan_cross_repo_consumers(
        ["records.history", "ping", "cutover.gate"], repo_root=tmp_path
    )

    assert len(hits["records.history"]) == 1
    assert len(hits["ping"]) == 1
    assert hits["cutover.gate"] == []


def test_read_count_equals_memo_count_not_memo_times_name_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard for the one-pass shape itself, not just its outcome:
    `test_one_pass_tests_every_name_against_each_memo` verifies correct hits,
    but a per-name-outer loop that reads each memo once per requested name
    (O(memos x names) reads) would produce the same hits and still pass it.
    This test counts actual `Path.read_text` calls against the corpus and
    asserts that count equals the number of memos, independent of how many
    op names are requested — it goes red the moment the loop nesting is
    inverted, even though the hit outcome would stay correct."""
    _write(tmp_path, "inbox", "one.md", "This memo names `records.history`.")
    _write(tmp_path, "inbox", "two.md", "This memo names `ping` and `records.history`.")
    _write(tmp_path, "inbox", "three.md", "Nothing here names any op.")

    read_calls: list[Path] = []
    original_read_text = Path.read_text

    def counting_read_text(self: Path, *args, **kwargs):
        if self.suffix == ".md" and "cross-repo" in self.parts:
            read_calls.append(self)
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    crc.scan_cross_repo_consumers(
        ["records.history", "ping", "cutover.gate"], repo_root=tmp_path
    )

    assert len(read_calls) == 3
