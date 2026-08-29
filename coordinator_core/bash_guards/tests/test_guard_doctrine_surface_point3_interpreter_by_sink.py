"""Point 3's interpreter leg, narrowed by sink: a `python -c` payload that
READS a governed surface and writes somewhere unrelated must stop being denied,
and every payload that actually writes one must still be refused.

Purpose: `_is_interpreter_read_shape` declined the moment its segment carried
any write marker, regardless of target. An interpreter payload is a SINGLE
top-level segment, so a governed read and an unrelated write share it:

    python3 -c "print(open('<gov>').read()); open('/tmp/x','w').write('y')"

...denied, though the only write lands in scratch. This is the companion of the
point-4 defect closed at 78c7cef95 -- same root cause (a marker never related to
its sink), one leg over. It was left open there deliberately, recorded as a
separate item rather than folded in, and this file closes it on its own evidence.

The DENY corpus is the load-bearing half: a narrowing of a security guard is
only as good as the attacks it still refuses. Every shape that writes a governed
surface through an interpreter is pinned here, including the ones the narrowing
must refuse because it CANNOT read them (a non-literal path, a file object bound
to a name) rather than because it understood them.

Negative-spec: this file does NOT assert on deny TEXT, and it does NOT re-cover
point 4 -- `test_guard_doctrine_surface_point4_by_sink.py` owns that leg. It
also does not cover the shell-redirect interpreter shape
(`python3 -c '...' > <gov>`), which point 3's `_has_write_marker_for_point3`
already narrows by redirect target.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as guard

from .test_guard_doctrine_surface_point4_by_sink import GOV, IDENTIFIERS

DENY_CASES = [
    ("write-mode open on the governed literal", f"python3 -c \"open('{GOV}','w').write('x')\""),
    ("append-mode open on the governed literal", f"python3 -c \"open('{GOV}','a').write('x')\""),
    ("exclusive-mode open on the governed literal", f"python3 -c \"open('{GOV}','x').write('y')\""),
    ("governed write beside an ungoverned one", f"python3 -c \"open('/tmp/x','w').write('a'); open('{GOV}','w').write('b')\""),
    ("bound file object -- unreadable, must fail closed", f"python3 -c \"f=open('{GOV}','w'); f.write('x')\""),
    ("concatenated path -- not a literal, must fail closed", f"python3 -c \"open('{GOV}'+'','w').write('x')\""),
    ("variable path -- not a literal, must fail closed", f"p='{GOV}'; python3 -c \"open(p,'w').write('x')\""),
    ("write_text on a governed Path", f"python3 -c \"import pathlib; pathlib.Path('{GOV}').write_text('x')\""),
    ("redirect onto the governed surface", f"python3 -c \"print(open('{GOV}').read())\" > {GOV}"),
    ("os.system smuggling a redirect", f"python3 -c \"import os; os.system('echo x > {GOV}')\""),
    ("subprocess smuggling a write", f"python3 -c \"import subprocess; subprocess.run(['tee','{GOV}'])\""),
    ("eval payload naming the surface", f"python3 -c \"eval(open('{GOV}','w').write)\""),
    ("read of the governed surface piped into xargs", f"python3 -c \"print('{GOV}')\" | xargs tee"),
    ("python -m module, never a read shape", f"python3 -m json.tool {GOV} > {GOV}"),
]

#: The governed surface is only READ; every write in the payload is an
#: analysable literal-path open in a write mode, landing somewhere unrelated.
ALLOW_CASES = [
    ("read governed, write scratch", f"python3 -c \"print(open('{GOV}').read()); open('/tmp/x','w').write('y')\""),
    ("read governed into a scratch write", f"python3 -c \"open('/tmp/x','w').write(open('{GOV}').read())\""),
    ("two scratch writes beside the read", f"python3 -c \"open('/tmp/a','w').write('1'); open('/tmp/b','a').write('2'); print(open('{GOV}').read())\""),
    ("nested call in the path argument still resolves the mode", f"python3 -c \"open('/tmp/x','w').write(str(len(open('{GOV}').read())))\""),
]

#: Measured against a reconstruction of the old predicate, not assumed: the
#: cases the pre-narrowing interpreter leg actually denied. Everything else in
#: ALLOW_CASES is a control. See `test_the_regression_cover_is_labelled_honestly`.
REGRESSION_COVER = {
    "read governed, write scratch",
    "read governed into a scratch write",
    "two scratch writes beside the read",
    "nested call in the path argument still resolves the mode",
}


@pytest.mark.parametrize("label,cmd", DENY_CASES, ids=[c[0] for c in DENY_CASES])
def test_interpreter_writes_to_governed_surfaces_still_deny(label: str, cmd: str) -> None:
    """The regression half. Each of these either writes a governed surface or
    is a shape the narrowing cannot read -- both must refuse."""
    assert guard.is_denied_bash_write(cmd, IDENTIFIERS) is True, (
        f"{label}: the interpreter by-sink narrowing opened a governed write -- {cmd!r}"
    )


@pytest.mark.parametrize("label,cmd", ALLOW_CASES, ids=[c[0] for c in ALLOW_CASES])
def test_interpreter_reads_with_unrelated_writes_are_allowed(label: str, cmd: str) -> None:
    """The fix half. The governed surface is read; every write is an analysable
    literal open landing elsewhere."""
    assert guard.is_denied_bash_write(cmd, IDENTIFIERS) is False, (
        f"{label}: denied an interpreter read whose writes are all ungoverned -- {cmd!r}"
    )


def test_the_regression_cover_is_labelled_honestly() -> None:
    """Same discipline point 4's corpus carries: reconstruct the pre-narrowing
    interpreter leg and assert exactly the named cases were denied by it, so a
    later ALLOW case cannot pad the count without being classified."""
    flipped = set()
    for label, cmd in ALLOW_CASES:
        for segment in guard._split_top_level_segments(cmd):
            if not guard._mentions_governed_identifier(segment, IDENTIFIERS):
                continue
            if guard._has_write_marker(segment):
                flipped.add(label)
    assert flipped == REGRESSION_COVER, (
        f"ALLOW cases denied by the OLD interpreter leg were {sorted(flipped)}, but "
        f"REGRESSION_COVER names {sorted(REGRESSION_COVER)} -- classify every new "
        "case as regression cover or control rather than leaving the count ambiguous"
    )


def test_a_bound_file_object_stays_unanalysable() -> None:
    """The narrowing reads only the adjacent `open(<literal>, <write mode>)`
    shape. A file object bound to a name and written later needs dataflow inside
    the payload, which this guard does not have -- pinned so widening it is a
    deliberate act rather than drift."""
    segment = f"python3 -c \"f=open('/tmp/x','w'); f.write(open('{GOV}').read())\""
    assert guard._interpreter_write_sinks_are_ungoverned(segment, IDENTIFIERS) is False


def test_a_read_mode_open_is_not_treated_as_a_sink() -> None:
    """Reading a governed surface is the shape this narrowing exists to stop
    denying, so a read-mode open must never count as a write target."""
    segment = f"python3 -c \"open('/tmp/x','w').write(open('{GOV}').read())\""
    assert guard._interpreter_write_sinks_are_ungoverned(segment, IDENTIFIERS) is True


def test_the_substring_prefilter_hole_is_upstream_of_this_leg() -> None:
    """NOT A PASS -- a pinned, measured LIMIT, recorded so it is not mistaken for
    coverage this file provides.

    Point 1's governed-identifier prefilter is plain substring matching, so a
    payload that splits the name never reaches ANY sink leg:

        python3 -c "open('CLAUDE'+'.md','w').write('x')"

    is allowed, and was allowed identically before the point-3 narrowing
    (measured against the pre-change module, 2026-08-29). Closing it means
    folding literal concatenation before the prefilter, on the hot path every
    Bash call crosses -- its own design and its own perf spike, filed rather
    than smuggled in here. See
    `state/bug-backlog/2026-08-29-string-concatenation-defeats-the-governed-identifier-prefilter.yaml`.

    This test asserts the CURRENT behaviour so the day it is fixed, this file
    fails and is updated deliberately."""
    split_name = f"python3 -c \"open('{GOV[:-3]}'+'.md','w').write('x')\""
    assert guard.is_denied_bash_write(split_name, IDENTIFIERS) is False


def test_open_call_spans_survive_nested_parens() -> None:
    """A regex stopping at the first `)` would truncate `open(str(p), 'w')`
    before its mode and misread a write as a read. Depth counting is what makes
    the mode test trustworthy, so it is asserted directly."""
    spans = guard._open_call_spans("open(str(p), 'w').write(open('a').read())")
    assert [args for _, _, args in spans] == ["str(p), 'w'", "'a'"]
