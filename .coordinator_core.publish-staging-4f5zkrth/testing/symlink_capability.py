"""Shared symlink-capability probe and skip marker for the test corpus.

Test call sites that create real symlinks raise ``OSError`` on a Windows host
without Developer Mode (``SeCreateSymbolicLink`` unset), and the failure then
reads as a broken test rather than an unavailable host capability. Adoption
covered 17 sites across 13 files; a further ~8 files already carried a working
inline ``try/except OSError: pytest.skip(...)`` and were deliberately left
alone — correct already, and churning them buys nothing. (An earlier count of
"23 sites across 19 files" appears in this plan's sizing evidence: it came
from an enumeration that tested only for ``skipif``-shaped guards and so
mis-read those inline ones as unguarded.) Two prior fixes at
``coordinator_core/tests/test_locked_write.py`` (blanket
``sys.platform == "win32"`` skip) and
``coordinator_core/session/tests/test_scope.py`` (a runtime probe scoped to
one file, taking a caller-supplied ``tmp_path``) each solved this once,
locally, in a different spelling. This module generalises the second shape
— the correct one, since a platform check throws away real coverage on
Windows-with-Developer-Mode — into a suite-wide helper, following the same
"fix structurally, not per-site" precedent as the suite-root ``conftest.py``
home-quarantine fixture.

``CAN_CREATE_SYMLINK`` is probed exactly once, at import time, into a
module-level constant — 23 sites sharing a fast tier means a probe-per-test
would mean a filesystem write per test if this were done naively.

Capability, never platform: the probe actually attempts a symlink rather
than branching on ``sys.platform``/``os.name``. A Windows host with
Developer Mode enabled reports able; one without it reports unable; every
POSIX host reports able.

Spec backlink: pln-one-shared-symlink-capability-62c20a § C1
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


def _probe_symlink_capability() -> bool:
    """Attempt a real directory symlink in a throwaway temp dir; report
    whether it worked. Cleans up the link and its target unconditionally —
    a cleanup race (another process/probe reaping the same throwaway
    directory concurrently) must not fail the caller.
    """
    base = Path(tempfile.mkdtemp(prefix="symlink-capability-probe-"))
    target = base / "target"
    link = base / "link"
    try:
        target.mkdir()
        os.symlink(str(target), str(link), target_is_directory=True)
        return True
    except OSError:
        return False
    finally:
        try:
            if link.is_symlink() or link.exists():
                link.unlink()
        except OSError:
            pass
        try:
            if target.exists():
                target.rmdir()
        except OSError:
            pass
        try:
            base.rmdir()
        except OSError:
            pass


CAN_CREATE_SYMLINK = _probe_symlink_capability()
"""Whether this process can create a filesystem symlink. Probed once, at
import time — never re-probed per test."""

requires_symlink_capability = pytest.mark.skipif(
    not CAN_CREATE_SYMLINK,
    reason=(
        "host cannot create symlinks (missing Windows Developer Mode / "
        "SeCreateSymbolicLink privilege)"
    ),
)
"""Shared skip marker built from :data:`CAN_CREATE_SYMLINK`.

One ``MarkDecorator`` serves both application styles the 23 sites need —
pytest applies a ``skipif`` mark identically whether it reaches a test via
module-level ``pytestmark = [requires_symlink_capability]`` or a per-test
``@requires_symlink_capability`` decoration; nothing about the object
differs between the two uses.
"""
