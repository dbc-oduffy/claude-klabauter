"""A write target the literal reader cannot see, and the remedy that follows.

Purpose: one live-hook session on 2026-08-31 produced five consecutive denials
from `guard_doctrine_surface_bash_write` across four command shapes. All five
denials were CORRECT. Three of the five carried the wrong remedy -- "the
governed name is quoted content, not a write target. Edit the real
destination" -- about a destination the operator had just named. The two that
read correctly were the two `>`/`>>` redirects.

Two independent defects sat under that, and this file pins both.

1. `_looks_quoted_content_shaped` exonerated a command via
   `_redirect_target_token` ONLY. A bare redirect is one of several shapes
   that put a name in a real write position: `cp`/`mv`/`tee`/`install`/
   `sed -i` take their destination as an ordinary positional argument, and an
   interpreter payload takes it as a call argument. Those destinations are
   routinely quoted, so `_strip_quoted_spans` erased them and the all-quoted
   test came back True.

2. `_PY_QUOTED_LITERAL` excluded every PREFIXED string literal, `r'...'`
   with `f'...'`. The `f` exclusion is right and stays -- this module has no
   Python parser and must not treat an interpolation as a path. `r`/`b`/`u`
   are the opposite case: their literal text IS the value. On Windows, a raw
   string is the idiomatic spelling for a backslash path, so the miss landed
   squarely on `bump_outside_repo_write`'s own subject -- an absolute,
   repo-crossing target -- not merely on this guard's message.

Negative-spec: this file does NOT assert that any command is denied or
allowed. Defect 1 is message SELECTION on a path `check()` has already
decided to deny, and defect 2 is read by two callers with different verdict
logic. Verdict coverage for those lives in
`test_guard_doctrine_surface_point4_by_sink.py` and
`test_bump_outside_repo_write.py`; duplicating it here would couple this
file's failure signal to changes that have nothing to do with either defect.

The governed names below are assembled from fragments rather than written
whole. This guard reads its own test file's literals when a session edits it
through Bash, and a spelled-out governed name in this corpus is exactly the
shape it refuses -- the session that landed the fix tripped that while
writing the fix.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards._write_bump_sink_shapes import (
    extract_interpreter_payload_write_sink_targets,
)
from coordinator_core.bash_guards.guard_doctrine_surface_bash_write import (
    _looks_quoted_content_shaped,
)

_GOVERNED_MD = "CLAUDE" + ".md"
_GOVERNED_DOCTRINE = "em-operating-" + "doctrine.md"
_GOVERNED_ROLE = "agent-role-" + "dispatched.md"

#: docstring, "GOVERNED IDENTIFIER SOURCE"). Lowercased, as the caller
_IDENTIFIERS = (
    _GOVERNED_MD.lower(),
    _GOVERNED_DOCTRINE.lower(),
    _GOVERNED_ROLE.lower(),
)


_REAL_WRITE_TARGET_COMMANDS = [
    pytest.param('cat > "scratch/%s" <<EOF' % _GOVERNED_MD, id="heredoc-redirect"),
    pytest.param("echo p > scratch/%s" % _GOVERNED_DOCTRINE, id="bare-redirect"),
    pytest.param('cp README.md "scratch/%s"' % _GOVERNED_ROLE, id="cp-destination"),
    pytest.param('mv a.md "scratch/%s"' % _GOVERNED_MD, id="mv-destination"),
    pytest.param('echo x | tee "scratch/%s"' % _GOVERNED_MD, id="tee-destination"),
    pytest.param("sed -i 's/a/b/' \"scratch/%s\"" % _GOVERNED_MD, id="sed-inplace"),
    pytest.param(
        "python -c \"from pathlib import Path; Path('scratch/%s').write_text('x')\""
        % _GOVERNED_MD,
        id="payload-write-text",
    ),
    pytest.param(
        "python -c \"from pathlib import Path; Path(r'scratch/%s').write_text('x')\""
        % _GOVERNED_MD,
        id="payload-write-text-raw-string",
    ),
    pytest.param(
        "python -c \"open('scratch/%s','w').write('x')\"" % _GOVERNED_MD,
        id="payload-open-w",
    ),
]


@pytest.mark.parametrize("command", _REAL_WRITE_TARGET_COMMANDS)
def test_a_real_write_target_is_not_quoted_content(command: str) -> None:
    assert _looks_quoted_content_shaped(command, _IDENTIFIERS) is False


_QUOTED_CONTENT_COMMANDS = [
    pytest.param('git commit -m "update %s wording"' % _GOVERNED_MD, id="commit-message"),
    pytest.param('grep -rn "%s" docs/' % _GOVERNED_MD, id="grep-pattern"),
    pytest.param('echo "see %s for the rule"' % _GOVERNED_DOCTRINE, id="echo-prose"),
]


@pytest.mark.parametrize("command", _QUOTED_CONTENT_COMMANDS)
def test_quoted_prose_still_selects_the_quoted_content_remedy(command: str) -> None:
    assert _looks_quoted_content_shaped(command, _IDENTIFIERS) is True


_TARGET = "scratch/out.md"

_PREFIXES_THAT_PRESERVE_THE_VALUE = ["", "r", "R", "b", "B", "u", "U", "rb", "br", "RB"]


@pytest.mark.parametrize("prefix", _PREFIXES_THAT_PRESERVE_THE_VALUE)
def test_value_preserving_prefix_resolves_the_target(prefix: str) -> None:
    cmd = "python -c \"from pathlib import Path; Path(%s'%s').write_text('x')\"" % (
        prefix,
        _TARGET,
    )
    assert extract_interpreter_payload_write_sink_targets(cmd) == [_TARGET]


@pytest.mark.parametrize("prefix", _PREFIXES_THAT_PRESERVE_THE_VALUE)
def test_value_preserving_prefix_resolves_the_builtin_open_target(prefix: str) -> None:
    cmd = "python -c \"open(%s'%s','w').write('x')\"" % (prefix, _TARGET)
    assert extract_interpreter_payload_write_sink_targets(cmd) == [_TARGET]


@pytest.mark.parametrize("prefix", ["f", "F", "fr", "rf", "Rf", "fR"])
def test_an_interpolating_prefix_still_yields_nothing(prefix: str) -> None:
    cmd = "python -c \"from pathlib import Path; Path(%s'%s').write_text('x')\"" % (
        prefix,
        _TARGET,
    )
    assert extract_interpreter_payload_write_sink_targets(cmd) == []


def test_a_bare_identifier_butted_against_a_quote_still_yields_nothing() -> None:
    cmd = "python -c \"from pathlib import Path; Path(bar'%s').write_text('x')\"" % _TARGET
    assert extract_interpreter_payload_write_sink_targets(cmd) == []
