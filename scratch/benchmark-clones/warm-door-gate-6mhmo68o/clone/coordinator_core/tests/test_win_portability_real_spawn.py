"""Real-process spawn split-out from test_win_portability.py.

SPAWN-RATCHET Rule 2/4 (coordinator_core/tests/test_no_new_spawning_tests.py):
exactly one test in test_win_portability.py spawns a real process --
test_bare_subprocess_run_reproduces_the_original_break, which reproduces the
`subprocess.run(stdout=<fileno-less StringIO>)` defect against a REAL child
(sys.executable), not a faked one -- faking the spawn would destroy the
test's entire point. The other ~100 tests in that file spawn nothing, so a
module-level `pytest.mark.cadence` there would drag them out of the fast tier
for no reason. This sibling module carries just the one real-spawn test,
tiered onto cadence on its own.

See test_win_portability.py for the rest of the win_portability test suite.
"""

from __future__ import annotations

import io
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _write_child_script(tmp_path, stdout_text="out-line\n", stderr_text="err-line\n", returncode=0):
    script = tmp_path / "run_forwarding_child.py"
    script.write_text(
        "import sys\n"
        f"sys.stdout.write({stdout_text!r})\n"
        f"sys.stderr.write({stderr_text!r})\n"
        f"sys.exit({returncode})\n"
    )
    return str(script)


def test_bare_subprocess_run_reproduces_the_original_break(tmp_path):
    """Proves the defect exists before asserting the fix: a bare
    subprocess.run call against a fileno-less StringIO target raises
    io.UnsupportedOperation -- the exact exception whose bare str() collapses
    to the single word "fileno" in workday_complete.apply's failed[] entries."""
    child = _write_child_script(tmp_path)
    buf = io.StringIO()
    with pytest.raises(io.UnsupportedOperation):
        subprocess.run([sys.executable, child], stdout=buf, stderr=buf)
