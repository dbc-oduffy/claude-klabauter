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
import shutil
import site
import tempfile
from pathlib import Path

import pytest

#: The real `os` callables, bound at import BEFORE any test can monkeypatch
#: them. `_quarantine_real_home`'s teardown needs them: it runs while a test's
#: own patches are still installed, and several warm tests replace `os.unlink`
#: with a narrower fake to prove the client never unlinks a refusing socket.
_REAL_UNLINK = os.unlink
_REAL_RMDIR = os.rmdir
_REAL_WALK = os.walk
_REAL_JOIN = os.path.join

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


def _capture_real_doe_root() -> str:
    """Resolve the sibling DoE-claude checkout ONCE, at collection time, under
    the real (un-quarantined) HOME — used ONLY to locate the manifest to copy
    into a throwaway stub (see ``_STUB_DOE_ROOT`` below). The real path itself
    is never seeded into a quarantined test's ``.doe-root`` pointer.

    Same capture-before-quarantine shape as ``_REAL_USER_SITE`` above, for the
    same class of reason. ``coordinator/bin/lib/coordinator_registry.py``
    resolves its manifest (``coordinator/schemas/coordinator-registry.manifest
    .json``, which DR-047 keeps in DoE-claude while this repo owns the engine)
    at IMPORT time, through a ladder whose every live rung is home-anchored:
    the ``.doe-root`` pointer files, the marketplace-cache probe, the flat
    plugin-layout probe, and the machine-local registry CLI all hang off
    ``$HOME``/settings-home. Quarantining HOME therefore does not isolate that
    module — it makes it unresolvable, and it fails loud with an
    install-integrity ``FileNotFoundError`` at import. Every test that loads a
    ``coordinator/bin/`` CLI (in-process via ``SourceFileLoader``, as
    ``baton_assemble.apply._load_doc_new_module`` does, or as a spawned
    subprocess inheriting this environment) dies before reaching its assertion.

    Returns "" when nothing resolves — on a machine with no DoE-claude checkout
    the seeding below is skipped and behavior is unchanged.
    """
    try:
        from coordinator_core.testing.doe_root import resolve_doe_root

        root = resolve_doe_root()
    except Exception:  # pragma: no cover - defensive; a broken resolver must not break collection
        return ""
    return root if root and os.path.isdir(root) else ""


_REAL_DOE_ROOT = _capture_real_doe_root()

_REAL_DOE_MANIFEST_RELPATH = os.path.join(
    "coordinator", "schemas", "coordinator-registry.manifest.json"
)

_REAL_DOE_CROSS_REPO_MEMO_SCHEMA_RELPATH = os.path.join(
    "coordinator", "schemas", "cross-repo-memo.schema.json"
)

# The explicit, named set of relpaths seeded into the stub DoE-claude root.
# Membership rule: a file belongs here ONLY if some quarantined test must
# READ it from the DoE side, and it was added deliberately for that reason —
# never a whole-tree or whole-directory copy. Keeping this list narrow is
# load-bearing: the stub's "strictly less exposure than the real repo"
# isolation guarantee (see docstring below) depends on copying the minimum
# set of files quarantined tests actually need, not everything that happens
# to be nearby.
_STUB_DOE_SEED_RELPATHS = (
    _REAL_DOE_MANIFEST_RELPATH,
    _REAL_DOE_CROSS_REPO_MEMO_SCHEMA_RELPATH,
)


def _build_stub_doe_root(base_dir: str) -> str:
    """Build a throwaway DoE-claude STUB under ``base_dir`` and return its path.

    Copies only the explicitly named files quarantined tests actually need
    to READ — listed in ``_STUB_DOE_SEED_RELPATHS`` above — out of the real
    checkout captured by ``_capture_real_doe_root``. Nothing else from the
    real repo is copied or referenced. A file earns a place in that tuple
    only when a quarantined test genuinely reads it from the DoE side, added
    deliberately one at a time — this must never become a whole-tree copy.

    This is the fix for a P1: seeding the REAL DoE-claude path into a
    quarantined test's ``.doe-root`` pointer made the manifest READ succeed,
    but ``coordinator_registry.py::doe_root()`` is also the documented anchor
    other call sites join WRITE targets onto (``state/lessons-outbox``,
    ``state/improvement-queue``) — so any quarantined test that reached a
    ``doe_root()``-anchored write path without its own override could write
    into the LIVE sibling repo, the exact corruption class this fixture
    exists to prevent. Pointing at a stub instead keeps the needed reads
    working while making every write land inside the throwaway quarantine
    dir — strictly more isolation than tests had before the real path was
    ever seeded (``16e1c220c``), and strictly less exposure than the real
    repo. That "strictly less exposure" rationale is why the seed set stays
    an explicit named tuple rather than a directory copy: every additional
    file widens the exposure the stub was built to shrink.

    Returns "" if the real DoE root or the REGISTRY MANIFEST cannot be
    located, mirroring ``_capture_real_doe_root``'s graceful degradation —
    callers must treat an empty return the same as "nothing to seed". The
    manifest is deliberately load-bearing rather than one seed among equals:
    a stub carrying schemas but no registry resolves ``doe_root()`` into a
    tree that fails its read later and further away, which is worse than
    degrading here. Any OTHER missing seed file is skipped, not fatal — it
    surfaces as its own reader's ENOENT, naming the file it wanted.
    """
    if not _REAL_DOE_ROOT:
        return ""
    if not os.path.isfile(os.path.join(_REAL_DOE_ROOT, _REAL_DOE_MANIFEST_RELPATH)):
        return ""

    import shutil

    stub_root = os.path.join(base_dir, "doe-claude-stub")
    for relpath in _STUB_DOE_SEED_RELPATHS:
        real_path = os.path.join(_REAL_DOE_ROOT, relpath)
        if not os.path.isfile(real_path):
            continue
        stub_path = os.path.join(stub_root, relpath)
        os.makedirs(os.path.dirname(stub_path), exist_ok=True)
        shutil.copyfile(real_path, stub_path)

    # `coordinator/bin` itself carries no seeded file (bin/lib scripts are
    # claude-klabauter-side per `_doe_bin_dir()`'s own docstring), but a real DoE-claude
    # checkout has it on disk, and downstream readers (e.g. `_doe_bin_dir()`'s
    # callers) join a sibling path back out of it via
    # `coordinator/bin/../schemas/...`. `..` traversal is resolved by the OS
    # against the actual directory tree, so `bin` must exist as a real (if
    # empty) directory here or that join fails with ENOENT before the schema
    # file it correctly names is ever reached.
    os.makedirs(os.path.join(stub_root, "coordinator", "bin"), exist_ok=True)

    return stub_root


@pytest.fixture(autouse=True)
def _quarantine_real_home(request, tmp_path_factory, monkeypatch):
    """Redirect every home-resolution env var into a per-test throwaway dir.

    Autouse at the suite root, so it is instantiated before any package-local
    autouse fixture and before the test body. Fixture ORDERING alone does not
    guarantee a later override wins, though: on Windows, ``expanduser``/
    ``Path.home()`` consult ``USERPROFILE`` (then ``HOMEDRIVE``+``HOMEPATH``)
    before ``HOME``, so a test that subsequently sets only ``HOME`` does NOT
    win there — it silently keeps resolving into this fixture's quarantine
    dir, because ``USERPROFILE`` is still set. Use
    ``coordinator_core.testing.home_sandbox.sandbox_home``, which sets every
    variable the running platform consults (including ``USERPROFILE`` and
    clearing ``HOMEDRIVE``/``HOMEPATH``), whenever a test needs to *name* and
    assert against its own sandboxed home rather than merely inherit this
    fixture's throwaway one.

    Opt out with ``@pytest.mark.real_home`` when a test is a parity oracle
    against the LIVE tree — e.g. the emit-parity suite resolves the real
    coordinator root through the machine-local registry, and quarantining its
    home turns a meaningful comparison into a ``RuntimeError: coordinator root
    not found``. Use the marker sparingly and only for read-only oracles: it
    hands the test the real home back, so anything that WRITES under it can
    corrupt live machine config — which is the bug this fixture exists to stop.
    """
    # The machine-mutation kill switch is set for real_home tests TOO, above the
    # opt-out, and is deliberately NOT part of what `real_home` hands back. The
    # marker means "resolve the real home", which its docstring above scopes to
    # read-only oracles; it never meant "and you may also mutate real machine
    # state". Leaving the switch below the early return granted exactly that, so
    # a marked test had the real home AND no kill switch at once -- the pairing
    # behind the live `.doe-root` pollution (state/bug-backlog/2026-08-26-a-test-
    # writes-the-live-claude-machine-lo-6cdf6bc87771.yaml). A test that genuinely
    # must mutate real machine state now asks for it by name with
    # `@pytest.mark.real_machine_mutation`, at its own call site.
    if not request.node.get_closest_marker("real_machine_mutation"):
        monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")

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

    # Second consequence of hiding ~/.gitconfig, same shape as the identity one
    # above but with a GUI blast radius. Git for Windows ships no `man.exe`, so
    # `git <verb> --help` resolves `help.format` to `web` and hands off to
    # `git-web--browse`, which LAUNCHES THE OPERATOR'S DEFAULT BROWSER at a local
    # `git-<verb>.html`. The operator's global mitigation for that (the
    # help.format/web.browser/browser.noop.cmd triple) lives in ~/.gitconfig --
    # which this fixture has just made invisible. Measured on 2026-08-07: with
    # the quarantine applied, `git config --get web.browser` answers empty/rc=1
    # on a box where the global triple is set, so the suite runs UNPROTECTED and
    # any test shelling out to `git <verb> --help` sprays browser tabs (observed:
    # dozens a minute from the bash-guard alternative-liveness gate).
    # `GIT_CONFIG_*` is the injection form that survives a quarantined HOME.
    # `browser.noop.cmd` is `eval`-ed by `git-web--browse`, so its value must
    # stay free of shell metacharacters -- a parenthesis is a hard syntax error.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "3")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "help.format")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "web")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "web.browser")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "noop")
    monkeypatch.setenv("GIT_CONFIG_KEY_2", "browser.noop.cmd")
    monkeypatch.setenv("GIT_CONFIG_VALUE_2", "echo not-opening-browser-for:")

    # Belt-and-braces companion to the HOME/USERPROFILE quarantine above: this
    # fixture redirects the FILESYSTEM a test writes into, but a 2026-07-28
    # incident showed at least one production call site mutates real MACHINE
    # STATE (Windows `HKCU\Environment` PATH, via `[Environment]::
    # SetEnvironmentVariable`) keyed on a caller-supplied path rather than on
    # HOME — a sandboxed home does not, by itself, stop that write. Disable
    # the whole class suite-wide rather than relying on every such call site
    # independently deriving the same temp-path heuristic correctly. See
    # `coordinator_core.install.substrate._refuse_machine_mutation`.
    # SET ABOVE the `real_home` opt-out, not here -- see the comment at the top
    # of this fixture for why the two must not be granted together.

    # Same shape, second surface: the warm engine's runtime base is
    # `%LOCALAPPDATA%`, which the HOME/USERPROFILE quarantine above does not
    # touch. Warm tests pass a `tmp_path` as `engine_root` believing that
    # isolates them; it varies only `svc_dir`'s clone-hash component, so each
    # run minted a fresh REAL directory under
    # `%LOCALAPPDATA%/coordinator/warm/` and populated it with breadcrumb and
    # telemetry fixtures nothing ever removed (measured 2026-08-20 on the
    # authoring box: 1027 clone-key directories, 244 holding synthetic fixture
    # content). Redirect the base itself -- per-test, so two tests still get
    # distinct trees, and suite-wide rather than in a warm-local conftest so a
    # future writer anywhere under `coordinator_core/` inherits the isolation
    # instead of rediscovering the defect.
    #
    # SHORT ON POSIX, AND THAT IS NOT A TIDINESS PREFERENCE -- it is the whole
    # `sun_path` budget. A unix socket address is capped at 104 bytes on macOS
    # (`election.SUN_PATH_MAX_BYTES` holds the line at 100), and the quarantine
    # above is ~90 bytes deep before `warm-runtime-base/coordinator/warm/
    # <16-hex-clone-hash>/<token>.sock` is appended: measured 160-175 bytes.
    # `election.socket_path` then raises `SocketPathTooLongError` -- correctly,
    # by its own docstring, "rather than left to bind(), which reports it as an
    # unexplained OSError" -- during the warm client's PREAMBLE, before any test
    # body's monkeypatched seam is reached. 31 tests across six files failed
    # that way on every POSIX box, each reporting some downstream puzzle (a
    # `None` result, an IndexError on an empty list) rather than the cause.
    #
    # Fixed HERE rather than in each file, because here is where the length
    # comes from. Six files had grown their own copy of the same short-base
    # fixture before this landed; they are deleted with it. A future warm test
    # written anywhere under `coordinator_core/` inherits a usable base instead
    # of rediscovering the defect -- the same argument this fixture's own
    # comment above already makes for putting the isolation suite-wide.
    #
    # Windows keeps the quarantine path unchanged: named pipes have no
    # `sun_path` equivalent, `%TEMP%` is not `/tmp`, and there is no defect to
    # fix there.
    from coordinator_core.warm import breadcrumb as _warm_breadcrumb

    if os.name == "nt":
        _warm_base = quarantine / "warm-runtime-base"
    else:
        # Per-test, so two tests still get distinct trees (this fixture's own
        # requirement), and removed on teardown so the short root does not
        # accumulate what the quarantine used to absorb.
        _warm_base = Path(tempfile.mkdtemp(prefix="cwrb-", dir="/tmp"))

        def _drop_warm_base() -> None:
            # `ignore_errors=True` covers OSError and nothing else, and this
            # teardown runs while a test's own monkeypatches are still live.
            # `shutil.rmtree` calls `os.unlink(name, dir_fd=...)`; a test that
            # replaced `os.unlink` with a narrower fake (several here do, to
            # prove the client never unlinks a refusing socket) raises
            # TypeError straight through the `ignore_errors` guard and turns
            # a passing test into a teardown ERROR. Removing a throwaway temp
            # directory must never be the thing that fails a test, so this
            # swallows everything and leaves at most an empty dir in /tmp.
            try:
                shutil.rmtree(_warm_base, ignore_errors=True)
            except Exception:  # noqa: BLE001 -- see above
                # Swallowing alone LEAKS: measured 25 stray roots after a few
                # suite runs, and on a box carrying dozens of concurrent
                # sessions that accumulates. Retry with the real `os`
                # callables captured at import, before any test could patch
                # them, which is the only reason this second attempt can
                # succeed where the first failed.
                for parent, dirnames, filenames in _REAL_WALK(_warm_base, topdown=False):
                    for name in filenames:
                        try:
                            _REAL_UNLINK(_REAL_JOIN(parent, name))
                        except OSError:
                            pass
                    for name in dirnames:
                        try:
                            _REAL_RMDIR(_REAL_JOIN(parent, name))
                        except OSError:
                            pass
                try:
                    _REAL_RMDIR(_warm_base)
                except OSError:
                    pass

        request.addfinalizer(_drop_warm_base)

    monkeypatch.setenv(_warm_breadcrumb.RUNTIME_BASE_ENV, str(_warm_base))

    # Make the throwaway home a FAITHFUL home rather than an empty one for the
    # one read the quarantine would otherwise break outright: the `.doe-root`
    # pointer that `coordinator_registry`'s import-time manifest bootstrap
    # resolves through (see `_capture_real_doe_root` above for the mechanism).
    # Seeded as a FILE inside the quarantine, not as a `REPO_DOE_CLAUDE` env
    # override: the env route outranks the machine-local registry in the
    # ratified DR-071 precedence and would silently mask the rung a test is
    # exercising, whereas the pointer file sits at the same rung a real install
    # populates and leaves that precedence intact. Read-only plumbing — it
    # restores no write access to the real home, so it does not reopen the
    # live-machine-config-corruption hole this fixture exists to close.
    #
    # The pointer value itself is a THROWAWAY STUB (`_build_stub_doe_root`),
    # never `_REAL_DOE_ROOT`. Seeding the real path here made the manifest
    # read succeed but ALSO made `doe_root()` — the documented join-anchor
    # other call sites use for WRITE targets (`state/lessons-outbox`,
    # `state/improvement-queue`) — resolve to the live sibling checkout, so a
    # quarantined test reaching a `doe_root()`-anchored write path without its
    # own override could corrupt the real repo: exactly the class of bug this
    # fixture exists to prevent. The stub carries only a copy of the one file
    # a manifest read needs, so reads still succeed and every write instead
    # lands inside this test's own throwaway quarantine directory.
    #
    # Both locations are written because a real install carries both and the
    # reader (`coordinator/lib/read_doe_root_pointer.py`) tries them in this
    # order: `${settings-home}/machine-local/.doe-root` (durable, DR-072) then
    # `${CLAUDE_HOME:-$HOME}/.claude/.doe-root` (legacy fallback). Seeding only
    # the first would leave any test that redirects COORDINATOR_SETTINGS_HOME
    # on its own back at an unresolvable pointer.
    stub_doe_root = _build_stub_doe_root(str(quarantine))
    if stub_doe_root:
        for pointer in (
            quarantine / ".coordinator-claude-settings" / "machine-local" / ".doe-root",
            quarantine / ".claude" / ".doe-root",
        ):
            pointer.parent.mkdir(parents=True, exist_ok=True)
            pointer.write_text(stub_doe_root + "\n", encoding="utf-8")

    # `_capture_real_doe_root`'s collection-time call resolved through
    # `ops.coordinator_doe_root`, whose module-scope memo would otherwise hold
    # the REAL checkout path for the rest of the process — a second resolver
    # still handing out the live sibling repo under quarantine, which is the
    # same corruption class the stub above exists to close. Dropping the memo
    # per test forces every in-test resolution back through the quarantined
    # environment.
    try:
        from coordinator_core.ops.coordinator_doe_root import _reset_doe_root_cache

        _reset_doe_root_cache()
    except ImportError:  # pragma: no cover - defensive, mirrors the capture helper
        pass

    return quarantine


# ---------------------------------------------------------------------------
# foreign-repo probe memo quarantine
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_foreign_repo_probe_memo():
    """Give every test an empty ``git_scope`` foreign-repo probe memo.

    ``foreign_repo_unusable_reason`` caches "is this path a usable git repo"
    per ``(realpath, pid)``, which is right for a process that only READS
    sibling clones and wrong for a suite that builds, mutates and deletes
    fixture repos in one interpreter. Clearing on both sides of the test makes
    the memo unobservable across test boundaries: no test inherits a verdict
    about a path a peer created, and none leaves one behind.

    Same shape and same reason as the ``_reset_doe_root_cache()`` call in
    ``_quarantine_real_home`` above — an interpreter-lifetime memo that is
    correct in production and a cross-test channel in a suite. Unconditional
    (no ``real_home`` opt-out): the memo is process state, not home state.
    """
    from coordinator_core.git_scope import reset_foreign_repo_probe_memo

    reset_foreign_repo_probe_memo()
    yield
    reset_foreign_repo_probe_memo()


@pytest.fixture(autouse=True)
def _reset_engine_root_process_state():
    """Give every test a cold ``engine_root`` module state.

    ``coordinator_engine_root`` memoizes its Rung 1.5/Rung 2 answer process-scope
    on ``_registry_mtime_pair``, and the sibling gate/shim/advisory caches in the
    same module are interpreter-lifetime too. That is right for a hook process
    that resolves once and exits, and wrong for a suite where nearly every test
    points ``COORDINATOR_SETTINGS_HOME`` at its own registry-less ``tmp_path`` --
    those all collapse to the SAME memo key, so the first test to resolve a root
    answers for every later test in the worker.

    Measured 2026-08-25 rather than assumed: ``tests/test_two_axis_env_export.py``
    and ``ops/test_render_template_tree.py`` both pass alone and both fail in the
    tier, and ``test_engine_root.py``'s own fall-through case was served a stale
    pointer value by an earlier test in its file. Order-dependence, not a resolver
    defect -- the module already ships every one of these as a named reset seam.

    Same shape and same reason as ``_reset_foreign_repo_probe_memo`` above, and
    unconditional for the same reason: this is process state, not home state.
    """
    from coordinator_core import engine_root

    def _cold() -> None:
        for seam in (
            "_reset_root_memo",
            "_reset_gate_memo",
            "_reset_shim_cache",
            "_reset_skew_advisory",
            "_reset_engine_root_env_advisories",
            "_reset_locator_axis_advisories",
        ):
            # Resolved by name, not imported: a seam renamed or retired upstream
            # should not turn every test in the suite into a collection error.
            fn = getattr(engine_root, seam, None)
            if fn is not None:
                fn()

    _cold()
    yield
    _cold()


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


# ---------------------------------------------------------------------------
# Dispatch-axis stamp gate opt-in (state/handoffs/2026-08-21_103635_reaching-
# the-warm-engine.md) — this suite imports and dispatches against the LIVE,
# unstamped tree by design (that is the entire point of a test suite), so it
# is one of the two sanctioned callers of `ipc.allow_unstamped_dispatch` named
# in that function's own docstring. NOT an environment variable — see
# `_fail_on_environ_leak` above for why this suite treats an env-var-shaped
# opt-in as a defect class in its own right: it would be inherited by every
# subprocess a test spawns, silently disarming the gate in processes nobody
# intended. `pytest_configure` runs exactly once per session, before any test
# collects, so this is a single, visible, in-process declaration — not
# something a test can accidentally trigger or a later test can accidentally
# inherit from a DIFFERENT source. A test that wants to assert the REFUSAL
# itself flips `coordinator_core.ipc._unstamped_dispatch_allowed` off via
# `monkeypatch.setattr`, which reverts automatically at that test's own
# teardown — see `ipc.allow_unstamped_dispatch`'s own docstring.
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    from coordinator_core.ipc import allow_unstamped_dispatch

    allow_unstamped_dispatch()


@pytest.fixture
def exercise_suspended_op(monkeypatch):
    """Let a test drive an op that `op_budget_suspension` has turned off.

    The suspension exists to stop an over-budget op occupying the shared box at
    RUNTIME. A test is not the box: exercising a suspended op's own contract costs
    nothing anyone is queued behind, and that coverage is exactly what a
    reinstatement case has to stand on. Deleting these tests because the op is off
    would mean an op comes back with its envelope contract unproven — the failure
    mode the suspension is trying to prevent, arriving by a different door.

    Use ONLY for a test asserting what the op itself does. A test asserting that a
    suspended op is REFUSED must not take this fixture — see
    `tests/test_op_suspension_ratchet.py`, which deliberately does not.

    `monkeypatch.setattr` reverts at teardown, so the suspension is live again for
    every other test in the session. There is no process-wide clear and no env knob;
    this is a per-test fixture by construction, so it cannot leak into runtime.
    """
    from coordinator_core import op_budget_suspension

    monkeypatch.setattr(op_budget_suspension, "SUSPENDED_OPS", {})
    return None


# ---------------------------------------------------------------------------
# Live session-hub litter guard
# ---------------------------------------------------------------------------
#
# Closes the class of defect commit c08e942e9 named ("two test-isolation
# defects that wrote outside the sandbox, and the seams that let them"), on the
# surface it did not cover: the real repo's own
# `.git/coordinator-sessions/`. Measured 2026-08-26 — three directories in the
# LIVE hub carrying test-fixture names, minted by tests that resolved a repo
# root from the process cwd (the live repo) while taking their session id from
# a monkeypatched env var: `sess-1` and `sess-abc` (born 08-13, holding one
# `repo-identity-gate.log` each) and `altlive-probe` (born 08-17, holding one
# `overrides.log`). Both writers have since been taught not to MINT a session
# dir — `write_guards/guard_doctrine_surface_edits.py` returns unless the dir
# already exists, and `bash_guards/_override_log_path.py` routes to the
# `no-session` bucket instead — so those three kept receiving appends only
# because they already existed. This fixture closes the SYMPTOM for any future
# cause, which the two named fixes cannot: a guard that has to be remembered
# for each new writer is the guard that gets forgotten.
#
# Cost, measured on this box 2026-08-26 against the live 374-entry hub
# (`time.process_time`, k=1000): 0.26ms per `os.listdir`, so 0.52ms per test
# for the before/after pair — a non-recursive listing, no walk and no spawn.
# Same shape and same justification as
# `coordinator_core/install/conftest.py`'s repo-root litter guard, whose
# function-scoped-over-session-scoped reasoning applies here verbatim (a
# session-scoped snapshot would attribute litter to "somewhere in this run"
# and force a re-run under `-k` to localize it).
#
# NOT flaky under concurrent peers, which is the one thing this guard has to
# get right on a box running 50-70 sessions against this same tree: a peer
# session legitimately creates a directory here at any moment, so a new entry
# alone is never the assertion. A new entry is flagged only when it is BOTH
# absent from the harness session registry AND not UUID-shaped — a live peer's
# id is always a harness UUID, and every fixture name observed in the wild
# (`sess-1`, `sess-abc`, `altlive-probe`, `test-session-abc123`, `sess-msys-*`)
# is neither.
#
# Negative-spec:
#   - Does NOT detect an APPEND into a directory that already existed. That
#     needs a per-file stat of ~380 directories per test, which this repo's
#     brightline will not pay for; the two producer fixes above are what close
#     that half, and a dir that is never minted is never appended into.
#   - Does NOT clean up what it flags. A test that leaks into the live hub is
#     broken and must fail loudly, not have its symptom swept.
#   - Considers DIRECTORIES ONLY. The hub also carries plain files that no
#     session owns -- `archive-terminal-handoffs.lock` was observed failing
#     this guard mid-run on 2026-08-26, attributed to whichever test happened
#     to straddle a peer's lock acquisition. A lock file is not a session
#     directory and can never be the leak this guard names.
#   - DOES now attribute a new DIRECTORY by recorded owner, closing the
#     wrong-owner half this spec previously deferred (`altlive-isolation-
#     fixture-session`, and `c7-cold-fwd-probe` observed 2026-08-26 21:40:54Z
#     mid-run and blamed on whichever test straddled it). The deferral said
#     "rather than solved with an mtime/pid heuristic", and that still holds:
#     what is read below is not a heuristic but the owning session's own
#     `meta.json` stamp. See `_dir_is_ours`.
#   - Still does NOT attribute a dir carrying NO `meta.json`. That is the
#     fail-closed arm and it is the historically-correct one: all three
#     original leaks (`sess-1`, `sess-abc`, `altlive-probe`) held a single log
#     file and no `meta.json`, because a fixture leak is minted by a log-
#     appending guard rather than by session init. No stamp means no owner
#     means flag it.
#   - Lives HERE rather than in the repo-root `conftest.py` because
#     `coordinator_core/pytest.ini` wins as configfile for any invocation
#     whose path argument sits under `coordinator_core/`, which makes that
#     the rootdir and the root conftest unreachable — and this package is
#     where the leaking tests are. The root conftest re-exports the fixture
#     by name so the `coordinator/` testpaths are covered too.

import json
import uuid as _uuid

import pytest as _pytest

_LIVE_HUB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".git",
    "coordinator-sessions",
)


def _looks_like_a_harness_session_id(name: str) -> bool:
    """`True` for a canonical UUID — the shape every real harness session id
    has, and no test fixture name observed in the live hub has ever had."""
    try:
        return str(_uuid.UUID(name)) == name.lower()
    except (ValueError, AttributeError, TypeError):
        return False


def _live_hub_session_dirs() -> set:
    """Directory names directly under the live hub. `scandir` rather than
    `listdir` because the hub carries peer-owned lock FILES that are not
    session directories and must never be read as leaked ones; `is_dir()`
    comes off the dirent here, so this stays one directory walk."""
    with os.scandir(_LIVE_HUB) as entries:
        return {e.name for e in entries if e.is_dir()}


def _dir_is_ours(name: str) -> bool:
    """`True` when this process's own harness session minted `name`, `False`
    when a DIFFERENT session did, and `True` (fail-closed — flag it) whenever
    ownership cannot be established.

    The discriminator is `meta.json`'s `stable_pid`: `session/core.py` stamps it
    with the owning `claude.exe` PID taken from `CLAUDE_PID`, which every child
    this session spawns inherits. So a directory minted by a test in THIS
    session — in-process, or by any CLI it shells out to — carries our
    `CLAUDE_PID`, and one minted by a concurrent peer carries theirs. That is a
    recorded ownership stamp, not the mtime/pid heuristic this guard's spec
    declined; nothing here guesses from timing.

    Every unprovable case returns `True` so the guard keeps its teeth: no
    `meta.json` at all (the shape all three original leaks had), an unreadable
    or stampless one, or a `CLAUDE_PID` absent from our own environment. The
    exemption is narrow by construction — it fires only on a positive,
    disagreeing stamp."""
    ours = os.environ.get("CLAUDE_PID")
    if not ours:
        return True
    meta = os.path.join(_LIVE_HUB, name, "meta.json")
    try:
        with open(meta, "r", encoding="utf-8") as fh:
            stamped = json.load(fh).get("stable_pid")
    except (OSError, ValueError, AttributeError):
        return True
    if not stamped:
        return True
    return str(stamped) == str(ours)


@_pytest.fixture(autouse=True)
def _no_new_live_session_hub_entries():
    try:
        before = _live_hub_session_dirs()
    except OSError:
        yield
        return
    yield
    try:
        after = _live_hub_session_dirs()
    except OSError:
        return
    new_entries = after - before
    if not new_entries:
        return
    # Only now — on the rare positive — pay for the registry read.
    try:
        from coordinator_core.session import harness_registry

        live = set(harness_registry.snapshot())
    except Exception:
        live = set()
    leaked = sorted(
        name
        for name in new_entries
        if name not in live
        and not _looks_like_a_harness_session_id(name)
        and _dir_is_ours(name)
    )
    assert not leaked, (
        "test created entries in the REAL repo's session hub "
        f"{_LIVE_HUB}: {leaked!r} — a guard or CLI under test resolved its "
        "repo root from the process cwd (the live repo) while taking its "
        "session id from a fixture. Point the code under test at a tmp_path "
        "repo, or pass the root explicitly. See this file's Live session-hub "
        "litter guard note."
    )
