"""Machine-local registry sandboxing and live-registry write tripwire for tests.

WHY THIS EXISTS
---------------
The machine-local registry (``<settings-home>/machine-local/registry.local.toml``)
is the fleet's cross-repo resolution substrate: ``machine_resolver.registry_get
("repos.<id>")`` is how every repo finds every other repo. It lives OUTSIDE the
working tree, so nothing in the repo's own hygiene (tmp_path, git status, a dirty
tree gate) notices when a test writes into it.

On 2026-07-28 a run of ``coordinator/tests/test_new_project_scaffold.py`` wrote
five entries into the live registry — ``repos.myapp``, ``repos.branchapp``,
``repos.myproject``, ``repos.flagapp``, ``repos.emptyapp`` — each pointing at a
``pytest-of-*/pytest-NNNN/...`` temp directory that no longer exists. The tests
themselves were disciplined about ``tmp_path``; the escape was one layer down.
``coordinator_core.ops.new_project_scaffold._register_repo`` self-registers every
scaffolded project by spawning ``machine-local set repos.<slug> <abs-path>``, and
that child process inherits the parent's environment. With no settings-home or
registry-dir override in that environment, ``machine-local`` resolved the REAL
``$HOME/.coordinator-claude-settings/machine-local`` and wrote there. The three
tests in that module that DID override ``HOME`` for the child left the live
registry untouched; the five that did not are exactly the five that polluted it.

``coordinator_core/conftest.py``'s ``_quarantine_real_home`` covers the
``coordinator_core/`` tree only. The ``coordinator/tests/`` and ``coordinator/
bin/`` trees — adopted into ``testpaths`` on 2026-07-22/25 — inherited no such
backstop, which is why the escape was available there and nowhere else.

THE TWO LAYERS
--------------
:func:`sandbox_registry_dir` (prevention) points ``MACHINE_LOCAL_REGISTRY_DIR``
— the registry ladder's rung-1 override, honoured identically by the
``machine-local`` CLI and by ``machine_resolver.registry_dir()`` — at a per-test
directory SEEDED WITH A COPY of the live registry files. The copy matters: these
tests legitimately READ the registry (the scaffold resolves ``repos.example_doctrine_repo``
through it), so a bare empty directory would convert a pollution bug into a
resolution failure. Reads see identical bytes; writes land in the copy and are
discarded with the tmp dir.

:func:`assert_live_registry_unchanged` (detection) is the backstop for whatever
prevention misses — a test that sets its own ``COORDINATOR_SETTINGS_HOME`` back
at the real home, a code path that writes the file directly, a future override
rung nobody here anticipated. It compares the live ``registry.local.toml`` bytes
against a snapshot, RESTORES them, and fails the test by name. Restoring is
deliberate: a tripwire that leaves the damage in place has only relocated the
incident.

NEGATIVE SPEC
    - Does NOT resolve the live registry dir through
      ``machine_resolver.registry_dir()``. That function honours
      ``MACHINE_LOCAL_REGISTRY_DIR`` first, so once layer 1 is armed it would
      report the sandbox and the tripwire would guard the copy instead of the
      original. :func:`live_registry_dir` deliberately reads settings-home only.
    - Does NOT touch ``COORDINATOR_SETTINGS_HOME``. The ``machine-local``
      forwarder resolves its own implementation (``_machine_local.py``) under
      ``<settings-home>/bin/``, so redirecting settings-home at a tmp dir makes
      the CLI unresolvable and turns every registry call into a hard failure.
      ``MACHINE_LOCAL_REGISTRY_DIR`` separates the two concerns exactly.
    - Does NOT guard ``registry.toml`` (the tracked, in-repo-shaped file) for
      writes. It is not gitignored machine state and a stray write to it shows
      up in ``git status``; ``registry.local.toml`` is the one with no other
      witness.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

# Files the machine-local registry ladder reads, in precedence order. Both are
# copied into a sandbox so a sandboxed read is byte-identical to a live one.
REGISTRY_FILENAMES = ("registry.local.toml", "registry.toml")

# The one file a test must never modify: gitignored per-machine state whose only
# witness is this tripwire.
LIVE_REGISTRY_FILENAME = "registry.local.toml"


def live_registry_dir() -> Path:
    """Resolve the REAL machine-local registry directory, ignoring the
    ``MACHINE_LOCAL_REGISTRY_DIR`` test-isolation override.

    Mirrors ``_machine_local.py::_settings_home``'s ladder — the
    ``COORDINATOR_SETTINGS_HOME`` override first, else a
    ``.coordinator-claude-settings`` directory under the CLAUDE_HOME override
    or, absent that, the expanded user home — minus the rung-1 override, so
    callers can ask "what would an UNSANDBOXED process have written to" even
    from inside a sandbox.
    """
    settings_home = os.environ.get("COORDINATOR_SETTINGS_HOME")
    if not settings_home:
        home = (
            os.environ.get("CLAUDE_HOME")
            or os.environ.get("HOME")
            or os.environ.get("USERPROFILE")
            or os.path.expanduser("~")
        )
        settings_home = os.path.join(home, ".coordinator-claude-settings")
    return Path(settings_home) / "machine-local"


def snapshot_live_registry() -> Optional[bytes]:
    """Return the live ``registry.local.toml`` bytes, or ``None`` when absent.

    ``None`` is a meaningful state, not an error: a machine that has never run
    ``machine-local set`` has no local registry, and a test that CREATES one is
    exactly as much of a violation as a test that edits one.
    """
    path = live_registry_dir() / LIVE_REGISTRY_FILENAME
    try:
        return path.read_bytes()
    except (OSError, ValueError):
        return None


def sandbox_registry_dir(monkeypatch, dest) -> Path:
    """Point ``MACHINE_LOCAL_REGISTRY_DIR`` at ``dest``, seeded from the live registry.

    Returns ``dest`` as a ``Path``. Creates it if absent and copies each of
    :data:`REGISTRY_FILENAMES` that exists in the live registry dir, so registry
    READS behave identically inside the sandbox while every WRITE is contained.

    The override is set via ``monkeypatch`` so it is inherited by any subprocess
    the test spawns with the ambient environment — which is the whole point:
    the 2026-07-28 escape happened in a child process, not in-process.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    source = live_registry_dir()
    for name in REGISTRY_FILENAMES:
        src = source / name
        if src.is_file():
            shutil.copyfile(src, dest / name)

    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(dest))
    return dest


def assert_live_registry_unchanged(
    before: Optional[bytes], nodeid: str, before_identity: str
) -> None:
    """Restore and fail loudly when the live ``registry.local.toml`` changed.

    ``before`` is a :func:`snapshot_live_registry` result taken before the test
    ran. On a mismatch the original bytes are written back FIRST (a
    newly-created file is deleted), then the caller's test is failed with a
    message naming it and the keys it added — the report has to point at the
    cause, not at whichever unrelated test later fails to resolve a phantom
    repo.

    ``before_identity`` is ``str(live_registry_dir())`` captured at setup
    time, alongside ``before``. Review finding 2 (2026-07-28): comparing only
    the bytes is unsound if ``COORDINATOR_SETTINGS_HOME`` itself changes
    between setup and teardown (e.g. a test that mutates it via raw
    ``os.environ[...] = ...`` and fails to restore it) — ``before`` and
    ``after`` would then be snapshots of two DIFFERENT files: a real
    pollution at the original location would go undetected (``after`` never
    looks there again), and restoring ``before``'s bytes would write them to
    an unrelated, wrong location. Pinning the identity first turns that
    silent failure into a loud one that names the mutation instead of
    guessing at a bytes diff across two files.
    """
    after_identity = str(live_registry_dir())
    if after_identity != before_identity:
        import pytest

        pytest.fail(
            f"{nodeid} left COORDINATOR_SETTINGS_HOME mutated: the live "
            f"machine-local registry directory moved from {before_identity!r} "
            f"to {after_identity!r} between setup and teardown, so the "
            "live-registry identity check could not run — comparing bytes "
            "across two different files would be meaningless and could "
            "restore the wrong one.\n\n"
            "Restore COORDINATOR_SETTINGS_HOME before this fixture's teardown "
            "runs — prefer monkeypatch.setenv (which always undoes) over raw "
            "os.environ assignment.",
            pytrace=False,
        )

    after = snapshot_live_registry()
    if after == before:
        return

    path = live_registry_dir() / LIVE_REGISTRY_FILENAME
    try:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(before)
        restored = "The live registry has been restored to its pre-test contents."
    except OSError as exc:  # pragma: no cover — defensive
        restored = (
            f"RESTORE FAILED ({exc}) — the live registry is still modified and "
            "needs manual repair."
        )

    added = _added_lines(before, after)
    added_detail = ("\n  added:\n" + "\n".join(f"    {line}" for line in added)) if added else ""

    import pytest

    pytest.fail(
        f"{nodeid} modified the LIVE machine-local registry at {path}."
        f"{added_detail}\n\n"
        "That file is the fleet's cross-repo resolution substrate — an entry "
        "pointing at a pytest tmp dir becomes a permanent phantom receiver the "
        "moment the tmp dir is reaped, and a corrupted one breaks resolution "
        "for every repo on the machine.\n"
        f"{restored}\n\n"
        "Fix the WRITE, not this guard: point MACHINE_LOCAL_REGISTRY_DIR at a "
        "tmp dir for the test AND for every subprocess it spawns (see "
        "coordinator_core.testing.registry_sandbox.sandbox_registry_dir), or "
        "stub the registry-writing seam.",
        pytrace=False,
    )


def fail_on_live_registry_write_fixture(request):
    """Shared body for the tree-wide autouse live-registry-write tripwire.

    ``coordinator/tests/conftest.py`` and ``coordinator/bin/conftest.py`` each
    wire this the same way: ``_fail_on_live_registry_write =
    pytest.fixture(autouse=True)(fail_on_live_registry_write_fixture)``. This
    used to be a byte-identical copy in each conftest (Review: code-reviewer,
    Finding 3, 2026-07-28) — a future edit to one copy (an opt-out marker, a
    changed message) silently reintroducing asymmetric coverage between the
    two trees. There is exactly one implementation now.

    FAILs any test that modifies the live ``registry.local.toml`` — and undoes
    it. No opt-out marker by design: writing live machine config from a test
    is never correct. The prevention half is
    :func:`sandbox_registry_dir`, applied per-test by whoever spawns a
    registry writer.
    """
    before = snapshot_live_registry()
    before_identity = str(live_registry_dir())
    yield
    assert_live_registry_unchanged(before, request.node.nodeid, before_identity)


def _added_lines(before: Optional[bytes], after: Optional[bytes]) -> list[str]:
    """Lines present in ``after`` but not ``before`` — a cheap, dependency-free
    stand-in for a diff, sufficient because registry writes are line-appends of
    flat ``"key" = 'value'`` entries."""
    if after is None:
        return []
    before_lines = set((before or b"").decode("utf-8", "replace").splitlines())
    return [
        line
        for line in after.decode("utf-8", "replace").splitlines()
        if line.strip() and line not in before_lines
    ]
