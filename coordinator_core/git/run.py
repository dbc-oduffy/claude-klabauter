"""coordinator_core.git.run -- the one shared git runner, and the only two
bounds a git spawn in this tree is allowed to carry.

Purpose: the census behind
`docs/problems/2026-08-21-the-over-budget-timeout-hitlist.md` § G7 found
**60+ non-test modules each defining a private `_run_git`**, and ~90
module-private git-timeout constants spread across the values 2.0, 5, 10,
15, 20, 30, 60, 120, 180, 300, 600, 900, 1800 and 3600 -- the same
operation carrying a different number in every module. `git -C <repo>
rev-parse HEAD` measures 26.9 ms on this box (DR-344 § 4), so none of those
numbers was measured; 30 became the folklore default and was copied.

Six modules already got it right at 2.0 for the identical operation
(`git.repo_root`, `git.remote_url`, `git.divergence`,
`bash_guards._branch_set`, `bash_guards.dispatch_checks`,
`bash_guards.commit_tripwires`), which proves the number is achievable and
proves that nothing forces agreement. This module is the thing to reach
for, so that the 61st module needing a git read does not invent a 61st
number; `coordinator_core/tests/test_shared_git_runner.py` is the gate that
objects when it tries.

TWO BUDGETS AND ONE HEADROOM TERM, ALL NAMED, NONE PER-SITE.

  - `LOCAL_PLUMBING_BUDGET_SECS = 2.0` -- every local git read and write.
    Matches what `git/repo_root.py` already ships and what
    `ipc.CEREMONY_BUDGET_SECS` clamps every `ceremony.*` op to (DR-348), so
    a plumbing call on a ceremony path cannot out-live the op that issued
    it. A local git call that does not fit inside it is a defect report, not
    a candidate for a wider dial.
  - `REMOTE_BUDGET_SECS` -- the single constant for the handful of legs
    that genuinely talk to a remote (`fetch`, `push`, `ls-remote`, `clone`).
    This is a RUNAWAY GUARD, not a budget, and the distinction is
    load-bearing: DR-349 grants network legs no standing carve-out, so a
    remote leg sitting on a budgeted path is a defect *regardless* of what
    this constant says. The constant bounds the ones that are legitimately
    off the hot path; it does not license putting one on it.
  - `_SPAWN_SCHEDULING_HEADROOM_SECS` -- NOT a budget, and the reason the
    two above can be honest ones. A budget is PROCESS time;
    `subprocess.run(timeout=)` enforces WALL CLOCK, and on a box carrying
    50-70 concurrent sessions those differ by two orders of magnitude
    purely from waiting to be scheduled. `_wall_bound` is the only place in
    the tree that converts between the axes, and it does so by ADDING this
    term. Read its docstring and the constant's before touching either
    number: this module shipped the conflated version first and it handed
    every migrating call site a >5% false-timeout rate.

DERIVING FROM A CALLER'S BUDGET. A call site on a budgeted path passes its
caller's end-to-end budget through rather than naming a number:
`run_git(args, timeout=CEREMONY_BUDGET_SECS)`, exactly the shape
`ops/ceremony/branch_resolution.py`, `ops/ceremony/resolver.py` and
`ops/ceremony/update_docs_scan.py` already use. `timeout=` is NARROW-ONLY
here -- it is folded through `min()` against the ceiling for its lane (see
`_resolve_budget`), so a caller can always ask for less time and can
never buy more. That is DR-349 § 3's shape, and it is why this parameter is
not a dial: there is no value a call site can pass that widens anything.

IMPORT COST. `coordinator_core/git/` sits on `coordinator_core.ipc`'s
cold-start path, which is measured against a module-count ceiling in
`coordinator_core/benchmarks/import-budget-manifest.json`. `subprocess`
(and its ~10 transitive modules) is imported INSIDE `run_git`, the same
discipline `git/repo_root.py :: _spawn_rev_parse` documents -- a module
that merely imports this seam does not pay for it. Do not hoist it.

Negative-spec:
    - Does NOT raise on any failure path. A timeout, a missing `git`, or a
      non-zero exit all come back as a `GitResult` the caller branches on,
      matching what every private `_run_git` this replaces already did. A
      caller that must distinguish "clean" from "could not tell" reads
      `timed_out` / `returncode`, the way
      `git.divergence.diverging_paths(fail_loud=True)` does -- this module
      does not decide that policy for it.
    - Does NOT memoize. Repo IDENTITY is cacheable and
      `git.repo_root` already caches it; repo STATE (HEAD, status, refs) is
      not, because peer sessions commit into this shared worktree mid-run.
      A cache here would serve stale state to every caller at once. See
      `git/remote_url.py`'s "No memo" section for the same ruling made once
      already.
    - Does NOT accept `shell=True`, and takes `args` WITHOUT the leading
      `"git"`. Both are deliberate: the gate that enforces this seam keys on
      a module building its own `["git", ...]` argv, and a shell string
      would route around both the gate and the argv-quoting correctness the
      list form gives for free on Windows.
    - Does NOT accept `str` for `input`. Bytes-only, so that a `--stdin`
      caller cannot be newline-translated behind its back on Windows; see
      `run_git`'s own `input` entry for the incident. Enforced with an
      immediate `TypeError`, not the `GitResult` failure path above -- a
      wrong-typed argument is a caller bug, not something git did.
    - Does NOT implement DR-349 § 4's deadline threading (an op stamping a
      deadline at entry so stacked spawns cannot buy time by stacking).
      That is a real requirement and it is NOT built here: no caller stamps
      a deadline today, and inventing the parameter ahead of one would rest
      the design on a caller that does not exist. When the deadline exists,
      it belongs in `_resolve_budget` as one more `min()` term.
    - Does NOT replace `git.repo_root`'s parent-walk. Repo root, git dir and
      common dir are answered there WITHOUT a spawn; routing them through
      this module would trade a filesystem walk for a process.

Spec backlink: docs/problems/2026-08-21-the-over-budget-timeout-hitlist.md § G7
Decision backlink: docs/decisions/DR-349-one-budget-governs-every-constructed-op.md
Decision backlink: docs/decisions/DR-344-the-brightline-process-budget-for-claude-klabauter.md
"""

from __future__ import annotations

from typing import Mapping, NamedTuple, Optional, Sequence

from coordinator_core.telemetry import spawn_counter
from coordinator_core.win_portability import no_console_creationflags

#: Every LOCAL git call -- reads and writes alike. A budget, not a dial:
#: `git rev-parse HEAD` is 26.9 ms of PROCESS time on this box (DR-344 § 4),
#: and the widest local plumbing read in the tree is a pathspec-scoped
#: `git status`, which is proportional to the batch size rather than to repo
#: size. A local call that does not fit here is over budget, and the remedy
#: is the call, never this number. Ratcheted by `test_shared_git_runner.py`,
#: which carries an independent second copy of every value in this block.
LOCAL_PLUMBING_BUDGET_SECS: float = 2.0

#: The ONE constant for genuinely-remote legs. A runaway guard: it exists so
#: a wedged remote cannot hold a worker forever, and it is NOT a licence to
#: put a remote leg on a budgeted path (DR-349 grants network no standing
#: carve-out). Deliberately far below the 120s / 300s / 3600s the existing
#: remote sites carry, so that migrating one of them onto this seam surfaces
#: as a decision rather than passing silently -- per DR-349, every such "no"
#: is a defect report about that leg's placement, not evidence for a wider
#: number here. Ratchets DOWN only.
REMOTE_BUDGET_SECS: float = 30.0

#: THE TERM THAT MAKES THE TWO ABOVE ENFORCEABLE, and the one number here
#: that is not a budget.
#:
#: `subprocess.run(timeout=)` bounds WALL CLOCK. Both budgets above are
#: PROCESS time, which is the only axis `CLAUDE.md` § brightline permits a
#: conclusion to rest on. On this box those two axes differ by two orders of
#: magnitude, and NOT because anything is slow: 50-70 concurrent sessions is
#: the design condition, so a spawn waits to be scheduled before it runs.
#: Measured 2026-08-21 by the G1 session over plain `subprocess.run`, 60
#: samples per leg, process time via
#: `benchmarks/process_time.py :: batched_process_time_ms`:
#:
#:     leg                                  process    p50 wall  p95 wall  max wall
#:     git --version                          33.6ms      1,462     2,891     4,588
#:     git -C <mirror> rev-parse --show-toplevel 33.6ms     847     2,907     4,065
#:     git -C <mirror> status --porcelain    190.6ms        791     2,524     2,841
#:
#: A bare `git --version` therefore breaches a 2.0s WALL bound in more than
#: 5% of spawns. This module shipped exactly that defect: it used the 2.0
#: budget as the wall-clock argument and clamped it with `min()` so no caller
#: could widen it, which handed every migrating call site a >5% false-timeout
#: rate -- and `GitResult.timed_out` feeds callers that pick a commit
#: MECHANISM from it (`git.divergence.DivergenceCheckFailed`), where peer
#: load would have read as "we could not tell". Found empirically by G1, not
#: by inspection: `test_percolate_round_dest_paths_exist ::
#: test_mixed_batch_with_worktree_fast_path_and_git_probe_combined` went red
#: under a concurrent suite and green in isolation.
#:
#: ADDITIVE, not a multiplier: the delay is a roughly fixed per-spawn cost of
#: sharing the box, not a scaling of the work. 10.0 is ~2.2x the worst wall
#: sample observed (4,588ms). Kept as its OWN named term rather than folded
#: into a bigger literal so that the budgets above stay citable, stay
#: ratchetable, and keep biting on a real regression -- a single fat number
#: would hide a leg that got 5x slower inside headroom meant for scheduling.
#: This is the load norm made visible, and it is the one constant here whose
#: growth means the BOX changed rather than our code. Ratchets DOWN only.
_SPAWN_SCHEDULING_HEADROOM_SECS: float = 10.0

class GitResult(NamedTuple):
    """One git invocation's outcome, with the three failure kinds kept
    distinct rather than collapsed onto a single falsy value.

    `returncode` -- git's own exit status; `-1` for a timeout and `127` when
        the process never ran (no `git` on PATH, bad cwd). Both sentinels
        match what the private `_run_git` bodies this seam replaces already
        returned, so a migrating call site's existing `rc != 0` branch keeps
        its meaning.
    `timed_out` -- True ONLY for a genuine `TimeoutExpired`. A caller
        deciding a commit MECHANISM (rather than printing an advisory) needs
        "we could not tell" to be distinguishable from "git said no"; see
        `git.divergence.DivergenceCheckFailed` for the incident that
        distinction closes.
    `stdout` / `stderr` -- decoded text, never `None`; empty strings on a
        timeout or a failed spawn.
    `stdout_bytes` -- git's stdout as the bytes it actually wrote, populated
        ONLY when the call ran in binary mode (`binary=True`, or `input`,
        which implies it). `b""` otherwise, and `b""` on a timeout or a
        failed spawn. It is a SECOND view of the same stream rather than a
        replacement for `stdout`: a `-z` reader needs the undecoded bytes
        (see `run_git`'s `binary` entry), while everything around it -- error
        messages, `git_out`, existing call sites -- keeps reading text, and
        neither should have to know which the other wanted. Last field and
        defaulted, so the four-field keyword constructions in this tree's
        test doubles keep working untouched.
    """

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    stdout_bytes: bytes = b""

    @property
    def ok(self) -> bool:
        """True iff git ran and exited 0."""
        return self.returncode == 0


def _as_bytes(raw) -> bytes:
    """The raw counterpart to `_as_text`, for `GitResult.stdout_bytes`.

    Returns `b""` for a text-mode capture rather than re-encoding the decoded
    string. Re-encoding would look like it worked and would hand back bytes
    that are NOT what git wrote wherever `errors="replace"` had already
    substituted U+FFFD -- a silent lie to exactly the byte-exactness callers
    this field exists for. An empty value they can see is the honest answer;
    a caller that wants the bytes asks for `binary=True` and gets them.
    """
    return raw if isinstance(raw, bytes) else b""


def _resolve_budget(timeout: Optional[float], remote: bool) -> float:
    """The PROCESS-time budget for one call: the lane's ceiling, narrowed by
    an explicit `timeout` and never widened by one.

    This is DR-349 § 3's `min()`-after-resolution shape, the same one
    `ipc._timeout_for` already applies to `ceremony.*`. A caller passing
    `timeout=CEREMONY_BUDGET_SECS` lands on the budget; a caller passing 30
    for a local read lands on 2.0 and gets no say in it. There is no
    parameter, env var, or call-site argument that resolves above the
    ceiling -- that is the whole reason this function exists rather than
    `timeout or DEFAULT`.

    What this value is NOT is the number handed to `subprocess.run` -- see
    `_wall_bound`.
    """
    ceiling = REMOTE_BUDGET_SECS if remote else LOCAL_PLUMBING_BUDGET_SECS
    if timeout is None:
        return ceiling
    return min(float(timeout), ceiling)


def _wall_bound(timeout: Optional[float], remote: bool) -> float:
    """The WALL-CLOCK argument for `subprocess.run(timeout=)`: this call's
    process-time budget plus the shared box's scheduling cost.

    The two axes are not interchangeable and this function is the only place
    the tree converts between them. See `_SPAWN_SCHEDULING_HEADROOM_SECS`
    for the measurements, and for the defect this split repairs.

    Consequence worth stating, because it is the actual fix: a `timed_out`
    result now means the leg exceeded its budget AND every second of
    scheduling headroom -- i.e. genuinely wedged, not merely queued behind
    peers. That is what makes `GitResult.timed_out` safe for
    `git.divergence.DivergenceCheckFailed`'s caller to treat as
    indeterminate rather than as noise.

    Accepted consequence, stated rather than hidden: a caller narrowing to
    `timeout=0.25` still waits up to 10.25s of wall before giving up. There
    is no way to fail faster on the wall axis without failing on peer load,
    which is the measurement CLAUDE.md forbids acting on. A caller that
    genuinely must bound wall clock is asking a scheduling question, not a
    git question, and does not belong on this seam.

    Negative-spec: do NOT "simplify" this by folding the headroom into the
    budget constants. The budgets are cited by DR-344's brightline and
    ratcheted against it; a single fat number would let a leg get five times
    slower inside room that was allocated for the scheduler.
    """
    return _resolve_budget(timeout, remote) + _SPAWN_SCHEDULING_HEADROOM_SECS


def _as_text(raw) -> str:
    """Normalize one captured stream to `str`.

    Binary mode (the `input` path) hands back `bytes`; text mode hands back
    `str`; a timed-out or never-spawned call has neither. Decoding here
    rather than at the two `GitResult` construction sites keeps the
    `errors="replace"` policy identical across both modes -- a caller must
    not have to know which mode its call resolved to in order to know
    whether undecodable output raises. It does not, on either path.
    """
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return raw


def run_git(
    args: Sequence[str],
    *,
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
    remote: bool = False,
    env: Optional[Mapping[str, str]] = None,
    input: Optional[bytes] = None,
    binary: bool = False,
) -> GitResult:
    """Run `git <args>` under this seam's bound and return a `GitResult`.

    `args` carries the subcommand and its arguments WITHOUT a leading
    `"git"` -- `run_git(["rev-parse", "HEAD"])`, not
    `run_git(["git", "rev-parse", "HEAD"])`.

    `cwd` -- the directory to run in. A caller that already holds a repo
    root may prefer `["-C", root, ...]` in `args`; both reach the same repo,
    and `-C` additionally survives a cwd that is deleted underneath a
    long-lived process.

    `timeout` -- a PROCESS-time budget, narrow-only, see `_resolve_budget`.
    It is not the wall-clock bound handed to `subprocess.run`; that is this
    value plus `_SPAWN_SCHEDULING_HEADROOM_SECS`, per `_wall_bound`. Omit it
    for ordinary local plumbing; pass the caller's own end-to-end budget
    (`ipc.CEREMONY_BUDGET_SECS`) on a budgeted path so the subprocess bound
    cannot drift from the op that issued it.

    `remote` -- True only for a leg that actually contacts a remote. It
    raises the ceiling to `REMOTE_BUDGET_SECS`, and it is the one flag in
    this module a reviewer should be suspicious of: a `remote=True` call on
    a session, commit, or ceremony path is the defect DR-349 names, not a
    configuration.

    `input` -- BYTES fed to git's stdin, for the `--stdin` forms
    (`check-ignore -z --stdin`, `cat-file --batch-check`, `update-index
    --index-info`). Bytes-only, and that is the whole point: passing `input`
    switches this call to BINARY mode, because Python's text-mode stdin
    wrapper is created with `newline=None` and therefore translates every
    `\\n` you write into `os.linesep` -- `\\r\\n` on Windows. A caller that
    NUL-delimits with `-z` and then has its delimiters silently rewritten
    gets paths git echoes back C-quoted, matching nothing. That is not
    hypothetical: `coordinator/bin/percolate-round.py` carries the scar, and
    the failure surfaced between a publish round's real run and its commit.
    So there is no `str` overload to reach for -- build the bytes yourself
    (`b"\\0".join(...)`) and what git reads is what you wrote. Passing a
    `str` here raises `TypeError` immediately, before any subprocess is
    spawned -- a caller-contract violation, not a git-side failure, so it is
    outside the negative-spec's "never raises" promise (that promise covers
    what git itself can do: time out, be absent, exit non-zero; it was never
    a promise to swallow a wrong-typed argument silently).

    Consequence of binary mode, stated because it is an asymmetry rather
    than a detail: with `input`, `stdout`/`stderr` are decoded here (utf-8,
    `errors="replace"`) WITHOUT universal-newline translation, so a `\\r\\n`
    in git's output survives as `\\r\\n`. Without `input`, text mode
    translates it to `\\n` as it always has. A `--stdin` caller is asking a
    byte-exactness question on both directions of the pipe; every other
    caller keeps the behaviour it has today.

    `--stdin` is also the standard way to keep a BATCHED git call under the
    Windows argv cap, which is the shape
    `tests/test_no_unbatched_per_item_git_spawn.py` pushes modules toward.
    This parameter is what stops that gate and this one from pulling in
    opposite directions.

    `binary` -- capture stdout/stderr UNDECODED and publish them on
    `GitResult.stdout_bytes`. `input` already implies it; this flag is for
    the readers that need byte-exact OUTPUT without having anything to write,
    which is the whole `-z` family (`ls-files -z`, `status --porcelain -z`,
    `check-attr -z`) plus `cat-file --batch`. The reason it must exist is the
    same one that makes `input` bytes-only, pointed the other way: `stdout`
    decodes with `errors="replace"`, so a path byte git cannot decode comes
    back as U+FFFD. For a caller whose entire predicate is "are these bytes
    what the declaration says they are" (`ops/eol/census.py`), that
    substitution IS the bug it is looking for, and for
    `git/ls_files_bytes.py` it is the reason that module exists at all. Text
    mode would also universal-newline-translate a `
` inside a blob,
    which is precisely the difference `ops/eol/repair.py` reads `cat-file
    --batch` to measure. `stdout` stays populated (decoded, `replace`) in
    binary mode too, so an error path can still print something readable.

    Never raises for a git-side failure -- see the module's negative spec.
    Raises `TypeError` immediately if `input` is passed as `str`: that is a
    caller-contract violation, checked before any subprocess is spawned, not
    a failure path this function is promising to swallow.
    """
    if input is not None and not isinstance(input, bytes):
        raise TypeError(
            f"run_git(input=...) is bytes-only, got {type(input).__name__}; "
            "encode it yourself (e.g. b'\\0'.join(...)) -- see this "
            "function's `input` docstring entry for why there is no str "
            "overload."
        )

    # Function-local: this module sits under `coordinator_core/git/`, on
    # `ipc`'s cold-start path, and `subprocess` drags ~10 transitive modules
    # (select/selectors/signal/threading/locale/math) with it. Importing a
    # RUNNER must not cost a spawn's worth of imports to a module that only
    # imports the constants. Same discipline as `repo_root._spawn_rev_parse`.
    import subprocess

    # Binary whenever stdin is fed, text otherwise -- see `input`'s docstring
    # entry for the newline-translation defect this avoids. `stdin` and
    # `input` are mutually exclusive to `subprocess.run` (it raises
    # ValueError on both), so DEVNULL is supplied only when there is nothing
    # to write; a git command that finds an inherited stdin can block waiting
    # on a prompt, which is what DEVNULL is there to prevent.
    if input is not None:
        mode_kwargs = {"input": input}
    elif binary:
        # Binary with nothing to write: DEVNULL still applies, since the
        # reason it is here (a git command that finds an inherited stdin can
        # block on a prompt) has nothing to do with which mode we capture in.
        mode_kwargs = {"stdin": subprocess.DEVNULL}
    else:
        mode_kwargs = {
            "encoding": "utf-8",
            "errors": "replace",
            "stdin": subprocess.DEVNULL,
        }

    # The brightline's second axis. Counted here rather than at the call sites
    # because this is the chokepoint every git call is already required to pass
    # through (see `coordinator_core.git.git_state`'s docstring). Bumped BEFORE
    # the spawn, not after: process creation is the cost being counted, and a
    # `git` that raises still paid it — counting only on the success path would
    # hide exactly the timeouts worth finding.
    spawn_counter.bump()

    try:
        # NOT hand-rolled over `Popen`, deliberately. `subprocess.run`'s own
        # timeout path kills the child and -- on Windows -- re-drains it with
        # a second `communicate()` before re-raising, and `Popen.__exit__`
        # then closes every pipe. A wrapper that reimplements the timeout
        # around a raw `Popen` is how a fed stdin leaks a pipe on the failure
        # path, so the wrapping stops here.
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            timeout=_wall_bound(timeout, remote),
            env=dict(env) if env is not None else None,
            **mode_kwargs,
            **no_console_creationflags(),
        )
    except subprocess.TimeoutExpired:
        return GitResult(returncode=-1, stdout="", stderr="", timed_out=True)
    except OSError:
        return GitResult(returncode=127, stdout="", stderr="", timed_out=False)
    return GitResult(
        returncode=completed.returncode,
        stdout=_as_text(completed.stdout),
        # `getattr`, not `completed.stderr`: the modules migrating onto this
        # seam bring test doubles that were written against a private
        # `_run_git` reading only `(returncode, stdout)`, so a stand-in
        # result object with no `stderr` is the norm in this tree, not a
        # malformed one. A real `CompletedProcess` always carries the field.
        # Requiring it here would break those doubles for a value their
        # subject never reads, which makes migrating cost more than staying.
        stderr=_as_text(getattr(completed, "stderr", "")),
        timed_out=False,
        stdout_bytes=_as_bytes(completed.stdout),
    )


def git_ok(args: Sequence[str], **kwargs) -> bool:
    """True iff `git <args>` ran and exited 0. The predicate form the
    private runners spelled as `_git(...).returncode == 0` in a dozen
    modules; kwargs are `run_git`'s."""
    return run_git(args, **kwargs).ok


def git_out(args: Sequence[str], **kwargs) -> Optional[str]:
    """Stripped stdout of a SUCCESSFUL `git <args>`, else None.

    `None` means "no answer" (git failed, timed out, or never ran). A
    successful call with empty stdout returns the EMPTY STRING, not `None`
    -- `git rev-parse --show-prefix` at the repo toplevel legitimately emits
    nothing, and collapsing that onto the failure value is the bug
    `git/repo_root.py`'s "Negative caching" section records having shipped
    once already.
    """
    result = run_git(args, **kwargs)
    if not result.ok:
        return None
    return result.stdout.strip()
