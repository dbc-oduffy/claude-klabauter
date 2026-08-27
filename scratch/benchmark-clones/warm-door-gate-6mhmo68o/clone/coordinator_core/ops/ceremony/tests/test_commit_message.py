"""
coordinator_core.ops.ceremony.tests.test_commit_message

Golden-byte-match parity suite for commit_message.py -- the C2 chunk of the
`wsc_tail` rebuild (docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md).

Coverage:
  (a) golden_full_message           -- reproduces the exact golden fixture from
                                        the deleted `tests/wsc-asic/test-wsc-commit-parity.sh`
                                        (assertion (a)), byte-for-byte, including
                                        the U+2014 em-dash in the Kept entry.
  (b) blank_line_permutations       -- the four independently-conditional blank
                                        lines, exercised in every combination the
                                        bash original supported (subject-only,
                                        subject+prose, subject+deleted-only,
                                        subject+kept-only, subject+both-blocks-
                                        no-prose, and the full combination).
  (c) trailers_appended             -- non-empty trailers append as a final
                                        blank-line-separated block; empty
                                        trailers leaves the message unchanged
                                        (byte-identical to the no-trailers case).
  (d) format_kept_entry             -- em-dash separator composition helper.
  (e) path_set_composition          -- gate_paths / commit_paths dual-set rules.
  (f) swept_rename_parsing          -- valid parse + all four bash-original
                                        rejection cases.

Spec backlink: pln-rebuild-the-wsc-commit-ceremon-f7c2a0 § C2 (AC4).
Provenance: golden fixture text recovered from
  `DoE:85006468^:coordinator/tests/wsc-asic/test-wsc-commit-parity.sh`
  (deleted 2026-07-15 by the wsc-commit kill; recovered per the plan's C2
  provenance instruction).
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.ceremony.commit_message import (
    EM_DASH_SEPARATOR,
    SweptRenameError,
    compose_message,
    compute_commit_paths,
    compute_gate_paths,
    format_kept_entry,
    parse_swept_rename,
)

# ---------------------------------------------------------------------------
# (a) golden full-message byte-match
# ---------------------------------------------------------------------------

#: Recovered verbatim from the deleted parity test's GOLDEN_MESSAGE heredoc.
#: The em-dash below is a literal U+2014 character (three UTF-8 bytes:
#: 0xE2 0x80 0x94), matching the bash fixture's $'\342\200\224' escape.
_GOLDEN_MESSAGE = (
    "workstream-complete: my-feature\n"
    "\n"
    "Closed out the my-feature workstream: shipped the refactor and updated docs.\n"
    "\n"
    "Deleted (Step 2.67):\n"
    "tasks/my-feature/scratch-notes.md\n"
    "tasks/my-feature/draft-snippet.sh\n"
    "\n"
    "Kept (Step 2.67):\n"
    "tasks/my-feature/todo.md — still load-bearing for active sibling workstream\n"
    "--- end Step 2.67 blocks ---\n"
)


def test_golden_full_message_byte_match():
    actual = compose_message(
        subject="workstream-complete: my-feature",
        prose=(
            "Closed out the my-feature workstream: shipped the refactor and "
            "updated docs."
        ),
        deleted_paths=[
            "tasks/my-feature/scratch-notes.md",
            "tasks/my-feature/draft-snippet.sh",
        ],
        kept_entries=[
            format_kept_entry(
                "tasks/my-feature/todo.md",
                "still load-bearing for active sibling workstream",
            )
        ],
    )
    assert actual == _GOLDEN_MESSAGE


def test_golden_message_uses_u2014_em_dash():
    # Byte-level sanity check independent of the golden-string comparison above:
    # the separator in the composed Kept entry is specifically U+2014, not a
    # hyphen-minus or en-dash.
    entry = format_kept_entry("path", "reason")
    assert "—" in entry
    assert EM_DASH_SEPARATOR == " — "


# ---------------------------------------------------------------------------
# (b) blank-line permutations -- each of the four conditionals independently
# ---------------------------------------------------------------------------


def test_subject_only():
    assert compose_message(subject="chore: nothing else") == "chore: nothing else\n"


def test_subject_and_prose_no_blocks():
    actual = compose_message(subject="subj", prose="just prose, no blocks")
    assert actual == "subj\n\njust prose, no blocks\n"


def test_subject_and_deleted_only_no_prose_no_kept():
    actual = compose_message(subject="subj", deleted_paths=["a.txt", "b.txt"])
    assert actual == (
        "subj\n"
        "\n"
        "Deleted (Step 2.67):\n"
        "a.txt\n"
        "b.txt\n"
        "--- end Step 2.67 blocks ---\n"
    )


def test_subject_and_kept_only_no_prose_no_deleted():
    actual = compose_message(
        subject="subj", kept_entries=[format_kept_entry("k.txt", "still needed")]
    )
    assert actual == (
        "subj\n"
        "\n"
        "Kept (Step 2.67):\n"
        "k.txt — still needed\n"
        "--- end Step 2.67 blocks ---\n"
    )


def test_subject_deleted_and_kept_no_prose():
    actual = compose_message(
        subject="subj",
        deleted_paths=["a.txt"],
        kept_entries=[format_kept_entry("k.txt", "still needed")],
    )
    assert actual == (
        "subj\n"
        "\n"
        "Deleted (Step 2.67):\n"
        "a.txt\n"
        "\n"
        "Kept (Step 2.67):\n"
        "k.txt — still needed\n"
        "--- end Step 2.67 blocks ---\n"
    )


def test_subject_prose_deleted_no_kept():
    actual = compose_message(
        subject="subj",
        prose="prose here",
        deleted_paths=["a.txt"],
    )
    assert actual == (
        "subj\n"
        "\n"
        "prose here\n"
        "\n"
        "Deleted (Step 2.67):\n"
        "a.txt\n"
        "--- end Step 2.67 blocks ---\n"
    )


# ---------------------------------------------------------------------------
# (c) trailers -- appended, blank-line separated; empty leaves message untouched
# ---------------------------------------------------------------------------


def test_trailers_empty_is_byte_identical_to_no_trailers_call():
    with_empty_trailers = compose_message(subject="subj", prose="p", trailers="")
    without_trailers_arg = compose_message(subject="subj", prose="p")
    assert with_empty_trailers == without_trailers_arg


def test_trailers_appended_after_blank_line():
    actual = compose_message(
        subject="subj",
        trailers="Nature: infra\nPlan: docs/plans/x.md",
    )
    assert actual == "subj\n\nNature: infra\nPlan: docs/plans/x.md\n"


def test_trailers_appended_after_full_message():
    actual = compose_message(
        subject="subj",
        prose="p",
        deleted_paths=["a.txt"],
        trailers="Nature: infra",
    )
    assert actual == (
        "subj\n"
        "\n"
        "p\n"
        "\n"
        "Deleted (Step 2.67):\n"
        "a.txt\n"
        "--- end Step 2.67 blocks ---\n"
        "\n"
        "Nature: infra\n"
    )


def test_trailers_join_existing_trailer_block_no_blank_line():
    """Caller's own prose already ends in a trailer block -- must join it.

    Regression for the break-class defect fixed alongside `_ends_with_trailer_block`'s
    move to this module: a blank-line separator here would start a NEW
    paragraph and demote `Deliverable-Id:` to body prose for git's trailer
    parser (`interpret-trailers` sees only the LAST paragraph as trailers).
    """
    subj = "C1: subject line\n\nsome prose here.\n\nDeliverable-Id: dlv-abc123"
    actual = compose_message(subject=subj, trailers="Session-Id: deadbeef")
    assert actual == (
        "C1: subject line\n"
        "\n"
        "some prose here.\n"
        "\n"
        "Deliverable-Id: dlv-abc123\n"
        "Session-Id: deadbeef\n"
    )
    # Last paragraph (no blank line inside it) must contain BOTH trailers.
    last_paragraph = actual.rstrip("\n").split("\n\n")[-1]
    assert "Deliverable-Id: dlv-abc123" in last_paragraph
    assert "Session-Id: deadbeef" in last_paragraph


def test_trailers_after_prose_only_stays_blank_line_separated():
    """Caller message ending in plain prose (no trailer block) -- unchanged."""
    actual = compose_message(
        subject="subj", prose="just some prose.", trailers="Nature: infra"
    )
    assert actual == "subj\n\njust some prose.\n\nNature: infra\n"


def test_trailers_after_subject_only_shaped_like_key_value_stays_blank_line_separated():
    """A subject-only "wsc: subject only" shape is prose, not a trailer block.

    Mirrors `_ends_with_trailer_block`'s own documented trap: a trailing
    non-blank run that consumes the ENTIRE message is the subject line
    (first paragraph), never a trailer paragraph -- git's trailers always
    live in the message's last paragraph, and the last paragraph is never
    the first one.
    """
    actual = compose_message(subject="wsc: subject only", trailers="Nature: infra")
    assert actual == "wsc: subject only\n\nNature: infra\n"


# ---------------------------------------------------------------------------
# (d) format_kept_entry
# ---------------------------------------------------------------------------


def test_format_kept_entry():
    assert format_kept_entry("p", "r") == "p — r"


# ---------------------------------------------------------------------------
# (e) dual path-set composition
# ---------------------------------------------------------------------------


def test_compute_gate_paths_excludes_swept():
    gate = compute_gate_paths(["c1.md", "c2.md"], ["d1.md"])
    assert gate == ["c1.md", "c2.md", "d1.md"]


def test_compute_gate_paths_empty():
    assert compute_gate_paths([], []) == []


def test_compute_commit_paths_includes_swept():
    gate = compute_gate_paths(["c1.md"], ["d1.md"])
    commit_paths = compute_commit_paths(gate, ["old/src.md"], ["new/dst.md"])
    assert commit_paths == ["c1.md", "d1.md", "old/src.md", "new/dst.md"]


def test_compute_commit_paths_pure_archival_fold_no_gate_paths():
    # A pure --swept-rename-only invocation: gate_paths is empty, commit_paths
    # is exactly the swept src/dst pair.
    commit_paths = compute_commit_paths([], ["old/src.md"], ["new/dst.md"])
    assert commit_paths == ["old/src.md", "new/dst.md"]


# ---------------------------------------------------------------------------
# (f) swept-rename parsing -- valid + the four bash-original rejection cases
# ---------------------------------------------------------------------------


def test_parse_swept_rename_valid():
    assert parse_swept_rename("old/path.md|new/path.md") == (
        "old/path.md",
        "new/path.md",
    )


def test_parse_swept_rename_no_pipe_rejected():
    with pytest.raises(SweptRenameError, match="no \\| separator"):
        parse_swept_rename("no-pipe-here.md")


def test_parse_swept_rename_empty_src_rejected():
    with pytest.raises(SweptRenameError, match="empty src"):
        parse_swept_rename("|dst.md")


def test_parse_swept_rename_empty_dst_rejected():
    with pytest.raises(SweptRenameError, match="empty dst"):
        parse_swept_rename("src.md|")


def test_parse_swept_rename_multi_pipe_rejected():
    with pytest.raises(SweptRenameError, match="more than one \\|"):
        parse_swept_rename("src.md|mid|dst.md")
