from coordinator_core.ops.review_mint import wave_not_run as w


def test_contract_constants():
    assert w.WAVE_NOT_RUN_VAR == "_waveNotRun"
    assert w.REJECTED == "rejected: "
    assert w.NO_REVIEW == "returned no review"
    assert w.SKIPPED == "skipped: "


def test_entry_statement_text():
    js = w.not_run_entry_js(
        "i", "coordinator:senior-front-end", "'rejected: ' + String(e && e.message || e)"
    )
    assert js == (
        "_waveNotRun.push({ slot: i, agent_type: 'coordinator:senior-front-end', "
        "reason: 'rejected: ' + String(e && e.message || e) });"
    )


def test_agent_type_is_escaped():
    js = w.not_run_entry_js("3", "a'b", "'x'")
    assert "agent_type: 'a\\'b'" in js
