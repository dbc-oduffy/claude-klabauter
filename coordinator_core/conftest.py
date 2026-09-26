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

from coordinator_core import memo_corpus

_REAL_UNLINK = os.unlink
_REAL_RMDIR = os.rmdir
_REAL_WALK = os.walk
_REAL_JOIN = os.path.join

# MECHANISM (verified via a diagnostic pytest plugin patching
# under the ORIGINAL (now-orphaned) Package instance, so its FixtureDefs carry
# ._matchfactories` (fixtures.py) matches primarily by NODE IDENTITY
# THE FIX. Restore `baseid` string matching as an unconditional FALLBACK —
# NEGATIVE SPEC
#   - Does NOT touch fixture SELECTION when multiple same-name fixturedefs
#     9.1.x `.node` is an INSTANCE attribute, so a class-level
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

# per-test fixture below has a chance to monkeypatch HOME/USERPROFILE. A test
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

_STUB_DOE_SEED_RELPATHS = (
    _REAL_DOE_MANIFEST_RELPATH,
    _REAL_DOE_CROSS_REPO_MEMO_SCHEMA_RELPATH,
)


def _real_doe_seed_source(relpath: str) -> str:
    """Locate one seed file inside ``_REAL_DOE_ROOT``, tolerant of BOTH DoE
    layouts, and return its absolute path ("" when absent).

    The private DoE-claude checkout keeps these under ``coordinator/…``; the
    published `coordinator-claude` mirror ships them FLAT at its repo root,
    and that mirror is what a cloud container registers as `repos.doe_claude`
    — so `resolve_doe_root()` legitimately hands back a flat root there.
    `coordinator/bin/lib/coordinator_registry.py::_mp_candidate_manifest_path`
    already probes both arms for exactly this reason; hardcoding only the
    ``coordinator/`` arm here made the stub builder blind to the flat mirror,
    returned "" from `_build_stub_doe_root`, and left the quarantined HOME
    with no ``.doe-root`` pointer at all — so every test that loads a
    `coordinator/bin/` CLI died at import on the registry's install-integrity
    `FileNotFoundError`, which is the failure this whole seeding path exists
    to prevent.

    The STUB is always written in the canonical ``coordinator/…`` layout
    whatever the source layout was: `doe_root()`-anchored readers join that
    shape, and the registry prober accepts it on both arms.
    """
    if not _REAL_DOE_ROOT:
        return ""
    candidates = [relpath]
    head, _, tail = relpath.partition(os.sep)
    if head == "coordinator" and tail:
        candidates.append(tail)
    for candidate in candidates:
        path = os.path.join(_REAL_DOE_ROOT, candidate)
        if os.path.isfile(path):
            return path
    return ""


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
    if not _real_doe_seed_source(_REAL_DOE_MANIFEST_RELPATH):
        return ""

    import shutil

    stub_root = os.path.join(base_dir, "doe-claude-stub")
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
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)

    # Quarantining HOME/USERPROFILE also, as an unintended side effect, hides whatever
    # (see module-level `_REAL_USER_SITE` comment above for the exact mechanism). Fold
    # the real user-site path into PYTHONPATH so a spawned child's `sys.path` still
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

    # `git-web--browse`, which LAUNCHES THE OPERATOR'S DEFAULT BROWSER at a local
    # on a box where the global triple is set, so the suite runs UNPROTECTED and
    # `GIT_CONFIG_*` is the injection form that survives a quarantined HOME.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "3")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "help.format")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "web")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "web.browser")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "noop")
    monkeypatch.setenv("GIT_CONFIG_KEY_2", "browser.noop.cmd")
    monkeypatch.setenv("GIT_CONFIG_VALUE_2", "echo not-opening-browser-for:")

    # Belt-and-braces companion to the HOME/USERPROFILE quarantine above: this
    # fixture redirects the FILESYSTEM a test writes into, but a 2026-07-28

    # `%LOCALAPPDATA%`, which the HOME/USERPROFILE quarantine above does not
    # `%LOCALAPPDATA%/coordinator/warm/` and populated it with breadcrumb and
    # SHORT ON POSIX, AND THAT IS NOT A TIDINESS PREFERENCE -- it is the whole
    # (`election.SUN_PATH_MAX_BYTES` holds the line at 100), and the quarantine
    # unexplained OSError" -- during the warm client's PREAMBLE, before any test
    from coordinator_core.warm import breadcrumb as _warm_breadcrumb

    if os.name == "nt":
        _warm_base = quarantine / "warm-runtime-base"
    else:
        _warm_base = Path(tempfile.mkdtemp(prefix="cwrb-", dir="/tmp"))

        def _drop_warm_base() -> None:
            try:
                shutil.rmtree(_warm_base, ignore_errors=True)
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
    # Seeded as a FILE inside the quarantine, not as a `REPO_DOE_CLAUDE` env
    # The pointer value itself is a THROWAWAY STUB (`_build_stub_doe_root`),
    # never `_REAL_DOE_ROOT`. Seeding the real path here made the manifest
    # `${CLAUDE_HOME:-$HOME}/.claude/.doe-root` (legacy fallback). Seeding only
    # the first would leave any test that redirects COORDINATOR_SETTINGS_HOME
    stub_doe_root = _build_stub_doe_root(str(quarantine))
    if stub_doe_root:
        for pointer in (
            quarantine / ".coordinator-claude-settings" / "machine-local" / ".doe-root",
            quarantine / ".claude" / ".doe-root",
        ):
            pointer.parent.mkdir(parents=True, exist_ok=True)
            pointer.write_text(stub_doe_root + "\n", encoding="utf-8")

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

    try:
        from coordinator_core.ops.coordinator_doe_root import _reset_doe_root_cache

        _reset_doe_root_cache()
    except ImportError:  # pragma: no cover - defensive, mirrors the capture helper
        pass

    return quarantine


@pytest.fixture(autouse=True)
def _reset_foreign_repo_probe_memo():
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
            fn = getattr(engine_root, seam, None)
            if fn is not None:
                fn()

    _cold()
    yield
    _cold()


# bare `export` was correct because the process was about to exit. As an IMPORTED Python

#   PYTEST_CURRENT_TEST — pytest rewrites this on every setup/call/teardown transition,
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


# inherit from a DIFFERENT source. A test that wants to assert the REFUSAL


def pytest_configure(config: pytest.Config) -> None:
    from coordinator_core.ipc import allow_unstamped_dispatch

    allow_unstamped_dispatch()


@pytest.fixture
def exercise_suspended_op(monkeypatch):
    from coordinator_core import op_budget_suspension

    monkeypatch.setattr(op_budget_suspension, "SUSPENDED_OPS", {})
    return None


#   - Considers DIRECTORIES ONLY. The hub also carries plain files that no
#   - DOES now attribute a new DIRECTORY by recorded owner, closing the

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
    root = (os.environ.get("DOE_ROOT") or os.environ.get("REPO_DOE_CLAUDE") or "").strip()
    if not root:
        # Load coordinator_registry BY LOCATION, and do not leave
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
                root = _mod.doe_root()
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
                f"LESSON_PROMOTE_OUTBOX_ROOT, dir must EXIST), strip DOE_ROOT / "
                f"REPO_DOE_CLAUDE / CLAUDE_KLABAUTER_ROOT, and run the child cold -- a "
                f"warm-served CLI never receives any of them.",
                pytrace=False,
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


# `CLAUDE_HOME` while `COORDINATOR_SETTINGS_HOME` — consulted first by


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


@_pytest.fixture(autouse=True)
def _no_live_inbox_writes_from_suite():
    roots = _live_inbox_roots()
    before = {}
    for root in roots:
        try:
            with os.scandir(root) as entries:
                before[root] = {e.name for e in entries}
        except OSError:
            before[root] = set()
    yield
    for root in roots:
        try:
            with os.scandir(root) as entries:
                after = {e.name for e in entries}
        except OSError:
            continue
        leaked = sorted(after - before[root])
        if leaked:
            _pytest.fail(
                f"_no_live_inbox_writes_from_suite: {root} gained {leaked!r} — a "
                "test delivered into a LIVE cross-repo inbox. Point the receiver "
                "at a tmp_path repo instead.",
                pytrace=False,
            )


# `MODE_KEYS` entries may declare an `environment_default` (see
# SCOPE LIMIT, LOAD-BEARING: a `monkeypatch` does not cross a process


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


# `CLAUDE_CODE_AUTO_COMPACT_WINDOW` sets the window Claude Code compacts


@pytest.fixture(autouse=True)
def _pin_auto_compact_window_absent(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)


# CTRL_C_EVENT, sent via GenerateConsoleCtrlEvent to every process in the


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
