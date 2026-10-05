"""Hard per-test deadline that survives a GIL-holding C call.

pytest-timeout's thread method fires from a Python `threading.Timer`, which
needs the GIL; a test stuck inside one long C call (catastrophic `re`, a huge
`sorted`/`sum`) starves it and `--timeout` never ends the run. This arms
`faulthandler.dump_traceback_later(..., exit=True)`, a C-level watchdog thread
that needs no GIL, a grace period after pytest-timeout's own deadline, so the
normal timeout report still wins whenever it can fire.
"""

from __future__ import annotations

import faulthandler

import pytest

GRACE_SECONDS = 5.0


def _deadline(item: pytest.Item) -> float:
    marker = item.get_closest_marker("timeout")
    if marker is not None and marker.args:
        return float(marker.args[0])
    if marker is not None and "timeout" in marker.kwargs:
        return float(marker.kwargs["timeout"])
    try:
        configured = item.config.getoption("timeout")
    except ValueError:
        return 0.0
    return float(configured or 0.0)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem):
    limit = _deadline(item)
    if limit <= 0:
        return (yield)
    faulthandler.dump_traceback_later(limit + GRACE_SECONDS, exit=True)
    try:
        return (yield)
    finally:
        faulthandler.cancel_dump_traceback_later()
