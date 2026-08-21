"""coordinator_core.workstream_complete.tests.test_lesson_body_argv — how a
lesson body reaches `coordinator-lesson-add`.

Purpose: `build_lesson_capture_directives` must hand the CLI a body it will
accept. The CLI refuses a `--body` containing a newline and names `--body-file`
as the alternative; the assembler emitted `--body` unconditionally, so every
multi-line lesson died at `argv_rejected` — which is every lesson the corpus
actually wants, since its entries are multi-paragraph by convention.

Lives in its own module rather than in `test_workstream_complete.py` because
that file was carrying another session's in-flight rewrite when this landed,
and a shared test file is the wrong place to put work that has to be committed
on its own path.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.workstream_complete import directives_lessons_plan as _dc_lessons


def _args_for(body: str) -> list[str]:
    return _dc_lessons.build_lesson_capture_directives(
        {"lessons": [{"title": "t", "body": body, "scope": "project"}]}
    )[0]["args"]


def test_multiline_lesson_body_goes_through_body_file_not_body():
    """A multi-line body must reach the CLI as `--body-file`, never `--body`.

    This is the case the directive exists for: a one-line lesson is the rare
    shape, so emitting `--body` unconditionally meant the ceremony could only
    capture the lessons least worth capturing.
    """
    body = "first paragraph\n\nsecond paragraph"
    args = _args_for(body)

    assert "--body" not in args, "a multi-line body must not ride argv"
    assert "--body-file" in args

    written = Path(args[args.index("--body-file") + 1])
    try:
        assert written.read_text(encoding="utf-8") == body, (
            "the body must reach the CLI byte-identical — paragraph breaks in a "
            "lesson carry meaning, and the assembler does not own the prose"
        )
    finally:
        written.unlink(missing_ok=True)


def test_single_line_lesson_body_still_rides_argv():
    """The file materialisation is scoped to the case that needs it: a
    one-line body keeps the cheaper path and writes nothing to disk."""
    assert _args_for("one line") == [
        "--title", "t", "--body", "one line", "--scope", "project",
    ]


def test_trailing_newline_alone_is_enough_to_take_the_file_path():
    """The discriminator is "contains a newline", not "looks like prose".

    A body with a single trailing newline is still rejected by the CLI, so it
    must take the file path too — testing only a two-paragraph body would let a
    `body.strip()`-shaped regression pass.
    """
    args = _args_for("one line with a trailing newline\n")
    assert "--body-file" in args and "--body" not in args
    written = Path(args[args.index("--body-file") + 1])
    try:
        assert written.read_text(encoding="utf-8").endswith("\n"), (
            "a trailing newline is content; stripping it edits the lesson"
        )
    finally:
        written.unlink(missing_ok=True)


def test_body_file_is_named_for_the_directive_that_consumes_it():
    """An orphaned temp file must be attributable to the directive that made
    it, not anonymous in the system temp dir."""
    directive = _dc_lessons.build_lesson_capture_directives(
        {"lessons": [{"title": "t", "body": "a\nb", "scope": "project"}]}
    )[0]
    args = directive["args"]
    written = Path(args[args.index("--body-file") + 1])
    try:
        assert directive["id"] in written.name, (
            f"{written.name} does not name its directive ({directive['id']})"
        )
    finally:
        written.unlink(missing_ok=True)
