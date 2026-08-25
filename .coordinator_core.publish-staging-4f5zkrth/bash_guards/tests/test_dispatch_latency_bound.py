"""Standing latency bound for the PreToolUse(Bash) guard dispatch chain --
`dispatch.evaluate_payload_json`, the single entrypoint every Bash tool call
in every session passes through before the command runs.

WHAT THIS MAKES IMPOSSIBLE TO REINTRODUCE (the discharge argument). Three
separate super-linear latency defects were found in this chain on 2026-08-05,
each reactively, each only because somebody happened to measure:

  1. `check()` on a 3.2 MB single-token command -- ~105 s, from `shlex.
     shlex.read_token`'s instance-attribute string append being quadratic in
     the longest single token. Fixed by `_command_tokenizer.
     _MAX_TOKENIZABLE_COMMAND_CHARS`.
  2. `check_test_suite_invocation` on `bash -c 'pytest<800 KB, no
     whitespace>'` -- 15,406 ms, from a `shlex.split` behind a safety
     exemption that was not one. Fixed at that call site.
  3. Chained `python3 -c` segments sized just under the tokenizer ceiling --
     1,578 ms, from the constant-fold budget being scoped per payload rather
     than per command. Fixed at that call site.

Each fix was a patch to one site. Nothing on disk bounded the CHAIN, so the
next instance of the same class waits for the next reviewer to trip over it.
This module is that bound: an adversarial command-shape corpus run through
the real dispatcher, with a bound asserted on the whole chain rather than on
any one guard. A new guard, or a new call site inside an existing guard, that
re-tokenizes or re-scans the command a number of times proportional to the
command's own length fails here at authoring time instead of in a session.

The three defects above are NAMED CORPUS ROWS (`historical_*`), so none of
them can silently regress.

WHY THE PRIMARY ASSERTION IS NOT A STOPWATCH. `TestTokenizerWorkAmplification`
pins COMPLEXITY, not milliseconds: it counts the characters the dispatch
chain actually hands to `shlex` for one command and divides by the length of
that command. That ratio is machine-independent, load-independent, and
integral -- it is the same number on a loaded 12-way-parallel CI box as on an
idle laptop -- and it is exactly the quantity that goes super-linear when a
guard re-tokenizes the full command once per segment. A wall-clock assertion
that catches the same defect must be loose enough to survive a shared
machine, which makes it much weaker. The stopwatch leg
(`TestDispatchWallClockCeiling`) is kept as a backstop for the cost classes
amplification cannot see (catastrophic regex backtracking, per-segment
shell-outs) and is `cadence`-marked for that reason: timing assertions do not
belong in a tier that runs 12-way parallel.

`TestDispatchSubprocessSpawnCount` and its two siblings
(`TestDispatchRmTargetSpawnCount`, `TestDispatchBranchDeleteTokenSpawnCount`)
are the third leg, added 2026-08-05, pinning the axis the NEGATIVE SPEC below
used to name as uncovered: total OS process spawns for one dispatch. Like
tokenizer-work amplification, a spawn count is an integer -- the same
appeal, kept in the fast tier for the same reason. Three distinct per-`git`-
subprocess defects were found on that pinning pass, all in
`dispatch_checks.py`, none caught by the char-length-sized corpus above
because none of them move on character length:
  a. `check_destructive_git_orphan` CHECK 1 -- `rev-parse --verify`/
     `rev-list --count` spawned once per SEGMENT of a chained `git reset
     --hard`. Fixed by an exact-`(cwd, args)`-keyed per-call memo
     (`_new_git_memo`); pinned by `TestDispatchSubprocessSpawnCount`.
  b. `check_destructive_rm` -- `git -C <parent-dir> rev-parse
     --show-toplevel` spawned once per rm TARGET, byte-identical argv
     whenever targets share a parent dir. Fixed by the same memo, reused
     (not reimplemented) across the two guard functions; pinned by
     `TestDispatchRmTargetSpawnCount`, sized by target count rather than
     character length.
  c. `check_destructive_git_orphan` CHECK 3 -- `rev-parse --verify
     refs/heads/<name>` spawned once per branch TOKEN of a `git branch -D`.
     The exact-key memo from (a)/(b) CANNOT fix this one: the ref name
     genuinely varies per token, so there is no repeated call to
     deduplicate. Fixed instead by replacing N existence probes with one
     `for-each-ref` enumeration and an in-memory membership test
     (`_local_branch_names`); pinned by
     `TestDispatchBranchDeleteTokenSpawnCount`, sized by token count.

NEGATIVE SPEC -- what this module deliberately does NOT cover:
  - Guard SHELL-OUTS (`git status --porcelain` oracles) remain excluded from
    the tokenizer-work corpus above: their wall clock varies by an order of
    magnitude with repo state, and their per-call cost is bounded by each
    guard's own subprocess timeout rather than by anything here.
    `TestDispatchSubprocessSpawnCount` and its two siblings below ARE the
    per-dispatch subprocess-COUNT bound for that axis (added 2026-08-05,
    closing a gap this NEGATIVE SPEC previously left open) -- see
    `TestDispatchSubprocessSpawnCount`'s class docstring for the three
    measurement pitfalls that made this axis easy to mismeasure.
  - Guard VERDICTS. Nothing here asserts allow/deny; the corpus is timed, not
    classified. Verdict coverage lives in each guard's own test module and in
    `test_confinement_attack_corpus.py`.
  - Any per-guard budget. The bound is on the chain as a whole, because the
    chain as a whole is what a Bash call waits for.

Spec backlink: `_command_tokenizer._MAX_TOKENIZABLE_COMMAND_CHARS` (the
per-token ceiling this module's per-command bound sits above).
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Tuple

import pytest

from coordinator_core.bash_guards import _command_tokenizer as _ct
from coordinator_core.bash_guards.dispatch import evaluate_payload_json
from coordinator_core.subagent_sandbox import engine as _sandbox_engine

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
assert (_REPO_ROOT / ".git").exists(), (
    "%r is not a real git repo root -- the subprocess-count leg below "
    "measures `resolve_git_root()`'s COLD-cache spawn path, and in a "
    "non-repo cwd every resolution fails and (deliberately, per "
    "`engine.resolve_git_root`'s docstring) is never cached, so it "
    "re-spawns on every guard that asks. That inflates the count and "
    "measures a non-repo artifact instead of the guard chain -- silently, "
    "since a non-repo cwd still runs without raising." % str(_REPO_ROOT)
)


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

#: SECURITY PROPERTY -- a denial-of-service bound on the PreToolUse hot path,
#: NOT a tuning knob. Do not raise it to "let a new guard through": raising it
#: is how a per-segment re-tokenization ships, and every character over this
#: bound is charged to every Bash call in every session.
#:
#: The quantity bounded is TOKENIZER WORK AMPLIFICATION -- the total number of
#: characters the whole dispatch chain hands to `shlex` while classifying ONE
#: command, divided by the length of that command. It is the ratio that
#: distinguishes the two complexity classes, and it does so without a clock:
#:
#:   O(guards x n)   -- amplification is CONSTANT in n. Every well-behaved
#:                      shape measured (2026-08-05) sits between 3.0 and 41.7
#:                      and does not move as the command grows 16x.
#:   O(segments x n) -- amplification GROWS with n, because each of the n/k
#:                      segments re-tokenizes the whole n-character command.
#:
#: 41.7 is the measured worst well-behaved shape (`nested_sh_c`: nested
#: `sh -c`/`python3 -c` unwrapping, which legitimately re-tokenizes each
#: unwrapped payload). Realistic commands measure 12.4-27.8. The chain
#: registers 41 guards today (`dispatch._build_guard_chain`), so a ceiling of
#: 64 is "each registered guard may tokenize the command about one and a half
#: times" -- an amount of work proportional to the GUARD COUNT, which is the
#: only thing this ratio is allowed to be proportional to. It is not a round
#: number chosen for tidiness: it is 1.5x the worst legitimate shape and the
#: first power of two above it.
_MAX_TOKENIZER_WORK_AMPLIFICATION = 64.0

#: The shape half of the same property, and the sharper of the two: a 16x
#: longer command may not cost more than 1.5x as much tokenizer work PER
#: CHARACTER. Measured (2026-08-05) across every well-behaved corpus shape at
#: 512 vs 8192 characters, the observed ratio is 0.94-1.00 -- amplification is
#: flat to three significant figures, because it is a function of the guard
#: chain and not of the input. A quadratic re-tokenization scores 10.7x on
#: this same measurement. The 1.5 allowance is therefore ~10x clear of the
#: defect class and ~1.5x clear of every legitimate shape.
#:
#: This assertion, not the absolute one above, is what catches a NEW quadratic
#: whose constant happens to be small enough to hide under the ceiling at the
#: corpus sizes tested.
_MAX_AMPLIFICATION_GROWTH = 1.5

#: Wall-clock backstop for ordinary command sizes (<= 2 KiB). Derived from
#: measurement, not chosen: with both 2026-08-05 defects fixed, the realistic
#: corpus below measures 10-34 ms per dispatch on the authoring machine
#: (macOS/arm64, warm imports) -- down from the 20-120 ms this constant was
#: first derived against. 0.3 s is ~9x the measured worst, which is the
#: headroom a shared, parallel-loaded machine needs before a timing assertion
#: starts lying. A PreToolUse hook is spent BEFORE the user's command runs, so
#: this is latency the agent and the operator both wait for, on every Bash
#: call.
_MAX_DISPATCH_SECONDS_REALISTIC = 0.3

#: Wall-clock backstop for adversarial shapes at any size, including past the
#: tokenizer ceiling. Derived: the worst adversarial shape now measures 342 ms
#: at 16 KiB (`nested_shell_c`, which legitimately re-tokenizes each unwrapped
#: payload), and the 98 KiB past-the-ceiling shapes measure 88-144 ms. 1.5 s is
#: ~4.4x the measured worst.
#:
#: RATCHET, NOT A KNOB -- this constant moves DOWN as the chain gets faster and
#: never up. It was 3.0 s when authored, derived against a 617 ms worst shape;
#: both of the defects that budget was deliberately left red against are fixed
#: (`_BYPASS_RE` backtracking, and `check_destructive_git_revert`'s per-segment
#: re-tokenize), so a 3.0 s ceiling now sits ~9x above anything the corpus can
#: produce -- wide enough that a NEW 2.9 s defect would ship green against it.
#: A budget far above the measured worst is not a safety margin; it is a blind
#: spot with a number attached. Re-derive it downward whenever the worst
#: measured shape drops materially, and never raise it to accommodate a defect.
_MAX_DISPATCH_SECONDS_ADVERSARIAL = 1.5

#: Corpus sizes for the deterministic leg -- a 16x span, small enough that the
#: whole leg costs a few seconds. Amplification is scale-free, so the defect
#: class is visible at these sizes; there is no need to build a 3 MB command
#: to see it.
_SIZE_SMALL = 512
_SIZE_LARGE = 8192

#: Wall-clock leg sizes. `_SIZE_OVER_CEILING` is past
#: `_command_tokenizer._MAX_TOKENIZABLE_COMMAND_CHARS` (64 KiB) on purpose:
#: that ceiling stops `shlex` from seeing the text, and stops nothing else --
#: whole-command regex scans and per-segment walks still run, so past the
#: ceiling is a distinct latency regime that must be timed, not assumed safe.
_WALL_CLOCK_SIZES = (1024, 16384)
_SIZE_OVER_CEILING = 98304

#: Per-dispatch subprocess-COUNT ceiling. Derived 2026-08-05, post-fix, on
#: `_REPO_ROOT` (real git repo, cold `resolve_git_root()` cache -- see
#: `TestDispatchSubprocessSpawnCount`): the worst measured shape across the
#: whole `_SPAWN_CORPUS` at both `_SIZE_SMALL` and `_SIZE_LARGE` is
#: `historical_chained_git_reset_hard` at 5 spawns (512 chars); every other
#: shape/size cell measures 2. 5 x 2 = 10 -- doubled for headroom against
#: git-version/platform spawn-count variance (e.g. an extra `rev-parse
#: --path-format=absolute --git-common-dir` probe some git builds run that
#: this measurement did not need), while staying two orders of magnitude
#: below what an UNFIXED per-segment defect produces at these same sizes
#: (714 spawns measured at 8192 chars against `check_destructive_git_orphan`
#: before its 2026-08-05 memoization fix -- see that function's F0-b
#: docstring). RATCHET, NOT A KNOB, same policy as the wall-clock budgets
#: above: moves down as the chain's spawn count improves, never up to
#: accommodate a new defect.
_MAX_DISPATCH_SUBPROCESS_SPAWNS = 10

#: Shape half of the same property, mirroring `_MAX_AMPLIFICATION_GROWTH`:
#: spawn count at `_SIZE_LARGE` must not exceed spawn count at `_SIZE_SMALL`
#: by more than this ABSOLUTE tolerance (never a ratio -- a ratio bound on a
#: small integer makes 1 -> 2 spawns a fabricated "2x regression"). Measured
#: 2026-08-05: every shape in `_SPAWN_CORPUS` is FLAT or DECREASING from
#: small to large (`historical_chained_git_reset_hard` measures 5 spawns at
#: 512 chars and 4 at 8192; the other three measure 2 at both sizes) -- a
#: per-segment defect instead GROWS spawn count with input size without
#: bound (47 -> 714 measured on the unfixed `check_destructive_git_orphan`,
#: the exact defect this axis exists to catch), so a tolerance of 2 comfortably
#: covers measurement noise while still catching that shape at either size
#: tested.
_MAX_SPAWN_COUNT_GROWTH_ABSOLUTE = 2

#: `check_destructive_rm`'s per-TARGET defect (F0-b, 2026-08-05, found in the
#: same session as the reset leg above) scales with TARGET COUNT, not with
#: command character length -- `rm a.py b.py c.py` is a few dozen characters
#: regardless of how many targets it names. A char-length corpus (`_SIZE_
#: SMALL`/`_SIZE_LARGE`) would never see this axis move: it is why
#: `_RM_SPAWN_CORPUS` below is sized by `_RM_TARGET_COUNT_SMALL`/`_LARGE`
#: instead and measured through its own fixture/test class.
_RM_TARGET_COUNT_SMALL = 3
_RM_TARGET_COUNT_LARGE = 30

#: Per-dispatch ceiling for the rm-target-count leg. Derived 2026-08-05,
#: post-fix, on real git-tracked files under `_REPO_ROOT` (the false-negative
#: note in `_rm_existing_files`'s docstring matters here: a non-existent
#: target measures a flat, unrepresentative 2 spawns regardless of count).
#: Measured worst cell across `_RM_SPAWN_CORPUS` at both target counts is 4
#: spawns (`historical_chained_rm_same_dir_targets` at 1-30 same-dir
#: targets, flat -- the memo collapses every target sharing a parent dir to
#: one resolution). 4 x 2 = 8 -- doubled for the same git-version/platform
#: headroom reasoning as `_MAX_DISPATCH_SUBPROCESS_SPAWNS`, while staying an
#: order of magnitude below the unfixed defect (12 spawns measured at just
#: 10 same-dir targets, growing linearly with target count with no bound).
#: RATCHET, NOT A KNOB -- same policy as every other bound in this module.
_MAX_RM_SPAWN_COUNT = 8

#: Growth tolerance for the rm-target-count leg, mirroring `_MAX_SPAWN_
#: COUNT_GROWTH_ABSOLUTE`: spawn count at `_RM_TARGET_COUNT_LARGE` must not
#: exceed the count at `_RM_TARGET_COUNT_SMALL` by more than this ABSOLUTE
#: amount. Measured 2026-08-05: `historical_chained_rm_same_dir_targets` is
#: FLAT (same spawn count at 3 and 30 same-dir targets -- the whole point of
#: the memo); the unfixed defect instead grows roughly 1 spawn per
#: additional target (4 at 3 targets, 31 at 30 targets measured pre-fix), so
#: a tolerance of 2 is comfortably below what even a SINGLE reintroduced
#: per-target spawn would produce across this size span (27).
_MAX_RM_SPAWN_COUNT_GROWTH_ABSOLUTE = 2

#: Floor for the multi-directory rm row -- the complement to the ceiling/
#: growth checks above, and the one that actually catches a memo that
#: silently over-collapses. A ceiling/growth bound alone cannot catch a memo
#: keyed on target COUNT instead of `(cwd, args)`: such a bug would make the
#: multi-dir row spawn FEWER processes than it should (one resolution
#: reused across directories it must not be reused across), which passes
#: every bound above -- lower always reads as "better" to a ceiling check.
#: `_RM_MULTI_DIRS` below names >= 3 distinct real directories, so a
#: correctly-scoped memo must still spawn at least one resolution per
#: distinct directory actually used; this floor is that count, not a
#: derived/rounded number.
_MIN_RM_MULTI_DIR_SPAWNS = 3


# ---------------------------------------------------------------------------
# Adversarial corpus
# ---------------------------------------------------------------------------


def _chain(unit: str, size: int, joiner: str = "; ") -> str:
    """Repeat `unit` until the command is about `size` characters long."""
    count = max(1, size // (len(unit) + len(joiner)))
    return joiner.join(unit for _ in range(count))


#: Each entry maps a target command length to a command of that shape. Shapes
#: are derived from what the guards in this package actually parse -- segment
#: splitting, `sh -c`/`python3 -c` unwrapping, heredoc-body stripping, quote
#: walking, constant folding -- not from guesses about what looks expensive.
_CORPUS: Dict[str, Callable[[int], str]] = {
    # --- the three historical defects, pinned by name -----------------------
    # 2026-08-05: 3.2 MB in one token -> ~105 s, quadratic `shlex.read_token`.
    "historical_quadratic_single_token": lambda n: 'git commit -m "%s"' % ("A" * n),
    # 2026-08-05: `bash -c 'pytest<800 KB>'` -> 15,406 ms, unguarded
    # `shlex.split` behind a false safety exemption. No whitespace anywhere in
    # the payload, so the whole thing is ONE token.
    "historical_bash_c_no_whitespace": lambda n: "bash -c 'pytest%s'" % ("x" * n),
    # 2026-08-05: chained `python3 -c` segments just under the ceiling ->
    # 1,578 ms, constant-fold budget scoped per payload rather than per
    # command.
    "historical_chained_python_c_fold": lambda n: _chain(
        "python3 -c 'import subprocess; subprocess.run([%s])'"
        % ", ".join(["chr(103)"] * 30),
        n,
    ),
    # --- shape families ------------------------------------------------------
    "huge_token_unquoted": lambda n: "git commit -m %s" % ("A" * n),
    "unterminated_quote": lambda n: 'git commit -m "%s' % ("A" * n),
    "many_segments_benign": lambda n: _chain("echo a b c", n),
    "many_segments_git": lambda n: _chain("git add -A", n),
    "many_segments_git_stash": lambda n: _chain("git stash push -- a.py", n),
    "nested_shell_c": lambda n: _chain("sh -c \"sh -c 'python3 -c \\\"print(1)\\\"'\"", n),
    "heavy_quoting": lambda n: "git commit -m " + _chain('"a\\"b\'c"', n, " "),
    "heredoc_body": lambda n: "cat <<'EOF'\n%s\nEOF" % _chain("line of text", n, "\n"),
    "command_substitution": lambda n: "echo " + _chain("$(git rev-parse HEAD)", n, " "),
    "backslash_continuations": lambda n: _chain("git status \\\n --short", n, " && \\\n"),
    "pytest_invocation_chain": lambda n: _chain("python3 -m pytest coordinator_core/", n),
    # Review: coordinator:code-reviewer (Finding 2, 2026-08-05, corrected by
    # EM measurement -- see run notes) -- cause 1 of the 14675ce8e ReDoS
    # fix (the `_WRAPPER_FLAG_GROUP`/`_BYPASS_PREFIX` outer-star overlap
    # between the `env` branch and the bare-assignment branch) has no
    # named corpus row: `many_segments_git` at `_SIZE_OVER_CEILING`
    # reproduces the reported timing via cause 2 (the old unbounded `.*`)
    # only. A repeated bare `FOO=1 ` run measures FLAT regardless of the
    # fix (the outer star never gets a competing overlapping branch to
    # partition against); a repeated `env FOO=1 ` run is the shape that
    # actually goes catastrophic pre-fix, confirmed by hand-measuring both
    # the un-atomiced pattern in isolation and the un-atomiced pattern
    # threaded back through `check_no_verify` (see
    # `TestBypassRegexFallbackReachedAndDenies` and this dispatch's own run
    # notes): 220 chars pre-fix cost ~1.4 s, 16x per 40 characters, so this
    # row does not need to be large to be decisive. A bare, unwrapped
    # `--no-verify` marker plus a `git` token (present so the outer
    # `re.search(r"\bgit\b", flat)` gate in `check_no_verify` is satisfied,
    # but positioned so the head regex still exhausts the assignment run's
    # partitions before giving up) and a trailing unterminated quote (so
    # the command is unparseable and this path actually reaches
    # `_BypassRe.search` instead of the tokenized walk) reproduces it.
    "historical_env_assignment_run_redos": lambda n: (
        "env FOO=1 " * max(1, n // len("env FOO=1 ")) + '--no-verify "unterminated git'
    ),
}

#: Ordinary commands an agent actually types, for the tight wall-clock tier.
#: Deliberately spans the guard families that do real work (git classification,
#: test-suite breadth, search rewrites) rather than trivially-short strings.
_REALISTIC: Tuple[str, ...] = (
    "git status --short",
    "python3 -m pytest coordinator_core/bash_guards/tests/test_dispatch_latency_bound.py -q",
    'git commit -m "fix: a normal commit message of ordinary length"',
    "ls -la && grep -rn needle coordinator_core | head -20",
    "git stash push -- coordinator_core/bash_guards/dispatch.py",
    "python3 -c 'import json,sys; print(json.load(sys.stdin))'",
)

#: Corpus for the subprocess-COUNT axis (`TestDispatchSubprocessSpawnCount`),
#: deliberately separate from `_CORPUS` above -- these are exactly the
#: "guard SHELL-OUTS" shapes the module NEGATIVE SPEC excludes from the
#: tokenizer-work corpus, because their cost is real `git` subprocess
#: spawns, not `shlex` work. Chained `git reset --hard` is the 2026-08-05
#: pinned defect (`check_destructive_git_orphan` CHECK 1, F0-b in that
#: function's docstring: `rev-parse --verify`/`rev-list --count` spawned
#: once per segment instead of once per distinct (target, cwd) pair). The
#: other three exercise CHECK-1-adjacent legs of the same guard family
#: (`git checkout .`, `git stash`, `git restore .`) that were already
#: correctly bounded but had no pinned regression corpus row.
_SPAWN_CORPUS: Dict[str, Callable[[int], str]] = {
    "historical_chained_git_reset_hard": lambda n: _chain("git reset --hard HEAD", n),
    "many_segments_git_checkout_dot": lambda n: _chain("git checkout .", n),
    "many_segments_git_stash_bare": lambda n: _chain("git stash", n),
    "many_segments_git_restore_dot": lambda n: _chain("git restore .", n),
}

#: Real, on-disk, git-tracked-shaped `.py` files under `_REPO_ROOT`-relative
#: directories -- NEVER hardcoded filenames. `check_destructive_rm`'s
#: per-target defect (F0-b) only reproduces for targets that EXIST on disk
#: (an early false negative this session: probing with non-existent paths
#: measured a flat, unrepresentative 2 spawns and looked like "no defect").
#: Discovering real files at measurement time, rather than naming specific
#: ones, keeps this corpus robust to a future checkout not containing
#: whatever file an author happened to reference by name.
_RM_SAME_DIR = ("coordinator_core/bash_guards",)
_RM_MULTI_DIRS = (
    "coordinator_core/bash_guards",
    "coordinator_core/percolate",
    "coordinator_core/reconcile",
    "coordinator_core/subagent_sandbox",
)


def _dir_py_files(rel_dir: str) -> List[Path]:
    directory = _REPO_ROOT / rel_dir
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.py") if p.is_file())


def _rm_same_dir_cmd(count: int) -> str:
    """`rm <count real files, all in ONE directory>` -- the shape that
    reproduces `check_destructive_rm`'s per-target defect: every target
    shares a parent dir, so a correct memo collapses the whole row to one
    `git -C <dir> rev-parse --show-toplevel` resolution regardless of
    `count`."""
    files = _dir_py_files(_RM_SAME_DIR[0])
    assert len(files) >= count, (
        "not enough real .py files under %r (%d) to build an rm corpus row "
        "of %d same-dir targets -- checkout unexpectedly sparse"
        % (_RM_SAME_DIR[0], len(files), count)
    )
    rel = [str(p.relative_to(_REPO_ROOT)) for p in files[:count]]
    return "rm " + " ".join(rel)


def _rm_multi_dir_cmd(count: int) -> str:
    """`rm <count real files, spread round-robin across >= 3 directories>`
    -- the shape a memo keyed on target COUNT instead of `(cwd, args)`
    would wrongly collapse: distinct directories must still resolve
    independently. Round-robins one file per bucket per round so both
    `_RM_TARGET_COUNT_SMALL` and `_RM_TARGET_COUNT_LARGE` touch every
    configured directory, not just the first one with enough files.

    shell-doc-ok: the bracketed shape above is a placeholder describing
    this helper's return value, not a literal command; `>= 3` is a Python
    assertion floor quoted below, not a shell version constraint.
    """
    buckets = [b for b in (_dir_py_files(d) for d in _RM_MULTI_DIRS) if b]
    assert len(buckets) >= 3, (
        "need >= 3 populated directories among %r for the multi-dir rm "
        "corpus row -- checkout unexpectedly sparse" % (_RM_MULTI_DIRS,)
    )
    targets: List[Path] = []
    round_idx = 0
    while len(targets) < count:
        added = False
        for bucket in buckets:
            if round_idx < len(bucket):
                targets.append(bucket[round_idx])
                added = True
                if len(targets) >= count:
                    break
        if not added:
            break
        round_idx += 1
    assert len(targets) >= count, (
        "not enough real .py files across %r to build a %d-target multi-dir "
        "rm corpus row -- checkout unexpectedly sparse" % (_RM_MULTI_DIRS, count)
    )
    rel = [str(p.relative_to(_REPO_ROOT)) for p in targets[:count]]
    return "rm " + " ".join(rel)


def _rm_multi_dir_count() -> int:
    """Number of distinct directories `_rm_multi_dir_cmd` actually draws
    from -- the floor `test_multi_dir_targets_resolve_independently` checks
    against, derived from the same buckets rather than a separately
    hand-counted literal."""
    return len([b for b in (_dir_py_files(d) for d in _RM_MULTI_DIRS) if b])


#: rm-shaped rows for the target-COUNT axis (see `_RM_TARGET_COUNT_SMALL`/
#: `_LARGE`), deliberately separate from `_SPAWN_CORPUS` above (which is
#: sized by character length -- an axis this defect does not move on).
#: `historical_*` because this is a pinned 2026-08-05 defect, same
#: convention as `_CORPUS`'s `historical_*` rows.
_RM_SPAWN_CORPUS: Dict[str, Callable[[int], str]] = {
    "historical_chained_rm_same_dir_targets": _rm_same_dir_cmd,
    "many_targets_rm_multi_dir": _rm_multi_dir_cmd,
}

#: Token-count sizes for the `git branch -D` leg (F0-c) -- like the rm leg,
#: this defect scales with TOKEN count, not character length.
_BRANCH_TOKEN_COUNT_SMALL = 5
_BRANCH_TOKEN_COUNT_LARGE = 100

#: Per-dispatch ceiling for the branch-token-count leg. Derived 2026-08-05,
#: post-fix: `historical_chained_branch_delete_tokens` measures a flat 2
#: spawns from 5 to 400 tokens (one `for-each-ref` enumeration regardless of
#: token count, one `rev-parse --git-dir`). 2 x 4 = 8 -- generous headroom
#: (4x, wider than the reset/rm legs' 2x) because this leg's fix changes the
#: SHAPE of the query (N probes -> one enumeration) rather than just
#: deduplicating identical calls, and unlike those legs there is no already-
#: measured non-defect baseline above 2 to anchor a tighter multiple to.
#: Still two orders of magnitude below the unfixed defect (101 spawns
#: measured at 100 tokens, growing 1:1 with token count with no bound).
#: RATCHET, NOT A KNOB -- same policy as every other bound in this module.
_MAX_BRANCH_SPAWN_COUNT = 8

#: Growth tolerance for the branch-token-count leg, mirroring `_MAX_RM_
#: SPAWN_COUNT_GROWTH_ABSOLUTE`. Measured 2026-08-05: flat (2 spawns at 5
#: tokens, 2 at 100) -- the unfixed defect instead grows 1:1 with token
#: count (6 at 5 tokens, 101 at 100), so a tolerance of 2 is far below what
#: even a single reintroduced per-token spawn would produce across this
#: span (95).
_MAX_BRANCH_SPAWN_COUNT_GROWTH_ABSOLUTE = 2


def _branch_delete_tokens_cmd(count: int) -> str:
    """`git branch -D <count nonexistent branch names, including one
    slash-named>` -- nonexistent on purpose: this leg measures dispatch
    chain cost regardless of repo branch state (unlike the rm leg, where
    on-disk EXISTENCE is what triggers the defect -- see `_rm_same_dir_cmd`
    docstring), and a nonexistent branch is the fail-open path
    `_local_branch_names` must still resolve in ONE spawn. One token is
    deliberately slash-named (`feature/x`-shaped) -- a naive batching
    implementation that mishandles a namespaced ref would fail on exactly
    this token.
    """
    tokens = ["feature/token-0"] + ["nonexistent-branch-token-%d" % i for i in range(1, count)]
    return "git branch -D " + " ".join(tokens[:count])


#: `git branch -D`-shaped row for the token-count axis (F0-c). Separate
#: corpus, same reasoning as `_RM_SPAWN_CORPUS`: this defect does not move
#: on character length.
_BRANCH_SPAWN_CORPUS: Dict[str, Callable[[int], str]] = {
    "historical_chained_branch_delete_tokens": _branch_delete_tokens_cmd,
}


def _payload(cmd: str) -> str:
    """A full PreToolUse payload carrying a resolved SUBAGENT identity, so the
    identity-gated guards run their real bodies rather than short-circuiting
    on an absent `agent_id` -- worst case is what a DoS bound must measure.
    """
    return json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "session_id": "latency-bound-probe",
            "cwd": str(_REPO_ROOT),
            "agent_id": "agent_latency_probe",
            "agent_type": "coordinator:executor",
        }
    )


# ---------------------------------------------------------------------------
# Instrumentation
# ---------------------------------------------------------------------------


class _TokenizerWorkCounter:
    """Counts the characters the dispatch chain hands to `shlex` for one
    command, across BOTH seams a guard can reach it through:
    `_command_tokenizer.tokenize_full_command` (the shared one) and a direct
    `shlex.split` (the un-migrated call sites).

    Patches by IDENTITY across every already-imported `coordinator_core`
    module, not just the defining module: guards import these names with
    `from ... import tokenize_full_command as _tok`, which binds the function
    object into the importing module's namespace, so patching only the source
    module would miss every real caller and silently measure zero.

    Text past `_MAX_TOKENIZABLE_COMMAND_CHARS` is NOT counted: the tokenizer
    refuses it in O(1) (a length comparison), so charging its length would
    report work that was never done. That is also why the wall-clock leg
    covers the over-ceiling regime and this counter does not claim to.
    """

    def __init__(self) -> None:
        self.chars = 0
        self.calls = 0
        self._command_len = 1
        self._restore: List[Tuple[object, str, object]] = []

    def _count(self, text: str) -> None:
        self.calls += 1
        if not _ct.exceeds_tokenizable_ceiling(text):
            self.chars += len(text)

    def __enter__(self) -> "_TokenizerWorkCounter":
        orig_tokenize = _ct.tokenize_full_command
        orig_split = shlex.split

        def _tokenize(cmd_text: str):
            self._count(cmd_text)
            return orig_tokenize(cmd_text)

        def _split(s, *args, **kwargs):
            self._count(s)
            return orig_split(s, *args, **kwargs)

        replacements = {id(orig_tokenize): _tokenize, id(orig_split): _split}
        targets: List[Tuple[object, str]] = [(shlex, "split")]
        for mod_name, module in list(sys.modules.items()):
            if not mod_name.startswith("coordinator_core") or module is None:
                continue
            for attr in list(getattr(module, "__dict__", {})):
                if id(getattr(module, attr, None)) in replacements:
                    targets.append((module, attr))
        seen: set = set()
        for module, attr in targets:
            key = (id(module), attr)
            if key in seen:
                continue
            seen.add(key)
            orig = getattr(module, attr)
            replacement = replacements.get(id(orig))
            if replacement is None:
                continue
            self._restore.append((module, attr, orig))
            setattr(module, attr, replacement)
        return self

    def __exit__(self, *exc) -> None:
        for module, attr, orig in reversed(self._restore):
            setattr(module, attr, orig)
        self._restore.clear()

    @property
    def amplification(self) -> float:
        return self.chars / max(1, self._command_len)

    def measure(self, cmd: str) -> float:
        self.chars = 0
        self.calls = 0
        self._command_len = len(cmd)
        evaluate_payload_json(_payload(cmd))
        return self.amplification


def _time_dispatch(cmd: str) -> float:
    start = time.perf_counter()
    evaluate_payload_json(_payload(cmd))
    return time.perf_counter() - start


class _SpawnCountCounter:
    """Counts real OS process spawns the dispatch chain makes for one
    command.

    Patches `subprocess.Popen` ONLY -- `subprocess.run`/`check_output`/
    `call` are all implemented in terms of `Popen` (they resolve the name
    `Popen` in the `subprocess` module's own globals at call time), so
    patching several of them here would double- or triple-count the same
    underlying spawn. This mistake was made once already this session (a
    4x-inflated count from patching more than `Popen`) -- see the module
    docstring's NEGATIVE SPEC entry for this class.

    A single assignment to `subprocess.Popen` is sufficient with no
    identity-propagation dance (unlike `_TokenizerWorkCounter` above): every
    `import subprocess` anywhere in the process shares the same module
    object, and `_run_git`/`resolve_git_root` call `subprocess.run(...)`,
    which looks up `Popen` via that same shared module namespace -- so this
    patch is visible to every caller without needing to walk
    `sys.modules` for re-exported references.
    """

    def __init__(self) -> None:
        self.spawns = 0
        self._orig_popen = None

    def __enter__(self) -> "_SpawnCountCounter":
        self._orig_popen = subprocess.Popen
        orig = self._orig_popen

        def _counting_popen(*args, **kwargs):
            self.spawns += 1
            return orig(*args, **kwargs)

        subprocess.Popen = _counting_popen
        return self

    def __exit__(self, *exc) -> None:
        subprocess.Popen = self._orig_popen

    def measure(self, cmd: str) -> int:
        """Spawn count for ONE dispatch of `cmd`, starting from a COLD
        `resolve_git_root()` cache.

        makima is spawn-per-call with no resident daemon (CLAUDE.md), so in
        production every real dispatch starts cold -- a warm cache here
        would under-count and hide exactly the regressions this bound
        exists to catch (this was the second measurement mistake made this
        session: a warm cache silently absorbed a `resolve_git_root` spawn
        that a cold one would have charged).
        """
        self.spawns = 0
        _sandbox_engine.reset_resolve_git_root_cache()
        evaluate_payload_json(_payload(cmd))
        return self.spawns


@pytest.fixture(scope="module")
def amplification_profile() -> Dict[Tuple[str, int], float]:
    """One measurement pass over the whole corpus, shared by every row of both
    amplification tests -- parametrized rows give per-shape failure output
    without paying for a second dispatch of each shape.
    """
    profile: Dict[Tuple[str, int], float] = {}
    with _TokenizerWorkCounter() as counter:
        for name, build in _CORPUS.items():
            for size in (_SIZE_SMALL, _SIZE_LARGE):
                profile[(name, size)] = counter.measure(build(size))
        for index, cmd in enumerate(_REALISTIC):
            profile[("realistic_%d" % index, 0)] = counter.measure(cmd)
    return profile


@pytest.fixture(scope="module")
def wall_clock_profile() -> Dict[Tuple[str, int], float]:
    """Timings for the `cadence` leg. Module-scoped and lazily built, so the
    fast tier never pays for it -- only the `cadence`-marked tests request it.
    """
    profile: Dict[Tuple[str, int], float] = {}
    for name, build in _CORPUS.items():
        for size in _WALL_CLOCK_SIZES:
            profile[(name, size)] = _time_dispatch(build(size))
    for name in ("many_segments_git", "many_segments_benign", "historical_bash_c_no_whitespace"):
        profile[(name, _SIZE_OVER_CEILING)] = _time_dispatch(_CORPUS[name](_SIZE_OVER_CEILING))
    for index, cmd in enumerate(_REALISTIC):
        profile[("realistic_%d" % index, 0)] = _time_dispatch(cmd)
    return profile


@pytest.fixture(scope="module")
def spawn_count_profile() -> Dict[Tuple[str, int], int]:
    """One measurement pass over `_SPAWN_CORPUS`, shared by every row of
    `TestDispatchSubprocessSpawnCount`. Module-scoped like the other two
    profile fixtures, and, like them, deterministic: a spawn count is an
    integer produced by a fixed corpus against a fixed real repo, not a
    clock -- same appeal as `TestTokenizerWorkAmplification` staying in the
    fast tier (see module docstring), and why this leg is fast-tier too.
    """
    profile: Dict[Tuple[str, int], int] = {}
    with _SpawnCountCounter() as counter:
        for name, build in _SPAWN_CORPUS.items():
            for size in (_SIZE_SMALL, _SIZE_LARGE):
                profile[(name, size)] = counter.measure(build(size))
    return profile


@pytest.fixture(scope="module")
def rm_spawn_count_profile() -> Dict[Tuple[str, int], int]:
    """One measurement pass over `_RM_SPAWN_CORPUS`, sized by TARGET COUNT
    (`_RM_TARGET_COUNT_SMALL`/`_LARGE`), not character length -- see
    `_RM_SAME_DIR`/`_RM_MULTI_DIRS` module comment for why a char-length
    corpus would never see this defect move.
    """
    profile: Dict[Tuple[str, int], int] = {}
    with _SpawnCountCounter() as counter:
        for name, build in _RM_SPAWN_CORPUS.items():
            for count in (_RM_TARGET_COUNT_SMALL, _RM_TARGET_COUNT_LARGE):
                profile[(name, count)] = counter.measure(build(count))
    return profile


@pytest.fixture(scope="module")
def branch_spawn_count_profile() -> Dict[Tuple[str, int], int]:
    """One measurement pass over `_BRANCH_SPAWN_CORPUS`, sized by TOKEN
    COUNT (`_BRANCH_TOKEN_COUNT_SMALL`/`_LARGE`), same rationale as
    `rm_spawn_count_profile`.
    """
    profile: Dict[Tuple[str, int], int] = {}
    with _SpawnCountCounter() as counter:
        for name, build in _BRANCH_SPAWN_CORPUS.items():
            for count in (_BRANCH_TOKEN_COUNT_SMALL, _BRANCH_TOKEN_COUNT_LARGE):
                profile[(name, count)] = counter.measure(build(count))
    return profile


# ---------------------------------------------------------------------------
# Deterministic leg -- complexity, not milliseconds. Fast tier.
# ---------------------------------------------------------------------------


class TestTokenizerWorkAmplification:
    """The primary bound. No clock, no machine dependence, no flake surface."""

    @pytest.mark.parametrize("shape", sorted(_CORPUS))
    @pytest.mark.parametrize("size", (_SIZE_SMALL, _SIZE_LARGE))
    def test_amplification_within_bound(self, shape, size, amplification_profile):
        amplification = amplification_profile[(shape, size)]
        assert amplification <= _MAX_TOKENIZER_WORK_AMPLIFICATION, (
            "shape %r at %d chars hands %.1fx its own length to shlex "
            "(bound %.1fx). Some guard is re-tokenizing the whole command "
            "more than once per registered guard -- typically a full-command "
            "tokenize called from inside a per-segment loop, which is "
            "quadratic in command length on the PreToolUse hot path."
            % (shape, size, amplification, _MAX_TOKENIZER_WORK_AMPLIFICATION)
        )

    @pytest.mark.parametrize("shape", sorted(_CORPUS))
    def test_amplification_does_not_grow_with_input(self, shape, amplification_profile):
        small = amplification_profile[(shape, _SIZE_SMALL)]
        large = amplification_profile[(shape, _SIZE_LARGE)]
        growth = large / max(small, 1e-9)
        assert growth <= _MAX_AMPLIFICATION_GROWTH, (
            "shape %r amplifies %.1fx at %d chars but %.1fx at %d chars "
            "(growth %.1fx, bound %.1fx). Amplification growing with input "
            "length IS the super-linear signature: total tokenizer work is "
            "O(segments x n), not O(guards x n)."
            % (shape, small, _SIZE_SMALL, large, _SIZE_LARGE, growth, _MAX_AMPLIFICATION_GROWTH)
        )

    @pytest.mark.parametrize("index", range(len(_REALISTIC)))
    def test_realistic_commands_stay_within_bound(self, index, amplification_profile):
        amplification = amplification_profile[("realistic_%d" % index, 0)]
        assert amplification <= _MAX_TOKENIZER_WORK_AMPLIFICATION, (
            "ordinary command %r amplifies %.1fx (bound %.1fx)"
            % (_REALISTIC[index], amplification, _MAX_TOKENIZER_WORK_AMPLIFICATION)
        )


# ---------------------------------------------------------------------------
# Wall-clock backstop -- cadence tier only.
# ---------------------------------------------------------------------------


@pytest.mark.cadence
class TestDispatchWallClockCeiling:
    """Backstop for the cost classes amplification cannot see: catastrophic
    regex backtracking over the full command, and the past-the-tokenizer-
    ceiling regime where `shlex` never runs but every whole-command scan
    still does.

    `cadence`-marked because a stopwatch assertion in a tier that runs 12-way
    parallel measures scheduler contention as much as guard cost. The
    deterministic leg above is what gates per-commit.
    """

    @pytest.mark.parametrize("shape", sorted(_CORPUS))
    @pytest.mark.parametrize("size", _WALL_CLOCK_SIZES)
    def test_adversarial_shape_within_budget(self, shape, size, wall_clock_profile):
        elapsed = wall_clock_profile[(shape, size)]
        assert elapsed <= _MAX_DISPATCH_SECONDS_ADVERSARIAL, (
            "shape %r at %d chars took %.3f s through the dispatch chain "
            "(budget %.1f s). This latency is spent before the user's command "
            "runs, on every Bash call."
            % (shape, size, elapsed, _MAX_DISPATCH_SECONDS_ADVERSARIAL)
        )

    @pytest.mark.parametrize(
        "shape", ("many_segments_git", "many_segments_benign", "historical_bash_c_no_whitespace")
    )
    def test_past_tokenizer_ceiling_within_budget(self, shape, wall_clock_profile):
        elapsed = wall_clock_profile[(shape, _SIZE_OVER_CEILING)]
        assert elapsed <= _MAX_DISPATCH_SECONDS_ADVERSARIAL, (
            "shape %r at %d chars (past the %d-char tokenizer ceiling) took "
            "%.3f s (budget %.1f s). The ceiling stops shlex only -- "
            "whole-command regex scans and per-segment walks are not bounded "
            "by it and must be bounded here."
            % (
                shape,
                _SIZE_OVER_CEILING,
                _ct._MAX_TOKENIZABLE_COMMAND_CHARS,
                elapsed,
                _MAX_DISPATCH_SECONDS_ADVERSARIAL,
            )
        )

    @pytest.mark.parametrize("index", range(len(_REALISTIC)))
    def test_realistic_command_within_tight_budget(self, index, wall_clock_profile):
        elapsed = wall_clock_profile[("realistic_%d" % index, 0)]
        assert elapsed <= _MAX_DISPATCH_SECONDS_REALISTIC, (
            "ordinary command %r took %.3f s through the dispatch chain "
            "(budget %.1f s)"
            % (_REALISTIC[index], elapsed, _MAX_DISPATCH_SECONDS_REALISTIC)
        )


# ---------------------------------------------------------------------------
# Subprocess spawn-count leg -- deterministic, fast tier.
# ---------------------------------------------------------------------------


class TestDispatchSubprocessSpawnCount:
    """Per-dispatch subprocess-COUNT bound -- the axis this module's own
    NEGATIVE SPEC named as uncovered until 2026-08-05
    (`check_destructive_git_orphan` CHECK 1 spawned `rev-parse --verify`/
    `rev-list --count` once per segment of a chained `git reset --hard`,
    ~2 spawns per segment, ~1,778 spawns / 9.9 s at 16 KiB -- fixed by a
    per-call memo keyed on `(target, git_cwd)`; see that function's F0-b
    docstring).

    Like `TestTokenizerWorkAmplification`, a spawn count is an INTEGER --
    machine-independent, load-independent, no flake surface -- so this stays
    fast tier rather than `cadence`, same argument the module already makes
    for amplification over wall clock.

    Three measurement pitfalls this class exists to not repeat (each one
    tripped this session while deriving the bounds below):

    1. Patch `subprocess.Popen` ONLY. `subprocess.run`/`check_output`/`call`
       are implemented in terms of `Popen` -- patching several of them
       double-counts the same spawn. See `_SpawnCountCounter` docstring.
    2. Measure with a cwd that is a REAL git repo. In a non-repo cwd,
       `resolve_git_root()` fails and (deliberately) never caches the
       failure, so every guard that asks re-spawns -- inflating the count
       and measuring a non-repo artifact rather than the guard chain. See
       the `_REPO_ROOT` assertion at module load.
    3. Reset `engine.reset_resolve_git_root_cache()` before each measured
       dispatch. makima is spawn-per-call with no resident daemon, so
       production always starts cold; a warm cache under-counts and hides
       the exact regression this bound exists to catch. See
       `_SpawnCountCounter.measure`.
    """

    @pytest.mark.parametrize("shape", sorted(_SPAWN_CORPUS))
    @pytest.mark.parametrize("size", (_SIZE_SMALL, _SIZE_LARGE))
    def test_spawn_count_within_ceiling(self, shape, size, spawn_count_profile):
        spawns = spawn_count_profile[(shape, size)]
        assert spawns <= _MAX_DISPATCH_SUBPROCESS_SPAWNS, (
            "shape %r at %d chars spawned %d subprocesses through the "
            "dispatch chain (ceiling %d). Some guard is shelling out once "
            "per segment instead of once per distinct query -- typically a "
            "`_run_git` call inside a per-segment loop whose arguments do "
            "not vary per segment."
            % (shape, size, spawns, _MAX_DISPATCH_SUBPROCESS_SPAWNS)
        )

    @pytest.mark.parametrize("shape", sorted(_SPAWN_CORPUS))
    def test_spawn_count_does_not_grow_with_input(self, shape, spawn_count_profile):
        small = spawn_count_profile[(shape, _SIZE_SMALL)]
        large = spawn_count_profile[(shape, _SIZE_LARGE)]
        growth = large - small
        assert growth <= _MAX_SPAWN_COUNT_GROWTH_ABSOLUTE, (
            "shape %r spawns %d subprocesses at %d chars but %d at %d chars "
            "(growth %+d, absolute tolerance %d). Spawn count growing with "
            "command length -- not command SEGMENT count -- is the "
            "per-segment-shell-out signature this axis exists to catch: "
            "spawn count is allowed to be proportional to the number of "
            "registered guards, never to the number of segments in the "
            "command."
            % (shape, small, _SIZE_SMALL, large, _SIZE_LARGE, growth, _MAX_SPAWN_COUNT_GROWTH_ABSOLUTE)
        )


# ---------------------------------------------------------------------------
# Per-TARGET-count spawn leg -- check_destructive_rm, deterministic, fast
# tier.
# ---------------------------------------------------------------------------


class TestDispatchRmTargetSpawnCount:
    """Per-dispatch subprocess-COUNT bound for `check_destructive_rm`'s
    per-TARGET defect (F0-b, 2026-08-05, found the same session as the
    reset leg above): the scratch-allowlist/dirty-work root probes spawned
    `git -C <parent-dir> rev-parse --show-toplevel` once PER RM TARGET, with
    byte-identical argv whenever two targets share a parent directory (the
    everyday `rm a.py b.py c.py` shape) -- fixed by routing those probes
    through the same per-call `_new_git_memo` the reset leg uses.

    Sized by TARGET COUNT (`_RM_TARGET_COUNT_SMALL`/`_LARGE`), not character
    length -- unlike the tokenizer-work/reset axis, `rm a.py b.py c.py` is a
    few dozen characters regardless of how many targets it names, so a
    char-length corpus would never see this axis move. Real, git-tracked,
    on-disk `.py` files (see `_dir_py_files`): the defect only reproduces
    for EXISTING targets (an early false negative this session -- see
    `_RM_SAME_DIR` module comment).
    """

    @pytest.mark.parametrize("shape", sorted(_RM_SPAWN_CORPUS))
    @pytest.mark.parametrize("count", (_RM_TARGET_COUNT_SMALL, _RM_TARGET_COUNT_LARGE))
    def test_spawn_count_within_ceiling(self, shape, count, rm_spawn_count_profile):
        spawns = rm_spawn_count_profile[(shape, count)]
        assert spawns <= _MAX_RM_SPAWN_COUNT, (
            "shape %r at %d rm targets spawned %d subprocesses through the "
            "dispatch chain (ceiling %d). Some guard is shelling out once "
            "per rm TARGET instead of once per distinct directory."
            % (shape, count, spawns, _MAX_RM_SPAWN_COUNT)
        )

    @pytest.mark.parametrize("shape", sorted(_RM_SPAWN_CORPUS))
    def test_spawn_count_does_not_grow_with_target_count(self, shape, rm_spawn_count_profile):
        small = rm_spawn_count_profile[(shape, _RM_TARGET_COUNT_SMALL)]
        large = rm_spawn_count_profile[(shape, _RM_TARGET_COUNT_LARGE)]
        growth = large - small
        assert growth <= _MAX_RM_SPAWN_COUNT_GROWTH_ABSOLUTE, (
            "shape %r spawns %d subprocesses at %d targets but %d at %d "
            "targets (growth %+d, absolute tolerance %d). Spawn count "
            "growing with TARGET count is the per-target-shell-out "
            "signature this axis exists to catch."
            % (
                shape,
                small,
                _RM_TARGET_COUNT_SMALL,
                large,
                _RM_TARGET_COUNT_LARGE,
                growth,
                _MAX_RM_SPAWN_COUNT_GROWTH_ABSOLUTE,
            )
        )

    def test_multi_dir_targets_resolve_independently(self, rm_spawn_count_profile):
        """The complement to the ceiling/growth checks above -- see
        `_MIN_RM_MULTI_DIR_SPAWNS` docstring for why a ceiling alone cannot
        catch a memo that wrongly collapses DISTINCT directories into one
        resolution (fewer spawns always reads as "better" to a ceiling
        check).
        """
        spawns_small = rm_spawn_count_profile[("many_targets_rm_multi_dir", _RM_TARGET_COUNT_SMALL)]
        spawns_large = rm_spawn_count_profile[("many_targets_rm_multi_dir", _RM_TARGET_COUNT_LARGE)]
        floor = min(_MIN_RM_MULTI_DIR_SPAWNS, _rm_multi_dir_count())
        assert spawns_small >= floor and spawns_large >= floor, (
            "'many_targets_rm_multi_dir' spawned %d (small)/%d (large) "
            "subprocesses, below the floor of %d -- a memo that resolves "
            "fewer than one process per distinct directory used is "
            "wrongly collapsing DIFFERENT repos/dirs into the same cached "
            "answer, not just deduplicating identical calls."
            % (spawns_small, spawns_large, floor)
        )


# ---------------------------------------------------------------------------
# Per-TOKEN-count spawn leg -- check_destructive_git_orphan CHECK 3,
# deterministic, fast tier.
# ---------------------------------------------------------------------------


class TestDispatchBranchDeleteTokenSpawnCount:
    """Per-dispatch subprocess-COUNT bound for `check_destructive_git_
    orphan` CHECK 3's per-TOKEN defect (F0-c, 2026-08-05): `git branch -D
    <N tokens>` spawned `rev-parse --verify refs/heads/<name>` once PER
    BRANCH TOKEN, exactly 1 spawn per token, ~1,780 ms at 400 tokens --
    fixed by replacing N per-token existence probes with a single `git
    for-each-ref --format=%(refname:short) refs/heads/` enumeration and an
    in-memory membership test (`_local_branch_names`). Unlike the reset/rm
    legs, the exact-`(cwd, args)`-keyed memo CANNOT collapse this call
    (`br` genuinely varies per token), which is why this is a distinct fix
    shape and a distinct corpus/bound, not a reuse of the reset/rm ceiling.

    Sized by TOKEN COUNT (`_BRANCH_TOKEN_COUNT_SMALL`/`_LARGE`), same
    rationale as the rm leg. All target branch names are deliberately
    NONEXISTENT (unlike the rm leg's on-disk-existence requirement): this
    defect fires regardless of repo branch state, and one token is
    slash-named (`feature/x`-shaped) so a naive batching implementation
    that mishandles a namespaced ref fails this pin.
    """

    @pytest.mark.parametrize("shape", sorted(_BRANCH_SPAWN_CORPUS))
    @pytest.mark.parametrize("count", (_BRANCH_TOKEN_COUNT_SMALL, _BRANCH_TOKEN_COUNT_LARGE))
    def test_spawn_count_within_ceiling(self, shape, count, branch_spawn_count_profile):
        spawns = branch_spawn_count_profile[(shape, count)]
        assert spawns <= _MAX_BRANCH_SPAWN_COUNT, (
            "shape %r at %d branch tokens spawned %d subprocesses through "
            "the dispatch chain (ceiling %d). Some guard is shelling out "
            "once per branch TOKEN instead of enumerating local branches "
            "once."
            % (shape, count, spawns, _MAX_BRANCH_SPAWN_COUNT)
        )

    @pytest.mark.parametrize("shape", sorted(_BRANCH_SPAWN_CORPUS))
    def test_spawn_count_does_not_grow_with_token_count(self, shape, branch_spawn_count_profile):
        small = branch_spawn_count_profile[(shape, _BRANCH_TOKEN_COUNT_SMALL)]
        large = branch_spawn_count_profile[(shape, _BRANCH_TOKEN_COUNT_LARGE)]
        growth = large - small
        assert growth <= _MAX_BRANCH_SPAWN_COUNT_GROWTH_ABSOLUTE, (
            "shape %r spawns %d subprocesses at %d tokens but %d at %d "
            "tokens (growth %+d, absolute tolerance %d). Spawn count "
            "growing with TOKEN count is the per-token-shell-out signature "
            "this axis exists to catch."
            % (
                shape,
                small,
                _BRANCH_TOKEN_COUNT_SMALL,
                large,
                _BRANCH_TOKEN_COUNT_LARGE,
                growth,
                _MAX_BRANCH_SPAWN_COUNT_GROWTH_ABSOLUTE,
            )
        )
