"""Oracle for `check-mcp-versions.py::main::_npm_latest`.

The claim: `npm view` takes exactly ONE package-spec per invocation; a second positional does
not error, it parses as a FIELD -- so a batched form (`npm view pkgA pkgB version`) would
silently query the wrong thing rather than fail loudly. `npm` is a third-party binary, not ours
to change or vendor a parser for.

This is the weak oracle flagged in the register write-up: it needs `npm` on PATH, so it degrades
to "passes when runnable" rather than proving the claim unconditionally. Shipped as the OFFLINE
variant -- read `npm view --help`'s own usage line rather than actually querying the registry, so
it needs no network and stays cheap -- but it still spawns the `npm` binary itself, so it is
skip-guarded on `shutil.which("npm")` and marked for the cadence tier alongside every other
process-spawning test.

Bound to the site by `_ORACLE_CLAIMS` in
`coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py`.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

# Usage grammar per npm's own --help: a bracketed, non-ellipsised `<package-spec>` means
# "zero or one", while a trailing `...` (as on `<field>[.subfield]...`) means "any number".
# The claim under test is exactly the absence of `...` after `<package-spec>`.
_USAGE_LINE_RE = re.compile(r"^npm view .*$", re.MULTILINE)
_PACKAGE_SPEC_RE = re.compile(r"<package-spec>(\.\.\.)?")


def test_npm_view_usage_names_exactly_one_package_spec():
    npm_path = shutil.which("npm")
    if npm_path is None:
        pytest.skip("npm not on PATH -- oracle needs the real binary's --help text")

    # Resolved to the PATHEXT-suffixed sibling (npm.cmd on Windows), not the bare "npm" --
    # a bare name raises FileNotFoundError under CreateProcess without shell=True, which is
    # exactly the live Windows-portability gap this repo's own cruft_sweep.py and
    # find_polluter.py document at their own npm call sites.
    proc = subprocess.run(
        [npm_path, "view", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, (
        f"npm view --help exited {proc.returncode} -- cannot read the usage line this "
        "oracle depends on"
    )

    usage_match = _USAGE_LINE_RE.search(proc.stdout)
    assert usage_match, (
        "npm view --help no longer prints a 'npm view ...' usage line in the shape this "
        f"oracle expects; full output:\n{proc.stdout}"
    )
    usage_line = usage_match.group(0)

    spec_match = _PACKAGE_SPEC_RE.search(usage_line)
    assert spec_match, (
        f"npm view --help's usage line no longer names <package-spec> at all: {usage_line!r} "
        "-- the exemption this oracle discharges can no longer be verified against npm's own "
        "documented grammar."
    )
    assert spec_match.group(1) is None, (
        f"npm view --help's usage line now shows <package-spec>... (repeatable): {usage_line!r} "
        "-- npm view can take more than one package-spec per invocation, so the single-spec "
        "exemption for check-mcp-versions.py::_npm_latest no longer holds and the caller "
        "should batch."
    )
