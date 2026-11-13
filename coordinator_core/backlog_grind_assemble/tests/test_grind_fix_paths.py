from __future__ import annotations

import json

import pytest

from coordinator_core.backlog_grind_assemble import grind_fix_paths, grind_rows


def test_write_read_roundtrip_dedupes_and_normalizes(tmp_path):
    grind_fix_paths.write(tmp_path, "r1", ["a/b.md", "a\\b.md", "c.md"], ["n.md"], [])
    assert grind_fix_paths.read(tmp_path, "r1") == {"touched": ["a/b.md", "c.md"], "created": ["n.md"], "removed": []}
    assert not list((tmp_path / "fix-paths").glob("*.tmp*"))


def test_read_absent_is_empty_and_corrupt_refuses(tmp_path):
    assert grind_fix_paths.read(tmp_path, "none") == {"touched": [], "created": [], "removed": []}
    (tmp_path / "fix-paths").mkdir()
    (tmp_path / "fix-paths" / "bad.json").write_text("{nope", encoding="utf-8")
    with pytest.raises(ValueError):
        grind_fix_paths.read(tmp_path, "bad")


@pytest.mark.parametrize("row_id", ["../x", "a/b", "..", ""])
def test_escaping_row_id_refused(tmp_path, row_id):
    with pytest.raises(ValueError):
        grind_fix_paths.write(tmp_path, row_id, [], [], [])


def test_verb_writes_record(tmp_path, capsys):
    payload = json.dumps({"touched": ["x/README.md"], "created": [], "removed": ["y.md"]})
    code = grind_rows.main(["record-fix-paths", "--run-dir", str(tmp_path), "--row-id", "r2", "--paths-json", payload])
    assert code == grind_rows.EXIT_OK
    assert grind_fix_paths.read(tmp_path, "r2")["touched"] == ["x/README.md"]
    assert grind_fix_paths.read(tmp_path, "r2")["removed"] == ["y.md"]


def test_verb_refuses_bad_json_and_missing_flags(tmp_path):
    assert grind_rows.main(["record-fix-paths", "--run-dir", str(tmp_path), "--row-id", "r", "--paths-json", "[1]"]) == grind_rows.EXIT_REFUSAL
    assert grind_rows.main(["record-fix-paths", "--run-dir", str(tmp_path), "--row-id", "r", "--paths-json", "{bad"]) == grind_rows.EXIT_REFUSAL
    assert grind_rows.main(["record-fix-paths", "--run-dir", str(tmp_path)]) == grind_rows.EXIT_USAGE
    assert not (tmp_path / "fix-paths").exists()
