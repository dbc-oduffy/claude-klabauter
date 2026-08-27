"""
coordinator_core.install.test_resolve_claude_klabauter — direct unit coverage of
``coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py``'s reject paths.

This is the module the forwarder-ladder extraction (M1) pulled the
resolve-claude-klabauter-bin ladder into. Before the extraction, these reject paths
were only reachable via a full subprocess-exec of an emitted forwarder
(see test_forwarder_trust_guard.py, which still covers the end-to-end
forwarder+module integration). Now that the ladder lives in its own file,
its reject paths are unit-testable directly against the pure functions —
no subprocess, no forwarder emission required.

The module lives outside any Python package (installed standalone into a
bare bin/ directory, deliberately import-independent of coordinator_core —
see its own module docstring), so it is loaded here via
importlib.util.spec_from_file_location, mirroring
substrate.py's _load_setup_template_manifest loading convention for the
same reason (a hyphenated directory name precludes a normal ``import``).

Spec backlink: docs/plans/2026-07-23-... (M1)
"""
from __future__ import annotations

import importlib.util
import ast
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "coordinator" / "lib" / "resolve-claude-klabauter" / "_resolve_claude_klabauter.py"
)

_spec = importlib.util.spec_from_file_location("_resolve_claude_klabauter_under_test", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
resolve_claude_klabauter = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = resolve_claude_klabauter
_spec.loader.exec_module(resolve_claude_klabauter)


def _make_claude_klabauter_fixture(root: Path, sentinel_executable: bool = True) -> None:
    bin_dir = root / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    sentinel = bin_dir / "archive-stamp-cli"
    sentinel.write_text("#!/bin/sh\necho SENTINEL\n", encoding="utf-8")
    sentinel.chmod(0o755 if sentinel_executable else 0o644)
    if os.name == "nt" and sentinel_executable:
        # An extensionless shebang file with POSIX exec bits set is inert
        # on Windows -- `_resolve_claude_klabauter.py`'s own `_is_executable` probe
        # (mirroring `win_portability.is_executable`) only recognizes a
        # PATHEXT-suffixed sibling for an extensionless path.
        sentinel.with_name(sentinel.name + ".exe").write_bytes(b"")


def test_missing_both_registry_and_sentinel_raises_distinct_message(tmp_path: Path, monkeypatch):
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))

    with pytest.raises(resolve_claude_klabauter.ClaudeKlabauterResolutionError, match="cannot resolve claude-klabauter"):
        resolve_claude_klabauter.resolve_claude_klabauter_bin_dir()


def test_traversal_segment_rejected(tmp_path: Path, monkeypatch):
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    (ml_dir / ".claude-klabauter-live-root").write_text(str(tmp_path / "some" / ".." / "claude-klabauter"), encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))

    with pytest.raises(resolve_claude_klabauter.ClaudeKlabauterResolutionError, match="traversal segment"):
        resolve_claude_klabauter.resolve_claude_klabauter_bin_dir()


def test_root_does_not_exist_rejected(tmp_path: Path, monkeypatch):
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    (ml_dir / ".claude-klabauter-live-root").write_text(str(tmp_path / "nonexistent-claude-klabauter"), encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))

    with pytest.raises(resolve_claude_klabauter.ClaudeKlabauterResolutionError, match="does not exist"):
        resolve_claude_klabauter.resolve_claude_klabauter_bin_dir()


def test_root_resolved_but_coordinator_bin_missing_distinct_message(tmp_path: Path, monkeypatch):
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    incomplete_root = tmp_path / "incomplete-claude-klabauter"
    incomplete_root.mkdir()
    (ml_dir / ".claude-klabauter-live-root").write_text(str(incomplete_root), encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))

    with pytest.raises(resolve_claude_klabauter.ClaudeKlabauterResolutionError, match="has no coordinator/bin/ directory") as excinfo:
        resolve_claude_klabauter.resolve_claude_klabauter_bin_dir()
    assert "stale or partial" not in str(excinfo.value)


def test_coordinator_bin_present_but_sentinel_absent_distinct_message(tmp_path: Path, monkeypatch):
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    stale_root = tmp_path / "stale-claude-klabauter"
    (stale_root / "coordinator" / "bin").mkdir(parents=True)
    (ml_dir / ".claude-klabauter-live-root").write_text(str(stale_root), encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))

    with pytest.raises(resolve_claude_klabauter.ClaudeKlabauterResolutionError, match="stale or partial claude-klabauter migration") as excinfo:
        resolve_claude_klabauter.resolve_claude_klabauter_bin_dir()
    assert "has no coordinator/bin/ directory" not in str(excinfo.value)


def test_sentinel_appearing_mid_rename_resolves_instead_of_raising(tmp_path: Path, monkeypatch):
    """A sentinel absent on the FIRST probe but present on a re-probe resolves.

    Models the C6 rename window (grind-the-posix-exec-baseline-to-zero): the
    wave lands `archive-stamp-cli` -> `archive-stamp-cli.py` as delete-then-
    write, so for an instant neither shape is on disk. A single probe cannot
    tell that from a genuinely missing sentinel, and because archive-stamp-cli
    is the sole authorized frontmatter writer, raising there fails every
    concurrent session's handoff and memo stamping closed. Measured live
    2026-08-13 from a memo pickup taken mid-wave.
    """
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    root = tmp_path / "mid-rename-claude-klabauter"
    (root / "coordinator" / "bin").mkdir(parents=True)
    (ml_dir / ".claude-klabauter-live-root").write_text(str(root), encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))

    sentinel_py = root / "coordinator" / "bin" / "archive-stamp-cli.py"
    real_sleep = time.sleep

    def _write_sentinel_on_first_sleep(seconds):
        # The write lands during the first backoff — i.e. the file appears
        # between probe 1 and probe 2, exactly as the rename's second half does.
        if not sentinel_py.exists():
            sentinel_py.write_text("# archive-stamp-cli\n", encoding="utf-8")
        real_sleep(0)

    monkeypatch.setattr(resolve_claude_klabauter.time, "sleep", _write_sentinel_on_first_sleep)

    assert resolve_claude_klabauter.resolve_claude_klabauter_bin_dir().replace("\\", "/") == (
        root.as_posix() + "/coordinator/bin"
    )


def test_sentinel_absent_across_reprobe_still_raises(tmp_path: Path, monkeypatch):
    """The re-probe must not soften a genuinely absent sentinel into a pass.

    Negative-spec for the mid-rename retry above: a sentinel that stays absent
    across every backoff is a real stale/partial migration and must still fail
    loud, with the message naming the re-probe so the reader knows the miss was
    not a single unlucky read.
    """
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    root = tmp_path / "genuinely-absent-claude-klabauter"
    (root / "coordinator" / "bin").mkdir(parents=True)
    (ml_dir / ".claude-klabauter-live-root").write_text(str(root), encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))

    monkeypatch.setattr(resolve_claude_klabauter.time, "sleep", lambda seconds: None)

    with pytest.raises(
        resolve_claude_klabauter.ClaudeKlabauterResolutionError,
        match="stale or partial claude-klabauter migration",
    ) as excinfo:
        resolve_claude_klabauter.resolve_claude_klabauter_bin_dir()
    assert "re-probe" in str(excinfo.value)


def test_present_sentinel_never_pays_the_retry_sleep(tmp_path: Path, monkeypatch):
    """The happy path must not sleep — the retry is failure-path-only cost.

    On a box whose load norm is 50-70 concurrent LLMs, a resolver that slept on
    every successful resolution would be a real regression; the first probe has
    to short-circuit.
    """
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    root = tmp_path / "happy-path-claude-klabauter"
    _make_claude_klabauter_fixture(root)
    (ml_dir / ".claude-klabauter-live-root").write_text(str(root), encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))

    slept: list = []
    monkeypatch.setattr(resolve_claude_klabauter.time, "sleep", lambda seconds: slept.append(seconds))

    resolve_claude_klabauter.resolve_claude_klabauter_bin_dir()
    assert slept == []


def test_non_executable_sentinel_rejected(tmp_path: Path, monkeypatch):
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    root = tmp_path / "non-exec-sentinel-claude-klabauter"
    _make_claude_klabauter_fixture(root, sentinel_executable=False)
    (ml_dir / ".claude-klabauter-live-root").write_text(str(root), encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))

    with pytest.raises(resolve_claude_klabauter.ClaudeKlabauterResolutionError, match="stale or partial claude-klabauter migration"):
        resolve_claude_klabauter.resolve_claude_klabauter_bin_dir()


def test_valid_fixture_resolves_bin_dir(tmp_path: Path, monkeypatch):
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    root = tmp_path / "valid-claude-klabauter"
    _make_claude_klabauter_fixture(root)
    (ml_dir / ".claude-klabauter-live-root").write_text(str(root), encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))

    # `resolve_claude_klabauter_bin_dir` returns `claude_klabauter_root + "/coordinator/bin"`
    # where `claude_klabauter_root` is read back verbatim from `.claude-klabauter-live-root` --
    # i.e. it carries whatever separators `str(root)` used when the
    # fixture wrote that file (native, backslash on Windows), not a
    # normalized form. Compare backslash-insensitively rather than assume
    # either separator shape.
    assert resolve_claude_klabauter.resolve_claude_klabauter_bin_dir().replace("\\", "/") == root.as_posix() + "/coordinator/bin"


def test_registry_rung_wins_over_sentinel(tmp_path: Path, monkeypatch):
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    registry_root = tmp_path / "registry-claude-klabauter"
    _make_claude_klabauter_fixture(registry_root)
    (ml_dir / "registry.local.toml").write_text(
        f'"repos.claude_klabauter" = \'{registry_root}\'\n', encoding="utf-8"
    )
    (ml_dir / ".claude-klabauter-live-root").write_text(str(tmp_path / "sentinel-claude-klabauter-nonexistent"), encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))

    assert resolve_claude_klabauter.resolve_claude_klabauter_bin_dir().replace("\\", "/") == registry_root.as_posix() + "/coordinator/bin"


# ---------------------------------------------------------------------------
# _settings_home() HOME guard (2026-07-28). The Windows claude-doe.cmd -> `bash
# -c` launch chain is a non-login cmd-spawned env that can present with
# COORDINATOR_SETTINGS_HOME/CLAUDE_HOME/HOME all empty; the prior body then
# emitted a garbage path ("~/.coordinator-claude-settings"), whose shell
# equivalent ("$HOME/..." with empty $HOME) collapsed to
# "/.coordinator-claude-settings" — a Windows current-drive-root write (a stray
# 0-byte X:\.coordinator-claude-settings was created that way). The resolver must
# consult USERPROFILE and otherwise fail loud, never emit a junk path.
# ---------------------------------------------------------------------------
def test_settings_home_prefers_claude_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "sb"))
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    assert resolve_claude_klabauter._settings_home() == (tmp_path / "sb" / ".coordinator-claude-settings")


def test_settings_home_falls_back_to_userprofile_when_no_home(monkeypatch, tmp_path):
    # Bare cmd.exe: no HOME, but USERPROFILE is the Windows home analog.
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "winhome"))
    assert resolve_claude_klabauter._settings_home() == (tmp_path / "winhome" / ".coordinator-claude-settings")


def test_settings_home_fails_loud_when_home_unresolvable(monkeypatch):
    # All home signals gone AND '~' unexpandable -> fail loud, never a junk path.
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    for var in ("CLAUDE_HOME", "HOME", "USERPROFILE", "HOMEPATH", "HOMEDRIVE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(resolve_claude_klabauter.os.path, "expanduser", lambda p: p)
    with pytest.raises(resolve_claude_klabauter.ClaudeKlabauterResolutionError, match="cannot resolve the coordinator settings home"):
        resolve_claude_klabauter._settings_home()


def test_settings_home_empty_override_still_falls_through(monkeypatch, tmp_path):
    # An env that exports COORDINATOR_SETTINGS_HOME empty must not start failing —
    # empty override falls through to the home chain (unchanged behavior).
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", "")
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "sb"))
    assert resolve_claude_klabauter._settings_home() == (tmp_path / "sb" / ".coordinator-claude-settings")


# ---------------------------------------------------------------------------
# Standalone-shim import contract — the guard that would have caught the
# 2026-07-29 total bareword outage.
# ---------------------------------------------------------------------------

_STANDALONE_SHIMS = (
    Path(__file__).resolve().parents[2] / "coordinator" / "lib" / "resolve-claude-klabauter" / "_resolve_claude_klabauter.py",
    Path(__file__).resolve().parents[2] / "coordinator" / "lib" / "claude-home" / "_claude_home.py",
)


@pytest.mark.parametrize("shim_path", _STANDALONE_SHIMS, ids=lambda p: p.name)
def test_standalone_shim_imports_no_coordinator_core(shim_path: Path):
    """These files are installed into a bare ``<settings-home>/bin/`` beside
    the generated forwarders and run on ``#!/usr/bin/env python3`` with only
    the stdlib importable — their whole job is to LOCATE the claude-klabauter tree, so
    they cannot presuppose it is importable.

    Regression, 2026-07-29: `a141074a`'s 40-site ``os.access(X_OK)`` ->
    ``coordinator_core.win_portability.is_executable()`` sweep added a
    module-scope package import to ``_resolve_claude_klabauter.py``. Every one of the
    ~334 bareword CLIs on PATH then died with ``ModuleNotFoundError:
    coordinator_core`` before the ladder's first line — including
    ``~/.local/bin/claude-doe``, i.e. launching Claude Code itself. The
    prose contract was already in that module's docstring; nothing enforced
    it, so a mechanical sweep walked straight through.

    A lazy (function-scope) import is banned too, not just module-scope: it
    would make the sentinel probe demand a full importable checkout,
    conflating "coordinator/bin/ holds a launchable sentinel" with "this tree
    is an installed Python package"."""
    tree = ast.parse(shim_path.read_text(encoding="utf-8"), filename=str(shim_path))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if a.name.split(".")[0] == "coordinator_core"]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "coordinator_core":
                offenders.append(node.module)
    assert not offenders, (
        f"{shim_path.name} imports {offenders} — it is installed standalone into a bare "
        "bin/ directory where coordinator_core is NOT importable; this breaks every "
        "bareword CLI on PATH at once. Inline a stdlib-only equivalent instead."
    )


# ---------------------------------------------------------------------------
# Bootstrap-independence regression, real invocation shape (2026-07-29
# outage). test_standalone_shim_imports_no_coordinator_core above is a
# STATIC text check (an AST walk over the source) — a companion guard, not a
# substitute for this one. It cannot observe the property production
# actually depends on: that the file imports successfully standing alone,
# outside the coordinator_core package. The two pre-existing test files in
# this pair (test_resolve_claude_klabauter.py, test_resolve_claude_klabauter_exec_cli.py) both
# import the module under test IN-PROCESS, inside claude-klabauter's own pytest
# session — where coordinator_core is always importable no matter what the
# module does, because it is already resolvable via this checkout's cwd
# (verified: not a pip/editable install — see this test's own docstring
# below). They stayed 20/20 green THROUGHOUT the outage for exactly that
# reason. A subprocess is the only way to honestly exercise this: the
# running pytest process already has coordinator_core in sys.modules (this
# very file lives under the coordinator_core package), and CPython does not
# re-walk sys.path for an already-resolved module — there is no way to
# "un-import" it in-process. Only a fresh interpreter, launched with a
# controlled sys.path and no PYTHONPATH back-door, can honestly stand in for
# a settings-home forwarder's own import of this file.
# ---------------------------------------------------------------------------

_CHECK_IMPORT_SCRIPT = """\
import sys
import _resolve_claude_klabauter as mod

assert callable(mod.exec_cli), "exec_cli is not callable after import"
assert callable(mod.resolve_claude_klabauter_bin_dir), "resolve_claude_klabauter_bin_dir is not callable after import"
print("sys.path=" + repr(sys.path))
print("IMPORT_OK")
"""


def _run_standalone_import_probe(shim_source: Path, tmp_path: Path) -> "subprocess.CompletedProcess[str]":
    """Copy *shim_source* into a fresh, otherwise-empty ``bin/`` dir under
    *tmp_path* — mirroring the settings-home ``bin/`` layout the shim's own
    module docstring describes ("installed standalone into the operator's
    ``<settings-home>/bin/`` beside ~334 generated forwarders") — then spawn
    a subprocess that imports it by bare module name from inside that
    directory (a real forwarder's own ``sys.path`` contains only its
    ``bin/`` directory, via CPython's implicit "script's own directory"
    prepend; nothing here adds claude-klabauter's root).

    Env is an explicit whitelist dict, not ``env -i`` or selective
    ``del``/``monkeypatch.delenv`` on the inherited environment — ``env -i``
    has no Windows-shell equivalent and this must stay portable (P0 here:
    ``is_executable``, the very function this outage broke the import of, is
    a Windows-portability helper). ``PYTHONPATH``/``PYTHONHOME`` are the two
    variables that could smuggle ``coordinator_core``'s location back onto
    the child's ``sys.path``, so they are the ones deliberately withheld;
    nothing else from ``os.environ`` is forwarded. ``cwd`` is *tmp_path*
    itself — pytest's own per-test scratch tree, never under this checkout —
    so cwd-relative resolution (the actual reason ``coordinator_core``
    stays reachable in-process here; this is not a pip/editable install,
    confirmed by hand while authoring this test: ``python3 -m pip show
    coordinator_core`` reports "Package(s) not found" on this machine) has
    nothing to find either."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy(shim_source, bin_dir / shim_source.name)
    (bin_dir / "_check_import.py").write_text(_CHECK_IMPORT_SCRIPT, encoding="utf-8")

    env: dict = {}
    for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC"):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    # PYTHONPATH / PYTHONHOME deliberately absent from `env` above.

    return subprocess.run(
        [sys.executable, str(bin_dir / "_check_import.py")],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        **no_console_creationflags(),
    )


_RESOLVE_CLAUDE_KLABAUTER_SOURCE = (
    Path(__file__).resolve().parents[2] / "coordinator" / "lib" / "resolve-claude-klabauter" / "_resolve_claude_klabauter.py"
)


def test_resolve_claude_klabauter_shim_importable_standalone_via_subprocess(tmp_path: Path):
    """Regression, 2026-07-29 bootstrap-inversion outage: the module whose
    entire job is locating claude-klabauter had a module-scope
    ``from coordinator_core.win_portability import is_executable`` — so it
    could not import until AFTER it had already found the tree it exists to
    find. Every one of ~334 bareword CLIs on PATH died with
    ``ModuleNotFoundError: No module named 'coordinator_core'`` before the
    resolution ladder's first line, including ``~/.local/bin/claude-doe``,
    i.e. launching Claude Code itself. Fixed in commit ``a41aaaad`` (the
    import is now lazy, behind path resolution) — this test is the
    differential proof the fix has a durable regression guard: reverting the
    import to module scope reproduces the exact
    ``ModuleNotFoundError: No module named 'coordinator_core'`` this test
    asserts against, and restoring the fix makes it pass again (verified by
    hand while authoring this test; see the executor report this test
    shipped with for the verbatim before/after output)."""
    result = _run_standalone_import_probe(_RESOLVE_CLAUDE_KLABAUTER_SOURCE, tmp_path)
    assert result.returncode == 0, (
        "coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py failed to import "
        "standalone, outside the coordinator_core package — the real "
        "<settings-home>/bin/ forwarder invocation shape, and the exact "
        "2026-07-29 bootstrap-inversion outage shape. "
        f"returncode={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "IMPORT_OK" in result.stdout


def _resolve_installed_shim_path() -> Path:
    """Best-effort standalone (no coordinator_core import) mirror of
    ``_resolve_claude_klabauter._settings_home()``'s precedence, sufficient only to
    locate the on-disk artifact for this test's skipif guard — not a
    correctness assertion about settings-home resolution itself (that is
    covered by ``test_settings_home_*`` above, against the module under
    test directly)."""
    override = os.environ.get("COORDINATOR_SETTINGS_HOME")
    if override:
        return Path(override) / "bin" / "_resolve_claude_klabauter.py"
    home = os.environ.get("CLAUDE_HOME") or os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
    if not home:
        return Path()
    return Path(home) / ".coordinator-claude-settings" / "bin" / "_resolve_claude_klabauter.py"


_INSTALLED_SHIM_PATH = _resolve_installed_shim_path()


@pytest.mark.skipif(
    not (_INSTALLED_SHIM_PATH.name and _INSTALLED_SHIM_PATH.is_file()),
    reason=(
        "no installed <settings-home>/bin/_resolve_claude_klabauter.py found on this machine "
        "(no coordinator:install run, or COORDINATOR_SETTINGS_HOME/CLAUDE_HOME/HOME "
        "all unset) — the source-tree leg above still runs unconditionally"
    ),
)


# ---------------------------------------------------------------------------
# DR-132 two-tier ladder — `resolve_claude_klabauter_root_with_class()`. Mirrors
# DoE-claude `coordinator/hooks/scripts/_engine_root.py`'s
# `resolve_claude_klabauter_root_with_class()` step order; a conformance fixture
# (chunk C8) drives both implementations against the same registry-state
# cases, so drift here WILL be caught cross-repo.
# ---------------------------------------------------------------------------


def _make_published_engine_fixture(root: Path, stamped: bool = True) -> None:
    """A published engine root as ``_resolve_published_engine`` defines one.

    C5 (docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md) added the
    build stamp to that definition — "an engine root is a stamped build. No
    stamp, no engine." A directory shaped like an engine but carrying no
    ``coordinator_core/_engine_stamp`` is denied outright, so a fixture that
    writes only the directory is no longer a published engine and every
    divert case built on it silently collapses to the live-tree leg — which
    is what happened between C5 landing and this helper being updated.

    *stamped* exists so the denial itself is testable: pass ``False`` for the
    unstamped-root case (``test_unstamped_published_root_is_denied``), never
    to work around a failing assertion.
    """
    (root / "coordinator_core").mkdir(parents=True)
    if stamped:
        (root / "coordinator_core" / "_engine_stamp").write_text(
            "fixture-engine-stamp\n", encoding="utf-8"
        )


def test_live_tree_wins_when_session_is_the_source_tree(tmp_path: Path, monkeypatch):
    """2026-08-18 (C4): the discriminant is now structural — the session's
    OWN root must equal ``_resolve_claude_klabauter_root``'s resolved value, not
    membership in a registered ``engine.working_repos.*`` set (retired)."""
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    live_root = tmp_path / "live-claude-klabauter"
    _make_claude_klabauter_fixture(live_root)
    (ml_dir / ".claude-klabauter-live-root").write_text(str(live_root), encoding="utf-8")

    published_root = tmp_path / "published-klabauter"
    _make_published_engine_fixture(published_root)

    (ml_dir / "registry.local.toml").write_text(
        f'"repos.claude_klabauter" = \'{published_root}\'\n', encoding="utf-8"
    )

    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))
    # The session's own root IS the resolved live tree.
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(live_root))

    root, cls = resolve_claude_klabauter.resolve_claude_klabauter_root_with_class()
    assert root == str(live_root)
    assert cls == resolve_claude_klabauter.RESOLUTION_LIVE_WORKING_TREE


def test_published_wins_when_session_is_not_the_source_tree(tmp_path: Path, monkeypatch):
    """2026-08-18 (C4): a session whose OWN root differs from the resolved
    live tree diverts to the published engine — no ``engine.working_repos.*``
    entry can rescue it any more (that key is a pure locator elsewhere now,
    not this gate's input). ``engine.target`` must ALSO be readable (AC20,
    ruling correction) — written here so this test isolates the structural
    discriminant specifically; the absent-target case has its own test
    above (``test_absent_engine_target_does_not_divert_even_when_session_confirmed_different``)."""
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    live_root = tmp_path / "live-claude-klabauter"
    _make_claude_klabauter_fixture(live_root)
    (ml_dir / ".claude-klabauter-live-root").write_text(str(live_root), encoding="utf-8")

    published_root = tmp_path / "published-klabauter"
    _make_published_engine_fixture(published_root)

    session_root = tmp_path / "session-repo"
    session_root.mkdir()

    (ml_dir / "registry.local.toml").write_text(
        f'"repos.claude_klabauter" = \'{published_root}\'\n'
        '"engine.target" = \'candidate\'\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(session_root))

    # Live tree is resolvable too, but step 1 of the ladder fires ahead of
    # it once the gate concretely returns False — published wins even
    # though the live tree would otherwise have resolved fine.
    root, cls = resolve_claude_klabauter.resolve_claude_klabauter_root_with_class()
    assert root == str(published_root)
    assert cls == resolve_claude_klabauter.RESOLUTION_RESOLVED_ENGINE


def test_structural_gate_none_does_not_divert(tmp_path: Path, monkeypatch):
    """`engine.target` IS written here -- isolates the structural gate's
    OWN undeterminable case from the separate absent-target case below."""
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    live_root = tmp_path / "live-claude-klabauter"
    _make_claude_klabauter_fixture(live_root)
    (ml_dir / ".claude-klabauter-live-root").write_text(str(live_root), encoding="utf-8")

    published_root = tmp_path / "published-klabauter"
    _make_published_engine_fixture(published_root)

    (ml_dir / "registry.local.toml").write_text(
        f'"repos.claude_klabauter" = \'{published_root}\'\n'
        '"engine.target" = \'candidate\'\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    # No determinable session root (no CLAUDE_PROJECT_DIR, and cwd has no
    # `.git` in its ancestry) -> the structural gate is undeterminable
    # (None), which MUST NOT divert away from an otherwise-resolvable live
    # tree, regardless of engine.target being readable.
    no_git_cwd = tmp_path / "no-git-cwd"
    no_git_cwd.mkdir()
    monkeypatch.chdir(no_git_cwd)

    root, cls = resolve_claude_klabauter.resolve_claude_klabauter_root_with_class()
    assert root == str(live_root)
    assert cls == resolve_claude_klabauter.RESOLUTION_LIVE_WORKING_TREE


def test_absent_engine_target_does_not_divert_even_when_session_confirmed_different(
    tmp_path: Path, monkeypatch
):
    """AC20 (ruling correction, 2026-08-18): a box with `repos.claude_klabauter`
    registered but `engine.target` never written -- every machine installed
    before C8 -- MUST NOT divert, even when the structural gate is a
    CONFIRMED `False` (a session that is definitely not the live tree).
    Absence means "not yet rolled out", never a silent opt-in."""
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    live_root = tmp_path / "live-claude-klabauter"
    _make_claude_klabauter_fixture(live_root)
    (ml_dir / ".claude-klabauter-live-root").write_text(str(live_root), encoding="utf-8")

    published_root = tmp_path / "published-klabauter"
    _make_published_engine_fixture(published_root)

    session_root = tmp_path / "session-repo"
    session_root.mkdir()

    # No "engine.target" key anywhere -- the pre-C8 / not-yet-rolled-out state.
    (ml_dir / "registry.local.toml").write_text(
        f'"repos.claude_klabauter" = \'{published_root}\'\n', encoding="utf-8"
    )

    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(session_root))

    assert resolve_claude_klabauter.resolve_engine_target(ml_dir) is None
    assert resolve_claude_klabauter._is_claude_klabauter_source_tree(ml_dir) is False

    root, cls = resolve_claude_klabauter.resolve_claude_klabauter_root_with_class()
    assert root == str(live_root)
    assert cls == resolve_claude_klabauter.RESOLUTION_LIVE_WORKING_TREE


def test_no_klabauter_byte_identical_to_today(tmp_path: Path, monkeypatch):
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    live_root = tmp_path / "live-claude-klabauter"
    _make_claude_klabauter_fixture(live_root)
    (ml_dir / ".claude-klabauter-live-root").write_text(str(live_root), encoding="utf-8")

    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))

    root, cls = resolve_claude_klabauter.resolve_claude_klabauter_root_with_class()
    assert root == str(live_root)
    assert cls == resolve_claude_klabauter.RESOLUTION_LIVE_WORKING_TREE
    assert resolve_claude_klabauter.resolve_claude_klabauter_bin_dir() == root + "/coordinator/bin"


def test_both_unresolvable_raises(tmp_path: Path, monkeypatch):
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))

    with pytest.raises(resolve_claude_klabauter.ClaudeKlabauterResolutionError, match="cannot resolve claude-klabauter"):
        resolve_claude_klabauter.resolve_claude_klabauter_root_with_class()


def test_klabauter_only_resolves_published_engine(tmp_path: Path, monkeypatch):
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    published_root = tmp_path / "published-klabauter"
    _make_published_engine_fixture(published_root)

    (ml_dir / "registry.local.toml").write_text(
        f'"repos.claude_klabauter" = \'{published_root}\'\n', encoding="utf-8"
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))

    # No repos.claude_klabauter, no .claude-klabauter-live-root sentinel -- the
    # klabauter-only consumer-machine case.
    root, cls = resolve_claude_klabauter.resolve_claude_klabauter_root_with_class()
    assert root == str(published_root)
    assert cls == resolve_claude_klabauter.RESOLUTION_RESOLVED_ENGINE


def test_unstamped_published_root_is_denied(tmp_path: Path, monkeypatch):
    """C5's fail-closed rule, previously unguarded in this file: a published
    root that is shaped like an engine but carries no build stamp is not
    "usable" at all, so it can neither win the divert nor answer the
    klabauter-only case.

    The falsifier for `_make_published_engine_fixture`'s stamp write too — a
    helper that stamped nothing would make every divert case below pass for
    the wrong reason (live tree answering because the published rung was
    denied), which is exactly the failure this pair of tests was added to
    end. Here the live tree is deliberately absent, so a denial has nowhere
    to silently fall through to and surfaces as the raise."""
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    published_root = tmp_path / "published-klabauter"
    _make_published_engine_fixture(published_root, stamped=False)

    (ml_dir / "registry.local.toml").write_text(
        f'"repos.claude_klabauter" = \'{published_root}\'\n', encoding="utf-8"
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))

    assert resolve_claude_klabauter._resolve_published_engine(ml_dir) is None
    with pytest.raises(resolve_claude_klabauter.ClaudeKlabauterResolutionError, match="cannot resolve claude-klabauter"):
        resolve_claude_klabauter.resolve_claude_klabauter_root_with_class()


def test_installed_settings_home_shim_importable_standalone_via_subprocess(tmp_path: Path):
    """Same bootstrap-independence property as the source-tree test above,
    but against the artifact production actually runs: the copy installed
    at ``<settings-home>/bin/_resolve_claude_klabauter.py``, not the
    ``coordinator/lib/`` generator source it was installed FROM. Local green
    on the generator source is not clean-install green — an install that ran
    before the fix landed, or a broken re-install step, would leave a stale
    copy on disk that this repo's own tests would never see. skipif-guarded
    because the installed artifact may not exist on a fresh checkout with no
    prior ``coordinator:install`` run (e.g. CI)."""
    result = _run_standalone_import_probe(_INSTALLED_SHIM_PATH, tmp_path)
    assert result.returncode == 0, (
        f"installed shim at {_INSTALLED_SHIM_PATH} failed to import standalone — "
        f"returncode={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "IMPORT_OK" in result.stdout
