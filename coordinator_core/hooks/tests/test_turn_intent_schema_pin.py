"""Pins turn-intent record schema 1: the frozen keys the writer must keep emitting (additions are allowed)."""

import json
import os
import uuid

from coordinator_core.hooks.support import turn_intent


def test_written_record_keeps_the_schema_1_keys(tmp_path):
    session_id = str(uuid.uuid4())
    directory = turn_intent.session_dir(str(tmp_path), session_id)
    assert turn_intent.write_record(directory, session_id, True, "ok?", "marker", 123.5)
    with open(os.path.join(directory, turn_intent.RECORD_NAME), encoding="utf-8") as fh:
        record = json.load(fh)
    assert {"schema", "session_id", "needs_pm", "ask", "source", "turn_ended_at"} <= set(record)
    assert record["schema"] == 1
    assert record["turn_ended_at"] == 123.5
