"""test_bin_sh_polyglot_cli.py — end-to-end coverage for
check-bin-sh-polyglot.py's own `main()` (BIN-SH-POLYGLOT-INVARIANT).

Why this exists as a separate suite: `test_no_bin_polyglot_invariant.py` also
asserts the polyglot invariant, but it is an INDEPENDENT re-implementation with
its own window logic and its own exclusion set — it never invokes the CLI. It
therefore could not have caught the original defect this suite guards against
(the CLI's self-skip compared `os.path.abspath(filepath)` against its own path
only, so it false-positived on its sibling guard `check-sh-suffix-polyglot.py`,
whose module docstring quotes the trampoline inside the 20-line header window
under a non-`/bin/sh` shebang), and would not catch a regression of the fix
either. That bug shipped and survived on a clean tree precisely because nothing
exercised the CLI end-to-end.

Tests run against a throwaway scratch `bin/` directory holding copies of the
real guard files, never the live `coordinator/bin/` — the CLI resolves its scan
domain from its own `__file__`, so copying it is sufficient and the suite stays
independent of whatever else lands in the real directory. No git repo is needed:
the no-flag mode enumerates via `os.listdir`, not `git diff --cached`.

The offending-file fixture is built from the CLI module's own `TRAMPOLINE`
constant rather than a literal copy, so this file never carries the trampoline
in its own header window (which would make it an offender in the sibling
invariant suite) and cannot drift from the string actually being matched.

Spec backlink: docs/plans/2026-06-18-bin-cli-sh-shebang-polyglot.md
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
GUARD = os.path.join(_BIN_DIR, "check-bin-sh-polyglot.py")
SIBLING_GUARD = os.path.join(_BIN_DIR, "check-sh-suffix-polyglot.py")
PYTHON = sys.executable


def _load_guard_module():
    """Import the hyphen-named CLI by path — not importable by dotted name, and
    side-effect-free at import (`main()` is `__main__`-guarded)."""
    spec = importlib.util.spec_from_file_location("_cli_check_bin_sh_polyglot", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scratch_bin(tmp_path):
    """A scratch `bin/` holding copies of both real guards — the exact pairing
    that triggered the original false positive."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy(GUARD, str(bin_dir / "check-bin-sh-polyglot.py"))
    shutil.copy(SIBLING_GUARD, str(bin_dir / "check-sh-suffix-polyglot.py"))
    # The guards bootstrap `cc_invoke` off their OWN `<bin>/lib`, so a scratch
    # bin without one dies on ModuleNotFoundError before it can classify
    # anything — a green that never ran. Copied, not symlinked: the guard
    # scans `bin/` itself, and `lib/` being a real subdirectory is what its
    # "does NOT scan subdirectories" negative-spec is stated against.
    shutil.copytree(os.path.join(_BIN_DIR, "lib"), str(bin_dir / "lib"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    return bin_dir


def _run_guard(bin_dir):
    return subprocess.run(
        [PYTHON, os.path.join(str(bin_dir), "check-bin-sh-polyglot.py")],
        cwd=str(bin_dir), capture_output=True, text=True, **no_console_creationflags(),
    )


def test_guard_pair_alone_is_clean(tmp_path):
    """A `bin/` containing only the two trampoline-quoting guards must exit 0.
    Both quote the trampoline as data under a `#!/usr/bin/env python3` shebang;
    neither is a polyglot CLI. Drop either from `_GUARD_SELF_SKIP_BASENAMES` and
    this goes red — that is the original bug."""
    bin_dir = _scratch_bin(tmp_path)
    r = _run_guard(bin_dir)
    assert r.returncode == 0, (
        "expected exit 0, got {}\nstdout: {}\nstderr: {}".format(
            r.returncode, r.stdout, r.stderr
        )
    )
    assert "OK" in r.stdout


def test_real_offender_still_fires(tmp_path):
    """Companion to the test above: proves its green is a real pass and not a
    scan that silently found nothing to classify."""
    guard = _load_guard_module()
    bin_dir = _scratch_bin(tmp_path)
    (bin_dir / "some-tool").write_text(
        "#!/usr/bin/env python3\n{}\nimport sys\n".format(guard.TRAMPOLINE),
        encoding="utf-8",
    )
    r = _run_guard(bin_dir)
    assert r.returncode == 1, (
        "expected exit 1, got {}\nstdout: {}".format(r.returncode, r.stdout)
    )
    assert "BIN-SH-POLYGLOT-INVARIANT" in r.stdout
    assert "some-tool" in r.stdout
