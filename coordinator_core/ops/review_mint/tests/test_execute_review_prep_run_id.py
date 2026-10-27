"""compose_execute_review(prep_run_id=...): the wave prep command names the run, not the paths."""

import re

import pytest

from coordinator_core.ops.review_mint.execute_review import compose_execute_review
from coordinator_core.ops.review_mint.tests.test_execute_review import (
    _STAGE_SCHEMAS,
    _ratio_review,
)


def _compose(**extra):
    kwargs = dict(
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=["x.py"],
        executor_rows=9,
        prep_in_wave=True,
    )
    if extra.get("prep_run_id") is not None:
        kwargs["plan_path"] = "state/warp/r/manifest.json"
    kwargs.update(extra)
    return compose_execute_review(_ratio_review(), **kwargs)


def _prep_commands(phases):
    block = dict(phases)["Review wave"]
    return re.findall(r"review\.prep \\'\{.*?\}\\'", block)


def test_prep_run_id_replaces_the_path_list_in_every_prep_command():
    commands = _prep_commands(_compose(prep_run_id="ask-x"))
    assert commands
    for command in commands:
        assert '"run_id":"ask-x"' in command.replace('\\"', '"')
        assert "paths" not in command
    block = dict(_compose(prep_run_id="ask-x"))["Review wave"]
    assert "<every declared path>" not in block
    assert '"leg":true' in block.replace('\\"', '"')


def test_without_prep_run_id_the_explicit_path_placeholder_stays():
    block = dict(_compose())["Review wave"]
    assert "<every declared path>" in block
    assert "run_id" not in "".join(_prep_commands(_compose()))


def test_none_is_byte_identical_to_omitting_the_keyword():
    assert _compose(prep_run_id=None) == _compose()


def test_prep_run_id_may_equal_run_key():
    assert _compose(prep_run_id="ask-x", run_key="ask-x")


@pytest.mark.parametrize(
    "extra",
    [
        {"prep_in_wave": False},
        {"run_key": "other"},
    ],
)
def test_prep_run_id_guards_raise(extra):
    with pytest.raises(ValueError):
        _compose(prep_run_id="ask-x", **extra)


@pytest.mark.parametrize(
    "extra",
    [
        {"slice_identity_sha": "b" * 40},
        {"plan_path": "docs/plans/example.md"},
        {"slice_key_js": "x"},
    ],
)
def test_prep_run_id_refuses_inputs_that_fork_the_composer_id_from_the_op(extra):
    with pytest.raises(ValueError):
        _compose(prep_run_id="ask-x", **extra)


@pytest.mark.parametrize("base", [None, ""])
def test_prep_run_id_without_a_base_uses_the_nobase_prefix(base):
    from coordinator_core.ops.review_slice_id import run_prep_slice_prefix

    run = "ask-x"
    expected = run_prep_slice_prefix(run, base)
    assert expected == "manifest-ask-x-nobase-prep"
    block = dict(_compose(prep_run_id=run, run_key=run, run_base_sha=base))["Review wave"]
    assert expected in block


def test_prep_run_id_accepts_matching_slice_identity_sha():
    assert _compose(prep_run_id="ask-x", slice_identity_sha="a" * 40)


def test_prep_run_id_slice_id_is_the_canonical_run_prefix_and_unchanged():
    from coordinator_core.ops.review_slice_id import run_prep_slice_prefix

    run = "warp-x-20261026T133215-c4413e"
    legacy = f"manifest-{run}-{'a' * 12}-prep"
    assert run_prep_slice_prefix(run, "a" * 40) == legacy
    block = dict(_compose(prep_run_id=run, run_key=run))["Review wave"]
    assert legacy in block
    assert legacy not in "".join(_prep_commands(_compose(prep_run_id=run, run_key=run)))


def test_no_run_id_mode_prep_command_carries_slice_prefix():
    commands = _prep_commands(_compose(prep_run_id="ask-x"))
    assert commands
    assert all("slice_prefix" not in c for c in commands)
    assert all("slice_prefix" in c for c in _prep_commands(_compose()))


def test_run_id_leg_note_never_quotes_a_leg_value_line():
    from coordinator_core.ops.review_mint.execute_review import _wave_prep_instruction

    text = _wave_prep_instruction("m", "b", "p", None, grouped=False, run_id="r")
    assert "leg: true" not in text
    assert "only when the values below carry a `leg:` line" in text
    assert '"leg"' not in _wave_prep_instruction("m", "b", "p", None, grouped=False, paths_literal="[]")


def test_wave_prep_instruction_refuses_run_id_with_paths_literal():
    from coordinator_core.ops.review_mint.execute_review import _wave_prep_instruction

    with pytest.raises(ValueError):
        _wave_prep_instruction("m", "b", "p", None, grouped=False, paths_literal="[]", run_id="r")
