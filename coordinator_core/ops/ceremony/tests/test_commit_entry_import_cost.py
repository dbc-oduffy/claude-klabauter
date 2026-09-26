
from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

#: `commit_trailers`. `compute_machine` resolves `$COORDINATOR_MACHINE` and the
_FORBIDDEN_PREFIXES = (
    "asyncio",
    "coordinator_core.ops.fleet.archive_terminal_handoffs",
    "select",
    "selectors",
    "socket",
    "ssl",
)

_PROBE = textwrap.dedent(
    """
    import json
    import sys

    # Lazy ops deliberately left UNARMED — no COORDINATOR_CORE_LAZY_OPS env
    # var, no sys._coordinator_core_lazy_ops attribute. Arming it sweeps the
    # whole ops package in eagerly and the assertion would pass vacuously.
    import coordinator_core.git.commit  # noqa: F401

    sys.stdout.write(json.dumps(sorted(sys.modules)))
    """
)


def _loaded_modules() -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, (
        "import probe failed to import the commit entry point at all -- that is a "
        f"broken import, not an import-cost regression:\n{proc.stderr}"
    )
    return json.loads(proc.stdout)


def test_commit_entry_import_does_not_pull_asyncio() -> None:
    loaded = _loaded_modules()
    offenders = [
        name
        for name in loaded
        if any(
            name == prefix or name.startswith(prefix + ".")
            for prefix in _FORBIDDEN_PREFIXES
        )
    ]
    assert not offenders, (
        "importing the commit entry point pulled in modules the commit hot path must "
        f"not pay for: {', '.join(offenders)}.\n"
        "The archival sweep that used to defer these below a cadence gate was "
        "removed from the commit path entirely (PM ruling 2026-08-27), so there "
        "is no longer any in-pipeline caller entitled to them at all -- a hit "
        "here is a NEW dependency on the hot path, not a deferral that regressed."
    )


def test_hostname_resolution_survives_the_socket_deferral() -> None:
    """Same move on `machine_resolver`: the hostname rung still resolves, so the
    fallback chain below it (`$HOSTNAME`, then `"unknown"`) is not silently
    doing the work now."""
    from coordinator_core.machine_resolver import _hostname_short

    host = _hostname_short()
    assert host, (
        "_hostname_short() returned no hostname -- if the deferred `import "
        "socket` failed it would raise, so an empty result means gethostname "
        "itself is failing and compute_machine has quietly fallen through to "
        "$HOSTNAME or 'unknown'."
    )
    assert "." not in host, f"expected a domain-stripped short name, got {host!r}"
