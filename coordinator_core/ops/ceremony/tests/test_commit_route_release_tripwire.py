"""Tripwire: every `git commit` argv-construction site in `coordinator_core/`
must be accounted for by `release_committed_claims` or a stated ineligibility
reason.

RAG-bait / spec backlink: docs/plans/2026-08-11-claim-release-and-the-gate-
that-cannot-clear.md, chunk C3 ("A1's forward-going half"). C3a wires
`release_committed_claims` into each eligible commit route TODAY. That fixes
today's route set and nothing else -- the tenth route someone adds next
month is unguarded unless something MECHANICALLY notices a new commit site
appears. This module is that something: it is the artifact, not a wrapper,
that the plan calls for ("a tripwire test that enumerates `commit` argv
construction across `coordinator_core` and fails when a new commit site
appears outside a sanctioned, reasoned allowlist").

Negative spec: this test does NOT verify that an eligible site actually
CALLS `release_committed_claims` -- proving the call happened is C3a's own
test's job, at the site. This test only proves the site is *known* and
*classified*. A classification of `"release"` here is a claim staked by the
allowlist author, not something this module independently checks against
runtime behavior -- that is a deliberate scope cut (see AST-vs-behavior
tradeoff note below).

AST-vs-grep tradeoff: chosen AST over grep. Grep on the string `"commit"`
would false-positive on the word appearing in log messages, docstrings, and
unrelated git subcommands (`git commit-tree` is not a plain commit; a grep
for `commit` inside a docstring reading "already committed" is noise this
module would otherwise have to hand-filter with regex heuristics forever).
AST-shaped detection walks actual `ast.Call` nodes, requires the literal
string `"commit"` to appear as one of that call's own argument constants
(not merely somewhere nearby in the file), and needs zero `git`/subprocess
invocation to do it -- this module parses source text only, per the "must
not spawn git" hard constraint. The cost is that a commit argv built via
values only known at runtime (e.g. `subcommand_var = "commit"; _git([subcommand_var, ...])`)
would slip past a naive AST walk the same way it would slip past grep; no
site currently pinned in this file's allowlist does that, and a reviewer
should treat a runtime-only-assembled subcommand as a code-smell independent
of this test.

Runtime tradeoff (EM-ratified, 2026-08-11 chunk C3b follow-up): the brief
asked for "well under a second". After `os.walk` directory pruning and a
byte-level substring pre-filter (see `_iter_tracked_calls`), the full
coordinator_core/ walk (~930 non-test .py files) still measured 0.9-2.4s
across repeated runs on this box, which runs 50-70 concurrent LLM sessions
as a matter of course -- ambient load, not this walk's algorithmic cost,
dominates that range. Three of the four sites this enumerator found beyond
the brief's own pinned seed list (`backlog_grind_assemble/apply.py`,
`ops/workday_complete_step2_5_dirty_tree.py`'s two sites) live in
directories a narrower, faster walk would have excluded -- exactly the
"new route in an unexpected place" coverage this module exists for. Full-
tree coverage was chosen over a sub-second runtime; this module asserts a
load-invariant file-count sanity bound instead of a wall-clock one (see
`test_enumerator_walks_a_sane_file_count_with_no_subprocess`), since a
timing assertion is a flake generator at this concurrency and count holds
still where wall-clock does not.

Correction to the plan's own problem statement: the brief (and the plan
text it was drawn from) name `ops/fleet/memo_send.py::_commit_ledger_once`
as the settled-ineligible receiver-repo commit site. That function does not
exist at HEAD -- the actual site is `_commit_delivered_memo` (see
ALLOWLIST below). Same site, same settled reason; only the name was wrong.
Recorded here so a future pass does not "fix" this allowlist key back to
the plan's stale name.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

# coordinator_core/ops/ceremony/tests/ -> parents[3] is coordinator_core/
CORE_ROOT = Path(__file__).resolve().parents[3]

# Directories under coordinator_core/ this walk deliberately excludes: test
# modules construct `["commit", ...]` argv lists constantly as fixtures/
# expected-call assertions, which would swamp the real production-site
# enumeration with noise this test has no way to distinguish from a real
# site. Scoped-out and named per the runtime hard-constraint ("scope the
# walk and say what you scoped out").
_EXCLUDED_DIR_SEGMENTS = {"tests", "__pycache__"}

# The four unrelated commit-argv-construction mechanisms this enumerator
# must independently catch -- see MECHANISM_EXPECTED_NONEMPTY below, which
# asserts each bucket is non-empty so a change that silently narrows the
# walk to only one shape (a false-green) fails loudly.
_ASYNCIO_EXEC_NAMES = {"create_subprocess_exec"}
_RUN_GIT_HELPER_NAMES = {"_run_git"}
_GIT_NATIVE_UNDERSCORE_GIT = {"_git"}
_COMMIT_SCOPED_NAMES = {"commit_scoped"}

_ALL_TRACKED_CALLEE_NAMES = (
    _ASYNCIO_EXEC_NAMES
    | _RUN_GIT_HELPER_NAMES
    | _GIT_NATIVE_UNDERSCORE_GIT
    | _COMMIT_SCOPED_NAMES
)


def _callee_name(node: ast.Call) -> str | None:
    """Return the plain name of a call's callee, attribute access included.

    `asyncio.create_subprocess_exec(...)` and a bare `create_subprocess_exec(...)`
    both resolve to `"create_subprocess_exec"`; `git_native._git(...)` and a
    bare `_git(...)` both resolve to `"_git"`.
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _string_constants(node: ast.AST):
    """Yield every string literal reachable from `node`'s own argument tree.

    Recurses into list/tuple literals (`["commit", "-m", ...]` is the
    dominant shape) so `"commit"` is found wherever it sits in the call's
    own arguments -- not merely as the first positional arg.
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            yield child.value


def _iter_py_files():
    """Walk coordinator_core/, pruning excluded directories in-place (not a
    post-hoc filter over `rglob`) -- `rglob` still opens and stats every
    entry under a pruned directory before a filter can reject it, which is
    the dominant cost the runtime budget test caught. `os.walk`'s `dirnames`
    mutation skips the whole subtree at the OS-call level instead.
    """
    for dirpath, dirnames, filenames in os.walk(CORE_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIR_SEGMENTS]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            # Test modules are not confined to a `tests/` directory in this
            # tree (e.g. `ops/test_archive_stamp.py` sits directly in
            # `ops/`) -- exclude by the `test_`-prefix filename convention
            # this repo uses everywhere, not just by directory segment.
            # conftest.py is a test fixture file by the same convention.
            if filename.startswith("test_") or filename == "conftest.py":
                continue
            yield Path(dirpath) / filename


class _EnclosingFunctionTracker(ast.NodeVisitor):
    """Walks one module's AST, yielding (function_name, ast.Call) for every
    tracked-mechanism call, tagged with its nearest enclosing def/async def
    (module-level calls report as "<module>").
    """

    def __init__(self):
        self.stack: list[str] = ["<module>"]
        self.found: list[tuple[str, str, ast.Call]] = []  # (func_name, mechanism, call)

    def _visit_func(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node):  # noqa: N802 - ast visitor naming
        self._visit_func(node)

    def visit_AsyncFunctionDef(self, node):  # noqa: N802
        self._visit_func(node)

    def visit_Call(self, node: ast.Call):  # noqa: N802
        name = _callee_name(node)
        if name in _ALL_TRACKED_CALLEE_NAMES:
            if name in _COMMIT_SCOPED_NAMES:
                self.found.append((self.stack[-1], "commit_scoped", node))
            elif "commit" in set(_string_constants(node)):
                if name in _ASYNCIO_EXEC_NAMES:
                    mechanism = "asyncio_create_subprocess_exec"
                elif name in _RUN_GIT_HELPER_NAMES:
                    mechanism = "_run_git_helper"
                else:
                    mechanism = "git_native._git"
                self.found.append((self.stack[-1], mechanism, node))
        self.generic_visit(node)


def _iter_tracked_calls():
    """Yield (relpath, func_name, mechanism) for every tracked-mechanism
    call found across coordinator_core/ (test modules excluded).

    Shared by `_enumerate_commit_sites` (the allowlist-comparison view) and
    `test_enumerator_catches_all_four_mechanisms` (the per-mechanism view)
    so the walk -- and its runtime-budget-driven pre-filter -- exists once.
    """
    for path in _iter_py_files():
        # Cheap substring pre-filter before the ~900-file whole-tree walk
        # pays for a full decode + ast.parse: a file mentioning none of the
        # four tracked callee names cannot contain a tracked call, full
        # stop. Read+search as raw bytes first (utf-8 decode is real cost
        # across ~900 files -- measured decode+parse at 1.8-2.4s, decode
        # alone at ~1.0s) and only decode the small minority that match.
        # Safe because it is a superset filter (never excludes a file that
        # DOES contain a call worth walking), not a semantic shortcut.
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if not any(name.encode("ascii") in raw for name in _ALL_TRACKED_CALLEE_NAMES):
            continue
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        tracker = _EnclosingFunctionTracker()
        tracker.visit(tree)
        if not tracker.found:
            continue
        rel = path.relative_to(CORE_ROOT).as_posix()
        for func_name, mechanism, _call in tracker.found:
            yield rel, func_name, mechanism


def _enumerate_commit_sites():
    """Return {"module::function": mechanism} for every commit-argv site
    under coordinator_core/, excluding test modules (see _EXCLUDED_DIR_SEGMENTS).
    """
    sites: dict[str, str] = {}
    for rel, func_name, mechanism in _iter_tracked_calls():
        key = f"{rel}::{func_name}"
        # A function containing more than one commit call (e.g. three
        # sequential handoff/notes commits in one _handler) collapses to
        # one site -- the allowlist is keyed per function, not per call.
        sites.setdefault(key, mechanism)
    return sites


# ---------------------------------------------------------------------------
# The allowlist. Seeded from the plan's pinned site set (chunk C3b brief),
# keyed `module::function` (module path relative to coordinator_core/,
# forward-slash, no .py-stripping ambiguity since the extension stays on).
#
# reason is one of:
#   "release"      -- this site is expected to call release_committed_claims
#                      (C3a is wiring these; see per-entry "confirmed" note)
#   "ineligible: <why>" -- explicit, settled non-release reason
#
# "confirmed" tags whether this executor could verify the release call
# actually landed at this site by reading C3a's output -- C3a is a
# concurrently-running peer chunk this executor was instructed NOT to read
# mid-flight (coordination hazard named in the brief). False here means
# "classification asserted per the brief's seed list, not independently
# verified against C3a's landed edits" -- the EM reconciles at the wave
# boundary, not this test.
# ---------------------------------------------------------------------------
ALLOWLIST: dict[str, dict[str, object]] = {
    # Brief's seed name was "_commit_ledger_once" -- at HEAD the enclosing
    # function is "_commit_delivered_memo" (function renamed/refactored since
    # the brief was authored, or the brief's name was informal). Same site,
    # same settled reason; corrected function name so the enumerator's
    # actual output matches.
    "ops/fleet/memo_send.py::_commit_delivered_memo": {
        "reason": "ineligible: commits into the RECEIVER's repo -- releasing "
        "the local sid's claims against a peer worktree is meaningless",
        "confirmed": True,  # settled ineligible per plan, not a C3a open question
    },
    "ops/ceremony/commit_exec_bit.py::_handler": {
        "reason": "ineligible: commits with no pathspec at all, so a release "
        "keyed off \"what this commit covered\" has no bounded answer",
        "confirmed": True,  # settled ineligible per plan, not a C3a open question
    },
    "ops/fleet/_common.py::archive_and_commit": {
        "reason": "release",
        "confirmed": False,
    },
    "ops/fleet/_common.py::rm_and_commit": {
        "reason": "release",
        "confirmed": False,
    },
    "ops/session/boot_sweep.py::_commit_consumed_metadata": {
        "reason": "release",
        "confirmed": False,
    },
    "ops/distill_apply_disposal.py::_delete_tracked_and_append_log": {
        "reason": "release",
        "confirmed": False,
    },
    "ops/ceremony/detached_render_commit.py::commit_own_artifact": {
        "reason": "release",
        "confirmed": False,
    },
    "ops/ceremony/git_native.py::commit_with_message_file": {
        "reason": "release",
        "confirmed": False,
    },
    # The pathspec-file sibling of the entry above -- same commit, same
    # release eligibility, differing only in how the pathspec reaches git
    # (argv vs `--pathspec-from-file`, for the Windows 32767-char cap). Both
    # have existed since the pathspec-file split; only the argv one was ever
    # listed, so this tripwire has been red on the pair for as long as the
    # split has existed. Listed with the sibling's disposition, not a new one.
    "ops/ceremony/git_native.py::commit_with_message_file_pathspec_scoped": {
        "reason": "release",
        "confirmed": False,
    },
    # Found red alongside the entry above: a real coordinator op route (the
    # deliverable cascade's own scoped follow-up commit, routed through
    # `commit_scoped` exactly as `post_commit_tail`/`consumed_handoff_stamp`
    # are), so "release" pending the same classification those carry -- not
    # an auto-ineligible.
    "ops/deliverable_cascade.py::_commit_mutated_paths": {
        "reason": "release",
        "confirmed": False,
    },
    "ops/ceremony/commit_pipeline.py::commit": {
        "reason": "release",
        "confirmed": False,
    },
    "ops/ceremony/consumed_handoff_stamp.py::_commit_and_push_follow_up": {
        "reason": "release",
        "confirmed": True,  # C3d: release_committed_claims wired at this
        # site (session_id threaded through from post_commit_stamp_and_ship's
        # own required param), covered by
        # test_consumed_handoff_stamp_claim_release.py.
    },
    "ops/ceremony/post_commit_tail.py::_commit_and_push_origin_stub_close": {
        "reason": "release",
        "confirmed": True,  # C3d: release_committed_claims wired at this
        # site (sid threaded through run() -> _run_origin_stub_close ->
        # _to_thread_commit_and_push), covered by
        # test_post_commit_tail_claim_release.py.
    },
    # Found by the enumerator, NOT in the brief's pinned seed list -- this is
    # exactly the "tenth route" hazard the tripwire exists to catch. A real
    # coordinator op route (backlog_grind_assemble apply directives), so
    # "release" pending C3a/EM classification, not an auto-ineligible.
    "backlog_grind_assemble/apply.py::_commit_one": {
        "reason": "release",
        "confirmed": False,
    },
    # Also found by the enumerator, not in the brief's seed list. Benchmark
    # harness scaffolding that builds a disposable throwaway repo to measure
    # op performance against -- no real session claims exist against a
    # fixture repo for release_committed_claims to act on.
    "benchmarks/op_fixtures.py::materialize_fixture_repo": {
        "reason": "ineligible: builds a disposable benchmark-harness fixture "
        "repo, not a live coordinator worktree -- no session claims exist "
        "to release",
        "confirmed": True,
    },
    # Also found by the enumerator, not in the brief's seed list -- another
    # instance of the hazard this tripwire exists for.
    "ops/workday_complete_step2_5_dirty_tree.py::_act_commit": {
        "reason": "release",
        "confirmed": False,
    },
    "ops/workday_complete_step2_5_dirty_tree.py::_act_gitignore": {
        "reason": "release",
        "confirmed": False,
    },
}


def test_enumerator_walks_a_sane_file_count_with_no_subprocess():
    """Load-invariant sanity guard, NOT a wall-clock one.

    EM ruling (2026-08-11, chunk C3b follow-up): a self-timing assertion is
    a flake generator on a box running 50-70 concurrent sessions -- file-read
    latency scales with ambient load, not with this walk's algorithmic cost,
    so wall-clock is noise here and count is the invariant that actually
    holds still. See this module's own docstring for the measured runtime
    range and the explicit full-tree-coverage-over-runtime tradeoff; this
    test only guards against a genuine structural blow-up (an accidental
    O(n^2) re-walk, a walk that starts recursing into `.git/` or a venv, or
    a walk that starts shelling out) rather than asserting a bound that
    degrades to a coin-flip under load.
    """
    file_count = sum(1 for _ in _iter_py_files())
    # ~930 non-test .py files under coordinator_core/ at authoring time.
    # Generous headroom for ordinary repo growth; an order-of-magnitude jump
    # means the walk started recursing somewhere it should have pruned
    # (.git/, a venv, __pycache__ escaping the exclusion), not that the repo
    # organically grew that much.
    assert 100 < file_count < 5000, (
        f"walked {file_count} non-test .py files under coordinator_core/ -- "
        "expected roughly a few hundred to low thousands; investigate "
        "whether the walk started recursing into something it should prune "
        "(.git/, a venv, __pycache__) rather than raising this bound."
    )

    import subprocess
    import unittest.mock as mock

    with mock.patch("subprocess.run", side_effect=AssertionError(
        "enumerator must not spawn git -- it reads source, not history"
    )), mock.patch("subprocess.Popen", side_effect=AssertionError(
        "enumerator must not spawn git -- it reads source, not history"
    )):
        _enumerate_commit_sites()


def test_enumerator_catches_all_four_mechanisms():
    """False-green guard named in the brief: an enumerator that only finds
    one shape of commit-argv construction is worse than no enumerator, since
    it reads as coverage it does not have. Assert each of the four unrelated
    mechanisms independently contributes at least one site.
    """
    sites = _enumerate_commit_sites()
    by_mechanism: dict[str, list[str]] = {}
    for rel, func_name, mechanism in _iter_tracked_calls():
        by_mechanism.setdefault(mechanism, []).append(f"{rel}::{func_name}")

    expected_mechanisms = {
        "asyncio_create_subprocess_exec",
        "_run_git_helper",
        "git_native._git",
        "commit_scoped",
    }
    missing = expected_mechanisms - set(by_mechanism)
    assert not missing, (
        f"enumerator found zero sites for mechanism(s) {missing!r} -- this "
        "is a false-green: the walk is no longer catching all four "
        "unrelated commit-argv shapes the plan requires it to catch."
    )
    for mechanism in expected_mechanisms:
        assert by_mechanism[mechanism], f"mechanism {mechanism!r} had no sites"

    assert sites, "enumerator found no commit sites at all -- walk is broken"


def test_no_unlisted_commit_site():
    """AC: every enumerated commit-argv site must be in ALLOWLIST. An
    unlisted site fails loudly with the exact remediation: add the release
    call (site is now `release_committed_claims`-covered, so mark it
    "release"), or add an allowlist entry with a stated ineligibility
    reason.
    """
    sites = _enumerate_commit_sites()
    unlisted = sorted(set(sites) - set(ALLOWLIST))
    assert not unlisted, (
        "New git-commit argv construction site(s) found outside the "
        "allowlist in coordinator_core/ops/ceremony/tests/"
        "test_commit_route_release_tripwire.py:\n  "
        + "\n  ".join(unlisted)
        + "\n\nFor each: either wire a `release_committed_claims` call at "
        "the site and add it to ALLOWLIST with reason \"release\", or add "
        "it to ALLOWLIST with an explicit `\"ineligible: <why>\"` reason. "
        "A commit site is never silently unaccounted for."
    )


def test_no_stale_allowlist_entry():
    """The inverse failure: an allowlist entry naming a site that no longer
    exists silently shrinks real coverage (the entry looks like it is still
    doing work, but the function it names was renamed/removed/merged away).
    """
    sites = _enumerate_commit_sites()
    stale = sorted(set(ALLOWLIST) - set(sites))
    assert not stale, (
        "Stale allowlist entries name commit sites that no longer exist "
        "(renamed, removed, or merged away) -- remove them or fix the key:\n  "
        + "\n  ".join(stale)
    )


def test_every_allowlist_entry_has_a_reason():
    """The reason is data, not a comment: every entry must carry a non-empty
    `reason` string, and it must literally be `"release"` or start with
    `"ineligible:"` -- a free-text reason that satisfies neither shape is a
    classification nobody actually made.
    """
    for key, entry in ALLOWLIST.items():
        reason = entry.get("reason")
        assert isinstance(reason, str) and reason, f"{key} has no reason"
        assert reason == "release" or reason.startswith("ineligible:"), (
            f"{key} reason {reason!r} is neither \"release\" nor an "
            "\"ineligible: <why>\" string"
        )


@pytest.mark.parametrize(
    "key",
    sorted(k for k, v in ALLOWLIST.items() if not v.get("confirmed", False)),
)
def test_unconfirmed_entries_are_visibly_tracked(key):
    """Non-failing tripwire: every entry this executor could not confirm
    against C3a's concurrently-landed classification is parametrized here so
    it shows up by NAME in test output, not buried in a docstring, for the
    EM's wave-boundary reconciliation pass. Always passes -- it exists to be
    visible in `pytest -v` output, not to gate.
    """
    assert key in ALLOWLIST
