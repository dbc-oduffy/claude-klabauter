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
``~/.claude`` content-root pointer, repointing every coordinator skill on the machine at a
directory that vanishes on the next tmp reap. The three cross-repo inbox memos of
2026-07-20 (pointer-test-clobbers-real-home, pointer-test-corrupts-live-machine-config,
windows-test-home-leak) record it.

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

import sys

import pytest

# BV-20260927-03: under a `rootdir = coordinator_core` invocation (this
# file's own ``pytest.ini`` wins as configfile, confcutdir excludes the
# repo-root conftest — see this module's docstring), nothing upstream of
# THIS import has put the checkout root on sys.path yet. An unrelated,
# box-wide editable install of the PUBLISHED engine elsewhere on sys.path
# (e.g. a `.pth` pointing at `/root/klabauter`) can therefore win the
# `coordinator_core` package resolution below, and pytest's own conftest
# loader then raises ImportPathMismatchError. Force this checkout to win,
# regardless of what COORDINATOR_ENGINE_ROOT says for other (non-test)
# callers — that resolution is a separate, load-bearing concern untouched
# here.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT in sys.path:
    sys.path.remove(_REPO_ROOT)
sys.path.insert(0, _REPO_ROOT)
for _mod_name in [
    n for n in sys.modules if n == "coordinator_core" or n.startswith("coordinator_core.")
]:
    _mod_file = getattr(sys.modules[_mod_name], "__file__", None) or ""
    if _mod_file and not os.path.abspath(_mod_file).startswith(_REPO_ROOT):
        del sys.modules[_mod_name]

from coordinator_core import memo_corpus
from coordinator_core.benchmarks.isolated_clone import CloneTeardownLeak, rmtree_or_raise

# Box-scoped, not directory-scoped: admission thresholds come from machine-local, so a
# configured, loaded box would make every CLI-driving test hold up to `max_hold_s`
# (docs/plans/2026-09-27-load-aware-workflow-admission.md, C4). Set at IMPORT time so a
# `coordinator_core/pytest.ini`-rooted invocation (where the repo-root conftest never
# loads) is covered too, and so a spawned subprocess inherits it.
os.environ.setdefault("COORDINATOR_WORKFLOW_ADMISSION_DISABLE", "1")

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

    def _parent_index(node):
        # A node's ancestor chain is fixed once built, and this runs ~1.4M
        # times per full collection (once per fixture lookup), so the sets are
        # cached on the node itself. Keyed by object, never nodeid: duplicate
        # Packages share a nodeid, which is the very gap this patch handles.
        cached = node.__dict__.get("_mk_parent_index")
        if cached is None:
            parent_nodes = set(node.iter_parents())
            cached = (parent_nodes, {n.nodeid for n in parent_nodes})
            node.__dict__["_mk_parent_index"] = cached
        return cached

    def _matchfactories_with_baseid_fallback(self, fixturedefs, node):
        parent_nodes, parentnodeids = _parent_index(node)
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


def _capture_real_settings_home() -> str:
    """This operator's REAL `<home>/.coordinator-claude-settings`, resolved
    from the actual account home -- `pwd.getpwuid(os.getuid()).pw_dir` on
    POSIX, `USERPROFILE` captured here (at conftest IMPORT time, before any
    test's `monkeypatch.setenv` can touch it) on Windows. Deliberately NOT
    `_settings_home.settings_home()` itself: that reads `HOME`/`USERPROFILE`,
    which per-test quarantine below has already redirected by the time most
    callers run, and a `COORDINATOR_SETTINGS_HOME` override in the AMBIENT
    environment would make this the wrong comparison target anyway -- the
    leak this guards is a test resolving the operator's real ACCOUNT home,
    not whatever a machine-level override happens to point at.

    Feeds `_settings_home.FORBID_REAL_SETTINGS_HOME_ENV` below (2026-09-18
    incident: an install test wrote 371 launchers into this exact directory
    for real). Returns "" if unresolvable (e.g. no passwd entry in a minimal
    container) -- the guard is then inert rather than fail loud on a box
    where it cannot even name the path it would refuse.
    """
    if os.name == "nt":
        home = os.environ.get("USERPROFILE", "")
    else:
        try:
            import pwd

            home = pwd.getpwuid(os.getuid()).pw_dir
        except (KeyError, ImportError, OSError):
            home = ""
    if not home:
        return ""
    return str(Path(home) / ".coordinator-claude-settings")


_REAL_SETTINGS_HOME = _capture_real_settings_home()


def _capture_real_content_root() -> str:
    """Resolve the sibling content checkout ONCE, at collection time, under
    the real (un-quarantined) HOME — used ONLY to locate the manifest to copy
    into a throwaway stub (see ``_build_stub_content_root`` below). The real path itself
    is never seeded into a quarantined test's content-root pointer.

    Same capture-before-quarantine shape as ``_REAL_USER_SITE`` above, for the
    same class of reason. ``coordinator/bin/lib/coordinator_registry.py``
    resolves its manifest (``coordinator/schemas/coordinator-registry.manifest
    .json``, which DR-047 keeps in coordinator-content-repo while this repo owns the engine)
    at IMPORT time, through a ladder whose every live rung is home-anchored:
    the content-root pointer files, the marketplace-cache probe, the flat
    plugin-layout probe, and the machine-local registry CLI all hang off
    ``$HOME``/settings-home. Quarantining HOME therefore does not isolate that
    module — it makes it unresolvable, and it fails loud with an
    install-integrity ``FileNotFoundError`` at import. Every test that loads a
    ``coordinator/bin/`` CLI (in-process via ``SourceFileLoader``, as
    ``baton_assemble.apply._load_doc_new_module`` does, or as a spawned
    subprocess inheriting this environment) dies before reaching its assertion.

    Returns "" when nothing resolves — on a machine with no coordinator-content-repo checkout
    the seeding below is skipped and behavior is unchanged.
    """
    try:
        from coordinator_core.testing.content_root import resolve_content_root

        root = resolve_content_root()
    except Exception:  # pragma: no cover - defensive; a broken resolver must not break collection
        return ""
    return root if root and os.path.isdir(root) else ""


_REAL_CONTENT_ROOT = _capture_real_content_root()

_REAL_DOE_MANIFEST_RELPATH = os.path.join(
    "coordinator", "schemas", "coordinator-registry.manifest.json"
)

_REAL_DOE_CROSS_REPO_MEMO_SCHEMA_RELPATH = os.path.join(
    "coordinator", "schemas", "cross-repo-memo.schema.json"
)

_STUB_DOE_SEED_RELPATHS = (
    _REAL_DOE_MANIFEST_RELPATH,
    _REAL_DOE_CROSS_REPO_MEMO_SCHEMA_RELPATH,
    # dispatch.emit's plan route refuses to emit without an execute-review stage.
    os.path.join("coordinator", "contract", "review-roster-fragment.json"),
    os.path.join("coordinator", "schemas", "review-stage.schema.json"),
)


def _real_doe_seed_source(relpath: str) -> str:
    """Locate one seed file inside ``_REAL_CONTENT_ROOT``, tolerant of BOTH
    layouts, and return its absolute path ("" when absent).

    The private content checkout keeps these under ``coordinator/…``; the
    published `coordinator-claude` mirror ships them FLAT at its repo root,
    and that mirror is what a cloud container registers as `repos.content_root`
    — so `resolve_content_root()` legitimately hands back a flat root there.
    `coordinator/bin/lib/coordinator_registry.py::_mp_candidate_manifest_path`
    already probes both arms for exactly this reason; hardcoding only the
    ``coordinator/`` arm here made the stub builder blind to the flat mirror,
    returned "" from `_build_stub_content_root`, and left the quarantined HOME
    with no content-root pointer at all — so every test that loads a
    `coordinator/bin/` CLI died at import on the registry's install-integrity
    `FileNotFoundError`, which is the failure this whole seeding path exists
    to prevent.

    The STUB is always written in the canonical ``coordinator/…`` layout
    whatever the source layout was: `content_root()`-anchored readers join that
    shape, and the registry prober accepts it on both arms.
    """
    if not _REAL_CONTENT_ROOT:
        return ""
    candidates = [relpath]
    head, _, tail = relpath.partition(os.sep)
    if head == "coordinator" and tail:
        candidates.append(tail)
    for candidate in candidates:
        path = os.path.join(_REAL_CONTENT_ROOT, candidate)
        if os.path.isfile(path):
            return path
    return ""


def _build_stub_content_root(base_dir: str) -> str:
    """Build a throwaway content-checkout STUB under ``base_dir`` and return its path.

    Copies only the explicitly named files quarantined tests actually need
    to READ — listed in ``_STUB_DOE_SEED_RELPATHS`` above — out of the real
    checkout captured by ``_capture_real_content_root``. Nothing else from the
    real repo is copied or referenced. A file earns a place in that tuple
    only when a quarantined test genuinely reads it from the content side, added
    deliberately one at a time — this must never become a whole-tree copy.

    This is the fix for a P1: seeding the REAL content path into a
    quarantined test's content-root pointer made the manifest READ succeed,
    but ``coordinator_registry.py::content_root()`` is also the documented anchor
    other call sites join WRITE targets onto (``state/lessons-outbox``,
    ``state/improvement-queue``) — so any quarantined test that reached a
    ``content_root()``-anchored write path without its own override could write
    into the LIVE sibling repo, the exact corruption class this fixture
    exists to prevent. Pointing at a stub instead keeps the needed reads
    working while making every write land inside the throwaway quarantine
    dir — strictly more isolation than tests had before the real path was
    ever seeded (``16e1c220c``), and strictly less exposure than the real
    repo. That "strictly less exposure" rationale is why the seed set stays
    an explicit named tuple rather than a directory copy: every additional
    file widens the exposure the stub was built to shrink.

    Returns "" if the real content root or the REGISTRY MANIFEST cannot be
    located, mirroring ``_capture_real_content_root``'s graceful degradation —
    callers must treat an empty return the same as "nothing to seed". The
    manifest is deliberately load-bearing rather than one seed among equals:
    a stub carrying schemas but no registry resolves ``content_root()`` into a
    tree that fails its read later and further away, which is worse than
    degrading here. Any OTHER missing seed file is skipped, not fatal — it
    surfaces as its own reader's ENOENT, naming the file it wanted.
    """
    if not _REAL_CONTENT_ROOT:
        return ""
    if not _real_doe_seed_source(_REAL_DOE_MANIFEST_RELPATH):
        return ""

    import shutil

    stub_root = os.path.join(base_dir, "coordinator-content-repo-stub")
    for relpath in _STUB_DOE_SEED_RELPATHS:
        real_path = _real_doe_seed_source(relpath)
        if not real_path:
            continue
        stub_path = os.path.join(stub_root, relpath)
        os.makedirs(os.path.dirname(stub_path), exist_ok=True)
        shutil.copyfile(real_path, stub_path)

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
    if not request.node.get_closest_marker("real_machine_mutation"):
        monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")

    if request.node.get_closest_marker("real_home"):
        return None

    if _REAL_SETTINGS_HOME:
        from coordinator_core import _settings_home as _settings_home_mod

        monkeypatch.setenv(_settings_home_mod.FORBID_REAL_SETTINGS_HOME_ENV, _REAL_SETTINGS_HOME)

    quarantine = tmp_path_factory.mktemp("home-quarantine")
    monkeypatch.setenv("HOME", str(quarantine))
    monkeypatch.setenv("USERPROFILE", str(quarantine))
    # HOMEDRIVE+HOMEPATH are expanduser's second Windows tier; leaving them set
    # would let the real profile back in whenever USERPROFILE is deleted.
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    # COORDINATOR_SETTINGS_HOME is checked by `_settings_home.settings_home()`
    # AHEAD of every home var, so leaving it set defeats this whole fixture on
    # any box that exports it: the durable content-root pointer rung, the machine-local
    # registry rung, and the engine-build path all keep reading the operator's
    # LIVE settings tree no matter what home a test then sets. Measured
    # 2026-09-06 across eleven test files: 58 failures with it set, 1 with it
    # unset -- and every one of those 57 was a test silently measuring a
    # different branch than its own name claimed, which is worse than a red.
    # This is not a new policy, it is the gap the fixture already assumes shut:
    # it seeds a stub pointer at
    # `<quarantine>/.coordinator-claude-settings/machine-local/.coordinator-content-root`,
    # which only anything reads if settings-home resolves INTO the quarantine.
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)

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
        _warm_base = Path(tempfile.mkdtemp(prefix="cwrb-", dir="/tmp"))

        def _drop_warm_base() -> None:
            try:
                rmtree_or_raise(_warm_base, label="conftest warm runtime base")
            except CloneTeardownLeak:
                raise
            except Exception:  # noqa: BLE001 -- see above
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
    # one read the quarantine would otherwise break outright: the content-root
    # pointer that `coordinator_registry`'s import-time manifest bootstrap
    # resolves through (see `_capture_real_content_root` above for the mechanism).
    # Seeded as a FILE inside the quarantine, not as a `REPO_CONTENT_ROOT` env
    # override: the env route outranks the machine-local registry in the
    # ratified DR-071 precedence and would silently mask the rung a test is
    # exercising, whereas the pointer file sits at the same rung a real install
    # populates and leaves that precedence intact. Read-only plumbing — it
    # restores no write access to the real home, so it does not reopen the
    # live-machine-config-corruption hole this fixture exists to close.
    #
    # The pointer value itself is a THROWAWAY STUB (`_build_stub_content_root`),
    # never `_REAL_CONTENT_ROOT`. Seeding the real path here made the manifest
    # read succeed but ALSO made `content_root()` — the documented join-anchor
    # other call sites use for WRITE targets (`state/lessons-outbox`,
    # `state/improvement-queue`) — resolve to the live sibling checkout, so a
    # quarantined test reaching a `content_root()`-anchored write path without its
    # own override could corrupt the real repo: exactly the class of bug this
    # fixture exists to prevent. The stub carries only a copy of the one file
    # a manifest read needs, so reads still succeed and every write instead
    # lands inside this test's own throwaway quarantine directory.
    #
    # Both locations are written because a real install carries both and the
    # reader (`coordinator_core.content_root.read_pointer_files`) tries them in
    # this order: `${settings-home}/machine-local/.coordinator-content-root`
    # (durable, DR-072) then `${CLAUDE_HOME:-$HOME}/.claude/.coordinator-content-root`
    # (fallback). Seeding only the first would leave any test that redirects
    # COORDINATOR_SETTINGS_HOME on its own back at an unresolvable pointer.
    stub_content_root = _build_stub_content_root(str(quarantine))
    if stub_content_root:
        for pointer in (
            quarantine / ".coordinator-claude-settings" / "machine-local" / ".coordinator-content-root",
            quarantine / ".claude" / ".coordinator-content-root",
        ):
            pointer.parent.mkdir(parents=True, exist_ok=True)
            pointer.write_text(stub_content_root + "\n", encoding="utf-8")

    override_doc_src = Path(__file__).resolve().parent.parent / "docs" / "reference" / "guard-override-keys.md"
    if override_doc_src.is_file():
        override_doc_dst = (
            quarantine
            / ".coordinator-claude-settings"
            / "coordinator-claude"
            / "docs"
            / "wiki"
            / "guard-override-keys.md"
        )
        override_doc_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(override_doc_src, override_doc_dst)

    return quarantine


@pytest.fixture(autouse=True)
def _fence_real_registry_writes(monkeypatch):
    """No test reaches the operator's real registry.

    Trap: a fake install path and a cleared kill switch are not a sandbox —
    `_win_user_path_prepend` once replaced a real HKCU PATH with a fixture path
    and `claude` stopped launching. A test that needs the write path installs
    its own fake `winreg` (or monkeypatches these names after this fixture).
    """
    try:
        import winreg
    except ImportError:
        return

    def _refuse(*_a, **_k):
        raise AssertionError("test reached a real winreg write; inject a fake winreg")

    for name in ("SetValue", "SetValueEx", "DeleteValue", "DeleteKey", "DeleteKeyEx", "CreateKey", "CreateKeyEx"):
        if hasattr(winreg, name):
            monkeypatch.setattr(winreg, name, _refuse)


@pytest.fixture(autouse=True)
def _reset_foreign_repo_probe_memo():
    from coordinator_core.git_scope import reset_foreign_repo_probe_memo

    reset_foreign_repo_probe_memo()
    yield
    reset_foreign_repo_probe_memo()


@pytest.fixture(autouse=True)
def _isolate_advisory_dedupe_gitdir(monkeypatch, tmp_path_factory):
    """Point the per-session advisory dedupe store at a per-test directory.

    Tests dispatch with a fixed fake ``cwd`` and session id; when that ``cwd``
    resolves to a real ``.git`` on the box the dedupe markers persist across
    runs and silence a later run's first advisory.
    """
    import sys

    # Patch only when a test already paid for the heavy `dispatch` import;
    # forcing it here would tax every suite that never touches guards.
    dispatch = sys.modules.get("coordinator_core.bash_guards.dispatch")
    if dispatch is None:
        return
    store = tmp_path_factory.mktemp("advisory_dedupe_gitdir")
    monkeypatch.setattr(dispatch, "_resolve_gitdir_for_dedupe", lambda cwd: store)


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
            fn = getattr(engine_root, seam, None)
            if fn is not None:
                fn()

    _cold()
    yield
    _cold()


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
# The per-module cache-reset fixtures (the content-root resolver test's _clean_env,
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
    from coordinator_core.ipc import PYTEST_UNSTAMPED_DISPATCH_ENV, allow_unstamped_dispatch

    allow_unstamped_dispatch()
    # Spawned CLIs honour this only while pytest is running a test (see
    # `ipc.allow_unstamped_dispatch_under_pytest`); set here so it is part of
    # every test's environ baseline rather than reported as a leak.
    os.environ[PYTEST_UNSTAMPED_DISPATCH_ENV] = "1"


@pytest.fixture
def exercise_suspended_op(monkeypatch):
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
    try:
        return str(_uuid.UUID(name)) == name.lower()
    except (ValueError, AttributeError, TypeError):
        return False


def _live_hub_session_dirs() -> set:
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


_OWN_LIVE_STATE_DIRS = (
    Path(__file__).resolve().parent.parent / "state" / "improvement-queue",
    Path(__file__).resolve().parent.parent / "state" / "lessons-outbox",
)

def _resolve_live_doe_lessons_outbox():
    root = (os.environ.get("REPO_CONTENT_ROOT") or "").strip()
    if not root:
        # Load coordinator_registry BY LOCATION, and do not leave
        # `coordinator/bin/lib` on `sys.path`. An earlier version inserted it at
        # position 0 and left it there: this runs at conftest IMPORT time, before
        # every test module in the repo is imported, so that directory would
        # shadow same-named modules for the whole session — the exact
        # import-precedence hazard this repo's root conftest exists to close,
        # reintroduced by a guard. The path entry is removed in a `finally`.
        try:
            import importlib.util as _ilu  # noqa: PLC0415
            import sys as _sys  # noqa: PLC0415

            _lib = Path(__file__).resolve().parent.parent / "coordinator" / "bin" / "lib"
            _added = str(_lib) not in _sys.path
            if _added:
                _sys.path.insert(0, str(_lib))
            try:
                _spec = _ilu.spec_from_file_location(
                    "_guard_coordinator_registry", str(_lib / "coordinator_registry.py")
                )
                _mod = _ilu.module_from_spec(_spec)
                _spec.loader.exec_module(_mod)
                root = _mod.content_root()
            finally:
                if _added:
                    try:
                        _sys.path.remove(str(_lib))
                    except ValueError:
                        pass
        except Exception:
            root = ""
    return (Path(root) / "state" / "lessons-outbox") if root else None


_LIVE_DOE_LESSONS_OUTBOX = _resolve_live_doe_lessons_outbox()


@_pytest.fixture(autouse=True)
def _no_live_state_corpus_writes(request):
    doe = _LIVE_DOE_LESSONS_OUTBOX
    guarded = _OWN_LIVE_STATE_DIRS + ((doe,) if doe is not None else ())
    before = {}
    for d in guarded:
        try:
            before[d] = set(os.listdir(d)) if d.is_dir() else set()
        except OSError:
            before[d] = set()
    yield
    for d in guarded:
        try:
            after = set(os.listdir(d)) if d.is_dir() else set()
        except OSError:
            continue
        gained = after - before[d]
        if gained:
            _pytest.fail(
                f"_no_live_state_corpus_writes: {d} gained {sorted(gained)!r} during "
                f"{request.node.nodeid} -- a write-root rung was not neutralized. "
                f"Set the seam's isolation root (QUEUE_APPEND_OUTPUT_ROOT / "
                f"LESSON_PROMOTE_OUTBOX_ROOT, dir must EXIST), strip "
                f"REPO_CONTENT_ROOT / CLAUDE_KLABAUTER_ROOT, and run the child cold -- a "
                f"warm-served CLI never receives any of them.",
                pytrace=False,
            )


@_pytest.fixture(autouse=True)
def _redirect_live_session_hub(monkeypatch, tmp_path_factory):
    """While a non-UUID `COORDINATOR_SESSION_ID` is ambient, any
    `session.core.sessions_dir` resolution that lands on the REAL repo's hub is answered with a per-test tmp hub instead, so an ambient
    `COORDINATOR_SESSION_ID` plus a cwd-derived root cannot mint live entries.

    Negative-spec: covers in-process `sessions_dir()` consumers only; code that
    composes `<root>/.git/coordinator-sessions` itself, and child processes,
    still reach the live hub and stay with `_no_new_live_session_hub_entries`."""
    from coordinator_core.session import core as _core

    original = _core._sessions_dir_resolve
    live = os.path.normcase(os.path.abspath(_LIVE_HUB))
    holder: dict = {}

    def _resolve(cwd):
        result = original(cwd)
        sid = os.environ.get("COORDINATOR_SESSION_ID", "")
        if (
            result
            and sid
            and not _looks_like_a_harness_session_id(sid)
            and os.path.normcase(os.path.abspath(result)) == live
        ):
            if "hub" not in holder:
                holder["hub"] = str(
                    tmp_path_factory.mktemp("redirected-live-hub") / "coordinator-sessions"
                )
            return holder["hub"]
        return result

    _core.reset_sessions_dir_cache()
    monkeypatch.setattr(_core, "_sessions_dir_resolve", _resolve)
    yield
    _core.reset_sessions_dir_cache()


_HUB_WRITE_EVENTS = ("os.remove", "os.rename", "os.link", "os.symlink", "shutil.copyfile")
_hub_watch_active = False
_hub_written: "set[str]" = set()


def _hub_entry_of(target, hub: str) -> "str | None":
    """The top-level hub entry name `target` sits under, or `None` when it is outside `hub`,
    is the hub itself, or sits under a UUID-named (peer-session) entry."""
    if not isinstance(target, (str, bytes, os.PathLike)):
        return None
    try:
        path = os.path.normcase(os.path.abspath(os.fsdecode(target)))
    except (TypeError, ValueError):
        return None
    prefix = os.path.normcase(os.path.abspath(hub)) + os.sep
    if not path.startswith(prefix):
        return None
    name = path[len(prefix):].split(os.sep, 1)[0]
    if not name or _looks_like_a_harness_session_id(name):
        return None
    return name


def _hub_audit_hook(event: str, args: tuple) -> None:
    if not _hub_watch_active:
        return
    if event == "open":
        if len(args) >= 3 and isinstance(args[2], int) and args[2] & _WRITE_FLAGS:
            target = args[0]
        else:
            return
    elif event in _HUB_WRITE_EVENTS:
        target = args[1] if event != "os.remove" and len(args) > 1 else args[0]
    else:
        return
    if not isinstance(target, (str, bytes, os.PathLike)):
        return
    text = os.fsdecode(target)
    if "coordinator-sessions" in text:
        _hub_written.add(text)


sys.addaudithook(_hub_audit_hook)


def _writes_into_hub_entries(written, hub: str) -> "list[str]":
    """Sorted hub-relative paths in `written` that sit under a non-UUID hub entry."""
    prefix = os.path.normcase(os.path.abspath(hub)) + os.sep
    return sorted(
        {
            os.path.normcase(os.path.abspath(p))[len(prefix):]
            for p in written
            if _hub_entry_of(p, hub) is not None
        }
    )


@_pytest.fixture(autouse=True)
def _no_writes_into_live_hub_entries():
    """Fail a test whose own process wrote into a non-UUID entry of the real hub.

    Attribution is by audit hook, not by before/after stat: peers write the hub's
    shared infrastructure entries continuously, so a stat delta cannot tell their
    writes from this test's. Writes from a spawned child are not seen."""
    global _hub_watch_active
    _hub_written.clear()
    _hub_watch_active = True
    try:
        yield
    finally:
        _hub_watch_active = False
    leaked = _writes_into_hub_entries(list(_hub_written), _LIVE_HUB)
    _hub_written.clear()
    assert not leaked, (
        f"test wrote into existing entries of the REAL session hub {_LIVE_HUB}: "
        f"{leaked!r} — the code under test resolved the hub from the process cwd "
        "while taking its session id from a fixture. Point it at a tmp_path repo."
    )


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


# ---------------------------------------------------------------------------
# Live cross-repo-inbox write guard — P128-C2,
# docs/plans/2026-09-12-stop-engine-memo-fixtures-reaching-a-liv.md
# ---------------------------------------------------------------------------
#
# Eighteen synthetic fixture memos landed in example-retrieval-repo's REAL
# `state/cross-repo/inbox/` on 2026-09-04, because the memo fixtures set only
# `CLAUDE_HOME` while `COORDINATOR_SETTINGS_HOME` — consulted first by
# `_settings_home.settings_home()` — stayed pointed at the real machine-local
# registry until `0f0cd2b180` closed that vector two days later. Same
# eager-at-import-time discipline as `_resolve_live_doe_lessons_outbox`
# above and for the identical reason: resolving lazily inside a test risks a
# cold-env test running first and poisoning the ambient environment this
# reads, which would silently disarm the guard for the whole session. The
# root set is resolved here, before `_quarantine_real_home` (or any other
# fixture) applies, against the AMBIENT pre-quarantine environment.


def _resolve_live_inbox_roots() -> "tuple[str, ...]":
    own_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    roots = [os.path.join(memo_corpus.memo_corpus_root(own_repo_root), "inbox")]
    try:
        from coordinator_core.ops.fleet import _memo_resolver  # noqa: PLC0415

        registered_repos = _memo_resolver.read_registry_repos()
    except Exception:
        registered_repos = {}
    for repo_path in registered_repos.values():
        try:
            corpus_root, _ = memo_corpus.receiver_inbox_root(repo_path)
        except Exception:
            continue
        roots.append(os.path.join(corpus_root, "inbox"))
    return tuple(roots)


_LIVE_INBOX_ROOTS = _resolve_live_inbox_roots()


def _live_inbox_roots() -> "tuple[str, ...]":
    return _LIVE_INBOX_ROOTS


_inbox_watch_roots: "tuple[str, ...]" = ()
_inbox_written: "set[str]" = set()


def _record_inbox_write(target) -> None:
    if not _inbox_watch_roots or not isinstance(target, (str, bytes, os.PathLike)):
        return
    try:
        path = os.path.abspath(os.fsdecode(target))
    except (TypeError, ValueError):
        return
    for root in _inbox_watch_roots:
        if path.startswith(root + os.sep):
            _inbox_written.add(path)
            return


_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT


def _inbox_audit_hook(event: str, args: tuple) -> None:
    # In-process writes only: a subprocess's writes are never attributed.
    if event == "open":
        if len(args) >= 3 and isinstance(args[2], int) and args[2] & _WRITE_FLAGS:
            _record_inbox_write(args[0])
    elif event in ("os.rename", "os.link", "os.symlink"):
        _record_inbox_write(args[1])
    elif event == "shutil.copyfile":
        _record_inbox_write(args[1])


sys.addaudithook(_inbox_audit_hook)


@_pytest.fixture(autouse=True)
def _no_live_inbox_writes_from_suite():
    """Fail when THIS process wrote into a live inbox during the test.

    A directory delta alone is not enough: peer sessions deliver memos to the
    same inboxes at any moment. A new file counts only if the audit hook saw
    this process open/rename/link it.
    """
    global _inbox_watch_roots
    roots = tuple(os.path.abspath(r) for r in _live_inbox_roots())
    before = {}
    for root in roots:
        try:
            with os.scandir(root) as entries:
                before[root] = {e.name for e in entries}
        except OSError:
            before[root] = set()
    _inbox_written.clear()
    _inbox_watch_roots = roots
    try:
        yield
    finally:
        _inbox_watch_roots = ()
    for root in roots:
        try:
            with os.scandir(root) as entries:
                after = {e.name for e in entries}
        except OSError:
            continue
        leaked = sorted(
            n
            for n in after - before[root]
            if os.path.join(root, n) in _inbox_written
        )
        if leaked:
            _pytest.fail(
                f"_no_live_inbox_writes_from_suite: {root} gained {leaked!r} — a "
                "test delivered into a LIVE cross-repo inbox. Point the receiver "
                "at a tmp_path repo instead.",
                pytrace=False,
            )


# ---------------------------------------------------------------------------
# Environment-answered mode defaults — suite-wide quarantine, same class as the
# real-home quarantine above.
#
# `MODE_KEYS` entries may declare an `environment_default` (see
# `coordinator_core.session.mode_resolution`). `compaction_warnings` does: it
# answers `informational` on a box that is not the developer's own. Correct behaviour, and it makes an AMBIENT MACHINE FACT load-bearing
# for every test asserting anything downstream of that key.
#
# Left unpinned, such a test passes on an attended box and fails in a cloud
# session while naming neither — the same shape as the `HOME` leak this file
# was written for, and just as invisible. Measured 2026-09-05: eleven
# `coordinator_core/hooks` tests, none of which mentioned locality.
#
# Pinned to ABSTAIN, so the static default governs and tests reproduce
# attended-box behaviour by default. A test exercising the environment leg
# re-patches this itself and says so in its own name.
#
# SCOPE LIMIT, LOAD-BEARING: a `monkeypatch` does not cross a process
# boundary. A test that spawns the real hook (see
# `coordinator_core/tests/test_fleet_mode_process_boundary.py`) is NOT covered
# here and must state the value it wants in the subprocess's own inputs —
# never rely on "no fleet file" meaning `standard`.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Guard level — suite-wide pin to `strict`.
#
# The product default follows the machine profile (machine_profile.guard_level:
# strict on an author box, warn on a consumer box), so an unpinned test would
# read the ambient registry and see a deny or an `allow` advisory depending on
# the box. Tests that assert a guard's deny envelope state the level instead.
# The per-package conftests (bash_guards, hooks, write_guards) pin the same
# value; this covers every other test directory. A test exercising the warn or
# off leg sets MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL itself.
#
# SCOPE LIMIT: a `monkeypatch` does not cross a process boundary that builds
# its own env; a test spawning a real hook states the level in the child's env.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pin_guard_level_strict_suite_wide(monkeypatch):
    from coordinator_core import machine_profile

    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "strict")
    machine_profile.reset_cache()
    yield
    machine_profile.reset_cache()


@pytest.fixture(autouse=True)
def _pin_environment_answered_mode_defaults(monkeypatch):
    try:
        monkeypatch.setattr(
            "coordinator_core.session.mode_resolution."
            "_compaction_default_for_environment",
            lambda env=None: None,
        )
    except (ImportError, AttributeError):  # pragma: no cover - import-order safety
        pass


# ---------------------------------------------------------------------------
# Auto-compact window — suite-wide quarantine, same class as the two above.
#
# `CLAUDE_CODE_AUTO_COMPACT_WINDOW` sets the window Claude Code compacts
# against, and the context-pressure bands are runway distances back from
# `window - 33,000`. An operator who sets it fleet-wide (this PM does, to
# 500,000) therefore moves every band in every test that asserts anything
# downstream of a reading — a fixture chosen against a 1,000,000-token window
# lands in a different band, and the test names neither the window nor the
# variable it moved with.
#
# Pinned to ABSENT, so the model window governs and tests reproduce
# unoverridden behaviour by default. A test exercising the override leg sets
# it itself and says so in its own name — see
# `coordinator_core/hooks/tests/test_postuse_context_pressure.py ::
# test_threshold_matches_the_established_cloud_cut_under_the_env_override`.
#
# SCOPE LIMIT, same as above: a `monkeypatch` does not cross a process
# boundary. A test that spawns the real hook must state the window it wants in
# the subprocess's own environment.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pin_auto_compact_window_absent(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)


# ---------------------------------------------------------------------------
# Foreign-process kill tripwire
# ---------------------------------------------------------------------------
#
# The suite runs on a box carrying dozens of live sessions; it must be unable
# to signal any process it did not spawn. Installed at import, so it covers
# every xdist worker. A test's own monkeypatch of `os.kill` still wins (it
# replaces this wrapper for that test only), which is fine: a patched kill
# reaches nothing.
#
# Traps: on Windows `os.kill(pid, 0)` is NOT a liveness probe -- signal 0 is
# CTRL_C_EVENT, sent via GenerateConsoleCtrlEvent to every process in the
# group, and pid 0 means the whole console, i.e. the session hosting pytest.
# Only Python-level kills are caught; a raw ctypes TerminateProcess or a
# `-c` child that never imports this conftest is outside the wrapper's reach.


class ForeignProcessKill(RuntimeError):
    pass


def _is_own_process_tree(pid: int) -> bool:
    me = os.getpid()
    if pid == me:
        return True
    try:
        import psutil
    except ImportError:
        return False
    try:
        return any(p.pid == me for p in psutil.Process(pid).parents())
    except psutil.NoSuchProcess:
        return True
    except psutil.Error:
        return False


def _refuse_foreign(pid, what: str) -> None:
    if not isinstance(pid, int) or pid <= 0 or not _is_own_process_tree(pid):
        raise ForeignProcessKill(
            f"{what} targeted pid {pid!r}, outside this test process's tree -- "
            "a test may only signal processes it spawned."
        )


_real_os_kill = os.kill


def _guarded_os_kill(pid, sig):
    _refuse_foreign(pid, f"os.kill(sig={sig!r})")
    return _real_os_kill(pid, sig)


os.kill = _guarded_os_kill

try:
    import psutil as _psutil_for_tripwire
except ImportError:
    _psutil_for_tripwire = None

if _psutil_for_tripwire is not None:
    def _wrap_psutil(name):
        real = getattr(_psutil_for_tripwire.Process, name)

        def guarded(self, *args, **kwargs):
            _refuse_foreign(self.pid, f"psutil.Process.{name}")
            return real(self, *args, **kwargs)

        setattr(_psutil_for_tripwire.Process, name, guarded)

    for _name in ("kill", "terminate", "send_signal", "suspend"):
        _wrap_psutil(_name)
