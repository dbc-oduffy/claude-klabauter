from __future__ import annotations
"""
test_harvest_doe_root_machine_local_leg.py — regression test for the
machine-local-registry leg of coordinator-harvest-deferrals' idempotency
dedup-scan root resolution.

Spec backlink: DoE-claude:pln-full-coverage-planning-posture-bca96f § C7
(Review: code-reviewer slice2 Finding 1 — the harvest tool's own docstring
claimed its dedup-scan mirrored coordinator-queue-append's/coordinator-
lesson-promote's write-root resolution, but `_candidate_search_dirs()` only
reproduced the DOE_ROOT-ENV leg of `coordinator_registry.doe_root()`'s
three-step chain (DOE_ROOT env -> machine-local `repos.doe_claude` -> raise
_DoeUnresolvable). On a machine with DOE_ROOT unset but `repos.doe_claude`
registered in the machine-local registry — the expected steady state on any
fully-installed machine, not an edge case — the write seams resolve their
output root via the machine-local leg while the dedup scan looked in the
current git repo root instead, causing a second harvest run to double-write
a duplicate lesson-outbox/central-queue entry.

2026-07-25 revision (this dispatch) — TWO independent findings folded in:

1. `_candidate_search_dirs()`'s central-scope improvement-queue leg was
   scanning `coordinator_registry.doe_root()` (repos.doe_claude), but commit
   5b908173 ("central scope routes to claude-klabauter, not DoE — reconcile the two
   implementations", 2026-07-23) repointed coordinator-queue-append's actual
   central-scope write (both its legacy `_output_path()` branch and the
   native `queue.append` op) to `cli_shared.claude_klabauter_root()` (repos.claude_klabauter)
   instead. The dedup scan was never updated to follow — a live,
   currently-unfixed double-write regression for any central-scope
   improvement-queue harvest, fixed alongside this test (see
   coordinator-harvest-deferrals' `_resolved_claude_klabauter_root()`).

2. The full end-to-end idempotency check below can no longer force
   "state-1" (coordinator_core.invoke seam absent) across the board the way
   it originally did, because `schema.validate`/`schema.describe` have NO
   legacy fallback at all as of the mandatory-native cutover (W0.5 Option
   B+C, 2026-07-19 — see CLAUDE.md § Project Overview "claude-klabauter is a HARD
   prereq"; also coordinator-queue-append's own module comment: "There is NO
   legacy fallback: the legacy_fn passed to route() ... always raises").
   Pointing CLAUDE_KLABAUTER_ROOT at an unresolvable/empty directory (the original
   technique) now makes schema.validate hard-fail before any dedup logic
   ever runs — that failure IS what this test was originally reported
   failing on. Pointing CLAUDE_KLABAUTER_ROOT at a REAL, working coordinator_core
   checkout instead (satisfying schema.validate) forces the native seam
   present for the mutation ops too; coordinator-lesson-promote's native
   `queue.promote` op does NOT honour `LESSON_PROMOTE_OUTBOX_ROOT` (it
   resolves `repos.doe_claude` independently, on the invoking machine's REAL
   machine-local registry, not via the `MACHINE_LOCAL_IMPL` test-isolation
   stub) — diagnosing this by hand leaked one real file into the live
   `DoE-claude` sibling repo's `state/lessons-outbox/` before it was caught
   and deleted. coordinator-lesson-promote now carries a
   `LESSON_PROMOTE_OUTBOX_ROOT`-forces-legacy gate mirroring
   coordinator-queue-append's pre-existing `QUEUE_APPEND_OUTPUT_ROOT` one, so
   the override is honoured regardless of native-seam state — closing that
   escape hatch. `REPO_DOE_CLAUDE` (the ambient ammo behind `doe_root()`'s
   rung 1b) is also typically exported in a login shell on a provisioned
   machine and must be stripped for real machine-local-rung isolation; the
   original test never stripped it.

Given (2), this file now carries TWO tests with distinct, narrower
guarantees rather than one test trying to prove everything at once:

  - test_candidate_search_dirs_resolves_machine_local_leg_for_both_scopes:
    an in-process, no-subprocess check that directly calls
    `_candidate_search_dirs()` (imported from the harvest CLI module) with
    DOE_ROOT/REPO_DOE_CLAUDE/CLAUDE_KLABAUTER_ROOT unset and a fake `_machine_local.py`
    stub active — this is the actual regression net for finding (1) above,
    and it is safe by construction (never spawns coordinator-queue-append or
    coordinator-lesson-promote, so it can never reach a real sibling repo).

  - test_second_run_idempotent_end_to_end: spawns the real harvest CLI twice
    end-to-end, with QUEUE_APPEND_OUTPUT_ROOT and LESSON_PROMOTE_OUTBOX_ROOT
    both pointed at a fixture root (forcing both write seams' legacy path
    regardless of native-seam state, per the two "-forces-legacy" gates
    described above) and CLAUDE_KLABAUTER_ROOT pointed at THIS repo's own checkout
    (satisfying schema.validate's mandatory-native requirement safely, since
    neither mutation op can reach native once its own override is set).
    Proves the harvest tool's own idempotency end-to-end without depending
    on, or risking, any real machine-local registry entry.

Run with: python3 -m pytest test_harvest_doe_root_machine_local_leg.py
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_THIS_DIR)  # coordinator/bin
_COORDINATOR_DIR = os.path.dirname(_BIN_DIR)  # coordinator/
_REPO_ROOT = os.path.dirname(_COORDINATOR_DIR)  # repo root (this checkout)

_HARVEST_CLI = os.path.join(_BIN_DIR, "coordinator-harvest-deferrals.py")
_FIXTURES_DIR = os.path.join(_THIS_DIR, "fixtures", "plan-tasks-spine")

_SUBPROCESS_TIMEOUT_SECS = 30

# Env var names the machine-local reader (both coordinator_registry.py's and
# cli_shared.py's copies) and doe_root()/claude_klabauter_root() honour — stripped
# together everywhere this suite needs a genuinely-unresolved-except-via-stub
# baseline. See module docstring finding (2) re: REPO_DOE_CLAUDE.
_ENV_VARS_TO_STRIP_FOR_MACHINE_LOCAL_ISOLATION = (
    "DOE_ROOT",
    "REPO_DOE_CLAUDE",
    "CLAUDE_KLABAUTER_ROOT",
)


def _yaml_files_in(directory: str) -> list[str]:
    if not os.path.isdir(directory):
        return []
    return sorted(f for f in os.listdir(directory) if f.endswith(".yaml"))


_FAKE_MACHINE_LOCAL_TEMPLATE = """#!/usr/bin/env python3
# Fake _machine_local.py stub for test isolation — responds to `get repos.doe_claude`
# and `get repos.claude_klabauter` with fixed fixture paths, exercising
# coordinator_registry.doe_root()'s and cli_shared.claude_klabauter_root()'s real
# machine-local-registry rungs without touching the real registry.local.toml.
import sys

FAKE_DOE_ROOT = {fake_doe_root!r}
FAKE_CLAUDE_KLABAUTER_ROOT = {fake_claude_klabauter_root!r}

def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "get":
        if sys.argv[2] == "repos.doe_claude":
            print(FAKE_DOE_ROOT)
            return 0
        if sys.argv[2] == "repos.claude_klabauter":
            print(FAKE_CLAUDE_KLABAUTER_ROOT)
            return 0
    return 1

if __name__ == "__main__":
    sys.exit(main())
"""


def _write_stub(stub_dir: str, fake_doe_root: str, fake_claude_klabauter_root: str) -> str:
    stub_path = os.path.join(stub_dir, "_machine_local.py")
    with open(stub_path, "w", encoding="utf-8") as fh:
        fh.write(
            _FAKE_MACHINE_LOCAL_TEMPLATE.format(
                fake_doe_root=fake_doe_root, fake_claude_klabauter_root=fake_claude_klabauter_root
            )
        )
    return stub_path


def _load_harvest_module():
    """Import coordinator-harvest-deferrals (extensionless) as a module, in-process.

    Used only by the no-subprocess candidate-search-dirs test below — never by
    the end-to-end test, which deliberately runs the real CLI as a subprocess.
    """
    if _BIN_DIR not in sys.path:
        sys.path.insert(0, _BIN_DIR)
    loader = SourceFileLoader("coordinator_harvest_deferrals_module", _HARVEST_CLI)
    spec = importlib.util.spec_from_file_location(
        "coordinator_harvest_deferrals_module", _HARVEST_CLI, loader=loader
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_candidate_search_dirs_resolves_machine_local_leg_for_both_scopes() -> None:
    """_candidate_search_dirs() must resolve BOTH the central-scope
    improvement-queue leg (via cli_shared.claude_klabauter_root(), repos.claude_klabauter)
    and the lessons-outbox leg (via coordinator_registry.doe_root(),
    repos.doe_claude) through the machine-local registry rung when no env
    override is set for either — the exact regression this whole file exists
    to catch, exercised here in-process (no subprocess spawn of either write
    seam, so this cannot leak into any real sibling repo).
    """
    name = "test_candidate_search_dirs_resolves_machine_local_leg_for_both_scopes"

    fake_doe_root = tempfile.mkdtemp(prefix="harvest-fake-doe-root-")
    fake_claude_klabauter_root = tempfile.mkdtemp(prefix="harvest-fake-claude-klabauter-root-")
    stub_dir = tempfile.mkdtemp(prefix="harvest-fake-machine-local-impl-")
    saved_env = {k: os.environ.get(k) for k in _ENV_VARS_TO_STRIP_FOR_MACHINE_LOCAL_ISOLATION}
    saved_env["QUEUE_APPEND_OUTPUT_ROOT"] = os.environ.get("QUEUE_APPEND_OUTPUT_ROOT")
    saved_env["LESSON_PROMOTE_OUTBOX_ROOT"] = os.environ.get("LESSON_PROMOTE_OUTBOX_ROOT")
    saved_env["MACHINE_LOCAL_IMPL"] = os.environ.get("MACHINE_LOCAL_IMPL")
    try:
        stub_path = _write_stub(stub_dir, fake_doe_root, fake_claude_klabauter_root)

        for var in _ENV_VARS_TO_STRIP_FOR_MACHINE_LOCAL_ISOLATION:
            os.environ.pop(var, None)
        os.environ.pop("QUEUE_APPEND_OUTPUT_ROOT", None)
        os.environ.pop("LESSON_PROMOTE_OUTBOX_ROOT", None)
        os.environ["MACHINE_LOCAL_IMPL"] = stub_path

        module = _load_harvest_module()
        dirs = module._candidate_search_dirs({})

        expected_queue_dir = os.path.join(fake_claude_klabauter_root, "state", "improvement-queue")
        expected_lessons_dir = os.path.join(fake_doe_root, "state", "lessons-outbox")
        if expected_queue_dir not in dirs:
            raise AssertionError(
                f"{name}: expected central-scope improvement-queue candidate "
                f"{expected_queue_dir!r} (resolved via the machine-local "
                f"repos.claude_klabauter rung) in {dirs!r} — the central-scope "
                f"leg is not following cli_shared.claude_klabauter_root()"
            )
        if expected_lessons_dir not in dirs:
            raise AssertionError(
                f"{name}: expected lessons-outbox candidate {expected_lessons_dir!r} "
                f"(resolved via the machine-local repos.doe_claude rung) in "
                f"{dirs!r} — the lessons-outbox leg is not following "
                f"coordinator_registry.doe_root()"
            )
    finally:
        for var, val in saved_env.items():
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val
        shutil.rmtree(fake_doe_root, ignore_errors=True)
        shutil.rmtree(fake_claude_klabauter_root, ignore_errors=True)
        shutil.rmtree(stub_dir, ignore_errors=True)


def _write_plan_with_central_and_doctrine_rows(plan_path: str) -> None:
    """Write a minimal plan with a queue_scope:central row and a doctrine-edit
    row, both deferred+pm_approved — exercising both write seams
    (coordinator-queue-append's central branch and coordinator-lesson-promote)
    in one harvest run."""
    content = """---
plan_id: "pln-doe-root-machine-local-leg-test"
---

# Test plan — doe_root() machine-local leg

## Tasks

```yaml plan-tasks
- id: "D1"
  title: "Central-scope deferred item (machine-local doe_root leg)"
  body: "Exercises the central-scope improvement-queue write seam."
  change_kind: "doc-edit"
  surface: "docs/wiki/some-central-doc.md"
  queue_scope: "central"
  deferred: true
  pm_approved: true
- id: "D2"
  title: "Doctrine-edit deferred item (lessons-outbox leg)"
  body: "Exercises the lessons-outbox write seam."
  change_kind: "doctrine-edit"
  surface: "docs/wiki/some-doctrine-doc.md"
  deferred: true
  pm_approved: true
```
"""
    with open(plan_path, "w", encoding="utf-8") as fh:
        fh.write(content)


def test_second_run_idempotent_end_to_end() -> None:
    """Spawns the real harvest CLI twice end-to-end and asserts full
    idempotency (both rows queued on run 1, both deduped on run 2), with
    both write seams forced onto their legacy path via their respective
    test-isolation env overrides (QUEUE_APPEND_OUTPUT_ROOT /
    LESSON_PROMOTE_OUTBOX_ROOT) — see module docstring finding (2) for why
    this can no longer force state-1 for schema.validate/schema.describe
    (mandatory-native, no legacy fallback) and must instead force each
    mutation op's OWN legacy path via its override, independent of
    native-seam state. CLAUDE_KLABAUTER_ROOT points at this repo's own checkout so
    schema.validate succeeds; neither mutation op can reach native once its
    own override is set, so this is safe regardless of what CLAUDE_KLABAUTER_ROOT
    resolves to.
    """
    name = "test_second_run_idempotent_end_to_end"

    fake_doe_root = tempfile.mkdtemp(prefix="harvest-fake-doe-root-")
    plan_dir = tempfile.mkdtemp(prefix="harvest-machine-local-plandir-")
    try:
        plan_path = os.path.join(plan_dir, "plan.md")
        _write_plan_with_central_and_doctrine_rows(plan_path)

        env = dict(os.environ)
        for var in _ENV_VARS_TO_STRIP_FOR_MACHINE_LOCAL_ISOLATION:
            env.pop(var, None)
        env.pop("MACHINE_LOCAL_IMPL", None)
        # Force BOTH write seams onto their legacy path, regardless of
        # native-seam state — see module docstring finding (2).
        env["QUEUE_APPEND_OUTPUT_ROOT"] = fake_doe_root
        env["LESSON_PROMOTE_OUTBOX_ROOT"] = os.path.join(fake_doe_root, "state", "lessons-outbox")
        # Real checkout — required so schema.validate/schema.describe (no
        # legacy fallback) can resolve coordinator_core.invoke at all.
        env["CLAUDE_KLABAUTER_ROOT"] = _REPO_ROOT

        cmd = ["python3", os.path.abspath(_HARVEST_CLI), "--plan", plan_path]

        # Deliberately un-related cwd (this repo's tests dir) — the harvest
        # must resolve write targets via the env overrides above, not via
        # any git-root proxy.
        invocation_cwd = _THIS_DIR

        r1 = subprocess.run(
            cmd, cwd=invocation_cwd, capture_output=True, text=True, env=env, timeout=_SUBPROCESS_TIMEOUT_SECS
        )
        if r1.returncode != 0:
            raise AssertionError(f"{name}: first run: expected exit 0, got {r1.returncode}. stderr={r1.stderr}")
        if "Queued 2 deferred items" not in r1.stdout:
            raise AssertionError(f"{name}: first run: expected 'Queued 2 deferred items', got: {r1.stdout!r} stderr={r1.stderr!r}")

        r2 = subprocess.run(
            cmd, cwd=invocation_cwd, capture_output=True, text=True, env=env, timeout=_SUBPROCESS_TIMEOUT_SECS
        )
        if r2.returncode != 0:
            raise AssertionError(f"{name}: second run: expected exit 0, got {r2.returncode}. stderr={r2.stderr}")
        if "Queued 0 deferred items" not in r2.stdout:
            raise AssertionError(
                f"{name}: second run: expected 'Queued 0 deferred items' (fully deduped), "
                f"got: {r2.stdout!r} (a non-zero count means the dedup scan is NOT resolving "
                f"the same root the write seams resolve)"
            )
        if "already-harvested" not in r2.stdout:
            raise AssertionError(f"{name}: second run: expected an 'already-harvested' dedup note, got: {r2.stdout!r}")

        qdir = os.path.join(fake_doe_root, "state", "improvement-queue")
        ldir = os.path.join(fake_doe_root, "state", "lessons-outbox")
        q_files = _yaml_files_in(qdir)
        l_files = _yaml_files_in(ldir)
        if len(q_files) != 1:
            raise AssertionError(
                f"{name}: expected exactly 1 improvement-queue file under the fixture root "
                f"({qdir}) after two runs (idempotent), found {q_files}"
            )
        if len(l_files) != 1:
            raise AssertionError(
                f"{name}: expected exactly 1 lessons-outbox file under the fixture root "
                f"({ldir}) after two runs (idempotent), found {l_files}"
            )
    finally:
        shutil.rmtree(fake_doe_root, ignore_errors=True)
        shutil.rmtree(plan_dir, ignore_errors=True)


def main() -> int:
    print("test_harvest_doe_root_machine_local_leg.py")
    print("=" * 50)

    if not os.path.isfile(_HARVEST_CLI):
        print(f"ERROR: harvest CLI not found at {_HARVEST_CLI}", file=sys.stderr)
        return 1

    # Guard: never allow this suite to touch the real state dirs — in THIS
    # repo (state/improvement-queue, state/lessons-outbox don't exist here)
    # and in the real repos.doe_claude sibling repo's state/lessons-outbox,
    # which a prior diagnostic session accidentally leaked into (see module
    # docstring finding (2)) — even if a future edit accidentally drops an
    # env override or the machine-local stub isn't honoured.
    real_queue_dir = os.path.normpath(os.path.join(_REPO_ROOT, "state", "improvement-queue"))
    real_lessons_dir = os.path.normpath(os.path.join(_REPO_ROOT, "state", "lessons-outbox"))
    before_q = set(os.listdir(real_queue_dir)) if os.path.isdir(real_queue_dir) else set()
    before_l = set(os.listdir(real_lessons_dir)) if os.path.isdir(real_lessons_dir) else set()

    real_doe_claude_lessons_dir = os.environ.get("REPO_DOE_CLAUDE") or os.environ.get("DOE_ROOT")
    before_doe_l: set[str] = set()
    if real_doe_claude_lessons_dir:
        real_doe_claude_lessons_dir = os.path.normpath(
            os.path.join(real_doe_claude_lessons_dir, "state", "lessons-outbox")
        )
        if os.path.isdir(real_doe_claude_lessons_dir):
            before_doe_l = set(os.listdir(real_doe_claude_lessons_dir))

    print("\n-- harvest dedup-scan/write-seam root parity + end-to-end idempotency --")
    failures: list[str] = []
    for test_fn in (
        test_candidate_search_dirs_resolves_machine_local_leg_for_both_scopes,
        test_second_run_idempotent_end_to_end,
    ):
        try:
            test_fn()
            print(f"  PASS: {test_fn.__name__}")
        except AssertionError as exc:
            print(f"  FAIL: {exc}")
            failures.append(str(exc))

    after_q = set(os.listdir(real_queue_dir)) if os.path.isdir(real_queue_dir) else set()
    after_l = set(os.listdir(real_lessons_dir)) if os.path.isdir(real_lessons_dir) else set()
    if after_q - before_q:
        msg = (
            f"real_state_dir_untouched_guard_queue: real state/improvement-queue/ gained "
            f"unexpected file(s): {after_q - before_q} — this suite must never write to "
            f"the real repo state dirs"
        )
        print(f"  FAIL: {msg}")
        failures.append(msg)
    if after_l - before_l:
        msg = (
            f"real_state_dir_untouched_guard_lessons: real state/lessons-outbox/ gained "
            f"unexpected file(s): {after_l - before_l} — this suite must never write to "
            f"the real repo state dirs"
        )
        print(f"  FAIL: {msg}")
        failures.append(msg)
    if real_doe_claude_lessons_dir and os.path.isdir(real_doe_claude_lessons_dir):
        after_doe_l = set(os.listdir(real_doe_claude_lessons_dir))
        if after_doe_l - before_doe_l:
            msg = (
                f"real_doe_claude_lessons_dir_untouched_guard: the REAL repos.doe_claude "
                f"sibling repo's state/lessons-outbox/ ({real_doe_claude_lessons_dir}) gained "
                f"unexpected file(s): {after_doe_l - before_doe_l} — this suite leaked into a "
                f"live sibling repo"
            )
            print(f"  FAIL: {msg}")
            failures.append(msg)

    print()
    print(f"Results: {len(failures)} failed")
    if failures:
        print()
        print("Failures:")
        for msg in failures:
            print(f"  FAIL: {msg}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
