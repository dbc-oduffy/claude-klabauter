"""The minted-start relay schema accepts the start op's reply verbatim, including `refusal: null`."""

from __future__ import annotations

import json
import re

import jsonschema
import pytest

from coordinator_core.hooks import submit_engine_ask

pytestmark = pytest.mark.complete

_REPLY = {
    "ok": True,
    "script_path": "scratch/warp/warp-x-20261128T162751-b42d47.workflow.mjs",
    "run_script": "scratch/warp/warp-x-20261128T162751-b42d47.workflow.mjs",
    "fire_args": {"repoRoot": "D:/r", "runToken": "t"},
    "refusal": None,
}


def _start_schema() -> dict:
    body = re.search(r"const START_SCHEMA = (\{.*?\n\});", submit_engine_ask._MINTED_START_JS, re.S)
    assert body is not None
    text = re.sub(r"([{,]\s*)(\w+):", r'\1"\2":', body.group(1).replace("'", '"'))
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", text))


def test_the_start_ops_null_refusal_reply_validates() -> None:
    jsonschema.validate(_REPLY, _start_schema())


def test_a_null_run_script_validates() -> None:
    jsonschema.validate({**_REPLY, "run_script": None}, _start_schema())


def test_a_refusal_string_still_validates_and_ok_stays_required() -> None:
    schema = _start_schema()
    jsonschema.validate({"ok": False, "script_path": "", "refusal": "denied"}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"script_path": ""}, schema)
