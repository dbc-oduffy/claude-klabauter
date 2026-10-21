"""coordinator_core.bash_guards.tests.test_guard_doctrine_surface_interpreter_by_sink --
engine-body pin for the interpreter carve-out's by-sink narrowing in
`guard_doctrine_surface_bash_write`: an interpreter payload that reads a
governed surface and writes elsewhere must allow, and every payload that can
write a governed surface must keep denying.

Complements `test_guard_doctrine_surface_point3_interpreter_by_sink.py`; the
cases here are the ones that file does not carry (write_bytes, nested surface,
argv/str() sinks, chained shell sinks, perl/ruby/os.open, and the five shapes
review found open in a withdrawn wider dataflow predicate).
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as guard

from .test_guard_doctrine_surface_point4_by_sink import GOV, IDENTIFIERS, SURFACES

GOV_NESTED = SURFACES[2]

DENY_CASES = [
    ("write_bytes on a Path", f"python3 -c \"import pathlib; pathlib.Path('{GOV}').write_bytes(b'x')\""),
    ("nested governed surface", f"python3 -c \"open('{GOV_NESTED}','w').write('x')\""),
    ("non-literal path from a name", f"python3 -c \"p='{GOV}'; open(p,'w').write('x')\""),
    ("non-literal path through str()", f"python3 -c \"import pathlib; open(str(pathlib.Path('{GOV}')), 'w').write('x')\""),
    ("subprocess shell escape hatch", f"python3 -c \"import subprocess; subprocess.run('echo x > {GOV}', shell=True)\""),
    ("dynamic sink from argv", f"python3 -c \"import sys; open(sys.argv[1],'w').write('x')\" {GOV}"),
    ("bare path operand to a script", f"python3 fix_doctrine.py {GOV}"),
    ("redirect onto the governed surface", f"python3 -c \"print('x')\" > {GOV}"),
    ("tee sink in the same pipeline", f"python3 -c \"print('{GOV}')\" | tee {GOV}"),
    ("cp chained after the payload", f"python3 -c \"print('{GOV}')\" && cp /tmp/x {GOV}"),
    ("sed -i chained after the payload", f"python3 -c \"print(1)\"; sed -i s/a/b/ {GOV}"),
    ("perl write-mode open", f"perl -e \"open(FH,'>','{GOV}'); print FH 'x'\""),
]

REVIEW_FOUND_BYPASSES = [
    ("trailing reassignment after the write", f"python3 -c \"p='{GOV}'; open(p,'w').write('x'); p='/tmp/safe'\""),
    ("rebind clobbers a governed binding", f"python3 -c \"p='{GOV}'; p=p; open(p,'w').write('x')\""),
    ("os.open then os.write", f"python3 -c \"import os; fd=os.open('{GOV}', os.O_WRONLY); os.write(fd, b'x')\""),
    ("ruby class-level File.write", f"ruby -e \"File.write('{GOV}','x')\""),
    ("chained Path().open() receiver", f"python3 -c \"import pathlib; pathlib.Path('{GOV}').open('w').write('x')\""),
]

ALLOW_CASES = [
    ("read governed, open-write elsewhere", f"python3 -c \"print(open('{GOV}').read()); open('/tmp/out.txt','w').write('x')\""),
    ("measure governed, report to scratch", f"python3 -c \"open('scratch/report.txt','w').write(str(len(open('{GOV}').read())))\""),
    ("governed name as payload prose, write elsewhere", f"python3 -c \"open('/tmp/note.txt','w').write('{GOV} holds the budget')\""),
    ("read through a bound name, write elsewhere", f"python3 -c \"p='{GOV}'; s=open(p).read(); open('/tmp/o','w').write(s)\""),
]


@pytest.mark.parametrize("label,cmd", DENY_CASES, ids=[c[0] for c in DENY_CASES])
def test_real_governed_writes_still_deny(label: str, cmd: str) -> None:
    assert guard.is_denied_bash_write(cmd, IDENTIFIERS) is True, (
        f"{label}: the by-sink narrowing opened a real governed write -- {cmd!r}"
    )


@pytest.mark.parametrize(
    "label,cmd", REVIEW_FOUND_BYPASSES, ids=[c[0] for c in REVIEW_FOUND_BYPASSES]
)
def test_review_found_bypasses_stay_closed(label: str, cmd: str) -> None:
    assert guard.is_denied_bash_write(cmd, IDENTIFIERS) is True, (
        f"{label}: a reviewed bypass has been reopened -- {cmd!r}"
    )


@pytest.mark.parametrize("label,cmd", ALLOW_CASES, ids=[c[0] for c in ALLOW_CASES])
def test_reads_whose_write_lands_elsewhere_are_allowed(label: str, cmd: str) -> None:
    assert guard.is_denied_bash_write(cmd, IDENTIFIERS) is False, (
        f"{label}: denied an interpreter read whose write target is not governed -- {cmd!r}"
    )


def test_the_minimal_pair_differs_only_by_the_unrelated_write() -> None:
    with_write = f"python3 -c \"print(open('{GOV}').read()); open('/tmp/o','w').write('x')\""
    without_write = f"python3 -c \"print(open('{GOV}').read())\""
    assert guard.is_denied_bash_write(with_write, IDENTIFIERS) == guard.is_denied_bash_write(
        without_write, IDENTIFIERS
    )


def test_a_read_mode_open_is_never_treated_as_a_sink() -> None:
    read_only = f"python3 -c \"print(open('{GOV}').read()); open('/tmp/o','w').write('x')\""
    assert guard._interpreter_write_sinks_are_ungoverned(read_only, IDENTIFIERS) is True

    governed_sink = f"python3 -c \"open('{GOV}','w').write('x')\""
    assert guard._interpreter_write_sinks_are_ungoverned(governed_sink, IDENTIFIERS) is False


def test_open_call_spans_are_depth_counted() -> None:
    spans = guard._open_call_spans("open(str(p), 'w').write('x')")
    assert len(spans) == 1
    assert spans[0][2] == "str(p), 'w'"


def test_a_payload_with_no_analysable_open_fails_closed() -> None:
    assert guard._interpreter_write_sinks_are_ungoverned(
        f"ruby -e \"File.write('{GOV}','x')\"", IDENTIFIERS
    ) is False
