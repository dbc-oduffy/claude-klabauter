"""The state/sizings shell-write rule on a stdin-program heredoc that only QUOTES the path.

A literal with whitespace in it is prose naming the path, not a path operand: a body that writes
an ungoverned file while its message text names a sizing passes. A path operand, or a name bound
to one and written through, is still denied.

Negative-spec: does not assert deny text, and does not cover a shell variable passed to the
program as argv (`F=<sizing> && python3 - "$F"`), which no leg analyses today.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as guard

IDS = guard._SIZING_RECORD_IDENTIFIERS
SIZING = IDS[0] + "2026-10-07-x.yaml"


def _body(*lines: str) -> str:
    return "python3 - <<'EOF'\n" + "\n".join(lines) + "\nEOF"


def test_prose_naming_a_sizing_beside_an_ungoverned_write_passes():
    cmd = (
        "F=.coordinator-local/memo-outbox/x.md && python3 - \"$F\" <<'EOF'\n"
        "import sys; p=sys.argv[1]; s=open(p).read()\n"
        f"body='''... Sizing succeeded: `{SIZING}`, arm `m_plus` ...'''\n"
        "open(p,'w').write(s+body)\nEOF"
    )
    assert not guard.is_denied_bash_write(cmd, IDS)


@pytest.mark.parametrize(
    "cmd",
    [
        _body(f"p = '{SIZING}'", "open(p, 'w').write('x')"),
        _body("from pathlib import Path", f"Path('{SIZING}').write_text('x')"),
        _body(f"msg = 'wrote {SIZING} ok'", f"open('{SIZING}', 'w').write(msg)"),
    ],
    ids=["bound-name-written", "pathlib-literal-write", "prose-beside-literal-write"],
)
def test_a_sizing_path_operand_is_still_denied(cmd):
    assert guard.is_denied_bash_write(cmd, IDS)
