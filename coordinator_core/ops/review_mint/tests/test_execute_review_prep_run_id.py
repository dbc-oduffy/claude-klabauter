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


def test_wave_prep_instruction_refuses_run_id_with_paths_literal():
    from coordinator_core.ops.review_mint.execute_review import _wave_prep_instruction

    with pytest.raises(ValueError):
        _wave_prep_instruction("m", "b", "p", None, grouped=False, paths_literal="[]", run_id="r")
