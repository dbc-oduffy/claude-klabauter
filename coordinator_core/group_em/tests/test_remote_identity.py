import pytest

from coordinator_core.group_em import remote_identity as ri


def test_peer_key_roundtrip():
    key = ri.peer_key("box-ab12", "sess-1")
    assert key == "box-ab12:sess-1"
    assert ri.parse_key(key) == ("box-ab12", "sess-1")


def test_bare_sid_is_local():
    assert ri.parse_key("sess-1", local_id="box-me") == ("box-me", "sess-1")


def test_session_id_containing_colon_splits_on_first():
    assert ri.parse_key("b1:a:b") == ("b1", "a:b")


def test_invalid_box_prefix_is_bare():
    assert ri.parse_key("a b:sid", local_id="L") == ("L", "a b:sid")


def test_rewrite_from_remote_sender_is_peer_key():
    line = {"from_box": "box-a", "from_session": "s1", "text": "x"}
    out = ri.rewrite_from(line, "box-me")
    assert out["from"] == "box-a:s1"
    assert "from" not in line


def test_rewrite_from_local_sender_stays_bare():
    out = ri.rewrite_from({"from_box": "box-me", "from_session": "s1"}, "box-me")
    assert out["from"] == "s1"


def test_rewrite_from_without_session_or_with_unsafe_box_has_no_from():
    assert "from" not in ri.rewrite_from({"from_box": "box-a"}, "box-me")
    assert "from" not in ri.rewrite_from({"from_box": "bad:box", "from_session": "s"}, "box-me")


def test_peer_key_refuses_unsafe_ids():
    with pytest.raises(ValueError):
        ri.peer_key("bad:box", "s")
    with pytest.raises(ValueError):
        ri.peer_key("box", "")
