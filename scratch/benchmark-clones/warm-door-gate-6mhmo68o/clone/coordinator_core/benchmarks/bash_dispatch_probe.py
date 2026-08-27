"""
coordinator_core.benchmarks.bash_dispatch_probe -- C1's instrumentation
primitives for pln-guards-under-the-brightline: the derived process-time
floor, the full spawn-set enumeration, and the three pre-change baselines
C2/C3 exit-gate against.

Spec backlink: docs/plans/2026-08-21-guards-under-the-brightline.md, chunk
C1 (AC1, AC2, AC6, AC9, AC10, AC11).

WHY THIS FILE AND NOT A REUSED ONE. `benchmarks/gate.py`/`budget.py` gate a
DIFFERENT contract (`{target_ms, tolerance}` wall-clock SLA per REGISTERED
OP, qsub-01) -- this plan's Anti-scope forbids lifting a Finding-4 wall-clock
figure into an AC at all, and DR-344's brightline is itself a PROCESS-TIME
bound, never wall clock (CLAUDE.md § "The brightline"). `harness.py` is that
same wall-clock convention's runner. Only `process_time.py`'s
`batched_process_time_ms` (the Windows job-object primitive that already
avoids the three traps its own module docstring names) is the right
instrument here, and it carries no per-entrypoint gating shape of its own --
`test_warm_door_process_time_gate.py` is the one existing precedent for
wrapping it into a standing gate, and this module follows that precedent's
shape (named ceiling constant + derivation comment) for a different
entrypoint (`bash_guards.dispatch::evaluate_payload_json`, not the warm
door). AC6's spawn-enumeration and AC9-AC11's baseline-capture have NO
existing home at all in `benchmarks/` -- nothing there patches `subprocess`
or reads `guard_roster()`/`guard_message_corpus.py` -- so those functions
are new by construction, not a duplicate of anything reusable.

Negative spec: this module does not assert a spawns-per-call ceiling
anywhere. Research Finding 7's fractional average (6.8, 3.0 processes/call)
implies a conditional or retrying site the named-spawn trace does not
account for; until `enumerate_spawn_set` below has closed that gap over the
full command corpus, no ceiling here would be safe to commit to (see this
chunk's own dispatch brief). Hosting a real spawn-count ceiling is C2/C3's
job, in the existing `test_spawn_count_budget_ratchet.py` ratchet, once
each chunk's own fix has landed.

PLATFORM (C9, docs/plans/2026-08-22-the-brightlines-instrument-exists-on-
the-fleet-floor.md, AC20/AC24): `measure_derived_floor` and every
`batched_process_time_ms`/`_dispatch_cmd` call site here ride
`process_time.py`'s own Windows/Darwin dispatch unconditionally -- neither
raises nor skips on Darwin, and Windows behaviour is untouched. DR-344's
500ms brightline itself carries no platform scoping (checked against the
ruling text directly, `process_time.py` module docstring), so
`DISPATCH_PROCESS_TIME_CEILING_MS` in the paired test file is the SAME
500ms constant on both platforms -- what is per-platform is the MEASURED
evidence that the claude-klabauter-owned dispatch entrypoint clears it, not the bar
itself. Measured this session (2026-08-22, this box, Darwin,
`batched_process_time_quantiles(k=20, n=15)` per corpus row): `bash_echo_
hello` p50=60.1ms/p90=62.1ms, `bash_cat_pyproject_head` p50=64.5ms/
p90=71.2ms, `powershell_get_childitem` p50=61.9ms/p90=65.9ms -- every row
comfortably under the 500ms brightline, see the paired test file's own
AC1 section for the pinned evidence.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from coordinator_core.benchmarks.process_time import (
    IS_DARWIN,
    IS_WINDOWS,
    batched_process_time_ms,
)

_ENV_PAYLOAD_KEY = "BASH_DISPATCH_PROBE_PAYLOAD"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# The command corpus AC3-AC5 name, shared by every measurement below so a
# reader can see the same three shapes threaded through the floor, the
# spawn enumeration, and the executed-set baseline.
# ---------------------------------------------------------------------------


def _payload(tool_name: str, command: str, session_id: str) -> Dict[str, Any]:
    return {
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "session_id": session_id,
        "cwd": _REPO_ROOT,
    }


CORPUS_PAYLOADS: Dict[str, Dict[str, Any]] = {
    "bash_echo_hello": _payload("Bash", "echo hello", "bash-dispatch-probe-echo-hello"),
    "bash_cat_pyproject_head": _payload(
        "Bash", "cat pyproject.toml | head -5", "bash-dispatch-probe-cat-head"
    ),
    "powershell_get_childitem": _payload(
        "PowerShell", "Get-ChildItem", "bash-dispatch-probe-get-childitem"
    ),
}
"""AC3/AC4/AC5's three command shapes, one entry per criterion, keyed by a
stable label every function below reuses -- so a later chunk comparing its
own post-fix measurement against C1's baseline never has to guess which
corpus row corresponds to which AC."""


#: A payload whose `tool_name` is not in `_tool_names.COMMAND_TOOL_NAMES`,
#: so `evaluate_payload_json`'s own C1 master gate (dispatch.py's own
#: "union check against the DECLARED-matchers set") returns before the
#: guard chain is even built -- the "chain that spawns nothing" floor leg
#: AC2 asks for: dispatcher call overhead over the bare interpreter +
#: import closure, with the guard chain itself never entered.
_INERT_PAYLOAD: Dict[str, Any] = _payload("Write", "n/a", "bash-dispatch-probe-inert")


def _dispatch_cmd(payload: Dict[str, Any]) -> Tuple[list, dict]:
    """The `[argv], env` pair for one batched-process-time sample against
    `bash_guards.dispatch::evaluate_payload_json` -- see this module's own
    docstring for why the payload rides an env var rather than stdin:
    `batched_process_time_ms` has no per-invocation stdin hook (it spawns
    every one of its K children identically inside one job object), so the
    payload must be reachable some OTHER way every one of the K children can
    read identically. `preuse-bash-dispatch.py` itself feeds
    `evaluate_payload_json` via stdin in production -- this env-var
    substitution changes only HOW the same payload string reaches the same
    function, never the function called or the payload shape (per this
    chunk's dispatch brief: "Gate instead on
    `bash_guards.dispatch::evaluate_payload_json` with the same payload
    shape").
    """
    script = (
        "import os\n"
        "from coordinator_core.bash_guards.dispatch import evaluate_payload_json\n"
        "evaluate_payload_json(os.environ[%r])\n" % _ENV_PAYLOAD_KEY
    )
    env = dict(os.environ)
    env[_ENV_PAYLOAD_KEY] = json.dumps(payload)
    return [sys.executable, "-c", script], env


def _verify_single_invocation_succeeds(argv: list, env: dict) -> None:
    """`batched_process_time_ms` reports only the LAST invocation's `rc`
    (module docstring, trap 2) -- a fast-erroring script would otherwise
    read as a PASS for every sample but the final one. This runs ONE
    untimed, unbatched invocation with `check=True` first, so a script that
    cannot even complete once fails loudly here rather than hiding inside a
    batched average."""
    subprocess.run(
        argv,
        env=env,
        cwd=_REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


# ---------------------------------------------------------------------------
# AC2 -- the derived floor.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FloorMeasurement:
    """The three components of AC2's derived floor, each a
    `batched_process_time_ms` result dict (`process_time_ms`/`wall_ms`/
    `procs_per_call`/`rc`/`k`). Every threshold AC3-AC5's owning chunks
    write is this floor's `chain_spawns_nothing["process_time_ms"]` plus a
    STATED margin -- see each threshold's own reasoning at its point of use,
    never a round number picked for comfort (this chunk's own dispatch
    brief)."""

    bare_interpreter: Dict[str, Any]
    import_closure: Dict[str, Any]
    chain_spawns_nothing: Dict[str, Any]


def measure_derived_floor(k: int = 20) -> FloorMeasurement:
    """Measures, in process time (never wall clock), the three floor
    components AC2 names:

      1. `bare_interpreter` -- `python -c "pass"`: pure interpreter start
         cost, nothing this package contributes at all.
      2. `import_closure` -- `python -c "import
         coordinator_core.bash_guards.dispatch"`: (1) plus the unavoidable
         module-import cost of the dispatcher itself (Finding 3's
         call-time-dominates finding means this is NOT the site to chase,
         but it is still part of the floor a later threshold is measured
         against).
      3. `chain_spawns_nothing` -- `evaluate_payload_json` invoked against
         `_INERT_PAYLOAD` (a `tool_name` outside `COMMAND_TOOL_NAMES`, so the
         dispatcher's own C1 master gate returns before the guard chain is
         even built): (2) plus the marginal cost of reaching and returning
         from the dispatcher call itself, with the guard chain never
         entered -- the floor no guard-side fix below this chunk can ever
         reduce.

    Batched at `k` (default 20, matching this repo's own convention) inside
    ONE job object per leg on Windows (or, on Darwin, `k` root-pid-scoped
    invocations amortised the same way -- `process_time.py`'s own
    Windows/Darwin dispatch) to beat the ~15.6ms scheduler-tick
    quantisation a single Windows sample cannot resolve (`process_time.py`
    module docstring, trap 2). Raises `NotImplementedError` off both
    Windows and Darwin -- callers must check `IS_WINDOWS`/`IS_DARWIN` first,
    exactly like `process_time.py` itself.
    """
    if not (IS_WINDOWS or IS_DARWIN):
        raise NotImplementedError(
            "measure_derived_floor has no process-time primitive for this platform "
            "-- see coordinator_core.benchmarks.process_time's own Windows/Darwin split"
        )

    bare = batched_process_time_ms([sys.executable, "-c", "pass"], k=k, cwd=_REPO_ROOT)

    import_argv = [
        sys.executable,
        "-c",
        "import coordinator_core.bash_guards.dispatch",
    ]
    import_env = dict(os.environ)
    closure = batched_process_time_ms(import_argv, k=k, env=import_env, cwd=_REPO_ROOT)

    inert_argv, inert_env = _dispatch_cmd(_INERT_PAYLOAD)
    _verify_single_invocation_succeeds(inert_argv, inert_env)
    chain_nothing = batched_process_time_ms(inert_argv, k=k, env=inert_env, cwd=_REPO_ROOT)

    return FloorMeasurement(
        bare_interpreter=bare, import_closure=closure, chain_spawns_nothing=chain_nothing
    )


# ---------------------------------------------------------------------------
# AC6 -- full spawn-set enumeration.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpawnRecord:
    """One `subprocess.Popen` construction observed while
    `evaluate_payload_json` ran, in-process, against one corpus payload.
    `argv` is the exact command list/string passed to `Popen`; `stack` is a
    compact `file:line:function` trail (innermost call last) -- Finding 7's
    unattributed processes are exactly the spawns a NAMED-spawn trace
    (grepping for known call sites) cannot see; walking the real call stack
    at spawn time is the only way to attribute a conditional or retrying
    site regardless of whether this module's author already knew it was
    there."""

    argv: Tuple[str, ...]
    stack: Tuple[str, ...]


def _stack_summary(depth: int = 10) -> Tuple[str, ...]:
    frames = traceback.extract_stack()[:-2]  # drop this frame and the patched __init__ frame
    tail = frames[-depth:]
    return tuple(
        "%s:%d:%s" % (os.path.basename(f.filename), f.lineno, f.name) for f in tail
    )


class _SpawnRecorder:
    """Context manager patching `subprocess.Popen.__init__` to log every
    construction with its call stack, then restoring the original
    unconditionally. Patches `Popen.__init__` specifically (not
    `subprocess.run`/`subprocess.call`) because every subprocess-spawning
    path in the standard library funnels through `Popen` construction --
    patching there catches `run`, `call`, `check_output`, and a bare
    `Popen(...)` call alike, in one seam.
    """

    def __init__(self) -> None:
        self.records: List[SpawnRecord] = []
        self._orig_init: Optional[Callable] = None

    def __enter__(self) -> "_SpawnRecorder":
        self._orig_init = subprocess.Popen.__init__
        orig_init = self._orig_init
        records = self.records

        def _patched_init(popen_self, args, *a, **kw):  # noqa: ANN001
            if isinstance(args, (list, tuple)):
                argv = tuple(str(x) for x in args)
            else:
                argv = (str(args),)
            records.append(SpawnRecord(argv=argv, stack=_stack_summary()))
            return orig_init(popen_self, args, *a, **kw)

        subprocess.Popen.__init__ = _patched_init  # type: ignore[assignment]
        return self

    def __exit__(self, *exc_info) -> None:
        subprocess.Popen.__init__ = self._orig_init  # type: ignore[assignment]


def enumerate_spawn_set(payload: Dict[str, Any]) -> List[SpawnRecord]:
    """Runs `evaluate_payload_json` in-process (never as a subprocess
    itself -- the patch is on THIS interpreter's `subprocess.Popen`) against
    `payload`, and returns every `SpawnRecord` observed.

    Deliberately IN-PROCESS, not the batched out-of-process shape
    `measure_derived_floor` uses: a spawn patched inside a spawned child
    would be invisible to this recorder (the patch lives in THIS process's
    `subprocess` module, not the child's), so this function calls the
    dispatcher directly rather than through `_dispatch_cmd`.
    """
    from coordinator_core.bash_guards import dispatch

    raw = json.dumps(payload)
    with _SpawnRecorder() as recorder:
        dispatch.evaluate_payload_json(raw)
    return recorder.records


def enumerate_spawn_set_for_corpus() -> Dict[str, List[SpawnRecord]]:
    """`enumerate_spawn_set` over every `CORPUS_PAYLOADS` entry -- AC6's
    "full spawn set", keyed the same way `CORPUS_PAYLOADS` itself is."""
    return {label: enumerate_spawn_set(payload) for label, payload in CORPUS_PAYLOADS.items()}


# ---------------------------------------------------------------------------
# AC9 -- message-bytes baseline.
# ---------------------------------------------------------------------------


def _resolved_python3_invocation_prefix() -> str:
    from coordinator_core.bash_guards.dispatch_checks import _bt_python3_invocation

    return _bt_python3_invocation()


def capture_message_baseline() -> Dict[str, int]:
    """Message bytes per `(guard, row_id)` cell over
    `guard_message_corpus.py`'s own `CONFINEMENT_ROWS` +
    `ADVISORY_REWRITE_ROWS` + `PLATFORM_CONDITIONED_ROWS` -- reusing that
    module's row definitions verbatim (per this chunk's dispatch brief:
    "reuse the corpus's row definitions as the input set rather than
    inventing a second one"), fired through its own `fire_row` seam
    (`capture_one_guard`, C1 of the message-size plan -- unconditional,
    no dispatch-loop short-circuit).

    Keyed `"<guard>::<row_id>"` -> byte count, with `_bt_python3_
    invocation()`'s resolved, machine-specific interpreter prefix
    substituted out for a fixed placeholder BEFORE measuring bytes (AC9's
    machine-pinning problem: a stored golden byte count that embedded the
    real resolved path would drift on every box `_bt_python3_invocation()`
    resolves a different absolute path on).
    """
    from coordinator_core.bash_guards._message_size import extract_prose_text
    from coordinator_core.bash_guards.tests import guard_message_corpus as corpus

    prefix = _resolved_python3_invocation_prefix()
    placeholder = "<PYTHON3_INVOCATION>"

    rows = list(corpus.CONFINEMENT_ROWS) + list(corpus.ADVISORY_REWRITE_ROWS) + list(
        corpus.PLATFORM_CONDITIONED_ROWS
    )
    baseline: Dict[str, int] = {}
    for row in rows:
        capture = corpus.fire_row(row)
        text = extract_prose_text(capture.envelope)
        if prefix:
            text = text.replace(prefix, placeholder)
        key = "%s::%s" % (row.guard, row.row_id)
        baseline[key] = len(text.encode("utf-8"))
    return baseline


# ---------------------------------------------------------------------------
# AC10 -- roster baseline.
# ---------------------------------------------------------------------------


def capture_roster_baseline() -> Tuple[Dict[str, Any], ...]:
    """The full `roster.guard_roster()` snapshot -- every `GuardRosterEntry`
    field (`id`, `matchers`, `band`, `fail_closed`, AND `script`, per AC10's
    explicit note that a narrower id-and-matchers-only check would miss a
    `script` regression), as plain, JSON-serialisable dicts sorted by `id`
    for deterministic ordering."""
    from coordinator_core.bash_guards.roster import guard_roster

    entries = sorted(guard_roster(), key=lambda e: e.id)
    return tuple(
        {
            "id": e.id,
            "matchers": tuple(e.matchers),
            "band": e.band,
            "fail_closed": e.fail_closed,
            "script": e.script,
        }
        for e in entries
    )


# ---------------------------------------------------------------------------
# AC11 -- executed-set-and-verdicts baseline.
# ---------------------------------------------------------------------------


def _summarize_verdict(envelope: Optional[Dict[str, Any]]) -> str:
    """A compact, comparable verdict label for one guard's raw return --
    `None` (silent allow), `"deny"`/`"allow"`/`"ask"` (a real
    `hookSpecificOutput.permissionDecision`), or `"advisory"` (a non-`None`
    envelope carrying prose but no `permissionDecision`, e.g. an
    `additionalContext`-only rewrite)."""
    if envelope is None:
        return "none"
    hso = envelope.get("hookSpecificOutput") if isinstance(envelope, dict) else None
    if isinstance(hso, dict) and hso.get("permissionDecision"):
        return str(hso["permissionDecision"])
    return "advisory"


def capture_executed_set_baseline() -> Dict[str, Dict[str, Any]]:
    """The executed guard set and its verdicts per `CORPUS_PAYLOADS` label,
    captured through the REAL `evaluate_payload_json` (short-circuit,
    disarm suppression, sentinel-unlock annotation, matcher filtering --
    every bit of the real chain, unlike `guard_message_capture.py`'s
    unconditional-fire seam), by temporarily wrapping each `GuardEntry.fn`
    a fresh `_build_guard_chain()` call returns so a call is OBSERVED, never
    by reimplementing any slice of `evaluate_payload_json`'s own loop.

    AC11 exists precisely to catch a guard silently changing WHAT it sees
    without changing whether the suite it ships with fails (this plan's own
    C3 body: "a guard that silently starts seeing `None` where it saw a
    root would pass every existing unit test") -- observing the real
    dispatcher's own execution, not a hand-rolled approximation of it, is
    the only way this baseline can mean anything for that purpose.

    Returns `{label: {"executed": [name, ...], "verdicts": {name: verdict}}}`.
    """
    from coordinator_core.bash_guards import dispatch

    results: Dict[str, Dict[str, Any]] = {}
    for label, payload in CORPUS_PAYLOADS.items():
        raw = json.dumps(payload)
        trace: List[Tuple[str, str]] = []
        orig_build = dispatch._build_guard_chain

        def _wrapped_build(*args, **kwargs):
            chain = orig_build(*args, **kwargs)
            wrapped = []
            for entry in chain:
                def _make_fn(_entry=entry):
                    def _fn():
                        out = _entry.fn()
                        trace.append((_entry.name, _summarize_verdict(out)))
                        return out

                    return _fn

                wrapped.append(dataclasses.replace(entry, fn=_make_fn()))
            return wrapped

        dispatch._build_guard_chain = _wrapped_build  # type: ignore[assignment]
        try:
            dispatch.evaluate_payload_json(raw, collect_advisories=True)
        finally:
            dispatch._build_guard_chain = orig_build  # type: ignore[assignment]

        results[label] = {
            "executed": [name for name, _ in trace],
            "verdicts": {name: verdict for name, verdict in trace},
        }
    return results
