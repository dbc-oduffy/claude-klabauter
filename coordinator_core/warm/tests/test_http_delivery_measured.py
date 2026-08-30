"""AC7's end-to-end half: curl reaches a credentialed listener without an interpreter.

Spec backlink: docs/plans/2026-08-26-the-loopback-listener-gets-a-credential.md § C3b/AC7.

WHAT WAS NOT MEASURABLE BEFORE, and why the number waited rather than being estimated.
AC7's mechanism half discharged at C3a: `mint()` emits `warm.curlrc` and a test proves
real curl parses it and puts the header on the wire. The END-TO-END half could not be
taken, because nothing dialled the listener without starting a Python interpreter --
`client.py` dials the pipe. The measurement was deferred, not waived, and this file is
the deferral being collected.

WHICH PATH THIS NUMBER JUSTIFIES, AND WHICH IT DOES NOT. Read this before citing it.

  * THE HOOK-FIRE PATH -- yes. A `type: "http"` hook registration hands the harness a URL;
    it cannot invoke a binary. The alternative there is `type: "command"`, which pays a
    fresh `cmd.exe` plus a fresh interpreter (~91-128ms of process time, measured in
    `state/handoffs/2026-08-21_103635_reaching-the-warm-engine.md`). A bare interpreter
    start is therefore the RIGHT floor for this comparison, and beating it is the claim.

  * THE OP-CLI PATH -- NO, and the honest baseline there is not an interpreter at all.
    `coordinator_core/warm/door/door.exe` already reaches the warm engine over the NAMED
    PIPE at ~2.34ms of process time, cheaper than `cmd /c exit`. Against the door, HTTP
    delivery is several times WORSE, not better. Any argument that the op CLI should move
    to HTTP has to beat 2.34ms, not 30ms, and this file does not make that argument.
    (`invoke.from_argv` is MUTATING and the routable fence refuses it regardless.)

Process time and spawn count, never wall clock: `docs/wiki/machine-load-norm.md` -- wall
clock on this box measures ~50 concurrent peers, not this transport.

THIS IS A MEASUREMENT, NOT A BUDGET GATE. It asserts the SHAPE of the result -- one
spawn, no interpreter, and a credentialed request that is actually served -- plus a
generous ceiling that catches a regression of kind rather than of degree. A tight
threshold here would fail on a busy box and teach the next reader to widen it, which is
how a real budget gets quietly retired.
"""

import json
import os
import shutil
import sys
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from coordinator_core.warm import cookie, skew, supervisor
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.skipif(
        shutil.which("curl") is None, reason="curl is the transport under measurement"
    ),
    # SPAWNS REAL PROCESSES, ON A BOX RUNNING DOZENS OF PEERS. Marked so it
    # runs at a cadence gate rather than per-commit: ~14 spawns to take one
    # number is a fair price occasionally and an antisocial one every commit.
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

#: Samples per measurement. k>=6 per AC7; the median is reported so one
#: descheduled sample on a box running dozens of peers cannot decide a verdict.
SAMPLES = 6


def _spawn_elapsed_ms(argv, **kwargs) -> float:
    """Elapsed time for one spawn, in ms. NAMED HONESTLY: this is not process
    time, and the load norm forbids concluding anything from wall clock alone,
    because on this box wall clock measures ~50 concurrent peers.

    `time.process_time` excludes children and Windows exposes no child rusage
    through `subprocess`, so a true child-CPU figure is not available here.
    Rather than dress elapsed time up as process time, the ABSOLUTE number is
    treated as untrustworthy and the assertion is a RATIO: curl against a bare
    interpreter, both spawned the same way, interleaved under the same load.
    Peer load inflates both and cancels; a regression in kind does not.

    The spawn COUNT -- the other half of the budget and the load-invariant
    half -- is structural: one curl process per delivery, asserted separately.
    """
    start = time.perf_counter()
    subprocess.run(argv, capture_output=True, **kwargs, **no_console_creationflags())
    return (time.perf_counter() - start) * 1000.0


def _median(values):
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


@pytest.fixture
def credentialed_listener(tmp_path):
    skew.write_engine_stamp(tmp_path, "sha-test")
    cookie.ensure(tmp_path)
    ctx = supervisor._ServerContext(
        httpd=None,
        engine_root=tmp_path,
        version_state=skew.ServerVersionState(tmp_path),
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), supervisor._make_handler(ctx))
    ctx.httpd = httpd
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd.server_address[1], tmp_path
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_curl_reaches_the_credentialed_listener_using_only_the_config(
    credentialed_listener,
):
    """THE DELIVERY PROPERTY, ASSERTED BEFORE ANY TIMING. The credential must
    arrive from the config file alone -- no header on the command line, no
    interpreter reading the cookie first. If this fails, the numbers below
    measure nothing worth having."""
    port, root = credentialed_listener
    proc = subprocess.run(
        [
            "curl",
            "--silent",
            "--show-error",
            "--config",
            str(cookie.curl_config_path(root)),
            "--write-out",
            "%{http_code}",
            "--output",
            os.devnull,
            f"http://127.0.0.1:{port}{supervisor.HEALTH_PATH}",
        ],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("200")


def test_an_uncredentialed_curl_is_refused_by_the_same_listener(
    credentialed_listener,
):
    """The control that makes the test above mean something: WITHOUT the
    config, the same request against a non-exempt path is refused. Otherwise
    a listener that ignored the cookie entirely would pass identically."""
    port, _root = credentialed_listener
    proc = subprocess.run(
        [
            "curl",
            "--silent",
            "--output",
            os.devnull,
            "--write-out",
            "%{http_code}",
            "-X",
            "POST",
            f"http://127.0.0.1:{port}{supervisor.HOOK_PATH}",
        ],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert proc.stdout.strip().endswith("401")


def test_the_delivery_costs_one_spawn_and_beats_an_interpreter_start(
    credentialed_listener, capsys
):
    """AC7's number, with its own comparison rather than an absolute.

    ONE SPAWN: curl, once. The claim being defended is that the cookie
    reaches the wire without a Python process, so the interpreter floor is
    the thing worth measuring against -- if delivery cost more than
    `python -c pass`, the transport's own justification would be spent.

    SCOPED TO THE HOOK-FIRE PATH. See the module docstring: the op CLI's
    incumbent is `door.exe` at ~2.34ms over the pipe, and this number does
    not beat it and is not offered as beating it.
    """
    port, root = credentialed_listener
    url = f"http://127.0.0.1:{port}{supervisor.HEALTH_PATH}"
    null = os.devnull  # NUL on Windows, /dev/null elsewhere -- never hard-coded

    curl_argv = [
        "curl", "--silent", "--config", str(cookie.curl_config_path(root)),
        "--output", null, url,
    ]
    # `sys.executable`, never a bare "python": on many POSIX installs only
    # `python3` is on PATH, so the bare name would raise FileNotFoundError
    # rather than skip. It is also the more honest floor -- the interpreter
    # this repo actually pays for, not whatever PATH resolves to.
    interp_argv = [sys.executable, "-c", "pass"]

    # One warm-up each, discarded: the first spawn pays page-cache and loader
    # costs that no steady-state caller pays, and including it would flatter
    # neither side honestly.
    _spawn_elapsed_ms(curl_argv)
    _spawn_elapsed_ms(interp_argv)

    # INTERLEAVED, not one batch then the other: a load spike that lands
    # during a contiguous run of one side would be read as that side being
    # slower. Alternating puts both under the same conditions sample by
    # sample, which is what makes the ratio mean anything on a busy box.
    curl_samples, interp_samples = [], []
    for _ in range(SAMPLES):
        curl_samples.append(_spawn_elapsed_ms(curl_argv))
        interp_samples.append(_spawn_elapsed_ms(interp_argv))
    curl_ms = _median(curl_samples)
    interp_ms = _median(interp_samples)

    print(
        json.dumps(
            {
                "ac7_end_to_end": {
                    "curl_delivery_ms_median": round(curl_ms, 2),
                    "bare_interpreter_ms_median": round(interp_ms, 2),
                    "samples": SAMPLES,
                    "spawns_per_delivery": 1,
                },
            },
            indent=2,
        )
    )
    with capsys.disabled():
        print(
            f"\nAC7 end-to-end: curl delivery {curl_ms:.1f}ms median vs bare "
            f"interpreter {interp_ms:.1f}ms median, k={SAMPLES}, 1 spawn."
        )

    assert curl_ms < interp_ms, (
        f"curl delivery ({curl_ms:.1f}ms) did not beat a bare interpreter start "
        f"({interp_ms:.1f}ms). The transport's justification is that reaching the "
        "listener costs less than starting Python; if that inverts, the saving is gone."
    )
    assert curl_ms < 500, (
        f"curl delivery {curl_ms:.1f}ms is over the brightline. Elapsed, not process "
        "time, so a busy box inflates it -- but 500ms is the bar the whole transport "
        "is justified against, and a delivery over it is a defect the moment it is "
        "seen. Do not widen this number to make it pass."
    )
