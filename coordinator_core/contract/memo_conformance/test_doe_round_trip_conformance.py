"""
coordinator_core.contract.memo_conformance.test_doe_round_trip_conformance -- memo-tool
round-trip conformance gate for claude-klabauter's native memo verbs.

Purpose: run the round-trip conformance fixture
(``coordinator/bin/test_cross_repo_memo_roundtrip.py``) -- now CLAUDE-KLABAUTER-OWNED, resolved
from claude-klabauter's own tree via ``_claude_klabauter_root()`` -- and assert claude-klabauter's real ``memo.send``
op still satisfies the schema / DR-026 filename lockstep / delivery-collision contract
across all 5 lockstep sites.

Ownership note (2026-07-22): the 2026-07-21 executable-surface migration relocated this
fixture out of coordinator-claude's ``coordinator/bin/`` into claude-klabauter's own ``coordinator/bin/``
(``/Users/example-operator/X/coordinator-claude/coordinator/bin/cross-repo-memo-roundtrip.test.py`` no
longer exists). Before this fix the gate resolved the fixture off a coordinator-claude clone via
``doe_drift.resolve_doe_clone()`` and skipped loud when unresolvable -- since the
migration that meant the fixture was silently absent at the old path and the gate
skipped on every run, thinning contract coverage to zero without failing anything. The
fixture is now resolved from claude-klabauter's own tree and a missing fixture FAILS (not skips)
-- skip-loud was bootstrap-safety for a foreign clone that might not exist on every
machine; that posture does not apply to a file that lives in our own repo. Trigger:
``cross-repo/inbox/2026-07-22-claude-central-em-auto-push-hook-fixed-generator-now-yours.md``.

This is DELIBERATELY NOT a byte-compare against the old coordinator-claude CLI's output: the tool
this fixture gates intentionally diverges from the coordinator-claude CLI's former bytes on all five
footguns the ownership move exists to fix (fail-loud vs. silent folder-scan, prose-first
summary vs. heading-first, no-write dry-run resolution, nearest-match suggestion,
supersedes-based re-delivery vs. hand-edit clobber). The oracle here is the documented
CONTRACT (schema-valid frontmatter, the `kind` enum, DR-026 filename shape, collision
semantics), not any CLI's byte-identical behavior. See CONTRACT-VS-ERGONOMIC.md for the
full split between contract-invariant and CLI-ergonomic coverage in the original coordinator-claude
CLI's ~139-test suite, so cutover did not silently thin contract coverage.

Negative-spec: does NOT re-run ``coordinator/tests/test_cross_repo_memo_collision.py`` --
that file exercises the (retired) coordinator-claude CLI's OWN ``_write_file`` helper (the former oracle
side), not claude-klabauter's op; claude-klabauter's equivalent collision semantics are already covered
locally by ``coordinator_core/ops/fleet/tests/test_memo_send.py``, and the round-trip
fixture's own Collision 1/1b/2 + AC9 cases already dispatch claude-klabauter's REAL memo.send op
end-to-end (see CONTRACT-VS-ERGONOMIC.md).

Why this wrapper still exists after `coordinator/bin` joined ``testpaths`` (2026-07-25):
default collection now reaches the fixture directly, so the wrapper is NOT the last line
of defence for *running* it. It survives for the two vacuity modes direct collection
cannot see, both of which need a subprocess to observe:

  1. ``CLAUDE_KLABAUTER_ROOT`` pinning. The fixture's real-op sites (1, 2, 4, 5 + AC9) call its own
     ``skip_test()`` and ``return`` when the ambient resolver ladder cannot find a claude-klabauter
     checkout. That is a plain early return, so pytest scores those sites as PASSED, not
     skipped -- an all-skip run is byte-identical to a real 10-passed run from pytest's
     side. This wrapper pins ``CLAUDE_KLABAUTER_ROOT`` to the enclosing checkout (honoured first by
     ``cc_invoke._resolve_claude_klabauter_root``) so the real-op sites cannot degrade, and asserts
     no ``SKIP:`` line was printed if they somehow do.
  2. Positive evidence that the fixture reported anything at all. See the negative-spec
     block below.

Negative-spec (2026-07-28): this gate previously ran the fixture as a bare SCRIPT and
asserted only the ABSENCE of ``FAIL:`` lines in its stdout. Commit ``3e818e6b`` ("make
coordinator/bin tests collectable") removed the fixture's ``if __name__ == "__main__"``
runner, so the subprocess emitted NOTHING and exited 0 -- "no FAIL: lines in an empty
string" is trivially true, and the gate passed in 0.06s against the fixture's real ~4s.
The 5-site conformance gate had itself become the silent guard failure it exists to
prevent. The fixture is therefore invoked via ``-m pytest`` (not as a script), and every
assertion below is POSITIVE-evidence-shaped -- an unexplained non-zero exit fails, a
below-floor effective-coverage count fails, a printed SKIP fails. An absence-only
assertion is what made this vacuous; do not reintroduce one as the sole check.

Negative-spec (2026-07-28, ASSERTION ORDER -- do not re-sort these): the known-bad
reconciliation (``_KNOWN_DOE_SIDE_FAILURES``) is computed FIRST and every other
assertion is expressed in terms of it. An earlier revision asserted the floor and
``returncode == 0`` BEFORE consulting the known-bad set, which defeated the tolerance
mechanism entirely: one tolerated failure makes the inner runner report ``9 passed,
1 failed``, so the floor fired at ``9 < 10`` claiming the coverage "has been silenced"
(false -- it ran and failed by name), and even past the floor the inner runner's
non-zero exit fired before ``_matches_known`` was ever reached. A named, tracked
failure could therefore never pass this gate, and the misleading floor message also
mislabelled a genuinely NEW 1-of-10 regression as silenced coverage. The set was empty
at the time so the bug was dormant, but the History block below shows it gets
repopulated repeatedly. The three outcomes are now reported distinguishably: NEW
failure, silenced coverage, and tolerated known failure.

Negative-spec (2026-07-28, FAILURE DETECTION): failing test names are read from
pytest's own ``FAILED <file>::<name>`` short-summary lines, not only from the fixture's
legacy hand-rolled ``FAIL: <name>`` prints. ``3e818e6b`` converted the fixture to plain
pytest asserts, so it emits no ``FAIL:`` line at all any more -- matching only that
shape meant ``_KNOWN_DOE_SIDE_FAILURES`` could never match anything, however correctly
ordered the assertions were. Both shapes are matched so the set works against the
fixture as it is today and as it was.

Spec backlink: pln-memo-tool-rebuild-claude-klabauter-owns--bd5745 § C8 (A7/A8)
Spec backlink: docs/decisions/DR-210-claude-klabauter-native-tooling-ownership-strangler.md § 2(a)/2(b)
Oracle: coordinator/bin/test_cross_repo_memo_roundtrip.py (claude-klabauter-owned as of 2026-07-21;
resolved from claude-klabauter's own tree, no vendoring, no foreign-clone dependency)
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Known-bad tracking -- claude-klabauter-owned fixture defects, tracked pending a fixture-side
# fix (not a coordinator-claude-relay any more -- see ownership note below)
# ---------------------------------------------------------------------------
# History: Site 4 (surface-script glob) previously failed on a then-coordinator-claude-owned fixture
# defect -- test_cross_repo_memo_roundtrip.py's test_site4_surface_glob shelled the
# Python surface script (`workday-start-cross-repo-memo-surface.py`, shebang
# `#!/usr/bin/env python3`) via `subprocess.run(["bash", surface_script], ...)` instead
# of the Python interpreter. Relayed to claude-central-em via the C8 cutover memo rather
# than patched here (claude-klabauter did not own coordinator-claude/coordinator/bin/ at the time). Coordinator-claude
# fixed it in d55d882d ("Site-4 roundtrip test invoked python source via bash", on top
# of the 28a7b868 de-polyglot caller migration); the fixture now invokes it via
# `[sys.executable, surface_script]`. The healed assertion below fired (2026-07-21) and
# Site 4 was un-masked and removed here, per the set's own do-not-leave-stale contract.
# Source memo:
# cross-repo/inbox/2026-07-21-claude-central-em-memo-tool-cutover-ack-and-site4-already-fixed.md.
#
# History (healed 2026-07-22): Site 2 (schema applies_to) and Site 3a/3b (own-inbox
# guard hook) broke on the 2026-07-21/22 executable-surface migration -- the fixture
# resolved `coordinator/schemas/cross-repo-memo.schema.json` and
# `coordinator/hooks/scripts/validate-frontmatter-schema.js` relative to its OWN
# `_bin_dir()` (dirname(__file__)), which resolved inside claude-klabauter's checkout after the
# fixture relocated there, but per a0bb05a0's rationale "schemas are contract and stay
# coordinator-claude-side permanently" (schemas/ and hooks/ were deliberately NOT migrated with
# bin/lib/scripts/tests). Fixed in the fixture itself
# (coordinator/bin/test_cross_repo_memo_roundtrip.py's new `_doe_bin_dir()` helper,
# following the same DOE_ROOT/REPO_EXAMPLE_DOCTRINE_REPO env -> machine-local repos.example_doctrine_repo
# resolution pattern as coordinator_registry.doe_root() /
# percolate-preflight-scratch-publish.py's _resolve_coordinator_root()). Direct run
# confirmed 2026-07-22: all 10 fixture tests pass with a resolvable coordinator-claude clone.
#
# To track a NEW claude-klabauter-owned fixture defect: add its fixture test NAME (prefix-matchable)
# here, never swallow-all -- so a genuinely new claude-klabauter regression is never silently
# folded in, and so a later fixture-side fix un-masks as a hard failure (forcing
# narrowing, not staleness).
_KNOWN_DOE_SIDE_FAILURES: frozenset[str] = frozenset()

# FAIL: lines are the fixture's own test *name* immediately followed by
# `fail_test`'s free-form reason (here, the surface script's captured
# stderr/traceback, which spans multiple physical lines and is not part of the
# stable test identity) -- match by NAME PREFIX, not full-line equality, so a
# reason-text change alone does not spuriously read as a new/healed failure.
# LEGACY SHAPE: the fixture printed these while it was a hand-rolled script
# runner; since `3e818e6b` converted it to plain pytest asserts it prints none.
# Kept so the known-bad set still reads a hand-rolled failure line if one is ever
# reintroduced -- but it is no longer the primary detector (see below).
_FAIL_LINE_RE = re.compile(r"^\s*FAIL:\s*(.+?)\s*$", re.MULTILINE)

# PRIMARY SHAPE: pytest's own short-summary line, `FAILED <file>::<name>` with an
# optional ` - <reason>` tail. This is what the inner `-m pytest -q` run actually
# emits today. Only the NAME is captured (the file path and the reason text are
# not part of the stable test identity `_KNOWN_DOE_SIDE_FAILURES` is keyed on).
_PYTEST_FAILED_RE = re.compile(r"^FAILED\s+\S+?::(\S+)", re.MULTILINE)

# The fixture's OWN loud-skip marker (its `skip_test()` prints `  SKIP: <name> — <reason>`).
# A skip is a plain early `return` in the fixture, so pytest scores it PASSED -- this regex
# is the only way an outside observer can tell an all-skip run from a real one. Requires
# `-s` on the inner pytest invocation, since pytest swallows a passing test's stdout.
#
# NOT anchored at line start (2026-07-28): the inner run is `-q -s`, so pytest's own
# progress characters share a physical line with whatever the test prints -- a real
# all-skip run renders as `.  SKIP: <name> — <reason>`, and a `^\s*SKIP:` anchor
# silently failed to match every skip after the first. That made this guard, whose
# whole job is catching an all-skip run, unreliable in exactly the case it exists for
# (verified 2026-07-28 by forcing a skip print in a non-first fixture test: the
# anchored form matched nothing and the gate went green on that assertion).
_SKIP_LINE_RE = re.compile(r"SKIP:\s*(.+?)\s*$", re.MULTILINE)

# Positive-evidence floor. The fixture covers the 5 lockstep sites (site 3 splits into
# 3a/3b), the 3 collision cases, and AC9 -- 10 tests as of 2026-07-28. Asserted as a
# FLOOR, not an equality, so adding fixture cases never spuriously reds this gate, while
# silencing cases (or the whole run, as `3e818e6b` did) still fails loud.
_MIN_FIXTURE_TESTS = 10
_PYTEST_PASSED_RE = re.compile(r"(\d+) passed", re.MULTILINE)


def _matches_known(failing_line: str, known_names: frozenset[str]) -> bool:
    return any(failing_line.startswith(known) for known in known_names)


def _failing_names(output: str) -> set[str]:
    """Every failing test identity the inner runner reported, from either shape.

    Union of pytest's own ``FAILED <file>::<name>`` short-summary lines (what the
    fixture emits today) and the fixture's legacy hand-rolled ``FAIL: <name>``
    prints. Matched by name so ``_KNOWN_DOE_SIDE_FAILURES``'s prefix contract
    holds identically against both.
    """
    return set(_PYTEST_FAILED_RE.findall(output)) | set(_FAIL_LINE_RE.findall(output))


def _claude_klabauter_root() -> Path:
    # this file: coordinator_core/contract/memo_conformance/test_*.py
    return Path(__file__).resolve().parents[3]


def test_doe_round_trip_conformance_fixture() -> None:
    """claude-klabauter's memo.send op clears the 5-site + collision + AC9 round-trip fixture.

    The fixture is claude-klabauter-owned (relocated 2026-07-21 from coordinator-claude's
    coordinator/bin/ into claude-klabauter's own coordinator/bin/) -- a missing fixture here is a
    genuine local defect (a bad relocation, a stale checkout) and FAILS loud rather
    than skipping. Skip-loud was the correct bootstrap-safety posture only while the
    fixture lived in a foreign coordinator-claude clone that might not be resolvable on every
    machine; that posture no longer applies to a file that lives in our own tree.
    """
    claude_klabauter_root = _claude_klabauter_root()
    fixture = claude_klabauter_root / "coordinator" / "bin" / "test_cross_repo_memo_roundtrip.py"
    if not fixture.is_file():
        pytest.fail(
            f"round-trip conformance fixture not found at {fixture} -- expected in "
            "claude-klabauter's own tree since the 2026-07-21 executable-surface migration "
            "(it is no longer hosted by coordinator-claude)"
        )
        return

    env = {**os.environ, "CLAUDE_KLABAUTER_ROOT": str(claude_klabauter_root)}
    result = subprocess.run(
        # `-m pytest`, NOT a bare script run: the fixture has no `__main__` runner (removed
        # by 3e818e6b) and a script run therefore does nothing at all. `-s` keeps the
        # fixture's own `SKIP:` prints visible; `-p no:cacheprovider` keeps the inner run
        # from writing a .pytest_cache into the outer run's tree.
        [sys.executable, "-m", "pytest", str(fixture), "-q", "-s", "-p", "no:cacheprovider"],
        cwd=str(claude_klabauter_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        # This is the highest-fanout subprocess in this contract set -- it launches a
        # nested pytest runner that itself dispatches memo.send and shells further
        # subprocesses. An inherited stdin lets any of them block a headless run
        # forever on a prompt nobody can answer; DEVNULL turns that into an EOF.
        # Matches the pinning on every other subprocess call in this work
        # (the fixture's own `_git()`, review_trail_readjudication_report._run).
        stdin=subprocess.DEVNULL,
        # popup-safe-env-suppressed: no console window on Windows (headless test run).
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    output = f"{result.stdout}\n{result.stderr}"

    # Known-bad reconciliation FIRST -- every assertion below is expressed in terms of
    # it. Asserting the floor or the exit code before this point is what defeated the
    # tolerance mechanism (see the ASSERTION ORDER negative-spec above).
    failing = _failing_names(output)
    tolerated = {name for name in failing if _matches_known(name, _KNOWN_DOE_SIDE_FAILURES)}
    unexpected = failing - tolerated

    passed_counts = [int(n) for n in _PYTEST_PASSED_RE.findall(output)]
    passed = max(passed_counts, default=0)

    # (1) NEW failure -- the most specific and most actionable outcome, so it is
    # reported ahead of the coarser floor/exit checks a 1-of-10 regression would
    # otherwise trip first with a misleading "coverage has been silenced" message.
    assert not unexpected, (
        "round-trip conformance fixture reported NEW failure(s) beyond the tracked "
        f"known-bad set -- claude-klabauter contract regression suspected: {sorted(unexpected)}\n"
        "This is a NEW failure, not silenced coverage and not a tolerated known-bad: "
        "the named test(s) ran and failed. Fix the regression, or (only if the defect "
        "is genuinely fixture-side and tracked) add the name to "
        f"_KNOWN_DOE_SIDE_FAILURES.\nFull fixture output:\n{output}"
    )

    skipped = set(_SKIP_LINE_RE.findall(output))
    assert not skipped, (
        "round-trip conformance fixture LOUD-SKIPPED real-op site(s) despite CLAUDE_KLABAUTER_ROOT "
        f"being pinned to {claude_klabauter_root} -- the memo.send op was never dispatched, so the "
        "sites in question assert nothing while still scoring as pytest passes: "
        f"{sorted(skipped)}\nFull fixture output:\n{output}"
    )

    # (2) A non-zero exit is EXPECTED when -- and only when -- a tracked known-bad
    # failure explains it: the inner runner exits non-zero on any inner failure, so a
    # tolerated failure necessarily produces one. An unexplained non-zero exit (crash,
    # collection error, "no tests ran") still fails hard, which is what keeps a
    # vacuous/empty run red.
    assert result.returncode == 0 or tolerated, (
        f"round-trip conformance fixture exited {result.returncode} with no tracked "
        "known-bad failure that would explain it -- claude-klabauter contract regression, inner "
        "collection error, or a vacuous run.\n"
        f"Full fixture output:\n{output}"
    )

    # (3) Coverage floor, counted over tests that RAN: passing tests plus tolerated
    # known-bad failures. A tracked failure is coverage that ran and reported, not
    # coverage that was silenced, so folding it in here is what lets the known-bad set
    # work at all; only genuinely-missing tests can now trip this.
    effective_coverage = passed + len(tolerated)
    assert effective_coverage >= _MIN_FIXTURE_TESTS, (
        "round-trip conformance fixture accounted for "
        f"{effective_coverage} tests ({passed} passed + {len(tolerated)} tolerated "
        f"known-bad: {sorted(tolerated)}), below the {_MIN_FIXTURE_TESTS}-test floor -- "
        "the 5-site lockstep coverage has been SILENCED, not merely changed or failing. "
        "This is the exact failure mode 3e818e6b introduced (fixture emitted nothing, "
        "gate passed on an empty string). Do NOT lower the floor to match; find what "
        f"stopped running.\nFull fixture output:\n{output}"
    )

    healed = {
        known for known in _KNOWN_DOE_SIDE_FAILURES
        if not any(name.startswith(known) for name in failing)
    }
    assert not healed, (
        f"Tracked coordinator-claude-side fixture failure(s) {sorted(healed)} no longer reproduce -- "
        "coordinator-claude has apparently fixed the surface-script invocation bug. Narrow "
        "_KNOWN_DOE_SIDE_FAILURES accordingly (do not leave a stale known-bad entry "
        "that would mask a real future regression under the same test name)."
    )
