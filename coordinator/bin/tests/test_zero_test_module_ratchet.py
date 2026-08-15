"""test_zero_test_module_ratchet.py — regression net for the 2026-07-28 rule
"a test module that collects zero test nodes is a defect, not a neutral
file": every file on disk under the configured `testpaths` whose basename
matches a configured `python_files` pattern MUST contribute at least one
collected test node.

WHY THIS EXISTS (the hole it plugs). Three defects landed on 2026-07-28,
all the same family — tests present in the tree that assert nothing on any
tier:

  * a518b11b — two modules computed the repo root one directory too
    shallow. Serial runs carried cwd on sys.path and covered for it; xdist
    workers do not. 119 tests reported as collection ERRORs under the
    gating tier and never ran.
  * 35754b70 — coordinator/bin/lib/test_provenance.py held nine assertions
    in a hand-rolled `run_tests()` under `if __name__ == "__main__"`, with
    no `test_`-prefixed function. It imported cleanly and collected ZERO
    tests: silent serially, silent in parallel, silent per-file.
  * d2835d35 — four modules resolved the repo root by round-tripping
    through its literal directory name, behind a `pytest.skip` gate that
    would have deleted 33 tests silently on a renamed clone.

The 2026-07-25 collectability migration (3e818e6b) verified itself with
"118 modules collect" — a MODULE count, which a module containing zero
tests satisfies perfectly. This suite counts NODES per module instead,
which is the distinction that migration could not make.

FIRST REAL CATCH, and a correction to the record. fed88c4a's commit message
says this guard's first in-tier failure was the shared-branch write race
between its two observations. That was an assumption stated as fact — the
failure text was never captured before the claim was written. When it WAS
captured, the cause was different and the guard was right: `--collect-only`
exited 2 because coordinator_core/ops/tests/test_queue_append_concurrency.py
and test_queue_parity.py fail at import with `'queue.append' not in
_REGISTRY`.

That was first read as an import-ORDER dependency owned by
coordinator_core/ops/ and pre-existing. It was neither, and the correction
matters more than the original claim. The cause was THIS file:
`_nested_pytest_env` copied `os.environ` wholesale, so the nested collect
inherited `COORDINATOR_CORE_LAZY_OPS=1` — which the outer run acquires just
by COLLECTING any module that imports coordinator/bin/lib/cc_invoke.py, since
that module sets the flag process-wide at import. Under it,
`import coordinator_core.ops` skips eager registration and the two import
guards fire correctly. Whole-tree collection in a clean environment was green
throughout. The fix is the `COORDINATOR_CORE_LAZY_OPS` entry in
`_NESTED_PYTEST_ENV_SCRUB` below; the lesson is that this guard's own
subprocess environment is part of its oracle, and an inherited variable is an
outer-run artifact exactly like PYTEST_ADDOPTS.

So the undeterminable-set branch below is not a false positive to be tuned
away; it fired correctly on its first tier run. The race-tolerance in
`_really_yields_no_nodes` remains sound on its own merits — a shared branch
really does mutate between the two observations — but it was never
demonstrated to be the cause of anything, and nothing here should be read as
evidence that it was.

ORACLE (read before changing the mechanism). The offender set is the
on-disk `python_files` set MINUS the set of file paths appearing in
collected node ids, from ONE `--collect-only` subprocess. It is
deliberately NOT a serial-vs-parallel (or any other) collection diff: a
collection diff structurally cannot see a module that collects fine under
both regimes and yields nothing under both, which is the entire failure
mode 35754b70 exhibited.

CONFIG IS READ, NEVER COPIED. `testpaths` and `python_files` are parsed
from pyproject.toml at runtime. A hardcoded duplicate silently stops
covering a tree the day someone widens the config — which is exactly how
`coordinator/bin/lib/` escaped 3e818e6b's scope.

Runs in the fast tier (one subprocess, ~7s against the current ~19.5k-node
tree), not per-file.

Run: python3 -m pytest coordinator/bin/tests/test_zero_test_module_ratchet.py
Exit 0 = invariant holds (zero offenders); non-zero = at least one test
module contributes no test nodes, or the collected set could not be
determined — offending paths and the remediation command are printed.

NEGATIVE SPEC
    - Has NO skip path, by design. If it cannot determine the collected
      set — `--collect-only` exits non-zero, pyproject.toml is unreadable
      or lacks the pytest ini table, the subprocess fails — it FAILS with
      the reason. A guard that skips when it cannot run is the same
      fail-quiet shape as the bugs it exists to catch. Do not add a
      `pytest.skip` anywhere in this file.
    - Does NOT apply the fast tier's `-m` marker filter to the collection.
      The invariant is about COLLECTION, not selection: a module whose
      every test is marked `cadence` legitimately contributes zero nodes
      to the fast tier and is not an offender. Filtering here would
      manufacture false positives for exactly the modules markers exist
      to defer.
    - Does NOT assert anything about how many tests a module contributes,
      what they assert, or whether they pass — one collected node clears
      it. This is a ratchet against silence, not a coverage gate.
    - Does NOT check files outside `testpaths`, and does not itself decide
      what `testpaths` should be. A test module parked in an unconfigured
      directory is invisible to pytest and therefore invisible here; that
      is a testpaths defect, caught by the widening check documented in
      pyproject.toml, not by this suite.
    - Does NOT catch a test module that is BOTH new and broken within a
      single run. `_really_yields_no_nodes` resolves a standalone
      collection error to "not an offender", on the theory that a genuine
      collection-error defect is already failing the run via the whole-tree
      collect. That holds only if the file existed when the whole-tree
      subprocess observed the tree. A module that lands after that
      observation but before the re-check is seen erroring by neither half,
      so the run goes GREEN — an actual pass, not a differently-worded
      failure. Tolerated deliberately: tightening it would resolve the
      shared-branch write race into false positives, which is the failure
      mode that gets a guard muted. It is self-healing — the next
      invocation sees the file stably present and reports it.
    - EXEMPTIONS are individually named files with individually stated
      reasons (see `_ZERO_NODE_EXEMPT`). No globs, no directory
      exemptions, no "known offenders" bucket — if the exemption set needs
      to grow past a couple of entries, the invariant is wrong and that is
      the finding, not the entries.

Spec backlink: 2026-07-28 zero-test-module ratchet dispatch; a518b11b,
35754b70, d2835d35 (the three defects), 3e818e6b (the module-count
migration whose verification this suite supersedes).
"""
from __future__ import annotations

import fnmatch
import os
import subprocess
import sys
import tomllib

import pytest

from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(_TESTS_DIR))
)  # .../coordinator/bin/tests -> .../coordinator/bin -> .../coordinator -> repo root
_PYPROJECT = os.path.join(_REPO_ROOT, "pyproject.toml")

# pytest's built-in `norecursedirs` default, mirrored so the on-disk oracle
# walks exactly the tree pytest walks. This repo does not override
# `norecursedirs` in pyproject.toml; the assertion below fails loud if that
# ever changes, rather than silently scanning a wider tree than pytest does.
_PYTEST_NORECURSEDIRS_DEFAULT = (
    "*.egg",
    ".*",
    "_darcs",
    "build",
    "CVS",
    "dist",
    "node_modules",
    "venv",
    "{arch}",
)

# ---------------------------------------------------------------------------
# _ZERO_NODE_EXEMPT — files that legitimately contribute zero collected test
# nodes. Every entry is ONE repo-root-relative file path plus the specific
# reason THAT file holds no tests. Globs, directory prefixes, and
# "known offenders" buckets are forbidden: this set exists for structural
# impossibilities, not for a backlog. It is currently EMPTY — as of
# 2026-07-28 all 1070 on-disk test modules under `testpaths` contribute at
# least one node, so the invariant holds with no carve-outs at all. If
# clearing a failure here needs more than a couple of entries, stop: that
# means the invariant is mis-stated and the mis-statement is the finding.
# ---------------------------------------------------------------------------
_ZERO_NODE_EXEMPT: dict[str, str] = {}


def _pass(label: str) -> None:
    print(f"  PASS: {label}")


def _fail(label: str, detail: str = "") -> None:
    msg = label if not detail else f"{label}\n    {detail}"
    raise AssertionError(msg)


def _pytest_ini_options() -> dict:
    """Return pyproject.toml's [tool.pytest.ini_options] table.

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
            f"{_PYPROJECT} — this guard derives testpaths/python_files from "
            "config and must never carry a hardcoded copy of them.",
        )
    return ini


def _configured_scope() -> tuple[list[str], list[str]]:
    """Return (testpaths, python_files) as configured — never hardcoded."""
    ini = _pytest_ini_options()

    testpaths = ini.get("testpaths")
    if not testpaths:
        _fail(
            "no `testpaths` configured",
            "[tool.pytest.ini_options] must declare testpaths for this guard "
            "to know what tree pytest collects.",
        )
    python_files = ini.get("python_files")
    if not python_files:
        _fail(
            "no `python_files` configured",
            "[tool.pytest.ini_options] must declare python_files for this "
            "guard to know which on-disk files pytest treats as test modules.",
        )

    if "norecursedirs" in ini:
        _fail(
            "`norecursedirs` is now configured in pyproject.toml",
            "this guard's on-disk walk mirrors pytest's DEFAULT norecursedirs "
            "(_PYTEST_NORECURSEDIRS_DEFAULT). Update that constant to match "
            "the configured value, or the oracle will scan a different tree "
            "than pytest collects.",
        )

    return list(testpaths), list(python_files)


def _is_pruned_dir(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in _PYTEST_NORECURSEDIRS_DEFAULT)


def _ondisk_test_files(testpaths: list[str], python_files: list[str]) -> set[str]:
    """Every file under `testpaths` whose basename matches `python_files`.

    Paths are returned repo-root-relative with forward slashes so they
    compare directly against pytest node ids on every platform.
    """
    found: set[str] = set()
    for entry in testpaths:
        abs_entry = os.path.join(_REPO_ROOT, *entry.split("/"))
        # Every current `testpaths` entry is a directory, so this branch is
        # dead as configured and unexercised by any test here. It exists
        # because pytest accepts a bare file as a `testpaths` entry, and the
        # walk below would silently skip one.
        if os.path.isfile(abs_entry):
            if any(
                fnmatch.fnmatch(os.path.basename(abs_entry), p) for p in python_files
            ):
                found.add(entry)
            continue
        for root, dirs, files in os.walk(abs_entry):
            dirs[:] = [d for d in dirs if not _is_pruned_dir(d)]
            for name in files:
                if not any(fnmatch.fnmatch(name, p) for p in python_files):
                    continue
                rel = os.path.relpath(os.path.join(root, name), _REPO_ROOT)
                found.add(rel.replace(os.sep, "/"))
    return found


"""Env vars stripped from a nested `--collect-only` subprocess's environment.

Every entry guards the same thing: the nested collect must observe what a
bare `pytest --collect-only` would, not what the outer run happens to be
doing.

  * `PYTEST_ADDOPTS` could inject a marker filter or extra paths and
    silently reshape the oracle.
  * The xdist vars are inherited whenever the outer suite runs under
    `-n auto` (the fast tier always does) and would tell the nested
    one-shot invocation it is a worker of a parallel run it is not part
    of, changing how any conftest or plugin that branches on worker
    identity behaves.
  * `COORDINATOR_CORE_LAZY_OPS` makes `import coordinator_core.ops` skip
    its eager per-module import list, so only ops reached by a targeted
    lazy import ever register. ~50 test modules assert the op registry at
    import time, so a collection process that inherits it fails collection
    outright. It is NOT an outer-run choice: `coordinator/bin/lib/cc_invoke.py`
    sets it process-wide as an import side effect, so merely COLLECTING any
    test module that imports cc_invoke arms it for every child this process
    later spawns (see that module's 2026-07-26 leak note and `child_env()`,
    the same fix applied at two spawn sites in coordinator/bin/). Stripped
    unconditionally rather than only when cc_invoke injected it, because a
    pytest process is never a `coordinator_core.invoke` dispatch process —
    lazy ops is incompatible with collecting this suite under any
    provenance, an operator's own export included.
"""
_NESTED_PYTEST_ENV_SCRUB = (
    "PYTEST_ADDOPTS",
    "PYTEST_XDIST_WORKER",
    "PYTEST_XDIST_WORKER_COUNT",
    "PYTEST_XDIST_TESTRUNUID",
    "COORDINATOR_CORE_LAZY_OPS",
)


def _nested_pytest_env() -> dict[str, str]:
    """Environment for a nested `--collect-only` subprocess.

    See `_NESTED_PYTEST_ENV_SCRUB` for what is removed and why.
    """
    env = dict(os.environ)
    for var in _NESTED_PYTEST_ENV_SCRUB:
        env.pop(var, None)
    return env


def _collected_test_files() -> set[str]:
    """File paths appearing in collected node ids, from ONE subprocess.

    Passes no path arguments so pytest resolves `testpaths` from config
    itself — the same resolution the real tiers use. Applies no `-m`
    filter (see NEGATIVE SPEC).
    """
    env = _nested_pytest_env()

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
        **no_console_creationflags(),
    )

    if proc.returncode != 0:
        _fail(
            f"`pytest --collect-only` exited {proc.returncode} — the collected "
            "set could not be determined",
            "This guard fails rather than skips: an undeterminable collected "
            "set is the same blind spot it exists to close. Collection errors "
            "are themselves the defect class a518b11b belonged to — fix the "
            "collection error first.\n"
            f"    stdout tail:\n{_tail(proc.stdout)}\n"
            f"    stderr tail:\n{_tail(proc.stderr)}",
        )

    files = {
        line.split("::", 1)[0].strip()
        for line in proc.stdout.splitlines()
        if "::" in line
    }
    if not files:
        _fail(
            "`pytest --collect-only` reported zero collected node ids",
            "The whole repo cannot genuinely have no tests — this is a "
            "mis-scoped collection or an output-format change, not a green "
            "result.\n"
            f"    stdout tail:\n{_tail(proc.stdout)}",
        )
    return {f.replace(os.sep, "/") for f in files}


def _tail(text: str, n: int = 25) -> str:
    lines = (text or "").splitlines()[-n:]
    return "\n".join(f"      {line}" for line in lines) or "      (empty)"


def _really_yields_no_nodes(rel_path: str) -> bool:
    """Re-collect ONE candidate to confirm it genuinely yields no nodes.

    The two halves of the oracle are separate observations of a tree that
    several sessions write to concurrently: the on-disk walk runs first, the
    `--collect-only` subprocess after. A file that lands, moves, or is still
    being written between the two shows up as a difference without being a
    defect. Without this second look the guard cries wolf on a shared branch,
    and a guard that cries wolf gets muted — which would leave the hole it
    exists to close open again, just with a passing test in front of it.

    Fail-safe direction is deliberate: a candidate that has since vanished, or
    whose re-collection cannot be completed, returns False (not an offender).
    A real zero-node module is a standing condition and will be caught on the
    next run; a transient one must never be reported as a defect. This is the
    single place in this guard where uncertainty resolves to silence, and it
    is scoped to one already-identified path — never to the invariant itself.
    """
    abs_path = os.path.join(_REPO_ROOT, *rel_path.split("/"))
    if not os.path.isfile(abs_path):
        return False  # vanished between the two observations — a race, not a defect

    env = _nested_pytest_env()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", abs_path],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
        **no_console_creationflags(),
    )
    # pytest exits 5 for "no tests collected" — that is not an error, it is
    # this guard's positive signal, and reading it as an error is how the
    # re-verification silently stopped detecting anything the first time it
    # was written. 0 means nodes were collected (or none, if stdout says so);
    # anything else is a collection ERROR — a real defect, but of the
    # a518b11b class, which pytest already fails the run on by itself. Do not
    # double-report that under this guard's label.
    _NO_TESTS_COLLECTED = 5
    if proc.returncode == _NO_TESTS_COLLECTED:
        return True
    if proc.returncode != 0:
        return False
    return not any("::" in line for line in proc.stdout.splitlines())


def test_every_test_module_contributes_at_least_one_node() -> None:
    """Every on-disk `python_files` match under `testpaths` yields >= 1 node.

    A module that imports cleanly and collects nothing is indistinguishable
    from a passing module in every tier summary — that is precisely what
    made 35754b70's nine dead assertions invisible for three days.
    """
    testpaths, python_files = _configured_scope()
    ondisk = _ondisk_test_files(testpaths, python_files)

    if not ondisk:
        _fail(
            "zero-node-module scan",
            f"found zero on-disk files matching {python_files} under "
            f"{testpaths} — the scan is almost certainly mis-scoped (repo "
            "root resolution bug), not a genuinely test-free repo.",
        )

    collected = _collected_test_files()
    candidates = sorted(ondisk - collected - set(_ZERO_NODE_EXEMPT))
    offenders = [p for p in candidates if _really_yields_no_nodes(p)]

    if offenders:
        detail = "\n    ".join(offenders)
        _fail(
            f"{len(offenders)} test module(s) under testpaths contribute ZERO "
            "collected test nodes",
            f"offending paths:\n    {detail}\n"
            "    These files are named like tests and are inside testpaths, "
            "but pytest collects nothing from them — they assert nothing on "
            "any tier, silently.\n"
            "    Diagnose: python3 -m pytest --collect-only <path>\n"
            "    Usual causes: assertions parked in a hand-rolled runner "
            "under `if __name__ == \"__main__\"` with no `test_`-prefixed "
            "function (35754b70); every test function renamed out of the "
            "`python_functions` convention; a module-level `return`/guard "
            "that elides the definitions.\n"
            "    Fix: convert the assertions to `test_`-prefixed functions "
            "1:1 (see coordinator/bin/lib/test_provenance.py for the "
            "conversion idiom). Do NOT add an entry to _ZERO_NODE_EXEMPT "
            "unless the file is STRUCTURALLY incapable of holding a test.",
        )
    else:
        _pass(
            f"all {len(ondisk)} on-disk test module(s) under {testpaths} "
            f"contribute at least one collected node "
            f"({len(_ZERO_NODE_EXEMPT)} documented exemption(s) applied)"
        )


# One representative module rather than the whole tree: its import-time
# `assert "queue.append" in _REGISTRY` is the exact guard a leaked
# COORDINATOR_CORE_LAZY_OPS trips, and collecting one file costs ~1s where
# the whole tree costs ~7s. Pinned by path deliberately — if it moves or its
# import guard is deleted, this regression net must fail loudly and be
# re-pointed, not quietly stop proving anything.
_LAZY_OPS_CANARY_MODULE = "coordinator_core/ops/tests/test_queue_parity.py"


@pytest.mark.allow_environ_leak
def test_nested_collect_survives_a_leaked_lazy_ops_flag(monkeypatch) -> None:
    """A nested collect must not inherit COORDINATOR_CORE_LAZY_OPS.

    Regression for the 2026-07-28 defect this guard misdiagnosed as an
    import-order bug in coordinator_core/ops/ (see the module docstring's
    FIRST REAL CATCH). Importing coordinator/bin/lib/cc_invoke.py sets the
    flag process-wide, so any outer run that merely COLLECTS a cc_invoke
    consumer armed it for every child spawned afterwards — and under it,
    `import coordinator_core.ops` skips eager registration, so the canary
    module's import-time registry assertion fails and `--collect-only`
    exits 2 on a green tree.

    Asserts through a real subprocess rather than on the returned dict:
    a dict-shape assertion would still pass if a future spawn site stopped
    routing through `_nested_pytest_env`.
    """
    canary = os.path.join(_REPO_ROOT, *_LAZY_OPS_CANARY_MODULE.split("/"))
    assert os.path.isfile(canary), (
        f"canary module {_LAZY_OPS_CANARY_MODULE} is gone — re-point "
        "_LAZY_OPS_CANARY_MODULE at another module that asserts the op "
        "registry at import time, or this test proves nothing."
    )

    monkeypatch.setenv("COORDINATOR_CORE_LAZY_OPS", "1")

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", canary],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=_nested_pytest_env(),
        **no_console_creationflags(),
    )

    assert proc.returncode == 0, (
        "nested `--collect-only` exited "
        f"{proc.returncode} with COORDINATOR_CORE_LAZY_OPS=1 in the parent "
        "environment — _nested_pytest_env is leaking the flag into the "
        "child, which skips eager op registration and breaks collection of "
        "every module that asserts the op registry at import time.\n"
        f"stdout tail:\n{_tail(proc.stdout)}\nstderr tail:\n{_tail(proc.stderr)}"
    )


def test_exemption_entries_are_live_and_individually_justified() -> None:
    """Every `_ZERO_NODE_EXEMPT` entry names a real file and states a reason.

    Stops the set rotting into a "known offenders" bucket: a stale entry
    (file since deleted or since fixed) silently widens the carve-out, and
    an empty reason is the glob exemption this design forbids, spelled
    differently.
    """
    missing = sorted(
        p for p in _ZERO_NODE_EXEMPT if not os.path.isfile(os.path.join(_REPO_ROOT, *p.split("/")))
    )
    unjustified = sorted(p for p, why in _ZERO_NODE_EXEMPT.items() if not (why or "").strip())

    if missing or unjustified:
        parts = []
        if missing:
            parts.append("no longer on disk:\n    " + "\n    ".join(missing))
        if unjustified:
            parts.append("no stated reason:\n    " + "\n    ".join(unjustified))
        _fail(
            "_ZERO_NODE_EXEMPT has stale or unjustified entries",
            "\n    ".join(parts)
            + "\n    Fix: delete the stale entry, or state the specific "
            "reason that one file cannot hold a collected test.",
        )
    else:
        _pass(
            f"_ZERO_NODE_EXEMPT: {len(_ZERO_NODE_EXEMPT)} entry(ies), all live "
            "and individually justified"
        )
