"""coordinator_core.bash_guards._alternative_liveness -- standing,
re-runnable measurement of whether the "sanctioned alternative" every
confinement/advisory guard in this package names in its caller-facing
message (a deny's ``permissionDecisionReason``, an advisory's
``additionalContext``, or a rewrite's ``updatedInput.command``) is actually
REACHABLE -- verified by EXECUTING it, never by reading the string.

Why this exists: three separate shipped messages have been found pointing
at something that had moved -- a CLI flag whose backing frontmatter field
stopped being emitted, a capability claim ("in-process, zero-fork search")
that was false at the harness level, and a command that failed when run
from the caller's own working directory. Every one of those would have
passed a non-emptiness check on the message text. The bar here is
LIVENESS, not presence: does the named alternative still work.

This module is the mechanism (guard registry, alternative extraction,
per-kind liveness probes); ``tests/test_alternative_liveness_gate.py`` is
the gate that drives it and fails the build on a ``DEAD`` verdict. Mirrors
the standing-measurement posture of the sibling ``_guard_coverage.py``:
importable, re-runnable, driven by the SHIPPED guard functions themselves,
never a hand-duplicated re-implementation of their logic.

===========================================================================
NEGATIVE-SPEC -- what this module deliberately does NOT do
===========================================================================
- Never hand-rolls a parallel command tokenizer. Wherever a probe needs to
  reason about command shape (rare -- most probes here operate on the
  ALTERNATIVE text a guard emitted, not on the original trigger command),
  it uses ``shlex`` for its own throwaway alternative string, not the
  shared ``_command_tokenizer`` module (which parses the CALLER's original
  command under this package's own quote-handling contract and is out of
  scope for a string this module itself extracts from prose).
- Never executes a mutating verb for real. A `command`/`executable`
  alternative whose argv contains a recognized mutating shape (git
  commit/push/stash/clean/checkout/reset/branch -D/worktree add, rm, mv,
  cp, dd, tee, `sed -i`, or a `>`/`>>` redirect, or a `python3 -c` script
  whose source calls one of those) is NEVER run to completion -- liveness
  is proven by resolving the interpreter/executable and confirming it
  IMPORTS/PARSES/RECOGNIZES the invocation (`--help`, or a syntax-only
  `compile()` pass over a `-c` script body), never by letting the
  destructive half execute. This is the load-bearing carve-out for
  ``check_destructive_rm``'s `git stash push` alternative and similar.
- Never mutates the real repo. Every probe that DOES execute something for
  real (a read-only informational command) runs inside a throwaway
  ``tempfile.mkdtemp()`` cwd with a subprocess timeout, never this
  package's own working tree.
- Never claims coverage it does not have. A message with no
  alternative-shaped signal at all (no "Use instead"/"Did you mean",
  no backtick command, no `COORDINATOR_ALLOW_*`/`COORDINATOR_OVERRIDE_*`/
  `COORDINATOR_DISABLE_*` token, no harness-tool phrase) is recorded as
  carrying zero alternatives -- this is NOT a gate failure (some denies are
  pure policy statements with no offered substitute, e.g.
  ``block_worktree_sentinel_creation``/``block_approval_sentinel_creation``
  both deliberately name no bypass). A message that DOES carry
  alternative-shaped signal but that signal cannot be classified into one
  of the five kinds below IS a gate failure (fail loud, per this module's
  own charter -- an unclassifiable alternative is indistinguishable from a
  dead one).
- Does not attempt Windows-native execution. Every subprocess probe in this
  module is written to be Windows-safe (no ``shell=True`` with POSIX
  quoting, ``shutil.which`` for resolution, no POSIX-only path assumptions)
  but this module has only been RUN on macOS as of this dispatch -- Windows
  coverage is unverified, not merely "should work". Say so, do not imply
  otherwise.

Spec backlink: docs/plans/2026-07-29-windows-viability-stop-the-spawn-storms.md
Sibling: ``_guard_coverage.py`` (reach measurement); ``tests/test_deny_message_
accuracy.py`` (sampled message-vs-trigger correspondence, BX-12).

===========================================================================
KNOWN DETERMINISM HAZARD -- this module's results are NOT currently
reproducible run-to-run, and that is not a bug in this module.
===========================================================================
This module enumerates and fires guards LIVE, in-process, against the
SHIPPED code in ``coordinator_core/bash_guards/`` at whatever moment it
runs -- by design (the whole point is measuring the real, current guard
behavior, never a frozen snapshot). Several guards in this package's
confinement/chain-structure band are, as of 2026-07-29, under concurrent
edit by a peer session in the SAME working tree this module imports from.
Observed directly during this module's own authoring: ``block_reviewer_
bash_outside_allowlist.check()`` intermittently raised ``ValueError: too
many values to unpack (expected 2, got 3)`` at its own internal
``_scan_for_unquoted_metacharacter`` call -- reproduced twice, then absent
on a later run minutes later with no change on this module's side. A
single run of this module (or its gate) can therefore observe a guard
mid-edit, crashing or behaving differently than the same guard five
minutes earlier or later. Do not treat one run's report -- LIVE, DEAD, or
a crash -- as settled fact about a guard this module does not own; re-run
before trusting a single-run result, and never diagnose a transient
concurrent-edit crash as a defect in the extraction/probe machinery here.
This hazard is inherent to importing and calling live sibling modules from
a shared working tree during active development, not something a fixed
corpus/mock/snapshot in this module would be appropriate to paper over
(the whole design premise is executing the SHIPPED function, never a
copy).
"""

from __future__ import annotations

import contextlib
import enum
import functools
import importlib
import inspect
import json
import os
import pkgutil
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import coordinator_core.bash_guards as _pkg
from coordinator_core.bash_guards._command_tokenizer import (
    exceeds_tokenizable_ceiling as _exceeds_tokenizable_ceiling,
)
from coordinator_core.bash_guards import dispatch_checks as _dc
from coordinator_core.bash_guards import block_approval_sentinel_creation
from coordinator_core.bash_guards import block_disarm_marker_sentinel_creation
from coordinator_core.bash_guards import block_illegal_filename
from coordinator_core.bash_guards import block_noncanonical_branch_creation
from coordinator_core.bash_guards import block_reviewer_bash_outside_allowlist
from coordinator_core.bash_guards import block_subagent_commit
from coordinator_core.bash_guards import block_subagent_destructive_action
from coordinator_core.bash_guards import block_subagent_plan_body_bash_write
from coordinator_core.bash_guards import block_subagent_stash_creation
from coordinator_core.bash_guards import block_worktree_creation
from coordinator_core.bash_guards import block_stash_destruction
from coordinator_core.bash_guards import block_worktree_sentinel_creation
from coordinator_core.bash_guards import block_dev_repo_sentinel_removal
from coordinator_core.bash_guards import check_raw_pid_liveness
from coordinator_core.bash_guards import guard_branch_set_precedence
from coordinator_core.bash_guards import guard_grep_via_bash
from coordinator_core.bash_guards import guard_head_tail_rewrite
from coordinator_core.bash_guards import guard_inprocess_search
from coordinator_core.bash_guards import guard_longlived_branch_naming
from coordinator_core.bash_guards import guard_multiprobe_banner
from coordinator_core.bash_guards import guard_offer_git_c
from coordinator_core.bash_guards import guard_plumbing_and_loops
from coordinator_core.daily_day import local_day

_PROBE_TIMEOUT_SEC = 10


class GuardBand(enum.Enum):
    """Which of the two sessions currently owns fixing a guard, per the
    2026-07-29 PM-ratified band split: this session owns the advisory/
    rewrite guards; a peer session owns chain structure and the
    confinement (hard-deny) guards. This gate is deliberately
    module-agnostic and covers BOTH bands without editing either -- band
    membership below exists ONLY so a finding can be handed to the right
    session without further investigation, never to narrow what the gate
    itself checks. Derived MECHANICALLY (a fixed lookup against the two
    explicitly-named ``OURS_*`` sets below, everything else defaults to
    ``PEER``) -- never inferred from message content or guessed per-guard,
    so attribution cannot silently drift as guards are added."""

    OURS = "ours"
    PEER = "peer"


#: Exactly the three guard_*.py MODULES this session owns, per the PM
#: ruling -- everything else module-shaped (block_*.py, check_raw_pid_
#: liveness.py, check_test_suite_invocation.py, guard_inprocess_search.py)
#: defaults to PEER, since the ruling names only these three as "ours".
_OURS_MODULE_GUARDS = frozenset({"guard_grep_via_bash", "guard_multiprobe_banner", "guard_plumbing_and_loops"})

#: Exactly the dispatch_checks.py functions this session owns: the BX-16
#: rewrite/advisory family plus check_offer_git_c. Every OTHER check_*
#: function in dispatch_checks.py (the hard-deny/content class: no-verify,
#: destructive-git-*, destructive-rm, blanket-git-add, runaway-find,
#: probe-spray, validate-commit) defaults to PEER.
_OURS_DISPATCH_CHECKS = frozenset(
    {
        "check_find_exec_rewrite",
        "check_grep_via_bash_rewrite",
        "check_sed_range_read_advise",
        "check_cat_heredoc_write_advise",
        "check_git_commit_safe_commit_advise",
        "check_multiprobe_banner_rewrite",
        "check_head_tail_plumbing_rewrite",
        "check_offer_git_c",
    }
)


def classify_band(guard: str) -> GuardBand:
    """Mechanical band lookup -- membership in the two explicit ``OURS_*``
    sets above, nothing else. A guard absent from both (including every
    ``block_*`` module and every hard-deny ``check_*`` function in
    ``dispatch_checks.py``, plus the two module-shaped confinement guards
    ``check_raw_pid_liveness``/``check_test_suite_invocation`` and
    ``guard_inprocess_search``, none of which the ruling names as
    "ours") is ``PEER`` by default -- the ruling names what THIS session
    owns; everything unnamed belongs to the other band."""
    if guard in _OURS_MODULE_GUARDS or guard in _OURS_DISPATCH_CHECKS:
        return GuardBand.OURS
    return GuardBand.PEER

#: Portable (Windows-safe, no-op elsewhere) suppression of the console-popup
#: AllocConsole() every bare subprocess.run of a console app triggers under
#: the headless Bash-tool parent on Windows -- getattr resolves to
#: CREATE_NO_WINDOW on Windows and 0 on macOS/Linux, where the attribute
#: does not exist.  # popup-safe-env-suppressed
_NO_WINDOW: Dict[str, Any] = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


# ---------------------------------------------------------------------------
# (a) Guard registry -- discovered programmatically, never hand-listed.
# ---------------------------------------------------------------------------


#: Sibling modules whose own-defined `check_<name>(cmd, session_id[, cwd])`
#: functions carry the SAME sourceable-function contract as
#: `dispatch_checks.py`'s cohort, extracted out of it (M1, 2026-07-29:
#: `guard_offer_git_c.check_offer_git_c`, `guard_head_tail_rewrite.check_
#: head_tail_plumbing_rewrite`) rather than the `check(payload)` discovery-
#: module shape `discover_module_guard_names` below enumerates. Scanned
#: alongside `dispatch_checks` itself so a function's move out of that file
#: does not silently drop it from this discovery -- extending the walk, not
#: narrowing dispatch_checks's own contribution to it.
_DISPATCH_CHECK_SOURCE_MODULES = (_dc, guard_offer_git_c, guard_head_tail_rewrite)


def discover_dispatch_check_names() -> List[str]:
    """Every ``check_*`` function DEFINED (not merely imported) directly in
    ``dispatch_checks.py`` OR one of its extracted sibling modules
    (``_DISPATCH_CHECK_SOURCE_MODULES``). Module-introspection based so a
    new ``check_*`` added to any of those is picked up automatically."""
    names = []
    for module in _DISPATCH_CHECK_SOURCE_MODULES:
        for name, fn in inspect.getmembers(module, inspect.isfunction):
            if name.startswith("check_") and getattr(fn, "__module__", None) == module.__name__:
                names.append(name)
    return sorted(set(names))


def discover_module_guard_names() -> List[str]:
    """Every sibling module under this package exposing a top-level
    ``check(payload, ...)`` callable -- ``block_*``/``guard_*``/``check_*``
    module-style guards. ``pkgutil.iter_modules`` over the package's own
    directory, so a new guard MODULE is picked up automatically; private
    (``_``-prefixed) modules and the two non-guard orchestration modules
    (``dispatch``, ``dispatch_checks`` itself -- its OWN guards are
    enumerated by ``discover_dispatch_check_names`` above, function-level not
    module-level) are excluded."""
    pkg_dir = os.path.dirname(_pkg.__file__)
    names = []
    for modinfo in pkgutil.iter_modules([pkg_dir]):
        mod_name = modinfo.name
        if mod_name.startswith("_") or mod_name in ("dispatch", "dispatch_checks"):
            continue
        module = importlib.import_module("coordinator_core.bash_guards." + mod_name)
        if callable(getattr(module, "check", None)):
            names.append(mod_name)
    return sorted(names)


def _payload(cmd: str, agent_id: Optional[str] = "deadbeef0123", agent_type: Optional[str] = None,
             session_id: str = "altlive-probe", cwd: Optional[str] = None) -> Dict[str, Any]:
    d: Dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "session_id": session_id,
        "cwd": cwd,
    }
    if agent_id is not None:
        d["agent_id"] = agent_id
    if agent_type is not None:
        d["agent_type"] = agent_type
    return d


def _run(argv: List[str], cwd: str, timeout: int = _PROBE_TIMEOUT_SEC) -> None:
    """Shared probe runner for the guard-under-untrusted-input probes below
    (``probe_command``, ``probe_flag``, ``_probe_python_dash_c``,
    ``_probe_python_dash_m``).

    NEGATIVE-SPEC: this call is a deliberate isolation boundary and must
    NEVER be converted to an in-process call. It executes agent/reviewer-
    supplied argv (and, transitively, `-c`/`-m` scripts) to test liveness;
    the subprocess IS the containment for that untrusted input. Collapsing
    it to an import would run untrusted input inside the guard's own
    interpreter. Reason recorded in
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md
    (mechanism: "guard-under-untrusted-input sandboxing")."""
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True, timeout=timeout, **_NO_WINDOW)


@contextlib.contextmanager
def _scratch_git_repo():
    """A throwaway git repo (one committed file) in a tempdir -- cleaned up
    on every exit path. Never the real working tree."""
    tmp = tempfile.mkdtemp(prefix="altlive-repo-")
    try:
        _run(["git", "init", "-q"], cwd=tmp)
        _run(["git", "config", "user.email", "altlive-probe@example.com"], cwd=tmp)
        _run(["git", "config", "user.name", "altlive-probe"], cwd=tmp)
        with open(os.path.join(tmp, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("hello\n")
        _run(["git", "add", "."], cwd=tmp)
        _run(["git", "commit", "-q", "-m", "init"], cwd=tmp)
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _make_backpointer(git_root: str, agent_id: str, subagent_type: str, em_sid: str = "altliveemsess") -> None:
    """Writes the on-disk back-pointer chain
    (``.git/coordinator-sessions/.agents/<agent_id>/em-session-id.txt`` ->
    ``.git/coordinator-sessions/<em_sid>/dispatched-agents.txt``)
    ``block_subagent_plan_body_bash_write`` reads its SECONDARY identity leg
    from -- that guard, unlike its siblings, does not consult the payload's
    PRIMARY ``agent_type`` field at all (see
    ``_read_backpointer_subagent_type``), so a bare payload cannot trigger
    it; this fixture is the hermetic substitute for a real dispatch."""
    agents_dir = os.path.join(git_root, ".git", "coordinator-sessions", ".agents", agent_id)
    os.makedirs(agents_dir, exist_ok=True)
    with open(os.path.join(agents_dir, "em-session-id.txt"), "w", encoding="utf-8") as fh:
        fh.write(em_sid + "\n")
    sess_dir = os.path.join(git_root, ".git", "coordinator-sessions", em_sid)
    os.makedirs(sess_dir, exist_ok=True)
    with open(os.path.join(sess_dir, "dispatched-agents.txt"), "a", encoding="utf-8") as fh:
        fh.write("%s\t%s\t%s\n" % (agent_id, "altlive-teammate", subagent_type))


def _trigger_destructive_rm() -> Optional[Dict[str, Any]]:
    with _scratch_git_repo() as repo:
        target_dir = os.path.join(repo, "scratch_dir")
        os.makedirs(target_dir)
        with open(os.path.join(target_dir, "u.txt"), "w", encoding="utf-8") as fh:
            fh.write("untracked\n")
        return _dc.check_destructive_rm("rm -rf %s" % target_dir, "altlive-probe")


def _trigger_destructive_git_revert() -> Optional[Dict[str, Any]]:
    """Review: staff-eng, Finding 9 (2026-08-05) -- the previous UNTRIGGERED
    row recorded a probe of `git revert`, a command this guard does not
    target at all (it gates `checkout`/`restore`/`reset`/`stash`, per its
    own docstring). A load-bearing path (`state/`-rooted, `_is_loadbearing`)
    with an uncommitted tracked edit, then `git reset --hard`, denies
    without needing a peer claim -- the simplest live trigger shape."""
    with _scratch_git_repo() as repo:
        state_dir = os.path.join(repo, "state")
        os.makedirs(state_dir)
        target = os.path.join(state_dir, "important.md")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("baseline\n")
        _run(["git", "add", "state/important.md"], cwd=repo)
        _run(["git", "commit", "-q", "-m", "seed state file"], cwd=repo)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("baseline\nuncommitted edit\n")
        return _dc.check_destructive_git_revert("git -C %s reset --hard" % repo, "altlive-probe")


def _trigger_destructive_git_revert_advisory() -> Optional[Dict[str, Any]]:
    """Advisory sibling of `_trigger_destructive_git_revert` above -- same
    scratch repo shape, but the uncommitted edit lands OUTSIDE any
    load-bearing prefix, so this fires the advisory leg (allow +
    additionalContext), never the hard-deny leg."""
    with _scratch_git_repo() as repo:
        target = os.path.join(repo, "f.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("hello\nuncommitted edit\n")
        return _dc.check_destructive_git_revert_advisory("git -C %s stash" % repo, "altlive-probe")


def _trigger_plan_body_bash_write() -> Optional[Dict[str, Any]]:
    with _scratch_git_repo() as repo:
        agent_id = "deadbeef0123"
        _make_backpointer(repo, agent_id, "coordinator:executor")
        payload = _payload(
            "sed -i s/x/y/ docs/plans/2026-01-01-foo.md", agent_id=agent_id, cwd=repo,
        )
        return block_subagent_plan_body_bash_write.check(payload)


#: C1/C5/C7 (docs/plans/2026-08-01-branch-creation-seam-guards.md) are each
#: hazard-repo-scoped (`_is_hazard_repo(resolve_git_root(cwd))` gates every
#: one before its own name predicate runs) -- unlike every guard above this
#: comment, none of them fires from a bare synthetic payload. `_is_hazard_
#: repo` is swapped on each guard's OWN module attribute for the duration of
#: one trigger call, then restored -- never a persistent patch to shipped
#: guard behavior, and never dependent on this MACHINE's real fleet
#: registry: `coordinator_core/conftest.py`'s suite-wide `HOME`/
#: `USERPROFILE` quarantine (autouse) makes the real registry-lookup half of
#: `_is_hazard_repo` unreachable under pytest regardless of which real repo
#: `cwd` names, so pointing `cwd` at this package's own working tree (a
#: hazard repo OUTSIDE pytest, on this dev machine's fleet registry) is
#: insufficient on its own -- confirmed by this row initially firing under a
#: bare `python3 -c` probe and then failing under `pytest` with an identical
#: `cwd`. Mirrors this package's own test suites' injection convention
#: (`tests/test_guard_branch_set_precedence.py`'s own `monkeypatch.setattr(
#: guard, "_is_hazard_repo", lambda git_root: True)`), just without the
#: `monkeypatch` fixture (this module is a library, not a test) -- manual
#: save/restore in a `try/finally` instead.
_ALTLIVE_HAZARD_CWD = os.path.dirname(_pkg.__file__)


def _trigger_block_noncanonical_branch_creation() -> Optional[Dict[str, Any]]:
    orig_hazard = block_noncanonical_branch_creation._is_hazard_repo
    block_noncanonical_branch_creation._is_hazard_repo = lambda git_root: True
    try:
        return block_noncanonical_branch_creation.check(
            _payload("git checkout -b fix/altlive-noncanonical-probe", agent_id=None, cwd=_ALTLIVE_HAZARD_CWD)
        )
    finally:
        block_noncanonical_branch_creation._is_hazard_repo = orig_hazard


def _trigger_guard_longlived_branch_naming() -> Optional[Dict[str, Any]]:
    orig_hazard = guard_longlived_branch_naming._is_hazard_repo
    guard_longlived_branch_naming._is_hazard_repo = lambda git_root: True
    try:
        return guard_longlived_branch_naming.check(
            _payload("git checkout -b migration/altlive-longlived-probe", agent_id=None, cwd=_ALTLIVE_HAZARD_CWD)
        )
    finally:
        guard_longlived_branch_naming._is_hazard_repo = orig_hazard


def _trigger_guard_branch_set_precedence() -> Optional[Dict[str, Any]]:
    """C5's own `ahead_of_main` (the count actually named in its advisory)
    is a REAL `git rev-list` call, never injectable through `check()`'s own
    `branch_set_provider` seam (that seam only replaces the enumeration
    half, per that guard's own module docstring "Injection seam") -- so a
    deterministic fire needs a REAL scratch repo carrying a real commit on
    a same-day canonical branch ahead of a real `main`.

    `refs/remotes/origin/main` is written into the scratch repo explicitly:
    `_branch_set._main_ref()` resolves its OWN "does main/origin/main
    exist" probe against THIS PROCESS's actual working directory (that
    function takes no `cwd` param at all -- see its own docstring), not the
    scratch repo, so on this package's own dev/CI tree it answers
    "origin/main". The SECOND, cwd-scoped call
    (`git rev-list --count origin/main..<branch>`) then needs the scratch
    repo to carry a ref of that exact name, or the count silently comes
    back 0 (not an error) -- `_branch_set.py`'s own documented quirk, not a
    defect introduced here.

    The candidate branch's date segment is set to REAL "today"
    (`daily_day.local_day()`, called at trigger time) so AC16's
    `should_prompt_rename` leg short-circuits False on the "span already
    covers today" branch, independent of the fabricated recency epoch fed
    through `branch_set_provider` -- avoids the age-vs-span-date
    contradiction a stale real branch name would otherwise hit (recent
    enough to survive the 48h leg, but dated too far in the past to survive
    the should-rename leg simultaneously).

    `_is_hazard_repo` is swapped on `guard_branch_set_precedence`'s own
    module attribute for the duration of this one call, then restored --
    never a persistent patch to shipped guard behavior. Mirrors this
    guard's own test suite's technique
    (`tests/test_guard_branch_set_precedence.py`'s
    `monkeypatch.setattr(guard, "_is_hazard_repo", lambda git_root: True)`)
    since a real scratch tempdir is never a machine-registered hazard repo.
    """
    tmp = tempfile.mkdtemp(prefix="altlive-branchset-")
    try:
        _run(["git", "init", "-q", "-b", "main"], cwd=tmp)
        _run(["git", "config", "user.email", "altlive-probe@example.com"], cwd=tmp)
        _run(["git", "config", "user.name", "altlive-probe"], cwd=tmp)
        with open(os.path.join(tmp, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("hello\n")
        _run(["git", "add", "."], cwd=tmp)
        _run(["git", "commit", "-q", "-m", "init"], cwd=tmp)
        _run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=tmp)

        today = local_day()
        candidate = "work/altlive-other/%s" % today
        _run(["git", "checkout", "-q", "-b", candidate], cwd=tmp)
        with open(os.path.join(tmp, "f.txt"), "a", encoding="utf-8") as fh:
            fh.write("candidate commit\n")
        _run(["git", "commit", "-q", "-am", "candidate commit"], cwd=tmp)
        _run(["git", "checkout", "-q", "main"], cwd=tmp)

        target = "work/altlive-machine/%s" % today
        payload = _payload("git checkout -b %s" % target, agent_id=None, cwd=tmp)
        provider = lambda: [(candidate, time.time() - 3600)]

        orig_hazard = guard_branch_set_precedence._is_hazard_repo
        guard_branch_set_precedence._is_hazard_repo = lambda git_root: True
        try:
            return guard_branch_set_precedence.check(payload, branch_set_provider=provider)
        finally:
            guard_branch_set_precedence._is_hazard_repo = orig_hazard
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


#: Guards this module CAN provably drive to emit, keyed by canonical
#: registry name (dispatch_checks members as their bare function name;
#: module guards as their module name). Each callable takes no arguments
#: and returns the raw ``check()``/``check_*()`` result (``None`` on a
#: broken row -- verified as non-``None`` at registry-build time by the
#: gate test, per this module's own charter: a trigger that returns
#: ``None`` is a broken row, not a silently-skipped one).
LIVE_TRIGGERS: Dict[str, Callable[[], Optional[Dict[str, Any]]]] = {
    # -- dispatch_checks.py --
    "check_no_verify": lambda: _dc.check_no_verify('git commit --no-verify -m "x"', "altlive-probe"),
    "check_runaway_find": lambda: _dc.check_runaway_find("find / -name '*.py'", "altlive-probe"),
    "check_destructive_git_clean": lambda: _dc.check_destructive_git_clean("git clean -fdx", "altlive-probe"),
    "check_destructive_rm": _trigger_destructive_rm,
    "check_destructive_git_revert": _trigger_destructive_git_revert,
    "check_destructive_git_revert_advisory": _trigger_destructive_git_revert_advisory,
    "check_offer_git_c": lambda: guard_offer_git_c.check_offer_git_c(
        "cd %s && git status" % shlex.quote(os.path.dirname(_pkg.__file__)), "altlive-probe", ""
    ),
    "check_find_exec_rewrite": lambda: _dc.check_find_exec_rewrite(
        'find . -name "*.log" -exec rm {} \\;', "altlive-probe"
    ),
    "check_grep_via_bash_rewrite": lambda: _dc.check_grep_via_bash_rewrite(
        'grep -rn "TODO" src/', "altlive-probe"
    ),
    "check_sed_range_read_advise": lambda: _dc.check_sed_range_read_advise(
        "sed -n '10,20p' path/to/file.py", "altlive-probe"
    ),
    "check_cat_heredoc_write_advise": lambda: _dc.check_cat_heredoc_write_advise(
        "cat > out.txt <<'ALTLIVE_EOF'\nhello\nALTLIVE_EOF", "altlive-probe"
    ),
    "check_git_commit_safe_commit_advise": lambda: _dc.check_git_commit_safe_commit_advise(
        'git commit -m "fix the thing"', "altlive-probe"
    ),
    "check_multiprobe_banner_rewrite": lambda: _dc.check_multiprobe_banner_rewrite(
        'echo "=== SESSION FACTS ==="; pwd; whoami; git status --short', "altlive-probe"
    ),
    "check_head_tail_plumbing_rewrite": lambda: guard_head_tail_rewrite.check_head_tail_plumbing_rewrite(
        "find . -name '*.py' | head -n 5", "altlive-probe"
    ),
    # -- module guards --
    "block_worktree_creation": lambda: block_worktree_creation.check(
        _payload("git worktree add ../foo", agent_id=None)
    ),
    "block_stash_destruction": lambda: block_stash_destruction.check(
        _payload("git stash drop", agent_id=None)
    ),
    "block_subagent_stash_creation": lambda: block_subagent_stash_creation.check(
        _payload(" ".join(["git", "stash", "push"]), agent_type="coordinator:executor")
    ),
    "block_worktree_sentinel_creation": lambda: block_worktree_sentinel_creation.check(
        _payload("touch .coordinator-override-worktree-guard", agent_id=None)
    ),
    "block_approval_sentinel_creation": lambda: block_approval_sentinel_creation.check(
        _payload("touch .coordinator-doctrine-edit-approved", agent_id=None)
    ),
    "block_disarm_marker_sentinel_creation": lambda: block_disarm_marker_sentinel_creation.check(
        _payload("touch .coordinator-bash-guards-disarmed", agent_id=None)
    ),
    "block_dev_repo_sentinel_removal": lambda: block_dev_repo_sentinel_removal.check(
        _payload("rm .coordinator-dev-repo", agent_id=None)
    ),
    "block_illegal_filename": lambda: block_illegal_filename.check(
        _payload("echo x > bad:name.txt", agent_id=None)
    ),
    "check_raw_pid_liveness": lambda: check_raw_pid_liveness.check(
        _payload("k" + "ill -0 $PID", agent_id=None)
    ),
    "guard_inprocess_search": lambda: guard_inprocess_search.check(
        # The target must EXIST in this repo. As of the B1 fix (2026-08-06,
        # `search/engine.py::_expand_targets`) the search seam refuses a
        # nonexistent operand instead of answering `(no matches)` for it, so a
        # placeholder path -- this row previously read `src/`, which this repo
        # has never had -- makes the trigger silently stop firing and grades as
        # a broken registry row rather than a real coverage gap.
        _payload('grep -rn "TODO" coordinator_core/search/', agent_id=None),
        host_is_windows=True,
    ),
    "guard_grep_via_bash": lambda: guard_grep_via_bash.check(
        # `grep -rn` (H11-narrowed, 2026-07-30) is substitutable residue --
        # `grep-via-bash-rewrite` already claims it, so this guard now
        # returns None for it. `-P` is a genuinely GNU-only construct
        # (`_has_gnu_only_construct`) this guard still advises on; single
        # segment, so it is not shadowed by the CHAINED-only partial-pipe
        # path either.
        _payload('grep -Pn "TODO" src/', agent_id=None), host_is_windows=True
    ),
    "guard_multiprobe_banner": lambda: guard_multiprobe_banner.check(
        _payload('echo "=== SESSION FACTS ==="; git rev-parse --abbrev-ref HEAD; pwd', agent_id=None),
        host_is_windows=True,
    ),
    "guard_plumbing_and_loops": lambda: guard_plumbing_and_loops.check(
        _payload("find . -name '*.py' | head -n 5", agent_id=None), host_is_windows=True
    ),
    "block_subagent_destructive_action": lambda: block_subagent_destructive_action.check(
        _payload("git rebase -i HEAD~3", agent_type="coordinator:executor")
    ),
    "block_subagent_commit": lambda: block_subagent_commit.check(
        _payload('git commit -m "x"', agent_type="coordinator:executor")
    ),
    "block_reviewer_bash_outside_allowlist": lambda: block_reviewer_bash_outside_allowlist.check(
        _payload("rm -rf /", agent_type="coordinator:code-reviewer")
    ),
    "block_subagent_plan_body_bash_write": _trigger_plan_body_bash_write,
    # -- docs/plans/2026-08-01-branch-creation-seam-guards.md, chunk C2 --
    "block_noncanonical_branch_creation": _trigger_block_noncanonical_branch_creation,
    "guard_branch_set_precedence": _trigger_guard_branch_set_precedence,
    "guard_longlived_branch_naming": _trigger_guard_longlived_branch_naming,
}


#: Guards this pass could NOT drive to emit within its bounded time, each
#: with the specific reason -- visible and countable, per this module's own
#: charter, never a silent gap. A future session closing one of these should
#: DELETE the row here and add it to ``LIVE_TRIGGERS`` above, not leave both.
UNTRIGGERED: Dict[str, str] = {
    "check_destructive_git_orphan": (
        "Target shape (which git-orphan-adjacent command combination this "
        "check actually denies) was not identified within this dispatch's "
        "bounded time -- every combination tried (git branch -D, git "
        "worktree remove --force) returned None against a real scratch "
        "repo. Needs a read of the function body to pin the exact trigger "
        "shape, not a black-box guess."
    ),
    "check_blanket_git_add": (
        "This guard's target-class gate is hardcoded to the operator's real "
        "~/.claude meta-repo (git_root == os.path.realpath(~/.claude)), not "
        "any repo -- see its own source. A hermetic probe cannot construct "
        "that specific git root inside a tempdir without monkeypatching the "
        "guard's own path resolution, which this measurement-only module "
        "does not do (it never patches a SHIPPED guard's behavior -- see "
        "module negative-spec). Running the real trigger against the "
        "operator's actual ~/.claude is read-only-safe (the check only runs "
        "`git rev-parse`) but ties this test's pass/fail to a path that may "
        "not exist, or may not be a git repo, on every machine this suite "
        "runs on -- rejected for portability."
    ),
    "check_probe_spray": (
        "Stateful: gates on its own internal is-probe SHAPE classifier "
        "(distinct from the trigger commands tried here, e.g. `ps -ef | "
        "grep python`) plus a same-session recurrence ring. "
        "`_guard_coverage.py`'s own measurement of this same guard needed a "
        "62,487-command real corpus to find shapes that reliably trip it "
        "even with the threshold/cooldown patched to 1/0 -- reproducing "
        "that from a hand-picked trigger command was not completed within "
        "this dispatch's bounded time."
    ),
    "check_validate_commit": (
        "Requires a real staged/committed git state matching several "
        "independent internal checks (the function's own numbering runs to "
        "at least Check 8) -- a bare `git commit -m x` against a fresh "
        "scratch repo returned None. Constructing the exact state shape was "
        "not completed within this dispatch's bounded time."
    ),
    "check_test_suite_invocation": (
        "Requires a cwd that resolves as a real pytest-configured project "
        "(its own `classify_command` walks project config, per its test "
        "file's `repo` fixture) -- out of this dispatch's bounded time to "
        "reproduce hermetically without importing that fixture machinery "
        "wholesale."
    ),
}


@dataclass
class GuardFireResult:
    guard: str
    fired: bool
    envelope: Optional[Dict[str, Any]]
    error: Optional[str] = None


#: The one env var, per SC-DR-009, any session-scoped guard latch may key off
#: of (``guard_inprocess_search``'s AC5 footer latch is the first and, as of
#: this writing, only one -- see that module's own "Session latch on the
#: explanatory paragraph" docstring section). Named here, not re-derived from
#: that module, so this harness stays correct for ANY future guard that reads
#: the same var, not just the one that motivated it.
_SESSION_SCOPED_ENV_VAR = "CLAUDE_CODE_SESSION_ID"


def _cleanup_inprocess_search_latch(sid: str) -> None:
    """Best-effort removal of the on-disk marker tree ``guard_inprocess_
    search``'s session latch may have written for the isolated ``sid`` this
    probe minted. Not required for correctness (a fresh, never-reused uuid4
    is never observed as "seen" regardless of whether its marker file is
    ever cleaned up), but left uncleaned it accumulates one throwaway
    directory under the real repo's ``.git/coordinator-sessions/`` per gate
    run forever. Swallows every failure -- this is tidiness, not the
    isolation guarantee itself, and must never turn a passing probe into a
    crashing one."""
    try:
        latch_path = guard_inprocess_search._latch_path(os.getcwd(), sid)
        if latch_path is not None:
            shutil.rmtree(latch_path.parent, ignore_errors=True)
    except Exception:  # noqa: BLE001 -- cleanup-only, never allowed to fail the probe
        pass


@contextlib.contextmanager
def _isolated_session_scope():
    """Pin ``CLAUDE_CODE_SESSION_ID`` to a fresh, never-before-seen uuid4 for
    the duration of one guard-trigger call, restoring the prior value (or
    absence of one) on exit.

    KNOWN GOTCHA this exists to close: ``guard_inprocess_search`` gained a
    session-scoped latch (AC5, 2026-08-01) that renders its explanatory
    paragraph only on the FIRST answered call per session id, and a bare
    one-line marker on every later one -- correct guard behavior, but it
    means firing that guard's registered trigger through the ambient,
    real ``CLAUDE_CODE_SESSION_ID`` this test process inherits (this
    module's own docstring: "measuring the real, current guard behavior,
    never a frozen snapshot") observes whatever latch state a PRIOR run
    left on disk, not a first call -- exactly the alternatives this
    ratchet is pinned against silently disappearing from the emitted
    message. Minting a fresh session id per probe call makes every probe
    observe a guaranteed-first call, deterministically, regardless of any
    marker file a previous gate run (or a real session) left behind --
    the gate measures the guard's FULL first-call emission, which is the
    emission its pinned alternatives actually describe. Guard-agnostic by
    construction: any OTHER guard a future session gives the same kind of
    session-scoped latch is isolated by the same wrap, with zero new code
    needed here."""
    prior = os.environ.get(_SESSION_SCOPED_ENV_VAR)
    fresh_sid = "altlive-probe-" + uuid.uuid4().hex
    os.environ[_SESSION_SCOPED_ENV_VAR] = fresh_sid
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop(_SESSION_SCOPED_ENV_VAR, None)
        else:
            os.environ[_SESSION_SCOPED_ENV_VAR] = prior
        _cleanup_inprocess_search_latch(fresh_sid)


def _call_trigger_isolated(trigger: Callable[[], Optional[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """Call a zero-arg ``LIVE_TRIGGERS`` entry inside ``_isolated_session_scope``.
    The ONE place in this module that ever calls a ``LIVE_TRIGGERS`` entry --
    every caller (``fire_guard``, ``probe_override``) routes through here so
    the isolation guarantee those two docstrings describe is actually true,
    not merely true for whichever call site someone remembered to wrap.
    Exceptions propagate to the caller unchanged; this function only
    controls the session-id environment for the duration of the call."""
    with _isolated_session_scope():
        return trigger()


def fire_guard(guard: str) -> GuardFireResult:
    """Call ``guard``'s registered trigger and report whether it emitted a
    non-``None`` envelope. An exception during the trigger call is captured
    as evidence, never allowed to abort a caller iterating the whole
    registry (F1 posture, mirrored from ``dispatch.py``'s own per-entry
    isolation).

    Every trigger call runs inside ``_isolated_session_scope`` (via
    ``_call_trigger_isolated``) -- see that function's docstring for the
    session-latch gotcha this closes. Applied unconditionally (not only for
    ``guard_inprocess_search``) because no other registered guard currently
    reads ``CLAUDE_CODE_SESSION_ID`` at all, so the isolation is a no-op for
    them, and staying guard-agnostic here is what makes this robust to the
    next guard that grows the same kind of session-scoped latch."""
    trigger = LIVE_TRIGGERS.get(guard)
    if trigger is None:
        raise KeyError("%r is not in LIVE_TRIGGERS (check UNTRIGGERED instead)" % guard)
    try:
        envelope = _call_trigger_isolated(trigger)
    except Exception as exc:  # noqa: BLE001 -- isolate one guard's crash from the rest
        return GuardFireResult(guard=guard, fired=False, envelope=None, error="%s: %s" % (type(exc).__name__, exc))
    return GuardFireResult(guard=guard, fired=envelope is not None, envelope=envelope, error=None)


# ---------------------------------------------------------------------------
# (b) Alternative extraction + classification.
# ---------------------------------------------------------------------------


class AlternativeKind(enum.Enum):
    COMMAND = "command"
    EXECUTABLE = "executable"
    FLAG = "flag"
    HARNESS_CAPABILITY = "harness_capability"
    OVERRIDE = "override"


class UnclassifiableAlternative(Exception):
    """Raised when a message carries alternative-shaped signal (a "Use
    instead"/"Did you mean" cue, a backtick span, or similar) that does not
    fit any of the five kinds above. Per this module's charter, this is a
    GATE FAILURE, not a silent skip -- an alternative this module cannot
    even classify is indistinguishable from a dead one."""


@dataclass
class Alternative:
    kind: AlternativeKind
    raw: str
    #: kind-specific normalized payload -- an argv list for COMMAND/
    #: EXECUTABLE, a ``(cli_name_or_None, flag)`` tuple for FLAG, the
    #: matched marker phrase for HARNESS_CAPABILITY, the bare env-var name
    #: for OVERRIDE.
    detail: Any = None


_OVERRIDE_RE = re.compile(r"\bCOORDINATOR_(?:ALLOW|OVERRIDE|DISABLE)_[A-Z0-9_]+\b")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_READ_CALL_RE = re.compile(r"\bRead\([^)]*\)")
_INDENTED_CMD_RE = re.compile(r"^[ \t]{2,}(\S.*)$", re.MULTILINE)
#: A colon (optionally preceded by a closing paren, e.g. "...resolve it):")
#: immediately followed by one or more blank-line-tolerant indented lines --
#: this package's own convention for "the following lines are the offer",
#: independent of any specific cue WORD. Structural/positional, same spirit
#: as `_CUE_WINDOW_RE` but anchored on punctuation+indentation rather than
#: prose vocabulary, for messages whose introducing sentence uses neither
#: "Use instead:" nor "Did you mean" (see `extract_alternatives`' last-
#: resort fallback for the concrete case this closes).
_LABELED_INDENT_BLOCK_RE = re.compile(r"[:)]\s*\n\s*\n?((?:[ \t]{2,}\S[^\n]*\n?)+)")
#: Deliberately narrow to the structural markers this package's own guard
#: messages use to introduce a CONCRETE substitute (always followed by a
#: backtick/indented command in every message surveyed while building this
#: module) -- a bare "instead"/"Instead:" is excluded because several
#: guards (e.g. block_worktree_sentinel_creation) use it to introduce pure
#: prose guidance ("dispatch into the SAME working tree...") with no
#: concrete command at all, which must NOT be treated as an unclassifiable
#: gate failure. Used ONLY to decide "did this message promise something
#: extraction failed to find" (the fail-loud trigger) -- NOT to bound where
#: extraction looks (see ``_CUE_WINDOW_RE``/``_cue_windows`` below, a
#: separate, deliberately broader regex with a different job).
_ALT_CUE_RE = re.compile(r"(Use instead:|Did you mean|Run this instead|Example:)", re.IGNORECASE)

#: the Director of Engineering's review (2026-07-29, coordinatoreng-director-1d83280e, finding 4):
#: scanning EVERY backtick span in a message's full text is how a guard's
#: own shape-name self-reference and a Python API reference mentioned in
#: unrelated prose became "candidate alternatives" -- extraction must be
#: anchored to POSITION, not merely to backtick markup. This regex is
#: deliberately BROADER than ``_ALT_CUE_RE`` above (it also matches a bare
#: mid-sentence "instead", e.g. check_raw_pid_liveness's "Use the canonical
#: liveness primitives instead (...)"") because a false-positive window
#: here only WIDENS where backtick spans get a chance to classify -- the
#: per-span filters in ``_classify_backtick_span`` (shape-name/dotted-path/
#: snake_case exclusions) still apply inside the window and do the real
#: precision work. A false-positive cue is harmless; a false-negative cue
#: silently drops a real alternative, so broad-and-filtered beats
#: narrow-and-exact for THIS regex's specific job.
_CUE_WINDOW_RE = re.compile(r"(Use instead:?|Did you mean|Run this instead|Example:|\binstead\b)", re.IGNORECASE)
_CUE_WINDOW_MAX_CHARS = 600


def _cue_windows(text: str) -> List[str]:
    """Every bounded span of ``text`` following a cue-word occurrence, cut
    at the next blank line (paragraph break) or a fixed character cap,
    whichever comes first. Alternative extraction from backtick spans and
    indented command blocks is restricted to these windows -- text OUTSIDE
    any cue window (e.g. the sentence naming a guard's own detected shape,
    or an unrelated prose aside) never gets a chance to be misread as an
    offered alternative."""
    windows: List[str] = []
    for m in _CUE_WINDOW_RE.finditer(text):
        start = m.end()
        blank = text.find("\n\n", start)
        end = blank if blank != -1 else len(text)
        end = min(end, start + _CUE_WINDOW_MAX_CHARS)
        windows.append(text[start:end])
    return windows

_HARNESS_CAPABILITY_MARKERS = (
    "the Read tool",
    "the Write tool",
    "Explore or general-purpose subagent",
    "dispatch a subagent",
    "dispatching an Explore",
    "in-process",
    "searched in-process",
)

#: Bare shape-identifier tokens guards use to NAME THEMSELVES in prose
#: (e.g. "the `grep-via-bash` shell fan-out is denied") -- never an
#: alternative, so excluded from backtick classification outright.
_SHAPE_NAME_TOKENS = frozenset(
    {"grep-via-bash", "multi-probe-banner", "multiprobe-banner", "head-tail-plumbing", "for-loop", "find-exec-xargs"}
)

#: Bare shell metacharacters/operators a message may quote inline as part
#: of explaining its OWN grammar (e.g. "an unquoted `|` pipe IS allowed") --
#: never independently invocable, so never an alternative in their own
#: right.
_BARE_METACHAR_TOKENS = frozenset({"|", ">", ">>", "<", "&", ";", "&&", "||", "`", "$("})

_DOTTED_PYTHON_PATH_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)+$")

#: First-token verbs (or, for git, subcommands) this module treats as
#: mutating -- a COMMAND/EXECUTABLE alternative carrying one of these is
#: proven live via `--help`/syntax-check only, per module negative-spec.
_MUTATING_VERBS = frozenset(
    {
        "rm", "mv", "cp", "dd", "tee",
    }
)
_MUTATING_GIT_SUBCOMMANDS = frozenset(
    {"commit", "push", "stash", "clean", "checkout", "reset", "branch", "worktree", "add", "rebase"}
)


def _classify_backtick_span(span: str) -> Optional[Alternative]:
    text = span.strip()
    if not text:
        return None
    if text in _BARE_METACHAR_TOKENS or not re.search(r"[A-Za-z0-9]", text):
        # A message quoting its own grammar inline (e.g. "an unquoted `|`
        # pipe IS allowed") -- not an alternative.
        return None
    if re.search(r"[<>|]|&&|\$\(", text):
        # Contains real shell metacharacters (redirect/pipe/subshell) this
        # module cannot probe without shell=True (never used here -- see
        # module negative-spec). Grammar-illustration spans like
        # "grep foo bar 2>/dev/null" inside a "what IS allowed" explanation
        # are common; shlex would silently mangle the redirect into a bogus
        # positional argument rather than honoring it, producing a false
        # DEAD. Skip rather than mis-probe.
        return None
    if text.startswith("Read(") or text.startswith("Write("):
        return Alternative(AlternativeKind.HARNESS_CAPABILITY, span, text)
    # DoS bound inherited from `_command_tokenizer`, not a local tuning knob:
    # past the ceiling take the same whitespace fallback an unterminated quote
    # already takes.
    if _exceeds_tokenizable_ceiling(text):
        argv = text.split()
    else:
        try:
            argv = shlex.split(text, posix=True)
        except ValueError:
            argv = text.split()
    if not argv:
        return None
    if argv[0] in _SHAPE_NAME_TOKENS:
        # The guard naming ITSELF/its own detected shape, not an offer.
        return None
    if len(argv) == 1 and argv[0].endswith("/"):
        # Latent gap this C2 dispatch exposed: a bare single-token span
        # ending in "/" (e.g. guard_longlived_branch_naming's own "Reserve
        # `migration/` for work the PM has actually authorized as
        # longlived") names a directory/branch-prefix, never an executable
        # -- a trailing slash cannot denote a runnable file on any
        # supported platform, so this is unambiguous, not a guess: not an
        # invocable alternative to grade LIVE/DEAD.
        return None
    if argv[0].startswith("-"):
        # A bare flag with no CLI name in the same span to check --help
        # against -- UNVERIFIABLE by construction (probe_flag), never
        # guessed at. The threaded-cli_name form (`<cli> <flag>`, two
        # tokens, below) is the shape that actually proves anything.
        return Alternative(AlternativeKind.FLAG, span, (None, argv[0]))
    if len(argv) == 2 and argv[1].startswith("-"):
        # `<cli> <flag>` -- a flag asserted to exist on a named CLI. Thread
        # BOTH through as the detail tuple (the Director of Engineering's review, finding 5:
        # probe_flag was never reachable with a cli_name before this) so
        # probe_flag can check the CLI's own --help output rather than
        # reporting UNVERIFIABLE by construction.
        return Alternative(AlternativeKind.FLAG, span, (argv[0], argv[1]))
    if len(argv) == 1:
        if _DOTTED_PYTHON_PATH_RE.match(argv[0]) and "/" not in argv[0]:
            # A dotted internal-module reference mentioned in prose
            # (e.g. "ceremony.scoped_git_commit") alongside a REAL runnable
            # alternative elsewhere in the same message -- not itself an
            # invocable argv[0], so not liveness-checkable as a command.
            return None
        if "_" in argv[0] and "-" not in argv[0]:
            # snake_case Python symbol/function reference (e.g.
            # `session_live(sid)`, `cs_live_session_ids`) documented
            # alongside a real CLI form elsewhere in the message -- this
            # package's real executables are hyphenated
            # (session-liveness-cli, coordinator-safe-commit, ...); an
            # underscored bareword is prose naming an API, not a
            # subprocess-invocable command.
            return None
        return Alternative(AlternativeKind.EXECUTABLE, span, argv)
    return Alternative(AlternativeKind.COMMAND, span, argv)


def extract_alternatives(hso: Dict[str, Any]) -> List[Alternative]:
    """Extract every alternative a guard's emitted ``hookSpecificOutput``
    names, classified into one of the five ``AlternativeKind`` members.
    Returns an empty list for a message that names NO alternative at all
    (a bare policy statement -- not a gate failure, see module negative-
    spec). Raises ``UnclassifiableAlternative`` for alternative-shaped
    signal this function cannot classify (a gate failure)."""
    text = hso.get("permissionDecisionReason") or hso.get("additionalContext") or ""
    alts: List[Alternative] = []

    updated = hso.get("updatedInput")
    if isinstance(updated, dict) and updated.get("command"):
        cmd = updated["command"]
        # DoS bound inherited from `_command_tokenizer`, not a local tuning
        # knob -- same whitespace fallback as an unparseable rewrite.
        if _exceeds_tokenizable_ceiling(cmd):
            argv = cmd.split()
        else:
            try:
                argv = shlex.split(cmd, posix=True)
            except ValueError:
                argv = cmd.split()
        if argv:
            alts.append(Alternative(AlternativeKind.COMMAND, cmd, argv))

    for m in _OVERRIDE_RE.finditer(text):
        alts.append(Alternative(AlternativeKind.OVERRIDE, m.group(0), m.group(0)))

    for m in _READ_CALL_RE.finditer(text):
        alts.append(Alternative(AlternativeKind.HARNESS_CAPABILITY, m.group(0), m.group(0)))

    # Backtick-span classification is restricted to CUE WINDOWS (text
    # following "Use instead:"/"Did you mean"/"Example:"/a mid-sentence
    # "instead", cut at the next paragraph break) -- never the whole
    # message. This is the position-anchoring the Director of Engineering's review required
    # (finding 4): a guard's own shape-name self-reference ("the
    # `grep-via-bash` shell fan-out is denied") sits BEFORE any cue and
    # never enters a window; a Python API reference documented in the same
    # sentence as a cue word still goes through `_classify_backtick_span`'s
    # per-token filters (dotted-path/snake_case/shape-name exclusions),
    # which do the remaining precision work inside the window.
    classified_backtick_spans = set()
    for window in _cue_windows(text):
        for m in _BACKTICK_RE.finditer(window):
            span = m.group(1)
            if span in classified_backtick_spans:
                continue
            if _OVERRIDE_RE.fullmatch(span.strip()):
                continue  # already captured as OVERRIDE above
            alt = _classify_backtick_span(span)
            if alt is not None:
                alts.append(alt)
                classified_backtick_spans.add(span)

    for marker in _HARNESS_CAPABILITY_MARKERS:
        if marker in text:
            alts.append(Alternative(AlternativeKind.HARNESS_CAPABILITY, marker, marker))

    if not alts:
        # No alternative-shaped signal at all -- legitimate for a
        # bare-policy deny (see negative-spec). Only escalate to
        # UnclassifiableAlternative when a cue phrase promised one (the
        # NARROW _ALT_CUE_RE, not the broad _CUE_WINDOW_RE -- a bare
        # mid-sentence "instead" must not manufacture a failure on a
        # message that never actually promised a concrete substitute).
        if _ALT_CUE_RE.search(text):
            for window in _cue_windows(text):
                for m in _INDENTED_CMD_RE.finditer(window):
                    candidate = m.group(1).strip()
                    if candidate and not candidate.startswith(("Subagent:", "Command:", "Denied:", "Reason:")):
                        # DoS bound inherited from `_command_tokenizer`,
                        # not a local tuning knob.
                        if _exceeds_tokenizable_ceiling(candidate):
                            argv = candidate.split()
                        else:
                            try:
                                argv = shlex.split(candidate, posix=True)
                            except ValueError:
                                argv = candidate.split()
                        if argv:
                            alts.append(Alternative(AlternativeKind.COMMAND, candidate, argv))
            if not alts:
                # Last resort, still POSITION-anchored (never a bare
                # backtick/prose scan): a colon immediately introducing an
                # indented block is this package's own convention for "the
                # following lines are the offer" even when the introducing
                # sentence uses neither "Use instead:" nor "Did you mean"
                # (e.g. block_reviewer_bash_outside_allowlist's "...
                # scaffolding their own findings sidecar:\n\n  coordinator-
                # doc-new --type review-findings ..."). Only engaged when a
                # cue phrase was already found SOMEWHERE in the message
                # (so a pure policy statement with an incidental colon
                # elsewhere never triggers this) and ordinary windowed
                # scanning still found nothing.
                for m in _LABELED_INDENT_BLOCK_RE.finditer(text):
                    for line in m.group(1).splitlines():
                        candidate = line.strip()
                        if not candidate or candidate.startswith(("Subagent:", "Command:", "Denied:", "Reason:")):
                            continue
                        # DoS bound inherited from `_command_tokenizer`,
                        # not a local tuning knob.
                        if _exceeds_tokenizable_ceiling(candidate):
                            argv = candidate.split()
                        else:
                            try:
                                argv = shlex.split(candidate, posix=True)
                            except ValueError:
                                argv = candidate.split()
                        if argv:
                            alts.append(Alternative(AlternativeKind.COMMAND, candidate, argv))
            if not alts:
                # Final resort, still cue-gated and still position-anchored
                # (never a bare whole-message backtick scan): every ordinary
                # avenue above found nothing because the ONLY backtick
                # span(s) in the cue window(s) carry an angle-bracket
                # placeholder (e.g. block_noncanonical_branch_creation's own
                # "Use instead: ... e.g. `work/<machine>/2026-08-01`") --
                # `_classify_backtick_span` correctly treats `<`/`>` as an
                # unprobable shell metacharacter for its OWN (unconditional,
                # every-span) main-loop job, so widening that check there
                # would silently reclassify spans in OTHER, already-passing
                # guards' messages (confirmed: check_raw_pid_liveness's own
                # `<sid>`/`<claim_dir>` placeholder spans, which co-occur
                # with a REAL alternative elsewhere in its message and must
                # stay excluded). Scoped here instead, to the narrow case
                # where a cue phrase promised something and literally
                # nothing else in the message classifies -- a templated
                # NAME example is graded HARNESS_CAPABILITY (the same
                # honest, UNVERIFIABLE-by-default probe path already used
                # for `<claude-klabauter-root>`-style templates elsewhere), never a
                # false DEAD (an unresolvable basename) or a hard failure.
                for window in _cue_windows(text):
                    for m in _BACKTICK_RE.finditer(window):
                        candidate = m.group(1).strip()
                        if candidate and _PLACEHOLDER_RE.search(candidate) and not re.search(r"[|]|&&|\$\(", candidate):
                            alts.append(Alternative(AlternativeKind.HARNESS_CAPABILITY, candidate, candidate))
            if not alts:
                raise UnclassifiableAlternative(
                    "message carries an alternative-offering cue phrase but no "
                    "backtick/indented-command/labeled-block/override/harness-"
                    "marker signal this extractor recognizes: %r" % text[:300]
                )
    return alts


# ---------------------------------------------------------------------------
# (c) Liveness probes -- one per kind. Verdicts are three-valued.
# ---------------------------------------------------------------------------


class VerdictStatus(enum.Enum):
    LIVE = "LIVE"
    DEAD = "DEAD"
    UNVERIFIABLE = "UNVERIFIABLE"


@dataclass
class Verdict:
    status: VerdictStatus
    evidence: str


_PLACEHOLDER_RE = re.compile(r"<[^<>]+>")
_BAREWORD_PLACEHOLDER_RE = re.compile(r"\b(MSG|PATTERN|SID|CLAIM_DIR)\b")


def _dummy_substitute(token: str) -> str:
    token = _PLACEHOLDER_RE.sub("altlive-placeholder", token)
    token = _BAREWORD_PLACEHOLDER_RE.sub("altlive-placeholder", token)
    return token


def _resolve_on_path_or_settings_home(name: str) -> Optional[str]:
    """Resolve an executable by bare name on PATH, then under the
    settings-home bin dir (the forwarder convention every coordinator CLI
    outside claude-klabauter's own tree uses) -- never a hardcoded absolute path."""
    found = shutil.which(name)
    if found:
        return found
    settings_home = os.environ.get("COORDINATOR_SETTINGS_HOME") or os.path.join(
        os.path.expanduser("~"), ".coordinator-claude-settings"
    )
    candidate = os.path.join(settings_home, "bin", name)
    if os.name != "nt" and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    if os.name == "nt":
        cmd_candidate = candidate + ".cmd"
        if os.path.isfile(cmd_candidate):
            return cmd_candidate
    return None


def _is_mutating_argv(argv: List[str]) -> bool:
    if not argv:
        return False
    head = os.path.basename(argv[0])
    if head in _MUTATING_VERBS:
        return True
    if head == "git" and len(argv) > 1 and argv[1] in _MUTATING_GIT_SUBCOMMANDS:
        return True
    if head.startswith("sed") and "-i" in argv[1:]:
        return True
    return False


def _probe_python_dash_m(argv: List[str], m_idx: int) -> Verdict:
    """``<interpreter> -m <module> ...`` -- proven live by an import-only
    check (``import <module>``), never by running the module's own
    ``__main__`` (which may perform the very mutating action -- e.g. a
    ceremony commit op -- this module's negative-spec forbids executing
    for real).

    NEGATIVE-SPEC: the ``subprocess.run`` below is a deliberate isolation
    boundary and must NEVER be converted to an in-process ``import``. It
    resolves and probes an interpreter named in agent/reviewer-supplied
    input; the subprocess is the containment for that untrusted input.
    Reason recorded in
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md
    (mechanism: "guard-under-untrusted-input sandboxing")."""
    if m_idx + 1 >= len(argv):
        return Verdict(VerdictStatus.DEAD, "-m flag present with no module name")
    interpreter = argv[0]
    resolved = interpreter if os.path.isabs(interpreter) and os.path.isfile(interpreter) else shutil.which(
        os.path.basename(interpreter)
    )
    if not resolved:
        return Verdict(VerdictStatus.DEAD, "interpreter %r does not resolve on PATH" % interpreter)
    module = argv[m_idx + 1]
    try:
        proc = subprocess.run(
            [resolved, "-c", "import " + module],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SEC,
            **_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Verdict(VerdictStatus.DEAD, "interpreter invocation failed: %s" % exc)
    if proc.returncode != 0:
        return Verdict(VerdictStatus.DEAD, "module %r fails to import:\n%s" % (module, proc.stderr[-500:]))
    return Verdict(VerdictStatus.LIVE, "resolved %s; -m module %r imports cleanly (submodule's own __main__ not executed)" % (resolved, module))


def _probe_python_dash_c(argv: List[str]) -> Verdict:
    """``<interpreter> -c '<script>'`` -- proven live by a syntax-only
    ``compile()`` pass over the script body, per module negative-spec
    (never executes a rewrite payload's real body, which routinely deletes
    files by design).

    NEGATIVE-SPEC: the ``subprocess.run`` below is a deliberate isolation
    boundary and must NEVER be converted to an in-process ``compile()``/
    ``exec()`` call in this interpreter. It executes an agent-supplied
    `-c` script (via a syntax-only compile probe) to test liveness; the
    subprocess is the containment for that untrusted input. Reason
    recorded in
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md
    (mechanism: "guard-under-untrusted-input sandboxing")."""
    if "-m" in argv:
        return _probe_python_dash_m(argv, argv.index("-m"))
    try:
        c_idx = argv.index("-c")
    except ValueError:
        return Verdict(VerdictStatus.UNVERIFIABLE, "argv contains no -c/-m flag to extract a script/module from")
    if c_idx + 1 >= len(argv):
        return Verdict(VerdictStatus.DEAD, "‑c flag present with no script body")
    interpreter = argv[0]
    resolved = interpreter if os.path.isabs(interpreter) and os.path.isfile(interpreter) else shutil.which(
        os.path.basename(interpreter)
    )
    if not resolved:
        return Verdict(VerdictStatus.DEAD, "interpreter %r does not resolve on PATH" % interpreter)
    script = argv[c_idx + 1]
    probe_argv = [
        resolved,
        "-c",
        "compile(open(0, 'r', encoding='utf-8').read(), '<altlive-probe>', 'exec')",
    ]
    try:
        proc = subprocess.run(
            probe_argv, input=script, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SEC, **_NO_WINDOW
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Verdict(VerdictStatus.DEAD, "interpreter invocation failed: %s" % exc)
    if proc.returncode != 0:
        return Verdict(VerdictStatus.DEAD, "script fails to compile:\n%s" % proc.stderr[-500:])
    return Verdict(VerdictStatus.LIVE, "resolved %s; -c script body compiles cleanly" % resolved)


def probe_command(alt: Alternative) -> Verdict:
    """Runs an arbitrary agent-supplied argv in a throwaway temp cwd to
    prove liveness.

    NEGATIVE-SPEC: every ``subprocess.run`` call in this function (directly,
    and transitively via ``_probe_python_dash_c``) is a deliberate isolation
    boundary and must NEVER be converted to an in-process call. The
    argv this function probes is agent/reviewer-supplied and untrusted; the
    subprocess is the containment for it, never merely a latency shortcut.
    Reason recorded in
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md
    (mechanism: "guard-under-untrusted-input sandboxing")."""
    argv: List[str] = alt.detail
    if not argv:
        return Verdict(VerdictStatus.DEAD, "empty argv")
    head = argv[0]
    head_basename = os.path.basename(head)

    if head_basename in ("python", "python3", "py", "pythonw") or head_basename.endswith(("python.exe", "python3.exe")):
        return _probe_python_dash_c(argv)

    resolved = head if (os.path.isabs(head) and os.path.isfile(head)) else _resolve_on_path_or_settings_home(head_basename)
    if not resolved:
        return Verdict(VerdictStatus.DEAD, "argv[0] %r does not resolve on PATH or settings-home bin" % head)

    if _is_mutating_argv(argv):
        if head_basename != "git":
            # `_MUTATING_VERBS` is a small, hand-curated set of standard POSIX
            # utilities (rm/mv/cp/dd/tee) -- resolution on PATH already proves
            # the alternative is real, unlike a `git <subcommand>` where the
            # subcommand itself could be a hallucinated typo. A `--help` round
            # trip adds nothing here and actively misfires on BSD userlands:
            # BSD `rm`/`mv`/`cp` reject the GNU-only `--help` flag with a
            # sysexits.h EX_USAGE (64) exit, which is proof the binary is
            # real and responding, not that it is dead. Trust the resolution
            # step alone for this fixed, non-git verb set.
            return Verdict(
                VerdictStatus.LIVE,
                "mutating verb %r resolved to %r -- proven live via PATH resolution only "
                "(never executed for real, --help skipped: not all POSIX utilities support "
                "it consistently, e.g. BSD rm/mv/cp)" % (argv, resolved),
            )
        help_argv = [resolved, argv[1]] + ["--help"]
        try:
            proc = subprocess.run(help_argv, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SEC, **_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Verdict(VerdictStatus.DEAD, "--help invocation failed: %s" % exc)
        if proc.returncode not in (0, 1):
            return Verdict(
                VerdictStatus.DEAD,
                "mutating verb %r resolved but --help exited %d:\n%s" % (argv, proc.returncode, proc.stderr[-500:]),
            )
        return Verdict(
            VerdictStatus.LIVE,
            "mutating shape %r -- proven live via --help only (never executed for real), exit %d" % (argv, proc.returncode),
        )

    safe_argv = [resolved] + [_dummy_substitute(t) for t in argv[1:]]
    tmp = tempfile.mkdtemp(prefix="altlive-cmd-")
    try:
        try:
            proc = subprocess.run(safe_argv, cwd=tmp, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SEC, **_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Verdict(VerdictStatus.DEAD, "invocation failed: %s" % exc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    stderr_lower = proc.stderr.lower()
    dead_markers = ("command not found", "no such file or directory", "unrecognized option",
                    "unknown option", "modulenotfounderror", "is not recognized as an internal")
    if any(marker in stderr_lower for marker in dead_markers):
        return Verdict(VerdictStatus.DEAD, "stderr indicates the alternative is dead:\n%s" % proc.stderr[-500:])
    if proc.returncode in (0, 1):
        return Verdict(VerdictStatus.LIVE, "executed %r in throwaway cwd, exit %d" % (safe_argv, proc.returncode))
    return Verdict(
        VerdictStatus.UNVERIFIABLE,
        "executed %r, exit %d, no recognized dead-marker in stderr -- ambiguous, not guessed at:\n%s"
        % (safe_argv, proc.returncode, proc.stderr[-500:]),
    )


def probe_executable(alt: Alternative) -> Verdict:
    argv: List[str] = alt.detail
    if not argv:
        return Verdict(VerdictStatus.DEAD, "empty argv")
    return probe_command(alt)  # single-token argv is the same shape as a mutating no-op check


def probe_flag(alt: Alternative) -> Verdict:
    """A flag asserted to exist on a CLI -- proven live by invoking that
    CLI's own ``--help`` and asserting the flag string appears in its
    output. ``alt.detail`` is a ``(cli_name, flag)`` tuple; ``cli_name`` is
    threaded straight from extraction (``_classify_backtick_span``'s
    ``<cli> <flag>`` two-token shape) rather than passed separately, per
    the Director of Engineering's review (finding 5): a bare flag extracted with no accompanying
    CLI name in the same backtick span (``cli_name is None``) is
    UNVERIFIABLE, never guessed at -- this module does not invent a CLI to
    check a flag against.

    NEGATIVE-SPEC: the ``subprocess.run`` below is a deliberate isolation
    boundary and must NEVER be converted to an in-process call. It runs a
    `--help` probe of an agent/reviewer-named binary; the subprocess is the
    containment for that untrusted input. Reason recorded in
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md
    (mechanism: "guard-under-untrusted-input sandboxing")."""
    cli_name, flag = alt.detail
    if not cli_name:
        return Verdict(VerdictStatus.UNVERIFIABLE, "flag %r was named with no CLI in the same span to check --help against" % flag)
    resolved = _resolve_on_path_or_settings_home(cli_name)
    if not resolved:
        return Verdict(VerdictStatus.DEAD, "CLI %r (owner of flag %r) does not resolve" % (cli_name, flag))
    try:
        proc = subprocess.run([resolved, "--help"], capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SEC, **_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Verdict(VerdictStatus.DEAD, "--help invocation failed: %s" % exc)
    haystack = proc.stdout + proc.stderr
    if flag in haystack:
        return Verdict(VerdictStatus.LIVE, "%r --help lists %r" % (resolved, flag))
    return Verdict(VerdictStatus.DEAD, "%r --help does NOT list %r" % (resolved, flag))


#: Committed, hand-maintained capability_id -> {available, hosts, agent_types,
#: note} oracle. Path resolved relative to THIS file (never cwd), per this
#: package's own "scripts self-resolve their own root" convention.
_CAPABILITY_MANIFEST_PATH = Path(__file__).with_name("harness_capability_manifest.json")


@functools.lru_cache(maxsize=1)
def _capability_manifest() -> Dict[str, Dict[str, Any]]:
    """Load and cache the committed harness-capability manifest.

    Returns ``{}`` (never raises) on a missing/malformed file -- an absent
    manifest must degrade to "every capability id is UNVERIFIABLE", the
    same honest gap this probe reported before the manifest existed, never
    a crash on the hot evaluation path. Cached because this is read on
    every HARNESS_CAPABILITY alternative in a single gate run and the file
    does not change mid-run.
    """
    try:
        with _CAPABILITY_MANIFEST_PATH.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    capabilities = raw.get("capabilities")
    return capabilities if isinstance(capabilities, dict) else {}


def _capability_id(guard: Optional[str], detail: Any) -> Optional[str]:
    """Compose the manifest lookup key for a HARNESS_CAPABILITY alternative.

    Deliberately ``<guard>::<marker>``, never the bare marker alone. The
    marker half is prose lifted from ``_HARNESS_CAPABILITY_MARKERS`` (or a
    ``Read(...)``/``Write(...)`` span), and the SAME marker string is
    reused by unrelated guards to describe DIFFERENT things -- e.g.
    ``guard_head_tail_rewrite``'s own advisory text calls its
    ``python3 -c`` subprocess rewrite "in-process, zero extra forks",
    which is a distinct, FALSE claim (a real subprocess is not literally
    in-process) from ``guard_inprocess_search``'s genuine zero-fork
    in-process answerer. Keying by the bare marker alone would grade the
    false claim LIVE the moment the true one is recorded -- exactly the
    inversion this probe's docstring already names as the founding defect.
    Prefixing with the guard that actually fired scopes each manifest
    entry to the ONE claim it was verified against. Returns ``None`` when
    ``guard`` is unknown (no key can be composed; the caller reports
    UNVERIFIABLE rather than guessing at an ambiguous bare-marker key).
    """
    if not guard:
        return None
    return "%s::%s" % (guard, detail)


def probe_harness_capability(alt: Alternative, guard: Optional[str] = None) -> Verdict:
    """A claim about a tool or dispatch the harness offers.

    Looks ``guard``+``alt.detail`` up in the committed capability manifest
    (``harness_capability_manifest.json``, loaded via
    ``_capability_manifest``) via ``_capability_id`` and returns:
      - LIVE when the manifest marks that exact ``<guard>::<marker>`` id
        ``available: true``;
      - DEAD when it is marked ``available: false``;
      - UNVERIFIABLE -- the same honest gap this probe has always
        reported -- when the id is absent from the manifest, or when its
        ``available`` field is ``null``/``"unknown"``, or when ``guard``
        is not supplied.

    This module runs as a subprocess and structurally cannot invoke the
    Read/Write tool, dispatch a subagent, or introspect the live harness
    roster -- so it never independently verifies a capability itself. It
    only ever repeats what the manifest, a human-reviewed static record,
    already asserts. A capability claim with no manifest entry stays
    UNVERIFIABLE forever, by design, until a maintainer adds one.

    A prior revision of this function returned UNVERIFIABLE
    unconditionally, and before that, keyed a substring match ("in-process"
    appearing in both the guard message and an on-disk audit document) as
    corroboration -- the Director of Engineering's review (2026-07-29,
    coordinatoreng-director-1d83280e) caught that this INVERTS the founding
    defect the gate was commissioned to catch: that audit document is a
    REFUTATION ("is factually inaccurate for its ... invoked through Bash,
    not an in-process harness tool"), so co-occurrence was being read as
    corroboration and the exact false claim the gate exists to catch
    graded LIVE. The manifest replaces phrase-matching a prose document
    with an exact, guard-scoped id lookup against a record a human
    actually reviewed and committed -- see ``_capability_id`` for why the
    id is ``<guard>::<marker>``, never the bare marker.
    """
    capability_id = _capability_id(guard, alt.detail)
    if capability_id is None:
        return Verdict(
            VerdictStatus.UNVERIFIABLE,
            "harness-capability claim %r fired with no guard name supplied -- "
            "cannot compose a guard-scoped manifest id, so this probe reports "
            "the honest gap rather than keying on the bare marker alone"
            % str(alt.detail),
        )
    entry = _capability_manifest().get(capability_id)
    if entry is None:
        return Verdict(
            VerdictStatus.UNVERIFIABLE,
            "harness-capability claim %r has no entry for id %r in %s -- "
            "no structured record exists yet for this exact guard+marker "
            "pairing" % (str(alt.detail), capability_id, _CAPABILITY_MANIFEST_PATH.name),
        )
    available = entry.get("available")
    note = entry.get("note", "")
    if available is True:
        return Verdict(VerdictStatus.LIVE, "capability manifest %r: available=true -- %s" % (capability_id, note))
    if available is False:
        return Verdict(VerdictStatus.DEAD, "capability manifest %r: available=false -- %s" % (capability_id, note))
    return Verdict(
        VerdictStatus.UNVERIFIABLE,
        "capability manifest %r: available=%r (not a confirmed true/false) -- %s" % (capability_id, available, note),
    )


def probe_override(alt: Alternative, guard: str, baseline: GuardFireResult) -> Verdict:
    """An advertised ``COORDINATOR_ALLOW_*``/``COORDINATOR_OVERRIDE_*``/
    ``COORDINATOR_DISABLE_*`` escape hatch -- proven live BEHAVIOURALLY,
    by re-firing ``guard``'s own registered trigger with the env var
    patched into ``os.environ`` and asserting the verdict actually
    changes, never by grepping source text.

    the Director of Engineering's review (finding 6) caught two problems with the prior
    source-grep design: (a) every ``check_*`` guard shared ``_dc`` (the
    whole 5,491-line ``dispatch_checks.py``) as its "module", so an
    override advertised by one function scored LIVE if ANY other function
    in the same file happened to read that literal; (b) presence of a
    literal proves nothing about whether the read is on the path that
    actually gates THIS guard's verdict -- a var read inside a dead
    branch passes identically to one that works. Re-firing the trigger
    with the var actually set proves the thing the message promises: that
    setting it changes what the guard does. ``baseline`` is the ALREADY-
    CAPTURED fire result from the same evaluation (never re-derived here,
    per the "capture once, don't re-derive" discipline) so this probe
    costs exactly one extra trigger call, not two.

    Re-fired via ``_call_trigger_isolated``, the same isolated-trigger
    helper ``fire_guard`` uses -- this is the SECOND (and only other) place
    in this module that fires a ``LIVE_TRIGGERS`` entry, so it carries the
    same ``_isolated_session_scope`` guarantee as the baseline fire."""
    env_var: str = alt.detail
    trigger = LIVE_TRIGGERS.get(guard)
    if trigger is None:
        return Verdict(VerdictStatus.UNVERIFIABLE, "no registered trigger for %r to behaviorally re-fire with %r set" % (guard, env_var))

    had_prior = env_var in os.environ
    prior_value = os.environ.get(env_var)
    os.environ[env_var] = "1"
    try:
        patched_envelope = _call_trigger_isolated(trigger)
    except Exception as exc:  # noqa: BLE001 -- isolate: a crash under the override is evidence, not a probe failure
        return Verdict(VerdictStatus.DEAD, "guard crashed with %r=1 set (%s: %s) -- the override does not safely disable it" % (env_var, type(exc).__name__, exc))
    finally:
        if had_prior:
            os.environ[env_var] = prior_value
        else:
            os.environ.pop(env_var, None)

    baseline_decision = None
    if baseline.envelope:
        baseline_decision = baseline.envelope.get("hookSpecificOutput", {}).get("permissionDecision")
    patched_decision = None
    if patched_envelope:
        patched_decision = patched_envelope.get("hookSpecificOutput", {}).get("permissionDecision")

    if patched_envelope is None:
        return Verdict(VerdictStatus.LIVE, "%r=1 makes the guard stand down entirely (baseline decision was %r)" % (env_var, baseline_decision))
    if patched_decision != baseline_decision:
        return Verdict(VerdictStatus.LIVE, "%r=1 changes the verdict (%r -> %r)" % (env_var, baseline_decision, patched_decision))
    return Verdict(
        VerdictStatus.DEAD,
        "%r=1 set and the guard re-fired with an IDENTICAL verdict (%r both times) -- "
        "the guard never reads what its own message advertises, or the read is not on "
        "the path that gates this verdict" % (env_var, baseline_decision),
    )


def probe_alternative(alt: Alternative, guard: Optional[str] = None, baseline: Optional[GuardFireResult] = None) -> Verdict:
    if alt.kind is AlternativeKind.COMMAND:
        return probe_command(alt)
    if alt.kind is AlternativeKind.EXECUTABLE:
        return probe_executable(alt)
    if alt.kind is AlternativeKind.FLAG:
        return probe_flag(alt)
    if alt.kind is AlternativeKind.HARNESS_CAPABILITY:
        return probe_harness_capability(alt, guard)
    if alt.kind is AlternativeKind.OVERRIDE:
        if guard is None or baseline is None:
            return Verdict(VerdictStatus.UNVERIFIABLE, "no guard/baseline supplied to behaviorally check override liveness")
        return probe_override(alt, guard, baseline)
    raise AssertionError("unreachable: unknown AlternativeKind %r" % alt.kind)


# ---------------------------------------------------------------------------
# Top-level per-guard evaluation.
# ---------------------------------------------------------------------------


@dataclass
class GuardEvaluation:
    guard: str
    fire: GuardFireResult
    band: GuardBand
    alternatives: List[Alternative] = field(default_factory=list)
    verdicts: List[Tuple[Alternative, Verdict]] = field(default_factory=list)
    extraction_error: Optional[str] = None


def evaluate_guard(guard: str) -> GuardEvaluation:
    fire = fire_guard(guard)
    ev = GuardEvaluation(guard=guard, fire=fire, band=classify_band(guard))
    if not fire.fired or fire.envelope is None:
        return ev
    hso = fire.envelope.get("hookSpecificOutput", {})
    try:
        ev.alternatives = extract_alternatives(hso)
    except UnclassifiableAlternative as exc:
        ev.extraction_error = str(exc)
        return ev
    for alt in ev.alternatives:
        verdict = probe_alternative(alt, guard=guard, baseline=fire)
        ev.verdicts.append((alt, verdict))
    return ev


def evaluate_all(guards: Optional[List[str]] = None) -> List[GuardEvaluation]:
    names = guards if guards is not None else sorted(LIVE_TRIGGERS.keys())
    return [evaluate_guard(name) for name in names]


def format_report(evaluations: List[GuardEvaluation]) -> str:
    """Per-guard lines (each tagged with its band, so a line can be handed
    over without cross-referencing anything else), followed by a per-band
    DEAD/UNVERIFIABLE breakdown -- the artifact a reader with none of this
    session's context needs to know who owes which fix."""
    lines = []
    for ev in evaluations:
        tag = "[%s]" % ev.band.value
        if not ev.fire.fired:
            lines.append("%s %s: DID NOT FIRE (%s)" % (tag, ev.guard, ev.fire.error or "trigger returned None"))
            continue
        if ev.extraction_error:
            lines.append("%s %s: UNCLASSIFIABLE ALTERNATIVE -- %s" % (tag, ev.guard, ev.extraction_error))
            continue
        if not ev.verdicts:
            lines.append("%s %s: fired, 0 alternatives named" % (tag, ev.guard))
            continue
        for alt, verdict in ev.verdicts:
            lines.append(
                "%s %s: [%s] %s -- %s" % (tag, ev.guard, alt.kind.value, verdict.status.value, verdict.evidence[:200])
            )

    lines.append("")
    lines.append("--- per-band breakdown ---")
    for band in (GuardBand.OURS, GuardBand.PEER):
        band_evs = [ev for ev in evaluations if ev.band is band]
        dead = [(ev, alt, v) for ev in band_evs for alt, v in ev.verdicts if v.status is VerdictStatus.DEAD]
        unverifiable = [(ev, alt, v) for ev in band_evs for alt, v in ev.verdicts if v.status is VerdictStatus.UNVERIFIABLE]
        not_fired = [ev for ev in band_evs if not ev.fire.fired]
        lines.append(
            "%s: %d guard(s) evaluated, %d DEAD, %d UNVERIFIABLE, %d did-not-fire"
            % (band.value, len(band_evs), len(dead), len(unverifiable), len(not_fired))
        )
        for ev, alt, v in dead:
            lines.append("    DEAD: %s [%s] %r -- %s" % (ev.guard, alt.kind.value, alt.raw, v.evidence[:200]))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    evaluations = evaluate_all()
    print(format_report(evaluations))
    dead = [
        (ev.guard, ev.band, alt, v)
        for ev in evaluations
        for alt, v in ev.verdicts
        if v.status is VerdictStatus.DEAD
    ]
    return 1 if dead else 0


if __name__ == "__main__":
    sys.exit(main())
