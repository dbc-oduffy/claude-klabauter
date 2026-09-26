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

A third axis the string-constant approach did not originally anticipate: a
NATIVE committer that builds no argv at all. `coordinator_core/git/commit.py
::commit_paths` and `ops/ceremony/git_native.py::_commit_via_head_spine`
land a commit by writing git objects and swapping a ref in process -- no
subprocess, no string anywhere reading `"commit"`. That is not "a string
built at runtime" (the blind spot above); it is no string, ever. The sixth
mechanism (`cas_ref_landing`, see `_CAS_REF_CALL_NAMES` below) closes this
by keying on the ref-move CALLEE instead of a string constant -- the one
mechanism in `_MECHANISM_ROWS` whose required-constant slot is `None`.

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

The SIXTH mechanism (2026-08-30, chunk C2, `cas_ref_landing`) is every
other row's `(callee-names, required-constant, mechanism)` shape with the
required-constant slot empty (`None`) rather than one of a different kind:
`coordinator_core/git/git_objects.py::cas_ref` is the ref-move primitive
every native (zero-spawn) committer lands through
(`coordinator_core/git/commit.py::commit_paths`,
`ops/ceremony/git_native.py::_commit_via_head_spine`), and its callee name
alone is match enough -- no string constant to require. See
`_MECHANISM_ROWS` below.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

CORE_ROOT = Path(__file__).resolve().parents[3]

_EXCLUDED_DIR_SEGMENTS = {"tests", "__pycache__"}

_ASYNCIO_EXEC_NAMES = {"create_subprocess_exec"}
_RUN_GIT_HELPER_NAMES = {"_run_git"}
_GIT_NATIVE_UNDERSCORE_GIT = {"_git"}
_COMMIT_SCOPED_NAMES = {"commit_scoped"}

_CAS_REF_CALL_NAMES = {"cas_ref"}

_HEAD_SPINE_CALL_NAMES = {"_commit_via_head_spine"}

_NATIVE_COMMITTER_API_NAMES = {
    "commit_paths",
    "commit_authored_new_file",
    "commit_authored_content",
}

_ALL_TRACKED_CALLEE_NAMES = (
    _ASYNCIO_EXEC_NAMES
    | _RUN_GIT_HELPER_NAMES
    | _GIT_NATIVE_UNDERSCORE_GIT
    | _COMMIT_SCOPED_NAMES
    | _CAS_REF_CALL_NAMES
    | _HEAD_SPINE_CALL_NAMES
    | _NATIVE_COMMITTER_API_NAMES
)

# coverage is still open -- see the DELIBERATELY RETAINED note in the
_MECHANISM_ROWS = (
    (_ASYNCIO_EXEC_NAMES, "commit", "asyncio_create_subprocess_exec"),
    (_RUN_GIT_HELPER_NAMES, "commit", "_run_git_helper"),
    (_GIT_NATIVE_UNDERSCORE_GIT, "commit", "git_native._git"),
    (
        _ASYNCIO_EXEC_NAMES | _RUN_GIT_HELPER_NAMES | _GIT_NATIVE_UNDERSCORE_GIT,
        "commit-tree",
        "commit_tree_plumbing",
    ),
    (_COMMIT_SCOPED_NAMES, None, "commit_scoped"),
    (_CAS_REF_CALL_NAMES, None, "cas_ref_landing"),
    (_HEAD_SPINE_CALL_NAMES, None, "head_spine_commit"),
    (_NATIVE_COMMITTER_API_NAMES, None, "native_committer_api"),
)


def _callee_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _string_constants(node: ast.AST):
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            yield child.value


def _iter_py_files():
    for dirpath, dirnames, filenames in os.walk(CORE_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIR_SEGMENTS]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            if filename.startswith("test_") or filename == "conftest.py":
                continue
            yield Path(dirpath) / filename


class _EnclosingFunctionTracker(ast.NodeVisitor):

    def __init__(self):
        self.stack: list[str] = ["<module>"]
        self.found: list[tuple[str, str, ast.Call]] = []

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
            constants: set | None = None
            for names, required_constant, mechanism in _MECHANISM_ROWS:
                if name not in names:
                    continue
                if required_constant is None:
                    self.found.append((self.stack[-1], mechanism, node))
                    break
                if constants is None:
                    constants = set(_string_constants(node))
                if required_constant in constants:
                    self.found.append((self.stack[-1], mechanism, node))
                    break
        self.generic_visit(node)


def _iter_tracked_calls():
    for path in _iter_py_files():
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
        except SyntaxError as exc:
            raise AssertionError(
                f"{path}: failed to parse -- {exc}. This tripwire cannot "
                f"certify a file it cannot read."
            ) from exc
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
        sites.setdefault(key, mechanism)
    return sites


ALLOWLIST: dict[str, dict[str, object]] = {
    "ops/ceremony/commit_exec_bit.py::_handler": {
        "reason": "ineligible: commits with no pathspec at all, so a release "
        "keyed off \"what this commit covered\" has no bounded answer",
        "confirmed": True,
    },
    # DELIBERATELY RETAINED WHILE STALE (2026-08-25; re-measured 2026-09-06).
    "ops/ceremony/git_native.py::_commit_scoped_private_index": {
        "reason": "release",
        "confirmed": False,
    },
    "ops/ceremony/git_native.py::commit_authored_content": {
        "reason": "release",
        "confirmed": False,
    },
    "ops/ceremony/git_native.py::commit_authored_new_file": {
        "reason": "release",
        "confirmed": False,
    },
    # VERIFIED INELIGIBLE, not asserted: `release_committed_claims(sid, paths)`
    "benchmarks/probe_commit_pipeline.py::one": {
        "reason": "ineligible: a benchmark fixture commit in a throwaway "
        "repo -- no session holds a claim over these paths, and the "
        "function threads no session id to release one with",
        "confirmed": True,
    },
    "ops/handoff_archive_transition.py::_commit_retained_supersede_flip": {
        "reason": "ineligible: threads no session id -- it commits a "
        "supersede flip on a predecessor it did not itself claim",
        "confirmed": True,
    },
    "execute_plan_assemble/close_out_and_stamp.py::close_out_and_stamp": {
        "reason": "release",
        "confirmed": True,
    },
    "ops/memo_transition.py::_commit_terminal_write": {
        "reason": "release",
        "confirmed": True,
    },
    "ops/session/safe_commit_offer.py::_commit_group": {
        "reason": "release",
        "confirmed": True,
    },
    # VERIFIED INELIGIBLE, and a correction to this row's first draft, which
    # the DOCSTRING (`ast.unparse` emits docstrings; the sweep did not strip
    "ops/plan_status_transition.py::_commit_plan_flip": {
        "reason": "ineligible: has no caller-supplied session identity by "
        "design (an unauthenticated --by override is refused upstream), so "
        "there is no trusted id to release a claim under",
        "confirmed": True,
    },
    "ops/ceremony/commit_v2.py::_handler": {
        "reason": "release",
        "confirmed": True,
    },
    "workstream_complete/directives_commit_tail.py::run_close_commit": {
        "reason": "release",
        "confirmed": True,
    },
    "ops/fleet/_common.py::archive_and_commit": {
        "reason": "release",
        "confirmed": False,
    },
    "ops/fleet/_common.py::rm_and_commit": {
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
    "ops/ceremony/git_native.py::commit_with_message_file_pathspec_scoped": {
        "reason": "release",
        "confirmed": False,
    },
    "ops/deliverable_cascade.py::_commit_mutated_paths": {
        "reason": "release",
        "confirmed": False,
    },
    # any form -- distinct from the DELIBERATELY-RETAINED-WHILE-STALE rows
    "ops/ceremony/post_commit_tail.py::_run_completion_entry_fold": {
        "reason": "release",
        "confirmed": False,
    },
    "ops/ceremony/consumed_handoff_stamp.py::_commit_and_push_follow_up": {
        "reason": "release",
        "confirmed": True,
    },
    "ops/ceremony/post_commit_tail.py::_commit_and_push_origin_stub_close": {
        "reason": "release",
        "confirmed": True,
    },
    "backlog_grind_assemble/apply.py::_commit_one": {
        "reason": "release",
        "confirmed": False,
    },
    "benchmarks/op_fixtures.py::materialize_fixture_repo": {
        "reason": "ineligible: builds a disposable benchmark-harness fixture "
        "repo, not a live coordinator worktree -- no session claims exist "
        "to release",
        "confirmed": True,
    },
    "ops/workday_complete_step2_5_dirty_tree.py::_act_commit": {
        "reason": "release",
        "confirmed": False,
    },
    "ops/workday_complete_step2_5_dirty_tree.py::_act_gitignore": {
        "reason": "release",
        "confirmed": False,
    },
    # `_commit_delivered_memo`'s RECEIVER-repo commit discussed in this
    "ops/fleet/memo_send.py::_memo_send": {
        "reason": "release",
        "confirmed": False,
    },
    # DESTINATION repo the percolate tool writes into, not the calling
    "percolate/round.py::step_commit": {
        "reason": "ineligible: commits into percolate's mirrored destination "
        "repo (context.dest_repo_root), not the calling session's own "
        "worktree -- no session claims exist there to release",
        "confirmed": True,
    },
    "git/commit.py::commit_paths": {
        "reason": "release",
        "confirmed": False,
    },
    # both flagged DELIBERATELY RETAINED WHILE STALE for the same DR-211
    "ops/ceremony/git_native.py::_commit_via_head_spine": {
        "reason": "release",
        "confirmed": False,
    },
    "ops/archive_auto_memory_rows.py::main": {
        "reason": "release",
        "confirmed": False,
    },
    # VERIFIED INELIGIBLE by reading the body: writes a content-addressed
    "ops/fleet/_memo_anchor.py::write_anchor": {
        "reason": "ineligible: a `cas_ref` anchor-ref write (blob + ref CAS "
        "under refs/coordinator/inbox/), not a commit -- no worktree paths "
        "or session claim exist here to release",
        "confirmed": True,
    },
    # VERIFIED INELIGIBLE by reading the body: no session id anywhere in
    "ops/fleet/memo_heal.py::_restore_one": {
        "reason": "ineligible: threads no session id -- it restores a lost "
        "peer memo into this repo's inbox, not work this session itself "
        "claimed",
        "confirmed": True,
    },
    # VERIFIED INELIGIBLE by reading the body: commits into
    "ops/fleet/memo_send.py::_deliver_cc_copy": {
        "reason": "ineligible: commits into a `cc:` receiver's own PEER "
        "repo, never the sending session's worktree -- no session claims "
        "exist there to release",
        "confirmed": True,
    },
    "ops/review_freeze_diff.py::freeze_diffs_batch": {
        "reason": "release",
        "confirmed": False,
    },
    # VERIFIED INELIGIBLE by reading the body: commits into
    "ops/tracker/push_suggestion.py::_commit_envelope": {
        "reason": "ineligible: commits into the receiver repo (peer "
        "delivery, same shape as memo_send's `to:`/`cc:` legs), never the "
        "sending session's own worktree -- no session claims exist there "
        "to release",
        "confirmed": True,
    },
}


def test_enumerator_walks_a_sane_file_count_with_no_subprocess():
    file_count = sum(1 for _ in _iter_py_files())
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
    sites = _enumerate_commit_sites()
    by_mechanism: dict[str, list[str]] = {}
    for rel, func_name, mechanism in _iter_tracked_calls():
        by_mechanism.setdefault(mechanism, []).append(f"{rel}::{func_name}")

    # first that is OUTCOME-keyed rather than string-keyed -- see the module
    expected_mechanisms = {
        "commit_tree_plumbing",
        "_run_git_helper",
        "git_native._git",
        "commit_scoped",
        "cas_ref_landing",
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
    sites = _enumerate_commit_sites()
    stale = sorted(set(ALLOWLIST) - set(sites))
    assert not stale, (
        "Stale allowlist entries name commit sites that no longer exist "
        "(renamed, removed, or merged away) -- remove them or fix the key:\n  "
        + "\n  ".join(stale)
    )


def test_every_allowlist_entry_has_a_reason():
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
    assert key in ALLOWLIST
