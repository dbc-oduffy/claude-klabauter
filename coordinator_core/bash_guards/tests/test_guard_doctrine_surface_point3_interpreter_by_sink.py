
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

ALLOW_CASES = [
    ("read governed, write scratch", f"python3 -c \"print(open('{GOV}').read()); open('/tmp/x','w').write('y')\""),
    ("read governed into a scratch write", f"python3 -c \"open('/tmp/x','w').write(open('{GOV}').read())\""),
    ("two scratch writes beside the read", f"python3 -c \"open('/tmp/a','w').write('1'); open('/tmp/b','a').write('2'); print(open('{GOV}').read())\""),
    ("nested call in the path argument still resolves the mode", f"python3 -c \"open('/tmp/x','w').write(str(len(open('{GOV}').read())))\""),
]

#: ALLOW_CASES is a control. See `test_the_regression_cover_is_labelled_honestly`.
REGRESSION_COVER = {
    "read governed, write scratch",
    "read governed into a scratch write",
    "two scratch writes beside the read",
    "nested call in the path argument still resolves the mode",
}


@pytest.mark.parametrize("label,cmd", DENY_CASES, ids=[c[0] for c in DENY_CASES])
def test_interpreter_writes_to_governed_surfaces_still_deny(label: str, cmd: str) -> None:
    assert guard.is_denied_bash_write(cmd, IDENTIFIERS) is True, (
        f"{label}: the interpreter by-sink narrowing opened a governed write -- {cmd!r}"
    )


@pytest.mark.parametrize("label,cmd", ALLOW_CASES, ids=[c[0] for c in ALLOW_CASES])
def test_interpreter_reads_with_unrelated_writes_are_allowed(label: str, cmd: str) -> None:
    assert guard.is_denied_bash_write(cmd, IDENTIFIERS) is False, (
        f"{label}: denied an interpreter read whose writes are all ungoverned -- {cmd!r}"
    )


def test_the_regression_cover_is_labelled_honestly() -> None:
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
    segment = f"python3 -c \"f=open('/tmp/x','w'); f.write(open('{GOV}').read())\""
    assert guard._interpreter_write_sinks_are_ungoverned(segment, IDENTIFIERS) is False


def test_a_read_mode_open_is_not_treated_as_a_sink() -> None:
    segment = f"python3 -c \"open('/tmp/x','w').write(open('{GOV}').read())\""
    assert guard._interpreter_write_sinks_are_ungoverned(segment, IDENTIFIERS) is True


SPLIT_NAME_CASES = [
    ("explicit concat in a payload", f"python3 -c \"open('{GOV[:-3]}'+'.md','w').write('x')\""),
    ("implicit concat in a payload", f"python3 -c \"open('{GOV[:-3]}''.md','w').write('x')\""),
    ("spaced explicit concat", f"python3 -c \"open('{GOV[:-3]}' + '.md','w').write('x')\""),
    ("mixed quote styles", f"python3 -c \"open('{GOV[:-3]}'+\\\".md\\\",'w').write('x')\""),
    ("shell adjacency in a redirect target", f"echo x > '{GOV[:-3]}''.md'"),
    ("three-way split", f"python3 -c \"open('{GOV[:-4]}'+'{GOV[-4]}'+'.md','w').write('x')\""),
]


@pytest.mark.parametrize("label,cmd", SPLIT_NAME_CASES, ids=[c[0] for c in SPLIT_NAME_CASES])
def test_a_name_split_across_a_concatenation_still_denies(label: str, cmd: str) -> None:
    assert guard.is_denied_bash_write(cmd, IDENTIFIERS) is True, (
        f"{label}: a split governed name evaded the prefilter -- {cmd!r}"
    )


def test_whitespace_separated_words_are_never_joined() -> None:
    """The fold is deliberately ZERO-WIDTH only. `'a' 'b'` is one string in
    Python but two arguments in shell, so joining it would invent governed
    mentions in ordinary commands -- an over-denial introduced by a fix for an
    under-denial. Pinned so a later widening is a deliberate act."""
    assert guard._fold_literal_joins("cat 'foo' 'bar'") == "cat 'foo' 'bar'"
    two_words = f"grep -n heading '{GOV[:-3]}' '.md'"
    assert guard.is_denied_bash_write(two_words, IDENTIFIERS) is False


def test_folding_cannot_reduce_what_the_prefilter_admits() -> None:
    """The fold runs IN ADDITION to the raw check, never instead of it, so it
    can only widen what reaches the sink legs. A plainly-named surface must
    still be seen even in text the fold would rewrite."""
    plain = f"echo x > {GOV} ; python3 -c \"print('a'+'b')\""
    assert guard._mentions_governed_identifier(plain, IDENTIFIERS) is True
    assert guard.is_denied_bash_write(plain, IDENTIFIERS) is True


def test_open_call_spans_survive_nested_parens() -> None:
    spans = guard._open_call_spans("open(str(p), 'w').write(open('a').read())")
    assert [args for _, _, args in spans] == ["str(p), 'w'", "'a'"]
