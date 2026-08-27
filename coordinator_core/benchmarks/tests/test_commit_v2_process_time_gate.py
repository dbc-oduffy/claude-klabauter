"""
coordinator_core.benchmarks.tests.test_commit_v2_process_time_gate

C6 of docs/plans/2026-08-27-something-must-commit-ceremony-commit-v2.md --
"The process-time gate, wired to the instrument that can see a warm-served
op." A STANDING GATE, not a one-off report: asserts the v2 op
(`ceremony.commit_v2`, C3) against the prime exit criterion through the
installed door -- <=50ms process time and <=1 job-object process beyond the
warm server, on the unpinned-LF (plain tracked-file-edit) shape; the
`eol=crlf` shape is measured and REPORTED alongside, never folded into the
gated shape's figures, because its 2-spawn fallback path (C4) is a known,
accepted cost on a different shape, not a regression of the LF path.

BRACKETED, NEVER PER-CALL (plan Problem section, C1's own retraction).
`LiveTreeAccountant` quantises job-object CPU to a ~15.625ms tick, so a
`snapshot()` pair around ONE dispatch returns a tick COUNT, not a cost, and
a threshold within one tick of the measurement is a coin flip. Every figure
here is N dispatches inside ONE job window, divided by N, repeated across
`WINDOWS` independent windows; the gate asserts on the bracketed MEAN across
windows and reports the spread (min/max) beside it -- never a single
window's figure, and never a per-call snapshot pair.

ATTACH-BEFORE-WARMTH (mandatory correction carried from C1, see this
plan's own "CORRECTION to C1's figure" section and the dispatch brief's
CORRECTION IN FLIGHT block). `warm/server.py :: _pool_dispatch` builds its
`ProcessPoolExecutor` lazily on the FIRST dispatch, and the POOL WORKER --
not the server process -- executes every op this file measures.
`LiveTreeAccountant` counts by job-object membership, fixed at process
creation: a worker spawned before the accountant attaches is invisible to
it forever. This file therefore enters `LiveTreeAccountant` BEFORE firing
even the warmth probe (never C1's original, retracted ordering), and
ASSERTS the pool landed inside the job afterward -- the same "assert
warmth, never infer it" discipline this file already applies to the
warm/cold distinction, applied to job membership too, because nothing else
in the suite would catch a silent regression back to the understating
ordering.

TWO AXES, NEVER BLENDED (SECOND CORRECTION, dispatch brief). `ipc.py`'s
sink records `time.process_time()` of the measuring process alone; this
file (like C1) reports job-object CPU across the whole server tree. Both
figures gated here come from the SAME axis (job-object, via
`LiveTreeAccountant`) -- this file never cites the sink's `process_time()`
figure and never subtracts one axis from the other.

REUSE, NOT A THIRD FIXTURE SHAPE. The isolated-server boot recipe
(hardlink `coordinator_core/`, stamp, boot `python -m
coordinator_core.warm.server`, poll a PID-alive breadcrumb) is the same
shape `test_commit_op_wallclock_budget.py::warm_engine_root` and
`test_commit_v2_floor_spike.py::warm_root` already use -- duplicated here
rather than imported (a pytest fixture cannot be called directly outside
pytest's own fixture protocol, the constraint both sibling files' own
docstrings record) -- but this file gates PROCESS TIME and SPAWN COUNT
only, never wallclock (DR-344/CLAUDE.md: wallclock is excluded from every
gate on this repo; `test_commit_op_wallclock_budget.py` owns that axis
under its own AC4/AC7).

EOL=CRLF SHAPE, MEASURED FINDING (this chunk, not assumed from the plan's
text). The plan's C6 body expects the `eol=crlf` shape to be "reported
separately with its 2 spawns" -- i.e. that CR-bearing content under an
`eol=crlf` pin reaches C4's batched fallback and lands. Probed directly
against the current `ceremony.commit_v2` handler (`coordinator_core/ops/
ceremony/commit_v2.py`, C3): the handler calls `commit_paths` with no
`blob_fallback` supplied (its own docstring says so explicitly), so a CR-
bearing `eol=crlf` path is REFUSED in process (`committed: false`, a
structured `FilterUnsupported` error) rather than routed to the 2-spawn
fallback -- C4 landed the fallback CAPABILITY inside `commit.py` itself, but
the op-level wiring that would pass a `blob_fallback` callable into
`commit_paths` from `commit_v2.py` is not present on this tree today. This
file measures and reports that REFUSAL path honestly (0 job-object spawns,
`committed: false` on every sample) rather than asserting the 2-spawn
success figure the plan's prose anticipated -- a design/wiring gap in
`commit_v2.py`, outside this chunk's `writes:` scope (`coordinator_core/
benchmarks/tests/` only) to fix. Recorded here as a MEASURED finding for
whichever chunk owns wiring `blob_fallback` into the op, not remediated by
this one. The `eol=crlf` leg stays UNGATED either way (this chunk's own
body: "reported separately"), so this finding does not fail the gate this
file exists to enforce -- it changes what the reported numbers mean.

DOOR COLD-MISS TRAP (dispatch brief, this plan's C6 body verbatim). The
door's cold fallback spawns `{engine_root}\\coordinator\\bin\\coordinator-
invoke.py`, absent from this file's hardlink clone, and the ordinary
fallback path prints nothing by design -- so a warm MISS here surfaces as
`rc=2, can't open file`, never as a diagnosis naming warmth. This file
never infers warmth from a successful-looking response; it asserts rc==0
on an explicit warmth probe and asserts job-object growth after it, before
trusting any figure gated below.

REAL CHECKIN SURFACE, NOT AN LF-ONLY FIXTURE (plan anti-scope: "do not
claim green off an LF-only fixture corpus"). Both shapes below run against
a repo carrying this repo's own `.gitattributes` pins (imported from
`coordinator_core.git.tests.conftest.REAL_GITATTRIBUTES`, the same corpus
C2 built), `core.autocrlf=true`, `core.fileMode=false` -- a suite that
cannot fail on a `.gitattributes`-pinned path is not evidence about this
repo.

Spec backlink: docs/plans/2026-08-27-something-must-commit-ceremony-commit-
v2.md, C6. Depends on C3 (`ceremony.commit_v2` op) existing to gate.

NEGATIVE SPEC: writes no production code; asserts against the installed
`ceremony.commit_v2` op only, never against `ceremony.commit` (dead, stays
dead) and never against `run_commit_pipeline`/`git_native.py` directly.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Callable, Iterator, List, Optional

import pytest

from coordinator_core.benchmarks.process_time import IS_WINDOWS, LiveTreeAccountant
from coordinator_core.benchmarks.isolated_clone import (
    mkdtemp_for_clone,
    reap_processes_under,
    rmtree_or_raise,
)
from coordinator_core.git.tests.conftest import REAL_GITATTRIBUTES
from coordinator_core.warm import breadcrumb
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

WINDOW_N = 40
"""Dispatches per job window -- matches C1's own figure. The tick is
~15.625ms, so N=40 divides the quantisation error by 40, roughly +/-0.4ms
at the reported per-call figure -- resolvable against a 50ms bar where a
single call is not."""

WINDOWS = 3
"""Independent windows. The spread across them is the honest error bar; a
single window's figure is a point estimate with no way to see its own
noise."""

OP_NAME = "ceremony.commit_v2"

AXIS_PROCESS_TIME = "process_time_ms (job-object CPU, LiveTreeAccountant, bracketed mean)"
AXIS_PROCS = "procs (job-object TotalProcesses beyond the warm server, LiveTreeAccountant, bracketed mean)"

PROCESS_TIME_TARGET_MS = 50.0
"""Prime exit criterion (plan Problem section / sizing object): "commits a
scoped pathspec ... at or under 50ms process time through the installed
door ... on the tracked-file-edit shape"."""

PROCS_TARGET = 1.0
"""This chunk's own dispatch-brief body, verbatim: "<=1 proc beyond the
server ... on the unpinned-LF shape". The `eol=crlf` shape's 2 spawns are
reported separately (below) and never checked against this target."""

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_BOOT_WAIT_SECS = 90.0
_POLL_SECS = 0.2
_SUBPROCESS_TIMEOUT_S = 120

_INSTALLED_DOOR_EXE = (
    Path(os.path.expanduser("~"))
    / ".coordinator-claude-settings"
    / "bin"
    / "coordinator-invoke.exe"
)
"""Resolved at IMPORT time, before the suite root's `conftest.py`
quarantines `HOME`/`USERPROFILE` into a throwaway dir for every unmarked
test -- a `Path.home()`-style call inside a fixture or test body would
resolve to the quarantine and silently skip this gate on a box that has the
door installed (`test_commit_op_wallclock_budget.py`'s own identical
constant and its docstring's measured 2/2 skip)."""


def _require_windows() -> None:
    if not IS_WINDOWS:
        pytest.skip("job-object accounting is Windows-only")


def _source_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _hardlink_coordinator_core(source_root: Path, isolated_root: Path) -> int:
    n = 0
    src_pkg = source_root / "coordinator_core"
    for root, dirs, files in os.walk(src_pkg):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel = Path(root).relative_to(src_pkg)
        dst_dir = isolated_root / "coordinator_core" / rel
        dst_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            os.link(Path(root) / name, dst_dir / name)
            n += 1
    return n


def _write_isolated_stamp(isolated_root: Path) -> None:
    """A valid `_engine_stamp` in the ISOLATED destination only. Without it
    the door's root validation fails SILENTLY and the dial is served by the
    machine's PUBLISHED engine instead -- measuring a different tree under
    this tree's label."""
    (isolated_root / "coordinator_core" / "_engine_stamp").write_text(
        f"sha:commit-v2-process-time-gate-{uuid.uuid4().hex}\n", encoding="utf-8"
    )


def _env(engine_root: Path) -> dict:
    env = dict(os.environ)
    env["COORDINATOR_WARM"] = "1"
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(engine_root)
    env.pop("VIRTUAL_ENV", None)
    return env


@pytest.fixture(scope="module")
def warm_root() -> Iterator[tuple]:
    """Isolated warm server, booted with NO warmth probe fired yet -- the
    accountant must attach before the first dispatch of any kind, per the
    ATTACH-BEFORE-WARMTH correction in this module's own docstring. Deliberate
    duplicate of `test_commit_v2_floor_spike.py::warm_root`'s own recipe."""
    _require_windows()
    source_root = _source_root()
    if not (source_root / "coordinator_core").is_dir():
        pytest.skip(f"{source_root} carries no coordinator_core/ to hardlink")

    tmp_parent = mkdtemp_for_clone(source_root, prefix="commit-v2-process-time-gate-")
    root = tmp_parent / "clone"
    proc: Optional[subprocess.Popen] = None
    try:
        _hardlink_coordinator_core(source_root, root)
        _write_isolated_stamp(root)
        proc = subprocess.Popen(
            [sys.executable, "-m", "coordinator_core.warm.server"],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_CREATE_NO_WINDOW,
        )
        import time

        deadline = time.time() + _BOOT_WAIT_SECS
        crumb = None
        while time.time() < deadline:
            crumb = breadcrumb.read_breadcrumb(engine_root=root)
            if crumb and crumb.get("pid"):
                break
            time.sleep(_POLL_SECS)
        if not (crumb and crumb.get("pid")):
            pytest.skip(f"isolated warm server did not boot within {_BOOT_WAIT_SECS}s")
        # Captured HERE and yielded, never re-read later -- the breadcrumb is
        # the server's own liveness record and re-reading it after the fact
        # can return None while the process is still up and serving (see the
        # sibling C1 spike's identical note).
        yield root, int(crumb["pid"])
    finally:
        crumb = breadcrumb.read_breadcrumb(engine_root=root)
        if crumb and crumb.get("pid"):
            try:
                import psutil

                p = psutil.Process(int(crumb["pid"]))
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                pass
        elif proc is not None and proc.poll() is None:
            proc.terminate()
        reaped = reap_processes_under(tmp_parent)
        shutil.rmtree(breadcrumb.svc_dir(engine_root=root), ignore_errors=True)
        rmtree_or_raise(tmp_parent, label="commit_v2_process_time_gate", reaped=reaped)


# ---------------------------------------------------------------------------
# Fixture repos -- this repo's real checkin surface (C2's own corpus), never
# an LF-only stand-in (plan anti-scope).
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=check,
        **no_console_creationflags(),
    )


def _build_checkin_repo(tmp_path: Path, name: str) -> Path:
    """A repo carrying this repo's real `.gitattributes` pins,
    `core.autocrlf=true`, `core.fileMode=false` -- the identical recipe
    `coordinator_core/git/tests/test_checkin_surface_fixtures.py
    ::checkin_repo_factory` uses, duplicated here (that fixture is
    module-private to its own file, and this file's own docstring already
    names why fixtures are not imported across pytest's protocol
    boundary)."""
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "c6-process-time@example.com")
    _git(repo, "config", "user.name", "c6-process-time")
    _git(repo, "config", "core.autocrlf", "true")
    _git(repo, "config", "core.fileMode", "false")
    (repo / ".gitattributes").write_bytes(REAL_GITATTRIBUTES.encode("utf-8"))
    _git(repo, "add", "--", ".gitattributes")
    _git(repo, "commit", "-q", "-m", "seed attributes")
    return repo


def _door_argv(door: Path, params_path: Path, repo: Path) -> List[str]:
    return [
        str(door), OP_NAME,
        "--params-file", str(params_path), "--repo", str(repo),
        "--allow-unstamped-dispatch",
    ]


def _write_params(params_path: Path, *, paths: List[str], message: str) -> None:
    params_path.write_text(
        json.dumps({"paths": paths, "message": message}), encoding="utf-8"
    )


def _dispatch(argv: List[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=env,
        timeout=_SUBPROCESS_TIMEOUT_S,
        creationflags=_CREATE_NO_WINDOW,
    )


def _parse_committed(stdout: str) -> Optional[bool]:
    """`result.committed` from a `ceremony.commit_v2` JSON-RPC envelope, or
    `None` if stdout was not a parseable envelope at all (a transport-level
    failure, distinct from a structured `committed: false` refusal)."""
    try:
        parsed = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    result = parsed.get("result") if isinstance(parsed, dict) else None
    if not isinstance(result, dict):
        return None
    return bool(result.get("committed"))


def _bracketed_window(
    accountant: LiveTreeAccountant,
    dispatch_one: Callable[[int], int],
    n: int,
) -> dict:
    """N dispatches inside ONE job window -- divides the ~15.625ms tick
    error by n rather than paying it per sample (module docstring:
    BRACKETED, NEVER PER-CALL). `dispatch_one(i)` performs the i-th call's
    own harness-side content mutation (outside any timing this function
    does -- this function measures job-object CPU across the whole window,
    not per-call wallclock, so mutation cost is irrelevant to what it
    reports) and returns that call's returncode."""
    before = accountant.snapshot()
    rcs = [dispatch_one(i) for i in range(n)]
    after = accountant.snapshot()
    return {
        "ms_per_call": round(
            (after["process_time_ms"] - before["process_time_ms"]) / n, 3
        ),
        "procs_per_call": round((after["procs"] - before["procs"]) / n, 3),
        "rc_ok": sum(1 for r in rcs if r == 0),
        "n": n,
    }


def _run_windows(
    accountant: LiveTreeAccountant, dispatch_one: Callable[[int], int]
) -> List[dict]:
    return [_bracketed_window(accountant, dispatch_one, WINDOW_N) for _ in range(WINDOWS)]


def _mean(values: List[float]) -> float:
    return round(statistics.mean(values), 3)


def test_c6_commit_v2_process_time_gate(warm_root, tmp_path_factory) -> None:
    warm_root_path, server_pid = warm_root
    door = _INSTALLED_DOOR_EXE
    if not door.exists():
        pytest.skip(f"{door} not installed on this box")
    env = _env(warm_root_path)

    lf_repo = _build_checkin_repo(tmp_path_factory.mktemp("commit_v2_lf"), "lf_repo")
    lf_rel = "src/tracked.txt"
    (lf_repo / "src").mkdir(parents=True, exist_ok=True)
    (lf_repo / lf_rel).write_text("seed\n", encoding="utf-8")
    _git(lf_repo, "add", "--", lf_rel)
    _git(lf_repo, "commit", "-q", "-m", "seed tracked file")
    lf_params_path = lf_repo / ".c6-lf-params.json"
    lf_committed: List[Optional[bool]] = []

    def _dispatch_lf(i: int) -> int:
        (lf_repo / lf_rel).write_text(f"harness rev {i}\n", encoding="utf-8")
        _write_params(lf_params_path, paths=[lf_rel], message=f"c6 lf rev {i}")
        completed = _dispatch(_door_argv(door, lf_params_path, lf_repo), env)
        lf_committed.append(_parse_committed(completed.stdout))
        return completed.returncode

    crlf_repo = _build_checkin_repo(tmp_path_factory.mktemp("commit_v2_crlf"), "crlf_repo")
    crlf_rel = "coordinator/bin/launcher.cmd"
    (crlf_repo / "coordinator" / "bin").mkdir(parents=True, exist_ok=True)
    (crlf_repo / crlf_rel).write_bytes(b"@echo seed\r\n")
    _git(crlf_repo, "add", "--", crlf_rel)
    _git(crlf_repo, "commit", "-q", "-m", "seed eol=crlf file")
    crlf_params_path = crlf_repo / ".c6-crlf-params.json"
    crlf_committed: List[Optional[bool]] = []

    def _dispatch_crlf(i: int) -> int:
        (crlf_repo / crlf_rel).write_bytes(f"@echo rev {i}\r\n".encode("ascii"))
        _write_params(crlf_params_path, paths=[crlf_rel], message=f"c6 crlf rev {i}")
        completed = _dispatch(_door_argv(door, crlf_params_path, crlf_repo), env)
        crlf_committed.append(_parse_committed(completed.stdout))
        return completed.returncode

    with LiveTreeAccountant(server_pid) as acct:
        # ATTACH-BEFORE-WARMTH (mandatory correction). This warmth probe is
        # the FIRST dispatch through this isolated server -- the accountant
        # is already attached, so the pool workers it spawns land INSIDE the
        # job. See module docstring's ATTACH-BEFORE-WARMTH section.
        procs_before_probe = acct.snapshot()["procs"]
        probe = _dispatch([str(door), "ping", "{}"], env)
        assert probe.returncode == 0, (
            f"warmth precondition FAILED: ping through the door rc={probe.returncode} "
            f"stdout={probe.stdout[:300]!r} stderr={probe.stderr[:300]!r} -- a "
            "cold miss on this isolated clone surfaces as this non-zero rc "
            "(the door's cold fallback spawns coordinator-invoke.py, absent "
            "here, and prints nothing per this module's own DOOR COLD-MISS "
            "TRAP note), never as a diagnosis naming warmth."
        )
        # Do not trust the ordering above by inspection alone -- assert the
        # pool workers actually landed inside the job (module docstring).
        procs_after_probe = acct.snapshot()["procs"]
        assert procs_after_probe > procs_before_probe, (
            f"[{AXIS_PROCS}] pool workers were not observed inside the job "
            f"after the warmth probe (window size n/a -- this is the "
            f"pre-window attach assertion): procs before={procs_before_probe} "
            f"after={procs_after_probe} -- the accountant may have attached "
            "AFTER the pool was already spawned, which silently excludes the "
            "workers doing every op's real work from every figure below."
        )

        lf_windows = _run_windows(acct, _dispatch_lf)
        crlf_windows = _run_windows(acct, _dispatch_crlf)

    print("\n" + "=" * 78)
    print(f"C6 -- {OP_NAME} PROCESS-TIME GATE (job-object, bracketed)")
    print(f"bracketed: {WINDOWS} windows x {WINDOW_N} dispatches, divided by N")
    print("=" * 78)
    for label, windows in (("unpinned-LF (gated)", lf_windows), ("eol=crlf (reported)", crlf_windows)):
        ms = [w["ms_per_call"] for w in windows]
        procs = [w["procs_per_call"] for w in windows]
        ok = sum(w["rc_ok"] for w in windows)
        tot = sum(w["n"] for w in windows)
        print(
            f"{label:<22} ms/call {' / '.join(f'{m:.2f}' for m in ms):>26}   "
            f"procs/call {' / '.join(f'{p:.2f}' for p in procs):>20}   rc0 {ok}/{tot}"
        )
    print("=" * 78)

    # rc sanity, both arms -- a failed dispatch under either shape means the
    # figures below describe something other than a successful commit.
    for label, windows in (("unpinned-LF", lf_windows), ("eol=crlf", crlf_windows)):
        for w in windows:
            assert w["rc_ok"] == w["n"], (
                f"{label} shape had failed dispatches (window n={w['n']}): {w}"
            )
            assert w["ms_per_call"] > 0.0, (
                f"[{AXIS_PROCESS_TIME}] {label} shape (window n={w['n']}) measured "
                f"0.00ms/call -- a bracketed window cannot be free; the instrument "
                f"is not attached to the work. {w}"
            )

    # The LF shape is GATED, so a call that transports rc==0 but refuses to
    # commit (committed: false) would silently corrupt the gate below with
    # cheap-refusal timings wearing a "success" label -- assert the real
    # commit landed on every sample.
    assert all(lf_committed), (
        f"[gate corruption guard] arm=unpinned-LF: {lf_committed.count(False)} "
        f"of {len(lf_committed)} dispatches transported rc==0 but returned "
        f"committed=false (or an unparseable envelope) -- the process-time "
        f"gate below would be measuring refusals, not commits. "
        f"committed flags: {lf_committed}"
    )
    crlf_ok = sum(1 for c in crlf_committed if c)
    print(
        f"eol=crlf shape commit outcome: {crlf_ok}/{len(crlf_committed)} actually "
        f"committed (committed=true); see this module's docstring's "
        f"EOL=CRLF SHAPE, MEASURED FINDING section -- the current op handler "
        f"supplies no blob_fallback, so CR-bearing content under this pin is "
        f"REFUSED in process today, which is why this shape's spawn count "
        f"below reads far under the plan's anticipated 2-spawn fallback figure."
    )

    # --- THE GATE: unpinned-LF shape only, prime exit criterion. ---
    lf_ms_values = [w["ms_per_call"] for w in lf_windows]
    lf_procs_values = [w["procs_per_call"] for w in lf_windows]
    lf_ms_mean = _mean(lf_ms_values)
    lf_procs_mean = _mean(lf_procs_values)

    assert lf_ms_mean <= PROCESS_TIME_TARGET_MS, (
        f"[{AXIS_PROCESS_TIME}] arm=unpinned-LF window_size={WINDOW_N} "
        f"windows={WINDOWS}: bracketed mean {lf_ms_mean}ms exceeds the "
        f"{PROCESS_TIME_TARGET_MS}ms prime exit criterion by "
        f"{round(lf_ms_mean - PROCESS_TIME_TARGET_MS, 3)}ms "
        f"(per-window: {lf_ms_values})"
    )
    assert lf_procs_mean <= PROCS_TARGET, (
        f"[{AXIS_PROCS}] arm=unpinned-LF window_size={WINDOW_N} "
        f"windows={WINDOWS}: bracketed mean {lf_procs_mean} procs/call exceeds "
        f"the {PROCS_TARGET} prime exit criterion by "
        f"{round(lf_procs_mean - PROCS_TARGET, 3)} (per-window: {lf_procs_values})"
    )

    # --- REPORTED, NOT GATED: eol=crlf shape, its known 2-spawn fallback. ---
    crlf_ms_values = [w["ms_per_call"] for w in crlf_windows]
    crlf_procs_values = [w["procs_per_call"] for w in crlf_windows]
    print(
        f"eol=crlf shape (reported, NOT gated against the LF target): "
        f"process_time mean={_mean(crlf_ms_values)}ms (per-window {crlf_ms_values}) "
        f"procs mean={_mean(crlf_procs_values)} (per-window {crlf_procs_values}) -- "
        f"{crlf_ok}/{len(crlf_committed)} committed=true (see MEASURED FINDING "
        f"docstring section: today this measures the in-process REFUSAL path, "
        f"0 spawns, not C4's anticipated 2-spawn fallback success path)"
    )
