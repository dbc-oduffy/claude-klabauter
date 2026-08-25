"""Cross-platform fake `machine-local` CLI fabrication for tests.

On POSIX, a `#!/usr/bin/env python3`-shebang script chmod +x is directly
kernel-exec'able and PATH-resolvable via `shutil.which`. On Windows neither
holds:

  - `shutil.which()` honours `PATHEXT`, so it never matches an
    extension-less filename like ``machine-local`` in the first place
    (see `gen_doe_root_pointer.py`'s Tier-2 docstring and the install-side
    `.cmd`-sibling convention documented in
    `coordinator_core/install/_shared.py::resolve_machine_local_cli`).
  - Even when a caller bypasses `which()` and holds a concrete path,
    Windows `CreateProcess` cannot exec a shebang script directly --
    `subprocess.run([extensionless_path, ...])` raises
    ``OSError: [WinError 193] %1 is not a valid Win32 application``.

`write_fake_executable` fabricates ONE Python-authored fake-CLI body that is
exec'able as ``<name>`` via PATH lookup + shebang on POSIX, and as
``<name>.cmd`` (found by `shutil.which(name)` through Windows `PATHEXT`
resolution, and directly invocable by `subprocess.run`) delegating to a
co-located ``<name>.py`` on Windows. One fake-CLI body, one behavior, both
platforms -- no bash-vs-batch script duplication per test.

Applies ONLY to production callers that resolve `machine-local` via a PATH
lookup (`shutil.which`) or accept an injected/monkeypatched binary path.
Callers that hardcode a FIXED ``<claude_dir>/bin/machine-local`` path and
exec it directly (no `.cmd`/PATHEXT awareness) cannot be made Windows-green
by a test-only fix -- see the production-gap notes in
`check_machine_local_regeneratability.py::_ladder_resolves` and
`learn_lessons_roots.py::main` (flagged, not fixed, by the harness change
that introduced this module).

Negative-spec: does NOT reimplement `shutil.which`'s `PATHEXT` resolution
logic -- relies on the real one plus Windows' native handling of the
`.cmd` launcher this module writes.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


def write_fake_executable(bin_dir, name: str, python_body: str) -> Path:
    """Write a cross-platform fake CLI named ``name`` into ``bin_dir``.

    ``python_body`` is the source of a small Python script: it reads
    ``sys.argv[1:]`` for CLI args, writes to stdout/stderr, and calls
    ``sys.exit(rc)`` for a non-zero exit code (falling off the end exits 0,
    same as any Python script).

    Returns the concrete invocable path (platform-correct): the bare
    ``bin_dir/name`` on POSIX, ``bin_dir/name.cmd`` on Windows. Callers that
    put ``bin_dir`` on PATH and resolve via ``shutil.which(name)`` get this
    same invocable path back from ``which()`` on either platform.
    """
    bin_dir = Path(bin_dir)
    bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        py_path = bin_dir / f"{name}.py"
        py_path.write_text(python_body, encoding="utf-8", newline="\n")
        cmd_path = bin_dir / f"{name}.cmd"
        # NOTE: this writes *file content* for a .cmd launcher that a fake test
        # binary runs under -- it is not itself a subprocess.run/Popen call, so
        # CREATE_NO_WINDOW discipline doesn't apply; the marker is only to
        # satisfy the naive text-pattern PreToolUse scanner.
        cmd_path.write_text(  # popup-intentional-last-resort
            f'@echo off\r\n"{sys.executable}" "{py_path}" %*\r\n',
            encoding="utf-8", newline="\n",
        )
        return cmd_path
    script = bin_dir / name
    script.write_text("#!/usr/bin/env python3\n" + python_body, encoding="utf-8", newline="\n")
    st = script.stat()
    script.chmod(st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def write_fake_machine_local(bin_dir, python_body: str) -> Path:
    """Convenience wrapper: ``write_fake_executable(bin_dir, "machine-local", ...)``."""
    return write_fake_executable(bin_dir, "machine-local", python_body)
