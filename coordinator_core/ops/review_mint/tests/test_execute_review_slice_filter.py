"""compose_execute_review(slice_filter_js=...): a resumed review reviews only the filtered slices."""

from coordinator_core.ops.review_mint.execute_review import NO_SLICES_HALT, compose_execute_review
from coordinator_core.ops.review_mint.tests.test_execute_review import (
    _STAGE_SCHEMAS,
    _ratio_review,
)

_FILTER = "_resume.blocked_slices"


def _compose(prep_in_wave, **extra):
    return compose_execute_review(
        _ratio_review(),
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=["x.py"],
        executor_rows=9,
        prep_in_wave=prep_in_wave,
        **extra,
    )


def _joined(phases):
    return "\n".join(block for _, block in phases)


def test_none_is_byte_identical_on_both_prep_paths():
    for in_wave in (False, True):
        assert _compose(in_wave, slice_filter_js=None) == _compose(in_wave)
        assert _FILTER not in _joined(_compose(in_wave))


def test_separate_prep_narrows_slices_before_the_guards():
    text = _joined(_compose(False, slice_filter_js=_FILTER))
    narrow = f"_reviewPrep.slices = _reviewPrep.slices.filter(s => {_FILTER}.includes(s?.id))"
    assert narrow in text
    assert f"Array.isArray({_FILTER}) && {_FILTER}.length" in text
    assert f"_reviewPrep.slices.some(s => {_FILTER}.includes(s?.id))" in text  # no match -> review all, not a false NO_SLICES halt
    assert text.index("const _reviewPrep = await") < text.index(narrow) < text.index(NO_SLICES_HALT)
    assert text.index(narrow) < text.index("'whole-diff'")


def test_in_wave_prep_carries_ids_into_the_agent_prompt_and_caps_the_count():
    text = _joined(_compose(True, slice_filter_js=_FILTER))
    assert "review_slice_ids: " in text and f"JSON.stringify({_FILTER})" in text
    assert "Review only the listed slice ids of your group" in text
    assert f"{_FILTER}.length ? {_FILTER}.length : Infinity" in text
    assert "_reviewPrep.slices.filter(" not in text.split("const _reviewPrep = (() =>")[0]


def test_unfiltered_in_wave_prompt_and_count_are_untouched():
    text = _joined(_compose(True))
    assert "review_slice_ids" not in text
    assert "Infinity" not in text


def test_no_slices_guard_consults_the_hook_only_when_a_composer_hoisted_it():
    guard = _joined(_compose(False)).split(NO_SLICES_HALT)[0].rsplit("\n", 1)[-1]
    assert "typeof _alreadyDeliveredClose === 'function' && await _alreadyDeliveredClose()" in guard
    assert "_alreadyDelivered =" not in guard  # the hook owns its state; the plan route has no hook
