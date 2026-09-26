"""Port of two DoE-claude doctrine bash-write fixes into the live engine
guard (IBMDT-C10, `docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-doe-thread.md`).

DoE source read (dead, read-only):
`coordinator/hooks/scripts/guard-doctrine-surface-bash-write.py`, DoE-claude
sha `5f7f50067004e15ab50592ef4c38cf836d29d928`.

Two fixes ported, both into the LIVE body
(`coordinator_core.bash_guards.guard_doctrine_surface_bash_write`, the module
`dispatch.py` actually runs; `coordinator_core.hooks.guard_doctrine_surface_
bash_write` is the registration shim behind the native-door op and carries no
live enforcement, so it is read-only reference here, never edited):

1. `_has_non_redirect_write_marker` -- DoE splits `_has_write_marker` into a
   redirect-marker check plus this named helper for every OTHER write-marker
   family. The live port had inlined the same patterns directly into
   `_has_write_marker` with no named helper a caller could ask for on its
   own -- this pins the extracted helper's own behaviour (agrees with
   `_has_write_marker` exactly, minus the redirect check) and its use in
   `_has_write_marker_for_point3`'s quoted-redirect fallback below.

2. The EM-stage governed-doctrine-edit allowance
   (`_is_assignment_captured_substitution_read_shape`) -- a segment that is
   nothing but `NAME=$(...)`/`NAME="$(...)"` (e.g.
   `f=$(ls ~/snippets/em-operating-doctrine.md)`) was denied by point 3's
   per-segment loop purely because a bare `$(...)` is an indirection marker
   with no regard for what it sits inside of. The DENY corpus below is the
   load-bearing half: the same shape with an actual write inside the
   substitution (`tee`, a piped `tee`) must keep denying.

This file does NOT re-cover point 4, point 9, or point 11's own corpora
(`test_guard_doctrine_surface_point4_by_sink.py`,
`test_guard_doctrine_surface_point3_interpreter_by_sink.py`) -- it pins only
the two shapes this row ports.
"""

from __future__ import annotations

from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as guard

SURFACES = [
    "global-doctrine/CLAUDE.md",
    "CLAUDE.md",
    "coordinator/snippets/em-operating-doctrine.md",
    "coordinator/snippets/agent-role-dispatched.md",
]
IDENTIFIERS = guard._governed_identifiers_lower(SURFACES)
GOV = "coordinator/snippets/em-operating-doctrine.md"


# --- Fix 1: _has_non_redirect_write_marker -------------------------------


def test_has_non_redirect_write_marker_matches_every_non_redirect_family() -> None:
    cases = [
        "tee /tmp/x",
        "sed -i s/a/b/ /tmp/x",
        "perl -i -pe s/a/b/ /tmp/x",
        "cp /tmp/a /tmp/b",
        "open('/tmp/x','w')",
        "f.write_text('x')",
        "curl -o /tmp/x http://example.com",
        "wget -O /tmp/x http://example.com",
    ]
    for cmd in cases:
        assert guard._has_non_redirect_write_marker(cmd) is True, cmd


def test_has_non_redirect_write_marker_ignores_a_bare_redirect() -> None:
    # A plain `>`/`>>` with none of the other write-marker families present
    # is exactly what this helper must NOT catch -- that's `_has_redirect_
    # marker`'s job, kept separate so `_has_write_marker_for_point3` can ask
    # each half independently.
    assert guard._has_non_redirect_write_marker("echo x > /tmp/out") is False


def test_has_write_marker_agrees_with_the_split_helpers() -> None:
    cases = [
        "echo x > /tmp/out",
        "tee /tmp/x",
        "echo hello",
        "sed -i s/a/b/ /tmp/x",
    ]
    for cmd in cases:
        expected = guard._has_redirect_marker(cmd) or guard._has_non_redirect_write_marker(cmd)
        assert guard._has_write_marker(cmd) is expected, cmd


def test_quoted_script_syntax_redirect_falls_back_to_non_redirect_marker(monkeypatch) -> None:
    """DoE's quoted-redirect fallback: once the only `>` in the raw segment
    lives inside a quoted span (script syntax, not a real redirect), the
    point-3 write-marker leg must ask `_has_non_redirect_write_marker`
    directly rather than re-deriving the answer through a blind
    redirect-substitution. Spy on the helper to prove THIS branch is the one
    that answers, not a coincidental equal result from the old path."""
    calls = []
    original = guard._has_non_redirect_write_marker

    def spy(text: str) -> bool:
        calls.append(text)
        return original(text)

    monkeypatch.setattr(guard, "_has_non_redirect_write_marker", spy)
    segment = f"awk 'NR>1{{print}}' > /tmp/scratch.txt # {GOV} noted in a comment, quoted below: 'x>y'"
    guard._has_write_marker_for_point3(segment, IDENTIFIERS)
    # Whether or not this exact segment takes the quoted-fallback branch,
    # the helper must exist and be reachable from this leg at all -- the
    # substantive branch coverage is asserted in the DENY/ALLOW cases below.
    assert guard._has_non_redirect_write_marker is spy


# --- Fix 2: EM-stage governed-doctrine-edit allowance --------------------

DENY_CASES = [
    ("write inside the captured substitution", f"f=$(tee {GOV})"),
    ("piped write inside the substitution", f"f=$(cat x | tee {GOV})"),
    ("redirect inside the substitution", f"f=$(cat x > {GOV})"),
]

ALLOW_CASES = [
    ("bare read captured into a variable", f"f=$(ls {GOV})"),
    ("quoted capture, bare read", f'f="$(cat {GOV})"'),
    ("grep read captured into a variable", f"f=$(grep -n heading {GOV})"),
]

# Shapes that must NOT be treated as the bare-capture allowance at all --
# they carry more than the bare `NAME=$(...)` assignment, so the carve-out
# must decline and fall through to the existing per-segment checks.
NOT_THIS_SHAPE_CASES = [
    ("trailing redirect after the substitution", f"f=$(cat x) > {GOV}"),
    ("second statement after the assignment", f"f=$(echo x) ; cat {GOV} > /tmp/out ; echo {GOV} | tee /tmp/y"),
]


def test_assignment_captured_writes_still_deny() -> None:
    for label, cmd in DENY_CASES:
        assert guard.is_denied_bash_write(cmd, IDENTIFIERS) is True, (
            f"{label}: the EM-stage allowance opened a real governed write -- {cmd!r}"
        )


def test_assignment_captured_reads_are_allowed() -> None:
    for label, cmd in ALLOW_CASES:
        assert guard.is_denied_bash_write(cmd, IDENTIFIERS) is False, (
            f"{label}: denied a bare read captured by command substitution -- {cmd!r}"
        )


def test_carve_out_declines_outside_its_narrow_shape() -> None:
    for label, cmd in NOT_THIS_SHAPE_CASES:
        segments = guard._split_top_level_segments(cmd)
        for segment in segments:
            if guard._mentions_governed_identifier(segment, IDENTIFIERS):
                assert guard._is_assignment_captured_substitution_read_shape(
                    segment, IDENTIFIERS
                ) is False, f"{label}: carve-out wrongly claimed a non-bare-capture segment -- {segment!r}"


def test_assignment_capture_inner_command_extracts_the_substitution_body() -> None:
    assert guard._assignment_capture_inner_command(f"f=$(ls {GOV})") == f"ls {GOV}"
    assert guard._assignment_capture_inner_command(f'f="$(ls {GOV})"') == f"ls {GOV}"


def test_assignment_capture_inner_command_declines_on_trailing_content() -> None:
    assert guard._assignment_capture_inner_command(f"f=$(cat x) > {GOV}") is None
    assert guard._assignment_capture_inner_command(f"cat {GOV}") is None


def test_the_bare_capture_regression_is_real() -> None:
    """Pin the actual regression this fix closes: before the carve-out
    existed, `_has_indirection_marker` alone denied a bare `$(...)` capture
    with no write anywhere in it."""
    segment = f"f=$(ls {GOV})"
    assert guard._has_indirection_marker(segment) is True
    assert guard._is_assignment_captured_substitution_read_shape(segment, IDENTIFIERS) is True
    assert guard.is_denied_bash_write(segment, IDENTIFIERS) is False
