"""bounded_read refuses a file over its size cap with OVERSIZE and still reads one at the cap."""

from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr


def test_over_cap_is_oversize(tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"x" * 11)
    assert hrr.bounded_read(f, 1000, max_bytes=10) is hrr.OVERSIZE


def test_at_cap_is_read(tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"x" * 10)
    assert hrr.bounded_read(f, 1000, max_bytes=10) == b"x" * 10


def test_default_cap_reads_small_file_and_missing_stays_missing(tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"abc")
    assert hrr.bounded_read(f, 1000) == b"abc"
    assert hrr.bounded_read(tmp_path / "nope", 1000) is hrr.MISSING


def test_oversize_does_not_read_the_body(tmp_path, monkeypatch):
    f = tmp_path / "f.bin"
    f.write_bytes(b"x" * 50)

    def boom(p):
        raise AssertionError("body read despite oversize")

    monkeypatch.setattr(hrr, "_read_file", boom)
    assert hrr.bounded_read(f, 1000, max_bytes=5) is hrr.OVERSIZE
