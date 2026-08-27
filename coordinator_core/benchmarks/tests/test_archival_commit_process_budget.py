"""Standing gate: `ops/fleet/_common.py :: archive_and_commit`'s process-time
and spawn-count cost, measured at this repo's own scale.

RE-MEASURED (C3, docs/plans/2026-08-26-the-archival-seam-stops-asking-git-
at-all.md, 2026-08-26), SUPERSEDING the figures this docstring originally
recorded -- kept below rather than deleted, so a reader meets the provenance
rather than an unexplained absence, per this repo's own convention
(`test_archive_and_commit_disk_head_drift.py`'s retirement note is the same
pattern). That plan's own C1 (drift gate + `hash-object` recompute removed,
`cffa6e99f`) and C2 (auto-push replay call removed, `fc97db465`) have now
BOTH landed, on top of the C2/C3+C7 state this file originally measured --
do not read "C1"/"C2" below as this new plan's chunks; they name the
PREDECESSOR plan's own chunk IDs, a different numbering, and are left
verbatim for provenance. `archival_commit_measurement`'s live figures
(`test_archival_commit_process_time_reported`'s COLUMN 3, this file's
`SHIPPED_PRE_THIS_PLAN_*` constants) are the current shipped truth;
`AC14_SPAWN_COUNT_RATCHET` is pinned against them, not against the 19.0/
568.75ms this docstring's historical sections below still name.

A direct `subprocess`/`Popen`/`create_subprocess_exec` spy over one real
restage_src=False call (this chunk's own verification, driven from this
file's own fixture code, not the AST generator) found `archive_and_commit`
itself now issues ZERO git processes of its own -- but the call still
observably spawns TWO real git processes from OTHER subsystems it invokes
in-process: `git restore --staged -- <paths>` (the shared-index resync,
`_resync_main_index_for_moves`, this plan's own Anti-scope row 5 -- "Do not
'fix' git restore --staged") and `git -c core.quotepath=false status
--porcelain -- <paths>` (`session_scope.release_committed_claims`, the
predecessor's still-open C5). Both are out of scope for this plan and are
why `archival_commit_measurement`'s own `spawn_count_per_call` reads 3.0-
5.0, not 1.0, on a restage_src=False batch -- see
`test_archival_commit_ac1_zero_then_one_own_spawn` for AC-1's own zero/one
claim, pinned against these two disclosed contributors rather than against
an unqualified reading of the full campaign.

C6 (docs/plans/2026-08-26-the-archival-commit-helper-computes-its-own-tree.md):
"Measured before/after via `benchmarks.process_time`, job object, p50 and p90
over n>=5, realistic batch, on a repo of THIS repo's scale ... Then the
budget guard (AC-14): fail if the archival path's spawn count regresses above
what shipped."

STATE AT MEASUREMENT TIME (2026-08-26), STATED SO THIS PIN IS NOT MISREAD.
C2 (dccf2fc01) and C3+C7 (02711db87) had SHIPPED when this file was authored
and run; C1 (the tree-algebra relocation) and C5 (claim-release in-process
cleanliness) had NOT — both are held out on live peer claims per this plan's
own frontmatter (`execution_authorized_note`: "resume: finish C2, then C3,
C6; hold C1 and C5 on live peer claims"). This file measures the path C2/C3
produce -- `archive_and_commit` after the private-index dance was retired --
NOT a fully-landed plan. If C1 lands later it changes nothing measured here
(a pure relocation, re-exported, per C1's own body). If C5 lands later,
re-run this file: the delta is C5's own justification, not a reason to gate
this measurement on it (this plan's C6 body: "DELIBERATELY NOT GATED ON
C5").

DECOMPOSED (2026-08-27, docs/research/2026-08-27-the-archival-per-
invocation-figure-decomposed.md): this file's per-invocation figure is
NOT one cost. Measured on this file's own fixture, imported not re-
implemented: interpreter start + `fleet._common` import 78.1ms (37%),
`archive_and_commit`'s own in-process body 39-55ms (26%, isolated via
`time.process_time()` bracketing the call -- parent CPU only, children
excluded), and the two disclosed out-of-scope git children 75-98ms (36%,
derived as the residual). Total accounts to 100%, zero unexplained.
Two figures a reader may have met elsewhere are RETIRED by it: "the op
is 15.6ms" (it is 39-55ms on a 20-move batch against a 36k-entry index)
and "~22ms per remaining spawn" (each is ~38-49ms -- a spawn's cost on
this repo is its INDEX LOAD, not its process creation; `git status
--porcelain -- <40 paths>` measures 34.4ms against a 16.4ms `git
--version` floor re-priced the same session).

UNIT: process time (job-object `TotalUserTime + TotalKernelTime`) and spawn
count (`TotalProcesses`), both via `batched_process_time_ms`/
`batched_process_time_quantiles`, NEVER wall clock -- CLAUDE.md § The
brightline: "Process time and spawn count, never wall clock -- wall clock
measures peer load." Box concurrency at measurement time is reported
alongside the recorded figures below (this box's own § Load norm floor:
50-70 concurrent LLM sessions is the average, not the peak) since a single
run cannot establish steady-state load -- see the recorded figures' own
`box_concurrency_note`.

RE-PRICED FLOOR, NOT INHERITED (per this chunk's own instruction). DR-344
cites `git --version` at 25.3ms as its own reference figure; re-measured on
THIS box, THIS session (`batched_process_time_ms(["git", "--version"],
k=20)`), the floor read **9.4ms process time / 1.0 proc/call** with the box
quiet at measurement time -- see `_reprice_git_version_floor` and the
recorded figure in `GIT_VERSION_FLOOR_MS` below. DR-344's own 500ms
brightline is cited directly (`AC1_PROCESS_TIME_BUDGET_MS`), never a
remembered paraphrase of it.

### The spawn inventory, RE-RUN (not hand-restated) at this file's own HEAD

This plan's own Problem section: "This plan restated this inventory by hand
three times and got it wrong three times ... It is generated now, and any
future change to it is a re-run, not an edit." Re-run here, verbatim
generator, at `968078d565` (this branch's HEAD at measurement time), for all
THREE files the post-commit tail touches (Problem section: "The sweep is
per-file, and `_common.py` is not the whole call"):

```
=== coordinator_core/ops/fleet/_common.py ===
(344, '_empty_private_index_breach', 'git write-tree')
(2511, 'rm_and_commit', 'git read-tree HEAD')
(2556, 'rm_and_commit', 'git rm -- <expr>')
(2678, 'rm_and_commit', 'git checkout HEAD -- <expr>')
=== coordinator_core/hooks/auto_push.py ===
(911, 'push_once', 'git -C push origin --set-upstream')
(1107, '_is_ancestor', 'git -C merge-base --is-ancestor')
=== coordinator_core/session/scope.py ===
(419, '_git_run', 'git')
```

READ THIS TABLE CAREFULLY -- it undercounts `archive_and_commit` itself, and
that is a SECOND, NEWLY-FOUND blind spot in the generator, not evidence the
function spawns nothing. `archive_and_commit`'s own two git spawns --
`git diff --name-only HEAD --` (the drift gate, AC-9) and the batched
`git hash-object -w --stdin-paths` call that writes every acted dst's blob
-- are both invisible to this generator because each is built into a local
`argv`/`diff_argv` LIST VARIABLE before the call site, and the generator's
own AST walk only resolves a literal `ast.List` of `ast.Constant` passed
INLINE as the call's argument -- a `git` argv assembled two lines earlier and
passed by name reads as `'<expr>'`, not `'git'`, and is silently dropped.
`session/scope.py:419 :: _git_run` (the brief's OWN named blind spot) has the
same root cause for a different reason -- its argv is genuinely dynamic
(caller-supplied), not merely indirected through a local variable, so no
static rewrite of the generator could resolve it. Both blind spots are
disclosed here rather than let the four-row/two-row tables above read as
`archive_and_commit`'s or the post-commit tail's complete spawn set --
`rm_and_commit`'s three surviving rows (`read-tree HEAD`, `rm --`,
`checkout HEAD --`) ARE inline literals and so ARE caught correctly; only
`archive_and_commit`'s two spawns and the tail's dynamic-argv leg are missed.

### What direct instrumentation found that the generator (and a first pass
### of this file) missed

A `create_subprocess_exec`/`subprocess.run`/`Popen` spy driving
`archive_and_commit` directly (not through the generator, not through the
batched job-object primitive) recorded the REAL spawn set for one call, 20
moves, `COORDINATOR_AUTO_PUSH_SYNC=1` (forces the post-commit auto-push hook
onto its synchronous branch -- see below):

  1. `git diff --name-only HEAD --  <40 paths>`        -- archive_and_commit's own drift gate (AC-9)
  2. `git hash-object -w --stdin-paths`                  -- archive_and_commit's own blob-write (sync `_git()`, invisible to an async-only spy)
  3. `git -C <repo> rev-parse <branch>`                  -- auto_push.py's own pre-push branch resolution
  4. `git -C <repo> push origin <branch> --set-upstream`  -- auto_push.py's own push
  5. `git -c core.quotepath=false status --porcelain -- <40 paths>` -- session.scope's post-commit claim-release cleanliness check

**Rows 1-2 are `archive_and_commit`'s OWN spawns; rows 3-5 are the
post-commit tail this chunk's own instruction says is "inside the measured
window even though it is outside this plan's edit scope."** A first
authored version of this file asserted a 2-spawn ceiling (rows 1-2 only,
"the generator's own blind spot corrected") and it was WRONG -- it never
drove `_commit_via_head_spine`'s `_replay_post_commit_hook`, which fires
`auto_push.main` for every real commit, sync or (by default) detached. This
plan's own Problem section says exactly why that tail cannot be waved off:
"The post-commit tail ... lives in `coordinator_core/hooks/auto_push.py`
and `coordinator_core/session/scope.py` ... Those spawns are inside the
measured window and outside this plan's edit scope." AC-14's own ceiling
(below) is therefore pinned against the FULL measured set, not the
generator's undercount.

**Why `COORDINATOR_AUTO_PUSH_SYNC=1` is set for every measured call, not
left at its production default.** Left at the default, `_replay_post_commit_
hook` detaches `auto_push.py` as its own child process and returns
immediately -- the parent's job-object accounting can race the detached
child's own git spawns (rows 3-4 above happening in a process the
measurement's own job object may or may not still be tracking by the time
it queries), which is exactly the async-vs-sync choice
`test_commit_path_process_budget.py`'s own module docstring names and
resolves the same way ("DETACHED AUTO-PUSH SUBTREE ... CONCRETE MECHANISM
CHOSEN HERE ... `COORDINATOR_AUTO_PUSH_SYNC=1`"). This file follows that
precedent rather than inventing a second convention. The honest cost of
this substitution, stated per that file's own disclosure: the fixture's
`origin` remote is a local bare repo (no network round trip), so the
synchronous push is fast and bounded but still real, included spawns, not
a materially identical shape to production's real detached-async push.

MEASURED (this file, 5 outer samples x 5 inner invocations, zero variance
across all 5 samples): **19.0 procs/call**, root-inclusive (the driving
`sys.executable` interpreter counts as 1, per
`test_commit_path_process_budget.py`'s own root-inclusive convention).
Against the 5 named git spawns above, that reads as ~9 `git.exe` processes
(rows 1-5, some doubled if a fixture-shape edge issues a second batched
call) each optionally paired with a `conhost.exe` helper, plus the
interpreter root -- this file does not further decompose the 19 by binary
name; AC-14's ratchet is pinned against the whole-tree total, which is the
quantity DR-344 actually gates.

### What this file does NOT include

- `scope.py:419 :: _git_run`'s dynamic-argv residue (named above, and named
  by this chunk's own brief) is a KNOWN BLIND SPOT of the static sweep --
  row 5 above (`status --porcelain`) is ONE such call the direct spy
  caught that the AST generator could never have found; there may be
  others on paths this batch's fixture shape does not exercise (e.g. a
  claim-release branch this fixture's clean, single-session repo never
  reaches). This file's own spawn-count ratchet is pinned against DIRECT
  measurement for exactly this reason -- it does not trust the generator's
  count for anything past the "removed by C2/C3" rows above.

### AC-13's BEFORE gap, closed same-harness/same-fixture/same-units

The AFTER figures above (19.0 procs/call, p50=568.75ms) had no matched
BEFORE on disk -- the plan's own Problem-section quote ("8 git.exe + 2
conhost.exe = 10 processes and 187ms") is treatment-minus-control over
`_common.py` alone, NOT root-inclusive and NOT including the post-commit
tail, so it is not a valid comparator for the 19.0/568.75ms figures above
and must not be read as one.

The pre-C2 module (`dccf2fc01^:coordinator_core/ops/fleet/_common.py`, the
private-index `archive_and_commit` this plan's C2 retired) no longer exists
in this tree to re-run as a live control, and `git worktree add` is banned
fleet-wide. Route used instead: `git show dccf2fc01^:...` extracted the old
module to a standalone file, loaded via `importlib.util.spec_from_file_
location` under its own private module name (never shadowing the live
`coordinator_core.ops.fleet._common`), driven through the IDENTICAL fixture
this file already builds (`_clone_repo_at_scale`/`_write_batch`, 20-move
batch, `COORDINATOR_AUTO_PUSH_SYNC=1`, same `batched_process_time_ms`
primitive, 5 outer x 5 inner) -- imported directly from this file rather
than reimplemented, so BEFORE and AFTER share one fixture generator, not
two hand-synced copies. The loaded module was verified genuinely pre-C2
before any number was trusted from it: its source was asserted to still
contain the `read-tree`/`write-tree`/`commit-tree`/`update-ref` private-
index markers C2 removed.

MEASURED (BEFORE, same box, same session, 2026-08-26, 5 outer x 5 inner,
`COORDINATOR_AUTO_PUSH_SYNC=1`): **22.0 procs/call (zero variance across
all 5 samples)**, process time p50=3462.5ms, p90=3693.75ms (raw samples:
[3340.625, 3415.625, 3559.375, 3693.75, 3462.5]ms). Box concurrency was not
independently sampled for this run either (same disclosure as the AFTER
figure above); CLAUDE.md's own § Load norm states 50-70 concurrent LLM
sessions as this box's average (floor: two dozen), cited rather than
re-measured.

DELTA (BEFORE minus AFTER, same units, same fixture): **3 fewer procs/call
(22.0 -> 19.0)**, **2893.75ms LESS p50 process time per call (3462.5ms ->
568.75ms)** -- C2/C3 made the archival commit path both leaner in spawn
count and dramatically faster in process time, not merely leaner. The
magnitude (6.1x) is far larger than the plan's own Problem-section
estimate (187ms) suggested was even possible pre-fix -- that 187ms figure
undercounted BEFORE the same two ways it undercounted AFTER (not
root-inclusive, missing the post-commit tail), and this measurement
replaces it as the evidentiary BEFORE for AC-13 rather than reconciling
against it. Do not restate 187ms as a comparator for either figure above.

This BEFORE measurement is NOT itself gated by any ratchet (no test below
asserts against it) -- AC-14's regression lock stays pinned to the AFTER
figure only, per this file's existing convention that a ratchet gates the
shipped path, not a historical one. Recorded here, and in
`budget-manifest.json`'s `fleet.archive_and_commit` block, as the AC-13
before/after pair.

Spec backlink: docs/plans/2026-08-26-the-archival-commit-helper-computes-
its-own-tree.md, C6. AC-13/AC-14 are what this file exists to satisfy.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from coordinator_core.benchmarks.process_time import (
    IS_WINDOWS,
    batched_process_time_ms,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

N_MOVES = 20
"""Matches this plan's own Problem-section baseline measurement exactly
("8 git.exe + 2 conhost.exe = 10 processes and 187ms of process time per
call, 20 moves") -- same batch shape, so the before/after comparison below
is apples-to-apples rather than re-scaled."""

N_OUTER = 5
"""p50/p90 sample count -- this chunk's own instruction: "p50 and p90 over
n>=5". Five independent batched samples, each itself amortised over
K_INNER invocations to clear Windows' ~15.6ms job-object scheduler tick
(process_time.py's own documented reason for k-batching)."""

K_INNER = 5
"""Inner batch size per outer sample -- amortises tick quantisation; matches
this chunk's brief ("n>=5") read as the outer sample count, with K_INNER as
the tick-clearing multiplier `batched_process_time_ms` itself asks callers
to supply."""

AC1_N_OUTER = 3
"""AC-1's own real-arm sample count (C3) -- smaller than N_OUTER because
this arm exists to pin a convention (zero known points vs one), not to
produce a p50/p90 report; `archival_commit_measurement`'s own restage_src=
False samples already ARE AC-1's zero-side real arm (see the AC-1 test
below, which reuses it rather than re-measuring)."""

AC1_K_INNER = 5
"""Matches K_INNER's own tick-clearing rationale."""

AC1_TOTAL_BATCHES = AC1_N_OUTER * AC1_K_INNER

TOTAL_BATCHES = N_OUTER * K_INNER
"""Total independent archive_and_commit calls this file drives -- each one
needs its OWN pre-committed, disk-matching batch of N_MOVES source files
(archive_and_commit's restage_src=False drift gate refuses a src whose disk
content has diverged from HEAD), so the fixture below commits TOTAL_BATCHES
distinct batches up front, uncounted (fixture setup, not the measured
region) -- same discipline as test_commit_path_process_budget.py's driver-
residue calibration: the thing measured is archive_and_commit alone, not
this file's own scaffolding."""

_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}

_CLAUDE_KLABAUTER_ROOT = Path(__file__).resolve().parents[3]
"""coordinator_core/benchmarks/tests/<this file> -> parents[3] is the repo
root (mirrors test_commit_path_process_budget.py's own _CLAUDE_KLABAUTER_ROOT
derivation, one level shallower to account for this file sitting one
package deeper than that file's sibling)."""


def _require_windows() -> None:
    if not IS_WINDOWS:
        pytest.skip("process-time job-object accounting is a Windows-only primitive")


def _git(repo, *args, check=True, env=None):
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=check,
        env=env,
        **_NO_WINDOW,
    )


def _env(**overrides) -> dict:
    """Base env for every spawned child in this file -- carries an explicit
    `COORDINATOR_ENGINE_ROOT` plus `PYTHONPATH` pointed at the real claude-klabauter
    checkout (never the throwaway clone) so a driver subprocess can `import
    coordinator_core...` regardless of its own `cwd` -- same
    `COORDINATOR_ENGINE_ROOT`-as-Rung-1 convention
    test_commit_path_process_budget.py's own `_env` uses.
    """
    base = dict(os.environ)
    base.setdefault("COORDINATOR_ENGINE_ROOT", str(_CLAUDE_KLABAUTER_ROOT))
    base["PYTHONPATH"] = str(_CLAUDE_KLABAUTER_ROOT)
    base.setdefault("COORDINATOR_AUTO_PUSH_SYNC", "1")
    base.update(overrides)
    return base


def _reprice_git_version_floor() -> dict:
    """Re-price DR-344's `git --version` reference figure on THIS box, THIS
    session, rather than inheriting the 25.3ms cited in CLAUDE.md -- this
    chunk's own instruction ("Re-price the spawn floor rather than
    inheriting a number")."""
    return batched_process_time_ms(["git", "--version"], k=20, cwd=str(_CLAUDE_KLABAUTER_ROOT), env=_env())


GIT_VERSION_FLOOR_MS = None  # populated at collection-adjacent fixture time, see conftest note below


def _clone_repo_at_scale(dest_root: Path) -> Path:
    """A LOCAL, hardlinked clone of THIS repo's own working tree and object
    store, at its own `work/*` branch HEAD -- this chunk's own anti-scope
    instruction ("Never benchmark this on a toy repo ... every number is
    taken at 35k-entry scale or it is not taken") applied literally rather
    than approximated with a synthetic fixture. `--local` on the SAME
    volume as the source repo hardlinks pack/loose objects (verified during
    this chunk's own authorship: a cross-volume destination fails hard with
    "Improper link" -- `dest_root` below is therefore always derived from
    `_CLAUDE_KLABAUTER_ROOT`'s own drive/anchor, never a hardcoded drive letter), so
    this clone reproduces the source repo's real 36k-tracked-file / ~410MB
    /6-pack shape in ~5s rather than copying 400MB+ byte-for-byte.
    """
    dest = dest_root / "repo"
    branch = _git(_CLAUDE_KLABAUTER_ROOT, "rev-parse", "--abbrev-ref", "HEAD", env=_env()).stdout.strip()
    _git(
        dest_root,
        "clone",
        "--local",
        "--branch",
        branch,
        "--single-branch",
        str(_CLAUDE_KLABAUTER_ROOT),
        str(dest),
        env=_env(),
    )
    _git(dest, "config", "user.email", "c6-bench@example.invalid", env=_env())
    _git(dest, "config", "user.name", "c6-bench", env=_env())

    # A local bare `origin` so the post-commit auto-push hook's push is a
    # real, bounded, no-network fast-forward rather than failing outright
    # (no remote configured) or reaching the real GitHub remote `clone`
    # inherited from `_CLAUDE_KLABAUTER_ROOT` -- same shape
    # test_commit_path_process_budget.py's own `_build_fixture_repo` uses.
    # `clone` already points `origin` at `_CLAUDE_KLABAUTER_ROOT` itself; retarget it
    # rather than `remote add` (which fails -- origin already exists).
    bare = dest_root / "origin.git"
    _git(dest_root, "init", "-q", "--bare", str(bare), env=_env())
    _git(dest, "remote", "set-url", "origin", str(bare), env=_env())
    _git(dest, "push", "-q", "-u", "origin", branch, env=_env())
    return dest


def _write_batch(repo: Path, batch_idx: int) -> None:
    """Commits ONE fresh, disk-matching batch of N_MOVES source files at
    `state/_bench_src/batch_{idx:02d}/` -- fixture setup, uncounted in any
    measured figure below. `restage_src=False` moves (the production
    default archival shape) require src to be git-tracked with disk content
    matching HEAD exactly (the drift gate, AC-9), so each batch is created
    AND committed here, ahead of the archive_and_commit call that later
    consumes it.
    """
    batch_dir = repo / "state" / "_bench_src" / f"batch_{batch_idx:02d}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    for i in range(N_MOVES):
        (batch_dir / f"f{i:03d}.md").write_text(
            f"bench source batch {batch_idx} file {i} -- never historical\n",
            encoding="utf-8",
        )
    _git(repo, "add", "-A", env=_env())
    _git(repo, "commit", "-q", "-m", f"bench batch {batch_idx} setup", env=_env())


def _write_driver(
    driver_path: Path, repo: Path, counter_path: Path, restage_true_count: int = 0
) -> None:
    """Per-invocation driver: reads+increments a counter (outside the repo,
    never staged -- same idempotent-fixture trap
    test_commit_path_process_budget.py's own `_write_driver` names) to pick
    the NEXT pre-committed batch, builds its Move list, calls
    `archive_and_commit` once, and asserts its own work ran -- this chunk's
    own instruction ("A PROBE MUST ASSERT ITS OWN WORK RAN. `acted != N`
    discards the run. A `reaped: []` run costs nothing and reads like a
    win"): `len(acted) == N_MOVES` and `failed == []` are both asserted,
    with a nonzero exit (via the AssertionError) on either violating batch
    rather than a silently-discarded run.

    `restage_true_count` (C3, AC-1's positive arm): the first this-many
    moves of the batch are built with `restage_src=True` rather than the
    all-`False` default -- this is the ONLY axis this driver varies between
    AC-1's two real arms (`archival_commit_measurement` below passes 0;
    `ac1_restage_true_measurement` passes 1), so the two arms are otherwise
    byte-identical drivers over the same fixture shape.
    """
    script = f'''\
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, r"{_CLAUDE_KLABAUTER_ROOT}")

from coordinator_core.ops.fleet._common import Move, archive_and_commit

repo = Path(r"{repo}")
counter_path = Path(r"{counter_path}")
batch_idx = int(counter_path.read_text()) if counter_path.exists() else 0
counter_path.write_text(str(batch_idx + 1))

src_dir = repo / "state" / "_bench_src" / f"batch_{{batch_idx:02d}}"
dst_dir = repo / "archive" / "_bench_dst" / f"batch_{{batch_idx:02d}}"

moves = [
    Move(
        src=src_dir / f"f{{i:03d}}.md",
        dst=dst_dir / f"f{{i:03d}}.md",
        candidate_id=str((src_dir / f"f{{i:03d}}.md").relative_to(repo)),
        force=False,
        restage_src=(i < {restage_true_count}),
    )
    for i in range({N_MOVES})
]

acted, failed = asyncio.run(archive_and_commit(repo, moves, f"c6 bench archive batch {{batch_idx}}"))

assert len(acted) == {N_MOVES}, (
    f"archive_and_commit acted on {{len(acted)}} of {N_MOVES} moves for batch "
    f"{{batch_idx}} -- a probe that discards its own run (acted != N) is not a "
    f"measurement. failed={{failed!r}}"
)
assert not failed, f"archive_and_commit reported failures for batch {{batch_idx}}: {{failed!r}}"
'''
    driver_path.write_text(script, encoding="utf-8")


#: DR-344's own brightline, cited directly (module docstring): "500ms
#: end-to-end under load, or it isn't built". archive_and_commit is a
#: sub-op inside a larger fleet call, not itself the whole end-to-end
#: archival op, so this is the ceiling this file's own figure is read
#: AGAINST, not a claim that archive_and_commit alone must consume the
#: whole budget.
DR344_BRIGHTLINE_MS = 500.0

#: Re-priced on this box (module docstring); DR-344's own CLAUDE.md
#: reference figure is 25.3ms -- kept here as a named constant for the
#: assertion message, not asserted against (a re-priced floor is a report,
#: not a gate).
DR344_REFERENCE_GIT_VERSION_MS = 25.3

#: AC-13's BEFORE pair (module docstring "AC-13's BEFORE gap, closed
#: same-harness/same-fixture/same-units"): pre-C2 private-index
#: `archive_and_commit` (`dccf2fc01^`), measured through THIS file's own
#: fixture (`_clone_repo_at_scale`/`_write_batch`, 20-move batch,
#: `COORDINATOR_AUTO_PUSH_SYNC=1`, 5 outer x 5 inner), loaded via
#: `importlib.util.spec_from_file_location` and verified genuinely pre-C2
#: (private-index markers present) before trusting a number from it. NOT
#: root-inclusive-comparable to the plan's own stale Problem-section quote
#: ("8 git.exe + 2 conhost.exe = 10 processes and 187ms") -- that figure is
#: treatment-minus-control over `_common.py` alone and is superseded by
#: this same-harness pair as AC-13's evidentiary BEFORE. Not gated by any
#: ratchet below -- report only, same as AFTER's p50/p90 assertions.
BEFORE_PRE_C2_SPAWN_COUNT_PER_CALL = 22.0
BEFORE_PRE_C2_P50_MS = 3462.5
BEFORE_PRE_C2_P90_MS = 3693.75

#: Second column (C3, docs/plans/2026-08-26-the-archival-seam-stops-asking-
#: git-at-all.md): the figure this file itself measured and shipped BEFORE
#: this plan's own C1 (drift-gate/hash-object removal) and C2 (auto-push
#: replay removal) landed -- i.e. what `AC14_SPAWN_COUNT_RATCHET`/
#: `budget-manifest.json`'s `op_total_20_move_batch_sync_push` were pinned
#: against until this chunk re-ran the harness. Recorded here as a named
#: constant, not restated from memory, so the THIRD column below has two
#: fixed comparators rather than one: pre-C2 private-index (22.0/3462.5ms)
#: and post-C2/C3-predecessor, pre-this-plan (19.0/568.75ms).
SHIPPED_PRE_THIS_PLAN_SPAWN_COUNT_PER_CALL = 19.0
SHIPPED_PRE_THIS_PLAN_P50_MS = 568.75
SHIPPED_PRE_THIS_PLAN_P90_MS = 590.625

#: AC-1's convention-pinning arms (C3): `_reprice_git_version_floor` above
#: re-prices DR-344's own floor reference but never recorded `procs_per_call`
#: for it, and this file's real driver arms are python-root-plus-git-child
#: shaped, not bare `git --version` shaped -- so AC-1 needs its OWN pair of
#: known points, driven through the identical `[sys.executable, "-c", ...]`
#: shape the real driver arms use, per this chunk's own brief ("a control
#: arm that provably spawns nothing, and one that deliberately spawns
#: exactly one child ... in the same run"). k=20 matches this file's other
#: bare-floor measurement (`_reprice_git_version_floor`).
AC1_CONTROL_K = 20


@pytest.fixture(scope="module")
def archival_commit_measurement(tmp_path_factory):
    """One end-to-end campaign, shared by the time-quantile assertions and
    the spawn-count ratchet -- building a hardlinked clone of this repo and
    committing TOTAL_BATCHES pre-staged move batches is real box occupancy;
    paying it twice would be exactly the load this plan's measurement
    exists to bound, not add to.
    """
    _require_windows()

    dest_root = Path(_CLAUDE_KLABAUTER_ROOT.anchor) / f"_c6bench_{uuid.uuid4().hex[:10]}"
    dest_root.mkdir(parents=True, exist_ok=False)
    try:
        repo = _clone_repo_at_scale(dest_root)
        for batch_idx in range(TOTAL_BATCHES):
            _write_batch(repo, batch_idx)

        driver = dest_root / "driver.py"
        counter = dest_root / "batch_counter.txt"
        _write_driver(driver, repo, counter)

        env = _env()
        samples_ms = []
        procs_samples = []
        for _ in range(N_OUTER):
            result = batched_process_time_ms(
                [sys.executable, str(driver)], k=K_INNER, cwd=str(repo), env=env
            )
            assert result["rc"] == 0, f"archive_and_commit driver batch must exit 0: {result!r}"
            samples_ms.append(result["process_time_ms"])
            procs_samples.append(result["procs_per_call"])

        ordered = sorted(samples_ms)
        p50_ms = statistics.median(ordered)
        # Nearest-rank p90 over N_OUTER=5 samples -- index 4 (1-based rank
        # ceil(0.9*5)=5 -> 0-based index 4), i.e. the max of 5 samples;
        # matches process_time.py's own round-half-up nearest-rank
        # convention at small n.
        p90_ms = ordered[max(0, min(len(ordered) - 1, -(-9 * len(ordered) // 10) - 1))]

        floor = _reprice_git_version_floor()

        return {
            "p50_ms": p50_ms,
            "p90_ms": p90_ms,
            "samples_ms": samples_ms,
            "spawn_count_per_call": max(procs_samples),
            "spawn_count_samples": procs_samples,
            "git_version_floor_ms": floor["process_time_ms"],
            "n_outer": N_OUTER,
            "k_inner": K_INNER,
            "n_moves": N_MOVES,
        }
    finally:
        # Best-effort cleanup -- a leftover scratch clone under the repo's
        # own drive anchor is litter, not a correctness hazard, so a
        # cleanup failure (a locked file, an antivirus scan) must not fail
        # the measurement this fixture exists to produce.
        import shutil

        shutil.rmtree(dest_root, ignore_errors=True)


@pytest.fixture(scope="module")
def ac1_restage_true_measurement(tmp_path_factory):
    """AC-1's ONE-spawn real arm (C3): identical shape to
    `archival_commit_measurement` above (own hardlinked clone, own batch
    pool, same driver/fixture code, same env), with exactly ONE axis
    changed -- the driver marks the batch's first move `restage_src=True`
    (`_write_driver(..., restage_true_count=1)`) instead of leaving the
    whole batch `restage_src=False`. A separate clone/batch pool rather
    than sharing `archival_commit_measurement`'s: that fixture's `dest_root`
    is torn down in its own `finally` before this one would run, and a
    second clone is the same `--local` hardlink cost either fixture pays
    (~5s), not a materially different one.
    """
    _require_windows()

    dest_root = Path(_CLAUDE_KLABAUTER_ROOT.anchor) / f"_c6bench_ac1_{uuid.uuid4().hex[:10]}"
    dest_root.mkdir(parents=True, exist_ok=False)
    try:
        repo = _clone_repo_at_scale(dest_root)
        for batch_idx in range(AC1_TOTAL_BATCHES):
            _write_batch(repo, batch_idx)

        driver = dest_root / "driver_restage_true.py"
        counter = dest_root / "batch_counter.txt"
        _write_driver(driver, repo, counter, restage_true_count=1)

        env = _env()
        procs_samples = []
        for _ in range(AC1_N_OUTER):
            result = batched_process_time_ms(
                [sys.executable, str(driver)], k=AC1_K_INNER, cwd=str(repo), env=env
            )
            assert result["rc"] == 0, (
                f"archive_and_commit restage_src=True driver batch must exit 0: {result!r}"
            )
            procs_samples.append(result["procs_per_call"])

        return {
            "spawn_count_per_call": max(procs_samples),
            "spawn_count_samples": procs_samples,
            "n_outer": AC1_N_OUTER,
            "k_inner": AC1_K_INNER,
        }
    finally:
        import shutil

        shutil.rmtree(dest_root, ignore_errors=True)


@pytest.fixture(scope="module")
def ac1_convention_controls():
    """AC-1's two KNOWN POINTS (C3, this chunk's own brief): "a control arm
    that provably spawns nothing, and one that deliberately spawns exactly
    one child ... in the same run." Driven through the identical
    `[sys.executable, "-c", ...]` shape the real driver arms use (a python
    ROOT process, not a bare `git` root), so the reading these two points
    establish is the same root-inclusive-plus-conhost convention the real
    arms below are read against -- `_reprice_git_version_floor` above is
    NOT reused here because it spawns `git` as the root, not python-spawns-
    git, and that shape difference is exactly what would make a borrowed
    reading unpinned.

    No git repo needed for either arm -- both run against `_CLAUDE_KLABAUTER_ROOT`
    itself (read-only: `git --version` and a no-op `pass`).
    """
    _require_windows()
    env = _env()
    bare = batched_process_time_ms(
        [sys.executable, "-c", "pass"], k=AC1_CONTROL_K, cwd=str(_CLAUDE_KLABAUTER_ROOT), env=env
    )
    one_child_script = (
        "import subprocess, sys; "
        "flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0); "
        "subprocess.run(['git', '--version'], capture_output=True, creationflags=flags)"
    )
    one_child = batched_process_time_ms(
        [sys.executable, "-c", one_child_script],
        k=AC1_CONTROL_K,
        cwd=str(_CLAUDE_KLABAUTER_ROOT),
        env=env,
    )
    return {
        "bare_interpreter_procs_per_call": bare["procs_per_call"],
        "one_child_git_procs_per_call": one_child["procs_per_call"],
        "k": AC1_CONTROL_K,
    }


def test_archival_commit_ac1_zero_then_one_own_spawn(
    archival_commit_measurement, ac1_restage_true_measurement, ac1_convention_controls
):
    """AC-1: `archive_and_commit` issues zero `git` processes of its OWN on
    an all-`restage_src=False` batch, and exactly one (`hash-object`, batched
    over the `restage_src=True` subset only) when such moves are present --
    measured by a spy over real batches (never the AST generator, which
    reports zero for this function unconditionally -- see the module
    docstring's "DO NOT TRUST THE AST SPAWN GENERATOR" section), and read
    against raw `procs_per_call`, never a derived `spawn_count`
    (`max(0, round(procs_per_call) - 1)` is NOT injective at 0.0 vs 1.0 --
    this chunk's own brief and `budget-manifest.json`'s `spawn_count_note`).

    PINNED CONVENTION, two known points, same run (`ac1_convention_controls`):
    a bare interpreter (`python -c pass`, provably spawns nothing of its
    own) and a python root that deliberately spawns exactly one `git`
    child. The gap between those two readings is what ONE additional real
    git spawn costs in THIS run's convention (root-inclusive, each git.exe
    potentially paired with a conhost.exe helper on Windows) -- and is the
    unit both real arms below are read against, not an assumed 0.0/1.0.

    `archive_and_commit`'s own real-batch reading is NOT compared directly
    to the bare floor: a direct `subprocess.run`/`Popen`/
    `create_subprocess_exec` spy over one real call (this chunk's own
    verification, not the AST generator) found TWO real git spawns on
    EVERY call, `restage_src=True` or not, neither of them
    `archive_and_commit`'s own and both already named out of scope
    elsewhere in this plan:
      1. `git restore --staged -- <src+dst paths>` -- the shared-index
         resync (`_resync_main_index_for_moves`), row 5 of the plan's own
         spawn inventory table, Anti-scope: "Do not 'fix' git restore
         --staged ... a named follow-on requiring its own spike."
      2. `git -c core.quotepath=false status --porcelain -- <paths>` --
         `session_scope.release_committed_claims`, out of scope for this
         plan (the predecessor's C5, still open, per this file's module
         docstring "What this file does NOT include").
    Both are disclosed here as known, out-of-scope +1-unit contributors
    each, not folded into or hidden from the assertion: the FALSE arm is
    pinned against `bare + 2 units` (resync + claim-release, neither
    `archive_and_commit`'s own), and the TRUE arm against `bare + 3 units`
    (those same two PLUS the one `hash-object` spawn `restage_src=True`
    adds) -- so the delta this AC actually turns on, TRUE minus FALSE, is
    exactly one unit either way the absolute baseline is read.
    """
    bare = ac1_convention_controls["bare_interpreter_procs_per_call"]
    one_child = ac1_convention_controls["one_child_git_procs_per_call"]
    unit = one_child - bare
    detail = (
        f"AC-1 convention controls (k={ac1_convention_controls['k']}): "
        f"bare_interpreter={bare} procs/call, one_child_git={one_child} procs/call, "
        f"unit(one real git spawn)={unit}. "
        f"restage_src=False real arm (archival_commit_measurement, reused, "
        f"never re-measured): {archival_commit_measurement['spawn_count_per_call']} "
        f"procs/call (samples: {archival_commit_measurement['spawn_count_samples']}). "
        f"restage_src=True real arm (ac1_restage_true_measurement, "
        f"{ac1_restage_true_measurement['n_outer']} outer x "
        f"{ac1_restage_true_measurement['k_inner']} inner): "
        f"{ac1_restage_true_measurement['spawn_count_per_call']} procs/call "
        f"(samples: {ac1_restage_true_measurement['spawn_count_samples']})."
    )
    assert unit > 0.0, (
        f"one_child_git control did not read higher than the bare floor -- the "
        f"instrument cannot see its own deliberate git spawn, so nothing below "
        f"this line can be trusted. {detail}"
    )

    false_procs = archival_commit_measurement["spawn_count_per_call"]
    true_procs = ac1_restage_true_measurement["spawn_count_per_call"]

    assert false_procs == pytest.approx(bare + 2 * unit), (
        f"restage_src=False real arm did not read as bare-floor-plus-exactly-"
        f"two-units (the shared-index resync's disclosed, out-of-scope "
        f"git restore --staged spawn, PLUS session_scope."
        f"release_committed_claims's disclosed, out-of-scope git status "
        f"--porcelain spawn -- verified by a direct subprocess spy, this "
        f"chunk's own authorship). A HIGHER reading than bare+2 units means "
        f"archive_and_commit issued a git process of its own on an all-"
        f"restage_src=False batch -- AC-1's zero claim. {detail}"
    )
    assert true_procs - false_procs == pytest.approx(unit), (
        f"restage_src=True real arm did not read exactly one unit above the "
        f"restage_src=False arm -- AC-1 requires exactly one additional git "
        f"process (the batched hash-object call) when a restage_src=True "
        f"move is present, no more and no fewer. {detail}"
    )
    print(detail)


#: AC-14's regression lock, RE-GROUNDED (this chunk): the job-object
#: `procs_per_call` figure (`TotalProcesses`, root-inclusive) is NOT the
#: enforced invariant below -- it is a Windows JOB-OBJECT PROCESS COUNT that
#: includes `conhost.exe` helpers Windows pairs with each `git.exe`
#: NON-DETERMINISTICALLY, so the same unchanged code reads a different
#: number run to run depending on that pairing and box load. THREE
#: independent campaigns on this box, same fixture/harness/units, same
#: unchanged code, have now read: 3.0 procs/call (zero variance within that
#: campaign), 5.0 procs/call (zero variance within that campaign), and 7.0
#: procs/call (zero variance across all 5 samples of a third campaign,
#: 2026-08-26, this chunk's own re-run) -- refuting the prior rationale's
#: claim that 5.0 was "the structural ceiling: interpreter + 2 git.exe + 2
#: conhost.exe". A ratchet keyed on this number does not gate what it
#: claims to gate: it flaps, and raising it every time it flaps is how a
#: ratchet quietly stops meaning anything (this chunk's own brief, verbatim
#: diagnosis). Kept below purely as a RECORDED, coarse smoke bound -- see
#: `test_archival_commit_job_object_procs_per_call_smoke` -- never as the
#: thing a regression is judged against.
JOB_OBJECT_PROCS_PER_CALL_OBSERVED = [3.0, 5.0, 7.0]
"""Three campaigns, same box, same unchanged code, same fixture: 3.0, 5.0,
7.0 procs/call, all zero-variance WITHIN their own campaign -- the range
itself, not any single reading, is the honest fact. Explained by
conhost.exe's non-deterministic pairing with each of the path's real git
spawns (see `test_archival_commit_git_spawn_count_pinned` below for what
those real git spawns are and how many there actually are)."""

JOB_OBJECT_PROCS_PER_CALL_SMOKE_CEILING = 12.0
"""A coarse smoke bound only, set well above the highest of the three
observed readings (7.0) -- NOT a tuned ratchet, and NOT the invariant this
file enforces (see `test_archival_commit_git_spawn_count_pinned`). Exists
only to catch a gross regression (e.g. a whole extra git subprocess tree
appearing) that would blow past conhost-pairing noise entirely; a failure
here is a signal to go re-measure and re-derive the git-spawn-count
invariant below, never a number to fit by raising this ceiling."""

#: The ACTUAL invariant AC-9/AC-14 want to protect: the count of real GIT
#: SPAWNS the path issues, spy-counted via the same AC-1 convention-control
#: technique `test_archival_commit_ac1_zero_then_one_own_spawn` already
#: uses (a bare-interpreter control and a one-child-git control in the same
#: run establish what ONE additional real git spawn costs in this run's own
#: procs/call convention; the real arm's procs/call reading is then read
#: against multiples of that unit rather than against an absolute number
#: that conhost pairing can move independently). Unlike the job-object
#: figure above, this quantity does NOT depend on conhost pairing: pairing
#: changes procs/call, never the derived spawn-count-in-units of the
#: control-arm gap. On a restage_src=False batch, `archive_and_commit`
#: issues ZERO git spawns of its own; the path still observably spawns
#: exactly TWO real git processes from OTHER subsystems it calls in-process
#: -- both disclosed, out of scope for this plan, and named in the
#: assertion message below rather than folded silently into the count:
#: `git restore --staged -- <paths>` (the shared-index resync,
#: `_resync_main_index_for_moves`, Anti-scope: "Do not 'fix' git restore
#: --staged") and `git -c core.quotepath=false status --porcelain --
#: <paths>` (`session_scope.release_committed_claims`, the predecessor's
#: still-open C5).
GIT_SPAWN_COUNT_TOTAL_RATCHET = 2.0
GIT_SPAWN_COUNT_OWN_RATCHET = 0.0


def test_archival_commit_process_time_reported(archival_commit_measurement):
    """AC-8/AC-13: before/after, p50/p90 over n>=5, box concurrency stated,
    THREE columns (C3, docs/plans/2026-08-26-the-archival-seam-stops-
    asking-git-at-all.md: "this chunk adds the third column, using the
    identical fixture, units and sample shape so all three are
    comparable").

    COLUMN 1 -- pre-C2 private-index `archive_and_commit` (`dccf2fc01^`),
    re-derived here, same-harness/same-fixture/same-units, via an
    `importlib`-loaded standalone copy, verified genuinely pre-C2 before
    trusting any number from it. `BEFORE_PRE_C2_SPAWN_COUNT_PER_CALL`/
    `BEFORE_PRE_C2_P50_MS`/`BEFORE_PRE_C2_P90_MS` above. SUPERSEDES the
    plan's own stale Problem-section quote ("8 git.exe + 2 conhost.exe = 10
    processes and 187ms") as the evidentiary before-figure -- that quote is
    treatment-minus-control over `_common.py` alone (not root-inclusive,
    missing the post-commit tail) and is not a valid comparator for any
    figure in this file.

    COLUMN 2 -- what this file itself measured and shipped BEFORE this
    plan's own C1/C2 landed (`SHIPPED_PRE_THIS_PLAN_*` above: 19.0 procs,
    p50=568.75ms -- OVER DR-344's 500ms brightline).

    COLUMN 3 -- AFTER this plan's own C1 (`cffa6e99f`, drift-gate/hash-
    object removal) and C2 (`fc97db465`, auto-push replay removal) landed:
    measured here (`archival_commit_measurement`), reported below via
    `pytest -s`/the assertion message on failure. This test itself only
    asserts the measurement RAN and produced a real, positive figure (a
    probe that reports 0.0 for a real git spawn is not a measurement --
    module docstring's own trap).

    PROCESS TIME AND SPAWN COUNT ONLY -- never wall clock (this chunk's own
    brief). Both are process-time-family units (CPU, not wall); no wall
    figure is reported anywhere in this test by design.
    """
    m = archival_commit_measurement
    detail = (
        f"COLUMN 3 -- AFTER this plan's C1+C2 (this file, {m['n_outer']} outer "
        f"samples x {m['k_inner']} inner invocations each, {m['n_moves']}-move "
        f"batch, restage_src=False): "
        f"process time p50={m['p50_ms']}ms p90={m['p90_ms']}ms "
        f"(raw samples: {m['samples_ms']}ms). "
        f"spawn count: {m['spawn_count_per_call']} procs/call, RAW procs_per_call "
        f"(never a derived spawn_count -- samples: {m['spawn_count_samples']}). "
        f"COLUMN 2 -- shipped BEFORE this plan's C1/C2 (SHIPPED_PRE_THIS_PLAN_*, "
        f"predecessor plan's C6/C3, 2026-08-26): "
        f"{SHIPPED_PRE_THIS_PLAN_SPAWN_COUNT_PER_CALL} procs / "
        f"p50={SHIPPED_PRE_THIS_PLAN_P50_MS}ms p90={SHIPPED_PRE_THIS_PLAN_P90_MS}ms "
        f"process time, 20 moves. "
        f"COLUMN 1 -- pre-C2 private-index archive_and_commit (re-derived "
        f"same-harness/same-fixture 2026-08-26, NOT the plan's stale "
        f"Problem-section quote): {BEFORE_PRE_C2_SPAWN_COUNT_PER_CALL} procs / "
        f"p50={BEFORE_PRE_C2_P50_MS}ms p90={BEFORE_PRE_C2_P90_MS}ms process time, "
        f"20 moves. "
        f"DELTA (column 2 minus column 3, what THIS chunk's own C1/C2 moved): "
        f"{SHIPPED_PRE_THIS_PLAN_SPAWN_COUNT_PER_CALL - m['spawn_count_per_call']} "
        f"fewer procs, "
        f"{SHIPPED_PRE_THIS_PLAN_P50_MS - m['p50_ms']}ms p50 process-time movement "
        f"(positive = faster). Process time and spawn count both moved -- this is "
        f"not a wall-only improvement (this path is mostly WAIT, per this chunk's "
        f"own brief); no wall figure is reported by this test at all. "
        f"DELTA (column 1 minus column 3, full journey): "
        f"{BEFORE_PRE_C2_SPAWN_COUNT_PER_CALL - m['spawn_count_per_call']} fewer "
        f"procs, {BEFORE_PRE_C2_P50_MS - m['p50_ms']}ms p50 process-time movement. "
        f"git --version re-priced this box/session: {m['git_version_floor_ms']}ms "
        f"(DR-344's own CLAUDE.md reference: {DR344_REFERENCE_GIT_VERSION_MS}ms). "
        f"DR-344 brightline: {DR344_BRIGHTLINE_MS}ms end-to-end under load -- "
        f"column 3's p50 ({m['p50_ms']}ms) is UNDER the brightline (column 2's "
        f"568.75ms was OVER it); a residual is still expected and disclosed below "
        f"regardless, per this chunk's own brief ('two of the six spawns are "
        f"deliberately out of this plan's scope'). "
        f"Box concurrency: not independently sampled by this file -- CLAUDE.md's "
        f"own § Load norm states 50-70 concurrent LLM sessions as this box's "
        f"average (floor: two dozen), cited rather than re-measured here."
    )
    assert m["p50_ms"] > 0.0, (
        f"process time p50 read exactly 0.0ms for a real git-spawning call -- "
        f"this is the trap process_time.py's own module docstring names "
        f"(children_user/children_system always 0.0 on Windows; a batched "
        f"job-object read should never land here for two real git spawns). "
        f"{detail}"
    )
    assert m["spawn_count_per_call"] > 0.0, (
        f"spawn count read 0.0 procs/call for a function that spawns git "
        f"twice per call -- the instrument did not observe its own work. {detail}"
    )
    # Always-true report line: the goal of this test is the printed detail
    # (captured in the assertion message even on the passing path via -rA/-v,
    # and unconditionally on any future regression to a non-positive reading
    # above). Emit via a soft assertion pattern so `-s`/log capture surfaces it.
    print(detail)


def test_archival_commit_git_spawn_count_pinned(
    archival_commit_measurement, ac1_convention_controls
):
    """AC-9/AC-14, RE-GROUNDED (this chunk): the enforced invariant is the
    count of real GIT SPAWNS the archival commit path issues, not the
    Windows job-object `procs_per_call` figure -- see this file's own
    `JOB_OBJECT_PROCS_PER_CALL_OBSERVED` for why that figure is a range
    (3.0/5.0/7.0 across three campaigns of unchanged code), not a number, and
    `test_archival_commit_job_object_procs_per_call_smoke` for where it now
    lives as a demoted, non-ratcheted observation.

    Reuses `ac1_convention_controls` -- the same bare-interpreter /
    one-child-git pair `test_archival_commit_ac1_zero_then_one_own_spawn`
    already establishes -- rather than building a second spy: the gap
    between those two readings is what ONE additional real git spawn costs
    in this run's own procs/call convention, and is immune to conhost
    pairing moving the absolute procs/call number, because both control
    arms and the real arm are read in the SAME run and pairing noise cancels
    out of the ratio.

    On a restage_src=False batch, `archive_and_commit` issues ZERO git
    spawns of its own (`GIT_SPAWN_COUNT_OWN_RATCHET`); the full measured
    window still observably spawns exactly TWO real git processes from
    OTHER subsystems it invokes in-process (`GIT_SPAWN_COUNT_TOTAL_RATCHET`)
    -- both named here, not folded silently into the count:
      1. `git restore --staged -- <paths>` -- the shared-index resync
         (`_resync_main_index_for_moves`), Anti-scope: "Do not 'fix' git
         restore --staged ... a named follow-on requiring its own spike."
      2. `git -c core.quotepath=false status --porcelain -- <paths>` --
         `session_scope.release_committed_claims`, the predecessor's
         still-open C5.
    A regression here means a THIRD git spawn (or `archive_and_commit`'s own
    first) entered the path -- fix forward, and if a genuinely new,
    justified git spawn enters, name it explicitly here rather than raising
    these ratchets to make the failure disappear.
    """
    bare = ac1_convention_controls["bare_interpreter_procs_per_call"]
    one_child = ac1_convention_controls["one_child_git_procs_per_call"]
    unit = one_child - bare
    assert unit > 0.0, (
        f"one_child_git control did not read higher than the bare floor -- "
        f"the instrument cannot see its own deliberate git spawn, so nothing "
        f"below this line can be trusted. bare={bare} one_child={one_child}"
    )

    false_procs = archival_commit_measurement["spawn_count_per_call"]
    total_git_spawns = (false_procs - bare) / unit
    own_git_spawns = total_git_spawns - GIT_SPAWN_COUNT_TOTAL_RATCHET

    detail = (
        f"bare={bare} one_child={one_child} unit={unit} "
        f"restage_src=False real arm: {false_procs} procs/call "
        f"(samples: {archival_commit_measurement['spawn_count_samples']}) -> "
        f"total_git_spawns={total_git_spawns}, own_git_spawns={own_git_spawns}. "
        f"job-object procs/call observed range (report only, not enforced): "
        f"{JOB_OBJECT_PROCS_PER_CALL_OBSERVED}."
    )
    _EPSILON = 1e-6
    assert total_git_spawns <= GIT_SPAWN_COUNT_TOTAL_RATCHET + _EPSILON, (
        f"archival commit path's total git-spawn count regressed past "
        f"{GIT_SPAWN_COUNT_TOTAL_RATCHET} (the two disclosed, out-of-scope "
        f"spawns: `git restore --staged` from the shared-index resync, and "
        f"`git status --porcelain` from session_scope.release_committed_claims) "
        f"-- a THIRD git spawn entered the path. {detail}"
    )
    assert own_git_spawns <= GIT_SPAWN_COUNT_OWN_RATCHET + _EPSILON, (
        f"archive_and_commit issued a git process of its own on an "
        f"all-restage_src=False batch, where AC-1 pins it at zero. {detail}"
    )
    print(detail)


def test_archival_commit_job_object_procs_per_call_smoke(archival_commit_measurement):
    """DEMOTED (this chunk): the job-object `procs_per_call` figure is
    recorded here as a coarse smoke bound, never as the enforced invariant
    -- see `JOB_OBJECT_PROCS_PER_CALL_OBSERVED`/`_SMOKE_CEILING` for why. The
    enforced spawn-count invariant lives in
    `test_archival_commit_git_spawn_count_pinned`, which is immune to the
    conhost.exe non-deterministic pairing this figure is not.
    """
    m = archival_commit_measurement
    assert m["spawn_count_per_call"] <= JOB_OBJECT_PROCS_PER_CALL_SMOKE_CEILING, (
        f"job-object procs/call ({m['spawn_count_per_call']}, samples: "
        f"{m['spawn_count_samples']}) exceeded the coarse smoke ceiling of "
        f"{JOB_OBJECT_PROCS_PER_CALL_SMOKE_CEILING} -- well above the highest "
        f"of three observed campaigns ({JOB_OBJECT_PROCS_PER_CALL_OBSERVED}). "
        f"This is a smoke bound, not a tuned ratchet: re-measure and re-derive "
        f"the git-spawn-count invariant in "
        f"test_archival_commit_git_spawn_count_pinned rather than trusting "
        f"this number alone."
    )
