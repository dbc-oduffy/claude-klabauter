"""own_carry.own_carry_params: pure follow-on commit params."""
import pytest

from coordinator_core.ops.dispatch_emit import own_carry

SHA = "a" * 40


def test_empty_left_is_none():
    assert own_carry.own_carry_params([], [], SHA, "s1") is None


def test_all_contested_is_none():
    assert own_carry.own_carry_params(["a/x.yaml"], {"a/x.yaml"}, SHA, "s1") is None


def test_mixed_keeps_uncontested_sorted():
    p = own_carry.own_carry_params(["b.yaml", "a.yaml", "c.yaml"], ["b.yaml"], SHA, "s1")
    assert p["paths"] == ["a.yaml", "c.yaml"]
    assert p["carry_own_left_behind"] is False
    assert p["carry_stopped_runs"] is False


def test_backslash_paths_normalised_both_sides():
    p = own_carry.own_carry_params(["d\\x.yaml", "d\\y.yaml"], ["d/x.yaml"], SHA, "s1")
    assert p["paths"] == ["d/y.yaml"]


def test_message_trailer_exact():
    p = own_carry.own_carry_params(["a"], [], SHA, "sess-9")
    assert p["message"] == (
        "chore(own-left-behind): land this session's own claimed writes\n\n"
        f"Own-Left-Behind-Of: {SHA}\nSession-Id: sess-9"
    )
    assert own_carry.TRAILER == "Own-Left-Behind-Of"
    assert own_carry.REPLY_KEY == "own_left_behind_commit"


@pytest.mark.parametrize("sha", ["", "abc", "g" * 40, "a" * 41])
def test_bad_sha_refused(sha):
    with pytest.raises(ValueError):
        own_carry.own_carry_params(["a"], [], sha, "s1")


def test_empty_session_refused():
    with pytest.raises(ValueError):
        own_carry.own_carry_params(["a"], [], SHA, "")
