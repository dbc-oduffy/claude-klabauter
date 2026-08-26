"""DR-363: no agent-facing advisory renders the operator's username.

The three B7 violations DR-363 measured (`grep-via-bash-guard-fire`,
`multiprobe-banner-fire`, `plumbing-and-loops-fire`) were one root cause with
three sites: each advisory interpolates `_bt_python3_invocation()`, which used to
return the resolved absolute interpreter. On a stock Windows install that path
runs through the user's home and carries their username.

The PM REJECTED the record's recommendation to widen B7's exemption to cover it,
and set the requirement instead: no username in the message. These tests pin that
requirement at the choke point, because **B7 going green on those rows no longer
proves the property holds** — B7's lens is `redaction_tokens()` over the publish
store, which is a statement about published bytes, and there were never any
published bytes here. A future edit that reintroduces an absolute home path would
be caught by nothing else.

NEGATIVE SPEC — the property is NOT "the advisory contains no absolute path".
An interpreter outside the user's home has no username to remove and is rendered
verbatim; the test for that case asserts it is left alone, so a later "fix" that
blanket-strips paths fails here rather than silently breaking every advisory on a
system-wide Python install.
"""

import os

import pytest

from coordinator_core.bash_guards.dispatch_checks import (
    _bt_python3_invocation,
    _bt_render_interpreter_path,
)


def _home_interpreter() -> str:
    return os.path.join(
        os.path.expanduser("~"), "AppData", "Local", "Programs", "Python", "Python313", "python.exe"
    )


def test_home_relative_interpreter_drops_the_username():
    rendered = _bt_render_interpreter_path(_home_interpreter())
    username = os.path.basename(os.path.expanduser("~"))
    assert username not in rendered, (
        "the advisory would put the operator's username in front of an agent: %r" % rendered
    )
    assert rendered.startswith('"$HOME/'), rendered


def test_interpreter_outside_home_is_left_alone():
    """The negative control. Blanket-stripping paths would break every advisory
    on a system-wide install, so the non-home case must survive untouched."""
    outside = os.path.join("C:" + os.sep, "Program Files", "Python313", "python.exe")
    rendered = _bt_render_interpreter_path(outside)
    assert "$HOME" not in rendered
    assert "Program Files" in rendered


def test_home_rendering_uses_expandable_quotes_not_literal_ones():
    """`shlex.quote` would emit single quotes, which make `$HOME` literal and the
    advisory unrunnable. The whole value of these advisories is that they can be
    pasted and run."""
    rendered = _bt_render_interpreter_path(_home_interpreter())
    assert not rendered.startswith("'"), "single quotes would suppress $HOME expansion: %r" % rendered
    assert "\\" not in rendered, "backslashes escape inside a double-quoted shell string: %r" % rendered


def test_live_invocation_carries_no_username():
    """The end-to-end property on THIS box, through the cache path the advisories
    actually call."""
    username = os.path.basename(os.path.expanduser("~"))
    assert username not in _bt_python3_invocation()


def test_rendering_stays_paste_runnable_by_construction():
    """An advisory that does not run is worse than none — DR-363 § Options, and
    the reason a bare `python3` stayed ruled out even after the rejection.

    This asserts the two properties that make the string runnable rather than
    running it. A test that actually spawned a shell here would add an unmarked
    spawning test and redden `test_no_new_spawning_tests.py`, so the execution
    check was done by hand instead, on 2026-08-26, in both hosts this fleet runs:

        bash:       "$HOME/AppData/.../python.exe" -c 'print(...)'   -> ran
        PowerShell: & "$HOME/AppData/.../python.exe" -c "print(...)"  -> ran

    `$HOME` is what makes one rendering serve both — Git Bash exports it and
    PowerShell defines it as an automatic variable."""
    rendered = _bt_render_interpreter_path(_home_interpreter())
    assert rendered.startswith('"$HOME/') and rendered.endswith('"')
    assert "/" in rendered and "\\" not in rendered


def test_rendering_version_is_folded_into_the_cache_key():
    """A warm cross-process cache would otherwise keep serving the pre-DR-363
    string, username and all, until `_machine_local.py` happened to change."""
    from coordinator_core.bash_guards.dispatch_checks import (
        _BT_INTERPRETER_RENDERING_VERSION,
        _bt_python3_invocation_cache_key,
    )

    key = _bt_python3_invocation_cache_key()
    if key is None:
        pytest.skip("pyresolve unavailable; this box never caches")
    assert _BT_INTERPRETER_RENDERING_VERSION in key
