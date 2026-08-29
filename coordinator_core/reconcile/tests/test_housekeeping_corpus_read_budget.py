"""The 200ms bar for the whole handoff-housekeeping job, as a standing guard.

Governing plan:
`docs/plans/2026-08-27-one-corpus-read-or-the-housekeeping-job-dies-a-fourth-time.md`,
chunk C6. This is the TIMING half. The dispatchability half — which key resolves,
and which module may reach the archival leg — is
`coordinator_core/tests/test_housekeeping_is_the_one_job.py`, because a timing
test cannot see a second door being opened onto the same compute.

WHAT IS MEASURED, AND WITH WHAT. The whole process TREE, through
`benchmarks/process_time.py :: single_invocation_tree_process_time` (a Windows
job object / Darwin kqueue+wait4). Never `os.times()`, whose
`children_user`/`children_system` are always 0.0 on Windows: a gate built on it
reads every git child as free, passes an implementation that shells out forty
times, and is the exact instrument defect that understated THIS PLAN'S OWN
floor by 4x (187.5ms, not the 46.9ms the plan carried until `4c420bc79`). A gate
blind to spawned children is not a weaker gate; it is a gate that certifies the
defect it exists to catch.

THE FIGURE IS A DIFFERENCE OF WARM CALLS, not a cold reading. The job only
EVER runs warm in production: `docs/plans/2026-08-27-one-corpus-read-or-the-
housekeeping-job-dies-a-fourth-time.md` § Anti-scope — "The job must not be
reachable by a cold CLI spawn" and "A CLI door that spawns a fresh interpreter
to do housekeeping is this plan failing even if every other number is green."
The door satisfies `warm/serve_classifier.py`, and the ceremonies that call it
(`workday-complete`, `workweek-complete`) run it in-process inside a live,
already-warm session. A gate that pays cold interpreter start and cold
cache-fill on every sample is measuring an occasion production never takes —
`docs/research/2026-08-27-the-archival-per-invocation-figure-decomposed.md`
found `archive_and_commit`'s op body 46.9ms cold and 0.0ms warm, entirely
cache-fill, and an in-process 6-call trace of the whole job found call 1 at
250.0ms and calls 2-6 stable at a 187.5ms p50 — full warmth by the SECOND
call, not a slow asymptote requiring many.

So this file no longer subtracts a FLOOR script's interpreter-and-import cost
from a single cold FULL call. It subtracts a LOW script's cost (one `_handler`
call, itself paying the cold interpreter/import price) from a HIGH script's
cost (two `_handler` calls, over a second, independently-built, identically-
shaped fixture). Both scripts pay the same cold start; the delta is exactly
the marginal, WARM second call — the occasion the job actually runs on. Each
call runs over its OWN freshly-built fixture (never a shared, reused one) so
neither script's second call is measuring a corpus the first call already
swept: see `_measure_one_pair` and the WARM/equal-work note below for why a
shared fixture was rejected. Measuring either script alone would still gate on
the DOOR's interpreter/import budget (`coordinator_core/warm/tests/
test_handoff_housekeeping_warm_serves.py` holds that half), not this one's.

EQUAL WORK PER CALL, verified empirically before this shape was trusted. An
earlier attempt at this same differential (loop N `_handler` calls in one
process over a shared, growing fixture, see plan handoff) produced incoherent
deltas — negative, and a K+1-call script reporting FEWER processes than a
K-call script — traced to calls 1..k-1 each reporting `archived=0` and only
the LAST call reporting the full `archived=8`: the fixture was being consumed
across calls (step 3's commit changes what step 2's re-scan sees), so later
calls in the loop had less work left to do, and the "delta" was measuring
fixture decay, not warmth. The fix here is that every call — LOW and HIGH
alike — gets its OWN independently `_build_corpus`-ed fixture, never a shared
or reused one; confirmed by direct reproduction (4 independent fixtures fed to
4 sequential in-process `_handler` calls) that every call then reports
`archived=8`, identically, regardless of position in the sequence. Warmth
comes from the shared PROCESS (imports, git pack cache, OS page cache), not
from a shared fixture — the two were conflated in the earlier attempt.

QUANTISATION. Windows job accounting lands on ~15.6ms scheduler ticks, and a
difference of two readings carries the noise of both. `n=5` pairs are built
and measured, and the assertion is on the MEDIAN delta — a single sample near a
200ms bar measures the tick, not the job. Fixtures cannot be reused across
samples because step 3 mutates them, which is why this is a median over paired
one-shots rather than `batched_process_time_ms`.

THE BAR IS 200ms. Not `SUSPENSION_BAR_MS` (2000ms), which is a
which-to-switch-off-first ordering and never a target; a gate citing 2s here
would be a defect in the gate. Process time and spawn count only — wall clock on
a box running 50-70 concurrent sessions measures the peers.

WHAT A BREACH MEANS. Over the bar, the plan's own occasions ruling ships
housekeeping on the four unconditional day/week ceremonies only; under it, the
second tier (`/pickup` lazily, `/workstream-start`, `/quick-wrap`,
`/workstream-complete`) is earned. The verdict is this gate's output, so a
failure here is a scope answer and not only a red test — do not widen
`_BRIGHTLINE_MS` to recover the tier.

Negative-spec:
  - Does NOT assert wall clock, and does not report one as evidence.
  - Does NOT measure against the live repository corpus. The job's step 3
    commits; a gate that mutated the operator's tree to take its own reading
    would be the worst-behaved thing in this file.
  - Does NOT re-test composition, ordering, or leg failure semantics
    (`coordinator_core/ops/tests/test_handoff_housekeeping.py` owns those), nor
    plan_sweep's own rails and spawn ratchet
    (`coordinator_core/ops/fleet/tests/test_archive_terminal_handoffs.py`).
  - Does NOT assert registry membership or op-key shape. See the sibling module
    for why that predicate is non-deterministic.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import statistics
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from coordinator_core.benchmarks.process_time import (
    IS_DARWIN,
    IS_WINDOWS,
    single_invocation_tree_process_time,
)
from coordinator_core.win_portability import no_console_creationflags

# Real-git spawn is load-bearing: the job's third step commits, and a gate that
# stubbed the committer would measure two thirds of the thing it gates.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: DR-344's brightline for one process, end to end. A SECOND independent
#: literal, deliberately not imported from `op_budget_suspension.PROCESS_BAR_MS`
#: — same discipline as `test_op_suspension_ratchet.py`'s own second literal of
#: the suspension bar. Lifting the bar in the source is then not enough to make
#: the tree green; it has to be lifted twice, which is the point at which it
#: stops being a quiet retune and becomes an argument with the PM.
_BRIGHTLINE_MS = 200.0

#: `git --version` costs 25.3ms of process time on this box (CLAUDE.md
#: § The brightline) — process creation IS the cost, not the query. The spawn
#: ceiling is therefore DERIVED from the time bar rather than picked: at 25.3ms
#: apiece, seven git children exhaust the 200ms budget on process creation alone
#: with nothing left for the job. A hand-chosen spawn number here would be a
#: threshold tuned to whatever the implementation happens to do.
_GIT_SPAWN_COST_MS = 25.3
_MAX_GIT_SPAWNS = int(_BRIGHTLINE_MS // _GIT_SPAWN_COST_MS)

#: DR-373: on Windows each git child arrives with a `conhost.exe`, so the job
#: object counts two processes per git spawn. Darwin has no such pairing.
_PROCS_PER_GIT_SPAWN = 2 if IS_WINDOWS else 1
_MAX_PROC_DELTA = _MAX_GIT_SPAWNS * _PROCS_PER_GIT_SPAWN

#: Representative size, taken from the real corpus at `4803b5ba5`: 271 live
#: records under `state/handoffs/`. Not a round number and not a convenience —
#: the bar is meaningless against a fixture smaller than the thing it gates.
_CORPUS_SIZE = 271
_CAP = _CORPUS_SIZE + 29

#: How many of those 271 are TERMINAL, i.e. actually get moved. This is the
#: axis the first version of this file got wrong, and the error was not
#: conservative -- it was measuring a different job.
#:
#: A ceremony's housekeeping pass pays the WALK every time and the MOVE only for
#: what has since gone terminal. Seeding all 271 as terminal makes every run an
#: archive-271-handoffs-in-one-call, whose cost is per-item mover work the cap
#: exists to bound -- not the steady-state cost the tier verdict rests on. It also
#: made both read censuses count the mover's own destination reads as if they
#: were a corpus walk: measured on the real tree at `18bc82baa`, a run that moves
#: nothing reads the archive ONCE and the live corpus 1.13x per record; the
#: all-terminal fixture reported 271 archived reads and 5.00x live, every one of
#: the extras belonging to the 271 moves. A gate that reports the mover as a walk
#: sends the next reader hunting an eager index that is not there.
#:
#: So the two questions are asked separately, against two fixtures:
#:   `measurement` / `read_census`  -- steady state, 8 terminal. Owns the BAR and
#:                                    the no-walk assertions.
#:   `bulk_measurement`             -- all 271 terminal. Owns the PER-MOVE cost,
#:                                    asserted per item, never as a flat total.
#: 8 is the observed order of magnitude for terminal candidates on a real
#: ceremony pass, and it is deliberately not 0: a fixture the job declines to
#: touch would measure a walk with the commit path never entered.
_STEADY_TERMINAL = 8

#: What a non-terminal record carries. NOT cosmetic — this constant is the
#: difference between measuring the steady state and measuring a worst case.
#:
#: `_scan_terminal`'s pre-filter (`_prefilter_scan_disqualifies`) refuses a
#: record byte-level, WITHOUT paying `dag._read_meta`'s full read+sha256+YAML
#: parse, only when BOTH `status` and `deployment_state` are readable plain
#: scalars. Its documented fall-through list is closed, and "either target key
#: absent" is on it. So a fixture record with no `deployment_state` cannot be
#: refused cheaply -- it falls through to the full parse, every time.
#:
#: This fixture used to omit the key. Measured 2026-08-29, `plan_sweep` over
#: 271 records:
#:     fixture, key absent : 0 of 271 refused (0.0%), 271 full parses (1.00/rec)
#:     real corpus at HEAD : 244 of 248 refused (98.4%), 4 full parses (0.02/rec)
#: Every live record on the real corpus carries the key (204 ready_to_fire,
#: 22 in_flight, 19 awaiting_gate, 2 shipped, 1 delivered). The fixture was
#: making all 271 take the expensive path where production takes it 4 times,
#: so the bar was being defended against a job that does not exist.
#:
#: This is the SECOND time this fixture measured a worst case while reading as
#: steady state. The first was seeding all 271 records terminal, corrected to
#: `_STEADY_TERMINAL`; the population fraction was fixed and the pre-filter
#: fraction was not. Before trusting any per-record ratio here, ask what
#: fraction of the fixture is supposed to be doing the expensive thing.
#:
#: `ready_to_fire` is the real corpus's dominant value (204 of 248) and is not
#: in `_TERMINAL_DEPLOYMENT_STATES` ({shipped, continued, closed}), so it does
#: not change which records qualify as a move -- only how cheaply the sweep can
#: decline the ones that never did. Deliberately NOT `awaiting_gate`: that is
#: the one state whose `blocked_by` resolution legitimately needs the
#: live+archived gate index, which would make the eager-walk assertion
#: untestable (see `_build_corpus`).
_NONTERMINAL_DEPLOYMENT_STATE = "ready_to_fire"

#: Paired one-shots. Enough that the median is not one tick's worth of luck,
#: few enough that a cadence run does not occupy a box carrying 50-70 peers.
_SAMPLES = 5

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    # popup-intentional-last-resort — test-only real-git fixture spawn, mirroring
    # `coordinator_core/ops/fleet/tests/test_archive_terminal_handoffs.py`'s own
    # helper rather than inventing a second one. Never inside a measured window.
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env=_GIT_ENV, timeout=60,
        stdin=subprocess.DEVNULL, **no_console_creationflags(),
    )
    assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result


def _build_corpus(root: Path, terminal: int = _STEADY_TERMINAL) -> Path:
    """A committed, clean repository of `_CORPUS_SIZE` handoffs, `terminal` of
    which qualify for a move.

    `status: claimed` with no `deployment_state` is the shape
    `test_archive_terminal_handoffs.py :: _seed_bulk_claimed` already proves
    plans as a move — reused rather than re-derived, so this fixture cannot
    drift away from the sweep's own terminality rules and quietly start
    measuring a job with nothing to do. The remainder are `status: open` plus
    `deployment_state: _NONTERMINAL_DEPLOYMENT_STATE`, which the sweep refuses
    as not-terminal.

    THE `deployment_state` ON THE NON-TERMINAL REMAINDER IS LOAD-BEARING, and
    this docstring previously claimed those records were "exactly the population
    a ceremony pass walks past" while omitting the key. They were not: without
    it the pre-filter cannot refuse them cheaply and all 271 pay a full parse,
    against 4 of 248 on the real corpus. See `_NONTERMINAL_DEPLOYMENT_STATE` for
    the measurement. Do not remove the key to "simplify" the fixture — that
    silently restores a worst-case measurement wearing a steady-state name.

    None of them is `deployment_state: awaiting_gate`, deliberately — that is the
    ONE state whose `blocked_by` resolution needs the live+archived gate index,
    so a fixture carrying one would make the eager-walk assertion untestable by
    giving the lazy path a legitimate reason to fire.

    Returns the git common dir, which is what `_handler` takes as `repo_root`.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "config", "user.name", "test")
    _git(root, "config", "user.email", "t@t")
    (root / "README.md").write_text("init\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "init")

    handoffs = root / "state" / "handoffs"
    handoffs.mkdir(parents=True, exist_ok=True)
    for i in range(_CORPUS_SIZE):
        name = f"2026-02-{(i % 28) + 1:02d}-corpus-{i}.md"
        if i < terminal:
            # Terminal: `status: claimed`, no `deployment_state` — the shape
            # `test_archive_terminal_handoffs.py :: _seed_bulk_claimed` proves
            # plans as a move. Unchanged.
            front = f"status: claimed\n"
        else:
            # Non-terminal: carries `deployment_state`, because EVERY record on
            # the real corpus does. See `_NONTERMINAL_DEPLOYMENT_STATE`.
            front = f"status: open\ndeployment_state: {_NONTERMINAL_DEPLOYMENT_STATE}\n"
        (handoffs / name).write_text(
            f'---\ntitle: "{name}"\ncreated: 2026-01-01\n{front}'
            f"---\n\nBody.\n",
            encoding="utf-8",
            newline="",
        )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"seed {_CORPUS_SIZE} handoffs ({terminal} terminal)")

    common = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(common.stdout.strip()).resolve()


#: LOW pays one cold `_handler` call; HIGH pays that same cold call plus a
#: SECOND, warm one over its own independent fixture. The delta isolates the
#: marginal warm call — see the module docstring's evidence that call 2 is
#: already at the stable p50 (250.0ms call 1, 187.5ms calls 2-6). Not larger:
#: a bigger K would only add more independent fixtures (and git spawns) to
#: build per sample without changing which call the delta isolates, because
#: the shared cold-start prefix cancels regardless of its length.
_WARM_K = 1

_PREAMBLE = """
import sys
sys.path.insert(0, {repo_root!r})
import json
from pathlib import Path

# Both scripts pay these BEFORE the measured region. `_close_finished` imports
# `handoff_reconcile` lazily, inside the call, so a script that skipped it
# would charge the job a first-import cost it does not own -- the same
# correction the close-coverage advisory's own AC5 measurement needed
# (Finding 2 there).
from coordinator_core.ops.handoff_housekeeping import _handler
from coordinator_core.ops import handoff_reconcile  # noqa: F401
"""

#: `common_dirs` is a list of git-common-dir strings, one per `_handler` call,
#: each its OWN independently `_build_corpus`-ed fixture -- see the module
#: docstring's EQUAL WORK note for why a shared/reused fixture is rejected.
#: Results are a JSON array, one entry per call, in call order, so the LAST
#: entry of the HIGH script is always the marginal (warm, second-in-process)
#: call's own verdict.
_CALL_SCRIPT = _PREAMBLE + """
results = []
for common_dir in {common_dirs!r}:
    result = _handler({{"cap": {cap}}}, Path(common_dir))
    results.append({{
        "exit_code": result["exit_code"],
        "archived": len(result["archived"]),
        "skipped": len(result["skipped"]),
        "failed": len(result["failed"]),
        "close_error": result["close_error"],
        "error": result.get("error"),
    }})
print(json.dumps(results))
"""


def _write_script(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _run_call_script(
    tmp_path: Path, label: str, common_dirs: list, cap: int = _CAP
) -> dict:
    script = _write_script(
        tmp_path / f"{label}.py",
        _CALL_SCRIPT.format(
            repo_root=str(_REPO_ROOT), cap=cap, common_dirs=list(common_dirs)
        ),
    )
    out_path = tmp_path / f"{label}.out"
    reading = single_invocation_tree_process_time(
        [sys.executable, str(script)],
        cwd=str(_REPO_ROOT),
        stdout_path=str(out_path),
        stderr_path=str(tmp_path / f"{label}.err"),
    )
    assert reading["rc"] == 0, (
        label, reading, (tmp_path / f"{label}.err").read_text(errors="replace")
    )
    verdicts = json.loads(out_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    return {"reading": reading, "verdicts": verdicts}


def _measure_one_pair(tmp_path: Path, index: int, terminal: int = _STEADY_TERMINAL) -> dict:
    """Build `2 * _WARM_K + 1` independent fixtures, run a LOW script of
    `_WARM_K` calls and a HIGH script of `_WARM_K + 1` calls, and return the
    delta as the marginal warm call's own cost.

    Every fixture is built fresh and used by exactly one call in exactly one
    script -- never shared between LOW and HIGH, and never reused across
    calls within a script -- so neither script's later calls are measuring a
    corpus an earlier call already swept (the EQUAL WORK failure mode the
    module docstring documents).
    """
    low_dirs = [
        str(_build_corpus(tmp_path / f"corpus-{index}-low-{i}", terminal=terminal))
        for i in range(_WARM_K)
    ]
    high_dirs = [
        str(_build_corpus(tmp_path / f"corpus-{index}-high-{i}", terminal=terminal))
        for i in range(_WARM_K + 1)
    ]

    low = _run_call_script(tmp_path, f"low-{index}", low_dirs)
    high = _run_call_script(tmp_path, f"high-{index}", high_dirs)

    return {
        "process_time_ms": high["reading"]["process_time_ms"] - low["reading"]["process_time_ms"],
        "procs": high["reading"]["procs"] - low["reading"]["procs"],
        "verdict": high["verdicts"][-1],
        "all_verdicts": low["verdicts"] + high["verdicts"],
        "low": low["reading"],
        "high": high["reading"],
    }


@pytest.fixture(scope="module")
def measurement(tmp_path_factory) -> dict:
    """The `_SAMPLES` paired one-shots, taken ONCE for every assertion below.

    Module-scoped deliberately. Each pair builds a fresh 271-record repository
    and runs two interpreters over it; re-taking that per test would double the
    box time for a second reading nobody asked for, on a machine where ~50
    peers are queued behind this one. The measurement is a fact about a commit,
    not about a test.
    """
    if not (IS_WINDOWS or IS_DARWIN):
        pytest.skip("single_invocation_tree_process_time has no primitive here")

    tmp_path = tmp_path_factory.mktemp("housekeeping-budget")
    samples = [_measure_one_pair(tmp_path, i) for i in range(_SAMPLES)]

    process_times = sorted(s["process_time_ms"] for s in samples)
    proc_deltas = sorted(s["procs"] for s in samples)

    return {
        "samples": samples,
        "process_times": process_times,
        "proc_deltas": proc_deltas,
        "p50_ms": statistics.median(process_times),
        "p50_procs": statistics.median(proc_deltas),
    }


def test_every_sample_measured_a_job_that_actually_ran(measurement: dict) -> None:
    """Fixture sanity, asserted before either budget reads the numbers.

    A reading taken over a corpus the job declined to touch is a measurement of
    nothing, and it would pass both budgets comfortably — the failure mode that
    makes a green perf gate worse than none.
    """
    for i, sample in enumerate(measurement["samples"]):
        # Every call in BOTH scripts (LOW and HIGH), not only the marginal
        # one the bar reads: the EQUAL WORK failure mode this module's
        # docstring documents corrupted an EARLIER call in the sequence, not
        # the last one, so a check scoped to `verdict` alone would have
        # missed it.
        for j, verdict in enumerate(sample["all_verdicts"]):
            assert verdict["exit_code"] == 0, (i, j, verdict)
            assert verdict["error"] is None, (i, j, verdict)
            assert verdict["close_error"] is None, (
                f"sample {i} call {j}: the close pass failed, so this reading "
                f"measures a two-leg job and is not the figure the bar is "
                f"about: {verdict}"
            )
            assert verdict["archived"] == _STEADY_TERMINAL, (
                f"fixture sanity, sample {i} call {j}: expected the "
                f"{_STEADY_TERMINAL} terminal records of {_CORPUS_SIZE} to be "
                f"filed and the rest walked past; got {verdict['archived']} "
                f"archived, {verdict['skipped']} skipped, {verdict['failed']} "
                f"failed. Every call must do IDENTICAL work -- a call doing "
                f"less than another is the fixture-decay failure mode the "
                f"module docstring's EQUAL WORK note documents, not warmth."
            )


def test_the_whole_housekeeping_job_fits_the_200ms_brightline(
    measurement: dict,
) -> None:
    """The gate, and the plan's scope verdict in one number.

    Measures the marginal WARM call (HIGH's `_WARM_K + 1`'th call minus
    LOW's `_WARM_K` calls) -- the occasion the job actually runs on, per the
    plan's Anti-scope: "The job must not be reachable by a cold CLI spawn."
    Fails honestly when the job is over: 200ms is the bar DR-344 ratified and
    nothing in this file may move it to recover the second ceremony tier.
    """
    process_times = measurement["process_times"]
    p50_ms = measurement["p50_ms"]

    print(
        f"handoff.housekeeping over {_CORPUS_SIZE} records, n={_SAMPLES} paired "
        f"one-shots (job-object whole-tree, marginal WARM call: HIGH's "
        f"{_WARM_K + 1}-call script minus LOW's {_WARM_K}-call script): "
        f"p50={p50_ms:.1f}ms process, "
        f"samples={[round(x, 1) for x in process_times]}, "
        f"proc delta p50={measurement['p50_procs']}, "
        f"samples={measurement['proc_deltas']}"
    )

    assert p50_ms < _BRIGHTLINE_MS, (
        f"handoff.housekeeping costs {p50_ms:.1f}ms of process time (p50, n="
        f"{_SAMPLES}) over a {_CORPUS_SIZE}-record corpus, past the "
        f"{_BRIGHTLINE_MS}ms brightline. Per the plan's occasions ruling this "
        f"is a SCOPE verdict, not only a red test: over the bar, housekeeping "
        f"ships on the four unconditional day/week ceremonies only, and the "
        f"second tier (/pickup lazily, /workstream-start, /quick-wrap, "
        f"/workstream-complete) is not earned. Cut the cost or take the "
        f"narrower tier — do not widen this literal. samples={process_times}"
    )


def test_the_job_stays_under_the_spawn_count_the_bar_can_afford(
    measurement: dict,
) -> None:
    """Spawn count, gated on a ceiling DERIVED from the time bar.

    A separate test rather than a second assert, because the two fail for
    different reasons and want opposite remedies: a time breach says the work
    is too expensive, a spawn breach says the work is being done by shelling
    out. A reader hitting a combined message would have to guess which half
    applies. It reads the SAME measurement, so the split costs no box time.
    """
    proc_deltas = measurement["proc_deltas"]
    p50_procs = measurement["p50_procs"]

    assert p50_procs <= _MAX_PROC_DELTA, (
        f"handoff.housekeeping adds {p50_procs} processes to the tree (p50, n="
        f"{_SAMPLES}), i.e. ~{p50_procs / _PROCS_PER_GIT_SPAWN:.1f} git spawns, "
        f"past the {_MAX_GIT_SPAWNS} the {_BRIGHTLINE_MS}ms bar can afford at "
        f"{_GIT_SPAWN_COST_MS}ms of process creation apiece. Process creation "
        f"IS the cost; batch the git work rather than raising this ceiling. "
        f"samples={proc_deltas}"
    )


# ---------------------------------------------------------------------------
# The file-read count. Untimed and in-process: this is a COUNT, and putting it
# in the measured window would be paying for the instrument.
# ---------------------------------------------------------------------------


def _classify(target: object, root: Path) -> str:
    """'live', 'archived' or '' for a path argument to `open`."""
    try:
        text = os.fspath(target)
    except TypeError:
        return ""
    if not isinstance(text, str):
        try:
            text = text.decode("utf-8", "replace")
        except AttributeError:
            return ""
    try:
        resolved = Path(text).resolve()
    except OSError:
        return ""
    live = (root / "state" / "handoffs").resolve()
    archived = (root / "archive").resolve()
    if live in resolved.parents:
        return "live"
    if archived in resolved.parents:
        return "archived"
    return ""


@pytest.fixture(scope="module")
def read_census(tmp_path_factory) -> dict:
    """One fixture, one run of the job, with every corpus read counted.

    Counted at `io.open`/`builtins.open` — the same function object under two
    names, patched at both because `Path.open` reaches it through the `io`
    namespace and plain `open(...)` through `builtins`, so neither name alone
    sees every read while patching both double-counts nothing.

    Write modes are excluded: the mover opens its destinations, and counting
    those would report the job's own output as if it were a walk.
    """
    root = tmp_path_factory.mktemp("housekeeping-reads") / "corpus"
    common_dir = _build_corpus(root, terminal=_STEADY_TERMINAL)

    counts = {"live": 0, "archived": 0}
    real_open = io.open

    def _counting_open(file, mode="r", *args, **kwargs):
        if "w" not in mode and "a" not in mode and "x" not in mode:
            bucket = _classify(file, root)
            if bucket:
                counts[bucket] += 1
        return real_open(file, mode, *args, **kwargs)

    from coordinator_core.ops.handoff_housekeeping import _handler
    from coordinator_core.ops import handoff_reconcile  # noqa: F401

    io.open = _counting_open
    builtins.open = _counting_open
    try:
        result = _handler({"cap": _CAP}, common_dir)
    finally:
        io.open = real_open
        builtins.open = real_open

    return {"counts": counts, "result": result}


def test_the_read_census_ran_the_whole_job(read_census: dict) -> None:
    """Fixture sanity for the count, asserted before either bound reads it.

    A job that refused at its first check reads nothing and satisfies every
    ceiling below.
    """
    result = read_census["result"]
    assert result["exit_code"] == 0, result
    assert result["close_error"] is None, result
    assert len(result["archived"]) == _STEADY_TERMINAL, result


def test_the_job_never_walks_the_archived_corpus(read_census: dict) -> None:
    """The plan's § Problem, as an executable assertion.

    All three killed ops died for the cost of a walk they CALLED rather than
    work they did: `handoff_reconcile` computed the live+archived gate index
    unconditionally and consumed it only for records in
    `deployment_state: awaiting_gate` — 16 of 253 on the real corpus, and 0 of
    271 here. The walk is now lazy, and "lazy" is a claim about a code path
    that no timing figure can distinguish from "fast today, on a small archive".
    834 archived records at `4803b5ba5` are 3x the live corpus and grow
    monotonically; a re-eagered index would cost more every week and never fail
    a bar until it suddenly did.
    """
    counts = read_census["counts"]
    moved = len(read_census["result"]["archived"])

    # The predicate is SCALES-WITH-MOVES, not equals-zero. The mover writes each
    # archived record to `archive/handoffs/YYYY-MM/` and reads its destination
    # back; those reads are the job's own output and counting them as a walk is
    # what made the first version of this assertion fire at 271 with 271 moves.
    # Measured both ways at `18bc82baa`: 271 archived reads with 271 moves, 8
    # with 8, and exactly 1 on the real tree with none. A walk would be flat in
    # the move count and linear in the ARCHIVE, which is 834 records and growing.
    assert counts["archived"] <= moved, (
        f"the job read {counts['archived']} file(s) out of the archived corpus "
        f"while moving {moved}. Reads at or below the move count are the mover "
        f"reading back what it just wrote; anything above is a WALK. No record "
        f"here is in `deployment_state: awaiting_gate`, so the live+archived gate "
        f"index must never be built — that unconditional walk is what killed all "
        f"three predecessor ops. See handoff_reconcile._load_gate_index."
    )


def test_the_live_corpus_is_read_a_bounded_number_of_times(
    read_census: dict,
) -> None:
    """Reads per record, not reads total — the axis that survives corpus growth.

    The job reads each live record at most three times by design: the close
    pass, `plan_sweep`'s classification (which must re-read, because step 1
    mutates the states step 2 selects on), and the mover. A fourth pass is a
    corpus walk somebody added, and at 271 records it is invisible in a timing
    figure that carries +/-15.6ms of tick noise. The constant slack covers
    policy, frontmatter and schema files, which do not scale with the corpus.
    """
    counts = read_census["counts"]
    # Three WALK passes over the whole corpus, plus the mover's own per-item
    # reads, which belong to the items being moved and not to the walk: on the
    # real tree at `18bc82baa` a move-nothing run reads 1.13x per record and the
    # all-terminal fixture read 5.00x, the whole difference being the 271 moves.
    # Charging those to the walk is what made the first version of this ceiling
    # report the mover as a fourth pass.
    _READS_PER_MOVE = 6
    ceiling = 3 * _CORPUS_SIZE + _READS_PER_MOVE * _STEADY_TERMINAL + 64
    per_record = counts["live"] / _CORPUS_SIZE

    print(
        f"live-corpus reads: {counts['live']} over {_CORPUS_SIZE} records "
        f"({per_record:.2f} per record)"
    )

    assert counts["live"] <= ceiling, (
        f"the job read the live corpus {counts['live']} times over "
        f"{_CORPUS_SIZE} records ({per_record:.2f} per record), past the "
        f"ceiling of {ceiling} (three corpus passes, plus {_READS_PER_MOVE} per "
        f"move for {_STEADY_TERMINAL} moves, plus 64 constant). Three passes are "
        f"the design (close, re-scan, move); a fourth is a corpus walk that will "
        f"not show up in a timing figure until the corpus is much larger than it "
        f"is today. If the breach scales with MOVES rather than with records, it "
        f"is the mover and belongs to the per-move budget instead."
    )
