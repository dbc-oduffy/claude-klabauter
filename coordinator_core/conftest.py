"""Suite-root conftest — suite-wide quarantine of the real user home directory.

Lives at ``coordinator_core/`` rather than the repo root so that it still
loads under a ``rootdir = coordinator_core`` invocation — when the path
argument's ancestry reaches ``coordinator_core/pytest.ini`` first, that file
wins as the configfile, ``confcutdir`` becomes ``coordinator_core/``, and the
repo-root conftest sits above the cut and never loads. That is specific to
that invocation: for a whole-suite run rootdir is the repo root
(``pyproject.toml``'s ``[tool.pytest.ini_options]`` is the configfile) and a
repo-root ``conftest.py`` does load — see the one at the repo root, which
relies on exactly that to fix the lazy-ops import-ordering hazard.

Why this exists
---------------
Dozens of tests in this suite sandbox the home directory with
``monkeypatch.setenv("HOME", str(tmp_path))``. On POSIX that is sufficient:
``os.path.expanduser("~")`` consults ``HOME`` first. **On Windows it is a
no-op** — ``expanduser`` prefers ``USERPROFILE`` (and then
``HOMEDRIVE``+``HOMEPATH``), falling back to ``HOME`` only when all of those
are absent. ``USERPROFILE`` is always set on Windows, so the sandbox is
bypassed and the code under test resolves the REAL ``C:\\Users\\<you>``.

That is not a theoretical leak. On 2026-07-20 three sibling repos independently
reported that running this suite on Windows wrote a pytest tmpdir into the real
``~/.claude/.doe-root``, repointing every coordinator skill on the machine at a
directory that vanishes on the next tmp reap. See:

- ``cross-repo/inbox/2026-07-20-claude-central-em-doe-root-pointer-test-clobbers-real-home.md``
- ``cross-repo/inbox/2026-07-20-claude-central-em-doe-root-pointer-test-corrupts-live-machine-config.md``
- ``cross-repo/inbox/2026-07-20-example-cockpit-repo-em-doe-root-clobbered-by-windows-test-home-leak.md``

The fix is structural rather than per-site: point EVERY home-resolution
variable at a throwaway per-test directory before the test runs. A test that
then sets only ``HOME`` still isolates correctly on POSIX, and on Windows
resolves into the quarantine instead of the real profile — so the failure mode
degrades from "silently corrupts live machine config" to "assertion fails in a
tmpdir". Per-site ``HOME``/``USERPROFILE`` pairing remains the clearer thing to
write (see ``coordinator_core.testing.home_sandbox.sandbox_home``); this
fixture is the backstop for the ones nobody remembered to pair.
"""

from __future__ import annotations

import os
import site

import pytest

# ---------------------------------------------------------------------------
# Package-conftest visibility patch (2026-07-28 — bare-file-arg Package-cache
# clobber)
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS. A multi-file `pytest` invocation that mixes a bare file
# path (no `::test_name` suffix) sitting directly under a Package directory
# with OTHER args that revisit one of that Package's own subpackages can make
# a whole subpackage's conftest fixtures (e.g. `handoff_repo` in
# coordinator_core/ops/tests/conftest.py) invisible to some — not all — of the
# tests in that subpackage, with pytest reporting "fixture 'X' not found" even
# though the conftest imported cleanly and the fixture is defined. Minimal
# repro (confirmed on pytest 9.1.1, no repo-side conftest or testpaths change
# involved — reproduces identically under `--confcutdir=coordinator_core`,
# which excludes the repo-root conftest entirely):
#
#     python3 -m pytest \
#         coordinator_core/ops/tests/test_handoff_reconcile_report.py \
#         coordinator_core/test_baton_assemble.py \
#         coordinator_core/ops/tests/test_handoff_archive_transition.py -q
#
# MECHANISM (verified via a diagnostic pytest plugin patching
# `_pytest.main.Session.collect`/`pytest_collectstart`, not guessed):
# `Session.collect()` walks each cmdline arg's path components against a
# `self._collection_cache` keyed by the PARENT collector object, so revisiting
# an already-collected Package normally reuses its cached children — EXCEPT
# for one case: `handle_dupes = not (len(matchparts) == 1 and
# matchparts[0].is_file())`, a narrow carve-out (pytest's own comment: "files
# given directly multiple times on the command line should not be
# deduplicated") that fires whenever the CURRENT hop's remaining match is a
# single bare file. `coordinator_core/test_baton_assemble.py` above is such a
# bare file, and it sits directly under Package(coordinator_core) — so
# collecting IT invalidates the cache entry for Package(coordinator_core)
# itself (the cache dict is unconditionally overwritten even when
# handle_dupes=False), silently minting a FRESH duplicate
# Package(coordinator_core/ops) -> Package(coordinator_core/ops/tests) chain.
# A later arg that redescends into that subpackage (here,
# test_handoff_archive_transition.py) attaches to the NEW duplicate Package
# instance, but `coordinator_core/ops/tests/conftest.py` was already parsed
# under the ORIGINAL (now-orphaned) Package instance, so its FixtureDefs carry
# `.node` pointing at the orphan. `_pytest.fixtures.FixtureManager
# ._matchfactories` (fixtures.py) matches primarily by NODE IDENTITY
# (`fixturedef.node in parent_nodes`) and — this is the actual gap — only
# falls back to matching by the `baseid` STRING (e.g. `'ops/tests'`) when
# `fixturedef.node is None`. When `.node` is set but simply belongs to an
# orphaned duplicate, neither branch matches and the fixture is dropped from
# that item's closure with no error at collection time, surfacing later as a
# "fixture not found" at test setup.
#
# THE FIX. Restore `baseid` string matching as an unconditional FALLBACK —
# never a replacement — for node-identity matching, exactly per pytest's own
# comment on the string branch ("legacy/plugins"). `baseid` is derived from
# the same node's nodeid at FixtureDef-construction time and does not go
# stale when a duplicate Package is minted, so it is strictly safe as a
# second check: every fixturedef `_matchfactories` used to yield still gets
# yielded (node-identity match still tried first); this only ADDS fixturedefs
# whose `.node` is a stale/orphaned duplicate of a node still on the current
# item's `baseid`-prefix chain.
#
# NEGATIVE SPEC
#   - Does NOT touch fixture SELECTION when multiple same-name fixturedefs
#     legitimately override each other (module overrides conftest, etc.) —
#     the override-resolution index math in `_get_active_fixturedef` is
#     untouched; this only affects which candidates make it into the
#     `fixturedefs` list `_matchfactories` filters, adding candidates that
#     `baseid` alone already says belong on this item's ancestor chain.
#   - Does NOT change pytest's collection/caching behavior itself — the
#     duplicate-Package minting still happens; this patches only the
#     downstream fixture-visibility symptom, because the alternative (forcing
#     pytest to never mint a duplicate Package) means monkeypatching
#     `Session.collect()`'s cache-invalidation logic, a far larger surface
#     with far more ways to silently change unrelated collection behavior.
#   - Applied defensively at BOTH levels, method and attribute.
#     `_matchfactories` is patched by NAME (`hasattr`), not assumed present.
#     `FixtureDef.node` is likewise read via `getattr(..., None)`, NOT
#     attribute access — it does not exist on every supported pytest. On
#     pytest 9.0.3 upstream `_matchfactories` matches on `baseid` ALONE and
#     `FixtureDef` carries no `.node` at all; the duplicate-Package gap this
#     block works around simply does not exist there. A bare `fixturedef.node`
#     therefore raised `AttributeError: 'FixtureDef' object has no attribute
#     'node'` inside the fixture closure of EVERY collected item — 4800
#     collection errors, the entire suite unrunnable, on a machine whose only
#     sin was a slightly older pytest (observed on a clean Windows install,
#     2026-07-28). With the `getattr`, 9.0.3 falls through to the `baseid`
#     branch and reproduces upstream 9.0.3 behavior exactly, while 9.1.x still
#     gets node-identity-first matching — one expression, correct on both, no
#     version sniffing.
#     Do NOT "simplify" this into a version check around the patch site: on
#     9.1.x `.node` is an INSTANCE attribute, so a class-level
#     `hasattr(FixtureDef, "node")` reads False there too and would silently
#     disable the fix on the very versions that need it.
#     Worst case in all cases reverts to the status quo (the bug this note
#     describes), never a new failure mode. This block itself pins no pytest
#     version; the repo declares a `>=9.1` floor in pyproject.toml's
#     [project.optional-dependencies].test as a verified-against statement —
#     below 9.1 the duplicate-Package gap does not exist, so this patch is a
#     no-op there and its regression pin proves nothing.
#
# Spec backlink: none (found and fixed in the same session; no antecedent
# plan). Regression pin: coordinator_core/tests/test_package_conftest_bare_
# file_arg_visibility.py, which reproduces the exact 3-arg repro above.
try:
    import _pytest.fixtures as _fx

    def _matchfactories_with_baseid_fallback(self, fixturedefs, node):
        parent_nodes = set(node.iter_parents())
        parentnodeids = {n.nodeid for n in parent_nodes}
        for fixturedef in fixturedefs:
            fixturedef_node = getattr(fixturedef, "node", None)
            if fixturedef_node is not None and fixturedef_node in parent_nodes:
                yield fixturedef
            elif fixturedef.baseid in parentnodeids:
                yield fixturedef

    if hasattr(_fx.FixtureManager, "_matchfactories"):
        _fx.FixtureManager._matchfactories = _matchfactories_with_baseid_fallback
except ImportError:  # pragma: no cover - defensive, see NEGATIVE SPEC above
    pass

# Captured at collection time, under the REAL (un-quarantined) HOME — before any
# per-test fixture below has a chance to monkeypatch HOME/USERPROFILE. A test
# process that spawns a subprocess later (many do, via `subprocess.run([sys.executable,
# ...], env=dict(os.environ))`) inherits whatever HOME the PARENT test process had at
# spawn time; that subprocess then computes ITS OWN user-site path fresh, based on the
# (quarantined) HOME it inherited — so a package installed only in the real user-site
# (e.g. jsonschema, pydantic on this machine) silently vanishes for the child, even
# though the parent test process still sees it fine. See the fixture docstring below
# for the concrete failure this fixes (`ModuleNotFoundError` in a HOME-quarantined
# subprocess) and why the fix belongs here rather than in the package under test.
_REAL_USER_SITE = site.getusersitepackages()


@pytest.fixture(autouse=True)
def _quarantine_real_home(request, tmp_path_factory, monkeypatch):
    """Redirect every home-resolution env var into a per-test throwaway dir.

    Autouse at the suite root, so it is instantiated before any package-local
    autouse fixture and before the test body — a test that subsequently sets
    ``HOME`` (or calls ``sandbox_home``) still wins, which is the intended
    layering.

    Opt out with ``@pytest.mark.real_home`` when a test is a parity oracle
    against the LIVE tree — e.g. the emit-parity suite resolves the real
    coordinator root through the machine-local registry, and quarantining its
    home turns a meaningful comparison into a ``RuntimeError: coordinator root
    not found``. Use the marker sparingly and only for read-only oracles: it
    hands the test the real home back, so anything that WRITES under it can
    corrupt live machine config — which is the bug this fixture exists to stop.
    """
    if request.node.get_closest_marker("real_home"):
        return None

    quarantine = tmp_path_factory.mktemp("home-quarantine")
    monkeypatch.setenv("HOME", str(quarantine))
    monkeypatch.setenv("USERPROFILE", str(quarantine))
    # HOMEDRIVE+HOMEPATH are expanduser's second Windows tier; leaving them set
    # would let the real profile back in whenever USERPROFILE is deleted.
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)

    # Preserve subprocess access to real user-site packages (2026-07-21 cluster-A fix).
    # Quarantining HOME/USERPROFILE also, as an unintended side effect, hides whatever
    # is installed ONLY in the real user-site directory from any subprocess a test
    # spawns — a dependency-resolution failure, not anything about the code under test
    # (see module-level `_REAL_USER_SITE` comment above for the exact mechanism). Fold
    # the real user-site path into PYTHONPATH so a spawned child's `sys.path` still
    # resolves it even though its HOME points at the throwaway quarantine dir. This is
    # read-only import-path plumbing — it does NOT restore real-HOME file access, so it
    # does not reopen the live-machine-config-corruption hole this fixture exists to
    # close (see module docstring above).
    if _REAL_USER_SITE:
        existing_pythonpath = os.environ.get("PYTHONPATH", "")
        pythonpath_entries = existing_pythonpath.split(os.pathsep) if existing_pythonpath else []
        if _REAL_USER_SITE not in pythonpath_entries:
            monkeypatch.setenv(
                "PYTHONPATH",
                os.pathsep.join([_REAL_USER_SITE, *pythonpath_entries]),
            )

    # Quarantining HOME also hides ~/.gitconfig from any test that shells out to
    # git — `git commit` then dies with "Please tell me who you are" and the
    # test sees a confusing downstream symptom ("you appear to have cloned an
    # empty repository"). Supply an identity via env so git-touching tests keep
    # working WITHOUT depending on the developer's global config, which is the
    # isolation win we actually want here.
    monkeypatch.setenv("GIT_AUTHOR_NAME", "coordinator-test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "coordinator-test@invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "coordinator-test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "coordinator-test@invalid")

    # Belt-and-braces companion to the HOME/USERPROFILE quarantine above: this
    # fixture redirects the FILESYSTEM a test writes into, but a 2026-07-28
    # incident showed at least one production call site mutates real MACHINE
    # STATE (Windows `HKCU\Environment` PATH, via `[Environment]::
    # SetEnvironmentVariable`) keyed on a caller-supplied path rather than on
    # HOME — a sandboxed home does not, by itself, stop that write. Disable
    # the whole class suite-wide rather than relying on every such call site
    # independently deriving the same temp-path heuristic correctly. See
    # `coordinator_core.install.substrate._refuse_machine_mutation`.
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")

    return quarantine


# ---------------------------------------------------------------------------
# probe-spray counter quarantine (2026-08-03 xdist cross-worker contamination)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _quarantine_probe_spray_state(tmp_path_factory, monkeypatch):
    """Give every test its own ``check_probe_spray`` counter directory.

    That guard keeps a rate-limit counter in ``tempfile.gettempdir()`` keyed by
    ``session_id`` or, absent one, the PARENT PID. Under pytest-xdist every
    worker shares the pytest process's parent, so the whole run accumulates
    into a single counter no matter what logical session id each test passes.
    Once three channel-test-shaped commands (``echo x``, a ``sed -n`` range
    read) land inside the 90s window from anywhere in the suite, the guard
    starts prepending a ``PROBE-SPRAY:`` ``additionalContext`` to the next
    Bash-shaped guard response — displacing the message an unrelated guard's
    test was asserting on. Observed as an intermittent failure of
    ``test_sed_range_read_advise_suppressed_by_machine_total_marker`` and
    ``test_crash_deny_is_scoped_to_the_crashed_guard_target_class`` together,
    roughly one run in eight at ``-n 6``, each passing in isolation.

    Ordering: defined ABOVE ``_fail_on_environ_leak`` so this ``setenv`` is
    part of that fixture's baseline snapshot rather than reported as a leak.

    Negative-spec: this quarantines only where the counter is STORED. It does
    not disable the nudge (``COORDINATOR_PROBE_NUDGE_OFF`` does that) — a test
    that wants to exercise probe-spray still can, and now gets a clean counter
    to do it against instead of whatever the rest of the suite left behind.

    Deliberately NOT ``tmp_path``: several tests assert on an exact traversal
    count over their own ``tmp_path`` (``test_tail_still_observes_whole_stream``
    counts ``os.walk`` yields), so materialising a directory inside it changes
    the number under test. ``tmp_path_factory`` puts the counter in a sibling
    directory the test never walks.
    """
    monkeypatch.setenv(
        "COORDINATOR_PROBE_SPRAY_STATE_DIR",
        str(tmp_path_factory.mktemp("probe-spray")),
    )


# ---------------------------------------------------------------------------
# os.environ leak guard (2026-07-21 interpreter-global-state sweep)
# ---------------------------------------------------------------------------
#
# Several production modules in this package are faithful ports of bash scripts where a
# bare `export` was correct because the process was about to exit. As an IMPORTED Python
# module the same write persists for the life of the interpreter — one shared interpreter
# across thousands of tests, plus inheritance into every `subprocess.run` child's env.
# Cluster fixed in 048d8acc; this fixture is the backstop that keeps it from recurring.
#
# The per-module cache-reset fixtures (test_coordinator_doe_root.py::_clean_env,
# test_deliverable_rollup.py::_reset_central_root_memo) own the deliberate
# interpreter-lifetime MEMOS we kept; queue_append's cache is path-keyed so it needs no
# reset. Those live beside their tests on purpose — this conftest guard is only the
# catch-all for env WRITES no reset seam can anticipate.

# Env vars the pytest harness itself owns and rewrites between phases. Excluded from the
# comparison rather than from the snapshot, so a test that sets one for real still cannot
# hide behind the exclusion.
#   PYTEST_CURRENT_TEST — pytest rewrites this on every setup/call/teardown transition,
#   so it differs between the pre-test and post-test snapshot of EVERY test.
_HARNESS_OWNED_ENV_KEYS = frozenset({"PYTEST_CURRENT_TEST"})


@pytest.fixture(autouse=True)
def _fail_on_environ_leak(request):
    """FAIL any test that leaves ``os.environ`` modified — do not silently restore.

    Why not just restore
    --------------------
    Restoring would make the suite green and hide the defect, which is precisely how the
    original leak survived: production resolvers were exporting env vars as a side effect
    of being CALLED, and every downstream failure pointed at an innocent victim test (a
    ``CLAUDE_KLABAUTER_ROOT`` resolution error three files away) rather than the resolver that
    dirtied the environment. The signal IS the deliverable here. This fixture names the
    offending test in its own failure, so the report points at the cause not the casualty.

    Why the existing autouse ``monkeypatch`` is not enough
    ------------------------------------------------------
    ``monkeypatch`` (used by ``_quarantine_real_home`` above, and by hundreds of tests)
    reverts only what IT set — ``monkeypatch.setenv``/``delenv`` record an undo entry at
    call time. A write performed by the code under test via a plain
    ``os.environ[...] = ...`` was never recorded by monkeypatch and therefore survives
    teardown untouched. That is exactly the gap the 2026-07-21 cluster escaped through: the
    tests were disciplined; the production code was not.

    Ordering: this fixture snapshots AFTER ``_quarantine_real_home`` has applied its
    monkeypatched vars (autouse fixtures resolve in definition order within a module, and
    monkeypatch's own teardown runs before this one's post-yield), so the quarantine's own
    writes are part of the baseline rather than reported as a leak.

    Opt out with ``@pytest.mark.allow_environ_leak`` — reserved for tests that deliberately
    assert on process-wide env mutation. Adding a marker is a doctrine decision, not a way
    to silence a red test: if production code dirtied the environment, fix the production
    code.
    """
    if request.node.get_closest_marker("allow_environ_leak"):
        yield
        return

    before = {k: v for k, v in os.environ.items() if k not in _HARNESS_OWNED_ENV_KEYS}
    yield
    after = {k: v for k, v in os.environ.items() if k not in _HARNESS_OWNED_ENV_KEYS}

    if before == after:
        return

    added = sorted(k for k in after if k not in before)
    removed = sorted(k for k in before if k not in after)
    changed = sorted(k for k in before if k in after and before[k] != after[k])

    details = []
    if added:
        details.append("  set:     " + ", ".join(f"{k}={after[k]!r}" for k in added))
    if removed:
        details.append("  deleted: " + ", ".join(f"{k} (was {before[k]!r})" for k in removed))
    if changed:
        details.append(
            "  changed: " + ", ".join(f"{k}: {before[k]!r} -> {after[k]!r}" for k in changed)
        )

    pytest.fail(
        f"os.environ leaked out of {request.node.nodeid}.\n"
        + "\n".join(details)
        + "\n\nThis test (or the production code it exercised) mutated the process "
        "environment without restoring it. An interpreter-global env write persists for "
        "the rest of the session and is inherited by every subprocess a later test spawns, "
        "so the damage surfaces as an unrelated test failing.\n"
        "Fix the WRITE, not this fixture: scope it with "
        "`coordinator_core.install._shared.env_overlay` (or a local contextmanager) if "
        "production code needs the value in-process, or use `monkeypatch.setenv` if it is "
        "the test's own setup.",
        pytrace=False,
    )
