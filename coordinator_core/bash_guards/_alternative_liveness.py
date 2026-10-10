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
  IMPORTS/PARSES/RECOGNIZES the invocation (`-h`, or a syntax-only
  `compile()` pass over a `-c` script body), never by letting the
  destructive half execute. This is the load-bearing carve-out for
  ``check_destructive_rm``'s `git stash push` alternative and similar.
- Never asks a probed CLI for BROWSER-RENDERED help. `git <verb> --help`
  on Git for Windows finds no ``man.exe``, resolves ``help.format`` to
  ``web``, and hands off to ``git-web--browse``, which launches the
  operator's default browser at a local ``git-<verb>.html`` -- a detached
  GUI process ``capture_output=True`` cannot contain. Every probe here
  therefore uses the terminal-only short form and runs under
  ``_probe_env()``'s no-op browser triple; see that function.
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

Spec backlink: coordinator-content-repo:pln-windows-viability-stop-the-spa-b969d9
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

import ast
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
import textwrap
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
from coordinator_core.bash_guards import block_venv_creation
from coordinator_core.bash_guards import block_worktree_creation
from coordinator_core.bash_guards import block_stash_destruction
from coordinator_core.bash_guards import block_topic_branch
from coordinator_core.bash_guards import block_editor_kill_by_name
from coordinator_core.bash_guards import guard_subagent_heavy_ue_launch
from coordinator_core.bash_guards import guard_heavy_command_admission
from coordinator_core.bash_guards import guard_deploy_dirty_tree
from coordinator_core.bash_guards import block_whole_filesystem_scan
from coordinator_core.bash_guards import block_perforce_submit
from coordinator_core.bash_guards import block_unreal_engine_resave
from coordinator_core.bash_guards import block_worktree_sentinel_creation
from coordinator_core.bash_guards import block_dev_repo_sentinel_removal
from coordinator_core.bash_guards import block_subagent_grant_acquisition
from coordinator_core.bash_guards import block_subagent_guard_grant
from coordinator_core.bash_guards import block_subagent_findings_reject
from coordinator_core.bash_guards import check_raw_pid_liveness
from coordinator_core.bash_guards import guard_powershell_via_bash
from coordinator_core.bash_guards import guard_grep_via_bash
from coordinator_core.bash_guards import guard_head_tail_rewrite
from coordinator_core.bash_guards import guard_inprocess_search
from coordinator_core.bash_guards import guard_multiprobe_banner
from coordinator_core.bash_guards import guard_offer_git_c
from coordinator_core.bash_guards import guard_plumbing_and_loops
from coordinator_core.bash_guards import block_fleet_delegation_creation
from coordinator_core.bash_guards import block_hand_authored_handoff_creation
from coordinator_core.bash_guards import guard_doctrine_surface_bash_write
from coordinator_core.bash_guards import guard_repo_setup_claude_home_refusal
from coordinator_core.bash_guards import guard_host_subagent_bash_ban
from coordinator_core.bash_guards import guard_host_subagent_bash_spawn_shapes
from coordinator_core.bash_guards import p4_verb_fence
from coordinator_core.daily_day import local_day

_PROBE_TIMEOUT_SEC = 10

GENERATES = []


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


_OURS_MODULE_GUARDS = frozenset({"guard_grep_via_bash", "guard_multiprobe_banner", "guard_plumbing_and_loops"})

_OURS_DISPATCH_CHECKS = frozenset(
    {
        "check_find_exec_rewrite",
        "check_grep_via_bash_rewrite",
        "check_sed_range_read_advise",
        "check_cat_heredoc_write_advise",
        "check_heredoc_repo_write_advise",
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


#: Git's own no-op help-browser triple, injected per-probe via `GIT_CONFIG_*`
#: rather than read from `~/.gitconfig`. Env injection is the only form that
#: survives this suite's own home quarantine: `coordinator_core/conftest.py`'s
#: autouse `_quarantine_real_home` repoints HOME/USERPROFILE at a tmpdir and
#: deletes HOMEDRIVE/HOMEPATH, so the operator's global mitigation is INVISIBLE
#: to every git subprocess spawned under pytest -- measured directly: with the
#: quarantine applied, `git config --get web.browser` answers empty/rc=1 on a
#: box where the global triple is set. `browser.noop.cmd` is `eval`-ed by
#: `git-web--browse`, so the value stays free of shell metacharacters (a
#: parenthesis is a hard syntax error there).
_BROWSER_SUPPRESSION_CONFIG: Tuple[Tuple[str, str], ...] = (
    ("help.format", "web"),
    ("web.browser", "noop"),
    ("browser.noop.cmd", "echo not-opening-browser-for:"),
)


def _probe_env() -> Dict[str, str]:
    """This module's environment for every probe subprocess: the ambient
    environment plus a no-op help-browser triple, so no probed CLI can hand a
    help request off to a GUI browser.

    Belt to `-h`'s braces. The `-h` short form in `probe_command` is the fix
    for the ONE shape known to launch a browser (`git <verb> --help`); this
    env is the standing guarantee for every OTHER probe here -- notably
    `probe_flag`, which runs `--help` against an arbitrary agent-named CLI and
    is otherwise one browser-launching CLI away from the same defect.

    Computed per call, never frozen at import: the ambient environment this
    reads is monkeypatched per-test by the quarantine fixture above."""
    env = dict(os.environ)
    env["GIT_CONFIG_COUNT"] = str(len(_BROWSER_SUPPRESSION_CONFIG))
    for i, (key, value) in enumerate(_BROWSER_SUPPRESSION_CONFIG):
        env["GIT_CONFIG_KEY_%d" % i] = key
        env["GIT_CONFIG_VALUE_%d" % i] = value
    return env


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


def _trigger_host_subagent_policy_guard(
    module: Any, policy_key: str, cmd: str
) -> Optional[Dict[str, Any]]:
    """Drive an OPT-IN host-subagent guard by giving it the one thing it
    requires: a repo whose `coordinator.local.md` declares the policy.

    Both `guard_host_subagent_bash_ban` and `guard_host_subagent_bash_spawn_
    shapes` return `None` for any session whose repo has not opted in, so a
    bare command string probes nothing about them -- they were in neither
    registry until 2026-08-30 for exactly that reason.

    The config file is the whole fixture: `_repo_config` walks up from `cwd`
    for `coordinator.local.md` and `_policy_is_deny` scans its frontmatter.
    NO `git init` -- neither guard consults a git root, and the `git`-shaped
    trigger rows in this module are the ones that time out first on a loaded
    box (measured 2026-08-30). A `mkdtemp` plus one small write is the whole
    cost, cleaned up before returning.
    """
    scratch = tempfile.mkdtemp(prefix="altlive-policy-")
    try:
        config = Path(scratch) / "coordinator.local.md"
        config.write_text(
            "---\n%s: deny\n---\n\nAltlive probe fixture.\n" % policy_key,
            encoding="utf-8",newline="\n"
        )
        return module.check(_payload(cmd, agent_id="deadbeef0123", cwd=scratch))
    finally:
        from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise

        rmtree_or_raise(Path(scratch), label="altlive-policy")


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
        with open(os.path.join(tmp, "f.txt"), "w", encoding="utf-8", newline="\n") as fh:
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
    with open(os.path.join(agents_dir, "em-session-id.txt"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(em_sid + "\n")
    sess_dir = os.path.join(git_root, ".git", "coordinator-sessions", em_sid)
    os.makedirs(sess_dir, exist_ok=True)
    with open(os.path.join(sess_dir, "dispatched-agents.txt"), "a", encoding="utf-8", newline="\n") as fh:
        fh.write("%s\t%s\t%s\n" % (agent_id, "altlive-teammate", subagent_type))


def _trigger_destructive_rm() -> Optional[Dict[str, Any]]:
    with _scratch_git_repo() as repo:
        target_dir = os.path.join(repo, "scratch_dir")
        os.makedirs(target_dir)
        with open(os.path.join(target_dir, "u.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("untracked\n")
        return _dc.check_destructive_rm("rm -rf %s" % target_dir, "altlive-probe")


def _trigger_destructive_git_revert() -> Optional[Dict[str, Any]]:
    """The previous UNTRIGGERED
    row recorded a probe of `git revert`, a command this guard does not
    target at all (it gates `checkout`/`restore`/`reset`/`stash`, per its
    own docstring). A load-bearing path (`state/`-rooted, `_is_loadbearing`)
    with an uncommitted tracked edit, then `git reset --hard`, denies
    without needing a peer claim -- the simplest live trigger shape."""
    with _scratch_git_repo() as repo:
        state_dir = os.path.join(repo, "state")
        os.makedirs(state_dir)
        target = os.path.join(state_dir, "important.md")
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("baseline\n")
        _run(["git", "add", "state/important.md"], cwd=repo)
        _run(["git", "commit", "-q", "-m", "seed state file"], cwd=repo)
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("baseline\nuncommitted edit\n")
        return _dc.check_destructive_git_revert("git -C %s reset --hard" % repo, "altlive-probe")


def _trigger_stale_write() -> Optional[Dict[str, Any]]:
    """`check_stale_write`'s deny leg needs a baseline this session itself
    recorded, then disk content that no longer matches it -- the guard
    denies only when BOTH hashes exist and disagree, so a fixture that
    merely writes a file proves nothing (no recorded hash reads as "not
    stale", by that function's own fail-toward-allow posture).

    The whole fixture lives inside the scratch repo, including the touch
    record: `append_touch_claims` writes to `<root>/.git/coordinator-
    sessions/<sid>`, so pointing `root` at the throwaway repo keeps this
    probe out of the real tree's live-session hub. Writing it against the
    real root would mint a session directory that `conftest._no_new_live_
    session_hub_entries` then fails an unrelated test for.
    """
    from coordinator_core.session import touch_record as _tr

    with _scratch_git_repo() as repo:
        repo = os.path.realpath(repo)
        rel = "state/note.md"
        target = os.path.join(repo, "state", "note.md")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("the content this session last read\n")
        _tr.append_touch_claims(
            [rel],
            "altlive-stale-probe",
            repo,
            content_hashes={rel: _tr.compute_content_hash(target) or ""},
            kind=_tr.KIND_READ,
        )
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("the different content now on disk\n")
        return _dc.check_stale_write("echo replacement > %s" % rel, "altlive-stale-probe", repo)


def _trigger_destructive_git_revert_advisory() -> Optional[Dict[str, Any]]:
    """Advisory sibling of `_trigger_destructive_git_revert` above -- same
    scratch repo shape, but the uncommitted edit lands OUTSIDE any
    load-bearing prefix, so this fires the advisory leg (allow +
    additionalContext), never the hard-deny leg."""
    with _scratch_git_repo() as repo:
        target = os.path.join(repo, "f.txt")
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("hello\nuncommitted edit\n")
        return _dc.check_destructive_git_revert_advisory("git -C %s checkout -f" % repo, "altlive-probe")


def _trigger_check_git_commit_safe_commit_advise_amend() -> Optional[Dict[str, Any]]:
    """Key-specific trigger for `check_git_commit_safe_commit_advise`'s
    `COORDINATOR_ALLOW_GIT_COMMIT_AMEND` override key -- see
    `KEY_SPECIFIC_TRIGGERS`'s own docstring for why this exists as a SEPARATE
    row rather than widening `LIVE_TRIGGERS["check_git_commit_safe_commit_
    advise"]` itself.

    A fresh `_scratch_git_repo()` per call (never the real working tree, and
    never dependent on it) whose one committed HEAD carries no `Session-Id:`
    trailer at all -- `_bt_head_commit_amend_provenance`'s `owned` can only
    ever read True from an EXACT trailer match against the probe's own
    `session_id`, and this repo's HEAD was authored entirely outside this
    module's session machinery, so `owned=False` is a property of the FIXTURE
    (a repo this trigger built and controls end to end), never a guess about
    the real tree's current HEAD -- re-runnable identically regardless of
    what commit happens to be at HEAD in the box this suite runs on. Reaches
    the amend gate's deny branch (`--amend`, no `--only`, no explicit
    pathspec) at baseline, which is the shape `probe_override` needs to prove
    `COORDINATOR_ALLOW_GIT_COMMIT_AMEND=1` actually changes."""
    with _scratch_git_repo() as repo:
        cmd = 'git -C %s commit --amend -m "amend probe"' % repo
        return _dc.check_git_commit_safe_commit_advise(cmd, "altlive-probe-amend-nonmatching-session")


def _trigger_plan_body_bash_write() -> Optional[Dict[str, Any]]:
    with _scratch_git_repo() as repo:
        agent_id = "deadbeef0123"
        _make_backpointer(repo, agent_id, "coordinator:executor")
        payload = _payload(
            "sed -i s/x/y/ docs/plans/2026-01-01-foo.md", agent_id=agent_id, cwd=repo,
        )
        return block_subagent_plan_body_bash_write.check(payload)


#: C1 (docs/plans/2026-08-01-branch-creation-seam-guards.md) is
#: hazard-repo-scoped (`_is_hazard_repo(resolve_git_root(cwd))` gates it
#: before its own name predicate runs) -- unlike every guard above this
#: comment, it does not fire from a bare synthetic payload. `_is_hazard_
#: repo` is swapped on the guard's OWN module attribute for the duration of
#: one trigger call, then restored -- never a persistent patch to shipped
#: guard behavior, and never dependent on this MACHINE's real fleet
#: registry: `coordinator_core/conftest.py`'s suite-wide `HOME`/
#: `USERPROFILE` quarantine (autouse) makes the real registry-lookup half of
#: `_is_hazard_repo` unreachable under pytest regardless of which real repo
#: `cwd` names, so pointing `cwd` at this package's own working tree (a
#: hazard repo OUTSIDE pytest, on this dev machine's fleet registry) is
#: insufficient on its own -- confirmed by this row initially firing under a
#: bare `python3 -c` probe and then failing under `pytest` with an identical
#: `cwd`. Mirrors this package's own test suite's injection convention
#: (`monkeypatch.setattr(guard, "_is_hazard_repo", lambda git_root: True)`),
#: just without the `monkeypatch` fixture (this module is a library, not a
#: test) -- manual save/restore in a `try/finally` instead.
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


def _trigger_check_blanket_git_add() -> Optional[Dict[str, Any]]:
    """``check_blanket_git_add`` resolves its hazard-repo git root from
    ``os.getcwd()`` (or a ``-C <dir>`` embedded in ``cmd`` itself, see
    ``_bt_blanket_add_dash_c_cwd``'s own docstring) rather than a
    ``check(payload)``-shaped ``cwd`` field -- it takes a bare ``cmd``
    string, not a payload -- so this trigger drives that resolution via an
    explicit ``git -C <dir> add -A`` rather than relying on whatever
    directory the test runner's own process happens to be in.

    ``_is_hazard_repo`` is swapped on `dispatch_checks`'s own module
    attribute (``_dc._is_hazard_repo``) for the duration of this one call,
    same convention as `_trigger_block_noncanonical_branch_creation` above
    -- a temporary swap on the
    SHIPPED guard's own module, restored in `finally`, never a persistent
    patch. Supersedes the prior `UNTRIGGERED` reason ("target-class gate is
    hardcoded to the operator's real ~/.claude meta-repo"), which stopped
    being true once `_is_hazard_repo` replaced that hardcoded check.

    ``_ALTLIVE_HAZARD_CWD`` is passed to ``-C`` UNQUOTED -- deliberately,
    not merely because it happens to contain no spaces. `check_blanket_
    git_add`'s own per-segment matcher (`seg_cmd = re.sub(r"'[^']*'", " ",
    seg_cmd)`) strips ANY single-quoted span before re-matching the git-add
    shape, a rule meant for quoted commit-message-style TEXT arguments, not
    a quoted `-C` path. Quoting a backslash-bearing Windows path here (e.g.
    via `shlex.quote`, which quotes on seeing a bare backslash) blanks the
    `-C` value out from under `_GIT_ADD_GLOBAL_OPT_RE`'s own `-C\\s+\\S+`
    match, which then greedily consumes the FOLLOWING `add` token as -C's
    value instead and the whole gate silently stops matching -- confirmed
    live during this dispatch (quoted form returns None, unquoted fires).
    """
    orig_hazard = _dc._is_hazard_repo
    _dc._is_hazard_repo = lambda git_root: True
    try:
        with _scratch_git_repo() as repo:
            return _dc.check_blanket_git_add("git -C %s add -A" % repo, "altlive-probe")
    finally:
        _dc._is_hazard_repo = orig_hazard


def _trigger_p4_verb_fence() -> Optional[Dict[str, Any]]:
    """``p4_verb_fence.check`` gates entirely on ``_is_p4_gated(cwd)`` (a
    zero-spawn ``coordinator.local.md`` marker walk + ``is_p4_repo``) before
    any command-text parsing runs, so a deterministic fire needs that gate
    forced open rather than a real p4-mirrored scratch repo -- same
    module-attribute swap-and-restore convention as
    ``_trigger_check_blanket_git_add`` above, on this guard's own
    ``_is_p4_gated`` rather than ``dispatch_checks._is_hazard_repo``.
    ``p4 submit`` is D6's own always-denied verb, independent of any
    changelist/session state."""
    orig_gated = p4_verb_fence._is_p4_gated
    p4_verb_fence._is_p4_gated = lambda cwd: True
    try:
        return p4_verb_fence.check(_payload("p4 submit -c 1", agent_id=None))
    finally:
        p4_verb_fence._is_p4_gated = orig_gated


def _trigger_check_destructive_git_clean() -> Optional[Dict[str, Any]]:
    """Drives ``git clean`` against a scratch repo holding one untracked
    load-bearing file, so the fire never depends on the ambient tree."""
    with _scratch_git_repo() as repo:
        os.makedirs(os.path.join(repo, "state"))
        with open(os.path.join(repo, "state", "orientation_cache.md"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("untracked\n")
        return _dc.check_destructive_git_clean(
            "git -C %s clean -fdx" % shlex.quote(repo.replace("\\", "/")), "altlive-probe"
        )


def _trigger_deploy_dirty_tree() -> Optional[Dict[str, Any]]:
    """An untracked file makes the scratch tree dirty; a clean tree passes."""
    with _scratch_git_repo() as repo:
        with open(os.path.join(repo, "app.ts"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("x\n")
        return guard_deploy_dirty_tree.check(_payload("firebase deploy", agent_id=None, cwd=repo))


def _trigger_doctrine_surface_bash_write() -> Optional[Dict[str, Any]]:
    """Overwrites an EXISTING ``CLAUDE.md`` in a scratch repo: creating a
    missing one is a carve-out and stays silent, so the fire needs the file
    present in the payload cwd."""
    with _scratch_git_repo() as repo:
        with open(os.path.join(repo, "CLAUDE.md"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("existing\n")
        return guard_doctrine_surface_bash_write.check(
            _payload("echo x > CLAUDE.md", agent_id=None, cwd=repo),
            ["CLAUDE.md", "MEMORY.md", "coordinator.local.md", "AGENTS.md"],
        )


def _trigger_block_perforce_submit() -> Optional[Dict[str, Any]]:
    """``block_perforce_submit.check`` denies only on an armed box (machine
    registry names a Perforce server); force ``_armed`` open for the probe."""
    orig_armed = block_perforce_submit._armed
    block_perforce_submit._armed = lambda: True
    try:
        return block_perforce_submit.check(_payload("p4 submit -c 1", agent_id=None))
    finally:
        block_perforce_submit._armed = orig_armed


LIVE_TRIGGERS: Dict[str, Callable[[], Optional[Dict[str, Any]]]] = {
    "check_no_verify": lambda: _dc.check_no_verify('git commit --no-verify -m "x"', "altlive-probe"),
    "check_runaway_find": lambda: _dc.check_runaway_find("find / -name '*.py'", "altlive-probe"),
    "check_destructive_git_clean": _trigger_check_destructive_git_clean,
    "check_destructive_rm": _trigger_destructive_rm,
    "check_destructive_git_revert": _trigger_destructive_git_revert,
    "check_destructive_git_revert_advisory": _trigger_destructive_git_revert_advisory,
    "check_stale_write": _trigger_stale_write,
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
    "check_heredoc_repo_write_advise": lambda: _dc.check_heredoc_repo_write_advise(
        "python3 - <<'ALTLIVE_EOF'\n"
        "import pathlib\n"
        'pathlib.Path("altlive_probe_target.txt").write_text("hi")\n'
        "ALTLIVE_EOF",
        "altlive-probe",
        None,
        os.path.dirname(_pkg.__file__),
    ),
    "check_git_commit_safe_commit_advise": lambda: _dc.check_git_commit_safe_commit_advise(
        'git commit -m "fix the thing"', "altlive-probe"
    ),
    "check_multiprobe_banner_rewrite": lambda: _dc.check_multiprobe_banner_rewrite(
        'echo "=== SESSION FACTS ==="; pwd; whoami; git status --short', "altlive-probe"
    ),
    "check_blanket_git_add": _trigger_check_blanket_git_add,
    "p4_verb_fence": _trigger_p4_verb_fence,
    "check_head_tail_plumbing_rewrite": lambda: guard_head_tail_rewrite.check_head_tail_plumbing_rewrite(
        "find . -name '*.py' | head -n 5", "altlive-probe"
    ),
    "block_worktree_creation": lambda: block_worktree_creation.check(
        _payload("git worktree add ../foo", agent_id=None)
    ),
    "block_venv_creation": lambda: block_venv_creation.check(
        _payload("python -m venv .venv", agent_id=None)
    ),
    "block_stash_destruction": lambda: block_stash_destruction.check(
        _payload("git stash drop", agent_id=None)
    ),
    "block_topic_branch": lambda: block_topic_branch.check(
        _payload("git checkout -b topic/altlive-probe", agent_id=None)
    ),
    "block_editor_kill_by_name": lambda: block_editor_kill_by_name.check(
        _payload("taskkill /IM UnrealEditor.exe", agent_id=None)
    ),
    "guard_subagent_heavy_ue_launch": lambda: guard_subagent_heavy_ue_launch.check(
        _payload("UnrealEditor-Cmd.exe Proj.uproject -run=Cook")
    ),
    "guard_heavy_command_admission": lambda: guard_heavy_command_admission.check(
        _payload("tsc --noEmit")
    ),
    "guard_deploy_dirty_tree": _trigger_deploy_dirty_tree,
    "block_whole_filesystem_scan": lambda: block_whole_filesystem_scan.check(
        _payload("find / -name altlive-probe", agent_id=None)
    ),
    "block_unreal_engine_resave": lambda: block_unreal_engine_resave.check(
        _payload("UnrealEditor-Cmd Proj.uproject -run=ResavePackages", agent_id=None)
    ),
    "block_perforce_submit": _trigger_block_perforce_submit,
    "block_subagent_stash_creation": lambda: block_subagent_stash_creation.check(
        _payload(" ".join(["git", "stash", "push"]), agent_type="coordinator:executor")
    ),
    "block_worktree_sentinel_creation": lambda: block_worktree_sentinel_creation.check(
        _payload("touch .coordinator-override-worktree-guard", agent_id=None)
    ),
    "block_approval_sentinel_creation": lambda: block_approval_sentinel_creation.check_ungated(
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
    # The three rows below closed a registry gap this gate caught on
    # 2026-08-30: each module has a top-level `check()` and was in neither
    # `LIVE_TRIGGERS` nor `UNTRIGGERED`, so its alternatives were never
    # probed for liveness at all. Every trigger was measured against its own
    # guard before being written here.
    "block_fleet_delegation_creation": lambda: block_fleet_delegation_creation.check(
        _payload("touch fleet-delegation.json", agent_id=None)
    ),
    "block_hand_authored_handoff_creation": lambda: block_hand_authored_handoff_creation.check(
        _payload("echo x > state/handoffs/hand-authored.md", agent_id=None)
    ),
    "guard_repo_setup_claude_home_refusal": lambda: guard_repo_setup_claude_home_refusal.check(
        _payload(
            "python3 -m coordinator_core.install.scaffold_structure --root "
            + os.path.join(os.path.expanduser("~"), ".claude").replace(chr(92), "/"),
            agent_id=None,
        )
    ),
    # `governed_surfaces` is a REQUIRED positional the live caller
    "guard_doctrine_surface_bash_write": _trigger_doctrine_surface_bash_write,
    # named untriggerable: the gate's UNTRIGGERED pin is a SUPPRESSION
    # ceiling, and a guard that can actually be exercised belongs above it.
    "guard_host_subagent_bash_ban": lambda: _trigger_host_subagent_policy_guard(
        guard_host_subagent_bash_ban, "subagent_bash_policy", "rg TODO"
    ),
    # A LOOP shape, not a bare search: this guard DECLINES (returns None) on
    # anything `guard_inprocess_search` can answer in-process, so `rg TODO`
    # and friends probe nothing here even with the policy on -- measured
    # 2026-08-30 against this same fixture. A per-item loop is the shape its
    # deny actually exists for.
    "guard_host_subagent_bash_spawn_shapes": lambda: _trigger_host_subagent_policy_guard(
        guard_host_subagent_bash_spawn_shapes,
        "subagent_bash_spawn_shapes",
        "for f in *.py; do wc -l $f; done",
    ),
    "check_raw_pid_liveness": lambda: check_raw_pid_liveness.check(
        _payload("k" + "ill -0 $PID", agent_id=None)
    ),
    "guard_inprocess_search": lambda: guard_inprocess_search.check(
        _payload(
            'grep -rn "TODO" coordinator_core/search/',
            agent_id=None,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(_pkg.__file__))),
        ),
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
    "block_subagent_grant_acquisition": lambda: block_subagent_grant_acquisition.check(
        _payload(
            'python3 -m coordinator_core.session.claude_md_grant grant pm "note"'
        )
    ),
    # `agent_id` present (the default `_payload()` value) is sufficient to
    # fire this identity-gated hard-deny -- see `block_subagent_guard_grant.
    # check`'s own "IDENTITY-GATE POSTURE" docstring section: it denies on
    # the raw presence of `agent_id` alone, never on further resolution.
    # `-m coordinator_core.session.em_guard_grant grant` mirrors this
    # guard's sibling row above (`block_subagent_grant_acquisition`, the
    # near-identical `claude_md_grant` guard this module was ported from),
    # substituting the gated module path and dropping the `pm` positional
    # (this guard's `_GATED_SUBCOMMANDS` check reads only the first token
    "block_subagent_findings_reject": lambda: block_subagent_findings_reject.check(
        _payload("python3 -m coordinator_core.ops.review_findings_ledger reject x")
    ),
    "block_subagent_guard_grant": lambda: block_subagent_guard_grant.check(
        _payload(
            'python3 -m coordinator_core.session.em_guard_grant grant "note"'
        )
    ),
    "guard_powershell_via_bash": lambda: guard_powershell_via_bash.check(
        _payload(
            'powershell.exe -NoProfile -Command "$p=Get-Process -Id 1"',
            agent_id=None,
        )
    ),
    "block_noncanonical_branch_creation": _trigger_block_noncanonical_branch_creation,
}


#: Per-``(guard_name, override_env_var)`` triggers for the rare case where a
#: guard advertises TWO (or more) independently-gated override keys that are
#: only REACHABLE via different input shapes -- ``LIVE_TRIGGERS`` holds
#: exactly one trigger per guard NAME (see that dict's own comment and every
#: pinned test resting on that shape: ``test_every_module_guard_is_
#: registered``, the ``EXPECTED_UNTRIGGERED``/``EXPECTED_UNVERIFIABLE_COUNTS``/
#: ``EXPECTED_LIVE_FLOORS`` pins), so a guard whose second key needs a
#: differently-shaped command to reach its own gate cannot be proven live by
#: re-firing the ONE registered trigger with that key's var set -- the
#: alternate code path is never entered, and the key scores DEAD regardless
#: of whether it is genuinely reachable.
#:
#: Root cause this closes: ``check_git_commit_safe_commit_advise`` gates a
#: bare non-amend commit behind ``COORDINATOR_ALLOW_GIT_COMMIT_BARE`` and a
#: ``git commit --amend`` whose HEAD is not provably this session's behind
#: ``COORDINATOR_ALLOW_GIT_COMMIT_AMEND`` -- disjoint input shapes. The
#: guard's ``LIVE_TRIGGERS`` entry is bare-shaped (``'git commit -m "fix the
#: thing"'``); re-firing it with ``COORDINATOR_ALLOW_GIT_COMMIT_AMEND=1`` set
#: can never change anything (the amend branch is never entered), so
#: ``probe_override`` scored a genuinely live key DEAD. This registry adds an
#: amend-shaped trigger for exactly that ``(guard, env_var)`` pair -- see
#: ``_trigger_check_git_commit_safe_commit_advise_amend``.
#:
#: NEGATIVE-SPEC -- this is NOT a widening of ``LIVE_TRIGGERS`` and must
#: never become one:
#: - Scoped to keys on DISJOINT INPUT SHAPES only. A guard whose single
#:   registered trigger already reaches every override key it advertises has
#:   no business here; adding a row for such a key would only ever turn a
#:   correct DEAD into a false LIVE by construction (a second, redundant
#:   trigger cannot disprove liveness, only fail to prove it) -- see
#:   ``TestMetaGateProvenToFail``'s dedicated meta-test for a trigger that
#:   does NOT read its own var and must still score DEAD even when
#:   registered here.
#: - Never a substitute for fixing a genuinely dead override. A key with no
#:   reachable code path at all stays DEAD under a correctly-written
#:   key-specific trigger -- this registry only supplies the INPUT SHAPE a
#:   key needs to be exercised; it asserts nothing about the OUTCOME.
#: - `LIVE_TRIGGERS` itself, `fire_guard`, the baseline fire in
#:   `evaluate_guard`, and every registration/ratchet test enumerated above
#:   are untouched by this dict's existence -- `probe_override` is the ONLY
#:   consumer, and only when a `(guard, env_var)` pair has a row here.
KEY_SPECIFIC_TRIGGERS: Dict[Tuple[str, str], Callable[[], Optional[Dict[str, Any]]]] = {
    (
        "check_git_commit_safe_commit_advise",
        "COORDINATOR_ALLOW_GIT_COMMIT_AMEND",
    ): _trigger_check_git_commit_safe_commit_advise_amend,
}

#: Overrides read ONLY as an inline per-command prefix (`KEY=reason git ...`),
#: never from the environment: `probe_override` re-fires these with the
#: prefix prepended instead of patching `os.environ`, which such a guard
#: deliberately ignores. Each callable takes the prefix (`"KEY=reason "`).
INLINE_PREFIX_TRIGGERS: Dict[Tuple[str, str], Callable[[str], Optional[Dict[str, Any]]]] = {
    ("block_topic_branch", "COORDINATOR_OVERRIDE_TOPIC_BRANCH"): lambda prefix: block_topic_branch.check(
        _payload(prefix + "git checkout -b topic/altlive-probe", agent_id=None)
    ),
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


# ---------------------------------------------------------------------------
# write_guards/ discovery extension (chunk C9, agent-facing-messages-not-
# apology plan) -- this module previously enumerated ONLY bash_guards/ (via
# discover_dispatch_check_names/discover_module_guard_names above), so
# nothing gate-verified that a write_guards/ guard's named alternative
# stayed reachable. `discover_write_guard_names` reuses
# `write_guards.engine`'s own `pkgutil.iter_modules([_PKG_DIR])` +
# `importlib.import_module` walk (via its public `discover_guard_names()`)
# rather than re-implementing it -- same walk this module's docstring
# points at, one copy, not a parallel reimplementation that could drift
# from the engine's own CLASS/MATCHERS/check filtering.
# ---------------------------------------------------------------------------


def discover_write_guard_names() -> List[str]:
    """Every ``coordinator_core/write_guards/`` module the engine itself
    would load (valid ``CLASS``/``MATCHERS`` plus a callable top-level
    ``check``) -- the write_guards/ analogue of ``discover_module_guard_
    names`` above. A module that fails to import is silently excluded here
    (mirrors ``write_guards.engine.discover_guard_names``'s own
    ``import_failed`` split); that failure has its own dedicated CI signal
    in ``write_guards/tests/test_guard_registry_manifest.py`` and is not
    this gate's concern -- this gate is about ALTERNATIVE liveness for
    guards that DO load, not import health."""
    from coordinator_core.write_guards import engine as _write_guards_engine

    names, _import_failed = _write_guards_engine.discover_guard_names()
    return sorted(names)


def _trigger_validate_frontmatter_schema_advisory() -> Optional[Dict[str, Any]]:
    """Fires the warn-mode schema-validation leg with a `state/handoffs/*.md`
    write carrying no frontmatter at all -- the simplest deterministic
    `build_violation_payload_advisory` trigger, needing no coordinator-content-repo
    sibling checkout (the schema-validation leg matches purely off
    claude-klabauter's own vendored `_VENDORED_SCHEMAS_DIR`; `_load_doe_registry()`
    fails open when the DoE root is unresolvable, per that guard's own
    module docstring). Scratch git repo, never the real working tree --
    same pattern as this module's own `_scratch_git_repo` bash_guards
    triggers above."""
    from coordinator_core.write_guards import (
        validate_frontmatter_schema_advisory as _wg_advisory,
    )

    with _scratch_git_repo() as repo:
        repo = os.path.realpath(repo)
        handoffs_dir = os.path.join(repo, "state", "handoffs")
        os.makedirs(handoffs_dir, exist_ok=True)
        rel_path = "state/handoffs/altlive-probe.md"
        with open(os.path.join(handoffs_dir, "altlive-probe.md"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("placeholder\n")
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": rel_path,
                "content": "no frontmatter here, on purpose\n",
            },
            "cwd": repo,
        }
        return _wg_advisory.check(payload)


#: write_guards/ analogue of `LIVE_TRIGGERS` above -- kept as a SEPARATE
#: dict (not merged into `LIVE_TRIGGERS`) so this extension never touches
#: the bash_guards-scoped registry-integrity tests
#: (`test_every_dispatch_check_is_registered`/`test_every_module_guard_is_
#: registered`) or their pinned `EXPECTED_UNTRIGGERED`/`_UNTRIGGERED_PINNED_
#: MAX` ratchets, per this dispatch's explicit "never relax an existing
#: pin" instruction. `fire_guard`/`evaluate_guard` still work unmodified
#: against these keys: `fire_guard` looks a name up in `LIVE_TRIGGERS`
#: directly, so the gate test below calls the trigger through this dict
#: itself rather than routing through `fire_guard`.
WRITE_GUARD_LIVE_TRIGGERS: Dict[str, Callable[[], Optional[Dict[str, Any]]]] = {
    "validate_frontmatter_schema_advisory": _trigger_validate_frontmatter_schema_advisory,
}

#: write_guards/ analogue of `UNTRIGGERED` above. This chunk's own copy
#: change touched exactly one write_guards module
#: (`validate_frontmatter_schema_advisory`, wired into `WRITE_GUARD_LIVE_
#: TRIGGERS` above); constructing a hermetic, non-mutating, hazard-repo-
#: independent live trigger for each of the other 41 write_guards modules
#: (each with its own path-shape/frontmatter-shape/repo-state
#: precondition -- see e.g. `block_cutover_phase_hand_edit`'s STRICT-mode
#: phase-flip precondition, `guard_settings_json_write`'s settings-repo
#: precondition) is out of THIS chunk's measured, narrowly-scoped remit
#: (see this dispatch's own "MEASURED REALITY" framing -- do not go
#: hunting for a large sweep the measurement did not ask for). Discovery
#: itself is NOT narrowed: `discover_write_guard_names()` still walks the
#: whole package, so a FUTURE write_guards module or alternative-signal
#: change is still counted here, not silently invisible -- only its
#: trigger construction is deferred, and deferred LOUDLY (this dict, not
#: a bare `discovered - registered` no-op) rather than never gate-tracked
#: at all.
_WRITE_GUARD_UNTRIGGERED_REASON = (
    "Hermetic live trigger not yet constructed for this write_guards module "
    "(chunk C9, agent-facing-messages-not-apology plan, 2026-08-12) -- "
    "discovery covers it (discover_write_guard_names), a live-fire trigger "
    "does not yet. Close by adding a WRITE_GUARD_LIVE_TRIGGERS row, not by "
    "deleting this entry."
)
WRITE_GUARD_UNTRIGGERED: Dict[str, str] = {
    name: _WRITE_GUARD_UNTRIGGERED_REASON
    for name in discover_write_guard_names()
    if name not in WRITE_GUARD_LIVE_TRIGGERS
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
    needed here.

    The latch marker lands in a private temp directory, never the real
    repo's `.git/coordinator-sessions/`: a phantom `altlive-probe-<hex>`
    directory there, however briefly, reads as a live peer session to every
    real session's liveness scan and to a concurrent xdist worker's hub-litter
    check -- cleanup after the fact cannot win that race."""
    prior = os.environ.get(_SESSION_SCOPED_ENV_VAR)
    fresh_sid = "altlive-probe-" + uuid.uuid4().hex
    latch_root = Path(tempfile.mkdtemp(prefix="altlive-latch-"))
    real_latch_path = guard_inprocess_search._latch_path
    os.environ[_SESSION_SCOPED_ENV_VAR] = fresh_sid
    guard_inprocess_search._latch_path = (
        lambda _cwd, sid: latch_root / sid / guard_inprocess_search._LATCH_MARKER_NAME
    )
    try:
        yield
    finally:
        guard_inprocess_search._latch_path = real_latch_path
        if prior is None:
            os.environ.pop(_SESSION_SCOPED_ENV_VAR, None)
        else:
            os.environ[_SESSION_SCOPED_ENV_VAR] = prior
        from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise

        rmtree_or_raise(Path(latch_root), label="alternative-liveness latch root")


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
    next guard that grows the same kind of session-scoped latch.

    Looks up ``guard`` in ``LIVE_TRIGGERS`` first, then
    ``WRITE_GUARD_LIVE_TRIGGERS`` (the write_guards/ discovery extension,
    chunk C9) -- the two registries share disjoint key namespaces (bash_
    guards module/function names vs. write_guards module names), so no
    caller-visible ambiguity is introduced by checking both."""
    trigger = LIVE_TRIGGERS.get(guard) or WRITE_GUARD_LIVE_TRIGGERS.get(guard)
    if trigger is None:
        raise KeyError(
            "%r is not in LIVE_TRIGGERS or WRITE_GUARD_LIVE_TRIGGERS "
            "(check UNTRIGGERED/WRITE_GUARD_UNTRIGGERED instead)" % guard
        )
    try:
        envelope = _call_trigger_isolated(trigger)
    except Exception as exc:  # noqa: BLE001 -- isolate one guard's crash from the rest
        return GuardFireResult(guard=guard, fired=False, envelope=None, error="%s: %s" % (type(exc).__name__, exc))
    return GuardFireResult(guard=guard, fired=envelope is not None, envelope=envelope, error=None)


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


#: 2026-08-11 (guard-messages-point-to-docs-never-name plan, chunk C1):
#: `operator_override_note`'s own 2026-08-11 C2 reshape stopped
#: interpolating the env-var name into its rendered output at all (see that
#: function's own docstring), so this regex now finds ZERO matches in the
#: overwhelming majority of real guard messages -- a guard whose ONLY
#: offered alternative is its override key would silently drop out of
#: `extract_alternatives`'s OVERRIDE detection with no gate turning red
#: (the exact "vacuous, not red" failure this module's own charter treats
#: as worse than a test failing). Kept, unmodified, as a belt for any
#: HAND-WRITTEN mention of a bare env var that still slips into rendered
#: text outside the builder (the class `test_no_handwritten_override_
#: clauses.py` polices separately) -- but the OVERRIDE alternative this
#: module actually verifies liveness for is now primarily sourced from the
#: guard's own call-site ARGUMENT, never the render alone. See
#: `_source_override_alternatives` below, and `evaluate_guard`'s merge of
#: the two sources.
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
#: DEFINED IN ``_advisory_dedupe``, re-exported here, and the direction of
#: that dependency is load-bearing rather than stylistic. This module runs
#: ``discover_write_guard_names()`` at import time (the module-level
#: comprehension below), which imports ``write_guards.engine`` and through it
#: the whole ``coordinator_core.ops`` registry -- measured at 480-710ms of
#: process time. ``_advisory_dedupe.terse_alternative_text`` needs ONLY these
#: two values, and it runs on the PreToolUse hot path every time an advisory
#: repeat-fires, so importing this module to reach them charged the entire
#: registry import to DR-344's budget on the fleet's most-fired guard. Homing
#: them in the leaf module lets the hot path read them without ever loading
#: this one. Do not move them back, and do not add a module-scope import of
#: anything heavy to ``_advisory_dedupe`` to compensate.
from coordinator_core.bash_guards._advisory_dedupe import (  # noqa: E402
    _CUE_WINDOW_MAX_CHARS,
    _CUE_WINDOW_RE,
)


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


def _split_alternative_text(text: str) -> List[str]:
    """Tokenize an alternative string this module extracted from a guard's own
    message, preserving Windows path separators.

    `shlex.split(text, posix=True)` treats a backslash as an ESCAPE, so
    ``git -C C:\\claude-klabauter\\coordinator_core status`` tokenizes its path as
    ``X:claude_klabautercoordinator_core`` -- a path that cannot exist, which
    made the alternative execute, fail with "cannot change to", and grade DEAD.
    Disabling `escape` keeps POSIX quote handling (a quoted multi-word argument
    is still one token) while leaving backslashes literal; verified
    byte-identical to the previous behavior on every non-backslash shape this
    module encounters.

    NEGATIVE-SPEC: deliberately NOT routed through the shared
    `_command_tokenizer`. That module parses the CALLER's original command under
    this package's own quote-handling contract and carries the SAME posix-escape
    defect for its own reasons; converging them is a change to every
    fail-closed guard's tokenization, which is owned elsewhere (see
    `state/audits/2026-08-07-bash-guard-tokenizer-eats-windows-path-separators.md`).
    This helper's scope is the throwaway alternative strings extracted here."""
    if _exceeds_tokenizable_ceiling(text):
        return text.split()
    lex = shlex.shlex(text, posix=True)
    lex.whitespace_split = True
    lex.escape = ""
    try:
        return list(lex)
    except ValueError:
        return text.split()


def _classify_backtick_span(span: str) -> Optional[Alternative]:
    text = span.strip()
    if not text:
        return None
    if text in _BARE_METACHAR_TOKENS or not re.search(r"[A-Za-z0-9]", text):
        return None
    if re.search(r"[<>|]|&&|\$\(", text):
        return None
    if text.startswith("Read(") or text.startswith("Write("):
        return Alternative(AlternativeKind.HARNESS_CAPABILITY, span, text)
    argv = _split_alternative_text(text)
    if not argv:
        return None
    if argv[0] in _SHAPE_NAME_TOKENS:
        return None
    if len(argv) == 1 and argv[0].endswith("/"):
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
            return None
        if "_" in argv[0] and "-" not in argv[0]:
            return None
        return Alternative(AlternativeKind.EXECUTABLE, span, argv)
    return Alternative(AlternativeKind.COMMAND, span, argv)


_INDENTED_RUN_RE = re.compile(r"(?:^[ \t]{2,}\S[^\n]*(?:\n|\Z))+", re.MULTILINE)
_NON_OFFER_PREFIXES = ("Subagent:", "Command:", "Denied:", "Reason:")
#: First tokens of a line that continues a script body (a rewrite's
#: `python3 -c` source or shell control flow), never a command on its own.
_BODY_LINE_HEADS = frozenset(
    {
        "import", "from", "for", "while", "if", "elif", "else", "try", "except",
        "finally", "with", "def", "class", "return", "print", "pass", "fi", "do",
        "done", "then", "esac", "case", "}", ")",
    }
)


def _offer_argv(candidate: str) -> Optional[List[str]]:
    """argv for a self-contained command line, or None when the line cannot
    stand alone (unbalanced quote, or a script-body keyword head)."""
    if _exceeds_tokenizable_ceiling(candidate):
        return candidate.split()
    try:
        argv = shlex.split(candidate, posix=True)
    except ValueError:
        return None
    if not argv or argv[0] in _BODY_LINE_HEADS:
        return None
    return argv


def _inside_open_quote(prefix: str) -> bool:
    """True when ``prefix`` leaves a shell quote open, so the text that follows
    is the body of a quoted payload (a rewrite's ``python3 -c '...'``). A prefix
    past the tokenizer ceiling reads as open, so it is never graded."""
    if _exceeds_tokenizable_ceiling(prefix):
        return True
    try:
        shlex.split(prefix, posix=True)
    except ValueError:
        return True
    return False


def _indented_offers(text: str) -> List[Alternative]:
    """Plain indented commands a cue window offers. A run of consecutive
    indented lines is graded line by line only when every line is a
    self-contained command and no quote is open where the run starts; either
    failing marks the run as a rewrite body, which is never graded."""
    offers: List[Alternative] = []
    seen = set()
    for window in _cue_windows(text):
        for run in _INDENTED_RUN_RE.finditer(window):
            if _inside_open_quote(window[: run.start()]):
                continue
            lines = [ln.strip() for ln in run.group(0).splitlines() if ln.strip()]
            lines = [ln for ln in lines if not ln.startswith(_NON_OFFER_PREFIXES)]
            argvs = [_offer_argv(ln) for ln in lines]
            if not lines or any(argv is None for argv in argvs):
                continue
            for ln, argv in zip(lines, argvs):
                if ln not in seen:
                    seen.add(ln)
                    offers.append(Alternative(AlternativeKind.COMMAND, ln, argv))
    return offers


def extract_alternatives(hso: Dict[str, Any], *, override_route_known: bool = False) -> List[Alternative]:
    """Extract every alternative a guard's emitted ``hookSpecificOutput``
    names, classified into one of the five ``AlternativeKind`` members.
    Returns an empty list for a message that names NO alternative at all
    (a bare policy statement -- not a gate failure, see module negative-
    spec). Raises ``UnclassifiableAlternative`` for alternative-shaped
    signal this function cannot classify (a gate failure).

    ``override_route_known`` -- default ``False``, so every caller that does not
    know a call-site override route (notably ``_firing_shape.py``, which must
    stay guard-agnostic and purely textual) sees the raw-line fallback below.
    ``evaluate_guard`` passes ``True`` when it has already resolved a real
    OVERRIDE route for the guard from the call-site argument
    (``_source_override_alternatives``); the raw-line fallback then stays off,
    because it would otherwise grade each line of a multi-line rewrite
    (`guard_plumbing_and_loops`'s python3 -c body) as its own command. Plain
    one-command indented offers are still extracted under a cue, via
    ``_indented_offers``, which skips rewrite bodies."""
    text = hso.get("permissionDecisionReason") or hso.get("additionalContext") or ""
    alts: List[Alternative] = []

    updated = hso.get("updatedInput")
    if isinstance(updated, dict) and updated.get("command"):
        cmd = updated["command"]
        argv = _split_alternative_text(cmd)
        if argv:
            alts.append(Alternative(AlternativeKind.COMMAND, cmd, argv))

    for m in _OVERRIDE_RE.finditer(text):
        alts.append(Alternative(AlternativeKind.OVERRIDE, m.group(0), m.group(0)))

    for m in _READ_CALL_RE.finditer(text):
        alts.append(Alternative(AlternativeKind.HARNESS_CAPABILITY, m.group(0), m.group(0)))

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

    if override_route_known:
        # A known OVERRIDE route (sourced from the call site) stands in for
        # the render-derived match that once kept `alts` non-empty, so the
        # raw-line fallback below stays off. Plain indented offers are still
        # graded; rewrite bodies are skipped by `_indented_offers`.
        if _ALT_CUE_RE.search(text):
            alts.extend(_indented_offers(text))
        return alts

    if not alts:
        # No alternative-shaped signal at all -- legitimate for a
        # bare-policy deny (see negative-spec). Only escalate to
        # UnclassifiableAlternative when a cue phrase promised one (the
        # NARROW _ALT_CUE_RE, not the broad _CUE_WINDOW_RE -- a bare
        # mid-sentence "instead" must not manufacture a failure on a
        # message that never actually promised a concrete substitute).
        if _ALT_CUE_RE.search(text):
            alts.extend(_indented_offers(text))
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
                    cues = [c.end() for c in _ALT_CUE_RE.finditer(text, 0, m.start())]
                    if cues and _inside_open_quote(text[cues[-1] : m.start()]):
                        continue
                    for line in m.group(1).splitlines():
                        candidate = line.strip()
                        if not candidate or candidate.startswith(("Subagent:", "Command:", "Denied:", "Reason:")):
                            continue
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
                # for `<claude-klabauter-live-root>`-style templates elsewhere), never a
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


#: Windows system binaries that share a bare name with a POSIX utility the
#: guards name in their alternatives. A guard's alternative runs under bash,
#: where the name means the POSIX tool; the System32 ``find.exe`` is a text
#: searcher that rejects POSIX flags.
_WINDOWS_NAME_COLLISIONS = frozenset({"find", "sort"})


def _posix_twin_of_windows_builtin(name: str, found: str) -> Optional[str]:
    """The Git for Windows ``usr/bin`` twin of ``name`` when ``found`` is the
    colliding System32 binary; None when ``found`` is not that binary or no
    Git for Windows install sits above ``git`` on PATH."""
    stem = os.path.splitext(os.path.basename(name))[0].lower()
    if stem not in _WINDOWS_NAME_COLLISIONS:
        return None
    system_dir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    if os.path.normcase(os.path.dirname(found)) != os.path.normcase(system_dir):
        return None
    git = shutil.which("git")
    if not git:
        return None
    for parent in Path(git).resolve().parents:
        twin = parent / "usr" / "bin" / (stem + ".exe")
        if twin.is_file():
            return str(twin)
    return None


def _resolve_on_path_or_settings_home(name: str) -> Optional[str]:
    """Resolve an executable by bare name on PATH, then under the
    settings-home bin dir (the forwarder convention every coordinator CLI
    outside claude-klabauter's own tree uses) -- never a hardcoded absolute path."""
    found = shutil.which(name)
    if found and os.name == "nt":
        found = _posix_twin_of_windows_builtin(name, found) or found
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
            encoding="utf-8",
            errors="replace",
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
            probe_argv, input=script, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=_PROBE_TIMEOUT_SEC, **_NO_WINDOW
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
        # `-h`, never `--help`. Git for Windows ships no `man.exe`, so
        # `git <verb> --help` resolves `help.format` to `web` and hands off to
        # `git-web--browse`, which launches the operator's default browser at a
        # local `git-<verb>.html`. That browser is a detached GUI process, so
        # neither `capture_output=True` nor `_NO_WINDOW` contains it, and this
        # branch fires on every guard advisory naming a mutating git
        # alternative -- observed as dozens of tabs a minute during a suite run.
        # `-h` prints usage to the terminal and never reaches `git-web--browse`.
        help_argv = [resolved, argv[1], "-h"]
        try:
            proc = subprocess.run(
                help_argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_PROBE_TIMEOUT_SEC,
                env=_probe_env(),
                **_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Verdict(VerdictStatus.DEAD, "-h invocation failed: %s" % exc)
        # Measured on Git for Windows 2.x: every subcommand in
        # `_MUTATING_GIT_SUBCOMMANDS` exits 129 under `-h` with its usage block
        # on STDOUT, while an unrecognized subcommand exits 1 with "is not a
        # git command" on stderr. Accepting {0, 129} rather than the previous
        # {0, 1} is therefore a STRICTER gate, not a looser one: a hallucinated
        # subcommand used to grade LIVE on its rc=1 and now correctly grades
        # DEAD, which is the discrimination this probe exists to provide.
        if proc.returncode not in (0, 129):
            return Verdict(
                VerdictStatus.DEAD,
                "mutating verb %r resolved but -h exited %d (expected 0 or 129; rc=1 means "
                "git did not recognize the subcommand):\n%s"
                % (argv, proc.returncode, (proc.stdout + proc.stderr)[-500:]),
            )
        return Verdict(
            VerdictStatus.LIVE,
            "mutating shape %r -- proven live via -h only (never executed for real), exit %d" % (argv, proc.returncode),
        )

    safe_argv = [resolved] + [_dummy_substitute(t) for t in argv[1:]]
    tmp = tempfile.mkdtemp(prefix="altlive-cmd-")
    try:
        try:
            proc = subprocess.run(safe_argv, cwd=tmp, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=_PROBE_TIMEOUT_SEC, **_NO_WINDOW)
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
    return probe_command(alt)


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
        # `--help` is correct here -- this probe must read the CLI's own help
        # TEXT to assert the flag appears in it, which `-h` does not reliably
        # render for every CLI. The browser hazard is closed by `_probe_env()`
        # instead: `cli_name` is agent-supplied, so this is the one probe that
        # can invoke an ARBITRARY binary's `--help`, and the no-op browser
        # triple is what keeps a browser-launching one from opening a window.
        proc = subprocess.run(
            [resolved, "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PROBE_TIMEOUT_SEC,
            env=_probe_env(),
            **_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Verdict(VerdictStatus.DEAD, "--help invocation failed: %s" % exc)
    haystack = proc.stdout + proc.stderr
    if flag in haystack:
        return Verdict(VerdictStatus.LIVE, "%r --help lists %r" % (resolved, flag))
    return Verdict(VerdictStatus.DEAD, "%r --help does NOT list %r" % (resolved, flag))


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
    same ``_isolated_session_scope`` guarantee as the baseline fire.

    ``KEY_SPECIFIC_TRIGGERS`` (see that dict's own docstring) is consulted
    FIRST, keyed on ``(guard, env_var)`` -- for a guard advertising two
    override keys reachable only via different input shapes, the caller-
    supplied ``baseline`` (captured from ``LIVE_TRIGGERS[guard]``, a
    DIFFERENT command shape) is not a meaningful comparison point for THIS
    key, so a key-specific trigger's own baseline is captured here (env var
    explicitly unset, same isolation/save-restore discipline as the
    set-and-refire call below) rather than reusing the caller's. Falls back
    to ``LIVE_TRIGGERS.get(guard)`` + the caller-supplied ``baseline``
    unchanged when no key-specific row exists -- the pre-existing behavior
    for every guard with a single override key."""
    env_var: str = alt.detail
    inline_trigger = INLINE_PREFIX_TRIGGERS.get((guard, env_var))
    if inline_trigger is not None:
        return _probe_inline_prefix_override(env_var, inline_trigger, baseline)
    had_prior = env_var in os.environ
    prior_value = os.environ.get(env_var)

    key_trigger = KEY_SPECIFIC_TRIGGERS.get((guard, env_var))
    if key_trigger is not None:
        trigger = key_trigger
        os.environ.pop(env_var, None)
        try:
            own_baseline_envelope = _call_trigger_isolated(trigger)
        except Exception as exc:  # noqa: BLE001 -- a crash while unset is evidence, not a probe failure
            if had_prior:
                os.environ[env_var] = prior_value
            return Verdict(
                VerdictStatus.DEAD,
                "guard crashed capturing its own key-specific baseline (%r unset) "
                "(%s: %s) -- cannot prove this override's liveness" % (env_var, type(exc).__name__, exc),
            )
        finally:
            if had_prior:
                os.environ[env_var] = prior_value
            else:
                os.environ.pop(env_var, None)
        baseline_decision = None
        if own_baseline_envelope:
            baseline_decision = own_baseline_envelope.get("hookSpecificOutput", {}).get("permissionDecision")
    else:
        trigger = LIVE_TRIGGERS.get(guard)
        if trigger is None:
            return Verdict(VerdictStatus.UNVERIFIABLE, "no registered trigger for %r to behaviorally re-fire with %r set" % (guard, env_var))
        baseline_decision = None
        if baseline.envelope:
            baseline_decision = baseline.envelope.get("hookSpecificOutput", {}).get("permissionDecision")

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


def _probe_inline_prefix_override(
    env_var: str, trigger: Callable[[str], Optional[Dict[str, Any]]], baseline: GuardFireResult
) -> Verdict:
    baseline_decision = None
    if baseline.envelope:
        baseline_decision = baseline.envelope.get("hookSpecificOutput", {}).get("permissionDecision")
    try:
        patched_envelope = _call_trigger_isolated(lambda: trigger("%s=altlive-probe " % env_var))
    except Exception as exc:  # noqa: BLE001 -- a crash under the override is evidence, not a probe failure
        return Verdict(VerdictStatus.DEAD, "guard crashed with inline %s=... (%s: %s)" % (env_var, type(exc).__name__, exc))
    patched_decision = None
    if patched_envelope:
        patched_decision = patched_envelope.get("hookSpecificOutput", {}).get("permissionDecision")
    if patched_envelope is None or patched_decision != baseline_decision:
        return Verdict(VerdictStatus.LIVE, "inline %s=... changes the verdict (%r -> %r)" % (env_var, baseline_decision, patched_decision))
    return Verdict(
        VerdictStatus.DEAD,
        "inline %s=... prefixed and the guard re-fired with an IDENTICAL verdict (%r)" % (env_var, baseline_decision),
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


# never from the rendered message. See `_OVERRIDE_RE`'s own comment above

_MAX_OVERRIDE_SOURCE_DEPTH = 3


def _resolve_guard_callable(guard: str) -> Optional[Callable]:
    """Resolve a `LIVE_TRIGGERS`/registry key (a `dispatch_checks`-family
    function name, or a `block_*`/`guard_*`/`check_*` MODULE name) to the
    actual callable whose source `_source_override_env_vars` should scan --
    never a lambda from `LIVE_TRIGGERS` itself (those are throwaway
    fixture-construction closures, not the guard's own logic)."""
    for module in _DISPATCH_CHECK_SOURCE_MODULES:
        fn = getattr(module, guard, None)
        if inspect.isfunction(fn) and getattr(fn, "__module__", None) == module.__name__:
            return fn
    try:
        module = importlib.import_module("coordinator_core.bash_guards." + guard)
    except Exception:  # noqa: BLE001 -- a guard name with no importable module resolves to None
        return None
    fn = getattr(module, "check", None)
    return fn if callable(fn) else None


def _resolve_static_str(node: ast.AST, module: Any) -> Optional[str]:
    """Best-effort static resolution of an `operator_override_note` call's
    first positional argument to a concrete string: a plain literal, or a
    reference to a same-module constant already bound to a string (the
    `_OVERRIDE_ENV = "COORDINATOR_..."`-shaped constant every real guard in
    this package already declares). Declines (returns `None`) rather than
    guess at anything else -- a dynamically-computed argument is simply not
    sourced, not approximated."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        val = getattr(module, node.id, None)
        if isinstance(val, str):
            return val
    return None


def _source_override_env_vars(fn: Callable, _seen: Optional[set] = None, _depth: int = 0) -> List[str]:
    """Every `operator_override_note(env_var, ...)` call-site argument this
    module can statically resolve inside `fn`'s own body OR a same-module
    helper it calls (depth-limited call-graph walk, mirroring
    `test_override_route_inventory.py`'s `_same_module_call_graph_calls_
    override_note`) -- the C1 fix: the override-route liveness gate's ONE
    source of truth for "what env var does this guard advertise", now that
    `operator_override_note`'s C2 reshape removed that name from the
    rendered message entirely. Best-effort and silent on a resolution
    miss (an unfoldable argument, an unparseable source) -- a guard that
    cannot be sourced this way simply contributes no OVERRIDE alternative,
    same as one that genuinely names none."""
    if _seen is None:
        _seen = set()
    if fn in _seen or _depth > _MAX_OVERRIDE_SOURCE_DEPTH:
        return []
    _seen.add(fn)
    try:
        src = textwrap.dedent(inspect.getsource(fn))
        tree = ast.parse(src)
    except (OSError, TypeError, SyntaxError):
        return []
    module = inspect.getmodule(fn)
    found: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
        if name != "operator_override_note" or not node.args:
            continue
        val = _resolve_static_str(node.args[0], module)
        if val:
            found.append(val)
    if module is not None:
        for name in set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", src)):
            candidate = getattr(module, name, None)
            if inspect.isfunction(candidate) and inspect.getmodule(candidate) is module:
                found.extend(_source_override_env_vars(candidate, _seen, _depth + 1))
    # A thin wrapper that lazily imports its body from a sibling guard module
    # (kept out of dispatch_checks.py by the line ratchet) names its override
    # there, so follow function-local `from coordinator_core.bash_guards.X
    # import f` into that module.
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("coordinator_core.bash_guards.")
        ):
            continue
        try:
            sibling = importlib.import_module(node.module)
        except Exception:
            continue
        for alias in node.names:
            if alias.name == "operator_override_note" or not re.search(
                r"\b%s\s*\(" % re.escape(alias.asname or alias.name), src
            ):
                continue
            candidate = getattr(sibling, alias.name, None)
            if inspect.isfunction(candidate):
                found.extend(_source_override_env_vars(candidate, _seen, _depth + 1))
    return found


def _source_override_alternatives(guard: str) -> List[Alternative]:
    """`guard`'s OVERRIDE alternatives, sourced from its own check
    function's call-site argument to `operator_override_note` -- the
    replacement for scraping `_OVERRIDE_RE` out of the rendered message
    (C1). Deduplicated by env-var value (a guard calling the builder more
    than once with the same key names one route, not two)."""
    fn = _resolve_guard_callable(guard)
    if fn is None:
        return []
    seen: set = set()
    alts: List[Alternative] = []
    for env_var in _source_override_env_vars(fn):
        if env_var in seen:
            continue
        seen.add(env_var)
        alts.append(Alternative(AlternativeKind.OVERRIDE, env_var, env_var))
    return alts


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
    source_overrides = _source_override_alternatives(guard)
    try:
        ev.alternatives = extract_alternatives(hso, override_route_known=bool(source_overrides))
    except UnclassifiableAlternative as exc:
        ev.extraction_error = str(exc)
        return ev
    # C1 merge: OVERRIDE alternatives sourced from the guard's own call-site
    # argument (never vacuous now that the render carries no key name at
    # all), deduped against anything `extract_alternatives` already found
    # in the rendered text (belt for a hand-written mention outside the
    # builder -- see `_OVERRIDE_RE`'s own comment).
    already = {a.detail for a in ev.alternatives if a.kind is AlternativeKind.OVERRIDE}
    for alt in source_overrides:
        if alt.detail not in already:
            ev.alternatives.append(alt)
            already.add(alt.detail)
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
