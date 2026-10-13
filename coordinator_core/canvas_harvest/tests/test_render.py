"""Tests for canvas_harvest.render: the SETTLED.md skeleton."""

from coordinator_core.canvas_harvest import model, render


def _lifted(threads):
    comments = None if threads is None else model.CommentsRead(threads=threads, rendered_text="", read_at="t")
    return model.Lifted(None, comments, None, None, None, None)


def test_skeleton_with_threads_has_heading_and_no_list_item():
    text = render.settled_skeleton(["t1", "t2"])
    assert text.startswith(render.HEADING + "\n")
    assert "t1" in text and "t2" in text
    assert "[thread:<id>]" not in text.split("-->")[1]
    for line in text.splitlines():
        assert not line.startswith(("- ", "* "))


def test_skeleton_without_threads_is_only_the_no_threads_item():
    assert render.settled_skeleton([]) == f"{render.HEADING}\n\n- [no-threads]\n"
    lines = [ln for ln in render.settled_skeleton([]).splitlines() if ln.startswith("- ")]
    assert lines == ["- [no-threads]"]


def test_write_is_byte_identical_on_second_call(tmp_path):
    assert render.write_settled_skeleton(_lifted([{"id": "a"}]), tmp_path) is True
    target = tmp_path / render.SETTLED_NAME
    target.write_bytes(target.read_bytes() + b"- [thread:a] decided\n")
    before = target.read_bytes()
    assert render.write_settled_skeleton(_lifted([{"id": "a"}, {"id": "b"}]), tmp_path) is False
    assert target.read_bytes() == before


def test_no_comments_read_writes_no_threads_item(tmp_path):
    render.write_settled_skeleton(_lifted(None), tmp_path)
    assert (tmp_path / render.SETTLED_NAME).read_text() == render.settled_skeleton([])
