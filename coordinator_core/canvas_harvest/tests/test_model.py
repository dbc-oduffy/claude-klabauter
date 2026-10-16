"""Tests for canvas_harvest.model: serializer, path check, marker table, id parsing, thread helpers."""

import pytest

from coordinator_core.canvas_harvest import model

NOT_FOUND_TEXT = (
    "artifact not found — it may have been deleted, or it has not been shared with you. "
    "Check the id and ask the owner to share it."
)
NEEDS_APPROVAL_TEXT = (
    'Nothing was done. This needs a yes to: "Orchard Ledger" was made by someone else. '
    "Let Claude read it? No one can answer in this session."
)


def test_dump_json_bytes_and_key_order():
    out = model.dump_json({"b": 1, "a": ["é", {"z": None}]})
    assert out == '{\n  "b": 1,\n  "a": [\n    "é",\n    {\n      "z": null\n    }\n  ]\n}\n'.encode("utf-8")
    assert b"\r" not in out


@pytest.mark.parametrize("name", ["/abs", "../x", "a/../../x", "", "a//b", "a:b", "a\\b", "CON", "nul.txt", "COM1", "x.", "x ", "a\x01b", "C:x"])
def test_safe_child_refuses(tmp_path, name):
    with pytest.raises(model.HarvestError) as exc:
        model.safe_child(tmp_path, name)
    assert exc.value.reason == model.UNSAFE_PATH


def test_safe_child_accepts_nested(tmp_path):
    assert model.safe_child(tmp_path, "frames/a/b.html") == tmp_path / "frames/a/b.html"
    assert model.safe_child(tmp_path, "threads/3b1f-6a52.json") == tmp_path / "threads/3b1f-6a52.json"


def test_safe_child_refuses_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    base = tmp_path / "base"
    base.mkdir()
    (base / "link").symlink_to(outside)
    with pytest.raises(model.HarvestError) as exc:
        model.safe_child(base, "link/x")
    assert exc.value.reason == model.UNSAFE_PATH


def test_markers_over_fixture_texts():
    assert model.classify_error(NOT_FOUND_TEXT) == model.NOT_FOUND
    assert model.classify_error("Error: " + NOT_FOUND_TEXT) == model.NOT_FOUND
    assert model.classify_error(NEEDS_APPROVAL_TEXT) == model.NEEDS_APPROVAL
    assert model.classify_error("You haven't viewed the latest version") is None
    assert model.classify_error("") is None


def test_artifact_id_from_url():
    assert model.artifact_id_from_url("https://claude.ai/artifact/Qz7LmPx2NcVt9RkHdYw3Fe") == "Qz7LmPx2NcVt9RkHdYw3Fe"
    assert model.artifact_id_from_url("claude.ai/artifact/Qz7LmPx2NcVt9RkHdYw3Fe?x=1") == "Qz7LmPx2NcVt9RkHdYw3Fe"
    assert model.artifact_id_from_url("Qz7LmPx2NcVt9RkHdYw3Fe") == "Qz7LmPx2NcVt9RkHdYw3Fe"
    assert model.artifact_id_from_url("https://example.com/page") is None
    assert model.artifact_id_from_url("") is None


THREADS = [
    {"id": "t1", "comments": [{"created_at": "2026-10-02T09:14:05Z"}]},
    {"id": "t2", "comments": [{"created_at": "2026-10-03T17:00:00+02:00"}, {"created_at": "2026-10-03T15:30:42Z"}]},
]


def test_thread_ids_and_latest_comment_at():
    assert model.thread_ids(THREADS) == ["t1", "t2"]
    assert model.latest_comment_at(THREADS) == "2026-10-03T15:30:42Z"
    assert model.latest_comment_at([{"id": "x", "comments": []}]) is None
    assert model.latest_comment_at([]) is None


@pytest.mark.parametrize(
    "path,expected",
    [("project/a.html", True), ("project/", False), ("project", False), ("xproject/a", False), ("app.js", False), (None, False)],
)
def test_is_project_file(path, expected):
    assert model.is_project_file(path) is expected


def test_record_file_ceiling():
    assert model.MAX_RECORD_FILES == 48
