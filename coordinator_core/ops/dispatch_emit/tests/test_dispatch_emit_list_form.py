"""dispatch.emit list form: `emits` runs each single-form element in order and collects `replies`."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import op


def test_list_form_returns_one_reply_per_element_in_order(tmp_path):
    (tmp_path / ".git").mkdir()
    out = op._dispatch_emit(
        {"emits": [{"abandon_run": "ask-a"}, {"abandon_run": "ask-b"}], "_caller_cwd": str(tmp_path)}, None
    )
    assert [r["run_id"] for r in out["replies"]] == ["ask-a", "ask-b"]
    assert all(r["abandoned"] is False for r in out["replies"])


def test_failing_element_lands_as_error_slot_and_rest_still_run(tmp_path):
    (tmp_path / ".git").mkdir()
    out = op._dispatch_emit(
        {
            "emits": [{"abandon_run": "ask-a"}, {"bogus_key": 1}, {"abandon_run": "ask-c"}],
            "_caller_cwd": str(tmp_path),
        },
        None,
    )
    replies = out["replies"]
    assert replies[0]["run_id"] == "ask-a"
    assert "bogus_key" in replies[1]["error"]
    assert replies[2]["run_id"] == "ask-c"


def test_raising_element_lands_as_error_string():
    out = op._dispatch_emit({"emits": [{"abandon_run": "ask-a"}]}, None)
    assert isinstance(out["replies"][0]["error"], str) and out["replies"][0]["error"]


def test_emits_with_single_form_key_is_refused():
    out = op._dispatch_emit({"emits": [], "plan_path": "p.md"}, None)
    assert "plan_path" in out["error"] and "replies" not in out


def test_emits_is_an_accepted_param_and_non_list_is_refused():
    assert op._unknown_param_refusal({"emits": []}) is None
    assert "list" in op._dispatch_emit({"emits": "x"}, None)["error"]


def test_non_object_and_nested_elements_are_error_slots():
    out = op._dispatch_emit({"emits": ["x", {"emits": []}]}, None)
    assert all(isinstance(r["error"], str) for r in out["replies"])


def test_single_form_unchanged_and_empty_list_yields_empty_replies(tmp_path):
    assert op._dispatch_emit({"emits": []}, None) == {"replies": []}
    assert "params.nope is not accepted" in op._dispatch_emit({"nope": 1}, None)["error"]
