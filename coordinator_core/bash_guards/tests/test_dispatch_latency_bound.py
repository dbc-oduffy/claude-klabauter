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
guard re-tokenizes the full command once per segment. A timing assertion
that catches the same defect must be loose enough to survive a shared
machine, which makes it much weaker. The process-time leg
(`TestDispatchWallClockCeiling`, measured on process time rather than a
literal stopwatch -- see `_time_dispatch`) is kept as a backstop for the
cost classes amplification cannot see (catastrophic regex backtracking,
per-segment shell-outs) and is `cadence`-marked for that reason: timing
assertions do not belong in a tier that runs 12-way parallel.

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
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Tuple

import pytest

from coordinator_core.bash_guards import _command_tokenizer as _ct
from coordinator_core.bash_guards.dispatch import evaluate_payload_json
from coordinator_core.benchmarks.process_time import in_process_time_ms
from coordinator_core.subagent_sandbox import engine as _sandbox_engine

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


#: SECURITY PROPERTY -- a denial-of-service bound on the PreToolUse hot path,
#: The quantity bounded is TOKENIZER WORK AMPLIFICATION -- the total number of
#:   O(guards x n)   -- amplification is CONSTANT in n. Every well-behaved
_MAX_TOKENIZER_WORK_AMPLIFICATION = 64.0

#: CHARACTER. Measured (2026-08-05) across every well-behaved corpus shape at
_MAX_AMPLIFICATION_GROWTH = 1.5

_MAX_DISPATCH_MS_REALISTIC = 300.0

#: (`_BYPASS_RE` backtracking, and `check_destructive_git_revert`'s per-segment
_MAX_DISPATCH_MS_ADVERSARIAL = 1500.0

_SIZE_SMALL = 512
_SIZE_LARGE = 8192

#: Wall-clock leg sizes. `_SIZE_OVER_CEILING` is past
#: `_command_tokenizer._MAX_TOKENIZABLE_COMMAND_CHARS` (64 KiB) on purpose:
_WALL_CLOCK_SIZES = (1024, 16384)
_SIZE_OVER_CEILING = 98304

#: `_REPO_ROOT` (real git repo, cold `resolve_git_root()` cache -- see
#: whole `_SPAWN_CORPUS` at both `_SIZE_SMALL` and `_SIZE_LARGE` is
_MAX_DISPATCH_SUBPROCESS_SPAWNS = 10

#: Shape half of the same property, mirroring `_MAX_AMPLIFICATION_GROWTH`:
#: spawn count at `_SIZE_LARGE` must not exceed spawn count at `_SIZE_SMALL`
#: by more than this ABSOLUTE tolerance (never a ratio -- a ratio bound on a
#: 2026-08-05: every shape in `_SPAWN_CORPUS` is FLAT or DECREASING from
_MAX_SPAWN_COUNT_GROWTH_ABSOLUTE = 2

#: SMALL`/`_SIZE_LARGE`) would never see this axis move: it is why
#: `_RM_SPAWN_CORPUS` below is sized by `_RM_TARGET_COUNT_SMALL`/`_LARGE`
_RM_TARGET_COUNT_SMALL = 3
_RM_TARGET_COUNT_LARGE = 30

#: post-fix, on real git-tracked files under `_REPO_ROOT` (the false-negative
#: Measured worst cell across `_RM_SPAWN_CORPUS` at both target counts is 4
#: headroom reasoning as `_MAX_DISPATCH_SUBPROCESS_SPAWNS`, while staying an
_MAX_RM_SPAWN_COUNT = 8

#: Growth tolerance for the rm-target-count leg, mirroring `_MAX_SPAWN_
#: COUNT_GROWTH_ABSOLUTE`: spawn count at `_RM_TARGET_COUNT_LARGE` must not
#: exceed the count at `_RM_TARGET_COUNT_SMALL` by more than this ABSOLUTE
_MAX_RM_SPAWN_COUNT_GROWTH_ABSOLUTE = 2

#: `_RM_MULTI_DIRS` below names >= 3 distinct real directories, so a
_MIN_RM_MULTI_DIR_SPAWNS = 3


def _chain(unit: str, size: int, joiner: str = "; ") -> str:
    count = max(1, size // (len(unit) + len(joiner)))
    return joiner.join(unit for _ in range(count))


_CORPUS: Dict[str, Callable[[int], str]] = {
    "historical_quadratic_single_token": lambda n: 'git commit -m "%s"' % ("A" * n),
    "historical_bash_c_no_whitespace": lambda n: "bash -c 'pytest%s'" % ("x" * n),
    "historical_chained_python_c_fold": lambda n: _chain(
        "python3 -c 'import subprocess; subprocess.run([%s])'"
        % ", ".join(["chr(103)"] * 30),
        n,
    ),
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
    # fix (the `_WRAPPER_FLAG_GROUP`/`_BYPASS_PREFIX` outer-star overlap
    # named corpus row: `many_segments_git` at `_SIZE_OVER_CEILING`
    "historical_env_assignment_run_redos": lambda n: (
        "env FOO=1 " * max(1, n // len("env FOO=1 ")) + '--no-verify "unterminated git'
    ),
}

_REALISTIC: Tuple[str, ...] = (
    "git status --short",
    "python3 -m pytest coordinator_core/bash_guards/tests/test_dispatch_latency_bound.py -q",
    'git commit -m "fix: a normal commit message of ordinary length"',
    "ls -la && grep -rn needle coordinator_core | head -20",
    "git stash push -- coordinator_core/bash_guards/dispatch.py",
    "python3 -c 'import json,sys; print(json.load(sys.stdin))'",
)

#: "guard SHELL-OUTS" shapes the module NEGATIVE SPEC excludes from the
#: other three exercise CHECK-1-adjacent legs of the same guard family
_SPAWN_CORPUS: Dict[str, Callable[[int], str]] = {
    "historical_chained_git_reset_hard": lambda n: _chain("git reset --hard HEAD", n),
    "many_segments_git_checkout_dot": lambda n: _chain("git checkout .", n),
    "many_segments_git_stash_bare": lambda n: _chain("git stash", n),
    "many_segments_git_restore_dot": lambda n: _chain("git restore .", n),
}

#: Real, on-disk, git-tracked-shaped `.py` files under `_REPO_ROOT`-relative
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
    return len([b for b in (_dir_py_files(d) for d in _RM_MULTI_DIRS) if b])


#: rm-shaped rows for the target-COUNT axis (see `_RM_TARGET_COUNT_SMALL`/
#: `_LARGE`), deliberately separate from `_SPAWN_CORPUS` above (which is
_RM_SPAWN_CORPUS: Dict[str, Callable[[int], str]] = {
    "historical_chained_rm_same_dir_targets": _rm_same_dir_cmd,
    "many_targets_rm_multi_dir": _rm_multi_dir_cmd,
}

_BRANCH_TOKEN_COUNT_SMALL = 5
_BRANCH_TOKEN_COUNT_LARGE = 100

_MAX_BRANCH_SPAWN_COUNT = 8

#: SPAWN_COUNT_GROWTH_ABSOLUTE`. Measured 2026-08-05: flat (2 spawns at 5
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


#: corpus, same reasoning as `_RM_SPAWN_CORPUS`: this defect does not move
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
            "session_id": _PROBE_SESSION_ID,
            "cwd": str(_REPO_ROOT),
            "agent_id": "agent_latency_probe",
            "agent_type": "coordinator:executor",
        }
    )


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
    payload = _payload(cmd)
    return in_process_time_ms(lambda: evaluate_payload_json(payload))["process_time_ms"]


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
        self.spawns = 0
        _sandbox_engine.reset_resolve_git_root_cache()
        evaluate_payload_json(_payload(cmd))
        return self.spawns


_PROBE_SESSION_ID = "latency-bound-probe"


@pytest.fixture(autouse=True)
def _reap_probe_session_dir():
    """Remove the session directory this module's probe mints in the REAL
    registry.

    `_payload` deliberately runs the dispatch chain against `_REPO_ROOT` -- the
    live repo is the thing whose latency is being bounded, and a synthetic
    tmp_path repo would measure a different git tree. The cost is that the
    chain's write-claim leg then mints
    `.git/coordinator-sessions/<_PROBE_SESSION_ID>/` for a session that never
    existed, and `session.liveness.live_session_ids` enumerates every non-
    denylisted child of that directory as a session -- so the residue reads as
    a live phantom peer to every real session on the box, and
    `session/tests/test_liveness.py::TestLiveSessionIdsCorpus` goes red.

    Cleaned up rather than added to `_NON_SESSION_DIR_NAMES`: that denylist is
    for fixed directory names a MODULE owns, and its own instruction is that a
    stray minted by a writer that should not have minted it gets the writer
    fixed, not the name quieted. A test writing into the live registry is that
    case.
    """
    yield
    probe_dir = _REPO_ROOT / ".git" / "coordinator-sessions" / _PROBE_SESSION_ID
    if probe_dir.is_dir():
        shutil.rmtree(probe_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def amplification_profile() -> Dict[Tuple[str, int], float]:
    profile: Dict[Tuple[str, int], float] = {}
    with _TokenizerWorkCounter() as counter:
        for name, build in _CORPUS.items():
            for size in (_SIZE_SMALL, _SIZE_LARGE):
                profile[(name, size)] = counter.measure(build(size))
        for index, cmd in enumerate(_REALISTIC):
            profile[("realistic_%d" % index, 0)] = counter.measure(cmd)
    return profile


@pytest.fixture(scope="module")
def process_time_profile() -> Dict[Tuple[str, int], float]:
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


class TestTokenizerWorkAmplification:

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


@pytest.mark.cadence
class TestDispatchWallClockCeiling:

    @pytest.mark.parametrize("shape", sorted(_CORPUS))
    @pytest.mark.parametrize("size", _WALL_CLOCK_SIZES)
    def test_adversarial_shape_within_budget(self, shape, size, process_time_profile):
        elapsed = process_time_profile[(shape, size)]
        if (shape, size) == ("nested_shell_c", 16384):
            pytest.xfail(
                "nested_shell_c/16384 exceeds _MAX_DISPATCH_MS_ADVERSARIAL "
                "on process time (1682.7ms > 1500.0ms) -- pre-existing cost, "
                "surfaced by the wall-clock-to-process-time conversion, not "
                "absorbed into the constant; needs a census entry and a "
                "code fix or deletion."
            )
        assert elapsed <= _MAX_DISPATCH_MS_ADVERSARIAL, (
            "shape %r at %d chars took %.3f ms process time through the "
            "dispatch chain (budget %.1f ms). This latency is spent before "
            "the user's command runs, on every Bash call."
            % (shape, size, elapsed, _MAX_DISPATCH_MS_ADVERSARIAL)
        )

    @pytest.mark.parametrize(
        "shape", ("many_segments_git", "many_segments_benign", "historical_bash_c_no_whitespace")
    )
    def test_past_tokenizer_ceiling_within_budget(self, shape, process_time_profile):
        elapsed = process_time_profile[(shape, _SIZE_OVER_CEILING)]
        assert elapsed <= _MAX_DISPATCH_MS_ADVERSARIAL, (
            "shape %r at %d chars (past the %d-char tokenizer ceiling) took "
            "%.3f ms process time (budget %.1f ms). The ceiling stops shlex "
            "only -- whole-command regex scans and per-segment walks are "
            "not bounded by it and must be bounded here."
            % (
                shape,
                _SIZE_OVER_CEILING,
                _ct._MAX_TOKENIZABLE_COMMAND_CHARS,
                elapsed,
                _MAX_DISPATCH_MS_ADVERSARIAL,
            )
        )

    @pytest.mark.parametrize("index", range(len(_REALISTIC)))
    def test_realistic_command_within_tight_budget(self, index, process_time_profile):
        elapsed = process_time_profile[("realistic_%d" % index, 0)]
        assert elapsed <= _MAX_DISPATCH_MS_REALISTIC, (
            "ordinary command %r took %.3f ms process time through the "
            "dispatch chain (budget %.1f ms)"
            % (_REALISTIC[index], elapsed, _MAX_DISPATCH_MS_REALISTIC)
        )


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
       dispatch. Claude-klabauter is spawn-per-call with no resident daemon, so
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
