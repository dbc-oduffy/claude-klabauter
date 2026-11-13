"""Already-covered is a first-stage fact: triage retires it, fix and the post-fix verify can report it as `covered_by`."""
import json
import re

from coordinator_core.ops.dispatch_emit import grind_stages as stages

_FIX_OUTCOMES = ["done", "NEEDS_WIDER_SCOPE", "PEER_DIRTY", "NOT_REPRODUCED", "NEEDS_PLAN", "MANIFEST_STALE"]


def _schema(call: str) -> dict:
    return json.loads(re.search(r"schema: (\{.*\}) \}\)", call, re.DOTALL).group(1))


def _prompt(call: str) -> str:
    """The call's prompt text with the JS string escapes of an apostrophe undone."""
    return call.replace("\\'", "'")


def _triage(**kw):
    return stages.compose_triage_call(
        label="t", phase_title="Grind", run_dir="state/queue-grind/p/run-1", profile="lessons",
        verdicts=("discard", "script-edit"), **kw,
    )


def _fix(**kw):
    return stages.compose_fix_call(label="f", phase_title="Grind", row_id="r0", profile="lessons", **kw)


def _verify(**kw):
    return stages.compose_verify_agent_call(
        label="v", phase_title="Grind", row_id_js="row.rowId", row_path_js="row.path",
        touched_files_js="row.touchedFiles", evidence_js="row.evidence", fix_plan_js="row.fixPlan",
        profile="lessons", run_id_js="RUN_ID", digest_js="row.digest", **kw,
    )


def test_triage_retires_a_row_whose_rule_is_already_at_head_never_fixes_it():
    for targeted in (False, True):
        call = _triage(targeted_runner=targeted)
        assert "already stated at HEAD gets a retire verdict (`discard`), never a fix verdict" in call
        assert "cite the covering `path:line` in `evidence`" in call


def test_fix_reply_carries_an_optional_covered_by_and_the_outcome_enum_is_unchanged():
    schema = _schema(_fix())
    assert schema["properties"]["covered_by"] == {"type": "string"}
    assert "covered_by" not in schema["required"]
    assert schema["properties"]["outcome"]["enum"] == _FIX_OUTCOMES


def test_fix_prompt_tells_the_agent_to_edit_nothing_when_the_rule_is_already_stated():
    call = _prompt(_fix())
    assert "if the row's rule is already stated at HEAD, edit nothing" in call
    assert "`touched_files`, and `covered_by` as that artifact's `path:line`" in call
    assert call.index("edit nothing") < call.index("Otherwise, fix the row")


def test_post_fix_verify_reports_covered_by_and_never_counts_the_fix_own_edit_as_coverage():
    call = _verify()
    assert _schema(call)["properties"]["covered_by"] == {"type": "string"}
    assert "set `covered_by` to that artifact as `path:line`" in call
    assert "never a file this row's fix touched or created" in _prompt(call)


def test_routing_verify_keeps_covered_by_and_does_not_gain_the_post_fix_sentence():
    call = _verify(routing=True)
    assert _schema(call)["properties"]["covered_by"] == {"type": "string"}
    assert "never a file this row's fix touched or created" not in _prompt(call)


def test_routing_verify_names_covered_anywhere_and_right_home_with_a_schema_field():
    call = _verify(routing=True)
    assert _schema(call)["properties"]["right_home"] == {"type": "string"}
    assert "right_home" not in _schema(call)["required"]
    prompt = _prompt(call)
    assert "if ANY artifact at HEAD already states the rule, even one outside the declared target" in prompt
    assert "set `right_home` to the correct file path" in prompt


def test_right_home_is_routing_only_never_on_retire_or_post_fix_verify():
    assert "right_home" not in _schema(_verify())["properties"]
    assert "right_home" not in _schema(_verify(routing=True, retire=True))["properties"]
    assert "right_home" not in _prompt(_verify())


def test_triage_forbids_the_row_own_file_as_a_target():
    for targeted in (False, True):
        assert "Never declare the row's own file" in _prompt(_triage(targeted_runner=targeted))
