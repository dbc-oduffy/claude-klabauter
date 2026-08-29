from __future__ import annotations
"""
test_harvest_deferrals_bin_dir_import_reachable.py — regression test for
coordinator-harvest-deferrals' sibling-module import of `_queue_append_locator`.

Spec backlink: this repo's 2026-08-29 review-finding sweep over the
lazy-bootstrap pass on `coordinator/bin/*.py`, Finding 1 (CRITICAL,
EM-confirmed at source).

`_queue_append_locator.py` lives in `coordinator/bin/` itself, NOT in
`coordinator/bin/lib/` — `import lib` (this CLI's own bootstrap, see
`_bootstrap_engine()`) only puts `coordinator/bin/lib` on `sys.path`, never
`coordinator/bin`. A `python3 coordinator-harvest-deferrals` invocation gets
`coordinator/bin` on `sys.path[0]` implicitly (a script's own directory), and
the warm door's `invoke_from_argv.py` also puts the bin directory on
`sys.path` — masking the gap under both of those entry points. In-process
dispatch (`workstream_complete.apply._load_cli_module`, via
`importlib.util.spec_from_file_location`) confers NEITHER of those.

This test reproduces the in-process-dispatch shape in a FRESH child
interpreter (never in-process inside the pytest worker) precisely because
pytest's own package-rooted collection (`coordinator/bin/tests/__init__.py`
forces `coordinator/bin` to be pytest's import root) leaves `coordinator/bin`
on `sys.path` for the whole worker process — any in-process attempt to strip
it back out is fighting residue from pytest's own collection machinery, not
reproducing the actual bug. A subprocess launched with `-I` (isolated mode:
no `PYTHONPATH`, no script directory on `sys.path[0]`, no user site-packages)
gives a `sys.path` that genuinely does not contain `coordinator/bin`, matching
what `workstream_complete.apply._load_cli_module` hands the interpreter.

The child script also pre-warms `sys.modules["lib"]` (a transient, scoped
`sys.path` insert of `coordinator/bin`, immediately reverted) before loading
the CLI module — matching the real warm-server condition this finding
describes: `import lib` (this CLI's own first bootstrap statement) resolves
from a PRIOR successful dispatch's cache hit in any process that has served
at least one CLI request through the warm door (`invoke_from_argv.py`) or a
direct `__main__` run, while `_queue_append_locator` — a module nothing else
in the process ever imports — has no such cache entry and depends entirely
on `coordinator/bin` being reachable at the moment `_bootstrap_engine()`
reaches it. Skipping the pre-warm step makes `import lib` itself fail first
(masking the finding this test targets) or, on a box with pywin32 installed,
silently resolve to an unrelated same-named `win32/lib` namespace package —
neither of which exercises the regression.

Negative-spec: this test would pass vacuously if `coordinator/bin` reached
the child's `sys.path` some other way (a `PYTHONPATH` inherited from the
parent, a `.pth` file, the script's own directory). `-I` closes all three;
the child script also asserts `coordinator/bin` is absent from `sys.path`
before attempting the load, so a future interpreter-flag regression fails
loud here instead of passing silently.

Run with: python3 -m pytest test_harvest_deferrals_bin_dir_import_reachable.py
"""

import os
import subprocess
import sys
import tempfile

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_THIS_DIR)  # coordinator/bin
_HARVEST_CLI = os.path.join(_BIN_DIR, "coordinator-harvest-deferrals.py")

_SUBPROCESS_TIMEOUT_SECS = 30

_CHILD_SCRIPT = '''
import importlib.util
import os
import sys
from importlib.machinery import SourceFileLoader

harvest_cli = {harvest_cli!r}
bin_dir = os.path.dirname(os.path.abspath(harvest_cli))

assert bin_dir not in [os.path.abspath(p) for p in sys.path if p], (
    "isolated child unexpectedly has coordinator/bin on sys.path: " + repr(sys.path)
)

# Pre-warm sys.modules["lib"] the way any process that has served one prior
# CLI dispatch through the warm door (or a direct __main__ run) already has
# -- a transient, scoped insert, reverted immediately after. See module
# docstring: this is what lets "import lib" succeed without also making
# "_queue_append_locator" trivially resolvable, which is the whole point.
sys.path.insert(0, bin_dir)
import lib  # noqa: F401
sys.path.remove(bin_dir)
assert "lib" in sys.modules
assert bin_dir not in [os.path.abspath(p) for p in sys.path if p], (
    "pre-warm step failed to revert coordinator/bin off sys.path"
)

loader = SourceFileLoader("harvest_module_under_test", harvest_cli)
spec = importlib.util.spec_from_file_location(
    "harvest_module_under_test", harvest_cli, loader=loader
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

{body}
'''


def _run_isolated(body: str) -> subprocess.CompletedProcess:
    """Run `body` (source referencing the loaded `module`) in a genuinely
    isolated child interpreter (`-I`: no PYTHONPATH, no script-dir on
    `sys.path[0]`, no user site-packages) with a script file on disk --
    never `-c`, so no shell-quoting layer can smuggle `coordinator/bin` back
    onto `sys.path`.
    """
    script = _CHILD_SCRIPT.format(harvest_cli=_HARVEST_CLI, body=body)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(script)
        script_path = fh.name
    try:
        return subprocess.run(
            [sys.executable, "-I", script_path],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    finally:
        os.unlink(script_path)


def test_bootstrap_reaches_queue_append_locator_without_implicit_bin_dir_on_path() -> None:
    """`_bootstrap_engine()` must resolve `_queue_append_locator` even when
    `coordinator/bin` is not already on `sys.path` -- the exact condition
    in-process dispatch presents, and the exact condition under which the
    lazy-bootstrap sweep silently reintroduced a 2026-07-27 fix (module
    docstring comment at the `_queue_append_locator` import site).
    """
    result = _run_isolated(
        "module._bootstrap_engine()\n"
        "assert 'find_cli_cmd' in module.__dict__\n"
        "assert callable(module.find_cli_cmd)\n"
        "print('OK')\n"
    )
    assert "ModuleNotFoundError" not in result.stderr, (
        f"_bootstrap_engine() raised under in-process dispatch conditions:\n{result.stderr}"
    )
    assert result.returncode == 0, (
        f"child exited {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "OK" in result.stdout


def test_resolve_cli_cmd_mid_module_entry_does_not_raise() -> None:
    """A mid-module entry point (`_resolve_cli_cmd`, never `main()`) must
    independently trigger a bootstrap that reaches `_queue_append_locator` --
    the invisible-to-`--help`/import/py_compile shape this finding names.
    """
    result = _run_isolated(
        "module._resolve_cli_cmd('coordinator-queue-append')\n"
        "print('OK')\n"
    )
    assert "ModuleNotFoundError" not in result.stderr, (
        f"_resolve_cli_cmd() raised via mid-module bootstrap:\n{result.stderr}"
    )
    assert result.returncode == 0, (
        f"child exited {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "OK" in result.stdout
