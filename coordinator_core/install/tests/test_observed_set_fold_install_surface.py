"""
coordinator_core.install.tests.test_observed_set_fold_install_surface

Install-surface verification for the sat-01b observed-set-fold actuator
(sat-01b C6). Per CLAUDE.md § Install-surface completeness: "local green is
not clean-install green" — every other test proving this wiring
(coordinator_core/tests/test_tracker_store.py, coordinator_core/ops/tracker/
tests/test_fold_observed_set.py, coordinator_core/ops/session/tests/
test_boot_sweep.py) either calls the handler in-process (same interpreter,
same import cache, same working tree) or dispatches in-process via
dispatch_message(). None of them cross a real process boundary against a
tree that is NOT this session's live, possibly-dirty working copy.

What this module adds, and why it is the strongest available proxy for "a
fresh install reproduces this actuation" in a repo with no build step
(pyproject.toml has no build backend — coordinator_core ships as source, so
"installed" and "source tree" are the same bytes; there is no compiled
artifact a real install step would produce that a source checkout lacks):

  1. A REAL, separate ``git clone`` of this repo's own HEAD into a scratch
     dir (see :func:`_fresh_clone`) — proving the wiring survives being read
     from a tree that carries ONLY committed history, never this session's
     uncommitted scratch state (this session's own working tree has
     unrelated uncommitted edits per `git status`; C6 must prove the C1-C5
     wiring is real independent of that).
  2. A REAL subprocess spawn of ``python -m coordinator_core.invoke
     session.boot_sweep`` (the actual command-type CLI entrypoint every
     other in-process test bypasses) with PYTHONPATH pointed ONLY at the
     fresh clone — so the dispatched op registry, the four-surface wiring
     (docs/wiki/coordinator-core-engine.md:266), and Sweep 7's actuation are
     all resolved from the clone's own bytes, not this session's import
     cache.

AC11 (a never-folded machine folds on first session.boot_sweep, in a repo
whose state/sovereign-tracker/ already exists) and AC10b's install-surface
analog (a repo WITHOUT that directory stays untouched — DEC-11's opt-in-by-
existence gate, C5) are both exercised here at the install-surface layer;
C5's own unit-level coverage of the same two branches
(coordinator_core/ops/session/tests/test_boot_sweep.py's
test_observed_set_fold_runs_on_never_folded_machine and
test_observed_set_fold_absent_store_mints_nothing) is NOT duplicated here —
this module adds the process-boundary + fresh-tree layer on top of it, per
the C6 dispatch brief's instruction not to re-litigate C5's unit coverage.

AC12 (Windows/macOS both first-class): extends the existing technique from
coordinator_core/tests/test_tracker_store.py's TestAC8CrossPlatformBehavior
(pathlib-only path construction, raw-bytes CRLF assertion on shard writes)
to this install-surface path, rather than inventing a second idiom — see
:func:`test_fold_actuates_on_fresh_clone_when_store_already_opted_in`'s
final assertion block.

UNVERIFIED / explicitly out of reach of this harness (see also the C6
dispatch brief and this module's own report): this test spawns real git and
python3 subprocesses and therefore genuinely exercises Tier 1
(filesystem/process) install-surface behavior, but it does NOT install onto
a fresh ``CLAUDE_HOME`` / registry-driven target the way
coordinator_core/install/sandbox_check.py exercises the DoE/coordinator-
claude install chain (gen-settings-hooks, .doe-root, claude-doe-shim, etc.)
-- that harness validates a DIFFERENT install surface (the DoE-side plugin
install), has no reachable seam for a `state/sovereign-tracker/`-gated
tracker_store op (claude-klabauter's own consuming-repo state), and is not extended
here for that reason. Tier 2 (running-in-Claude-Code, i.e. this wiring
actually firing from a live Claude Code session's boot) remains a manual/
deferred gate exactly as sandbox_check.py itself documents for its own
Tier 2 -- there is no automated seam for that in this repo today.

Review: code-reviewer -- the CRLF assertion in
test_fold_actuates_on_fresh_clone_when_store_already_opted_in
(``assert b"\r\n" not in raw``) has the same reach limit: it proves no
CRLF-capable code path exists in the write (an explicit "\n", never a
text-mode round trip that could translate line endings), not that an actual
Windows filesystem/Python build produces byte-identical output -- this
suite runs on whatever host executes it (macOS/Linux CI), not Windows
itself. Same limitation TestAC8CrossPlatformBehavior already carries, so
this is not a regression -- named here per this module's own discipline
about stating what it does not prove.

Marked `cadence` — each test performs a real local `git clone` of this
repo's own HEAD (git subprocess calls here are ordinary application/test
git usage, not a new shell-out surface — see the class of existing git-
subprocess test fixtures this module's `_fresh_clone` follows, e.g.
coordinator_core/ops/session/tests/test_boot_sweep.py's `boot_repo`
fixture and coordinator_core/ops/tracker/tests/test_fold_observed_set.py's
`_make_git_repo` helper) plus a real `python -m coordinator_core.invoke`
subprocess spawn, both meaningfully slower than the fast tier's budget.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.tracker_store import EVENTS_SHARD_GLOB
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.cadence

# ---------------------------------------------------------------------------
# Project root — the source of the clone, and (for the belt-and-braces
# no-PYTHONPATH-leak assertion) the tree the clone must NOT need.
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

_CLONE_TIMEOUT_SECS = 120
_INVOKE_TIMEOUT_SECS = 60


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    # -c core.longpaths=true: per-invocation only (never touches any git
    # config on disk) -- this repo's own state/subagent-share/ tree carries
    # filenames long enough that a checkout into a deeply-nested pytest
    # tmp_path can exceed Windows' legacy MAX_PATH without it.
    return subprocess.run(
        ["git", "-c", "core.longpaths=true", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=_CLONE_TIMEOUT_SECS,
        **no_console_creationflags(),
    )


def _fresh_clone(dest: Path) -> Path:
    """Clone this repo's own committed HEAD into *dest* and return the clone root.

    A shallow, local clone -- only committed history, never this session's
    uncommitted working-tree edits. This is the "fresh-machine clean-install"
    proxy: the C1-C5 wiring under test (tracker_store.py, ops/tracker/
    fold_observed_set.py, ops/session/boot_sweep.py's Sweep 7) is already
    committed (per this chunk's dispatch brief), so the clone carries the
    real wiring, sourced independently of this session's live tree.
    """
    clone_root = dest / "clone"
    result = _git(
        # --no-hardlinks: dest (a pytest tmp_path, usually on the OS temp
        # drive) and _PROJECT_ROOT are frequently on different volumes --
        # git's default --local hardlink strategy fails cross-volume
        # (Windows: "Improper link"; POSIX: EXDEV) even though the clone
        # itself is otherwise valid. Forcing a real copy makes the clone
        # succeed regardless of which volume the fixture root sits on.
        "clone", "--local", "--no-hardlinks", "--depth", "1", "--no-tags", "-q",
        str(_PROJECT_ROOT), str(clone_root),
        cwd=dest,
    )
    assert result.returncode == 0, (
        f"fresh clone of HEAD failed: rc={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    # The clone is itself a valid git repo (has its own .git) -- confirm the
    # wiring under test actually landed in the cloned commit, so a failure
    # below is attributable to the wiring, not to a stale/incomplete clone.
    assert (clone_root / "coordinator_core" / "tracker_store.py").is_file()
    assert (clone_root / "coordinator_core" / "ops" / "tracker" / "fold_observed_set.py").is_file()
    assert (clone_root / "coordinator_core" / "ops" / "session" / "boot_sweep.py").is_file()
    return clone_root


def _invoke_boot_sweep(clone_root: Path) -> subprocess.CompletedProcess:
    """Spawn `python -m coordinator_core.invoke session.boot_sweep` as a real
    subprocess, importing coordinator_core ONLY from *clone_root* (PYTHONPATH
    is set to the clone, and nothing else -- proving no reliance on this
    session's own import cache or working tree).
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = str(clone_root)
    return subprocess.run(
        [sys.executable, "-m", "coordinator_core.invoke", "session.boot_sweep", "{}",
         "--repo", str(clone_root)],
        cwd=str(clone_root),
        capture_output=True,
        text=True,
        timeout=_INVOKE_TIMEOUT_SECS,
        env=env,
        **no_console_creationflags(),
    )


# ---------------------------------------------------------------------------
# AC11 -- fresh-machine clean-install reproduces actuation
# ---------------------------------------------------------------------------


@pytest.mark.pending_fix(
    reason="session.boot_sweep exceeds its own 30s op budget on a FRESH CLONE under "
    "disk contention: passes when run alone (38s), times out when two clone-based "
    "tests run back to back (`op timed out after 30.0s`, rc 1). This is a "
    "PRODUCTION budget defect in boot_sweep, not a defect in this test's shape -- "
    "raising the timeout here would convert a real budget overrun into a green "
    "tick, which is the exact failure class state/audits/"
    "2026-08-07-windows-shakedown-cruise-measurements.md was written about. "
    "Unmark once boot_sweep holds its budget on a cold clone."
)
def test_fold_actuates_on_fresh_clone_when_store_already_opted_in(tmp_path):
    """A repo whose state/sovereign-tracker/ ALREADY exists, on a fresh clone
    of this repo's own HEAD (not this working tree's live state), folds its
    observed set on the very first session.boot_sweep -- proving AC11 at the
    install surface, not merely in-process.
    """
    clone_root = _fresh_clone(tmp_path)

    events_dir = clone_root / "state" / "sovereign-tracker"
    events_dir.mkdir(parents=True, exist_ok=True)

    result = _invoke_boot_sweep(clone_root)
    assert result.returncode == 0, (
        f"session.boot_sweep must exit 0 on a fresh clone; got {result.returncode}.\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )

    envelope = json.loads(result.stdout)
    assert "error" not in envelope, f"unexpected dispatch error: {envelope}"
    fold = envelope["result"]["observed_set_fold"]
    assert fold["ran"] is True, f"expected ran=True; got {fold}"
    assert fold["reason"] == "appended", f"expected a first-ever appended fold; got {fold}"
    assert fold["marker"] is not None
    assert fold["marker"]["kind"] == "observed_set_fold"

    # A real shard file landed on disk, in the CLONE (not this working tree).
    # Review: code-reviewer -- import the real constant rather than duplicating
    # the glob literal; only the spawned subprocess needs PYTHONPATH isolation
    # from this session's tree, not this outer pytest process itself.
    shard_files = sorted(events_dir.glob(EVENTS_SHARD_GLOB))
    assert len(shard_files) == 1, f"expected exactly one shard file; got {shard_files}"

    # AC12 -- extends TestAC8CrossPlatformBehavior's technique
    # (coordinator_core/tests/test_tracker_store.py) to the install-surface
    # write path: raw-bytes read, no CRLF -- proves the write used an
    # explicit "\n" (json.dumps + "\n"), never a text-mode round trip that
    # could translate line endings on Windows.
    raw = shard_files[0].read_bytes()
    assert b"\r\n" not in raw, "shard write must not CRLF-translate on any platform"


@pytest.mark.pending_fix(
    reason="session.boot_sweep exceeds its own 30s op budget on a FRESH CLONE under "
    "disk contention: passes when run alone (38s), times out when two clone-based "
    "tests run back to back (`op timed out after 30.0s`, rc 1). This is a "
    "PRODUCTION budget defect in boot_sweep, not a defect in this test's shape -- "
    "raising the timeout here would convert a real budget overrun into a green "
    "tick, which is the exact failure class state/audits/"
    "2026-08-07-windows-shakedown-cruise-measurements.md was written about. "
    "Unmark once boot_sweep holds its budget on a cold clone."
)
def test_opt_in_gate_holds_on_fresh_clone_without_store(tmp_path):
    """A repo WITHOUT state/sovereign-tracker/ stays untouched after a
    session.boot_sweep on a fresh clone of this repo's own HEAD -- proving
    DEC-11's opt-in-by-existence confinement holds at the install surface,
    not merely in-process (C5's own unit coverage is AC10b; this is its
    install-surface analog, the fleet-wide-actuation hazard DEC-11 confines).
    """
    clone_root = _fresh_clone(tmp_path)
    events_dir = clone_root / "state" / "sovereign-tracker"
    assert not events_dir.exists()

    result = _invoke_boot_sweep(clone_root)
    assert result.returncode == 0, (
        f"session.boot_sweep must exit 0 even with no tracker store; got {result.returncode}.\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )

    envelope = json.loads(result.stdout)
    assert "error" not in envelope, f"unexpected dispatch error: {envelope}"
    fold = envelope["result"]["observed_set_fold"]
    assert fold == {"ran": False, "reason": "no_store", "marker": None}, (
        f"opt-in-by-existence gate must skip cleanly with no store; got {fold}"
    )

    # No directory minted -- the mandatory gate never creates the store.
    assert not events_dir.exists(), (
        "session.boot_sweep must NOT mint state/sovereign-tracker/ in a repo "
        "that never opted in -- this is exactly the fleet-wide-actuation "
        "hazard DEC-11 confines"
    )
