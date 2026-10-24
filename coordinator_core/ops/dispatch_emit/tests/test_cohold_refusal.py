from coordinator_core.ops.dispatch_emit import cohold_refusal


def test_empty_is_none(tmp_path):
    assert cohold_refusal.refusal({}, "r1", tmp_path) is None


def test_two_holders_and_release_call(tmp_path):
    msg = cohold_refusal.refusal({"a/M.md": ["ask-1", "ask-2"]}, "r1", tmp_path)
    assert "`a/M.md` co-held by halted run ask-1; release:" in msg
    assert "`a/M.md` co-held by halted run ask-2; release:" in msg
    assert "dispatch.record_halted_run_landed {run_id: ask-1" in msg
    assert "dispatch.record_halted_run_landed {run_id: ask-2" in msg
