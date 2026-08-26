"""The in-process trailer envelope is a MEASURED boundary, and both sides of
it are load-bearing.

Inside it, `format_trailers_in_process` is byte-identical to
`git interpret-trailers` (5483/5483 over the byte-identity corpus's fuzz swept
across 20 seeds). Outside it, callers must spawn git -- 6 divergences live
there. These tests pin the predicate itself, because the predicate is the only
thing keeping a known-divergent shape off the commit path.
"""

from coordinator_core.git.commit_trailers import (
    can_format_trailers_in_process,
    format_trailers_in_process,
)


def test_crlf_is_inside_the_envelope():
    """CRLF must be ADMITTED. `Path.write_text` translates `\n` to `\r\n` in
    text mode, so every msg_file a Python caller writes on Windows is CRLF --
    an envelope excluding it is inert on this platform rather than
    conservative, and leaves the spawn it was meant to remove in place."""
    assert can_format_trailers_in_process(b"subject line\r\n")
    assert can_format_trailers_in_process(b"subject\r\n\r\nFoo: bar\r\n")


def test_comment_lines_are_outside_the_envelope():
    """A `#` line is where every residual divergence lives."""
    assert not can_format_trailers_in_process(b"# a comment\n")
    assert not can_format_trailers_in_process(b"subject\n\n# c\nFoo: bar\n")
    assert not can_format_trailers_in_process(b"subject\n\n  # indented\n")


def test_engine_shaped_message_is_admitted():
    assert can_format_trailers_in_process(
        b"subject line\n\nbody paragraph\n\nDeliverable-Id: dlv-x\n"
    )


def test_signed_off_by_unlocks_the_proportional_block_rule():
    """`trailer.c :: find_trailer_block_start` accepts a block holding
    non-trailer lines only when a GIT-GENERATED prefix is present. A
    co-author does not qualify; a sign-off does. Same shape, opposite
    placement -- joined vs separated by a blank line."""
    stray = b"subject\n\ntab\tinside\n%s\n"
    joined = format_trailers_in_process(
        stray % b"Signed-off-by: A <a@b>", ["Trailer: v"]
    )
    separated = format_trailers_in_process(
        stray % b"Co-Authored-By: B <b@c>", ["Trailer: v"]
    )
    assert joined.endswith(b"Signed-off-by: A <a@b>\nTrailer: v\n")
    assert separated.endswith(b"Co-Authored-By: B <b@c>\n\nTrailer: v\n")


def test_no_trailers_returns_the_message_untouched():
    msg = b"subject\r\n\r\n# even a comment\r\n"
    assert format_trailers_in_process(msg, []) is msg
