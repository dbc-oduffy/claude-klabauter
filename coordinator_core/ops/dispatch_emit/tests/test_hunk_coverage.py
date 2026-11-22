"""split_landed_hunks: per-path landed/left hunk count from blob bytes."""

from coordinator_core.ops.dispatch_emit.hunk_coverage import split_landed_hunks

BASE = b"".join(b"line%d\n" % i for i in range(1, 21))


def _edit(src: bytes, changes: dict) -> bytes:
    lines = src.splitlines(keepends=True)
    for idx, new in changes.items():
        lines[idx - 1] = new
    return b"".join(lines)


def test_full_landing_has_nothing_left():
    measured = _edit(BASE, {2: b"A\n", 18: b"B\n"})
    assert split_landed_hunks(BASE, measured, measured) == {"landed": 2, "left": 0, "left_hunks": []}


def test_half_landing_counts_one_left_with_measured_span():
    measured = _edit(BASE, {2: b"A\n", 18: b"B\n"})
    committed = _edit(BASE, {2: b"A\n"})
    out = split_landed_hunks(BASE, measured, committed)
    assert out == {"landed": 1, "left": 1, "left_hunks": ["18,1"]}


def test_half_landing_other_half_and_peer_edit_elsewhere():
    measured = _edit(BASE, {2: b"A\n", 18: b"B\n"})
    committed = _edit(BASE, {18: b"B\n", 10: b"PEER\n"})
    out = split_landed_hunks(BASE, measured, committed)
    assert out == {"landed": 1, "left": 1, "left_hunks": ["2,1"]}


def test_none_landed():
    measured = _edit(BASE, {2: b"A\n", 18: b"B\n"})
    out = split_landed_hunks(BASE, measured, BASE)
    assert out["landed"] == 0 and out["left"] == 2
    assert out["left_hunks"] == ["2,1", "18,1"]


def test_crlf_bytes():
    base = BASE.replace(b"\n", b"\r\n")
    measured = _edit(base, {2: b"A\r\n", 18: b"B\r\n"})
    committed = _edit(base, {2: b"A\r\n"})
    out = split_landed_hunks(base, measured, committed)
    assert (out["landed"], out["left"], out["left_hunks"]) == (1, 1, ["18,1"])


def test_lf_commit_of_crlf_hunk_is_left():
    base = BASE.replace(b"\n", b"\r\n")
    measured = _edit(base, {2: b"A\r\n"})
    committed = _edit(base, {2: b"A\n"})
    assert split_landed_hunks(base, measured, committed)["left"] == 1


def test_path_new_at_base():
    measured = b"a\nb\nc\n"
    assert split_landed_hunks(b"", measured, measured) == {"landed": 1, "left": 0, "left_hunks": []}
    out = split_landed_hunks(b"", measured, b"")
    assert out == {"landed": 0, "left": 1, "left_hunks": ["1,3"]}


def test_pure_deletion_hunk():
    measured = b"".join(BASE.splitlines(keepends=True)[:4] + BASE.splitlines(keepends=True)[6:])
    assert split_landed_hunks(BASE, measured, measured)["left"] == 0
    out = split_landed_hunks(BASE, measured, BASE)
    assert out["landed"] == 0 and out["left"] == 1
