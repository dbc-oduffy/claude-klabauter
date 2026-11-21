"""One commit_paths pass opens each attribute file once, however many CR-bearing text-pinned paths it lands."""

from __future__ import annotations


import pytest

from coordinator_core.git import call_io_scope, checkin_attrs

pytestmark = pytest.mark.complete


def test_dispositions_unchanged_inside_a_scope(tmp_path):
    root = tmp_path / "r"
    (root / ".git").mkdir(parents=True)
    (root / "sub").mkdir()
    (root / ".gitattributes").write_text("* text=auto\n*.bin -text\n", newline="\n")
    (root / "sub" / ".gitattributes").write_text("*.cmd eol=crlf\n", newline="\n")
    cases = {"a.txt": checkin_attrs.TEXT, "a.bin": checkin_attrs.BINARY, "sub/x.cmd": checkin_attrs.TEXT}
    bare = {p: checkin_attrs.checkin_disposition(root, p) for p in cases}
    with call_io_scope.call_io_scope():
        scoped = {p: checkin_attrs.checkin_disposition(root, p) for p in cases}
        pin = checkin_attrs.resolve_text_pin(root, "sub/x.cmd")
    assert bare == scoped == cases
    assert pin == checkin_attrs.TextPin(checkin_attrs.TEXT_AUTO, "crlf")


@pytest.mark.parametrize(
    "attrs",
    [
        "* text=auto\n",
        "*.png filter=lfs diff=lfs merge=lfs -text\n",
        "*.txt filter=lfs\n",
        "[attr]lfs filter=lfs -text\n*.bin lfs\n",
        "sub/*.dat filter=x\n",
    ],
)
@pytest.mark.parametrize("rel", ["a.txt", "b.png", "sub/c.dat", "sub/deep/d.txt"])
def test_clean_filter_may_apply_matches_content_hash(tmp_path, attrs, rel):
    from coordinator_core.git import content_hash

    root = tmp_path / "r"
    (root / ".git").mkdir(parents=True)
    (root / "sub" / "deep").mkdir(parents=True)
    (root / ".gitattributes").write_text(attrs, newline="\n")
    (root / "sub" / ".gitattributes").write_text("*.txt filter=y\n", newline="\n")
    want = content_hash._clean_filter_may_apply(root, rel)
    assert checkin_attrs.clean_filter_may_apply(root, rel) == want
    with call_io_scope.call_io_scope():
        assert checkin_attrs.clean_filter_may_apply(root, rel) == want
