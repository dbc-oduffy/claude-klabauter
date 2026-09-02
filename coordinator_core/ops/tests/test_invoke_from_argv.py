"""
coordinator_core.ops.tests.test_invoke_from_argv — tests for the "invoke.from_argv"
op (coordinator_core/ops/invoke_from_argv.py), the warm door's server-side
entrypoint.

Coverage:
  (a) params validation — argv must be list[str], cwd must be a non-empty str,
      else ValueError (surfaces as a standard JSON-RPC handler-exception error
      via dispatch_message, same as every other op's params validation).
  (b) byte-identical output against the real CLI, spawned fresh
      (`sys.executable -m coordinator_core.invoke`) — the acceptance property
      named in the dispatch brief. Covers a deterministic op
      (--dump-op-timeouts) for an exact byte comparison, and a live-clock op
      (ping) compared structurally (matching test_invoke_main.py's own
      ts-field allowance for the same reason).
  (c) the served path never dials the warm pipe back into itself
      (`allow_warm=False`) — proven by making `is_warm_enabled` raise if
      called at all, then asserting the op still dispatches successfully.
  (d) `cwd` is threaded through to repo_root resolution, not this test
      process's own os.getcwd() — a worktree-scoped op dispatched with `cwd`
      pointed outside any git repo (and no --repo) fails the same way the CLI
      itself would from that cwd, which could only happen if `cwd` (not this
      process's real, in-repo cwd) drove `find_repo_root`.
  (e) the served path's cold-dispatch branch (`_dispatch_argv_body`'s
      `response is None` block in __main__.py) does not leak an event loop
      (or its OS-level selector/IOCP handle) per call — every
      `invoke.from_argv` dispatch takes that branch (`allow_warm=False`), and
      it runs inside the resident warm server, so a per-call leak there is a
      standing-process leak, not a one-shot-CLI cosmetic. Proven by counting
      still-OPEN `asyncio.AbstractEventLoop` instances tracked by the
      collector across repeated calls with cyclic GC deliberately disabled
      (so a leak cannot be masked by an incidental collection pass) — flat
      across 50 calls, not growing with call count.

These tests call the handler directly (not via a subprocess) — it is sync,
holds no os._exit, and dispatch_message's own subprocess-only constraint
belongs to `main()`, not this op. See test_invoke_main.py's own module
docstring for why *that* module needs subprocess isolation and this one does
not.

Spec backlink: state/handoffs/2026-08-21_103635_reaching-the-warm-engine.md
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

import pytest

from coordinator_core.ops import invoke_from_argv
from coordinator_core.ops.invoke_from_argv import _invoke_from_argv, _run_entrypoint

# Several tests spawn a real `sys.executable -m coordinator_core.invoke`
# subprocess for the byte-identical CLI comparison (the dispatch brief's
# acceptance property) — declared + tiered off the per-commit path per the
# spawn ratchet (coordinator_core/tests/test_no_new_spawning_tests.py Rules 2/4).
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PROJECT_ROOT = str(Path(__file__).resolve().parents[3])
_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _cli_env(**overrides: str) -> dict:
    """Same PYTHONPATH-injection contract as test_invoke_main.py's _make_env,
    plus COORDINATOR_WARM=0 pinned by default so the CLI comparison subprocess
    cold-dispatches deterministically regardless of whether a live warm server
    happens to be running on this box — matching `allow_warm=False`'s effective
    behavior on the op side, so the two are a fair comparison."""
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{_PROJECT_ROOT}{os.pathsep}{existing_pp}" if existing_pp else _PROJECT_ROOT
    env.setdefault("COORDINATOR_WARM", "0")
    env.update(overrides)
    return env


def _run_cli(*args: str, cwd: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "coordinator_core.invoke", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=cwd,
        env=_cli_env(),
        creationflags=_NO_CONSOLE,
    )


# ---------------------------------------------------------------------------
# (a) params validation
# ---------------------------------------------------------------------------

def test_argv_must_be_a_list_of_strings():
    with pytest.raises(ValueError, match="params.argv"):
        _invoke_from_argv({"argv": "ping", "cwd": _PROJECT_ROOT})


def test_argv_must_be_a_list_of_strings_not_mixed_types():
    with pytest.raises(ValueError, match="params.argv"):
        _invoke_from_argv({"argv": ["ping", 1], "cwd": _PROJECT_ROOT})


def test_cwd_must_be_a_non_empty_string():
    with pytest.raises(ValueError, match="params.cwd"):
        _invoke_from_argv({"argv": ["ping"], "cwd": ""})


def test_cwd_must_be_present():
    with pytest.raises(ValueError, match="params.cwd"):
        _invoke_from_argv({"argv": ["ping"]})


# ---------------------------------------------------------------------------
# (b) byte-identical output against the real CLI
# ---------------------------------------------------------------------------

def test_dump_op_timeouts_byte_identical_to_cli():
    """--dump-op-timeouts is fully deterministic (no live clock/UUID in its
    payload) — an exact byte comparison against the real CLI subprocess is
    the acceptance property itself, not an approximation of it."""
    served = _invoke_from_argv({"argv": ["--dump-op-timeouts"], "cwd": _PROJECT_ROOT})
    cli = _run_cli("--dump-op-timeouts", cwd=_PROJECT_ROOT)

    assert served["exit_code"] == cli.returncode == 0
    assert served["stdout"] == cli.stdout, (
        f"served stdout must be byte-identical to the CLI's.\n"
        f"served: {served['stdout']!r}\ncli:    {cli.stdout!r}"
    )
    assert served["stderr"] == cli.stderr == ""


def test_ping_matches_cli_structurally():
    """ping's `ts` field is a live monotonic clock read independently by each
    call, so an exact byte comparison would be flaky by construction — same
    allowance test_invoke_main.py's test_bare_flag_matches_default_result_payload
    makes for the identical reason. Compare shape/exit_code/stderr exactly and
    the `ok` field's value exactly; only `ts` is excluded."""
    served = _invoke_from_argv({"argv": ["ping", "{}"], "cwd": _PROJECT_ROOT})
    cli = _run_cli("ping", "{}", cwd=_PROJECT_ROOT)

    assert served["exit_code"] == cli.returncode == 0
    assert served["stderr"] == cli.stderr == ""

    served_parsed = json.loads(served["stdout"])
    cli_parsed = json.loads(cli.stdout)
    assert served_parsed["jsonrpc"] == cli_parsed["jsonrpc"] == "2.0"
    assert set(served_parsed["result"].keys()) == set(cli_parsed["result"].keys())
    assert served_parsed["result"]["ok"] is cli_parsed["result"]["ok"] is True


def test_bare_ping_matches_cli_structurally():
    served = _invoke_from_argv({"argv": ["--bare", "ping", "{}"], "cwd": _PROJECT_ROOT})
    cli = _run_cli("--bare", "ping", "{}", cwd=_PROJECT_ROOT)

    assert served["exit_code"] == cli.returncode == 0
    assert served["stderr"] == cli.stderr == ""
    served_parsed = json.loads(served["stdout"])
    cli_parsed = json.loads(cli.stdout)
    assert set(served_parsed.keys()) == set(cli_parsed.keys())
    assert served_parsed["ok"] is cli_parsed["ok"] is True


def test_invalid_params_json_byte_identical_to_cli():
    """A pre-dispatch _fatal_stderr failure (branch 2 of test_invoke_main.py)
    carries no volatile fields — exact byte comparison, exit 1, stderr-only."""
    served = _invoke_from_argv({"argv": ["ping", "not json"], "cwd": _PROJECT_ROOT})
    cli = _run_cli("ping", "not json", cwd=_PROJECT_ROOT)

    assert served["exit_code"] == cli.returncode == 1
    assert served["stdout"] == cli.stdout == ""
    assert served["stderr"] == cli.stderr
    assert served["stderr"], "expected a non-empty error envelope on stderr"


def test_dump_op_timeouts_default_matches_live_process_resolution():
    """--dump-op-timeouts's `__default__` reads coordinator_core.ipc.
    DISPATCH_TIMEOUT_SECS, a module-level constant resolved ONCE at
    coordinator_core.ipc import time — not re-read from the environment per
    call. That is true for a served call exactly as it is true for a fresh
    CLI subprocess: neither can honor a post-import env override (proven
    separately, at the CLI level, by
    test_invoke_main.py::test_dump_op_timeouts_default_reflects_live_env_override,
    which sets the override BEFORE the subprocess's own ipc import). What
    this test proves instead is narrower and specific to the served path:
    the served call resolves the SAME already-imported DISPATCH_TIMEOUT_SECS
    live in THIS test process (via mock.patch, no subprocess needed for that
    half) as a freshly spawned CLI resolves from a matching environment.
    """
    from coordinator_core.ipc import DISPATCH_TIMEOUT_SECS

    served = _invoke_from_argv({"argv": ["--dump-op-timeouts"], "cwd": _PROJECT_ROOT})
    served_default = json.loads(served["stdout"])["__default__"]
    assert served_default == DISPATCH_TIMEOUT_SECS, (
        "served invoke.from_argv must resolve --dump-op-timeouts' __default__ "
        "from the SAME already-imported coordinator_core.ipc.DISPATCH_TIMEOUT_SECS "
        "this test process holds — not a re-import or a stale copy."
    )

    cli = _run_cli("--dump-op-timeouts", cwd=_PROJECT_ROOT)
    assert json.loads(cli.stdout)["__default__"] == served_default, (
        "a freshly spawned CLI, with no env override, must resolve the same "
        "default this process's own DISPATCH_TIMEOUT_SECS holds."
    )


# ---------------------------------------------------------------------------
# (c) served path never dials the warm pipe back into itself
# ---------------------------------------------------------------------------

def test_served_dispatch_never_checks_warm_enabled():
    """allow_warm=False must skip the warm preamble outright — is_warm_enabled
    must never even be CALLED, let alone connected to. A regression here would
    mean a request already being served by the warm server tries to dial its
    own pipe from inside itself."""
    with mock.patch(
        "coordinator_core.warm.settings.is_warm_enabled",
        side_effect=AssertionError("is_warm_enabled must not be called on the served path"),
    ):
        result = _invoke_from_argv({"argv": ["ping", "{}"], "cwd": _PROJECT_ROOT})
    assert result["exit_code"] == 0
    assert json.loads(result["stdout"])["result"]["ok"] is True


# ---------------------------------------------------------------------------
# (d) cwd is threaded through, never this process's own os.getcwd()
# ---------------------------------------------------------------------------

def test_worktree_scoped_op_resolves_repo_root_from_cwd_param_not_process_cwd():
    """A worktree-scoped op (handoff.has_live_children — same vehicle
    test_invoke_main.py uses) dispatched with `cwd` pointed at a directory
    OUTSIDE any git repo, and no --repo, must fail with a repo-unresolvable
    error — exactly what the real CLI does run from that same directory.

    This test's own process cwd is inside the claude-klabauter git repo (pytest is
    invoked from the repo), so if the handler's repo_root resolution silently
    fell back to os.getcwd() instead of the `cwd` param, this op would
    dispatch SUCCESSFULLY instead of failing — a false green that this test
    exists to catch.
    """
    tmp_dir = tempfile.mkdtemp(dir=tempfile.gettempdir(), prefix="invoke_from_argv_test_")
    try:
        probe = subprocess.run(
            ["git", "-C", tmp_dir, "rev-parse", "--git-dir"],
            capture_output=True, creationflags=_NO_CONSOLE,
        )
        assert probe.returncode != 0, (
            f"Setup error: {tmp_dir!r} is inside a git repo — invalidates this test."
        )

        served = _invoke_from_argv({
            "argv": ["handoff.has_live_children", '{"candidate": "x"}'],
            "cwd": tmp_dir,
        })
        cli = _run_cli("handoff.has_live_children", '{"candidate": "x"}', cwd=tmp_dir)

        assert served["exit_code"] == cli.returncode == 1
        assert served["stdout"] == cli.stdout == ""
        assert "not inside a git working tree" in served["stderr"]
        assert served["stderr"] == cli.stderr
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# (e) served-path loop lifecycle: no per-call leak
# ---------------------------------------------------------------------------

def _open_event_loop_count() -> int:
    """Count `asyncio.AbstractEventLoop` instances the collector currently
    tracks that are NOT closed. Deliberately does not call `gc.collect()` —
    the point of this helper is to observe accumulation exactly as it stands,
    not after a sweep that could paper over an unclosed-but-unreferenced loop
    the leak fix is supposed to prevent from ever existing in the first
    place."""
    return sum(
        1
        for obj in gc.get_objects()
        if isinstance(obj, asyncio.AbstractEventLoop) and not obj.is_closed()
    )


def test_repeated_served_dispatch_does_not_leak_event_loops():
    """`invoke.from_argv` always takes __main__.py's cold-dispatch branch
    (`allow_warm=False`), which creates a fresh `asyncio.new_event_loop()`
    per call. Called directly here exactly as the resident warm server would
    call it (repeatedly, on one thread — ipc.py's `_dispatch_message_impl`
    offloads this op's sync handler via `asyncio.to_thread`, i.e. the
    server's own loop's default executor, a persistent pool whose worker
    threads are REUSED across calls, so one OS thread genuinely does service
    many `invoke.from_argv` requests back to back over the server's
    lifetime).

    Before the fix, each call's loop was created and never closed: a `ping`
    dispatch also exercises a NESTED `asyncio.to_thread` (ping is itself a
    sync handler `_dispatch_message_impl` offloads), which lazily creates a
    default ThreadPoolExecutor on the freshly-made loop — so an unclosed loop
    here is not just an idle object, it is one holding a live executor and
    selector/IOCP handle. `gc.disable()` for the duration of the 50-call loop
    is deliberate: asyncio event loops commonly hold internal reference
    cycles (their own ready/scheduled queues reference back into the loop),
    so plain refcounting would not free an abandoned-but-never-closed loop
    promptly — only a cyclic GC pass would, which could otherwise mask
    exactly the accumulation this test exists to catch. With GC disabled, an
    unfixed leak accumulates one open loop per call, linearly, for the full
    50; the fix makes every call close its own loop explicitly, so the open
    count never exceeds a small constant regardless of how many calls run.
    """
    gc.disable()
    try:
        counts = []
        for _ in range(50):
            result = _invoke_from_argv({"argv": ["ping", "{}"], "cwd": _PROJECT_ROOT})
            assert result["exit_code"] == 0
            counts.append(_open_event_loop_count())
    finally:
        gc.enable()

    assert max(counts) <= 2, (
        f"open (unclosed) event-loop count must stay flat and small across "
        f"50 served dispatches, not grow with call count; got the sequence: {counts}"
    )
    # The defining before/after contrast: NOT monotonically growing with
    # call count (a leak's signature) — the last sample is no larger than
    # the first, well inside noise, whereas an unfixed leak would show
    # counts[-1] >= 50 (one abandoned loop per call).
    assert counts[-1] <= counts[0] + 1, (
        f"event-loop count must not grow across repeated calls; "
        f"first={counts[0]!r} last={counts[-1]!r} full sequence={counts!r}"
    )


def test_repeated_served_dispatch_thread_count_stays_bounded():
    """Companion to the loop-leak test: OS thread count must not grow
    unboundedly either. `loop.close()`'s `executor.shutdown(wait=False)` is
    non-blocking BY DESIGN (P1#5) — it signals shutdown, it does not
    guarantee the pool's worker threads have already exited by the time the
    next call starts — so this asserts BOUNDED (a small, non-growing
    ceiling), not that thread count is instantaneously zero-growth after
    every single call.
    """
    before = threading.active_count()
    for _ in range(50):
        result = _invoke_from_argv({"argv": ["ping", "{}"], "cwd": _PROJECT_ROOT})
        assert result["exit_code"] == 0
    # Give any still-shutting-down pool threads a brief window to exit —
    # bounded, not a leak-detection sleep loop: a real leak would still show
    # a large, calls-proportional excess after this window; a flat,
    # non-leaking implementation settles near `before` almost immediately.
    import time
    for _ in range(20):
        if threading.active_count() <= before + 4:
            break
        time.sleep(0.05)
    after = threading.active_count()

    assert after <= before + 4, (
        f"thread count must settle back near its starting level, not grow "
        f"proportionally with 50 calls; before={before} after={after}"
    )


# ---------------------------------------------------------------------------
# (f) `params.entrypoint` set: the process-global chdir does not race across
#     two concurrent calls with different `cwd`s (C7 — the entrypoint path
#     stops chdir-ing a process 50 sessions share).
# ---------------------------------------------------------------------------


def test_concurrent_entrypoint_calls_do_not_race_the_shared_process_cwd(tmp_path):
    """Two `_run_entrypoint` calls with DIFFERENT `cwd`s, started concurrently
    on separate threads, must not interleave their `os.chdir` spans. Each
    call's fake CLI records the cwd it observed on entry and on exit — if
    `_ENTRYPOINT_CWD_LOCK` did not serialize the chdir/call/restore span, the
    second thread's `os.chdir` could fire while the first thread's `main` is
    still mid-run, so the first call's exit-time `os.getcwd()` would no
    longer match its own entry-time `os.getcwd()`. This is the exact hazard
    named in state/audits/2026-08-27-torn-read-hazard-sweep.md.
    """
    import builtins
    import time

    bin_dir = tmp_path / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    script = bin_dir / "fake-entrypoint-cwd-race.py"
    script.write_text(
        "import builtins\n"
        "import os\n"
        "import time\n"
        "\n"
        "\n"
        "def main(argv):\n"
        "    rec = builtins._ENTRYPOINT_CWD_RACE_RECORD\n"
        "    entry_cwd = os.getcwd()\n"
        "    rec.append((\"start\", entry_cwd))\n"
        "    time.sleep(0.2)\n"
        "    rec.append((\"end\", entry_cwd, os.getcwd()))\n"
        "    return 0\n",
        encoding="utf-8",
    )

    cwd_a = tmp_path / "cwd_a"
    cwd_b = tmp_path / "cwd_b"
    cwd_a.mkdir()
    cwd_b.mkdir()

    builtins._ENTRYPOINT_CWD_RACE_RECORD = []
    try:
        with mock.patch.object(invoke_from_argv, "_ENGINE_ROOT", tmp_path), mock.patch.object(
            invoke_from_argv,
            "_WARM_ENTRYPOINT_ALLOWLIST",
            frozenset({"fake-entrypoint-cwd-race"}),
        ):
            results: dict = {}

            def _call(key: str, cwd: Path) -> None:
                results[key] = _run_entrypoint("fake-entrypoint-cwd-race", [], str(cwd))

            t1 = threading.Thread(target=_call, args=("a", cwd_a))
            t2 = threading.Thread(target=_call, args=("b", cwd_b))
            t1.start()
            time.sleep(0.02)
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

        assert not t1.is_alive() and not t2.is_alive()
        for key, result in results.items():
            assert result["exit_code"] == 0, f"{key}: {result}"

        record = builtins._ENTRYPOINT_CWD_RACE_RECORD
        assert len(record) == 4, record

        # Each call's own exit-time cwd matches its own entry-time cwd —
        # nothing chdir'd out from under it mid-run.
        for entry in record:
            if entry[0] == "end":
                _, entry_cwd, exit_cwd = entry
                assert entry_cwd == exit_cwd, (
                    f"cwd changed mid-call — the lock did not hold: {entry}"
                )

        # The two calls never interleaved: a ("start", "end") pair is
        # contiguous, never split by the other call's "start".
        kinds = [entry[0] for entry in record]
        assert kinds == ["start", "end", "start", "end"], (
            f"entrypoint calls interleaved instead of serializing: {kinds}"
        )
    finally:
        del builtins._ENTRYPOINT_CWD_RACE_RECORD


# ---------------------------------------------------------------------------
# (g) `params.entrypoint` set: the served CLI reads the CALLER's session
#     identity out of `os.environ`, never the warm server owner's.
#
#     The defect (cross-repo/inbox/2026-08-30-example-retrieval-repo-em-prepare-commit-
#     msg-stamps-warm-engine-owner-session-id.md, reproduced in this repo
#     2026-08-30): every `coordinator/bin/*.py` CLI resolves its session id by
#     reading `SESSION_ENV_PRECEDENCE` out of `os.environ` — the
#     `prepare-commit-msg` hook does so in a deliberately hand-mirrored copy of
#     that ladder. Served in-process here, that environment is the server
#     owner's, so the door's `_session_id` reached `resolve_session_id()` and
#     stopped there: same hook, correct cold, a stranger's id warm, on EVERY
#     hook-path commit on a box carrying the forwarder.
#
#     C4: the point fix (`_borrowed_session_identity`, formerly local to this
#     module) is deleted — `coordinator_core.warm.entry_seam.per_request_state`
#     (`_environ_identity_borrow`, C3) now makes the caller's identity true in
#     `os.environ` for the whole isolated dispatch this op runs inside, so
#     these tests open the SEAM's own scope (`isolated=True`) around
#     `_run_entrypoint`, instead of binding `session_identity_override`/
#     `warm_served_request` directly and relying on a borrow local to this
#     module. They still pin the property (caller's id wins, absent-when-
#     uncarried, server env restored), not the mechanism.
# ---------------------------------------------------------------------------

_CALLER_SID = "8b40d62c-55ef-4702-83ce-0cd8dc6513e3"
_SERVER_OWNER_SID = "b68689fb-a9a5-4f3d-9ca9-f688530ed7c1"

_SESSION_ENV_PROBE = (
    "import builtins\n"
    "import os\n"
    "\n"
    "\n"
    "def main(argv):\n"
    "    builtins._ENTRYPOINT_SESSION_ENV_SEEN = {\n"
    "        name: os.environ.get(name)\n"
    "        for name in ("
    "'COORDINATOR_SESSION_ID', 'CLAUDE_SESSION_ID', 'CLAUDE_CODE_SESSION_ID')\n"
    "    }\n"
    "    return 0\n"
)


def _run_session_env_probe(tmp_path, monkeypatch, *, carried, server_env):
    """Run a fake CLI through `_run_entrypoint`, inside the seam-level
    isolated-dispatch scope (`entry_seam.per_request_state(isolated=True)`)
    that C3 made responsible for mirroring `carried` into `os.environ`, with
    `server_env` as the server process's own environment; return what the CLI
    saw, plus this process's env afterwards.
    """
    import builtins

    from coordinator_core.session.core import SESSION_ENV_PRECEDENCE
    from coordinator_core.warm import entry_seam

    bin_dir = tmp_path / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "fake-entrypoint-session-env.py").write_text(
        _SESSION_ENV_PROBE, encoding="utf-8"
    )

    for name in SESSION_ENV_PRECEDENCE:
        monkeypatch.delenv(name, raising=False)
    for name, value in server_env.items():
        monkeypatch.setenv(name, value)

    builtins._ENTRYPOINT_SESSION_ENV_SEEN = None
    try:
        with mock.patch.object(invoke_from_argv, "_ENGINE_ROOT", tmp_path), mock.patch.object(
            invoke_from_argv,
            "_WARM_ENTRYPOINT_ALLOWLIST",
            frozenset({"fake-entrypoint-session-env"}),
        ):
            with entry_seam.per_request_state(
                session_id=carried, warm_served=True, isolated=True
            ):
                result = _run_entrypoint("fake-entrypoint-session-env", [], str(tmp_path))
        seen = builtins._ENTRYPOINT_SESSION_ENV_SEEN
    finally:
        del builtins._ENTRYPOINT_SESSION_ENV_SEEN

    assert result["exit_code"] == 0, result
    after = {name: os.environ.get(name) for name in SESSION_ENV_PRECEDENCE}
    return seen, after


def test_served_cli_reads_the_callers_session_id_not_the_servers(tmp_path, monkeypatch):
    """The defect itself. The server's own environment names the session that
    spawned it; the CLI must see the one the door carried."""
    seen, _ = _run_session_env_probe(
        tmp_path,
        monkeypatch,
        carried=_CALLER_SID,
        server_env={"CLAUDE_CODE_SESSION_ID": _SERVER_OWNER_SID},
    )

    assert seen["COORDINATOR_SESSION_ID"] == _CALLER_SID
    # The lower-tier names are popped, not merely outranked: a CLI reading only
    # `CLAUDE_CODE_SESSION_ID` would otherwise still resurface the owner's id.
    assert seen["CLAUDE_SESSION_ID"] is None
    assert seen["CLAUDE_CODE_SESSION_ID"] is None


def test_warm_request_carrying_no_identity_shows_the_cli_none(tmp_path, monkeypatch):
    """The fail-safe direction. A warm request the door sent no `_session_id`
    for is indistinguishable from a cold one at the ContextVar, and the two
    need opposite answers: omitting a trailer is coverage-neutral, stamping the
    server owner's is misattribution (`session.core.carried_session_id`)."""
    seen, _ = _run_session_env_probe(
        tmp_path,
        monkeypatch,
        carried=None,
        server_env={
            "COORDINATOR_SESSION_ID": _SERVER_OWNER_SID,
            "CLAUDE_CODE_SESSION_ID": _SERVER_OWNER_SID,
        },
    )

    assert seen == {
        "COORDINATOR_SESSION_ID": None,
        "CLAUDE_SESSION_ID": None,
        "CLAUDE_CODE_SESSION_ID": None,
    }


# ---------------------------------------------------------------------------
# (h) `params.entrypoint` set: `--help`/`-h` renders a target's REAL usage on
#     the warm door — the third door
#     (docs/plans/2026-09-02-the-loader-fires-the-assembly-not-the-em.md,
#     chunk C1 follow-up), corrected after a first pass synthesized a stub
#     line for EVERY entrypoint and silently replaced 11 targets' real usage
#     with `usage: <name> [--help]` on the warm door only (never reproduced
#     cold), hiding a live, published flag (`baton-assemble
#     --expect-discovery-tier`).
#
#     Fixtures rather than the real `coordinator/bin/baton-assemble.py` /
#     `plan-assemble.py`: those two route through `entry_point_shim`'s
#     engine-mapped entries, which resolve the claude-klabauter root via a
#     machine-local registry read this suite's own root `conftest.py`
#     deliberately quarantines the real HOME directory from — calling their
#     real `--help` inside THIS test process silently hits that
#     resolution failure and (correctly, per `_render_usage_text`'s own
#     fallback chain) falls back to a synthesized line, which would make
#     these tests pass on a false basis. The fixtures below exercise the
#     exact same `_run_entrypoint` code path (real `_load_entrypoint_main` +
#     real `main_fn(["--help"])` call under the real lock/chdir span) against
#     a target whose own module body has no such external dependency —
#     `workday-start-inbox-blitz-assemble` (the real ARGV_SHAPE_NONE member)
#     is used directly below since its short-circuit never loads a module at
#     all and so carries no such fragility.
#
#     `main_fn` IS entered with `["--help"]` standing in for the caller's
#     real argv for ARGV_SHAPE_FULL/TAIL targets, exactly as the cold door's
#     `entry_point_shim.run_target` does; it is NEVER entered for
#     ARGV_SHAPE_NONE, which cannot receive a flag at all.
# ---------------------------------------------------------------------------


def _write_delegating_entrypoint(bin_dir: Path, name: str, *, real_help_branch: bool) -> None:
    """A TAIL/FULL-shaped fixture. `real_help_branch=True` mirrors
    `baton-assemble`'s shape (an explicit, first-checked `--help` branch
    that prints real multi-line usage and returns before any op-reaching
    code runs). `real_help_branch=False` mirrors `plan-assemble`'s shape (no
    explicit branch — falls through to argparse, which prints its usage line
    to stderr ALONGSIDE a genuine 'unrecognized argument' parse-error line)."""
    if real_help_branch:
        body = (
            "import builtins\n"
            "\n"
            "\n"
            "def main(argv):\n"
            "    if '--help' in argv or '-h' in argv:\n"
            "        print('usage: " + name + " brief <kind> [--flag-added-later]')\n"
            "        print('  a real, multi-line usage block')\n"
            "        return 0\n"
            "    builtins._ENTRYPOINT_OP_ENTERED = True\n"
            "    return 0\n"
            "\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    import sys\n"
            "    sys.exit(main(sys.argv))\n"
        )
    else:
        body = (
            "import argparse\n"
            "import builtins\n"
            "\n"
            "\n"
            "def main(argv):\n"
            "    parser = argparse.ArgumentParser(prog='" + name + "', add_help=False)\n"
            "    parser.add_argument('--route')\n"
            "    try:\n"
            "        parser.parse_args(argv)\n"
            "    except SystemExit:\n"
            "        return 2\n"
            "    builtins._ENTRYPOINT_OP_ENTERED = True\n"
            "    return 0\n"
            "\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    import sys\n"
            "    sys.exit(main(sys.argv))\n"
        )
    (bin_dir / f"{name}.py").write_text(body, encoding="utf-8")


def test_help_delegates_to_a_full_shaped_targets_own_real_usage(tmp_path):
    """The regression this follow-up exists to close: a delegating target's
    real, multi-line usage must survive the warm door, not collapse to a
    one-line stub — proven against a fixture with its own explicit `--help`
    branch (the shape `baton-assemble` uses)."""
    import builtins

    name = "fake-entrypoint-full-shape-help"
    bin_dir = tmp_path / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    _write_delegating_entrypoint(bin_dir, name, real_help_branch=True)

    builtins._ENTRYPOINT_OP_ENTERED = False
    try:
        with mock.patch.object(invoke_from_argv, "_ENGINE_ROOT", tmp_path), mock.patch.object(
            invoke_from_argv, "_WARM_ENTRYPOINT_ALLOWLIST", frozenset({name})
        ):
            result = _run_entrypoint(name, ["--help"], str(tmp_path))

        assert result["exit_code"] == 0
        assert result["stderr"] == ""
        assert "brief <kind>" in result["stdout"]
        assert "--flag-added-later" in result["stdout"], (
            "a live flag on the target's own usage text must be visible "
            "through the warm door — a stub line would hide it"
        )
        assert len(result["stdout"].splitlines()) > 1, (
            "a real usage render is not a single synthesized line"
        )
        assert builtins._ENTRYPOINT_OP_ENTERED is False, (
            "a --help gesture must never reach the target's op-performing branch"
        )
    finally:
        del builtins._ENTRYPOINT_OP_ENTERED


def test_help_delegates_for_a_tail_shaped_target_without_leaking_the_parse_error(tmp_path):
    """`plan-assemble` has no explicit `--help` branch of its own and falls
    through to its hand-rolled parser's usage/parse-error path — the exact
    trap that already cost C1 a round (usage on stderr ALONGSIDE a genuine
    'unrecognized argument' line). Only the real usage line may surface."""
    name = "fake-entrypoint-tail-shape-help"
    bin_dir = tmp_path / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    _write_delegating_entrypoint(bin_dir, name, real_help_branch=False)

    with mock.patch.object(invoke_from_argv, "_ENGINE_ROOT", tmp_path), mock.patch.object(
        invoke_from_argv, "_WARM_ENTRYPOINT_ALLOWLIST", frozenset({name})
    ):
        result = _run_entrypoint(name, ["--help"], str(tmp_path))

    assert result["exit_code"] == 0
    assert result["stderr"] == ""
    assert f"usage: {name}" in result["stdout"]
    assert "--route" in result["stdout"]
    assert "unrecognized argument" not in result["stdout"].lower()


def test_help_synthesizes_only_for_the_argv_shape_none_entrypoint():
    """Positive assertion naming the shape, not an absence: synthesis is
    used ONLY for `workday-start-inbox-blitz-assemble` (the real
    ARGV_SHAPE_NONE member — its own `main()` takes no argv and always runs
    its full body regardless of what it is "called with", so it has no
    external-dependency fragility a fixture would need to dodge), and
    `main_fn` is never entered to produce it — proven by making the loader
    raise if called at all."""
    name = "workday-start-inbox-blitz-assemble"
    script = Path(invoke_from_argv._ENGINE_ROOT) / "coordinator" / "bin" / f"{name}.py"
    shape = invoke_from_argv._entrypoint_argv_shape(script)
    from coordinator_core.warm import serve_classifier

    assert shape == serve_classifier.ARGV_SHAPE_NONE, (
        f"{name} must be the ARGV_SHAPE_NONE fixture this test relies on; "
        f"got {shape!r} — this target's own __main__ guard shape changed"
    )

    with mock.patch.object(
        invoke_from_argv,
        "_load_entrypoint_main",
        side_effect=AssertionError("main_fn must never be loaded/called for --help on this shape"),
    ):
        result = _run_entrypoint(name, ["--help"], _PROJECT_ROOT)

    assert result["exit_code"] == 0
    assert result["stderr"] == ""
    assert result["stdout"] == f"usage: {name} [--help]\n"


def test_help_wins_after_a_subcommand_token(tmp_path):
    """Position is not special-cased — `--help` reached after a real
    subcommand token still renders the target's real usage."""
    import builtins

    name = "fake-entrypoint-help-subcommand"
    bin_dir = tmp_path / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    _write_delegating_entrypoint(bin_dir, name, real_help_branch=True)

    builtins._ENTRYPOINT_OP_ENTERED = False
    try:
        with mock.patch.object(invoke_from_argv, "_ENGINE_ROOT", tmp_path), mock.patch.object(
            invoke_from_argv, "_WARM_ENTRYPOINT_ALLOWLIST", frozenset({name})
        ):
            result = _run_entrypoint(name, ["brief", "--help"], str(tmp_path))

        assert result["exit_code"] == 0
        assert result["stderr"] == ""
        assert "brief <kind>" in result["stdout"]
        assert builtins._ENTRYPOINT_OP_ENTERED is False
    finally:
        del builtins._ENTRYPOINT_OP_ENTERED


def test_short_help_flag_also_delegates(tmp_path):
    name = "fake-entrypoint-help-short"
    bin_dir = tmp_path / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    _write_delegating_entrypoint(bin_dir, name, real_help_branch=True)

    with mock.patch.object(invoke_from_argv, "_ENGINE_ROOT", tmp_path), mock.patch.object(
        invoke_from_argv, "_WARM_ENTRYPOINT_ALLOWLIST", frozenset({name})
    ):
        result = _run_entrypoint(name, ["-h"], str(tmp_path))

    assert result["exit_code"] == 0
    assert "brief <kind>" in result["stdout"]


def test_normal_invocation_still_reaches_main_fn(tmp_path):
    """No-flag invocation is unaffected — `main_fn` is genuinely called,
    proven by a fake target that mutates a shared record when actually
    entered (rather than merely asserting exit_code, which a --help
    short-circuit could also satisfy)."""
    bin_dir = tmp_path / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    name = "fake-entrypoint-normal-invocation"
    (bin_dir / f"{name}.py").write_text(
        "import builtins\n"
        "\n"
        "\n"
        "def main(argv):\n"
        "    builtins._ENTRYPOINT_NORMAL_INVOCATION_SEEN = list(argv)\n"
        "    return 0\n",
        encoding="utf-8",
    )

    import builtins

    builtins._ENTRYPOINT_NORMAL_INVOCATION_SEEN = None
    try:
        with mock.patch.object(invoke_from_argv, "_ENGINE_ROOT", tmp_path), mock.patch.object(
            invoke_from_argv, "_WARM_ENTRYPOINT_ALLOWLIST", frozenset({name})
        ):
            result = _run_entrypoint(name, ["some", "args"], str(tmp_path))

        assert result["exit_code"] == 0
        assert result["stderr"] == ""
        assert builtins._ENTRYPOINT_NORMAL_INVOCATION_SEEN == ["some", "args"], (
            "main_fn must actually run and see the real argv for a no-flag call"
        )
    finally:
        del builtins._ENTRYPOINT_NORMAL_INVOCATION_SEEN


def test_the_servers_own_session_env_is_restored_after_the_call(tmp_path, monkeypatch):
    """Borrowed, not taken. `os.environ` is process-global in a server ~50
    sessions share — the same restore discipline the cwd and `sys.argv` borrows
    beside it already carry."""
    _, after = _run_session_env_probe(
        tmp_path,
        monkeypatch,
        carried=_CALLER_SID,
        server_env={"CLAUDE_CODE_SESSION_ID": _SERVER_OWNER_SID},
    )

    assert after == {
        "COORDINATOR_SESSION_ID": None,
        "CLAUDE_SESSION_ID": None,
        "CLAUDE_CODE_SESSION_ID": _SERVER_OWNER_SID,
    }
