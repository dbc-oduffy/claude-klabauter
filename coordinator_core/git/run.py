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

LOCAL_PLUMBING_BUDGET_SECS: float = 2.0

REMOTE_BUDGET_SECS: float = 30.0

#: THE TERM THAT MAKES THE TWO ABOVE ENFORCEABLE, and the one number here
#: MECHANISM from it (`git.divergence.DivergenceCheckFailed`), where peer
#: ADDITIVE, not a multiplier: the delay is a roughly fixed per-spawn cost of
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
        return self.returncode == 0


def _as_bytes(raw) -> bytes:
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
    what the declaration says they are" (`ops/eol/census.py`, deleted with the eol family at K-064 — kept as the clearest worked example of the shape, not as a file to open), that
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

    import subprocess

    if input is not None:
        mode_kwargs = {"input": input}
    elif binary:
        mode_kwargs = {"stdin": subprocess.DEVNULL}
    else:
        mode_kwargs = {
            "encoding": "utf-8",
            "errors": "replace",
            "stdin": subprocess.DEVNULL,
        }

    # The brightline's second axis, FALLBACK ONLY. `spawn_counter`'s audit hook
    if not spawn_counter.audit_hook_installed():
        spawn_counter.bump()

    # HAND-ROLLED OVER `Popen`, deliberately -- and this comment used to say
    # MEASURED, not reasoned: caught live 2026-08-31 with
    fed_input = mode_kwargs.pop("input", None)
    if fed_input is not None:
        mode_kwargs["stdin"] = subprocess.PIPE
    wall = _wall_bound(timeout, remote)
    try:
        with subprocess.Popen(
            ["git", *args],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env) if env is not None else None,
            **mode_kwargs,
            **no_console_creationflags(),
        ) as proc:
            try:
                stdout, stderr = proc.communicate(input=fed_input, timeout=wall)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=_SPAWN_SCHEDULING_HEADROOM_SECS)
                except subprocess.TimeoutExpired:
                    pass
                return GitResult(returncode=-1, stdout="", stderr="", timed_out=True)
    except OSError:
        return GitResult(returncode=127, stdout="", stderr="", timed_out=False)

    completed = subprocess.CompletedProcess(
        args=["git", *args],
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
    )
    return GitResult(
        returncode=completed.returncode,
        stdout=_as_text(completed.stdout),
        stderr=_as_text(getattr(completed, "stderr", "")),
        timed_out=False,
        stdout_bytes=_as_bytes(completed.stdout),
    )


def git_ok(args: Sequence[str], **kwargs) -> bool:
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
