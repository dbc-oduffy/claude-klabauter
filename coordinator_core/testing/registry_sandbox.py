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
tests legitimately READ the registry (the scaffold resolves ``repos.doe_claude``
through it), so a bare empty directory would convert a pollution bug into a
resolution failure. Reads see identical bytes; writes land in the copy and are
discarded with the tmp dir.

:func:`assert_live_registry_unchanged` (detection) is the backstop for whatever
prevention misses — a test that sets its own ``COORDINATOR_SETTINGS_HOME`` back
at the real home, a code path that writes the file directly, a future override
rung nobody here anticipated. It compares the live ``registry.local.toml`` bytes
against a snapshot and ATTRIBUTES THE CHANGE BY CONTENT, never by the fact that
it landed inside some test's window.

THE ATTRIBUTION RULE, stated exactly
------------------------------------
FAIL if and only if an ADDED line carries a ``pytest-of-*/pytest-*/`` path.
Every other shape is EXTERNAL — warn, leave the file alone, do not fail:
a removal, an in-place rewrite, a plain repo path added, a pytest path appearing
only among the REMOVED lines (a peer cleaning up an old phantom entry is exactly
that shape, and must not fail whoever is running at the time).

A test cannot plausibly delete a peer's key, so NO removal shape ever fails a
test. That falls out of reading only ``added`` — but it is meant, not incidental:
:func:`_classify_change` states it in one place so it cannot be lost to a later
edit that "tidies up" the asymmetry between the two sides of the diff.

The diff itself is SYMMETRIC (:func:`_describe_change`): both sides are reported
even though only one side can fail a test. Attribution and evidence are separate
jobs — narrowing the evidence to the side that can fail is what made the
2026-08-20 incident undiagnosable.

WHY THIS NEVER RESTORES (2026-08-20)
------------------------------------
It used to write the pre-test bytes back, on the reasoning that "a tripwire
which leaves the damage in place has only relocated the incident". That
reasoning holds only when the tripwire has correctly established causation, and
this one cannot: it compares bytes across a window, and a byte comparison across
a window proves coincidence, never authorship. This repo's own ``CLAUDE.md``
puts 50–70 concurrent LLM sessions on this machine as the AVERAGE — so an
unrelated write landing inside some test's window is an ordinary event, not a
freak one.

The concrete incident: on 2026-08-20 a run of the cruft-sweep suite failed
``test_orphans_hard_exclude_docs_name``, a test that spawns a read-only sweep
and provably writes no machine state. A peer session REMOVED one registry key
mid-window — 4066 bytes / 63 lines down to 3911 / 62. The tripwire restored the
file, resurrecting a key that session had deliberately deleted, and for about a
minute this box's fleet-wide resolution substrate carried an entry a peer had
removed on purpose. It self-healed only because that peer happened to re-apply
its removal at 11:03:25. Had it been a one-shot cleanup pass, the deletion would
have been undone permanently, witnessed by nothing but a confusing error in an
unrelated repo's scrollback. The destroyed key was never recovered: the only
record of what it had been was the failure message, lost to a truncated capture,
and the peer was never told.

Two properties of the old code made that both possible and undiagnosable:
  - It mutated a file it did not own, on evidence that could not distinguish its
    own tests from the rest of the box. A read-modify-write against a file 50–70
    sessions are touching races them even when the attribution is right.
  - Its diff was ADD-ONLY (``_added_lines``), so a removal — precisely what
    happened — rendered an empty evidence block. The operator got a test name
    that was wrong and nothing to check it against.

NOTHING LOAD-BEARING WAS TRADED AWAY. Removing the restore is not "less
containment in exchange for not destroying peer state": the containment was
never real. It rested on the guard knowing the running test caused the change,
which it cannot know, and which in the reproduction above it got wrong in the
most damaging available direction — undoing a deliberate deletion. What survives
is the containment the guard can actually earn: an added pytest temp path, which
still fails loudly with the remediation intact. Do not reinstate restoring as an
obvious improvement; the 2026-07-28 entries this module exists for all pointed at
``pytest-of-*/pytest-NNNN/...``, which :data:`_PYTEST_TMP_PATH_RE` still catches.

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
    - Does NOT write to the live registry under any circumstance — see "WHY
      THIS NEVER RESTORES" above. Detection only.
    - Does NOT make sandboxing the default for every test that touches the
      registry. That is the stronger prevention and it needs its own plan; the
      per-test opt-in via :func:`sandbox_registry_dir` is what exists today.
"""

from __future__ import annotations

import os
import re
import shutil
import warnings
from pathlib import Path
from typing import List, Optional

# Files the machine-local registry ladder reads, in precedence order. Both are
# copied into a sandbox so a sandboxed read is byte-identical to a live one.
REGISTRY_FILENAMES = ("registry.local.toml", "registry.toml")

# The one file a test must never modify: gitignored per-machine state whose only
# witness is this tripwire.
LIVE_REGISTRY_FILENAME = "registry.local.toml"

#: A value written from inside a pytest temporary directory — the one content
#: signature that identifies a TEST as the author of a registry write, because
#: no process outside a pytest run has such a path to write.
#: ``pytest-of-<user>/pytest-<n>/`` is ``tmp_path_factory``'s basetemp shape; the
#: bare ``pytest-of-`` prefix is matched on its own so a differently-nested or
#: ``--basetemp``-relocated temp dir still classifies correctly.
_PYTEST_TMP_PATH_RE = re.compile(r"pytest-of-|[\\/]pytest-\d+[\\/]", re.IGNORECASE)


class ForeignRegistryWriteWarning(UserWarning):
    """A concurrent session wrote the live registry during a test's window.

    Not a defect in the test that happens to be running — see the module
    docstring's "WHY THIS NEVER RESTORES". Its own category so a caller can
    filter or collect it; never raised, never escalated here.
    """


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
    """Report — never repair — a change to the live ``registry.local.toml``.

    ``before`` is a :func:`snapshot_live_registry` result taken before the test
    ran. On a mismatch the change is attributed BY CONTENT, not by the fact that
    it landed inside this test's window:

      - an ADDED line carrying a pytest temp path (:data:`_PYTEST_TMP_PATH_RE`)
        could only have been written by a test → fail ``nodeid``, naming the
        added and removed lines and pointing at :func:`sandbox_registry_dir`.
        This is the 2026-07-28 incident class, detected exactly as before.
      - every other shape — a removal, an in-place rewrite, a plain repo path
        added, a pytest path only among the REMOVED lines → one
        :class:`ForeignRegistryWriteWarning`. A concurrent session wrote it; the
        running test is not the author, and failing it sends whoever reads the
        report after the wrong thing.

    See :func:`_classify_change` for the rule and why removals can never fail.
    Both sides of the diff are REPORTED either way (:func:`_describe_change`) —
    evidence is a separate job from attribution.

    Writes nothing back in either branch. The module docstring's "WHY THIS NEVER
    RESTORES" carries the incident that removed the write-back; the short version
    is that a byte comparison across a window cannot establish authorship, and
    mutating a fleet-shared file on evidence that weak destroyed a peer's key.

    ``before_identity`` is ``str(live_registry_dir())`` captured at setup
    time, alongside ``before``. Review finding 2 (2026-07-28): comparing only
    the bytes is unsound if ``COORDINATOR_SETTINGS_HOME`` itself changes
    between setup and teardown (e.g. a test that mutates it via raw
    ``os.environ[...] = ...`` and fails to restore it) — ``before`` and
    ``after`` would then be snapshots of two DIFFERENT files, so a real
    pollution at the original location would go undetected (``after`` never
    looks there again). Pinning the identity first turns that silent failure
    into a loud one that names the mutation instead of guessing at a bytes diff
    across two files. That branch remains a FAIL: an in-process mutation of
    ``COORDINATOR_SETTINGS_HOME`` is attributable to the running test by
    construction, unlike a write to the file itself.
    """
    after_identity = str(live_registry_dir())
    if after_identity != before_identity:
        import pytest

        pytest.fail(
            f"{nodeid} left COORDINATOR_SETTINGS_HOME mutated: the live "
            f"machine-local registry directory moved from {before_identity!r} "
            f"to {after_identity!r} between setup and teardown, so the "
            "live-registry identity check could not run — comparing bytes "
            "across two different files would be meaningless.\n\n"
            "Restore COORDINATOR_SETTINGS_HOME before this fixture's teardown "
            "runs — prefer monkeypatch.setenv (which always undoes) over raw "
            "os.environ assignment.",
            pytrace=False,
        )

    after = snapshot_live_registry()
    if after == before:
        return

    path = live_registry_dir() / LIVE_REGISTRY_FILENAME
    added = _added_lines(before, after)
    removed = _removed_lines(before, after)
    detail = _describe_change(added, removed)

    if not _classify_change(added, removed):
        warnings.warn(
            f"{nodeid}: the live machine-local registry at {path} changed during "
            f"this test's window, by a write carrying no pytest temp path.{detail}\n"
            "A concurrent session on this box wrote it; the write is LEFT INTACT "
            "and this test is not at fault.",
            ForeignRegistryWriteWarning,
            stacklevel=2,
        )
        return

    import pytest

    pytest.fail(
        f"{nodeid} wrote the LIVE machine-local registry at {path} — the added "
        f"value names a pytest temp dir, which no other process on this box "
        f"could have written.{detail}\n\n"
        "That entry becomes a phantom receiver in the fleet's cross-repo "
        "resolution substrate the moment the temp dir is reaped. It has NOT "
        "been reverted — repair it by hand if it matters.\n\n"
        "Fix the WRITE: point MACHINE_LOCAL_REGISTRY_DIR at a tmp dir for the "
        "test AND for every subprocess it spawns (see "
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

    FAILs any test whose own write reached the live ``registry.local.toml``,
    identified by a pytest temp path in the added lines; warns and stands aside
    when the change came from one of the box's other sessions. Never writes the
    file back — see :func:`assert_live_registry_unchanged` and the module
    docstring's "WHY THIS NEVER RESTORES". No opt-out marker on the fail branch
    by design: writing live machine config from a test is never correct. The
    prevention half is :func:`sandbox_registry_dir`, applied per-test by whoever
    spawns a registry writer.
    """
    before = snapshot_live_registry()
    before_identity = str(live_registry_dir())
    yield
    assert_live_registry_unchanged(before, request.node.nodeid, before_identity)


def _lines(blob: Optional[bytes]) -> List[str]:
    """Non-blank lines of a registry snapshot; ``[]`` for an absent file."""
    return [
        line
        for line in (blob or b"").decode("utf-8", "replace").splitlines()
        if line.strip()
    ]


def _added_lines(before: Optional[bytes], after: Optional[bytes]) -> List[str]:
    """Lines present in ``after`` but not ``before`` — a cheap, dependency-free
    stand-in for a diff, sufficient because registry writes are line-appends of
    flat ``"key" = 'value'`` entries."""
    before_set = set(_lines(before))
    return [line for line in _lines(after) if line not in before_set]


def _removed_lines(before: Optional[bytes], after: Optional[bytes]) -> List[str]:
    """Lines present in ``before`` but not ``after``.

    The symmetric half of :func:`_added_lines`, and not a symmetry nicety: a
    removal was the change class that went undiagnosable on 2026-08-20, because
    an add-only diff renders an empty evidence block for it (module docstring,
    "WHY THIS NEVER RESTORES").
    """
    after_set = set(_lines(after))
    return [line for line in _lines(before) if line not in after_set]


def _describe_change(added: List[str], removed: List[str]) -> str:
    """Indented ``added:``/``removed:`` blocks for whichever side is non-empty."""
    blocks = []
    for label, group in (("added", added), ("removed", removed)):
        if group:
            body = "\n".join(f"    {line}" for line in group)
            blocks.append(f"  {label}:\n{body}")
    return ("\n" + "\n".join(blocks)) if blocks else ""


def _classify_change(added: List[str], removed: List[str]) -> List[str]:
    """The added lines proving the running test authored this change; ``[]`` if none.

    THE RULE, in the one place it lives: a change is test-authored if and only if
    an ADDED line carries a pytest temp path (:data:`_PYTEST_TMP_PATH_RE`).

    ``removed`` is accepted and deliberately NOT consulted. Two reasons, both
    load-bearing enough that the parameter is kept to carry them:

      - A test cannot plausibly delete a peer's registry key, so no removal shape
        may ever fail a test. On 2026-08-20 one did.
      - A pytest path among the REMOVED lines is a peer cleaning up an old
        phantom entry — the 2026-07-28 residue, exactly the thing this module
        wants gone. Failing the unrelated test that happened to be running for
        it would punish the cleanup.

    Attribution is by CONTENT, never by the time window: the window proves only
    that two events overlapped, which on a box averaging 50–70 concurrent
    sessions is an ordinary coincidence.
    """
    return [line for line in added if _PYTEST_TMP_PATH_RE.search(line)]
