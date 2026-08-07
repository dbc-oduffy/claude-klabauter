#!/usr/bin/env python3
"""test_testpaths_location_guard.py — regression net for the 2026-07-28 rule
"a test-shaped file parked outside `testpaths` is a defect, not a neutral
file": every TRACKED file whose basename matches `test_*.py`, `*.test.py`,
`test-*.py` or `*_test.py` MUST live under a configured `testpaths` root.

WHY THIS EXISTS (the hole it plugs). Its sibling
`test_zero_test_module_ratchet.py` asserts that every test module *under*
`testpaths` contributes at least one collected node, and its NEGATIVE SPEC
names its own limit honestly: a module parked OUTSIDE `testpaths` is
invisible to pytest and therefore invisible to that oracle. Both claude-klabauter
tiers are pure pytest over this config —

    fast_test_cmd: python3 -m pytest -m 'not cadence and not pending_fix and not designed_red' -n auto
    full_test_cmd: python3 -m pytest -m 'not pending_fix and not designed_red'

— and neither names a path, so `testpaths` IS the entire gate surface.
Anything outside it gates nothing, on any tier, ever.

The 2026-07-28 full-tree sweep
(`docs/research/2026-07-28-test-modules-outside-testpaths.md`) found two
populations sitting in exactly that blind spot, both live and both green:

  * `bin/tests/` — 9 modules, 166 passing tests over `claude-klabauter-doctor-probe.py`,
    `claude-klabauter-revendor-cockpit-contract.py`, `shell-init-guard.py` and friends.
    Admitting it was not a one-line widening: `bin/tests/__init__.py` and
    `coordinator/bin/tests/__init__.py` both claimed the top-level package
    name `tests`, so the widening needed `bin/__init__.py` first.
  * `scripts/test_setup.py` — 23 passing tests over `scripts/setup.py`, the
    whole standalone cross-platform install-chain node.

Both were admitted before this guard landed, which is what lets
`_OUTSIDE_TESTPATHS_EXEMPT` below be EMPTY. An empty exemption set is the
point: this is a closed invariant, not a backlog with a test in front of it.

COMPLEMENTS THE RATCHET, DOES NOT OVERLAP IT. The ratchet asks "does every
module in `testpaths` yield a node?"; this asks "is every test-shaped file
IN `testpaths`?". Neither subsumes the other — a file can be perfectly
collectable and still be parked where no tier will ever look at it, which
is precisely what both populations above were.

CONFIG IS READ, NEVER COPIED. `testpaths` is parsed from pyproject.toml at
runtime. A hardcoded duplicate silently stops covering a tree the day
someone widens the config — which is exactly how `coordinator/bin/lib/`
escaped the 2026-07-25 migration's scope.

ORACLE. The tracked-file set from `git ls-files`, filtered to the four
test-shaped basename patterns, minus everything under a `testpaths` root.
Tracked files only: an untracked file is a peer session's in-flight work,
not a landed defect, and flagging it would make this guard fire on every
shared branch until the author committed.

The one subprocess here is `git ls-files`, and that is deliberate rather
than an exception being smuggled in. This guard runs NO pytest and performs
NO collection — the ratchet's own history is the reason (its nested collect
inherited `COORDINATOR_CORE_LAZY_OPS` from the outer run and misreported a
green tree as broken), and a nested pytest would additionally trip the
Tier-U unscoped-run guard. `git ls-files` reads the index and is indifferent
to pytest's environment. Do not replace it with a `--collect-only` diff.

Run: python3 -m pytest coordinator/bin/tests/test_testpaths_location_guard.py
Exit 0 = invariant holds (zero test-shaped files outside `testpaths`);
non-zero = at least one such file exists, or the config/index could not be
read — offending paths and the remediation are printed.

NEGATIVE SPEC
    - Keys on LOCATION, NEVER on collectability. `coordinator/bin/test_cross_repo_memo_roundtrip.py`
      is a DELIBERATELY test-named file that another test invokes as a
      subprocess fixture; it is inside `testpaths` and therefore passes here,
      correctly, regardless of what it collects. Do NOT extend this guard
      toward "...and must contribute a node" — that question belongs to
      `test_zero_test_module_ratchet.py`, which already answers it, and
      extending it here would break this file on that fixture.
    - Has NO skip path, by design. If pyproject.toml is unreadable, lacks
      the pytest ini table, declares no `testpaths`, or `git ls-files`
      fails or returns nothing, it FAILS with the reason. A guard that
      skips when it cannot run has the same fail-quiet shape as the bugs it
      exists to catch. Do not add a `pytest.skip` anywhere in this file.
    - PYTHON SHAPES ONLY. Do NOT extend to `.sh` / `.bats` / `.js` / `.mjs`
      / `.cmd`. 57 such files exist across three heterogeneous sub-classes,
      and for a large minority the correct disposition is "invoked as a
      subprocess fixture by a pytest wrapper" — which a location guard
      cannot distinguish from residue without a hand-maintained list about
      as long as the population, needing an edit on every port. That guard
      would be noise. The right mechanism for that population is resolving
      the dead-orchestrator question (`run-full-tests.py` is a 17-entry
      registry nothing invokes), not a wider net here.
    - Does NOT decide what `testpaths` SHOULD be, and does not assert
      anything about files already inside it. Widening the config is a
      human call; this guard only insists the call gets made.
    - Does NOT scan untracked files (see ORACLE).
    - EXEMPTIONS are individually named files with individually stated
      reasons (see `_OUTSIDE_TESTPATHS_EXEMPT`). No globs, no directory
      exemptions, no "known offenders" bucket — the sweep that motivated
      this guard verified the set can be empty, so if it needs to grow past
      a couple of entries, the invariant is wrong and THAT is the finding,
      not the entries.

Spec backlink: docs/research/2026-07-28-test-modules-outside-testpaths.md
§ "Recommendation on the ratchet's blind spot"; sibling of
coordinator/bin/tests/test_zero_test_module_ratchet.py.
"""
from __future__ import annotations

import fnmatch
import subprocess
import tomllib
from pathlib import Path, PurePosixPath

from coordinator_core.win_portability import no_console_creationflags

_TESTS_DIR = Path(__file__).resolve().parent
# .../coordinator/bin/tests -> .../coordinator/bin -> .../coordinator -> repo root
_REPO_ROOT = _TESTS_DIR.parents[2]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

# The four basename shapes a reader would call "a test file". `test_*.py` is
# the only one `python_files` collects; the other three are here because a
# file wearing them is a test in intent and will be read as gating by anyone
# who finds it, whether or not pytest agrees. All three were extinct in the
# tree as of 2026-07-28 — the dotted shape only because
# `coordinator-artifact-subject.test.py` was renamed that same day, having
# been uncollectable for its whole life.
_TEST_SHAPES = ("test_*.py", "*.test.py", "test-*.py", "*_test.py")

#: Top-level directories excluded from the scan outright — not a `testpaths`
#: root and never intended to become one. `scratchpad` holds session-scoped
#: working files (backups, snapshots, in-progress drafts of test-shaped
#: files) swept aggressively per repo convention; it is not production
#: surface and a test-shaped file parked there is not "gating nothing
#: silently", it is scratch that was never meant to gate anything.
_EXCLUDED_DIR_NAMES = {"scratchpad"}

# ---------------------------------------------------------------------------
# _OUTSIDE_TESTPATHS_EXEMPT — test-shaped files that legitimately live outside
# every `testpaths` root. Every entry is ONE repo-root-relative file path plus
# the specific reason THAT file must stay unreachable by both tiers. Globs,
# directory prefixes and "known offenders" buckets are forbidden.
#
# It is currently EMPTY, and that is load-bearing rather than incidental: the
# 2026-07-28 whole-tree sweep verified zero dotted `*.test.py`, zero dashed
# `test-*.py`, zero trailing `*_test.py`, and — after admitting `bin` and
# `scripts` — zero `test_*.py` outside `testpaths`. If clearing a failure here
# needs an entry, prefer widening `testpaths` (the fix the two 2026-07-28
# admits took) or deleting the file. Reach for an entry only when neither is
# right, and say why in the value.
# ---------------------------------------------------------------------------
_OUTSIDE_TESTPATHS_EXEMPT: dict[str, str] = {}


def _pass(label: str) -> None:
    print(f"  PASS: {label}")


def _fail(label: str, detail: str = "") -> None:
    msg = label if not detail else f"{label}\n    {detail}"
    raise AssertionError(msg)


def _configured_testpaths() -> list[str]:
    """Return pyproject.toml's `testpaths` — read at runtime, never hardcoded.

    Fails loud rather than defaulting: a guard that falls back to a
    hardcoded config when it cannot read the real one is asserting against
    a fiction.
    """
    try:
        with open(_PYPROJECT, "rb") as fh:
            data = tomllib.load(fh)
    except OSError as exc:
        _fail("cannot read pyproject.toml", f"{_PYPROJECT}: {exc}")
    except tomllib.TOMLDecodeError as exc:
        _fail("pyproject.toml is not valid TOML", f"{_PYPROJECT}: {exc}")

    ini = data.get("tool", {}).get("pytest", {}).get("ini_options")
    if not isinstance(ini, dict):
        _fail(
            "pyproject.toml has no [tool.pytest.ini_options] table",
            f"{_PYPROJECT} — this guard derives testpaths from config and "
            "must never carry a hardcoded copy of it.",
        )

    testpaths = ini.get("testpaths")
    if not testpaths:
        _fail(
            "no `testpaths` configured",
            "[tool.pytest.ini_options] must declare testpaths for this guard "
            "to know which locations are gating.",
        )
    return [str(p).replace("\\", "/").strip("/") for p in testpaths]


def _tracked_files() -> list[str]:
    """Every tracked path, repo-root-relative with forward slashes.

    `git ls-files` emits forward slashes on every platform, so the result
    compares directly against `testpaths` entries on Windows too.
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            **no_console_creationflags(),
        )
    except OSError as exc:
        _fail(
            "cannot run `git ls-files` — the tracked-file set is undeterminable",
            f"{exc}\n    This guard fails rather than skips: an undeterminable "
            "file set is the same blind spot it exists to close.",
        )

    if proc.returncode != 0:
        _fail(
            f"`git ls-files` exited {proc.returncode} — the tracked-file set "
            "could not be determined",
            f"stderr: {(proc.stderr or '').strip() or '(empty)'}",
        )

    tracked = [p for p in proc.stdout.split("\0") if p]
    if not tracked:
        _fail(
            "`git ls-files` reported zero tracked files",
            "This repo cannot genuinely be empty — that is a mis-scoped "
            "invocation or a detached working tree, not a green result.",
        )
    return tracked


def _is_test_shaped(rel_path: str) -> bool:
    name = PurePosixPath(rel_path).name
    return any(fnmatch.fnmatch(name, shape) for shape in _TEST_SHAPES)


def _is_excluded(rel_path: str) -> bool:
    """True if ANY component of `rel_path` names an excluded directory (see
    `_EXCLUDED_DIR_NAMES`) — scratch, not a `testpaths` candidate.

    Deliberately matched at any depth rather than only at the repo root: a
    nested `scratchpad/` is the same non-production surface as a top-level
    one, and the alternative (anchoring to the first component) would let
    scratch parked one level down go on failing this guard.

    Review: code-reviewer — verified (via `git ls-files`) that only the
    top-level `scratchpad/` currently exists in the tree, so any-depth
    matching is presently indistinguishable from anchoring to `parts[0]`;
    this choice is about future nested scratch, not present blindness.
    """
    parts = PurePosixPath(rel_path).parts
    return any(part in _EXCLUDED_DIR_NAMES for part in parts)


def _under_testpaths(rel_path: str, testpaths: list[str]) -> bool:
    """True if `rel_path` is a `testpaths` entry or sits beneath one.

    Compares whole path segments, so a `testpaths` entry of `bin` admits
    `bin/tests/test_x.py` without also admitting a sibling `bin-scratch/`.
    """
    parts = PurePosixPath(rel_path).parts
    for entry in testpaths:
        entry_parts = PurePosixPath(entry).parts
        if parts[: len(entry_parts)] == entry_parts:
            return True
    return False


def test_every_test_shaped_file_lives_under_testpaths() -> None:
    """No tracked test-shaped file sits outside every `testpaths` root.

    A file outside `testpaths` is collected by no tier and therefore
    asserts nothing, however green it is when run by hand — which is
    exactly how `bin/tests/`'s 166 passing tests and `scripts/test_setup.py`'s
    23 gated nothing for months.
    """
    testpaths = _configured_testpaths()
    tracked = _tracked_files()

    test_shaped = [
        p for p in tracked if _is_test_shaped(p) and not _is_excluded(p)
    ]
    if not test_shaped:
        _fail(
            "testpaths location scan",
            f"found zero tracked files matching {list(_TEST_SHAPES)} anywhere "
            "in the repo — the scan is almost certainly mis-scoped (repo root "
            "resolution bug), not a genuinely test-free repo.",
        )

    offenders = sorted(
        p
        for p in test_shaped
        if not _under_testpaths(p, testpaths) and p not in _OUTSIDE_TESTPATHS_EXEMPT
    )

    if offenders:
        detail = "\n    ".join(offenders)
        _fail(
            f"{len(offenders)} test-shaped file(s) live OUTSIDE testpaths",
            f"configured testpaths: {testpaths}\n"
            f"    offending paths:\n    {detail}\n"
            "    These files are named like tests but sit where no tier "
            "collects them — both tiers are pure pytest over `testpaths` and "
            "name no path, so they assert nothing, silently, however green "
            "they are when run by hand.\n"
            "    Fix, in preference order: (1) add the containing root to "
            "`testpaths` in pyproject.toml, with a comment recording what the "
            "widening admits and how it was verified — see the existing "
            "entries; (2) move the file under an existing root; (3) delete it "
            "if it is dead. Check for a top-level package-name collision "
            "first: two `tests/` packages under unpackaged parents abort "
            "collection (see bin/__init__.py).\n"
            "    Do NOT add an entry to _OUTSIDE_TESTPATHS_EXEMPT unless none "
            "of those three is right, and state why in the entry.",
        )
    else:
        _pass(
            f"all {len(test_shaped)} tracked test-shaped file(s) live under "
            f"{testpaths} ({len(_OUTSIDE_TESTPATHS_EXEMPT)} documented "
            "exemption(s) applied)"
        )


def test_exemption_entries_are_live_and_individually_justified() -> None:
    """Every `_OUTSIDE_TESTPATHS_EXEMPT` entry names a real file, states a
    reason, and is still outside `testpaths`.

    Stops the set rotting into a "known offenders" bucket. A stale entry
    silently widens the carve-out three ways: the file may be gone, the
    reason may be blank (a glob exemption spelled differently), or the file
    may since have been admitted to `testpaths`, in which case the entry is
    dead weight that would mask a future regression at that path.
    """
    testpaths = _configured_testpaths()

    missing = sorted(
        p for p in _OUTSIDE_TESTPATHS_EXEMPT if not (_REPO_ROOT / p).is_file()
    )
    unjustified = sorted(
        p for p, why in _OUTSIDE_TESTPATHS_EXEMPT.items() if not (why or "").strip()
    )
    now_admitted = sorted(
        p for p in _OUTSIDE_TESTPATHS_EXEMPT if _under_testpaths(p, testpaths)
    )

    if missing or unjustified or now_admitted:
        parts = []
        if missing:
            parts.append("no longer on disk:\n    " + "\n    ".join(missing))
        if unjustified:
            parts.append("no stated reason:\n    " + "\n    ".join(unjustified))
        if now_admitted:
            parts.append(
                "now inside testpaths, so the exemption is dead weight:\n    "
                + "\n    ".join(now_admitted)
            )
        _fail(
            "_OUTSIDE_TESTPATHS_EXEMPT has stale or unjustified entries",
            "\n    ".join(parts)
            + "\n    Fix: delete the stale entry, or state the specific reason "
            "that one file must stay outside every gating root.",
        )
    else:
        _pass(
            f"_OUTSIDE_TESTPATHS_EXEMPT: {len(_OUTSIDE_TESTPATHS_EXEMPT)} "
            "entry(ies), all live and individually justified"
        )
