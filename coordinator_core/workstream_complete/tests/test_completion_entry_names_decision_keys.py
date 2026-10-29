"""The brief names the decisions keys `d-complete-entry` needs before apply runs."""

from __future__ import annotations

from coordinator_core.workstream_complete import (
    build_decisions_template,
    decision_keys,
    directives_completion,
    judgments,
)


def test_entry_keys_derive_from_alias_targets():
    assert directives_completion.COMPLETION_ENTRY_KEYS == tuple(
        decision_keys.COMPLETION_KEY_ALIASES.values()
    )
    assert {"title", "prose", "nature"} <= set(directives_completion.COMPLETION_ENTRY_KEYS)


def test_prose_judgment_point_question_names_every_key():
    question = judgments.build_completion_entry_prose_judgment_point()["question"]
    for key in directives_completion.COMPLETION_ENTRY_KEYS:
        assert key in question


def test_decisions_template_seeds_entry_keys():
    template = build_decisions_template([], {})
    for key in directives_completion.COMPLETION_ENTRY_KEYS:
        assert key in template
