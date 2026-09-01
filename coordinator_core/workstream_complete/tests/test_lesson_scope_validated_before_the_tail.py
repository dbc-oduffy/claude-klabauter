"""
coordinator_core.workstream_complete.tests.test_lesson_scope_validated_before_the_tail

Example-market-data-repo-em, `cross-repo/archive/2026-08-11-example-market-data-repo-em-
workstream-complete-engine-defects.md` defect 2.

WHAT HAPPENED, in full, at `directives_lessons_plan.py::_iter_capturable_
lessons`'s own inline comment (the canonical telling; not restated here) —
in short: a caller-supplied `scope` the CLI would reject was checked only
after the commit tail had already landed, so the ceremony read as done
while both lessons were silently lost.

WHY THE FIX GOES HERE AND NOT IN THE CLI. The CLI already rejected it —
correctly, and that is not the defect. The defect is WHEN: a value the caller
supplied, which the engine can check with no I/O, was being checked by a
subprocess that runs after the irreversible half of the ceremony. Validating in
`_iter_capturable_lessons` puts the refusal ahead of every directive, alongside
the missing-key and body/body_file refusals that were already there and already
say "nothing has been written".
"""

from __future__ import annotations

import pytest

from coordinator_core.workstream_complete.directives_lessons_plan import (
    _VALID_LESSON_SCOPES,
    build_lesson_capture_directives,
)


def _lesson(**over) -> dict:
    lesson = {"title": "A lesson", "body": "One line.", "scope": "project"}
    lesson.update(over)
    return lesson


def test_the_reported_value_is_refused_before_any_directive_is_built() -> None:
    """`"local"` — the exact value that cost them both lessons."""
    with pytest.raises(ValueError) as exc:
        build_lesson_capture_directives({"lessons": [_lesson(scope="local")]})
    message = str(exc.value)
    assert "'local'" in message
    assert "nothing has been written" in message


def test_the_refusal_names_the_valid_set_so_the_caller_can_fix_it_blind() -> None:
    """A refusal that says only "invalid" makes the caller go read the CLI's
    argparse help — which is what the value came from being guessed in the
    first place."""
    with pytest.raises(ValueError) as exc:
        build_lesson_capture_directives({"lessons": [_lesson(scope="local")]})
    message = str(exc.value)
    for value in sorted(_VALID_LESSON_SCOPES):
        assert value in message


def test_the_refusal_names_which_entry_is_wrong() -> None:
    """A close can carry several lessons; "one of them is invalid" is not
    actionable on a list of four."""
    with pytest.raises(ValueError) as exc:
        build_lesson_capture_directives(
            {"lessons": [_lesson(), _lesson(), _lesson(scope="local")]}
        )
    assert "[2]" in str(exc.value)


@pytest.mark.parametrize("scope", sorted(_VALID_LESSON_SCOPES))
def test_every_valid_scope_still_builds(scope: str) -> None:
    """The negative half — the guard must not narrow the accepted set. Swept
    over the enum rather than spot-checked, so adding a fourth value to
    `_VALID_LESSON_SCOPES` without teaching the downstream CLI fails here."""
    directives = build_lesson_capture_directives({"lessons": [_lesson(scope=scope)]})
    assert directives
    assert "--scope" in directives[0]["args"]
    assert scope in directives[0]["args"]


@pytest.mark.parametrize("scope", ["", None, "  ", "Universal", "wiki_only"])
def test_near_misses_and_empties_are_refused_too(scope) -> None:
    """Case and separator variants are the likely near-misses (`Universal`,
    `wiki_only`), and an absent scope must not silently reach a `--scope ''`
    argv. All refused by the same arm."""
    with pytest.raises(ValueError):
        build_lesson_capture_directives({"lessons": [_lesson(scope=scope)]})


def test_no_lessons_is_not_an_error() -> None:
    """The overwhelmingly common close: the guard must cost a plan-less,
    lesson-less session nothing."""
    assert build_lesson_capture_directives({}) == []
    assert build_lesson_capture_directives({"lessons": []}) == []
