"""A governed path as an argv operand of a read-only or ungoverned-write
command is not a governed write."""
from __future__ import annotations

import pytest

from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as guard

IDS = guard._SIZING_RECORD_IDENTIFIERS
SZ = "state/sizings/2026-10-11-x.yaml"
DOC = "coordinator/docs/wiki/some-page.md"

ALLOW_CASES = [
    (
        "accept-exit-criterion with a read-only command substitution",
        f'sizing-accept-exit-criterion --sizing {SZ} --apm-ruling "$(cat f)" --criterion x',
    ),
    (
        "python -c scratch write joined with accept-exit-criterion",
        f"python3 -c \"open('scratch/o.txt','w').write('x')\" && "
        f"sizing-accept-exit-criterion --sizing {SZ}",
    ),
    (
        "sizing-assemble intent quoting a governed snippet path",
        f'sizing-assemble --intent "edit {DOC} and state/sizings/z.yaml notes" --write {SZ}',
    ),
]

DENY_CASES = [
    ("echo of a substitution redirected onto a sizing", f'echo "$(cat f)" > {SZ}'),
    (
        "python -c writing a sizing joined with accept call",
        f"python3 -c \"open('{SZ}','w').write('x')\" && "
        f"sizing-accept-exit-criterion --sizing {SZ}",
    ),
    (
        "stdin program binding a governed path reaching a write",
        f"python3 - <<'PY'\np='{SZ}'\nopen(p,'w').write('x')\nPY",
    ),
]


@pytest.mark.parametrize("label,cmd", ALLOW_CASES, ids=[c[0] for c in ALLOW_CASES])
def test_argv_mention_allows(label: str, cmd: str) -> None:
    assert guard.is_denied_bash_write(cmd, IDS) is False, label


@pytest.mark.parametrize("label,cmd", DENY_CASES, ids=[c[0] for c in DENY_CASES])
def test_real_write_still_denies(label: str, cmd: str) -> None:
    assert guard.is_denied_bash_write(cmd, IDS) is True, label
