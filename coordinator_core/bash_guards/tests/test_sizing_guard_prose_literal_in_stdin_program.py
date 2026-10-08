"""The state/sizings shell-write rule on a stdin-program heredoc that only QUOTES the path.

A literal with whitespace in it is prose naming the path, not a path operand: a body that writes
an ungoverned file while its message text names a sizing passes. A path operand, a name bound to
one, or a sizing passed as argv (`F=<sizing> && python3 - "$F"`) and written through is denied.

Negative-spec: does not assert deny text.
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


def _argv(arg_line: str, *lines: str) -> str:
    return f"F={SIZING} && python3 - {arg_line} <<'EOF'\n" + "\n".join(lines) + "\nEOF"


@pytest.mark.parametrize(
    "cmd",
    [
        _argv('"$F"', "import sys; p=sys.argv[1]; s=open(p).read()", "open(p,'w').write(s+'x')"),
        _argv('"$F"', "import sys", "open(sys.argv[1], 'a').write('x')"),
        _argv('"$F"', "import sys, pathlib", "q = pathlib.Path(sys.argv[1])", "q.write_text('x')"),
        _argv('"$F"', "import sys", "a = sys.argv[1]", "b = a", "open(b, 'w').write('x')"),
        _argv('"$F"', "import sys, shutil", "shutil.copy('/tmp/x', sys.argv[1])"),
        _argv('x.md "$F"', "import sys", "src, dst = sys.argv[1], sys.argv[2]", "open(dst,'w').write('x')"),
        _argv('"$F"', "from sys import argv", "open(argv[1],'w').write('x')"),
        _argv('"$F"', "import sys", "for a in sys.argv[1:]:", "    open(a,'w').write('x')"),
    ],
    ids=[
        "bound-name", "direct-index", "pathlib-bound", "rebound-name",
        "shutil-copy", "tuple-unpack", "from-import-fails-closed", "slice-fails-closed",
    ],
)
def test_a_sizing_passed_as_argv_and_written_through_is_denied(cmd):
    assert guard.is_denied_bash_write(cmd, IDS)


@pytest.mark.parametrize(
    "cmd",
    [
        _argv('"$F"', "import sys, yaml", "print(yaml.safe_load(open(sys.argv[1])))"),
        _argv(
            '"$F" out.md',
            "import sys",
            "d = open(sys.argv[1]).read()",
            "open(sys.argv[2], 'w').write(d)",
        ),
    ],
    ids=["read-only", "read-sizing-write-elsewhere"],
)
def test_a_sizing_passed_as_argv_and_only_read_passes(cmd):
    assert not guard.is_denied_bash_write(cmd, IDS)
