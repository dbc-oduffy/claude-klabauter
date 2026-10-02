"""test_checked_repo_resolver.py -- C1 test surface for
`coordinator/bin/lib/repo_identity.py::resolve_checked_repo_root`.

Spec backlink: pln-one-checked-resolver-for-the-c-035d59
§ C1 / AC1-AC7.

C1's own module (no other chunk in this plan writes to this file -- each
chunk names its own test module, see the plan's "Dispatch shape" note).

AC7: every wrong-repo/verdict case below is constructed with REAL files on
disk (a fabricated `CLAUDE_CONFIG_DIR/sessions/` directory, real
directories with real `.git` markers for the plausibility band) -- never
by monkeypatching `compute_repo_identity_gate`'s (or the checked
resolver's) own return value. `CLAUDE_CONFIG_DIR` is set to a real
per-test tmp directory so `coordinator_core.session.harness_registry.
registry_dir()` resolves it exactly as it would on a live host; `CLAUDE_
PID` is likewise set to a real value, but the accompanying psutil
name-match leg (`coordinator_core.session.core._resolve_claude_pid_from_
env`'s "the pid resolves to a process literally named 'claude'" check)
cannot be constructed as a real fixture inside a test process running
under a different name -- that ONE leg is monkeypatched, mirroring
`coordinator_core/pickup_assemble/tests/test_repo_identity_gate.py`'s own
documented carve-out and the established `session/tests/test_harness_
registry.py::TestSelfRecord` convention it cites.

Every test calls `clear_repo_identity_memo()` in `setUp` -- without it,
two tests sharing a `(resolved_root, sid)` pair against different on-disk
registries would be served the first test's cached verdict, silently
passing AC7's wrong-repo assertion while asserting nothing (see the
module's own docstring "Memoization" section).
"""

from __future__ import annotations

import ast
import json
import os
import re
import time
import unittest
from pathlib import Path

import pytest
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
_LIB_DIR = os.path.join(_BIN_DIR, "lib")
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from cc_invoke import require_engine_on_path  # noqa: E402

_ENGINE_ROOT = require_engine_on_path(__file__)

from lib.repo_identity import (  # noqa: E402
    _VERDICT_EXPLICIT,
    _VERDICT_MATCH,
    _VERDICT_MISMATCH,
    _VERDICT_UNRESOLVED,
    clear_repo_identity_memo,
    resolve_checked_repo_root,
)
from coordinator_core.session import harness_registry as hr  # noqa: E402


def _epoch_to_filetime_ticks(epoch: float) -> int:
    return int((epoch + hr._FILETIME_EPOCH_OFFSET_SEC) * hr._FILETIME_TICKS_PER_SEC)


def _write_registry_record(sessions_dir: Path, filename: str, session_id: str, pid: int, cwd, epoch=None):
    sessions_dir.mkdir(parents=True, exist_ok=True)
    if epoch is None:
        epoch = time.time() - 60
    payload = {
        "sessionId": session_id,
        "pid": pid,
        "procStart": _epoch_to_filetime_ticks(epoch),
        "cwd": str(cwd),
    }
    (sessions_dir / filename).write_text(json.dumps(payload), encoding="utf-8")
    return epoch


def _make_repo(root: Path) -> None:
    """Real directory + real `.git` marker -- sufficient for the
    plausibility band; no actual `git init` needed."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(parents=True, exist_ok=True)


class _RepoIdentityTestCase(unittest.TestCase):
    """Shared harness: real `CLAUDE_CONFIG_DIR`, real registry JSON on
    disk, monkeypatched only at the psutil name-match leg (see module
    docstring)."""

    def setUp(self):
        clear_repo_identity_memo()
        self._env_patches = []
        self._attr_patches = []

    def tearDown(self):
        for key, old in self._env_patches:
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        for obj, name, old in self._attr_patches:
            setattr(obj, name, old)
        clear_repo_identity_memo()

    def _setenv(self, key: str, value):
        old = os.environ.get(key)
        self._env_patches.append((key, old))
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def _setattr(self, obj, name: str, value):
        old = getattr(obj, name)
        self._attr_patches.append((obj, name, old))
        setattr(obj, name, value)

    def _wire_pid_env(self, pid: int, hit: bool = True):
        import coordinator_core.session.core as _core

        if hit:
            self._setattr(_core, "_resolve_claude_pid_from_env", lambda: ((pid, 0.0), "env-hit"))
        else:
            self._setattr(_core, "_resolve_claude_pid_from_env", lambda: (None, "env-miss:absent"))
        self._setenv("CLAUDE_PID", str(pid))

    def _wire_registry_dir(self, tmp_path: Path) -> Path:
        config_dir = tmp_path / "claude-config"
        self._setenv("CLAUDE_CONFIG_DIR", str(config_dir))
        sessions_dir = config_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        return sessions_dir

    def _wire_stable_pid_alive(self, alive: bool = True):
        import coordinator_core.pickup_assemble as _pa

        self._setattr(_pa._session_core, "stable_pid_alive", lambda pid, stored_start_epoch="": alive)

    def _wire_sid_env(self, sid):
        self._setenv("CLAUDE_CODE_SESSION_ID", sid)


class TestVerdictMatch(_RepoIdentityTestCase):
    def test_match_when_anchor_equals_repo_root(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            repo_root = tmp_path / "repo"
            _make_repo(repo_root)
            sessions_dir = self._wire_registry_dir(tmp_path)
            _write_registry_record(sessions_dir, "111.json", "sess-a", 111, repo_root)
            self._wire_pid_env(111)
            self._wire_stable_pid_alive(True)
            self._wire_sid_env("sess-a")

            old_cwd = os.getcwd()
            os.chdir(str(repo_root))
            try:
                root, verdict = resolve_checked_repo_root()
            finally:
                os.chdir(old_cwd)

            self.assertEqual(verdict["verdict"], _VERDICT_MATCH)
            self.assertEqual(root, str(repo_root.resolve()))


class TestVerdictMismatch(_RepoIdentityTestCase):
    def test_mismatch_on_real_divergence(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            repo_root = tmp_path / "repo"
            foreign_root = tmp_path / "foreign"
            _make_repo(repo_root)
            _make_repo(foreign_root)
            sessions_dir = self._wire_registry_dir(tmp_path)
            _write_registry_record(sessions_dir, "333.json", "sess-c", 333, foreign_root)
            self._wire_pid_env(333)
            self._wire_stable_pid_alive(True)
            self._wire_sid_env("sess-c")

            old_cwd = os.getcwd()
            os.chdir(str(repo_root))
            try:
                root, verdict = resolve_checked_repo_root()
            finally:
                os.chdir(old_cwd)

            self.assertEqual(verdict["verdict"], _VERDICT_MISMATCH)
            self.assertEqual(root, str(repo_root.resolve()))


class TestVerdictUnresolved(_RepoIdentityTestCase):
    def test_unresolved_when_no_sid_env(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            repo_root = tmp_path / "repo"
            _make_repo(repo_root)
            self._wire_sid_env(None)

            old_cwd = os.getcwd()
            os.chdir(str(repo_root))
            try:
                root, verdict = resolve_checked_repo_root()
            finally:
                os.chdir(old_cwd)

            self.assertEqual(verdict["verdict"], _VERDICT_UNRESOLVED)
            self.assertEqual(root, str(repo_root.resolve()))

    def test_unresolved_when_no_registry_record(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            repo_root = tmp_path / "repo"
            _make_repo(repo_root)
            self._wire_registry_dir(tmp_path)
            self._wire_pid_env(999, hit=False)
            self._wire_sid_env("sess-none")

            old_cwd = os.getcwd()
            os.chdir(str(repo_root))
            try:
                root, verdict = resolve_checked_repo_root()
            finally:
                os.chdir(old_cwd)

            self.assertEqual(verdict["verdict"], _VERDICT_UNRESOLVED)

    def test_two_successive_unresolved_calls_each_re_read(self):
        """UNRESOLVED must never be memoized -- two successive calls with
        different on-disk registries must each be re-evaluated fresh."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            repo_root = tmp_path / "repo"
            _make_repo(repo_root)
            sessions_dir = self._wire_registry_dir(tmp_path)
            self._wire_pid_env(1010, hit=False)
            self._wire_sid_env("sess-unresolved-twice")

            old_cwd = os.getcwd()
            os.chdir(str(repo_root))
            try:
                _root1, verdict1 = resolve_checked_repo_root()
                self.assertEqual(verdict1["verdict"], _VERDICT_UNRESOLVED)

                # Now make the registry resolvable -- if UNRESOLVED had been
                # wrongly memoized, this second call would still report
                # UNRESOLVED instead of re-reading and finding MATCH.
                _write_registry_record(sessions_dir, "1010.json", "sess-unresolved-twice", 1010, repo_root)
                self._wire_pid_env(1010, hit=True)
                self._wire_stable_pid_alive(True)

                _root2, verdict2 = resolve_checked_repo_root()
                self.assertEqual(verdict2["verdict"], _VERDICT_MATCH)
            finally:
                os.chdir(old_cwd)


class TestExplicitRootNeverRefuses(_RepoIdentityTestCase):
    def test_explicit_root_never_gated(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            foreign_root = tmp_path / "foreign"
            _make_repo(foreign_root)
            # Deliberately wire a MISMATCH-shaped registry -- explicit root
            # must short-circuit before any of this is consulted.
            sessions_dir = self._wire_registry_dir(tmp_path)
            _write_registry_record(sessions_dir, "444.json", "sess-explicit", 444, foreign_root)
            self._wire_pid_env(444)
            self._wire_stable_pid_alive(True)
            self._wire_sid_env("sess-explicit")

            explicit = str(tmp_path / "somewhere-else")
            root, verdict = resolve_checked_repo_root(explicit_root=explicit)

            self.assertEqual(root, explicit)
            self.assertEqual(verdict["verdict"], _VERDICT_EXPLICIT)


class TestMemoization(_RepoIdentityTestCase):
    def test_match_memo_returns_cached_verdict_without_re_reading_registry(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            repo_root = tmp_path / "repo"
            _make_repo(repo_root)
            sessions_dir = self._wire_registry_dir(tmp_path)
            _write_registry_record(sessions_dir, "555.json", "sess-memo", 555, repo_root)
            self._wire_pid_env(555)
            self._wire_stable_pid_alive(True)
            self._wire_sid_env("sess-memo")

            old_cwd = os.getcwd()
            os.chdir(str(repo_root))
            try:
                _root1, verdict1 = resolve_checked_repo_root()
                self.assertEqual(verdict1["verdict"], _VERDICT_MATCH)

                # Delete the on-disk registry record entirely -- a fresh
                # (non-memoized) call would now be UNRESOLVED (no record).
                for f in sessions_dir.glob("*.json"):
                    f.unlink()

                _root2, verdict2 = resolve_checked_repo_root()
                self.assertEqual(verdict2["verdict"], _VERDICT_MATCH)
                self.assertEqual(verdict2, verdict1)
            finally:
                os.chdir(old_cwd)

    def test_mismatch_memo_returns_cached_verdict_without_re_reading_registry(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            repo_root = tmp_path / "repo"
            foreign_root = tmp_path / "foreign"
            _make_repo(repo_root)
            _make_repo(foreign_root)
            sessions_dir = self._wire_registry_dir(tmp_path)
            _write_registry_record(sessions_dir, "666.json", "sess-mismatch-memo", 666, foreign_root)
            self._wire_pid_env(666)
            self._wire_stable_pid_alive(True)
            self._wire_sid_env("sess-mismatch-memo")

            old_cwd = os.getcwd()
            os.chdir(str(repo_root))
            try:
                _root1, verdict1 = resolve_checked_repo_root()
                self.assertEqual(verdict1["verdict"], _VERDICT_MISMATCH)

                for f in sessions_dir.glob("*.json"):
                    f.unlink()

                _root2, verdict2 = resolve_checked_repo_root()
                self.assertEqual(verdict2["verdict"], _VERDICT_MISMATCH)
                self.assertEqual(verdict2, verdict1)
            finally:
                os.chdir(old_cwd)


class TestNoSubprocessSpawnedByRevParse(unittest.TestCase):
    """AC5's executable check, part 1: no `coordinator/bin/*.py` script
    (outside the ALLOWLIST below, or the frozen KNOWN_REMAINDER baseline)
    shells out to `git rev-parse --show-toplevel` directly -- the checked
    resolver is the only sanctioned path to that answer now.

    C8 reshaped this check (was `@pytest.mark.designed_red` under C1, and
    could never go green as a whole-tree walk since ~43 files outside this
    plan's own change-set were never this plan's to migrate -- see the
    plan's C8 row). It now asserts two things:

    1. This plan's own class-A change-set (`docs/plans/2026-08-11-one-
       checked-resolver-for-the-bin-family.md`'s `scope:` frontmatter,
       `.py` files directly under `coordinator/bin/`, minus the ones C7
       explicitly left alone) carries ZERO real `rev-parse
       --show-toplevel` call sites. A regression here -- a migrated
       script reintroducing the raw spawn -- fails immediately.
    2. The pre-existing remainder (files this plan never touched) is
       pinned as an exact frozen baseline (`KNOWN_REMAINDER`), so a NEW
       offender entering the tree -- in or out of scope -- fails loud,
       while the known-and-out-of-scope ones do not silently re-widen the
       check into permanent inertness.

    "Real call site" is detected by parsing (`ast`), not regexing the
    whole file: only `ast.Call` nodes whose source segment contains both
    `rev-parse` and `--show-toplevel` count. A module/function docstring
    or a `#` comment describing the old behaviour is never an `ast.Call`
    node, so prose describing a removed mechanism (several migrated
    scripts still carry this, corrected by C8 alongside this check) does
    not trip the assertion -- the earlier whole-file regex could not tell
    the two apart, which is exactly why it could never go green.
    """

    #: This plan's own class-A migration surface: the `.py` files listed
    #: directly under `coordinator/bin/` in the plan's `scope:` frontmatter
    #: (docs/plans/2026-08-11-one-checked-resolver-for-the-bin-family.md),
    #: minus `emit-cadence.py` and `fan-out-dispatch.py` -- both IN scope
    #: but explicitly left alone by C7 (see ALLOWLIST below) after C7's own
    #: investigation, not by omission here.
    PLAN_CHANGE_SET = {
        "append-goal-event.py",
        "append-plan-session.py",
        "baton-drift-sweep.py",
        "close-origin-stub-on-ship.py",
        "coordinator-ceremony-hook.py",
        "coordinator-session-loe.py",
        "coordinator-tasks-mirror.py",
        "coordinator-write-review-trail.py",
        "day-coverage-sweep.py",
        "emit-cockpit-snapshot.py",
        "goal-close-day.py",
        "priority-set.py",
        "prune-closed-bugs.py",
        "prune-closed-improvements.py",
        "query-handoff-columns.py",
        "reap-integrated-review-findings.py",
        "reap-stale-subagent-sidecars.py",
        "reconcile-completion-commits.py",
        "review-coverage-gate.py",
        "set-goal-kr-status.py",
        "standup.py",
        "sweep-shipped-handoffs.py",
        "verify-orientation-cache-sync.py",
        "whats-next.py",
        "workday-complete-step9-append-changelog.py",
        "wsc-tail.py",
    }

    #: Exact baseline of every `coordinator/bin/*.py` file outside
    #: PLAN_CHANGE_SET and ALLOWLIST whose `ast.Call` detector below still
    #: fires, re-measured against the class-B re-verification
    #: (state/audits/class-b-repo-root-reverification.md). Pinned exactly so
    #: a file reintroducing the raw spawn fails here instead of blending in;
    #: `test_known_remainder_is_live` fails when an entry stops firing, so
    #: the set only shrinks.
    KNOWN_REMAINDER = {
        # Real spawn, `git -C <cwd> rev-parse --show-toplevel`; read-only
        # gate decision, explicit `--repo-root` honored first.
        "parallel-review-gate-decision.py",
        # Not a spawn: the matched `ast.Call`s are a `print` and an
        # `add_argument(help=...)` whose prose names the git command. The
        # real resolution is the bare `show_toplevel()` tracked in
        # REACHABLE_CWD_FALLBACK / B_SAFE_CWD_FALLBACK below.
        "wsc-session-disposition.py",
        "queue-triage.py",
    }

    #: Every `coordinator/bin/*.py` file legitimately still allowed to
    #: contain a literal `rev-parse --show-toplevel` occurrence, and why:
    ALLOWLIST = {
        # That script's ENTIRE JOB is comparing `git rev-parse
        # --show-toplevel` against an expected path (Anti-scope: "do not
        # fix assert-cwd.py" -- it is the gate's crude ancestor, not a
        # victim of the defect).
        "assert-cwd.py",
        # Class-C cluster: resolve `git -C dirname(handoff_path)` -- their
        # own docstrings carry the literal phrase "not the process cwd";
        # this plan's Anti-scope forbids re-migrating them (a deliberate
        # fix already won there). Verified on disk by that phrase, not by
        # filename shape:
        "handoff-archive-transition.py",
        "handoff-reconcile-close-terminal.py",
        "handoff-stamp-phase.py",
        # Resolves `git -C <its own bin dir>`, never the process cwd --
        # same class-C shape, different neighbourhood.
        "check-bin-sh-polyglot.py",
        # Same class-C shape: `git rev-parse --show-toplevel` run with
        # `cwd=<its own bin dir>`, never the process cwd.
        "check-decision-citations.py",
        # Class-C per the class-B re-verification: `git -C <plugin_root>`
        # (script location / COORDINATOR_PLUGIN_ROOT), never the process cwd.
        "check-schema-version-bump.py",
        # Class-C: `git -C <script dir>`, never the process cwd.
        "check-sh-suffix-polyglot.py",
        # Class-C, path-keyed: `git -C <path argument> rev-parse
        # --show-toplevel` answers "is this path the root of its repo", for a
        # publish clone that is deliberately not the process's repo. Both
        # postdate the baseline; the test could not finish (see
        # `_has_real_rev_parse_toplevel_call`) so neither was ever checked.
        "klabauter-reconcile.py",
        "percolate-gate.py",
        # Class-C, new since the 2026-08-11 KNOWN_REMAINDER baseline was
        # frozen: `git -C dirname(handoff_path) rev-parse --show-toplevel`,
        # own docstring carries the literal phrase "not the process cwd";
        # same technique as the already-allowlisted
        # handoff-reconcile-close-terminal.py, not this plan's to migrate.
        "handoff-backfill-claim-stamp.py",
        # Named class-C individually in Anti-scope:
        "check-global-doctrine-mirror.py",
        "regen-cockpit-schema.py",
        # `handoff-discharge-criteria.py` (commit 232c9d960, 2026-08-13,
        # genuinely postdates the 2026-08-11 freeze -- a brand-new file,
        # not a rename): own docstring/`_resolve_repo_root` state verbatim
        # "mirrors handoff-backfill-claim-stamp.py::_resolve_repo_root" --
        # same Class-C shape (`git -C dirname(handoff_path)
        # rev-parse --show-toplevel`, resolving the TARGET HANDOFF's repo,
        # never the process cwd) as the already-allowlisted
        # handoff-backfill-claim-stamp.py / handoff-reconcile-close-
        # terminal.py. Not this plan's to migrate for the same reason those
        # two aren't.
        "handoff-discharge-criteria.py",
        # `percolate-round.py` (commit 786faba65, 2026-08-14, genuinely
        # postdates the freeze -- a real new `_resolve_repo_root(dest)`
        # call site, added deliberately per that commit's own message,
        # "resolve the repo root from git, not from the row's dest", to fix
        # a live regression). `resolve_checked_repo_root()` takes no path
        # argument and answers only "what repo is THIS PROCESS'S cwd in";
        # `_resolve_repo_root(dest)` answers a THIRD meaning of root --
        # the git worktree of `dest`, a percolate row's destination mirror,
        # which may be an entirely different repo than the process's own
        # cwd (same class as fan-out-dispatch.py's already-allowlisted
        # `target_git_root`, below). Migrating to the checked resolver
        # would answer the wrong question.
        "percolate-round.py",
        # NOT allowlisted, deliberately, though an earlier draft of this
        # set listed them: close-origin-stub-on-ship.py (C5),
        # coordinator-write-review-trail.py (C4), sweep-shipped-handoffs.py
        # (C3), and wsc-tail.py (C6) are all IN this plan's migration set.
        # Exempting a script the plan is actively fixing is how a guard
        # goes permanently inert while still reading as enforced.
        # C7 leave-alone slot, filled by C7's reported dispositions:
        #
        # Two resolvers, one deliberately never-exiting. The ONLY file in
        # this plan's scope importing no coordinator_core at all, so
        # migrating imposes the full engine bootstrap on a courtesy
        # marker-sync path that is off by default behind
        # COORDINATOR_EMISSION_CADENCE_LIVE. Bootstrap cost not worth
        # paying; left alone deliberately, not overlooked.
        "emit-cadence.py",
        # `target_git_root` is a THIRD meaning of root: the repo being
        # dispatched INTO, not the repo this process is in. The checked
        # resolver answers the latter, so migrating would answer the wrong
        # question. (Its separate `rev-parse --is-inside-work-tree` is a
        # different check and not this defect class.)
        "fan-out-dispatch.py",
    }

    #: Matches only within a single `ast.Call` node's own source segment
    #: (see `_has_real_rev_parse_toplevel_call` below) -- never against
    #: the whole file, so a docstring/comment mention can never trip it.
    _REV_PARSE_TOPLEVEL_RE = re.compile(
        r"rev-parse[\s\S]*--show-toplevel|--show-toplevel[\s\S]*rev-parse"
    )

    @classmethod
    def _has_real_rev_parse_toplevel_call(cls, text: str) -> bool:
        """True iff `text` contains an `ast.Call` node (a real invocation
        -- `subprocess.run(...)`, a local `_run_git(...)`/`_git(...)`
        wrapper, anything callable) whose own source segment mentions both
        `rev-parse` and `--show-toplevel`. A module/function docstring or a
        `#` comment describing the (removed) behaviour is never an
        `ast.Call` node, so it can never match here -- unlike a whole-file
        regex, which cannot tell prose from an invocation.
        """
        # Text prefilter plus a one-time line split: `ast.get_source_segment`
        # re-splits the whole file in a per-character Python loop for every
        # call node, which is minutes on the 2k-8k-line scripts.
        if "rev-parse" not in text or "--show-toplevel" not in text:
            return False
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return False
        lines = re.findall(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+$", text)
        encoded = [line.encode("utf-8") for line in lines]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or node.end_lineno is None:
                continue
            first, last = node.lineno - 1, node.end_lineno - 1
            if first == last:
                raw = encoded[first][node.col_offset : node.end_col_offset]
            else:
                raw = (
                    encoded[first][node.col_offset :]
                    + b"".join(encoded[first + 1 : last])
                    + encoded[last][: node.end_col_offset]
                )
            segment = raw.decode("utf-8", errors="replace")
            if cls._REV_PARSE_TOPLEVEL_RE.search(segment):
                return True
        return False

    def test_no_disallowed_rev_parse_show_toplevel_occurrence(self):
        bin_dir = Path(_BIN_DIR)
        offenders = []
        new_offenders = []
        for path in sorted(bin_dir.glob("*.py")):
            if path.name in self.ALLOWLIST:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if not self._has_real_rev_parse_toplevel_call(text):
                continue
            offenders.append(path.name)
            if path.name in self.PLAN_CHANGE_SET:
                # This plan's own migrated surface: zero tolerance, no
                # baseline carve-out -- a hit here is a regression.
                new_offenders.append(f"{path.name} (this plan's own change-set -- regression)")
            elif path.name not in self.KNOWN_REMAINDER:
                # Outside this plan's scope AND not in the frozen
                # baseline: a genuinely NEW offender entering the tree.
                new_offenders.append(f"{path.name} (new -- not in KNOWN_REMAINDER baseline)")
        self.assertEqual(
            new_offenders,
            [],
            f"the following coordinator/bin/*.py scripts shell out to "
            f"'git rev-parse --show-toplevel' outside the ALLOWLIST and the frozen "
            f"KNOWN_REMAINDER baseline: {new_offenders} -- route them through "
            f"lib/repo_identity.py instead, or add them to KNOWN_REMAINDER with a "
            f"named reason if genuinely out of this plan's scope",
        )

    def test_every_repo_identity_importer_references_the_verdict_field(self):
        """AC5's executable check, part 2: every module importing the
        checked resolver must also reference the verdict field it returns
        (`resolve_checked_repo_root` / `verdict`), not only bind the root
        and ignore disposition entirely. Matches NOTHING as of C1 (nothing
        imports `repo_identity` yet) -- it must still be written now,
        because C8 re-runs it after C2-C7 land and this is the artifact
        that discharges AC5's second half then.
        """
        bin_dir = Path(_BIN_DIR)
        lib_dir = bin_dir / "lib"
        candidates = list(bin_dir.glob("*.py")) + list(lib_dir.glob("*.py"))
        offenders = []
        for path in candidates:
            if path.name in ("repo_identity.py",):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "repo_identity" not in text or "resolve_checked_repo_root" not in text:
                continue
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            # The text hit above is a cheap PREFILTER, never the population
            # gate. `query-work-state.py` names both symbols in prose only --
            # its module docstring explains why its `--repo-root` flag
            # deliberately bypasses the checked resolver -- and was reported as
            # an offender for documenting that decision. The verdict-side walk
            # below already reasons about exactly this ("a module docstring
            # mentioning the word does not match"); the same reasoning belongs
            # on the way IN. Require a real import node, so a module that never
            # calls the resolver is never asked what it does with the verdict.
            imports_resolver = False
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "repo_identity":
                    imports_resolver = True
                    break
                if isinstance(node, ast.Import) and any(
                    alias.name == "repo_identity" for alias in node.names
                ):
                    imports_resolver = True
                    break
            if not imports_resolver:
                continue
            # Named, reasoned exemptions -- never a bare skip list. C18
            # (state/dispatch-briefs/2026-08-20-a-refusal-cannot-exit-zero/C18.md,
            # DR-277 EM decision D5) removed priority-set.py's cwd identity gate
            # outright: `priority.set` is scope="none" and resolves its ledger
            # write centrally, so a MISMATCH verdict has nothing to advise on
            # there. It still needs the resolver for `cwd_repo_root`, so it
            # imports and deliberately discards the verdict. This check's premise
            # -- "bound the root and ignored disposition entirely" is a defect --
            # does not hold for a door with no disposition to act on.
            # `tests/test_priority_set_no_cwd_gate.py` pins that absence, so the
            # behaviour is covered rather than merely exempted here.
            if path.name in ("priority-set.py",):
                continue
            # Match the FIELD ACCESS, not a bare name. The three shapes call
            # sites actually use are `v["verdict"]` (Subscript over a string
            # constant), `v.get("verdict")` (Call arg), and `v.verdict`
            # (Attribute) -- only the last surfaces as an ast.Attribute and
            # NONE surfaces as an ast.Name. A Name/Attribute-only walk
            # therefore both misses the real thing (a site branching on
            # `_verdict["verdict"]` reads as an offender) and accepts a fake
            # one (any local merely NAMED `verdict`, never read, passes).
            # All three shapes, and both failure directions, were observed
            # on this check's first live run against C2-C5.
            #
            # So: an exact `"verdict"` string constant anywhere in the
            # module, or an attribute of that name. A module docstring
            # mentioning the word does not match -- its Constant value is
            # the whole docstring, never the bare literal.
            reads_verdict_field = False
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "verdict":
                    reads_verdict_field = True
                    break
                if isinstance(node, ast.Constant) and node.value == "verdict":
                    reads_verdict_field = True
                    break
            if not reads_verdict_field:
                offenders.append(path.name)
        self.assertEqual(
            offenders,
            [],
            f"the following modules import repo_identity's checked resolver but "
            f"never reference its verdict field: {offenders}",
        )


class TestBareShowToplevelInventory(unittest.TestCase):
    """Pins the class-B re-verification (state/audits/class-b-repo-root-
    reverification.md) as an executable inventory.

    The rev-parse detector above only sees a spawn; the out-of-scope
    scripts moved to `coordinator_core.git.repo_root.show_toplevel`, a
    non-spawning walk, so a script deriving its root from the process cwd
    is invisible to it. This inventory finds those by the call shape
    (`show_toplevel()` with no argument, or fed `os.getcwd()`) and requires
    each one to carry a recorded verdict, so a new cwd-derived script, or a
    verdict gone stale, fails loud instead of being re-surveyed.
    """

    #: Never reaches the process cwd. Must not contain a bare call.
    CLASS_C = {
        "cartography.py",
        "check-schema-version-bump.py",
        "check-sh-suffix-polyglot.py",
        "debash-scorecard.py",
        "handoff-has-live-children.py",
        "cutover-cli.py",
        "sweep-boot.py",
    }

    #: cwd fallback exists but is correct by construction or read-only with
    #: no automated caller omitting the root.
    B_SAFE_CWD_FALLBACK = {
        "aggregate-rollup.py",
        "reap-sessions.py",
        "check-no-illegal-paths.py",
        "list-orphaned-plans.py",
        "wsc-session-disposition.py",
        "workday-start-inbox-blitz-assemble.py",
        "reaper-resting-batons.py",
        "assert-no-terminal-plans-in-live.py",
        "parallel-review-gate-decision.py",
    }

    #: The default invocation resolves from cwd, no automated caller threads
    #: a root, and the script mutates: class-A in effect, the follow-up
    #: migration set. Shrink-only: remove an entry when its script migrates
    #: onto lib/repo_identity.py.
    REACHABLE_CWD_FALLBACK = {
        "advance-tracker-status.py",
        "archive-paper-trail.py",
        "emit-goal-from-artifact.py",
        "freeze-review-diff.py",
        "parallel-review-orthogonality-guard.py",
        "queue-triage.py",
        "app-session.py",
        "coordinator-doc-new.py",
        "coordinator-harvest-deferrals.py",
        "coordinator-lesson-add.py",
        "cross-repo-memo.py",
        "cruft-sweep.py",
        "discharge-landed.py",
        "reap-claims-for-repos.py",
    }

    @staticmethod
    def _is_bare_show_toplevel(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "show_toplevel":
            return False

        def _is_getcwd(expr: ast.AST) -> bool:
            return (
                isinstance(expr, ast.Call)
                and isinstance(expr.func, ast.Attribute)
                and expr.func.attr == "getcwd"
            )

        args = list(node.args) + [kw.value for kw in node.keywords]
        return not args or all(_is_getcwd(a) for a in args)

    @classmethod
    def _bare_callers(cls) -> set:
        found = set()
        for path in Path(_BIN_DIR).glob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue
            if any(cls._is_bare_show_toplevel(n) for n in ast.walk(tree)):
                found.add(path.name)
        return found

    def test_verdict_tables_are_disjoint_and_name_real_files(self):
        tables = [
            self.CLASS_C,
            self.B_SAFE_CWD_FALLBACK - {"parallel-review-gate-decision.py"},
            self.REACHABLE_CWD_FALLBACK,
        ]
        seen = set()
        for table in tables:
            self.assertEqual(seen & table, set())
            seen |= table
        missing = sorted(n for n in seen | self.B_SAFE_CWD_FALLBACK if not (Path(_BIN_DIR) / n).is_file())
        self.assertEqual(missing, [], f"verdict tables name files that no longer exist: {missing}")

    def test_every_bare_show_toplevel_caller_has_a_verdict(self):
        known = (
            self.B_SAFE_CWD_FALLBACK
            | self.REACHABLE_CWD_FALLBACK
            | TestNoSubprocessSpawnedByRevParse.ALLOWLIST
        )
        unverdicted = sorted(self._bare_callers() - known)
        self.assertEqual(
            unverdicted,
            [],
            f"coordinator/bin scripts derive their repo root from the process cwd "
            f"with no recorded verdict: {unverdicted} -- route them through "
            f"lib/repo_identity.py, or classify them in the verdict tables with "
            f"the evidence in state/audits/class-b-repo-root-reverification.md",
        )

    def test_class_c_files_never_call_bare_show_toplevel(self):
        self.assertEqual(sorted(self.CLASS_C & self._bare_callers()), [])

    def test_cwd_fallback_verdicts_are_live(self):
        bare = self._bare_callers()
        spawners = TestNoSubprocessSpawnedByRevParse.KNOWN_REMAINDER
        stale = sorted(
            (self.B_SAFE_CWD_FALLBACK | self.REACHABLE_CWD_FALLBACK)
            - bare
            - spawners
        )
        self.assertEqual(
            stale,
            [],
            f"verdict-table entries no longer derive their root from the process cwd: "
            f"{stale} -- migrated or rewritten; delete them from the table",
        )

    def test_known_remainder_is_live(self):
        """Shrink-only: a KNOWN_REMAINDER entry whose detector no longer
        fires is dead weight that would hide a later regression."""
        has_call = TestNoSubprocessSpawnedByRevParse._has_real_rev_parse_toplevel_call
        dead = sorted(
            n
            for n in TestNoSubprocessSpawnedByRevParse.KNOWN_REMAINDER
            if not has_call((Path(_BIN_DIR) / n).read_text(encoding="utf-8"))
        )
        self.assertEqual(dead, [], f"KNOWN_REMAINDER entries no longer match the detector: {dead}")


if __name__ == "__main__":
    unittest.main()
