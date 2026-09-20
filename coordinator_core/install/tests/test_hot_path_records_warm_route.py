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

#: This suite is a read-only oracle against the LIVE warm plane, so it opts out
#: of `conftest._quarantine_real_home`.
#:
#: Capturing `_REAL_ENGINE_ROOT` and `_REAL_DOOR_PATH` at collection time --
#: which this file already did -- closes only half the hole. `ensure_listener`
#: re-resolves the service directory INTERNALLY, off the quarantined
#: `COORDINATOR_WARM_RUNTIME_BASE`, so `discovery_path()` landed in a per-test
#: temp dir and no listener on the box could ever be visible here. The guard was
#: unsatisfiable by construction: red on every box, forever, for a reason that
#: had nothing to do with the route it asserts.
#:
#: Worse than red. Each poll's `should_spawn` answered True inside the
#: quarantine, so a failing run spawned real detached supervisors against the
#: real engine root -- exactly the litter `ensure_listener`'s own docstring
#: warns about.
#:
#: Negative spec: nothing here may WRITE under the real home. The marker means
#: "resolve the real home" for a read-only assertion; a test that mutates live
#: machine config under it reintroduces the bug the quarantine exists to stop.
pytestmark = pytest.mark.real_home

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


def _capture_real_engine_root():
    """Resolve the stamped engine root ONCE, at collection time, under the
    REAL (un-quarantined) HOME.

    Same capture-before-quarantine shape as `coordinator_core/conftest.py`'s
    own `_REAL_USER_SITE` and `_capture_real_doe_root`, for the same class of
    reason. `resolve_engine_root_for_install` walks a home-anchored ladder
    (`machine-local repos.claude_klabauter`, then the published-mirror probe),
    and conftest monkeypatches HOME/USERPROFILE per test -- so resolving it
    INSIDE a test yields `kind='none'` on a box that demonstrably has a
    published engine root, and this test would fail for the environment
    rather than for the regression it exists to catch.
    """
    try:
        from coordinator_core.install.engine_root_for_install import (
            resolve_engine_root_for_install,
        )

        return resolve_engine_root_for_install()
    except Exception:  # pragma: no cover - defensive, mirrors conftest's own
        return None


_REAL_ENGINE_ROOT = _capture_real_engine_root()


def _capture_real_door_path():
    """The installed door binary's path, resolved ONCE at collection time
    under the REAL (un-quarantined) HOME -- same reason as
    `_capture_real_engine_root` above. `settings_home()` is home-anchored,
    so resolving it inside a test points at the per-test quarantine
    directory, where no door has ever been installed and never will be.
    """
    try:
        return _settings_home.settings_home() / "bin" / door_install.DOOR_INSTALLED_NAME
    except Exception:  # pragma: no cover - defensive, mirrors conftest's own
        return None


_REAL_DOOR_PATH = _capture_real_door_path()


def _ensure_warm_listener() -> str:
    """Bring a real warm listener up against the STAMPED ENGINE ROOT, or
    fail with a named reason -- never skip (module docstring's THE HOLE
    THIS CLOSES).

    THE ENGINE ROOT IS NOT `_REPO_ROOT`, corrected 2026-08-27. This loop
    used to pass `_REPO_ROOT` as `ensure_listener`'s argument. That
    parameter is named `engine_root` and is gated on a stamped one before
    any of its three branches -- "an unstamped tree is not an engine ...
    so a listener spawned against it could only ever answer
    `_serve_line`'s untrusted-caller refusal". Claude-klabauter is a
    CONSUMER of the engine, never a host of it (`claude-klabauter` is the
    published twin that carries the stamp), so the old call could only
    ever return `None`, and this test could only ever fail here -- not
    because the hot path had regressed, but because it was asking an
    unstampable tree to host. The repo whose work is done stays
    `_REPO_ROOT`; only the HOST moves.

    `warm.supervisor.ensure_listener` is fail-open and non-blocking by its
    own contract, so a single call cannot distinguish "no server, ever"
    from "server mid-boot, check again shortly". This loop polls it up to
    `_LISTENER_WAIT_ATTEMPTS` times before concluding no listener will
    answer.
    """
    from coordinator_core.warm import supervisor

    resolved = _REAL_ENGINE_ROOT
    if resolved is None or resolved.root is None:
        pytest.fail(
            "No engine root resolved for this box at collection time "
            f"(resolved={resolved!r}) -- "
            "this test asserts a POSITIVE route guard and refuses to skip; "
            "a box with no published engine root cannot host the warm "
            "server the hot path routes through."
        )

    for _ in range(_LISTENER_WAIT_ATTEMPTS):
        url = supervisor.ensure_listener(resolved.root)
        if url:
            return url
        time.sleep(_LISTENER_WAIT_INTERVAL_SECS)

    pytest.fail(
        "No warm listener came up for engine root "
        f"{resolved.root} after {_LISTENER_WAIT_ATTEMPTS} attempts "
        f"({_LISTENER_WAIT_ATTEMPTS * _LISTENER_WAIT_INTERVAL_SECS:.0f}s total) -- "
        "this test asserts a POSITIVE route guard and refuses to skip on a "
        "cold box (see module docstring); bring a warm server up for this "
        "repo (`coordinator-invoke` / `scripts/setup.py`) before re-running."
    )


def _resolve_door_path() -> Path:
    """The installed door binary this box actually resolves through PATH,
    or a named `pytest.fail` -- never a skip, mirroring `_ensure_warm_listener`.
    """
    door_path = _REAL_DOOR_PATH
    if door_path is None or not door_path.is_file():
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


#: Template for the stubbed door image (chunk C3, E4/staff-eng's settlement):
#: a minimal executable forwarder, NOT a real built door -- it never routes
#: through `door_install_posix_build.build_or_advise`, so a box with no
#: prebuilt image and no toolchain is not permanently red on a module
#: contractually forbidden to skip. The ROUTE STAMP stays real: this script
#: forwards the op through `coordinator_core.warm.client.try_warm_dispatch`
#: against the SAME resolved engine root the real listener was brought up
#: against (`_REAL_ENGINE_ROOT`), so the resident server itself stamps the
#: sink row via `warm.server._declare_execution_route` -- nothing here
#: fabricates a route value. The shebang pins `sys.executable` (this
#: process's own interpreter) rather than `#!/usr/bin/env python3`, so the
#: stub cannot land on a system interpreter lacking this checkout's
#: `coordinator_core` package on its path.
#:
#: `_caller_cwd` is stamped into the request envelope deliberately -- the
#: real native door adds this (`door_core.c`), and `op_latency._write_entry`
#: resolves the sink it writes to from the envelope's `_origin_worktree`,
#: else `ipc.resolve_caller_cwd`'s `_caller_cwd`, else -- silently -- the
#: EXECUTING (server) process's own cwd (`door_route_signal`'s module
#: docstring, REPO-SCOPING section). Omitting it here would not make the
#: route stamp fake -- it would make this test read a real row at the
#: WRONG repo_root (the resident server's own cwd, not `_REPO_ROOT`), which
#: is indistinguishable from `UNRESOLVED` to a caller scoped to `_REPO_ROOT`.
_STUB_DOOR_IMAGE_TEMPLATE = """#!{python_executable}
import os
import sys

# The stubbed door image is invoked as a SUBPROCESS of this pytest run, so it
# inherits `PYTEST_CURRENT_TEST` (pytest stamps it for the duration of every
# test). `coordinator_core.warm.client._try_warm_dispatch_inner` gates on
# exactly that pair -- `PYTEST_CURRENT_TEST` set AND
# `COORDINATOR_WARM_RUNTIME_BASE` unset means "this looks like a test
# process, refuse to touch the real warm plane" -- and returns `None`
# silently (Backstop 2's "never raise" contract), before ever reaching
# `_open_pipe`. That guard exists to stop an ORDINARY test from accidentally
# hammering a real resident server; THIS module's whole premise (module
# docstring) is the opposite -- a deliberate, real invocation against the
# real listener `_ensure_warm_listener` already brought up. The stub is not
# itself a pytest test process; dropping the inherited stamp here is what
# lets it present as the ordinary installed-door caller it is standing in
# for, not what disables an unrelated safety rail.
os.environ.pop("PYTEST_CURRENT_TEST", None)

sys.path.insert(0, {engine_root!r})

from coordinator_core.warm.client import try_warm_dispatch

_op = sys.argv[1] if len(sys.argv) > 1 else "ping"
try_warm_dispatch(
    {{
        "jsonrpc": "2.0",
        "id": 1,
        "method": _op,
        "params": {{}},
        "_caller_cwd": {repo_root!r},
    }}
)
"""


def _write_stub_door_image(bin_dst: Path) -> Path:
    """Write and install a stubbed door image at `bin_dst / <door name>` --
    the temp `bin_dst` case E4 settled on (module test's own docstring
    citation). Never a real build: no call to
    `door_install_posix_build.build_or_advise` anywhere in this path."""
    import sys

    door_path = bin_dst / door_install.DOOR_INSTALLED_NAME
    door_path.write_text(
        _STUB_DOOR_IMAGE_TEMPLATE.format(
            python_executable=sys.executable,
            engine_root=str(_REAL_ENGINE_ROOT.root) if _REAL_ENGINE_ROOT is not None else "",
            repo_root=str(_REPO_ROOT),
        ),
        encoding="utf-8",
    )
    door_path.chmod(0o755)
    return door_path


def _assert_stub_door_image_is_executable(door_path: Path) -> None:
    """A direct executability probe, ahead of the real invocation below --
    `read_door_route` itself SWALLOWS an `OSError`/`PermissionError` from a
    door subprocess that could not even start (its own docstring: "never
    raises on the door's own failure to run"), so a noexec temp dir and a
    genuine routing regression would otherwise both read back as the same
    `UNRESOLVED` result. This probe exists only to tell them apart, and
    fails with the cause NAMED VERBATIM (module docstring's IMAGE SOURCE
    AND TIER section) -- a named environment failure, never a routing
    verdict.
    """
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    try:
        subprocess.run(
            [str(door_path), "--self-check"],
            capture_output=True,
            timeout=5,
            check=False,
            **no_console_creationflags(),
        )
    except (OSError, PermissionError) as exc:
        pytest.fail(
            f"stubbed door image at {door_path} could not be executed at all: "
            f"{exc!r} -- this reads as a noexec temp dir (or similar exec "
            "restriction), a named environment failure, not a routing verdict "
            "(module docstring's IMAGE SOURCE AND TIER note)."
        )


@pytest.mark.cadence
@pytest.mark.warm_tier
@pytest.mark.spawns_process
def test_post_install_ordering_stubbed_door_image_records_warm_route(tmp_path):
    """The regression fixture F-022 actually asks for (chunk C3): the
    POST-INSTALL ordering case -- a warm listener resident FIRST, door
    images installed against a temp `bin_dst` SECOND, then a door
    invocation must record `WARM_SERVER`.

    Goes red on `IN_PROCESS` -- the actual symptom F-022's memo reported
    (a warm listener resident from before a rebuild, door images replaced
    under it, forwarders dispatching name-blind) -- not on the absence of
    a call the fix just added. `run_cold_control_invocation` is the
    `DISCRIMINATOR_UNAVAILABLE` control, so an inert op-latency sink can
    never read back as a pass here.
    """
    # Ordering, load-bearing: listener resident FIRST.
    _ensure_warm_listener()

    # Door images installed against a temp `bin_dst` SECOND.
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    door_path = _write_stub_door_image(bin_dst)
    _assert_stub_door_image_is_executable(door_path)

    # A bounded retry, same tolerance `_ensure_warm_listener` above already
    # grants a fresh listener: this box's own load norm (50-70 active peers,
    # module CLAUDE.md) makes a single contended pipe connect ("go cold,
    # never spawn" -- `warm.client`'s own anti-storm table) indistinguishable,
    # on ONE attempt, from the real IN_PROCESS regression this test exists to
    # catch. Retrying a non-mutating `ping` a bounded number of times is safe
    # (the op is idempotent) and never raises the regression bar: any attempt
    # that reads back `WARM_SERVER` still passes, and the failure path below
    # still runs -- and still never skips -- once every attempt is spent.
    result = door_route_signal.DoorRouteResult(door_route_signal.UNRESOLVED, None)
    for _ in range(_LISTENER_WAIT_ATTEMPTS):
        result = door_route_signal.read_door_route(door_path, _OP, repo_root=_REPO_ROOT)
        if result.route == door_route_signal.WARM_SERVER:
            break
        time.sleep(_LISTENER_WAIT_INTERVAL_SECS)

    if result.route == door_route_signal.UNRESOLVED:
        control = door_route_signal.run_cold_control_invocation(_OP, repo_root=_REPO_ROOT)
        if control.route == door_route_signal.UNRESOLVED:
            pytest.fail(
                "DISCRIMINATOR_UNAVAILABLE -- the op-latency sink is inert on "
                "this box (kill switch, unresolvable git common dir, or an "
                "unwritable sink); an UNRESOLVED result from the stubbed-door "
                "invocation cannot be trusted as a fall-through here, and this "
                "is not the routing regression this test exists to catch."
            )
        pytest.fail(
            f"the stubbed-door invocation read back UNRESOLVED, but the known-"
            f"cold control invocation recorded route={control.route!r} -- the "
            "sink IS live, so the stubbed door's own invocation never wrote a "
            "matching row at all (the forwarder never reached the resident "
            "listener). Not the DISCRIMINATOR_UNAVAILABLE case."
        )

    assert result.route == door_route_signal.WARM_SERVER, (
        f"expected a post-install door invocation, against a listener already "
        f"resident before the door image was installed, to record "
        f"route={door_route_signal.WARM_SERVER!r}, got {result.route!r} "
        f"(entry={result.entry!r}) -- exactly the F-022 symptom (a warm "
        "listener resident from before a rebuild, door images replaced under "
        "it, forwarders dispatching name-blind), not an environment problem."
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
