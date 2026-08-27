"""Tests for coordinator_core.install.door_route_signal -- the POSITIVE
route guard, with no skip path (spec backlink:
docs/dispatch/2026-08-26-every-forwarder-that-can-reach-the-door-does, C3;
carries forward docs/plans/2026-08-22-warm-engine-and-door-install-from-
published-root.md chunk C5).

`test_door_route_signal.py` proves the module's CLASSIFICATION logic against
a mocked `subprocess.run` -- useful, but it never actually calls the door
against a real warm server, so it can never catch a regression where the
real door/warm-server pair stops recording `route: warm_server` at all. This
module is the caller: it invokes `door_route_signal.read_door_route` against
the ACTUAL installed door and the ACTUAL warm server on this box, and
asserts POSITIVELY that a real hot-path invocation records
`route: warm_server` -- it goes red on `in_process`, exactly the outcome a
silent regression to always-cold would produce.

THE HOLE THIS CLOSES: the reflex here is `pytest.skip` when no warm server
is up. A skip is the green-that-means-nothing this plan exists to kill --
skipping is indistinguishable, in a CI/report summary, from "verified and
passing". This module never skips: `_ensure_warm_listener` brings a real
listener up via `coordinator_core.warm.supervisor.ensure_listener` (retried,
bounded), and if no listener answers after that bounded wait, the test calls
`pytest.fail` with a named, readable reason instead.

Negative-spec:
    - Adds no production call site -- `writes:` for this chunk names only
      this test file. `door_route_signal` itself is untouched; its
      install-verification caller and the 2026-08-22 plan's C5 backlink
      both depend on its contract staying exactly as it is.
    - Never mocks `subprocess.run` or the sink -- that coverage already
      exists in `test_door_route_signal.py`. This module is deliberately an
      end-to-end caller against the real door binary and the real warm
      server.
    - Never treats `UNRESOLVED` as a pass. Only a genuine `WARM_SERVER`
      row is PASS-worthy, matching `door_route_signal`'s own module
      docstring ("`WARM_SERVER` is the only PASS-worthy outcome").
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from coordinator_core import _settings_home
from coordinator_core.install import door_install, door_route_signal

#: The repo root this test suite actually runs against -- three levels up
#: from this file (tests/ -> install/ -> coordinator_core/ -> repo root),
#: matching `door_route_signal`'s REPO-SCOPING requirement that a caller
#: supply the `repo_root` that matches where the executing process's sink
#: write actually lands (module docstring's REPO-SCOPING section).
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The op exercised through the door -- "ping" is the exemplar op both
#: `door_route_signal`'s own module docstring (`README-posix.md`'s
#: `./door ping`) and `test_door_route_signal.py` use to exercise this exact
#: discriminator; it requires no params and is always registered.
_OP = "ping"

#: Bounded retry budget for bringing a listener up -- `ensure_listener`
#: itself NEVER WAITS FOR A BOOT (warm.supervisor's own doctrine: a
#: best-effort spawn returns None the same call), so this loop is what
#: turns "spawn requested" into "spawn observed live", not a wait baked
#: into the production call.
_LISTENER_WAIT_ATTEMPTS = 20
_LISTENER_WAIT_INTERVAL_SECS = 0.5


def _ensure_warm_listener() -> str:
    """Bring a real warm listener up for `_REPO_ROOT`, or fail with a named
    reason -- never skip (module docstring's THE HOLE THIS CLOSES).

    `warm.supervisor.ensure_listener` is fail-open and non-blocking by its
    own contract, so a single call cannot distinguish "no server, ever"
    from "server mid-boot, check again shortly". This loop polls it up to
    `_LISTENER_WAIT_ATTEMPTS` times before concluding no listener will
    answer.
    """
    from coordinator_core.warm import supervisor

    for _ in range(_LISTENER_WAIT_ATTEMPTS):
        url = supervisor.ensure_listener(_REPO_ROOT)
        if url:
            return url
        time.sleep(_LISTENER_WAIT_INTERVAL_SECS)

    pytest.fail(
        "No warm listener came up for "
        f"{_REPO_ROOT} after {_LISTENER_WAIT_ATTEMPTS} attempts "
        f"({_LISTENER_WAIT_ATTEMPTS * _LISTENER_WAIT_INTERVAL_SECS:.0f}s total) -- "
        "this test asserts a POSITIVE route guard and refuses to skip on a "
        "cold box (see module docstring); bring a warm server up for this "
        "repo (`coordinator-invoke` / `scripts/setup.py`) before re-running."
    )


def _resolve_door_path() -> Path:
    """The installed door binary this box actually resolves through PATH,
    or a named `pytest.fail` -- never a skip, mirroring `_ensure_warm_listener`.
    """
    door_path = _settings_home.settings_home() / "bin" / door_install.DOOR_INSTALLED_NAME
    if not door_path.is_file():
        pytest.fail(
            f"No installed door binary at {door_path} -- this test asserts a "
            "POSITIVE route guard against the REAL door and refuses to skip "
            "on a box with no door installed (see module docstring); run "
            "`scripts/setup.py` to install one before re-running."
        )
    return door_path


def test_hot_path_invocation_records_warm_server_route():
    """The positive route guard itself: a real door invocation of a named
    hot-path op, against a real warm server, must record `route: warm_server`
    -- and must go RED (not skip) if it instead reads back `in_process`,
    which is exactly the silent-fall-through regression this guard exists
    to catch."""
    _ensure_warm_listener()
    door_path = _resolve_door_path()

    result = door_route_signal.read_door_route(door_path, _OP, repo_root=_REPO_ROOT)

    assert result.route == door_route_signal.WARM_SERVER, (
        f"expected op {_OP!r} through door {door_path} to record "
        f"route={door_route_signal.WARM_SERVER!r}, got {result.route!r} "
        f"(entry={result.entry!r}) -- a fall-through to in_process here is "
        "the exact regression this positive guard exists to catch, not an "
        "environment problem to skip past."
    )
    assert result.entry is not None


def test_door_route_signal_recorded_route_value_set_is_pinned():
    """Companion pin (chunk C3): asserts `door_route_signal`'s recorded-route
    value set and `read_door_route`'s return contract directly, so an edit
    that redefines the thing this module measures goes red on its own
    rather than relying on a reviewer noticing.

    `WARM_SERVER`/`IN_PROCESS` are re-exports of
    `coordinator_core.telemetry.op_latency`'s own route constants (module
    docstring's re-export note) -- pinned here as literal strings because
    `read_door_route`'s classification (`route not in (WARM_SERVER,
    IN_PROCESS)` -> UNRESOLVED) depends on exactly these two values and no
    others."""
    assert door_route_signal.WARM_SERVER == "warm_server"
    assert door_route_signal.IN_PROCESS == "in_process"
    assert door_route_signal.UNRESOLVED == "unresolved"
    assert door_route_signal.DISCRIMINATOR_UNAVAILABLE == "discriminator_unavailable"

    # `DoorRouteResult` is a two-field NamedTuple: `route` (str) and `entry`
    # (the raw sink row, or None only when route is UNRESOLVED) -- pinned so
    # a field rename or reorder here is caught directly rather than only
    # showing up as an AttributeError deep in a caller.
    result = door_route_signal.DoorRouteResult(route=door_route_signal.WARM_SERVER, entry={"op": "ping"})
    assert result.route == door_route_signal.WARM_SERVER
    assert result.entry == {"op": "ping"}
    assert result._fields == ("route", "entry")
