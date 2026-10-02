from coordinator_core.write_guards import _post_write_body as m


def test_write_returns_content_or_none():
    assert m.post_write_body("Write", {"content": "x"}, None) == "x"
    assert m.post_write_body("Write", {}, None) is None


def test_edit_first_vs_replace_all():
    ti = {"old_string": "a", "new_string": "b"}
    assert m.post_write_body("Edit", ti, "aaa") == "baa"
    assert m.post_write_body("Edit", {**ti, "replace_all": True}, "aaa") == "bbb"
    assert m.post_write_body("Edit", ti, "zzz") is None
    assert m.post_write_body("Edit", ti, None) is None


def test_multiedit_sequential_and_stale_modes():
    edits = [
        {"old_string": "a", "new_string": "b"},
        {"old_string": "gone", "new_string": "x"},
        {"old_string": "b", "new_string": "c"},
    ]
    assert m.post_write_body("MultiEdit", {"edits": edits}, "a") is None
    assert m.post_write_body("MultiEdit", {"edits": edits}, "a", skip_stale=True) == "c"
    assert m.post_write_body("MultiEdit", {"edits": []}, "a") is None


def test_read_pre_image_cap_and_missing(tmp_path):
    ok = tmp_path / "ok.md"
    ok.write_text("hi")
    assert m.read_pre_image(ok) == "hi"
    big = tmp_path / "big.md"
    big.write_text("a" * (m.MAX_WHOLE_FILE_BYTES + 1))
    assert m.read_pre_image(big) is None
    assert m.read_pre_image(tmp_path / "nope.md") is None
