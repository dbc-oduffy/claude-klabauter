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


SHARE = ".coordinator-local/subagent-share/"


def test_machine_local_paths_dropped():
    left = [f"{SHARE}r/{i}.md" for i in range(7000)] + ["a.py", "b.py"]
    assert own_carry.own_carry_params(left, [], SHA, "s1")["paths"] == ["a.py", "b.py"]


def test_backslash_machine_local_dropped():
    left = [".coordinator-local\\subagent-share\\r\\x.md", "a.py"]
    assert own_carry.own_carry_params(left, [], SHA, "s1")["paths"] == ["a.py"]


def test_all_ignored_is_none():
    assert own_carry.own_carry_params(["a", "b"], [], SHA, "s1", ignored=["a", "b"]) is None


def test_ignored_paths_dropped_and_attested():
    p = own_carry.own_carry_params(["a", "b"], [], SHA, "s1", ignored=["a"])
    assert p["paths"] == ["b"]
    assert p["ignored_untracked"] == []


def test_ignored_none_omits_key_and_keeps_paths():
    p = own_carry.own_carry_params(["a", "b"], [], SHA, "s1")
    assert "ignored_untracked" not in p
    assert p["paths"] == ["a", "b"]


def test_empty_session_refused():
    with pytest.raises(ValueError):
        own_carry.own_carry_params(["a"], [], SHA, "")
