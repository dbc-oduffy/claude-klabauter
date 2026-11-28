from __future__ import annotations

from coordinator_core.warm import server


def test_refusal_message_carries_the_holders_the_door_would_drop():
    env = server._pool_saturated_envelope(
        {"id": 1, "method": "dispatch.ask_stage"},
        ["plan.match_candidates"] * 2 + ["review.reverify"],
        "C:/x/stall-dumps",
        hung=["plan.match_candidates"] * 2,
        io_wait=["review.reverify"],
    )
    msg = env["error"]["message"]
    assert "hung: plan.match_candidates x2" in msg
    assert "io-wait: review.reverify" in msg
    assert "stall_dir=C:/x/stall-dumps" in msg


def test_message_without_holder_classes_falls_back_to_holders():
    env = server._pool_saturated_envelope({"id": 1, "method": "x"}, ["a", "b"], None)
    assert "holders: a, b" in env["error"]["message"]
