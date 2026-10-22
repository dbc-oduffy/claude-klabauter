"""apply_own_delta: the row's hunks land on HEAD, a peer's dirty hunks never ride along."""
from coordinator_core.git.own_delta_merge import apply_own_delta

PRE = b'"""Doc."""\n\ndef f():\n    return 1\n'


def test_pre_equals_head_returns_post():
    post = b'"""New doc."""\n\ndef f():\n    return 1\n'
    assert apply_own_delta(PRE, post, PRE) == post


def test_peer_hunk_elsewhere_absent_row_hunk_present():
    post = b'"""New doc."""\n\ndef f():\n    return 1\n'
    head = b'"""Doc."""\n\ndef f():\n    return 1\n\ndef g():\n    return 2\n'
    assert apply_own_delta(PRE, post, head) == (
        b'"""New doc."""\n\ndef f():\n    return 1\n\ndef g():\n    return 2\n'
    )


def test_incident_shape_peer_import_block_not_taken_from_post():
    pre = b'"""Doc."""\n\ndef f():\n    return 1\n'
    post = b'"""Doc."""\n\ndef f():\n    return 2\n'
    head = b'"""Doc."""\nfrom x import y\n\ndef f():\n    return 1\n'
    got = apply_own_delta(pre, post, head)
    assert got == b'"""Doc."""\nfrom x import y\n\ndef f():\n    return 2\n'


def test_overlapping_edit_is_conflict():
    post = b'"""Doc."""\n\ndef f():\n    return 2\n'
    head = b'"""Doc."""\n\ndef f():\n    return 3\n'
    assert apply_own_delta(PRE, post, head) is None


def test_same_insert_point_is_conflict():
    pre = b"a\nb\n"
    assert apply_own_delta(pre, b"a\nX\nb\n", b"a\nY\nb\n") is None


def test_identical_change_both_sides_counts_once():
    pre = b"a\nb\nc\nd\n"
    assert apply_own_delta(pre, b"a\nB\nc\nd\n", b"a\nB\nc\nD\n") == b"a\nB\nc\nD\n"


def test_crlf_preserved():
    pre = b"a\r\nb\r\nc\r\n"
    post = b"a\r\nB\r\nc\r\n"
    head = b"a\r\nb\r\nc\r\nd\r\n"
    assert apply_own_delta(pre, post, head) == b"a\r\nB\r\nc\r\nd\r\n"


def test_insertion_at_eof():
    pre = b"a\nb\n"
    post = b"a\nb\nc\n"
    head = b"Z\na\nb\n"
    assert apply_own_delta(pre, post, head) == b"Z\na\nb\nc\n"


def test_no_trailing_newline_round_trips():
    pre = b"a\nb"
    post = b"A\nb"
    head = b"a\nb"
    assert apply_own_delta(pre, post, head) == b"A\nb"
    assert apply_own_delta(pre, post, b"a\nb\nq") == b"A\nb\nq"


def test_deletion_only_delta():
    pre = b"a\nb\nc\n"
    post = b"a\nc\n"
    head = b"Z\na\nb\nc\n"
    assert apply_own_delta(pre, post, head) == b"Z\na\nc\n"
