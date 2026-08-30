"""The interpreter write-sink extractor resolves a SINGLE-ASSIGNMENT bound
`Path("literal")` receiver — the read-modify-write idiom.

Filed as a cross-repo ask by doe-claude-em
(`cross-repo/inbox/2026-08-30-doe-claude-em-interpreter-write-sink-misses-the-
bound-receiver.md`) after a claude-klabauter session wrote three files into
DoE-claude's working tree through a `python - <<PY` heredoc and no foreign-repo
bump fired, while the `git checkout` to revert them WAS correctly refused — the
boundary held against the cleanup and not against the write.

The miss was never the heredoc scan (`_iter_heredoc_bodies` found the body
fine). It was one level in: every `_PY_*` pattern required the path as a
literal AT THE CALL SITE, and you must bind a path to edit a file in place, so
the whole family dropped out for exactly the accidental population the
2026-08-14 interpreter-body reversal was ratified to cover.

Negative-spec — this is a BUMP, not a boundary, and this file must not be read
as widening a deny path. `_python_write_targets_in_text` is consumed only by
`bump_foreign_repo_write` and `bump_outside_repo_write` (both advisory); the
point-3/point-4 governed-surface DENY guards reach their own
`_is_interpreter_read_shape` and are untouched. A test added here that asserts
on a refusal is testing the wrong surface.

Second negative-spec — the drop cases below are the load-bearing half. Resolving
a name to its first binding when that name is REBOUND would be a guess, which
this module's own docstring forbids; `for` targets and `with ... as` bindings
count as rebinding exactly as `=` does. Do not "improve" any DROP case into a
resolution.

Run: python3 -m pytest
coordinator_core/bash_guards/tests/test_write_bump_bound_path_receiver.py -q
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import _write_bump_sink_shapes as sink_shapes


def _targets(body: str) -> list:
    """Every write target the extractor finds in `body`, reached the way a
    real command reaches it — through the heredoc scan, not by calling the
    text extractor directly. The live defect was only ever observable end to
    end, so the fixture reproduces that path rather than a shortcut past it.
    """
    command = "python - <<PY\n" + body + "\nPY"
    return [
        target
        for heredoc_body in sink_shapes._iter_heredoc_bodies(command)
        for target in sink_shapes._python_write_targets_in_text(heredoc_body)
    ]


_FOREIGN = "X:/DoE-claude/a.md"


# ---------------------------------------------------------------------------
# Resolved — the shapes the bump must now see.
# ---------------------------------------------------------------------------


def test_the_live_defect_the_read_modify_write_idiom():
    """Case 3 of the filed repro, verbatim in shape: this returned `[]` before
    the fix and is how the three DoE-claude files were written unbumped."""
    assert _targets(
        f'import pathlib\np = pathlib.Path("{_FOREIGN}")\np.write_text(p.read_text())'
    ) == [_FOREIGN]


@pytest.mark.parametrize(
    "body",
    [
        f'p = Path("{_FOREIGN}")\np.write_text("x")',
        f'p = pathlib.Path("{_FOREIGN}")\np.write_bytes(b"x")',
        f'target = Path("{_FOREIGN}")\ntarget.open("w")',
    ],
    ids=["bare-Path", "qualified-write-bytes", "bound-open-write-mode"],
)
def test_bound_receiver_write_shapes_resolve(body):
    assert _targets(body) == [_FOREIGN]


def test_the_two_literal_shapes_are_unchanged():
    """Cases 1 and 2 of the repro passed before the fix and must still pass —
    the binding path is additive, never a rewrite of the literal patterns."""
    assert _targets(f'open("{_FOREIGN}","w").write(1)') == [_FOREIGN]
    assert _targets(f'pathlib.Path("{_FOREIGN}").write_text("x")') == [_FOREIGN]


# ---------------------------------------------------------------------------
# Dropped — "never a guess" holds. These are the half that keeps the fix honest.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, why",
    [
        (
            f'p = Path("{_FOREIGN}")\nfor p in things:\n    p.write_text("x")',
            "a for-target rebinds the name; the write goes somewhere unknown",
        ),
        (
            f'p = Path("{_FOREIGN}")\nwith foo() as p:\n    p.write_text("x")',
            "an `as` binding rebinds it just as an `=` would",
        ),
        (
            f'p = Path("a.md")\np = Path("{_FOREIGN}")\np.write_text("x")',
            "bound twice — neither binding is THE binding",
        ),
        (
            f'p = Path("{_FOREIGN}")\np += 1\np.write_text("x")',
            "augmented assignment is a rebinding",
        ),
        (
            'p = Path(base) / "a.md"\np.write_text("x")',
            "the bound value is computed, not a literal",
        ),
        (
            f'p = Path("{_FOREIGN}")\np.open("r")',
            "a read mode is not a write sink",
        ),
        (
            f'q = make_path("{_FOREIGN}")\nq.write_text("x")',
            "bound through a call this closed set does not name",
        ),
    ],
    ids=[
        "for-rebound",
        "with-as-rebound",
        "double-bound",
        "augmented-rebound",
        "computed-path",
        "read-mode",
        "unrecognised-binder",
    ],
)
def test_unresolvable_bindings_yield_nothing(body, why):
    assert _targets(body) == [], why


def test_a_rebound_name_does_not_poison_a_sibling_binding():
    """Dropping is per-name, not per-body: one unresolvable receiver must not
    suppress a second, genuinely single-bound one in the same payload."""
    body = (
        f'good = Path("{_FOREIGN}")\n'
        'bad = Path("b.md")\n'
        "for bad in things:\n"
        "    pass\n"
        'good.write_text("x")\n'
        'bad.write_text("y")'
    )
    assert _targets(body) == [_FOREIGN]
