"""
coordinator_core.install.substrate — coordinator-setup Phase 3 mechanical work.

Lays down ``<settings-home>/machine-local/`` substrate, installs bin/
resolvers (machine-local + claude-home families), and runs Windows
PATH/AppX health checks. Invoked IN-PROCESS by
``coordinator_core.install.maximalist`` (Phase 3 Step 1) and
``coordinator_core.install.first_run`` (both COLD, one-shot human/CI-invoked
install ceremonies); also invoked as a subprocess/CLI entry via
``coordinator/lib/install-substrate.py`` (the DoE-claude trampoline behind
``coordinator/commands/install.md`` Phase 3). No SessionStart hook currently
invokes this module — confirmed against DoE-claude's ``coordinator/hooks/hooks.json``
2026-08-14, which registers no ``substrate``-referencing SessionStart entry.

MUST be run as a subprocess/CLI entry (``python3 -m coordinator_core.install.substrate``),
never imported for its side effects — this module has no sourcing analogue;
``run()`` is the single entry point and is idempotent + re-runnable.

Idempotent: re-runs preserve operator-customized files, emit notices instead
of overwriting. Fail-loud on missing templates (hard precondition for
downstream skills).

Dual-anchor resolution (b644d5a9's executable-surface relocation moved
``coordinator/lib/`` and ``coordinator/bin/`` out of DoE-claude and into
Claude-klabauter's own tree): DoE-side surfaces (``templates/``, ``whoami/``,
``schemas/``) still resolve off ``CLAUDE_PLUGIN_ROOT``; claude-klabauter-side surfaces
(``coordinator/lib/``, ``coordinator/bin/``) resolve off the claude-klabauter root via
``coordinator_core.claude_klabauter_root.coordinator_claude_klabauter_root()`` — never by
``__file__``-walking or a hardcoded sibling-repo name.

Env:
    CLAUDE_PLUGIN_ROOT — required; the coordinator plugin install root
        (DoE-side surfaces only — see dual-anchor note above).
    CLAUDE_KLABAUTER_ROOT        — optional; short-circuits claude-klabauter-root resolution
        (see ``coordinator_core.claude_klabauter_root`` for the full resolution chain).
    CLAUDE_HOME        — optional; $HOME substitute.
    COORDINATOR_NON_INTERACTIVE — optional; "1" suppresses the AppX stub
        deletion consent prompt.
    COORDINATOR_DISABLE_MACHINE_MUTATION — optional; "1" refuses every
        real-machine-state mutation this module makes — Windows user-PATH
        registry writes, the orphan agent-helper forwarder sweep, the
        manifest-driven bin prune, the legacy coordinator-whoami dir
        delete/replace, the legacy .coordinator-venv delete, the rc-block
        writes into $HOME profile files (write_path_entry_guard_blocks), and
        the fnm brew/curl install — regardless of the path involved. Set
        suite-wide by the test harness (coordinator_core/conftest.py); see
        `_refuse_machine_mutation`.
    CHECK_ONLY         — optional; "1" reports would-do, writes nothing
        (also accepted as CLI --check-only).

Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
    (T4a-g3b chunk).
Spec backlink: coordinator/commands/install.md § Phase 3.

Documented divergence from bash: the bash original derives CLAUDE_PLUGIN_ROOT
from its OWN on-disk location (BASH_SOURCE-relative) when the env var is
unset, because it lives co-located inside the coordinator plugin tree. This
module lives in a different repo (claude-klabauter) and has no such co-location — the
self-derivation branch is dropped; CLAUDE_PLUGIN_ROOT is a hard-required env
var here (fail loud if unset, matching the bash "Validate the resolved root"
guard's spirit, not its BASH_SOURCE mechanism).
"""

from __future__ import annotations

import argparse
import enum
import filecmp
import json
import ntpath
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import FrozenSet, List, Optional, Tuple

from coordinator_core import machine_resolver
from coordinator_core._settings_home import settings_home
from coordinator_core.launchable import resolve_launchable
from coordinator_core.locked_write import held_lock
from coordinator_core.win_portability import is_executable, no_console_creationflags
from coordinator_core.install._shared import (
    RequireHomeError,
    atomic_write_bytes,
    is_pointer,
    require_home,
)
from coordinator_core.install import resolution_journal
from coordinator_core.install.shell_rc_guard import write_path_entry_guard_blocks
from coordinator_core.install.substrate_migrate import migrate_substrate_to_settings_home
from coordinator_core.install.write_surface import (
    ShapedClause,
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)
from coordinator_core.install.policy_gate import PolicyGateVerdict, evaluate_policy_gate
from coordinator_core.claude_klabauter_root import coordinator_claude_klabauter_root_with_class

# Generator-provenance declaration (generator_provenance.py). Every write
# (dst.write_text, _write_bin_manifest, the policy-gate report) targets
# <settings-home>/machine-local/ and its bin/ directory per this module's own
# docstring ("Lays down <settings-home>/machine-local/ substrate") --
# settings-home is outside the claude-klabauter repo tree.
GENERATES = []

_NO_CONSOLE = no_console_creationflags()


class SubstrateFatalError(RuntimeError):
    """Mirrors a bash `exit 1` FATAL precondition failure."""


def _is_windows_shell() -> bool:
    return (
        os.environ.get("OSTYPE") in ("msys", "cygwin")
        or os.environ.get("OS") == "Windows_NT"
    )


# Review: code-reviewer (Finding 8) — also used by substrate_migrate.py (deferred
# import of this module) as its own platform helpers; keep signatures stable.
def _run(argv, **kwargs) -> subprocess.CompletedProcess:
    """Run a child process, suppressing the Windows console window.

    Windows trap: CREATE_NO_WINDOW detaches the child from the console, but when
    stdio is INHERITED rather than redirected the child is left with console
    handles it can no longer use — it dies immediately with a non-zero rc and no
    output. That silently broke the Step 3h hardware audit (the only symptom was
    "[setup] WARNING: hardware audit failed", whose advice to re-run could never
    help), and would equally break any other non-capturing call here.

    So when the caller does NOT redirect stdio, capture it and forward it on to
    our own streams: the child gets real pipes (so it runs), the operator still
    sees its output, and the no-window flag keeps working.
    """
    kwargs.setdefault("timeout", 60)
    redirected = any(k in kwargs for k in ("capture_output", "stdout", "stderr"))
    if not _NO_CONSOLE or redirected:
        return subprocess.run(argv, **_NO_CONSOLE, **kwargs)

    kwargs["capture_output"] = True
    kwargs.setdefault("text", True)
    proc = subprocess.run(argv, **_NO_CONSOLE, **kwargs)
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return proc


def _quiet_output(argv, env=None) -> str:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, env=env, timeout=15, **_NO_CONSOLE
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"install-substrate: {argv[0] if argv else '<empty argv>'} failed: {exc}", file=sys.stderr)
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _cygpath_w(posix_path: str) -> str:
    return _quiet_output(["cygpath", "-w", posix_path])


_MACHINE_MUTATION_DISABLE_ENV = "COORDINATOR_DISABLE_MACHINE_MUTATION"


def _refuse_machine_mutation(
    path_being_written: str, *, what: str, check_temp_path: bool = True,
) -> Optional[str]:
    """Guard in front of every real-machine-state mutation this module makes
    (Windows user-PATH registry writes via ``[Environment]::
    SetEnvironmentVariable``, the orphan AppX stub delete, and the
    ``fnm``/brew-or-curl third-party installer). Returns a non-empty reason
    string when the mutation must be refused; ``None`` when it is safe to
    proceed.

    Two independent triggers, either one refuses:

    1. ``COORDINATOR_DISABLE_MACHINE_MUTATION=1`` — a belt-and-braces
       opt-out with no path heuristic at all, set suite-wide by the test
       harness (see ``coordinator_core/conftest.py::_quarantine_real_home``)
       so this whole class of mutation is closed for every test, not just
       ones that happen to route through a temp-rooted path. Applies to
       EVERY call site regardless of ``check_temp_path`` — this is the
       operator-facing switch, unconditional.
    2. ``path_being_written`` resolves under the OS temp dir
       (``tempfile.gettempdir()``) — the shape of a pytest ``tmp_path``
       fixture, never a genuine install location (a real install's
       settings-home bin dir or claude-CLI dir lives under the operator's
       profile, not system temp). Gated behind ``check_temp_path`` (see
       below).

    ``check_temp_path`` (default ``True``) — the discriminator is "can the
    test sandbox redirect this mutation?", never "is it dangerous?". Trigger
    2 exists for mutations the sandbox CANNOT redirect: the Windows registry
    PATH write and the AppX stub delete land on the real machine regardless
    of what ``tmp_path`` points at (the 2026-07-28 incident this guard was
    built for — a pytest tmpdir fixture path was written into a real
    operator's ``HKCU\\Environment`` PATH), and a real ``brew``/curl-piped
    installer must never run from a test no matter what path is passed.
    Plain FILESYSTEM-path mutations (a file/dir unlink, an rc-block write
    into a profile file) are the opposite case: the sandbox DOES correctly
    redirect them via ``tmp_path`` — that IS the mechanism the coverage-
    extension test suite for the delete/rc-block sites relies on to assert
    real unlink/write behaviour ends-to-end. For those call sites, pass
    ``check_temp_path=False`` so a `tmp_path`-rooted destination is treated
    as a legitimately-sandboxed real write, not a refused one; trigger 1
    (the env var) still refuses them exactly as it refuses every other site.

    Refuses LOUDLY — the caller is expected to print the returned reason to
    stderr — rather than silently no-op'ing, so a genuinely misconfigured
    real install (pointed at temp by accident) fails visibly instead of
    quietly skipping PATH integration and looking like success.
    """
    if os.environ.get(_MACHINE_MUTATION_DISABLE_ENV) == "1":
        return (
            f"refusing to {what}: {_MACHINE_MUTATION_DISABLE_ENV}=1 is set "
            "(test-suite belt-and-braces opt-out)"
        )
    if not check_temp_path:
        return None
    try:
        resolved = Path(path_being_written).resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
    except OSError:
        return None
    if resolved == temp_root or temp_root in resolved.parents:
        return (
            f"refusing to {what}: {path_being_written!s} resolves under the "
            f"system temp dir ({temp_root}) — that is the signature of a "
            "test sandbox path, not a genuine install location"
        )
    return None


def _win_user_path_entries() -> "Optional[tuple[list[str], str, int]]":
    """Read HKCU\\Environment PATH -> (entries, raw_value, value_type).

    Returns None when the value cannot be read at all -- callers print a
    "could not read Windows user PATH" warning and skip integration, matching
    the pre-conversion behaviour on an empty `powershell.exe` result. A missing
    key and a key that exists with no PATH value both mean the same thing --
    an empty PATH, legitimate to prepend onto -- not two branches; both hit
    the same `FileNotFoundError` handling below.

    Reads the RAW registry value: unlike the .NET
    ``GetEnvironmentVariable('PATH','User')`` this replaces, ``%VAR%``
    references are NOT expanded, so a value written back by
    ``_win_user_path_prepend`` preserves them instead of baking in this
    session's expansions.
    """
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            raw, value_type = winreg.QueryValueEx(key, "PATH")
    except FileNotFoundError:
        return ([], "", winreg.REG_EXPAND_SZ)
    except OSError:
        return None
    return ([e for e in raw.split(";") if e], raw, value_type)


def _win_user_path_prepend(entry: str, existing_raw: str, value_type: int) -> bool:
    """Prepend `entry` to HKCU\\Environment PATH, then broadcast the change.

    Writes back with the value type it was read with -- a REG_EXPAND_SZ PATH
    rewritten as REG_SZ silently stops expanding every `%VAR%` it contains,
    which is the classic installer-ate-my-PATH defect.

    The WM_SETTINGCHANGE broadcast is what makes already-running processes
    that honour it (Explorer, and shells launched from it afterwards) observe
    the new value. It is best-effort: a failed broadcast does not fail the write.
    """
    import ctypes
    import winreg
    new_value = f"{entry};{existing_raw}" if existing_raw else entry
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "PATH", 0, value_type, new_value)
    try:
        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, ctypes.c_wchar_p("Environment"), 0x0002, 1000,
            ctypes.byref(result),
        )
    except OSError:
        pass
    return True


_IO_REPARSE_TAG_APPEXECLINK = 0x8000001B


def _orphan_appx_stub(path: str) -> bool:
    """True when `path` is an app-execution alias whose backing package is gone.

    Shape check first (zero-length APPEXECLINK reparse point), then
    resolvability: a live alias' `os.stat` follows to the packaged target,
    an orphan's cannot. Replaces a PowerShell `LinkType -eq 'ReparsePoint'`
    test that could never fire: measured on Windows 11 / PowerShell 5.1, a
    live app-execution alias reports `LinkType` empty and `Target` as an empty
    collection, so the predicate this converts was unreachable for an orphan
    and everything else alike. Detection therefore widens from "never" to
    "actually orphaned" -- and deliberately not to live aliases: measured
    directly against `os.stat` (not PowerShell) on Windows 11, 2026-08-14,
    across all 55 zero-length APPEXECLINK aliases present in
    `%LOCALAPPDATA%\\Microsoft\\WindowsApps`, zero were reported as orphans --
    live aliases resolve through `os.stat` fine, so the resolvability split
    holds.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if st.st_size != 0:
        return False
    if getattr(st, "st_reparse_tag", 0) != _IO_REPARSE_TAG_APPEXECLINK:
        return False
    try:
        os.stat(path)
    except OSError:
        return True
    return False


_MANIFEST_ATTRS = ("SETUP_TEMPLATE_FILES", "SETUP_TEMPLATE_EXEC_FILES", "SETUP_TEMPLATE_HOOK_FILES")


def _load_setup_template_manifest(claude_klabauter_root: Path):
    """Load ``<claude_klabauter_root>/coordinator/lib/setup-templates-manifest.py``'s three
    ``list[str]`` module attributes — single-source-of-truth manifest, deliberately
    NOT hand-duplicated here (its own header: "Edit this list HERE and nowhere
    else").

    The manifest lives in claude-klabauter's OWN ``coordinator/lib/`` tree (b644d5a9's
    executable-surface relocation moved ``lib/`` out of the DoE-claude
    ``CLAUDE_PLUGIN_ROOT`` entirely), so this resolves off ``coordinator_claude_klabauter_root()``,
    not ``plugin_root`` — a future reader must NOT "restore" plugin_root
    resolution here on the theory that lib/ files belong under the plugin root;
    that theory stopped being true the day of the relocation.

    Formerly a `bash -c 'source ...'`-avoiding hand-rolled bash-array-literal
    parser (2026-07-21 pure-Python-shop cutover, retired in the same relocation
    that made the manifest itself a plain Python module) — the file is now
    itself Python, so a plain ``importlib`` load is the native, sanctioned
    reading of it (its own header: "Imported (never executed)"). The hyphenated
    filename precludes a normal ``import`` statement, hence
    ``importlib.util.spec_from_file_location``."""
    import importlib.util

    manifest = claude_klabauter_root / "coordinator" / "lib" / "setup-templates-manifest.py"
    if not manifest.is_file():
        raise SubstrateFatalError(
            f"install-substrate: setup-templates-manifest.py not found at {manifest}"
        )
    spec = importlib.util.spec_from_file_location("_setup_templates_manifest", manifest)
    if spec is None or spec.loader is None:
        raise SubstrateFatalError(
            f"install-substrate: could not load setup-templates-manifest.py at {manifest}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(spec.name, None)
        raise SubstrateFatalError(
            f"install-substrate: setup-templates-manifest.py at {manifest} failed to "
            f"import ({exc}) — it is corrupt or has a syntax error"
        ) from exc

    files, exec_files, hook_files = (getattr(module, attr, None) for attr in _MANIFEST_ATTRS)
    if not files:
        raise SubstrateFatalError(
            f"install-substrate: SETUP_TEMPLATE_FILES is empty or missing in {manifest} — "
            "setup-templates-manifest.py failed to define it or is corrupt"
        )
    return files, exec_files or [], hook_files or []


def _resolve_bin_templates_manifest_root() -> Path:
    """Resolve the claude-klabauter root `_load_bin_templates_manifest` reads from,
    when a caller does not already have one on hand.

    Rung 1: co-located — `bin-templates-manifest.py` lives in claude-klabauter's OWN
    `coordinator/lib/`, the SAME repo as this file (unlike DoE's
    `templates/bin/`, which genuinely is cross-repo and needs the parity
    test's registry-backed resolution). This rung is zero-subprocess,
    zero-env-dependent, and wins on every real checkout of this repo —
    including every test in this file's own directory, which is why
    `_static_bin_family_names()` can stay a practically zero-dependency
    call for its existing bare-arg callers.

    Rung 2: `coordinator_claude_klabauter_root()`'s registry chain, for the
    hypothetical split-install case where `coordinator_core` is resolved
    from somewhere other than beside its own `coordinator/lib/` sibling.
    Mirrors `coordinator_data_root.data_root()`'s two-rung shape."""
    colocated = Path(__file__).resolve().parents[2]
    if (colocated / "coordinator" / "lib" / "bin-templates-manifest.py").is_file():
        return colocated
    _claude_klabauter_root_str, _resolution_class = coordinator_claude_klabauter_root_with_class()
    return Path(_claude_klabauter_root_str)


class _BinTemplatesManifest:
    """The four named groups `coordinator/lib/bin-templates-manifest.py`
    declares, plus their union — returned by `_load_bin_templates_manifest`
    so callers can pick the group(s) they need without re-deriving
    membership by filtering the flat union (fragile: would require
    reconstructing set membership from a value-equal-but-not-identical
    tuple)."""

    __slots__ = ("ml_family", "ml_explicit", "platform_localize", "launcher_templates", "all")

    def __init__(self, ml_family, ml_explicit, platform_localize, launcher_templates, all_entries):
        self.ml_family = ml_family
        self.ml_explicit = ml_explicit
        self.platform_localize = platform_localize
        self.launcher_templates = launcher_templates
        self.all = all_entries

    def install_bin_resolvers_entries(self) -> "tuple":
        """Every entry `_install_bin_resolvers` actually installs — i.e.
        every group except `launcher_templates` (rendered elsewhere by
        `gen_claude_doe_launcher.py`, never copied via `_install_one` —
        see the manifest module's own docstring on that group)."""
        return self.ml_family + self.ml_explicit + self.platform_localize


_BIN_TEMPLATE_MANIFEST_GROUP_ATTRS = (
    "ML_FAMILY_FILES", "ML_EXPLICIT_FILES", "PLATFORM_LOCALIZE_FILES",
    "LAUNCHER_TEMPLATE_FILES", "ALL_BIN_TEMPLATE_FILES",
)
"""The five `bin-templates-manifest.py` group attribute names
`_load_bin_templates_manifest` reads, in the same order as
`_BinTemplatesManifest.__init__`'s positional args (minus `all_entries`,
which is `ALL_BIN_TEMPLATE_FILES` itself). Extracted to a module constant so
`WRITE_SURFACE`'s ml_family/ml_explicit/platform_localize clauses can name
these attrs in `discovered_by` by reading this constant rather than
restating the strings — a rename here alone keeps both in sync."""


def _load_bin_templates_manifest(claude_klabauter_root: Path) -> "_BinTemplatesManifest":
    """Load ``<claude_klabauter_root>/coordinator/lib/bin-templates-manifest.py``'s
    named `BinTemplateEntry` groups — single source of truth for DoE's
    ``templates/bin/`` classification (its own header: "edited HERE and
    nowhere else"). See that module's docstring for the full contract; see
    `_load_setup_template_manifest` immediately above for why this is an
    ``importlib`` load rather than a normal ``import`` statement (the
    hyphenated filename precludes one)."""
    import importlib.util

    manifest = claude_klabauter_root / "coordinator" / "lib" / "bin-templates-manifest.py"
    if not manifest.is_file():
        raise SubstrateFatalError(
            f"install-substrate: bin-templates-manifest.py not found at {manifest}"
        )
    spec = importlib.util.spec_from_file_location("_bin_templates_manifest", manifest)
    if spec is None or spec.loader is None:
        raise SubstrateFatalError(
            f"install-substrate: could not load bin-templates-manifest.py at {manifest}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(spec.name, None)
        raise SubstrateFatalError(
            f"install-substrate: bin-templates-manifest.py at {manifest} failed to "
            f"import ({exc}) — it is corrupt or has a syntax error"
        ) from exc

    ml_family, ml_explicit, platform_localize, launcher_templates, all_entries = (
        getattr(module, attr, None) for attr in _BIN_TEMPLATE_MANIFEST_GROUP_ATTRS
    )
    if not all_entries:
        raise SubstrateFatalError(
            f"install-substrate: ALL_BIN_TEMPLATE_FILES is empty or missing in {manifest} — "
            "bin-templates-manifest.py failed to define it or is corrupt"
        )
    return _BinTemplatesManifest(
        ml_family=tuple(ml_family or ()),
        ml_explicit=tuple(ml_explicit or ()),
        platform_localize=tuple(platform_localize or ()),
        launcher_templates=tuple(launcher_templates or ()),
        all_entries=tuple(all_entries),
    )


# --- C6: foreign-tracked-overwrite guard mechanics ---------------------------
#
# Placement note (the Staff Engineer F4, settled, not negotiable): the tracked-ness
# CLASSIFICATION lives in `_percolation_and_path_steps` (the call site that
# owns destination-provenance semantics), never in `_install_one` — see that
# function's own negative-spec docstring. Everything below is either pure
# write MECHANICS (`_careful_write`, via `atomic_write_bytes`) or a
# structural probe (`_resolve_directory_tracked_set`) with no opinion about
# who is foreign; `_install_one` only learns a `write_strategy` MECHANISM
# selector whose value the call site chooses.


def _resolve_directory_tracked_set(dest_dir: Path) -> Optional["FrozenSet[str]"]:
    """Resolve the set of paths (POSIX-relative to ``dest_dir``) TRACKED by
    whatever git repo (if any) ``dest_dir`` sits inside — the discriminator
    this chunk uses is TRACKED-NESS, not repo ownership (the Staff Engineer finding 0): a
    directory merely sitting inside a foreign repo, with nothing tracked
    under it (e.g. a dotfiles-managed HOME), is correctly NOT foreign under
    this predicate.

    Resolved ONCE per destination DIRECTORY — a single ``git ls-files -z``
    spawn — never per file; callers in `_percolation_and_path_steps` call
    this once for ``setup_dest`` before either write loop.

    Returns ``None`` when the probe itself is UNAVAILABLE — no ``git``
    executable resolvable on PATH, or the spawn itself fails/times out (the
    Windows-first-class degrade case: no bash, no git). Callers MUST treat
    ``None`` as "cannot classify — refuse overwrite, report", per the plan's
    degrade contract, NEVER as "nothing is tracked" (that would silently
    treat an unprobeable machine as safe to force-overwrite).

    Returns an EMPTY frozenset when git IS available but ``dest_dir`` is not
    inside a git repository at all (``git ls-files`` exits non-zero, e.g.
    "not a git repository") — a real, distinct answer (nothing is tracked
    here), not a probe failure."""
    git_bin = shutil.which("git")
    if git_bin is None:
        return None
    if not dest_dir.is_dir():
        return frozenset()
    try:
        # Review: code-reviewer (Finding 2) — routed through the shared _run
        # wrapper (accommodates timeout/stdin/capture_output) rather than a
        # second hand-rolled subprocess.run + _NO_CONSOLE call site.
        proc = _run(
            [git_bin, "-C", str(dest_dir), "ls-files", "-z", "--", "."],
            capture_output=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        # Review: code-reviewer (Finding 3) — nonzero exit is folded to "not a
        # repo" per the plan's stated two-way contract (unchanged), but we log
        # stderr so a genuine mid-repo failure (corrupt .git, permissions) isn't
        # indistinguishable from a legitimate non-repo when an operator is later
        # debugging an unexpected force-overwrite/delete.
        stderr = proc.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        print(
            f"install-substrate: git ls-files -C {dest_dir} exited "
            f"{proc.returncode} (treating as not-a-repo): {(stderr or '').strip()}",
            file=sys.stderr,
        )
        return frozenset()
    raw = proc.stdout
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return frozenset(p for p in raw.split("\x00") if p)


# `settings.json`/`hooks.json` are produced by a wholly separate module
# (`gen_settings_hooks.py::_atomic_write_json`) this chunk does not touch —
# never a legitimate careful-write destination under any circumstance. A
# flawed writeover of `hooks.json` has previously bricked an operator's
# ability to send messages (PM-named hazard) — this is a hard, name-based
# refusal independent of manifest membership.
_CAREFUL_WRITE_FORBIDDEN_NAMES = frozenset({"settings.json", "hooks.json"})


def _assert_careful_write_in_manifest(
    relative_path: str, manifest_relative_paths: "FrozenSet[str]"
) -> None:
    """Blast-radius negative-spec (AC19): the careful-write mechanism must
    never be reachable for a destination outside the percolation manifest
    (``SETUP_TEMPLATE_FILES`` + ``SETUP_TEMPLATE_HOOK_FILES``), and never for
    ``settings.json``/``hooks.json`` under any circumstance. Structurally
    unreachable in the normal call path (`_percolation_and_path_steps`'s two
    write loops iterate the manifest lists themselves, so ``relative_path``
    can only ever BE a manifest entry there) — this assertion is
    defense-in-depth against a future/fuzzed caller that gets it wrong,
    verified directly by a parametrized test rather than left merely
    untested."""
    name = Path(relative_path).name
    if name in _CAREFUL_WRITE_FORBIDDEN_NAMES:
        raise SubstrateFatalError(
            f"install-substrate: refusing careful-write of {relative_path!r} — "
            f"{name} is never a percolation-manifest destination"
        )
    if relative_path not in manifest_relative_paths:
        raise SubstrateFatalError(
            f"install-substrate: refusing careful-write of {relative_path!r} — "
            "not a member of the percolation setup-templates manifest "
            "(SETUP_TEMPLATE_FILES + SETUP_TEMPLATE_HOOK_FILES)"
        )


_OVERWRITE_BACKUP_SUBDIR = "setup-overwrite-backups"
"""Sibling-of-``setup/`` dirname under ``<install_base>/.claude/`` for
`_careful_write_backup_path`'s disposable pre-overwrite backups. Extracted
so `WRITE_SURFACE`'s backup-tree clause reads this same constant rather than
restating the literal — see that clause's docstring."""

_TRACKED_ML_FILES = ("README.md", ".gitignore", "registry.toml.example", "registry.local.toml.example")
"""The `<settings-home>/machine-local/` tracked-template file names copied
by `run`'s Step 2 (seed-if-absent) with preserve-on-diff notice when an
operator-customized copy already differs from the shipped template.
Extracted so both call sites in `run` (the check-only probe and the real
seed loop) and `WRITE_SURFACE`'s clause read one spelling."""

_ML_UNREAL_TOML_NAME = "unreal.toml"
"""`<settings-home>/machine-local/unreal.toml` — the Step 2b concern-
baseline file, seeded once from `unreal.toml.example` and never
subsequently overwritten by `run`."""

_ML_REGISTRY_TOML_NAME = "registry.toml"
"""`<settings-home>/machine-local/registry.toml` — the live, operator-
mutable registry. `run`'s Step 2c seeds it once from
`registry.toml.example` when absent; `_register_hardware_concern` (Step 3g)
separately mutates its `concerns` array in place on every run."""

_ML_HARDWARE_TOML_NAME = "hardware.toml"
"""`<settings-home>/machine-local/hardware.toml` — the Step 3f concern-
baseline file, seeded once from `hardware.toml.example` when absent;
values are written later by `detect-hardware.sh`, not by `run` itself."""

_SETTINGS_MANIFEST_FILENAME = "settings-manifest.md"
"""`<settings-home>/settings-manifest.md` — installed by Step 3c-ii via
`_install_one`, same preserve-on-diff policy as any non-code/non-forced
template (see `_install_one`'s docstring)."""

_WHOAMI_DIRNAME = "coordinator-whoami"
"""Shared leaf name for both the settings-home destination
(`<settings-home>/coordinator-whoami/`) and the legacy install-base
location (`<install_base>/.claude/coordinator-whoami`) `_c10a_steps`
relocates away from, replacing it with a compat pointer."""

_LEGACY_VENV_DIRNAME = ".coordinator-venv"
"""Shared leaf name for both the legacy `<install_base>/.claude/
.coordinator-venv` directory `_c10a_steps` deletes (its own surface, once
the settings-home venv passes its health probe) and the CURRENT
`<settings-home>/.coordinator-venv` tree it only reads the health of — the
latter is a distinct tree owned and declared by `ensure_venv`, not by this
module."""


def _careful_write_backup_path(install_base: Path, relative_path: str) -> Path:
    """The disposable pre-overwrite backup location for a foreign-tracked
    percolation destination:
    ``<install_base>/.claude/setup-overwrite-backups/<relative-path-with-
    slashes-preserved>.pre-install-<TIMESTAMP>.bak`` — a PLAIN SIBLING of
    ``setup/`` under the same, already-known install base (never inside the
    git-tracked ``setup/`` tree itself, so the backup is not itself subject
    to the next clobber). No registry lookup, no settings-home resolution,
    no coordinator invocation needed to find it — restorable by a human with
    a shell and nothing else:

        cp <install_base>/.claude/setup-overwrite-backups/<relative-path>.pre-install-<TIMESTAMP>.bak \\
           <install_base>/.claude/setup/<relative-path>
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return (
        install_base / ".claude" / _OVERWRITE_BACKUP_SUBDIR
        / f"{relative_path}.pre-install-{timestamp}.bak"
    )


def _careful_write(
    dst: Path, src: Path, *, relative_path: str,
    manifest_relative_paths: "FrozenSet[str]", install_base: Path,
) -> Path:
    """CAREFUL-WRITE CONTRACT (AC6/AC20/AC21): take a disposable, timestamped
    backup of ``dst``'s PRE-EXISTING content OUTSIDE the git-tracked
    ``setup/`` tree, THEN perform a mode-preserving atomic replace via
    :func:`atomic_write_bytes`, THEN return the backup location for the
    caller to report. The backup write happens BEFORE the atomic replace,
    unconditionally — if the backup write itself fails, the overwrite MUST
    NOT proceed (fail loud); this is stricter than
    ``guard_settings_integrity.py``'s best-effort ``.settings-clobbered.bak``
    because C6's destinations have no restore ladder behind them."""
    _assert_careful_write_in_manifest(relative_path, manifest_relative_paths)
    backup_path = _careful_write_backup_path(install_base, relative_path)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(dst, backup_path)
    except OSError as exc:
        raise SubstrateFatalError(
            f"install-substrate: careful-write backup FAILED for {dst} -> "
            f"{backup_path} ({exc}) — refusing to overwrite without a backup in place"
        ) from exc
    if not backup_path.is_file():
        raise SubstrateFatalError(
            f"install-substrate: careful-write backup did not land on disk at "
            f"{backup_path} — refusing to overwrite {dst} without a backup in place"
        )
    atomic_write_bytes(dst, src.read_bytes(), preserve_mode=True)
    return backup_path


def _install_one_effective_bytes(src: Path, python_bin_substitution: Optional[str]) -> bytes:
    """The bytes `_install_one` actually writes/compares for `src` — a plain
    byte-substring replace of every ``__PYTHON_BIN__`` occurrence when
    ``python_bin_substitution`` is not ``None`` (C2), else `src`'s raw
    content unchanged. See `_install_one`'s own docstring for why this
    function does not itself decide WHICH sources get a substitution
    value — only what to do once one is supplied."""
    raw = src.read_bytes()
    if python_bin_substitution is None:
        return raw
    return raw.replace(b"__PYTHON_BIN__", python_bin_substitution.encode("utf-8"))


def _install_one_content_matches(
    src: Path, dst: Path, python_bin_substitution: Optional[str]
) -> bool:
    """Whether `dst` already holds `src`'s effective (post-substitution)
    content — the substitution-aware replacement for the bare
    ``filecmp.cmp(src, dst, shallow=False)`` every `_install_one` branch
    used before C2. `dst` must already be known to exist; callers gate on
    that themselves (mirrors `filecmp.cmp`'s own precondition)."""
    return dst.read_bytes() == _install_one_effective_bytes(src, python_bin_substitution)


def _install_one(
    src: Path, dst: Path, exec_bit: bool, warn_prefix: str, check_only: bool,
    *, force_overwrite: bool = False,
    write_strategy: str = "force",
    careful_manifest_relative_paths: Optional["FrozenSet[str]"] = None,
    careful_relative_path: Optional[str] = None,
    careful_install_base: Optional[Path] = None,
    python_bin_substitution: Optional[str] = None,
) -> Optional[Path]:
    """Overwrite policy — code files vs config files vs caller-forced templates.

    Code files (*.py, *.sh, machine-local/resolve-coordinator-clone/claude-home
    extension-less wrappers, *.cmd): force-overwrite when content differs.
    Config files (*.toml and all others): preserve-on-diff — protect operator
    customizations.

    The third input to the policy, ``force_overwrite``, is an explicit
    caller-forced class: it ORs with (never replaces) the suffix/name
    classification above. This function deliberately does NOT learn to
    recognize doctrine-tracked-but-non-code templates by name or suffix —
    that classification decision belongs to the call site that owns the
    semantics of what it's installing, not to this shared low-level copier.
    The percolation hook files (``percolate-store.yaml``, the pre-ci/pre-rsync
    ``.gitkeep`` markers, the post-rsync allowlist, the README) are the
    motivating case: they read like operator config by suffix/name (a
    ``.yaml``, a ``.gitkeep``, a ``.txt``, a ``.md``) but are actually
    doctrine-tracked templates sourced from the coordinator-claude/DoE tree,
    so a stale destination must be repaired on re-install rather than
    preserved as if it were operator customization — see
    `_percolation_and_path_steps`'s ``setup_hook_files`` loop, which passes
    ``force_overwrite=True`` for exactly this reason.

    ``write_strategy`` (C6) is a MECHANISM selector only — ``"force"``
    (default, preserves every non-percolation caller's existing behaviour
    unchanged: a plain ``shutil.copyfile``), ``"careful"`` (foreign-tracked
    overwrite: disposable pre-write backup + mode-preserving atomic replace
    via :func:`_careful_write`/:func:`atomic_write_bytes`, requires
    ``careful_relative_path``/``careful_manifest_relative_paths``/
    ``careful_install_base``), or ``"refuse"`` (the git-identity-probe-
    unavailable degrade: content differs but the destination is left
    untouched, reported rather than silently skipped). This function still
    learns NOTHING about destination provenance — the call site
    (`_percolation_and_path_steps`) decides the VALUE, this function only
    executes whichever mechanism it is told to (the Staff Engineer F4).

    Returns the disposable backup ``Path`` when a ``"careful"`` write
    actually performed a backup-then-replace this call (WRITE_SURFACE
    clause 15's resolution — see `_percolation_and_path_steps`, which
    accumulates these across both its loops and journals them once);
    ``None`` in every other case (cold creation, force-overwrite,
    preserve-on-diff, refuse, check-only).

    ``python_bin_substitution`` (C2, AC6/AC8) is the same shape of input as
    ``force_overwrite``/``write_strategy``: an explicit, caller-supplied
    value, never a name/suffix this function recognizes on its own — the
    classification decision (which families need the substitution) stays
    at the call site (`_install_bin_resolvers`), per this docstring's own
    opening paragraph. When not ``None``, every literal ``__PYTHON_BIN__``
    occurrence in ``src``'s bytes is replaced with this value before the
    content is written OR compared against ``dst`` — this is a byte
    substring replace, not template-aware, and is a silent no-op on any
    source that does not carry the token (the POSIX extensionless
    forwarders in the same static families carry no such token — see
    `_install_bin_resolvers`'s call site comment). ``None`` (the default)
    preserves every existing caller's byte-verbatim-copy behaviour
    unchanged.

    Negative-spec (AC6 durability): this in-file substitution is NOT the
    durable fix by itself. DoE-claude's landed SessionStart sweep
    (``coordinator/hooks/scripts/_bin_impl_drift.py``) byte-copies
    ``templates/bin/`` content verbatim on a genuine template change, with
    no re-bake step — so a template edit re-introduces the literal
    ``__PYTHON_BIN__`` token here until the next full install re-runs this
    function. The durable half is ``<settings-home>/bin/.python-bin``,
    written by the shim's own runtime probe (never touched by the sweep,
    which only iterates ``templates/bin/``) — this function does not write
    that sidecar; see C1."""
    if check_only:
        if dst.exists() and _install_one_content_matches(src, dst, python_bin_substitution):
            print(f"[install-substrate] check: {dst.name} up to date -> {dst} (no-op)")
            return
        if dst.exists() and write_strategy in ("careful", "refuse"):
            # CHECK-MODE CONTRACT (AC6): a foreign-tracked (or
            # git-identity-unresolvable) stale destination reports as "not
            # managed here" rather than raising — check must not hard-fail
            # forever on the two destinations that were already dirty before
            # this guard existed, with no remediation the plan permits.
            print(
                f"[install-substrate] check: {dst.name} not managed here "
                f"(foreign-tracked, or git identity unresolvable) at {dst} — "
                "not reported as stale"
            )
            return
        status = "stale" if dst.exists() else "absent"
        raise SubstrateFatalError(
            f"install-substrate: check failed: {dst.name} is {status} at {dst} "
            f"(would write from {src})"
        )

    reason = None
    name = src.name
    if src.suffix in (".py", ".sh"):
        force_overwrite = True
        reason = "code file"
    if name in ("machine-local", "resolve-coordinator-clone", "claude-home") or src.suffix == ".cmd":
        force_overwrite = True
        reason = "code file"
    if reason is None and force_overwrite:
        reason = "tracked template"

    if not dst.exists():
        atomic_write_bytes(
            dst, _install_one_effective_bytes(src, python_bin_substitution),
            preserve_mode=True,
        )
        if exec_bit:
            dst.chmod(dst.stat().st_mode | 0o111)
    elif force_overwrite:
        if not _install_one_content_matches(src, dst, python_bin_substitution):
            if write_strategy == "careful":
                if (
                    careful_relative_path is None
                    or careful_manifest_relative_paths is None
                    or careful_install_base is None
                ):
                    raise SubstrateFatalError(
                        f"install-substrate: write_strategy='careful' for {dst} requires "
                        "careful_relative_path/careful_manifest_relative_paths/"
                        "careful_install_base — call-site bug"
                    )
                backup_path = _careful_write(
                    dst, src,
                    relative_path=careful_relative_path,
                    manifest_relative_paths=careful_manifest_relative_paths,
                    install_base=careful_install_base,
                )
                print(
                    f"[{warn_prefix}] updated {dst.name} ({reason}; re-install overwrites) "
                    f"via CAREFUL write (foreign-tracked destination) — "
                    f"destination: {dst}, backup: {backup_path}"
                )
                if exec_bit:
                    dst.chmod(dst.stat().st_mode | 0o111)
                return backup_path
            elif write_strategy == "refuse":
                # Windows-first-class degrade: the git-identity probe is
                # unavailable (no git on PATH) — refuse the overwrite rather
                # than guess, but still REPORT it (never a silent skip).
                # Cold creation is entirely unaffected by this branch — it
                # never reaches here (see the `if not dst.exists()` branch
                # above, unconditional regardless of write_strategy).
                print(
                    f"[{warn_prefix}] refusing to overwrite {dst.name} at {dst} — "
                    "git identity probe unavailable (no git on PATH); content "
                    "differs but destination preserved. Re-run once git is "
                    "resolvable on PATH to deliver this update safely."
                )
            else:
                # C0: routed through atomic_write_bytes (same-directory
                # mkstemp + os.replace) rather than a bare shutil.copyfile —
                # the previous check-then-write (filecmp.cmp above, then a
                # non-atomic copy here) was a TOCTOU window a peer session
                # could observe mid-copy on exactly the hot-path files this
                # plan exists to speed up (_machine_local.py, the .cmd
                # shims). os.replace is atomic on both Windows and POSIX, so
                # a concurrent reader now always observes either the old or
                # the new complete content, never a torn write.
                print(f"[{warn_prefix}] updated {dst.name} ({reason}; re-install overwrites)")
                atomic_write_bytes(
                    dst, _install_one_effective_bytes(src, python_bin_substitution),
                    preserve_mode=True,
                )
                if exec_bit:
                    dst.chmod(dst.stat().st_mode | 0o111)
        elif exec_bit:
            # Content already matches, but reapply the exec bit anyway: it can be
            # lost out-of-band (Windows checkout, core.fileMode=false, an archive
            # extraction, a permission-dropping copy) without the content
            # changing, and re-install is the documented repair path. Mirrors the
            # unconditional reapply on the preserve/identical branch below —
            # force-overwrite classes (.py/.sh/.cmd/wrappers) are exactly the
            # classes where the exec bit is load-bearing, so skipping it here
            # would silently leave the repair path non-functional.
            dst.chmod(dst.stat().st_mode | 0o111)
    elif _install_one_content_matches(src, dst, python_bin_substitution):
        if exec_bit:
            dst.chmod(dst.stat().st_mode | 0o111)
    else:
        if warn_prefix == "claude-home":
            print(
                f"[claude-home] WARNING: operator-customized {dst.name} preserved, "
                "but claude-home is a cross-repo contract surface — customization "
                f"is anti-doctrine. Diff against {src} and restore unless intentional."
            )
        else:
            print(
                f"[machine-local] operator-customized {dst.name} preserved; "
                f"template at {src} for diff reference"
            )


def _agent_cmd_dest_name(name: str) -> str:
    """The INSTALLED `.cmd` sibling's filename never carries the installed
    name's own extension either — e.g. an installed name `foo.sh` would get
    `foo.cmd`, never the malformed `foo.sh.cmd` a naive `f"{name}.cmd"` would
    produce (AC7 parity fix — historically exercised by the now-retired
    `mint-deliverable-id.sh` divergence, before that CLI's installed name was
    made extensionless). For an extensionless installed name (the case for
    every current agent-helper CLI) this is a no-op (`Path(name).stem ==
    name`)."""
    return Path(name).stem + ".cmd"


# Provenance markers for `_sweep_orphaned_agent_helpers`. `_AGENT_FORWARDER_
# MARKER`/`_AGENT_CMD_FORWARDER_MARKER`/`_AGENT_PS1_FORWARDER_MARKER` are
# substrings of the exact bodies `_write_agent_forwarder`/
# `_write_agent_cmd_forwarder`/`_write_agent_ps1_forwarder` emit below — defined
# once here and interpolated into those bodies (rather than duplicated as
# separate literals) so a positive-identification check can never drift out
# of sync with what the generators actually write. None of these strings
# appears in any OTHER family this module installs (ml_family/ch_family's
# machine-local and claude-home sources, the DoE-copied
# platform-localize/resolve-coordinator-clone templates, python3.cmd, or
# the 5 resolver files) — those are copied verbatim from source trees this
# module does not author, so they carry none of this module's own
# generated-body text.
#
# `_LEGACY_CMD_MARKER` is DIFFERENT in kind: it identifies `.cmd` files
# written by the RETIRED copy-a-source-.cmd-verbatim approach
# (`_write_agent_forwarder`'s docstring), whose bodies were byte-identical
# copies of a `coordinator/bin/<stem>.cmd` authored by
# `coordinator/bin/gen-launcher-shim.py` — every real, pre-existing
# agent-helper `.cmd` orphan on a live install predates the current
# generator and carries THIS marker, not `_AGENT_CMD_FORWARDER_MARKER`.
# Unlike the two markers above, this one is NOT exclusive to agent-helper
# forwarders — `gen-launcher-shim.py` also generated the legitimate,
# still-installed-every-run `platform-localize.cmd` and
# `resolve-coordinator-clone.cmd` DoE templates, confirmed carrying this
# exact marker on a live install. A legacy-marker match is therefore
# NEVER sufficient on its own; `_sweep_orphaned_agent_helpers` additionally
# requires the name be absent from `_static_bin_family_names()` (every
# other family's complete, statically-known name set) before treating a
# legacy-marker file as sweepable — see that function's docstring.
_AGENT_FORWARDER_MARKER = "from _resolve_claude_klabauter import exec_cli"
_AGENT_CMD_FORWARDER_MARKER = "GENERATED by _write_agent_cmd_forwarder"
_AGENT_PS1_FORWARDER_MARKER = "GENERATED by _write_agent_ps1_forwarder"
_LEGACY_CMD_MARKER = "Generated by coordinator/bin/gen-launcher-shim.py"

# Pre-`_LEGACY_CMD_MARKER` orphan names: `.cmd` files hand-authored before
# `gen-launcher-shim.py` existed to stamp `_LEGACY_CMD_MARKER` at all, so
# they carry NEITHER marker and fall through both branches of
# `_sweep_orphaned_agent_helpers`'s marker check — silently unsweepable
# forever, unlike every marker-carrying orphan that check does catch. This
# module's own docstring (see `_sweep_orphaned_agent_helpers`) already names
# `mint-deliverable-id.sh.cmd` as "confirmed live" motivating evidence for
# the legacy-marker branch, but a live install audit (C6,
# docs/plans/2026-08-10-entrypoint-gate-launcher-and-changed-only.md) found
# the on-disk file itself predates even that marker generation, so the
# branch it supposedly motivated never actually matches it. A bounded,
# explicitly-named set — never a name-shape heuristic — because deletion
# stays opt-in per file, matching this function's own "positive
# identification, never absence-implies-orphan" contract.
_PRE_MARKER_LEGACY_ORPHAN_NAMES = frozenset({"mint-deliverable-id.sh.cmd"})

# RAW-CMDLINE-PRESERVATION TARGETS -- mirrored from, not imported from,
# ``coordinator/bin/gen-launcher-shim.py``'s ``_RAW_CMDLINE_ENTRYPOINTS``
# (state/bug-backlog/2026-08-08-cmd-exe-shim-eats-the-caret-in-a-git-rev-
# 6679bf76eb8a.yaml). That set is keyed by generator-source-tree-relative
# POSIX path (``coordinator/bin/<file>.py``); this module is keyed by the
# on-disk TARGET filename `_derive_agent_helper_target_map` already resolves
# per forwarder (the same string gen-launcher-shim.py's path ends in), so the
# two are compared by that shared suffix, not by importing one set into the
# other's module. This module already declines to import gen-launcher-shim.py
# for the same reason on the .ps1 leg -- see `_write_agent_ps1_forwarder`'s
# docstring: that function emits its own body with its own marker rather than
# calling the generator's `render_ps1`, because the generator's source-side
# output carries no substrate marker at all and would make every installed
# launcher permanently unsweepable. A hyphenated-filename module also has no
# ordinary `import` form -- only `importlib.util.spec_from_file_location`
# against a path this module has no other reason to resolve -- which is
# extra coupling for one shared constant. Widen only for a NAMED,
# verified-live defect (as below) -- never speculatively.
#
# `scoped-git-commit` and `cross-repo-memo` added per
# cross-repo/inbox/2026-08-07-doe-claude-em-cmd-forwarder-drops-everything-
# after-a-newline.md: both take multi-line arguments as a matter of course
# (commit messages, memo bodies) and both are extensionless on-disk CLIs
# (no `.py` suffix -- see `_derive_agent_helper_target_map`'s stem-dedup
# rule), so their TARGET string in this module's keying convention is the
# bare name, not a `.py`-suffixed one. `gen-launcher-shim.py`'s own mirror
# set (`_RAW_CMDLINE_ENTRYPOINTS`) has since been brought into line with
# this one -- the two sets are kept in sync by convention (mirrored, not
# imported, per this module's own docstring above), with
# `test_bin_launcher_parity.py::test_raw_cmdline_entrypoints_matches_substrate_targets`
# as the drift guard. Extend BOTH sets together, or that test goes red.
#
# CORRECTED (docs/plans/2026-08-15-the-caret-fix-went-to-the-caller-that-never-broke.md):
# the capture this set opts a target into is NOT a fix for the caret loss, and the
# 2026-08-10 fix (docs/plans/2026-08-10-caret-fix-on-the-wrong-launcher.md) plus its guard
# suite validated only the one caller that was never broken. cmd.exe strips the caret while
# parsing its OWN `/c` string; whether `%CMDCMDLINE%` still holds the unstripped text
# depends on how the SPAWNING process quoted that string, and survives only when the entire
# post-`/c` string is wrapped in one outer quote pair (first and last character both `"`).
# PowerShell emits that shape; git-bash/MSYS and `subprocess.run([...])` list-form do not,
# so on those rungs the capture records text the caret is already gone from — it preserves
# what `%CMDCMDLINE%` still holds, not what the caller originally typed. See
# `coordinator/bin/lib/raw_cmdline_recovery.py`, whose classifier is the piece responsible
# for detecting an unsound capture rather than trusting it. Originating incidents:
# state/bug-backlog/2026-08-08-cmd-exe-shim-eats-the-caret-in-a-git-rev-6679bf76eb8a.yaml
# (DoE-claude tree) and docs/decisions/DR-303-windows-spawn-economics-is-a-fix-not-a-desig.md
# § Residual uncertainty ("Caret recovery ... reasoned from code on macOS").
_RAW_CMDLINE_TARGETS = frozenset(
    {"coordinator-write-review-trail.py", "scoped-git-commit", "cross-repo-memo.py"}
)


def _agent_cmd_raw_cmdline_block(target: str) -> str:
    """The `_LAUNCHER_RAW_CMDLINE_FILE` capture block, or the empty string
    when `target` is not in `_RAW_CMDLINE_TARGETS`.

    Empty-by-default for the same reason as every other optional block this
    generator emits: every forwarder for a target NOT named in
    `_RAW_CMDLINE_TARGETS` must render byte-identical to before this
    mechanism existed (AC1).

    Mirrors `gen-launcher-shim.py::_cmd_raw_cmdline_block` line for line --
    see that function's docstring for the full rationale, including the
    2026-08-14 collision fix (this block MUST change together with that
    one; the two sets are kept in sync by convention per this module's
    `_RAW_CMDLINE_TARGETS` docstring above, and so is this block's body).

    CORRECTED: emitting this block does not make the resulting `%CMDCMDLINE%`
    capture trustworthy. It is caller-conditional per this module's
    `_RAW_CMDLINE_TARGETS` docstring above -- sound only when the spawning
    process outer-quoted the entire post-`/c` string (PowerShell); on
    git-bash/MSYS and `subprocess.run([...])` list-form the capture records
    text the caret is already gone from. The consumer
    (`coordinator/bin/lib/raw_cmdline_recovery.py`) is responsible for
    classifying the spawn shape and refusing rather than trusting it.

    `echo %CMDCMDLINE%` redirected to a file, not
    `set "_X=%CMDCMDLINE%"`: cmd.exe's `set` re-strips any literal `^` from
    its own right-hand-side expansion during ITS OWN population -- the same
    caret-eating defect this mechanism exists to work around, striking a
    SECOND, independent time if `set` were used for the capture itself. The
    env var therefore names a FILE PATH (itself caret-free, so an ordinary
    `set` is safe for the var itself), never the raw text directly. Do not
    simplify this back to `set "_LAUNCHER_RAW_CMDLINE_FILE=%CMDCMDLINE%"`.

    The file lives inside a freshly `mkdir`-ed, retry-until-unique directory
    rather than at a bare `%RANDOM%%RANDOM%.tmp` path: `%RANDOM%` is seeded
    once per `cmd.exe` process at one-second resolution, so two launcher
    invocations starting in the same second draw the IDENTICAL sequence and
    collide on the identical path -- routine, not a corner case, at this
    machine's 50-70 concurrent-session norm. `mkdir` is atomic
    (`CreateDirectory` either creates or fails outright, no separate
    exists-check race window), so the retry loop below is genuinely
    collision-free, unlike a check-then-write pattern.

    Review: staff-eng (Finding 0) -- mirrors gen-launcher-shim.py::
    _cmd_raw_cmdline_block's own fix for the same finding: the retry above
    was originally an unbounded `goto`, which spins forever (stderr
    swallowed by `2>nul`) under a full/read-only/ACL-denied `%TEMP%`,
    hanging the forwarder BEFORE Python ever starts. Bounded to three
    unrolled attempts behind distinct labels; on all three failing, control
    falls through to `:_coordinator_raw_cmdline_giveup` WITHOUT setting
    `_LAUNCHER_RAW_CMDLINE_FILE` -- `recover_windows_argv` already treats a
    missing env var as a no-op fallback to `argv`, so this degrades to
    best-effort like every other failure mode of this mechanism, never a
    hang. MUST change together with the mirrored function above -- see this
    module's own `_RAW_CMDLINE_TARGETS` docstring.
    """
    if target not in _RAW_CMDLINE_TARGETS:
        return ""
    return (
        ":_coordinator_raw_cmdline_attempt1\n"
        'set "_LAUNCHER_RAW_CMDLINE_DIR=%TEMP%\\_coordinator_launcher_%RANDOM%%RANDOM%%RANDOM%"\n'
        '2>nul mkdir "%_LAUNCHER_RAW_CMDLINE_DIR%"\n'
        "if not errorlevel 1 goto :_coordinator_raw_cmdline_captured\n"
        ":_coordinator_raw_cmdline_attempt2\n"
        'set "_LAUNCHER_RAW_CMDLINE_DIR=%TEMP%\\_coordinator_launcher_%RANDOM%%RANDOM%%RANDOM%"\n'
        '2>nul mkdir "%_LAUNCHER_RAW_CMDLINE_DIR%"\n'
        "if not errorlevel 1 goto :_coordinator_raw_cmdline_captured\n"
        ":_coordinator_raw_cmdline_attempt3\n"
        'set "_LAUNCHER_RAW_CMDLINE_DIR=%TEMP%\\_coordinator_launcher_%RANDOM%%RANDOM%%RANDOM%"\n'
        '2>nul mkdir "%_LAUNCHER_RAW_CMDLINE_DIR%"\n'
        "if errorlevel 1 goto :_coordinator_raw_cmdline_giveup\n"
        ":_coordinator_raw_cmdline_captured\n"
        'set "_LAUNCHER_RAW_CMDLINE_FILE=%_LAUNCHER_RAW_CMDLINE_DIR%\\cmdline.tmp"\n'
        'echo %CMDCMDLINE%>"%_LAUNCHER_RAW_CMDLINE_FILE%"\n'
        ":_coordinator_raw_cmdline_giveup\n"
    )


def _write_agent_forwarder(name: str, dst: Path, check_only: bool, *, target: str) -> None:
    """Naked-Python forwarder that resolves and execs the claude-klabauter-resident
    CLI at ``<claude-klabauter-root>/coordinator/bin/<target>``, per the ratified
    resolve-claude-klabauter-bin contract (DoE-claude
    ``coordinator/snippets/resolve-claude-klabauter-bin.md``, DoE commit ``ad7fb0d1``).

    ``target`` — the real on-disk filename inside ``coordinator/bin/`` this
    forwarder execs — is REQUIRED and keyword-only, sourced from
    ``_derive_agent_helper_target_map``'s installed-name -> on-disk-filename
    scan. It is deliberately not defaultable: the only available default
    would be re-deriving the target from the installed name alone (e.g.
    ``name`` verbatim), which is correct ONLY when the installed name already
    IS the on-disk filename verbatim (true for every extensionless CLI) and
    is exactly the fail-open re-derivation this signature exists to make
    unrepresentable. Re-deriving the target from the installed name alone
    silently produced the nonexistent extensionless path for every
    ``.py``-suffixed CLI — the entire /workstream-complete + /handoff
    ceremony spine (``wsc-close.py``, ``wsc-session-disposition.py``,
    ``review-brightline-gate.py``, etc.) rc=127'd on a fresh install because
    of exactly this. See
    cross-repo/inbox/2026-07-23-claude-central-em-claude-klabauter-pickup-assemble-heads-up.md
    § 0.

    The full resolution ladder used to be inlined verbatim into THIS body
    (~50 lines duplicated per forwarder) — with the forwarder SET now
    derived from a coordinator/bin/ directory listing rather than a
    hand-maintained ~10-entry tuple (see ``_derive_agent_helper_names``),
    that duplication would scale to one near-identical copy per entry in
    ``_derive_agent_helper_target_map``'s live ``coordinator/bin/`` scan —
    call it for the current count rather than trusting a frozen figure here.
    The ladder
    is extracted ONCE into ``_resolve_claude_klabauter.py`` (installed alongside every
    forwarder in the same shim dir — see ``rm_family`` in
    ``_install_bin_resolvers``); this body is now the ~6-line import/call
    shim the extraction leaves behind, matching the ``claude-home``/
    ``_claude_home.py`` co-located-impl precedent already used elsewhere in
    this install chain.

    ``b644d5a9`` (DoE, 2026-07-22) relocated DoE-claude's entire executable
    surface into claude-klabauter's own ``coordinator/bin/`` — the forwarder
    this replaces still exec'd the now-empty DoE-side ``coordinator/bin/``
    and every one of the 7 agent-helper CLIs was rc=126 in the field
    (claude-central-em memo,
    cross-repo/inbox/2026-07-22-claude-central-em-forwarder-template-still-execs-dead-doe-bin.md).

    See ``_resolve_claude_klabauter.py``'s own module docstring for the full ladder
    contract (registry-key-then-sentinel resolution rungs, `coordinator/bin`
    composition, `..`-traversal guard, on-disk existence checks, executable
    sentinel probe, distinct fail-loud messages) and for why the old
    `.doe-root`/`CLAUDE_PLUGIN_ROOT` trust-prefix dance (`_cc_trusted` et
    al.) is deliberately NOT carried forward.

    NO `#!/bin/sh` polyglot trampoline line -- retired by the 2026-07-21 PM
    ruling (``coordinator/bin/tests/test_no_bin_polyglot_invariant.py``,
    ``test_no_bin_docstring_command_substitution.py``). The generated body's
    ``#``-comment header carries no module docstring today, but that is
    incidental to THIS template, not the gated invariant: the actual rule
    (re-scoped 2026-07-28) is narrower than "no docstring" -- a module
    docstring is fine as long as it carries no command-substitution span
    (a backtick pair or ``$(...)``), since module/file-top purpose
    docstrings are otherwise required at this structural boundary
    (CLAUDE.md Implementation Standards). Two independent reasons the
    trampoline itself stays retired, not one: (a) bash reads a triple-quoted
    docstring as live shell text on a mistaken `bash <file>` invocation and
    executes any backtick/``$(...)`` span inside it, which can hang forever
    if the span resolves to a stdin-blocking command -- a `#` comment is
    inert either way; (b) the trampoline's sh-shim re-exec costs
    ~326ms/invocation on Windows versus a direct python3 invocation (1306ms
    via the shim vs 980ms direct, byte-identical output -- measured by
    ``coordinator/bin/check-sh-suffix-polyglot.py``'s docstring,
    source-of-record ``state/audits/2026-07-20-sh-suffixed-python-
    trampolines.md`` in the DoE-claude clone, not this repo -- the path is
    qualified deliberately, and its absence here is not evidence it is
    missing), paid unconditionally on EVERY call regardless of whether
    hazard (a) is ever triggered. This function's template is installed once
    per forwarder in the set ``_derive_agent_helper_target_map`` derives from
    the live ``coordinator/bin/`` directory listing (351 at the time this was
    last measured against the live tree -- call the derivation for the
    current count, not this prose), so that tax is not one file's
    cost -- it is one cold-`bash.exe`-avoidance win per forwarder per
    install, on Windows, which CLAUDE.md treats as the primary platform."""
    content = f"""#!/usr/bin/env python3
# coordinator-claude bin forwarder for {name} — resolves claude-klabauter's
# `coordinator/bin/` directory via the co-located `_resolve_claude_klabauter.py`
# shim (the ratified resolve-claude-klabauter-bin contract, DoE-claude
# coordinator/snippets/resolve-claude-klabauter-bin.md) and execs `{target}` there.
# Regenerated verbatim on every install run — do not hand-edit.
# Spec backlink: cross-repo/inbox/2026-07-22-claude-central-em-forwarder-template-still-execs-dead-doe-bin.md
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
{_AGENT_FORWARDER_MARKER}  # noqa: E402

exec_cli("{target}")
"""
    if check_only:
        if dst.exists() and dst.read_text(encoding="utf-8") == content:
            print(f"[install-substrate] check: {dst.name} up to date -> {dst} (no-op)")
            return
        status = "stale" if dst.exists() else "absent"
        raise SubstrateFatalError(
            f"install-substrate: check failed: {dst.name} is {status} at {dst} "
            f"(would write forwarder)"
        )
    dst.write_text(content, encoding="utf-8")
    dst.chmod(dst.stat().st_mode | 0o111)


def _write_agent_cmd_forwarder(
    name: str, dst: Path, check_only: bool, *, python3_cmd_resolved_bin: str, target: str = ""
) -> None:
    """Generates the installed Windows ``.cmd`` half of an agent-helper
    forwarder pair. Invokes the CO-LOCATED Unix-half forwarder
    (``%~dp0<name>`` — i.e. ``dst``'s own sibling written by
    ``_write_agent_forwarder`` under the SAME installed ``name``), never the
    claude-klabauter-side ``<target>.py`` directly — the resolve-claude-klabauter-bin ladder
    must exist in exactly ONE place (``_resolve_claude_klabauter.py``, exec'd by the
    Unix half), so re-deriving claude-klabauter-root resolution in batch here would
    duplicate that ladder once per forwarder in
    ``_derive_agent_helper_target_map``'s live ``coordinator/bin/`` scan —
    call it for the current count rather than trusting a frozen figure here.

    ``name`` is the INSTALLED forwarder name — NOT
    ``_agent_cmd_dest_name(name)``'s stem-stripped ``.cmd`` filename. An
    installed name that carries its own suffix (e.g. a hypothetical
    ``foo.sh`` installing as ``foo.cmd`` — the shape the retired
    ``mint-deliverable-id.sh`` divergence used to exercise before that CLI's
    installed name was made extensionless) is exactly why this must stay
    `name`, not a re-derived stem: a naive ``%~dp0<stem>`` would target a
    Unix-half file that was never written under that name.

    Formerly this ``.cmd`` half was a VERBATIM COPY of claude-klabauter's own
    ``coordinator/bin/<stem>.cmd`` (only the destination filename was
    transformed — see the retired ``_agent_cmd_source_name``). That body
    targeted ``%~dp0<target>.py`` — correct in ``coordinator/bin/`` where
    the ``.py`` is co-located, but DEAD at the install destination, which
    ships only the 5 resolver files (``machine-local``, ``claude-home``,
    ``_resolve_claude_klabauter.py``, etc.) plus the generated Unix-half forwarders —
    never the claude-klabauter-side ``.py``/``.sh`` targets themselves. Every launcher
    in the (then-smaller) forwarder set derived from ``coordinator/bin/`` —
    see ``_derive_agent_helper_target_map`` for the current count — times 2
    install dirs, failed with "can't open file" on Windows. See
    cross-repo/inbox/2026-07-23-claude-central-em-cmd-forwarder-install-break.md
    § The short version.

    Because this body now GENERATES rather than copies, it also stops
    depending on a source-side ``.cmd`` twin existing at all — every
    installed forwarder gets a working launcher unconditionally (the prior
    copy-gated approach silently skipped Windows coverage for the ~8
    on-disk CLIs that never shipped a hand-authored ``.cmd`` twin, e.g.
    ``claude-doe``, the ``.js`` CLIs).

    ``python3_cmd_resolved_bin`` is the SAME interpreter path
    ``_resolve_baked_python_bin`` resolves for ``python3.cmd`` (``""`` when
    none was resolvable at install time) — substituted directly into the
    fast-path branch below, unlike the copied body this replaces, whose
    ``__PYTHON_BIN__`` placeholder was never substituted for agent-helper
    ``.cmd``s (the render leg that has since been withdrawn wrote only that
    one file, so its behavior says nothing about the forwarder-writing path
    documented here).
    That template comment claimed install-substrate substitutes it — a lie
    for every one of these files, though a benign one: the self-detecting
    ``if "!_py!"=="__PYTHON_BIN__" set "_py="`` guard cleared it and fell
    through to the ``where python.exe`` rung. Now that this body is
    generated (not copied), the substitution is done for real at generation
    time, matching the documented fast path instead of silently degrading
    to it.

    Interpreter-resolution ladder mirrors ``coordinator/bin/wsc-close.cmd``:
    baked ``__PYTHON_BIN__`` fast path -> host-local ``%LOCALAPPDATA%``
    resolution cache -> ``where python.exe`` (skipping the Microsoft Store
    App Execution Alias picker under ``WindowsApps``, and caching a
    successful hit for future invocations on this host) -> ``py -3`` ->
    fail-loud exit 127.

    The cache rung exists for DR-303 / the ``windows-interpreter-bake-is-
    empty`` bug: an install performed off-Windows (or via ``--setup-only``)
    bakes nothing (``_resolve_baked_python_bin`` is ``os.name``-gated), so
    every invocation fell through to the ``where``/``findstr`` ladder
    permanently — roughly 10 process spawns per op instead of 2. Caching
    under ``%LOCALAPPDATA%`` rather than the settings-home is deliberate:
    ``%LOCALAPPDATA%`` does not roam/sync between machines (unlike a
    Mac/Windows-synced ``~/.claude``), so a cached path can never carry the
    OTHER platform's interpreter the way a synced bake can — the exact
    poisoning this fixes, one level down. Guarded by the same ``if exist``
    self-heal as the bake rung: a stale or foreign cached path falls through
    to full re-resolution and re-caches. The write on a cache miss is a
    ``move`` (atomic same-volume rename) of a per-attempt temp file, never
    an in-place write, so a torn read is not reachable — see the write
    site's own comment for the concurrency argument. A stray temp file left
    behind by a `move` that lost the race (target open by a concurrent
    reader) is best-effort cleaned up (``rd /s /q``) after the move either
    way — a leaked `.tmp` never blocks a future write, since the temp
    DIRECTORY name is drawn fresh per attempt.

    Cross-dialect encoding: this cmd body writes/reads the cached path in
    the console's active codepage (``echo``/``set /p``); ``_write_agent_ps1_
    forwarder``'s body writes/reads UTF-8 without a BOM
    (``[System.IO.File]::WriteAllText``/``Get-Content``). Those two dialects
    do NOT share a cache file — each writes/reads its own
    ``%LOCALAPPDATA%\\coordinator\\`` file (``python-bin-cache.txt`` for
    cmd, ``python-bin-cache-ps1.txt`` for ps1) for exactly this reason: a
    single shared file would only round-trip for an ASCII interpreter path,
    silently corrupting the cache for a non-ASCII one (a codepage cannot be
    forced without spawning `chcp`/similar, which the zero-added-process
    contract on this path forbids — see the DR-303 rung's own paragraph
    above). The cost is that a host warms each dialect's cache
    independently rather than cross-warming; both still hit
    ``where python.exe``/``py -3`` on a cold cache exactly as before this
    mechanism existed, so cold-path behavior is unaffected.

    The baked rung is guarded by ``if exist``, matching DoE's
    ``templates/bin/python3.cmd``: the ladder falls through when the baked
    path is empty OR names something no longer on disk, so a *wrong* bake is
    self-healing rather than a permanent hard failure. Without the exist
    check, only an EMPTY bake fell through and a stale/foreign baked path was
    an unrecoverable ``rc=3`` ("The system cannot find the path specified") —
    the shape that bites hardest on a ``~/.claude`` synced between a Mac and
    a Windows box, where every launcher carries the OTHER platform's
    interpreter path and neither deleting nor regenerating can be correct on
    both machines at once. Falling back on non-existence is the only repair
    that is right on whichever platform is actually running the forwarder.
    ``set "_py="`` after the guard keeps a rejected bake from leaking into
    the later rungs.

    ``target`` is the on-disk filename in claude-klabauter's ``coordinator/bin/`` this
    forwarder ultimately reaches (the same value the Step-3b call site's
    ``agent_helper_target_map`` loop already holds) — used ONLY to gate
    `_agent_cmd_raw_cmdline_block`'s emission per `_RAW_CMDLINE_TARGETS`, not
    interpolated into the body itself (the Unix-half forwarder re-derives its
    own target; duplicating that here would put the ladder in a second
    place). Defaults to ``""``, which is never a member of
    `_RAW_CMDLINE_TARGETS` — a caller that omits it (every pre-existing
    caller of this function, none of which know about the raw-cmdline
    mechanism) keeps the gate closed and its output byte-identical to before
    this parameter existed (AC1). The one real caller that needs the gate
    open (Step 3b) passes its resolved target explicitly.
    """
    py = python3_cmd_resolved_bin or ""
    raw_cmdline_block = _agent_cmd_raw_cmdline_block(target)
    # Review: staff-eng (Finding 2) -- mirrors gen-launcher-shim.py::
    # render_cmd's own fix: cleans up the raw-cmdline capture dir on every
    # exit path (interpreter-cascade failure included), not just the happy
    # path recover_windows_argv itself cleans up after a successful read.
    # Empty for targets outside _RAW_CMDLINE_TARGETS, so their body stays
    # byte-identical.
    raw_cmdline_cleanup = (
        '2>nul rd /s /q "%_LAUNCHER_RAW_CMDLINE_DIR%"\n'
        if target in _RAW_CMDLINE_TARGETS
        else ""
    )
    content = f"""@echo off
setlocal
REM Windows launcher for {name} -- batch-direct exec of the co-located
REM Unix-half forwarder ("%~dp0{name}"), which resolves claude-klabauter's
REM coordinator/bin/ via _resolve_claude_klabauter.py and execs the real target
REM there. {_AGENT_CMD_FORWARDER_MARKER} on every install run --
REM do not hand-edit.
REM Spec backlink: cross-repo/inbox/2026-07-23-claude-central-em-cmd-forwarder-install-break.md
REM No `enabledelayedexpansion`: with it on, cmd.exe scans the WHOLE command
REM line -- including whatever %* substitutes in -- for `!...!` tokens before
REM running it, silently mangling any forwarded argument containing a literal
REM `!` (commit messages, JSON payloads, ...). Each interpreter rung below is
REM isolated behind its own `goto` label instead, so %ERRORLEVEL% is read
REM outside any parenthesized block (fresh at that point, not frozen at
REM block-parse-time) with no delayed expansion needed.
{raw_cmdline_block}set "_py={py}"
if not "%_py%"=="" if exist "%_py%" goto :run_baked
set "_py="

REM Host-local resolution cache (DR-303 / windows-interpreter-bake-is-empty):
REM lives under %LOCALAPPDATA%, which never syncs between machines, so it
REM cannot be poisoned by a Mac/Windows-synced settings-home the way a bake
REM can. Guarded by `if exist`/non-empty exactly like the bake rung above --
REM self-heals the same way when the cached path is stale or foreign.
REM Review: staff-eng (Finding 4) -- `if exist`/non-empty is an EXISTENCE
REM check, not a content validator: the cache file lives under
REM %LOCALAPPDATA%, writable by this user and admins only (the same trust
REM boundary %LOCALAPPDATA% itself already carries), so this is not
REM cross-user exploitable. The WindowsApps reject below matches both
REM writers' own filter so a cached App Execution Alias path can't slip
REM back in through the read side.
if not defined LOCALAPPDATA goto :skip_cache_read
set "_cachefile=%LOCALAPPDATA%\\coordinator\\python-bin-cache.txt"
if not exist "%_cachefile%" goto :skip_cache_read
set "_cached="
set /p _cached=<"%_cachefile%"
if "%_cached%"=="" goto :skip_cache_read
set "_cached=%_cached:"=%"
REM Review: review-integrator (F1) -- the prior piped-substring-search form
REM this replaces spawned a child cmd.exe (for the pipe) plus a second
REM process on every cache HIT, contradicting the zero-added-process
REM contract this whole rung exists for. cmd.exe's `%VAR:search=replace%`
REM substitution performs a CASE-INSENSITIVE search for `search` (a
REM documented cmd.exe quirk, not an assumption) and is parser-level -- no
REM process, no enabledelayedexpansion needed. `_cachedtest` differs from
REM `_cached` iff "WindowsApps" (any case) was found and substituted out.
set "_cachedtest=%_cached:WindowsApps=%"
if not "%_cachedtest%"=="%_cached%" goto :skip_cache_read
if not exist "%_cached%" goto :skip_cache_read
set "_py=%_cached%"
goto :run_baked
:skip_cache_read

for /f "delims=" %%i in ('where python.exe 2^>nul') do (
    echo %%i| findstr /I /C:"\\WindowsApps\\" >nul
    if errorlevel 1 (
        set "_py=%%i"
        goto :cache_and_run_baked
    )
)

where py >nul 2>&1
if not errorlevel 1 goto :run_py3

echo [{name}] ERROR: no Python interpreter found (python.exe / py -3). 1>&2
echo [{name}] Install Python: https://www.python.org/downloads/windows/ 1>&2
{raw_cmdline_cleanup}exit /b 127

:cache_and_run_baked
REM Persist the resolved interpreter for future invocations on THIS host.
REM Every writer resolves the same `_py` value (deterministic per machine),
REM so a write-write race can only ever race identical content into the
REM target. Review: staff-eng (Finding 0) -- the write happens inside a
REM per-writer temp DIRECTORY, not a bare %RANDOM%%RANDOM% filename: mkdir
REM is atomic (a second `mkdir` of the same name fails outright, no
REM exists-then-write race window), whereas %RANDOM% is seeded once per
REM cmd.exe at one-second resolution, so two writers started in the SAME
REM second draw the IDENTICAL %RANDOM% sequence and would share a temp
REM path -- see gen-launcher-shim.py::_cmd_raw_cmdline_block's docstring
REM for the incident this mirrors. With a genuinely private temp dir, the
REM target is mutated only via `move`, an atomic same-volume rename, never
REM an in-place write, so a reader can never observe a torn file. A losing
REM writer's `move` silently no-ops (`>nul 2>nul`): no retry, no wait,
REM steady-state cost stays zero extra processes either way. No spawned
REM process (wmic, an out-of-dialect shell, ...) is used to fetch a PID or
REM GUID -- that would itself add a process hop on this path; three unrolled `mkdir`
REM attempts (bounded, not an unbounded retry loop) is what actually
REM guarantees uniqueness, matching the raw-cmdline-capture mechanism.
if not defined LOCALAPPDATA goto :run_baked
set "_cachedir=%LOCALAPPDATA%\\coordinator"
if exist "%_cachedir%\\" goto :cache_write
mkdir "%_cachedir%" 2>nul
:cache_write
set "_tmpdir=%_cachedir%\\python-bin-cache.%RANDOM%%RANDOM%%RANDOM%.tmp"
2>nul mkdir "%_tmpdir%"
if not errorlevel 1 goto :cache_write_got_dir
set "_tmpdir=%_cachedir%\\python-bin-cache.%RANDOM%%RANDOM%%RANDOM%.tmp"
2>nul mkdir "%_tmpdir%"
if not errorlevel 1 goto :cache_write_got_dir
set "_tmpdir=%_cachedir%\\python-bin-cache.%RANDOM%%RANDOM%%RANDOM%.tmp"
2>nul mkdir "%_tmpdir%"
if errorlevel 1 goto :run_baked
:cache_write_got_dir
set "_tmpfile=%_tmpdir%\\python-bin-cache.tmp"
REM The cache file holds a BARE path, not a quoted one. `echo "%_py%">file`
REM wrote the quotes literally, so the file's content was not itself a usable
REM path -- only this forwarder's own reader could consume it, and only because
REM the read rung above strips quotes with `%_cached:"=%`. Any other consumer,
REM including the install-contract test that asserts the cached value points at
REM an interpreter that exists, saw a path that could never resolve.
REM Leading redirect (`>"file" echo ...`) rather than trailing: a trailing
REM redirect after an unquoted value binds the final character of the value to
REM the redirect operator, and it is also what forces the quoting that caused
REM this. No trailing space is emitted this way either.
>"%_tmpfile%" echo %_py%
move /y "%_tmpfile%" "%_cachefile%" >nul 2>nul
2>nul rd /s /q "%_tmpdir%"
goto :run_baked

:run_baked
"%_py%" "%~dp0{name}" %*
{raw_cmdline_cleanup}exit /b %ERRORLEVEL%

:run_py3
py -3 "%~dp0{name}" %*
{raw_cmdline_cleanup}exit /b %ERRORLEVEL%
"""
    if check_only:
        if dst.exists() and dst.read_text(encoding="utf-8") == content:
            print(f"[install-substrate] check: {dst.name} up to date -> {dst} (no-op)")
            return
        status = "stale" if dst.exists() else "absent"
        raise SubstrateFatalError(
            f"install-substrate: check failed: {dst.name} is {status} at {dst} "
            f"(would write forwarder)"
        )
    dst.write_text(content, encoding="utf-8")


def _agent_ps1_dest_name(cmd_dest_name: str) -> str:
    """The installed `.ps1` sibling's filename for a given RESOLVED `.cmd`
    destination name (a value of ``_resolve_agent_cmd_dest_collisions``'s
    return map) -- same stem, `.ps1` suffix, mirroring
    ``_agent_cmd_dest_name``'s stem-stripping so an extension-carrying
    installed name (e.g. ``foo.sh`` -> ``foo.cmd``) never produces a
    malformed ``foo.cmd.ps1``. Deliberately keyed off the `.cmd` dest name
    rather than re-deriving from the installed name directly, so the two
    legs can never disagree on which bare name they occupy -- this is also
    exactly the convention ``_sweep_orphaned_agent_helpers``'s
    ``protected_names`` complement already assumes (``Path(n).stem +
    ".ps1"`` for every protected `.cmd` dest name)."""
    return Path(cmd_dest_name).stem + ".ps1"


def _write_agent_ps1_forwarder(
    name: str, dst: Path, check_only: bool, *, python3_cmd_resolved_bin: str
) -> None:
    """Generates the installed Windows ``.ps1`` half of an agent-helper
    forwarder pair -- the second managed launcher class (see
    docs/plans/2026-08-07-ps1-launcher-class-and-fail-closed-policy-gate.md).
    Mirrors ``_write_agent_cmd_forwarder`` exactly in shape and intent: same
    co-located-Unix-half-invocation strategy (``$_here/<name>``, never the
    claude-klabauter-side ``<target>.py`` directly, so the resolve-claude-klabauter-bin ladder
    stays in exactly one place), same baked-``python3_cmd_resolved_bin``
    fast path with a ``Test-Path`` existence guard so a bake naming a
    now-missing interpreter self-heals instead of hard-failing (the same
    Mac/Windows-sync hazard ``_write_agent_cmd_forwarder``'s docstring
    documents), same host-local ``%LOCALAPPDATA%`` resolution-cache rung
    (DR-303 — see that function's docstring for the full rationale; this
    dialect writes/reads its OWN cache file,
    ``<LOCALAPPDATA>/coordinator/python-bin-cache-ps1.txt`` — a SEPARATE
    file from the cmd leg's ``python-bin-cache.txt``, never shared, because
    the two dialects write/read in different encodings (console codepage
    vs UTF-8 without BOM) and a single shared file only round-trips for an
    ASCII interpreter path — see that function's docstring "Cross-dialect
    encoding" paragraph for the full rationale), same ``where
    python.exe``-equivalent / ``py -3`` / fail-loud
    exit-127 ladder. Every cache step here is an in-process cmdlet
    (``Test-Path``, ``Get-Content``, ``New-Item``, ``[System.IO.File]``,
    ``Move-Item``) — no new process on the steady-state path.

    This is deliberately NOT ``gen-launcher-shim.py::render_ps1`` and does
    not call it -- that function is the GENERATOR's source-side path for
    ``coordinator/bin/`` itself and its emitted body carries no substrate
    marker at all. Calling it here would ship a ``.ps1`` carrying
    ``_LEGACY_CMD_MARKER`` and no ``_AGENT_PS1_FORWARDER_MARKER``, making
    every installed `.ps1` permanently unsweepable by
    ``_sweep_orphaned_agent_helpers``'s marker-branch check -- precisely
    the hazard that check exists to close (see this module's own marker
    constants block above and the plan's Problem §1 / Anti-scope).

    ``name`` is the INSTALLED forwarder name (not a re-derived stem) for
    the same reason ``_write_agent_cmd_forwarder`` takes it that way -- see
    that function's docstring.

    shell-doc-ok: the expansion quoted above is the emitted PowerShell body's
    own variable, reproduced verbatim because this docstring specifies what
    that script literally contains; re-rendering it in prose would describe a
    forwarder this function does not write.
    """
    py = python3_cmd_resolved_bin or ""
    content = f"""# PowerShell launcher for {name} -- direct exec of the co-located
# Unix-half forwarder ("$_here/{name}"), which resolves claude-klabauter's
# coordinator/bin/ via _resolve_claude_klabauter.py and execs the real target
# there. {_AGENT_PS1_FORWARDER_MARKER} on every install run --
# do not hand-edit.
# Spec backlink: pln-second-managed-launcher-class-aea900
$ErrorActionPreference = 'Stop'
$_here = Split-Path -Parent $MyInvocation.MyCommand.Path
$_entry = Join-Path $_here '{name}'
$_pybin = '{py}'
if ($_pybin -ne '' -and (Test-Path -LiteralPath $_pybin)) {{
    & $_pybin $_entry @args
    exit $LASTEXITCODE
}}
# Host-local resolution cache (DR-303 / windows-interpreter-bake-is-empty):
# %LOCALAPPDATA% never syncs between machines, unlike the settings-home a
# bake is written into, so it cannot be poisoned by a Mac/Windows-synced
# home the way a bake can. Mirrors the bake's own Test-Path self-heal: a
# cached path that is stale or foreign falls through to re-resolution.
# Every step here is in-process (no new spawn on the steady-state path).
$_cachefile = $null
if ($env:LOCALAPPDATA) {{
    $_cachefile = Join-Path $env:LOCALAPPDATA 'coordinator\\python-bin-cache-ps1.txt'
    if (Test-Path -LiteralPath $_cachefile) {{
        $_cached = $null
        try {{ $_cached = Get-Content -LiteralPath $_cachefile -TotalCount 1 -ErrorAction SilentlyContinue }} catch {{}}
        if ($_cached -and (Test-Path -LiteralPath $_cached)) {{
            & $_cached $_entry @args
            exit $LASTEXITCODE
        }}
    }}
}}
$_py = Get-Command python.exe -ErrorAction SilentlyContinue | Where-Object {{ $_.Source -notlike '*\\WindowsApps\\*' }} | Select-Object -First 1
if ($_py) {{
    if ($_cachefile) {{
        # Persist for future invocations on THIS host. Every writer resolves
        # the same value (deterministic per machine), so a write-write race
        # can only race identical content -- and the target is only ever
        # mutated via Move-Item -Force (an atomic same-volume rename, never
        # an in-place write), so a reader can never observe a torn file. A
        # losing writer's move is swallowed (SilentlyContinue): no retry, no
        # wait, no added steady-state process either way.
        try {{
            $_cachedir = Split-Path -Parent $_cachefile
            if (-not (Test-Path -LiteralPath $_cachedir)) {{
                New-Item -ItemType Directory -Path $_cachedir -Force -ErrorAction SilentlyContinue | Out-Null
            }}
            $_tmpfile = Join-Path $_cachedir ([System.Guid]::NewGuid().ToString('N') + '.tmp')
            [System.IO.File]::WriteAllText($_tmpfile, $_py.Source)
            Move-Item -LiteralPath $_tmpfile -Destination $_cachefile -Force -ErrorAction SilentlyContinue
        }} catch {{}}
    }}
    & $_py.Source $_entry @args
    exit $LASTEXITCODE
}}
$_pyl = Get-Command py -ErrorAction SilentlyContinue
if ($_pyl) {{
    & $_pyl.Source -3 $_entry @args
    exit $LASTEXITCODE
}}
[Console]::Error.WriteLine('[{name}] ERROR: no Python interpreter found (python.exe / py -3).')
[Console]::Error.WriteLine('[{name}] Install Python: https://www.python.org/downloads/windows/')
exit 127
"""
    if check_only:
        if dst.exists() and dst.read_text(encoding="utf-8") == content:
            print(f"[install-substrate] check: {dst.name} up to date -> {dst} (no-op)")
            return
        status = "stale" if dst.exists() else "absent"
        raise SubstrateFatalError(
            f"install-substrate: check failed: {dst.name} is {status} at {dst} "
            f"(would write forwarder)"
        )
    dst.write_text(content, encoding="utf-8")


# Names already installed by ml_family/ch_family/the coordinator-settings-home
# and platform-localize install lines (all sourced from DoE-claude's
# templates/bin or claude-klabauter's coordinator/lib/claude-home, NOT from
# coordinator/bin/) — these run BEFORE the derived agent-helper forwarder
# loop in _install_bin_resolvers, so a same-named entry surviving into the
# derived set would silently overwrite the real, already-correct install
# with a resolve-claude-klabauter-bin forwarder stub. Some of these names ALSO exist
# as unrelated files inside coordinator/bin/ (e.g. a `claude-home` and a
# `machine-local` entry both live there) — exclude them unconditionally
# rather than relying on the directory scan to never collide.
#
# ``claude-doe`` joins them on WINDOWS ONLY, and for a different reason: it is
# the one entry here that launches an INTERACTIVE TUI. On Windows the generic
# forwarder emitted by this loop reaches ``claude.exe`` three hops deep
# (cmd.exe -> python.exe -> python.exe -> claude.exe), and that nesting corrupts
# the console input mode — xterm focus-report sequences (ESC[I / ESC[O, DECSET
# 1004) stop being consumed and leak into input as literal ``[I``/``[O``,
# keystrokes misroute, and the host shell's prompt is left corrupted after exit.
# The interactive process must be a DIRECT child of the invoking shell.
# DoE-claude's ``gen-claude-doe-launcher.py`` renders a purpose-built
# ``claude-doe.{cmd,ps1}`` pair into ``~/.local/bin`` that keeps the launch
# shallow, but settings-home bin is PATH-prepended ahead of ``~/.local/bin``, so
# the generic forwarder SHADOWED it and won every bare ``claude-doe``
# invocation. Excluding the name here leaves the purpose-built launcher as the
# only copy, and the orphan prune below removes the shadow left by earlier runs.
#
# The guard is ``os.name`` because that purpose-built launcher is Windows-only —
# ``gen-claude-doe-launcher.py`` exits 0 without writing on macOS/Linux. On
# POSIX this forwarder IS the ``claude-doe`` CLI and must keep being installed;
# excluding it unconditionally would remove the command entirely there. POSIX
# also has no equivalent defect: its shim reaches the Python wrapper, which
# ``os.execv``s claude in place — a genuine process replacement, adding no
# intermediate process at all.
_AGENT_HELPER_RESERVED_NAMES = frozenset(
    {
        "machine-local",
        "claude-home",
        "resolve-coordinator-clone",
        "coordinator-settings-home",
        "platform-localize",
    }
    | ({"claude-doe"} if os.name == "nt" else set())
)

# Non-CLI data/doc file extensions that can appear alongside real CLIs in
# coordinator/bin/ (schema/manifest/baseline files) — never a forwarder
# candidate regardless of exec bit.
_AGENT_HELPER_DATA_SUFFIXES = frozenset({".md", ".toml", ".yaml", ".yml", ".txt"})


def _is_pytest_infrastructure(filename: str) -> bool:
    """True for a pytest collection artifact living in ``coordinator/bin/`` —
    a ``test_*.py`` module or ``conftest.py`` — which is a test file, never a
    CLI, and must never derive an installed forwarder.

    Why this is separate from the existing ``.test.py`` exclusion: that rule
    covers the ``<cli>.test.py`` companion-file convention (a test named
    AFTER its subject). This covers pytest's OWN ``test_*.py`` /
    ``conftest.py`` discovery convention, which arrived in this directory
    later — ``coordinator/bin/`` was wired into ``testpaths`` on 2026-07-25
    (CLAUDE.md § Build & Test) and now holds ~42 ``test_*.py`` modules plus a
    tree-wide ``conftest.py``. Nothing taught the forwarder derivation about
    that, so every one of them shipped as a bareword PATH entry: an operator
    (or an agent) typing ``test_sweep_boot`` got a forwarder that exec'd a
    pytest module directly, and ``conftest`` — a name with no ``main()`` at
    all — became an installed CLI. They also drowned the
    ``plugin_health.forwarder_drift`` probe, whose whole job is naming the
    handful of genuinely-missing CLIs.

    Negative-spec: this is a FILENAME-convention predicate, not a
    "does it look like a test" heuristic. A real CLI whose name legitimately
    begins with ``test-`` (hyphen, the CLI convention here) is untouched —
    only the underscore-prefixed pytest form and ``conftest.py`` itself
    match."""
    return filename == "conftest.py" or (filename.startswith("test_") and filename.endswith(".py"))


def _derive_agent_helper_target_map(agent_bin: Path) -> "dict[str, str]":
    """Derive the installed-forwarder name -> on-disk-target-filename map
    from claude-klabauter's own ``coordinator/bin/`` directory listing. This is the
    single source of truth both ``_derive_agent_helper_names`` (installed
    NAME set, for uninstall bookkeeping) and ``_install_bin_resolvers``
    (forwarder generation, which needs the real on-disk filename to exec)
    derive from — replacing the former hand-maintained
    ``_AGENT_HELPER_NAMES`` tuple (~10 entries) — adding a new CLI to
    ``coordinator/bin/`` now requires no edit here.

    Negative-spec: the installed name is ``.py``-stripped (see stem-dedup
    below) but the exec TARGET must stay the real on-disk filename —
    conflating the two (re-deriving the target from the installed name
    verbatim) silently produces a nonexistent extensionless target for
    every ``.py``-suffixed CLI. This mapping exists so the forwarder writer
    never has to re-derive; it just execs the value this scan already
    resolved.

    Collision precedence: two on-disk files deriving the same installed name
    is fatal (see the scan loop below) EXCEPT one specific shape — an
    extensionless CLI (``<name>``) and its ``.py`` twin (``<name>.py``) both
    present. That shape is not currently realized anywhere in
    ``coordinator/bin/`` — its two known live instances
    (``aggregate-chain-loe``/``aggregate-chain-loe.py``,
    ``audit-roadmap``/``audit-roadmap.py``, both genuine duplicate-CLI
    leftovers from the strangler port trampolining the same underlying
    ``coordinator_core`` module) were deduped at source: the extensionless
    sibling was deleted and the ``.py`` twin kept, per PM ruling. The
    precedence rule below is retained as defense-in-depth against the NEXT
    occurrence of this shape (a future port leaving both an extensionless
    and a ``.py`` trampoline behind again) — treating a fresh instance as
    fatal would hard-break install off this tree exactly as it would have
    here. The ``.py`` twin wins and a warning names both files — see the
    scan loop for the full rationale. Covered today only by the synthetic
    fixture in ``test_forwarder_trust_guard.py``
    (``test_derive_agent_helper_target_map_extensionless_and_py_twin_prefers_py``);
    the real-tree regression test no longer exercises this branch — see
    that test's own docstring.

    Exclusions and stem-dedup rules below apply to KEYS (installed names);
    each key's mapped value is the corresponding on-disk filename that
    survived those same exclusions.

    Exclusions: ``_``-prefixed and dotfile names, any subdirectory (covers
    ``lib/``, ``fixtures/``, ``tests/``, ``test-fixtures/``, ``repomap/``,
    ``install-health/``, ``__pycache__/`` uniformly — a bare-name CLI never
    lives one level deep), ``*.test.py``/``*.test.js``/``*.test.cmd``/
    ``*.test.sh`` companion test files, pytest's own collection artifacts
    (``test_*.py`` and ``conftest.py`` — a SECOND, distinct rule; see
    ``_is_pytest_infrastructure`` for why the ``.test.py`` rule above does
    not cover them), ``*.cmd``/``*.ps1`` twins (the
    installed ``.cmd`` half is GENERATED by ``_write_agent_cmd_forwarder``,
    never copied from an on-disk twin — see that function's docstring —
    so a source-side ``.cmd``/``.ps1`` file is excluded from the map,
    never independently-installed names), non-CLI data/doc files
    (``_AGENT_HELPER_DATA_SUFFIXES``), and
    ``_AGENT_HELPER_RESERVED_NAMES`` (already installed by a different
    family — see that set's own docstring for the collision it prevents).

    Stem-dedup: a CLI commonly ships as a ``<name>.py`` + ``<name>.cmd``
    (+ optionally ``<name>.ps1``) triplet, or as an extensionless polyglot
    with a ``.cmd`` twin (``claude-doe``, ``verify-coverage``,
    ``lint-frontmatter.js`` — note the latter's "extension" IS its bareword
    identity, there is no separate ``lint-frontmatter`` file to collide
    with). Only the ``.py`` suffix is stripped to form the installed name;
    every other suffix (``.js``, extensionless) is kept verbatim, since
    those ARE the installed/invoked name on this tree.

    ``mint-deliverable-id.py`` no longer diverges from this rule (it was
    formerly pinned to the asymmetric installed name
    ``mint-deliverable-id.sh``, which had no on-disk ``.sh`` twin in
    coordinator/bin/ — see git history for that retired special-case); it
    now derives its installed name via the ordinary stem-strip path like
    every other ``.py``-suffixed CLI, resolving to the extensionless
    ``mint-deliverable-id``.
    """
    if not agent_bin.is_dir():
        return {}

    mapping: "dict[str, str]" = {}
    for entry in agent_bin.iterdir():
        if entry.is_dir():
            continue
        n = entry.name
        if n.startswith("_") or n.startswith("."):
            continue
        if n.endswith((".test.py", ".test.js", ".test.cmd", ".test.sh")):
            continue
        if _is_pytest_infrastructure(n):
            continue
        if entry.suffix in (".cmd", ".ps1"):
            continue
        if entry.suffix in _AGENT_HELPER_DATA_SUFFIXES:
            continue
        installed_name = entry.stem if entry.suffix == ".py" else n
        if installed_name in mapping and mapping[installed_name] != n:
            existing = mapping[installed_name]
            py_name = f"{installed_name}.py"
            is_extensionless_vs_py_twin = {existing, n} == {installed_name, py_name}
            py_twin = py_name if is_extensionless_vs_py_twin else None
            if py_twin is not None:
                # Extensionless-vs-.py-twin collision (formerly realized by
                # aggregate-chain-loe + aggregate-chain-loe.py, audit-roadmap
                # + audit-roadmap.py -- both deduped at source, extensionless
                # sibling deleted; see the docstring above): a duplicate-CLI
                # shape left over from the strangler port, where BOTH files
                # trampoline the same coordinator_core module. The .py twin
                # wins by an explicit, documented rule rather than silent
                # iteration-order luck -- it carries the gen-launcher-shim.py
                # --ensure-unix provenance line the extensionless sibling
                # lacks, and matches the .py-CLI convention every other
                # install-path decision already assumes.
                #
                # NEGATIVE SPEC -- this branch is NOT dormant. An earlier
                # version of this comment claimed it was "exercised only by
                # the synthetic fixture test (no live on-disk instance)";
                # that is false and cost real debugging time. It has a live
                # pair today (`coordinator-prepare-commit-msg`) and fires on
                # the CLEAN path, unprompted by any failure. Two consumers
                # already depend on knowing that: `forwarder_self_heal`
                # captures stdout+stderr so session boot does not print this
                # every time, and `settings_home_report` redirects stdout so
                # the JSON doctor probe's contract survives it. The `print`
                # below therefore reaches any caller that has not arranged
                # otherwise -- check the tree before assuming it is quiet.
                dropped = existing if py_twin == n else n
                print(
                    f"[install-substrate] WARNING: duplicate CLI pair for "
                    f"installed name {installed_name!r} in {agent_bin} -- "
                    f"{py_twin!r} and {dropped!r} both exist; installing "
                    f"{py_twin!r} (the .py twin) and ignoring {dropped!r}"
                )
                mapping[installed_name] = py_twin
                continue
            raise SubstrateFatalError(
                "install-substrate: agent-helper installed-name collision at "
                f"{installed_name!r} between on-disk files {mapping[installed_name]!r} "
                f"and {n!r} in {agent_bin} -- both derive the same installed name, "
                "so a silent Path.iterdir()-order overwrite would pick one "
                "arbitrarily. This is not the extensionless-CLI-vs-its-.py-twin "
                "shape (that has an explicit precedence rule above) -- it is "
                "some other collision shape with no established winner. "
                "Remediation: rename one of the two on-disk files so their "
                "derived installed names no longer collide."
            )
        mapping[installed_name] = n

    for reserved in _AGENT_HELPER_RESERVED_NAMES:
        mapping.pop(reserved, None)

    return mapping


def _derive_agent_helper_names(agent_bin: Path) -> "tuple[str, ...]":
    """Installed-forwarder NAME set (keys of ``_derive_agent_helper_target_map``,
    sorted) — used where only the installed name is needed (uninstall
    bookkeeping in ``uninstall_legs.py``), not the on-disk exec target."""
    return tuple(sorted(_derive_agent_helper_target_map(agent_bin)))


def _sweep_orphaned_agent_helpers(
    dst_dir: Path,
    agent_helper_target_map: "dict[str, str]",
    agent_cmd_dest_map: "dict[str, str]",
    check_only: bool,
) -> None:
    """Delete agent-helper forwarder files THIS installer previously wrote
    into ``dst_dir`` but would no longer write on this run — the launcher
    orphans left behind when a CLI is deleted/renamed in ``coordinator/bin/``
    (the derived set shrinks) or when a same-installed-name collision flips
    which side wins the ``.cmd`` slot (``_resolve_agent_cmd_dest_collisions``
    — the loser's stale ``.cmd`` from a prior run keeps existing once it
    stops being the winner). Confirmed live: ``verify-cc-root-source-guard-
    sync.cmd`` pointing at a deleted ``.py``, which predates the current
    generator and carries ``_LEGACY_CMD_MARKER``, not
    ``_AGENT_CMD_FORWARDER_MARKER`` (a live dry-run against a real install
    caught the strict-marker-only version of this function missing exactly
    this shape).

    A pre-AC7 ``mint-deliverable-id.sh.cmd`` from the old double-suffix
    naming bug is a THIRD, older shape: a live install audit (C6,
    docs/plans/2026-08-10-entrypoint-gate-launcher-and-changed-only.md)
    found this exact file on disk, hand-authored before
    ``gen-launcher-shim.py`` existed to stamp ``_LEGACY_CMD_MARKER`` at
    all — it carries NEITHER marker, so it fell through this function
    silently forever until ``_PRE_MARKER_LEGACY_ORPHAN_NAMES`` (see that
    constant) started matching it by explicit name. Every OTHER real orphan
    in the fleet is legacy-marker-shaped, since the current generator
    regenerates its own marker on every run and so never goes stale on its
    own — this pre-marker name list exists for exactly the one older
    generation the marker mechanism cannot see.

    Provenance mechanism — POSITIVE content-marker identification, never a
    "not in the current derived set therefore delete" name heuristic, AND
    (for the legacy marker specifically) never marker-alone either:

    1. Marker match. A ``.cmd`` file must carry ``_AGENT_CMD_FORWARDER_
       MARKER`` (current generator) or ``_LEGACY_CMD_MARKER`` (retired
       copy-a-source-.cmd-verbatim approach); a ``.ps1`` file must carry
       ``_AGENT_PS1_FORWARDER_MARKER``; any other file must carry
       ``_AGENT_FORWARDER_MARKER``. ``_AGENT_FORWARDER_MARKER``/
       ``_AGENT_CMD_FORWARDER_MARKER``/``_AGENT_PS1_FORWARDER_MARKER`` are
       exclusive to this module's own f-string bodies (interpolated from
       the same constants the writers use, so they cannot drift) and appear
       in NO other family this module installs. ``_LEGACY_CMD_MARKER`` is
       NOT exclusive — it was stamped by
       ``coordinator/bin/gen-launcher-shim.py`` on every CLI's source-side
       ``.cmd`` AND ``.ps1``, including the legitimate,
       still-installed-every-run DoE templates ``platform-localize.cmd``/
       ``platform-localize.ps1`` and ``resolve-coordinator-clone.cmd``
       (confirmed on a live install), so a legacy-marker match is a
       necessary but not sufficient condition — and it is checked ONLY on
       the ``.cmd`` branch, never the ``.ps1`` branch: a static-family
       ``.ps1`` carrying the legacy marker survives via condition 2 below
       (membership in ``_static_bin_family_names()``), never via a
       legacy-marker allowance this branch doesn't grant.
    2. Absence from this run's complete write set. A name is eligible for
       sweep only if it is in NEITHER the dynamic agent-helper maps
       (``agent_helper_target_map``/``agent_cmd_dest_map``, ``.cmd`` case
       checked against ``.values()`` — the actual installed ``.cmd``
       filenames, so a collision loser is correctly excluded, unlike a
       ``.keys()`` check which is installed-NAME-keyed and would miss it,
       plus each of those names' ``.ps1`` sibling) NOR
       ``_static_bin_family_names()`` (every OTHER family's complete,
       statically-known name set — this is what keeps
       ``platform-localize.cmd``/``platform-localize.ps1``/
       ``resolve-coordinator-clone.cmd`` safe despite carrying the legacy
       marker: all three names are members of that set, so condition 2
       never holds for them).

    Both conditions must hold. An operator-hand-edited forwarder that
    strips the marker text is, by construction, no longer recognizable as
    ours and is correctly left alone — it has opted out of being
    regenerated/managed here at all. `_`-prefixed and dotfile entries
    (covers ``_machine_local.py``/``_claude_home.py``/``_resolve_claude_klabauter.py``)
    are excluded outright, mirroring ``_derive_agent_helper_target_map``'s
    own leading-``_`` exclusion.

    This makes the sweep idempotent by construction: once a file is
    removed, or once a name is (re)installed this run, condition 2 is never
    met again on the next run.
    """
    if not dst_dir.is_dir():
        # WRITE_SURFACE clause 13 (`_CLAUSE_ORPHAN_SWEEP`): the destination
        # doesn't exist yet, so this run never got far enough to determine
        # whether anything is orphaned — "never got there", NOT "resolved
        # to nothing". No journal row (leaves the clause unreported for
        # this run) rather than a misleading empty-tuple resolution.
        return
    protected_names = _static_bin_family_names() | set(agent_helper_target_map) | set(agent_cmd_dest_map.values())
    # Limb 2 of the `.ps1` launcher class (companion to the marker branch
    # above): every protected bare name's `.ps1` sibling is protected too,
    # so a currently-valid CLI's `.ps1` forwarder is never swept merely
    # because it doesn't (yet, or transiently) carry
    # `_AGENT_PS1_FORWARDER_MARKER`. This is a complement to the marker
    # check, not a substitute — protection alone would make `.ps1` orphans
    # unsweepable, which is why limb 1's marker branch above is what makes
    # deletion reachable at all.
    protected_names |= {Path(n).stem + ".ps1" for n in protected_names}
    orphans: "list[str]" = []
    removed: "list[WriteSurfaceEntry]" = []
    for entry in sorted(dst_dir.iterdir()):
        if not entry.is_file():
            continue
        name = entry.name
        if name.startswith("_") or name.startswith("."):
            continue
        if name in protected_names:
            continue
        try:
            text = entry.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if entry.suffix == ".cmd":
            if (
                _AGENT_CMD_FORWARDER_MARKER not in text
                and _LEGACY_CMD_MARKER not in text
                and name not in _PRE_MARKER_LEGACY_ORPHAN_NAMES
            ):
                continue
        elif entry.suffix == ".ps1":
            if _AGENT_PS1_FORWARDER_MARKER not in text:
                continue
        else:
            if _AGENT_FORWARDER_MARKER not in text:
                continue
        if check_only:
            orphans.append(name)
            print(f"[install-substrate] check: {name} is orphaned agent-helper forwarder at {entry} (would sweep)")
            continue
        blocked = _refuse_machine_mutation(
            str(entry), what=f"remove orphaned agent-helper forwarder {name}", check_temp_path=False,
        )
        if blocked:
            print(f"[install-substrate] REFUSED: {blocked}", file=sys.stderr)
            continue
        entry.unlink()
        removed.append(WriteSurfaceEntry(kind="file-path", path=str(entry), effect="delete"))
        print(f"[install-substrate] removed orphaned agent-helper forwarder {name} ({entry})")
    if not check_only:
        # A guard-refused entry never reached `entry.unlink()` above and is
        # correctly absent from `removed` — journaling only the deletes
        # that actually happened, per WRITE_SURFACE clause 13's contract.
        resolution_journal.record_resolution(_WRITER_ID, _CLAUSE_ORPHAN_SWEEP, removed)
    if check_only:
        if orphans:
            raise SubstrateFatalError(
                f"install-substrate: check failed: {len(orphans)} orphaned agent-helper "
                f"forwarder(s) in {dst_dir} ({', '.join(orphans)})"
            )
        print(f"[install-substrate] check: no orphaned agent-helper forwarders in {dst_dir} (no-op)")


def _run_hardware_audit(check_only: bool) -> None:
    """Step 3h: hardware concern audit — an in-process call into this same
    engine's native `coordinator_core.ops.detect_hardware`. Previously gated
    on `plugin_root/lib/detect-hardware.sh` existing on disk, a file-presence
    check that made no sense once the audit itself became a direct in-process
    Python call (the gate suppressed a working, file-independent audit any
    time the DoE-side `.sh` happened to be absent — which is unconditionally,
    post-b644d5a9's executable-surface relocation). Never raises — a failed
    audit degrades to a WARNING, matching the bash oracle's own non-fatal
    posture for this step."""
    if check_only:
        print("[install-substrate] would: run hardware audit (native detect_hardware op)")
        return
    from coordinator_core.ops.detect_hardware import main as _detect_hardware_main

    # detect_hardware's `machine-local set` children write straight to this
    # process's inherited fd (no_console_passthrough_kwargs, real fds, not
    # sys.stdout) — an unbuffered write that races every `print()` this
    # module and its callers made before this point. Redirected output is
    # block-buffered, so without this flush the whole block of "[install-
    # substrate] ..." / "[machine-local] ..." lines queued so far surfaces
    # AFTER the child's write instead of before it. detect_hardware.py flushes
    # around its OWN prints for the same reason; this flush covers everything
    # queued upstream of this call.
    #
    # NOT the only raw-fd `subprocess.run` site in this module: the fnm
    # curl-install leg's `subprocess.run(["bash", "-s", ...])` (below, the
    # fnm-provisioning function) also inherits the real fd with no
    # `capture_output`/`stdout=` kwarg. It needs no flush of its own only
    # because its immediately preceding `print(..., flush=True)` already
    # flushes independently, and `subprocess.run` blocks — nothing prints
    # after it returns for that flush to reorder against. A future raw-fd
    # `subprocess.run` site is not ruled out by this comment or that one;
    # each such site is safe (or not) on its own terms, not because this is
    # "the" raw-fd site in this module.
    sys.stdout.flush()
    hw_rc = _detect_hardware_main([])
    if hw_rc != 0:
        print("[setup] WARNING: hardware audit failed — re-run install or set hardware.* keys manually", file=sys.stderr)


def _register_hardware_concern(registry_live: Path) -> None:
    """Idempotent migration: ensure 'hardware' is registered in registry.toml's
    concerns array (inline or multiline form), preserving pre-existing
    entries. Pure-Python port of the bash inline-tomllib snippet (Step 3g) —
    no subprocess needed, this repo already runs under Python 3.11+."""
    try:
        import tomllib
    except ImportError:  # pragma: no cover — CI pins >=3.11
        print(
            f"[setup] WARNING: tomllib unavailable — cannot register 'hardware' "
            f"in concerns; add it manually to {registry_live}",
            file=sys.stderr,
        )
        return

    content = registry_live.read_text(encoding="utf-8")

    try:
        data = tomllib.loads(content)
    except Exception:
        return  # malformed — don't touch it

    concerns = data.get("concerns")
    if not isinstance(concerns, list):
        return
    if "hardware" in concerns:
        return

    inline_pat = re.compile(r"^(concerns\s*=\s*\[)([^\]]*?)(\])", re.MULTILINE | re.DOTALL)
    m = inline_pat.search(content)
    if m:
        inner = m.group(2)
        if "\n" in inner:
            lines = inner.rstrip().split("\n")
            last_line = lines[-1] if lines else ""
            indent = len(last_line) - len(last_line.lstrip())
            indent_str = " " * indent if indent else "  "
            insert = indent_str + '"hardware",\n'
            new_content = content[: m.end(2)] + insert + content[m.end(2):]
        else:
            inner_stripped = inner.strip()
            new_inner = (inner_stripped + ', "hardware"') if inner_stripped else '"hardware"'
            new_content = content[: m.start(2)] + new_inner + content[m.end(2):]
    else:
        section_pat = re.compile(r"^\[", re.MULTILINE)
        sm = section_pat.search(content)
        insert_line = 'concerns = ["hardware"]\n'
        if sm:
            new_content = content[: sm.start()] + insert_line + "\n" + content[sm.start():]
        else:
            new_content = content.rstrip("\n") + "\n" + insert_line

    try:
        parsed = tomllib.loads(new_content)
        new_concerns = parsed.get("concerns", [])
        if "hardware" not in new_concerns:
            raise ValueError("hardware not present after migration")
        orig_concerns = tomllib.loads(content).get("concerns", [])
        for c in orig_concerns:
            if c not in new_concerns:
                print(
                    f'[setup] ERROR: concern "{c}" was lost during migration — aborting write',
                    file=sys.stderr,
                )
                return
    except Exception as exc:
        print(f"[setup] WARNING: could not update {registry_live}: {exc}", file=sys.stderr)
        return

    tmp = registry_live.with_suffix(registry_live.suffix + f".tmp.{os.getpid()}")
    try:
        tmp.write_text(new_content, encoding="utf-8")
        os.replace(tmp, registry_live)
    except OSError as exc:
        if tmp.exists():
            tmp.unlink()
        print(f"[setup] WARNING: could not update {registry_live}: {exc}", file=sys.stderr)
        return

    print("[machine-local] registered hardware concern in registry.toml")


def _c10a_copy_one(src: Path, dst: Path) -> None:
    if not src.is_file():
        return
    if not dst.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    elif filecmp.cmp(src, dst, shallow=False):
        pass
    else:
        raise SubstrateFatalError(
            "install-substrate C10a: DIVERGENT whoami file — cannot safely relocate.\n"
            f"  Source : {src}\n  Dest   : {dst}\n"
            "  Remediation: manually reconcile the two files, then re-run install."
        )


_WHOAMI_EXCLUDE_DIRS = (
    "__pycache__", ".venv", ".pytest_cache", ".git", "build", "dist",
)


def _iter_whoami_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in _WHOAMI_EXCLUDE_DIRS and not d.endswith(".egg-info")
        ]
        for f in filenames:
            if f.endswith(".pyc") or f.endswith(".egg-link"):
                continue
            yield (Path(dirpath) / f).relative_to(root)


_REPO_ENV_IDENT_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def repo_key_to_env_var(machine_local_key: str) -> str:
    """Normalize a ``repos.<slug>`` machine-local key to its shell export
    name (``REPO_<SLUG>``), matching the bash ``tr 'a-z.-' 'A-Z__'``
    transform: strip the ``repos.`` prefix, uppercase, map ``.``/``-`` to
    ``_``.

    Port source: ``coordinator/templates/bin/claude-machine-local.sh``
    [DoE-claude repo] normalization comment block.
    """
    suffix = machine_local_key[len("repos."):] if machine_local_key.startswith("repos.") else machine_local_key
    table = str.maketrans("abcdefghijklmnopqrstuvwxyz.-", "ABCDEFGHIJKLMNOPQRSTUVWXYZ__")
    return "REPO_" + suffix.translate(table)


def resolve_repo_env_exports(
    keys: List[str],
    getter,
    preexisting_env: Optional[dict] = None,
) -> "tuple[dict, list, list]":
    """Pure-Python port of ``claude-machine-local.sh``'s per-key resolution
    loop (the shell script itself remains sourced-only — nothing can
    ``source`` a ``.py`` into a caller's live shell — so this function does
    NOT replace it; it gives non-shell Python callers, e.g. diagnostics or a
    future installer self-check, the same key-resolution semantics without
    shelling out to bash).

    Args:
        keys: machine-local keys already filtered to the ``repos.`` prefix
            (mirrors the bash ``machine-local keys | grep -E '^repos\\.'``
            step — filtering is the caller's job, not this function's).
        getter: callable ``(key: str) -> tuple[int, str]`` returning
            ``(rc, value)`` exactly as ``machine-local get <key>`` would via
            its exit code + stdout (0=resolved, 1=clean absence, >=2=error).
        preexisting_env: mapping to check for the §4b idempotency gate
            (deliberately pre-set overrides are honoured, not clobbered).
            Defaults to ``os.environ``.

    Returns:
        ``(exports, warnings, errors)`` — ``exports`` maps env-var name to
        resolved value (never an empty string — see negative-spec below);
        ``warnings`` covers non-conformant identifiers and rc=1 clean
        absences; ``errors`` covers rc>=2 operational failures.

    Negative-spec: rc=1 (clean absence) is skipped, not exported as ``""``
    — exporting an empty string would corrupt ``$REPO_X/subdir`` path joins
    to ``/subdir``, reproducing the bash script's own explicit guard.
    """
    env = os.environ if preexisting_env is None else preexisting_env
    exports: dict = {}
    warnings: list = []
    errors: list = []
    for key in keys:
        var = repo_key_to_env_var(key)
        if not _REPO_ENV_IDENT_RE.match(var):
            warnings.append(
                f"claude-machine-local: warning: skipping key '{key}' — "
                f"produces non-conformant shell identifier '{var}'"
            )
            continue
        if env.get(var):
            continue
        rc, value = getter(key)
        if rc == 0:
            exports[var] = value
        elif rc == 1:
            warnings.append(
                f"claude-machine-local: warning: '{key}' not resolved by "
                f"ladder — ${var} not exported"
            )
        else:
            errors.append(
                f"claude-machine-local: error: machine-local reader failed "
                f"for '{key}' (rc={rc})"
            )
    return exports, warnings, errors


class _BakedPythonBinReason(enum.Enum):
    """Why ``_resolve_baked_python_bin_detail`` returned the value it did.

    Exists because ``""`` alone is ambiguous: ``_install_bin_resolvers``'s
    AC8 fail-loud gate must fire ONLY on ``UNRESOLVED`` -- no interpreter
    reachable by any route, ``resolve_python_bin()``'s own "" -- and never on
    ``LAUNCHER_ONLY``/``NO_CONSOLE_SIBLING``/``RESOLUTION_ERROR``, all three
    of which have a working ``py -3`` runtime fallback baked into the shim
    template and are, per this module's own docstring below, "a
    *contractually valid* result, not a failure". Collapsing all four into a
    bare ``""`` was C2's actual bug (docs/plans/2026-08-16-registry-read-
    stops-costing-a-process.md AC8): it hard-failed install on a box whose
    only Python entry point is the ``py`` launcher, a normal, supported
    configuration that previously degraded gracefully.
    """

    RESOLVED = "resolved"
    NOT_WINDOWS = "not-windows"
    LAUNCHER_ONLY = "launcher-only"
    NO_CONSOLE_SIBLING = "no-console-sibling"
    RESOLUTION_ERROR = "resolution-error"
    UNRESOLVED = "unresolved"


def _resolve_baked_python_bin() -> str:
    """Resolve the interpreter to bake into ``python3.cmd`` (Step 3a).

    Thin wrapper over ``_resolve_baked_python_bin_detail`` for the (common)
    callers that only need the bake value, never the reason behind a ""
    result -- same resolution, same return value, unchanged by the AC8 fix
    below.

    Returns the absolute interpreter path to bake in, or ``""`` when none should
    be baked -- this is a *contractually valid* result, not a failure: the
    ``__PYTHON_BIN__`` template branches on an empty value and falls back to the
    ``py -3`` launcher at runtime (see ``templates/bin/python3.cmd``), so a bare
    py/pyw launcher name, a launcher with extra args, or "nothing found" all
    resolve here to "" by design.

    A ``resolve_python_bin()`` exception is different: it signals a resolution
    *error* (e.g. ``PythonPinInvalid`` -- a pinned interpreter that exists but
    fails validation, with its own remediation message) rather than "nothing to
    bake". That is swallowed into the same "" fallback for functional purposes
    (the rendered wrapper still works via the runtime `py -3` branch), but the
    error itself must not vanish -- silently discarding it would hide an
    actionable misconfiguration from the operator. Surface it as a warning
    rather than aborting install-substrate: the fallback keeps the install
    moving, so this warrants attention, not failure.

    Non-Windows hosts bake NOTHING, unconditionally. Every artifact this value
    reaches is a ``.cmd`` -- ``python3.cmd`` and the generated agent-helper
    ``.cmd`` halves -- and a ``.cmd`` is inert on macOS/Linux, so the resolved
    path here is a *machine-local absolute path for the platform that will
    never run the file*. Nothing correct can be baked from that side. A synced
    ``~/.claude`` makes this bite for real: a macOS install baked
    ``/Users/<user>/.coordinator-claude-settings/.coordinator-venv/bin/python``
    into every launcher in the ``coordinator/bin/``-derived forwarder set
    (see ``_derive_agent_helper_target_map`` for the current count) that only
    Windows executes, and every subsequent macOS install re-poisoned the ones
    a Windows install had just written correctly.

    The gate is on the BAKE, not on the emission: the ``.cmd`` files are still
    written cross-platform, so a synced home keeps serving a Windows consumer
    whose own installer has not run yet (gating emission instead would leave
    that consumer with no launcher at all -- strictly worse than an unbaked
    one). An unbaked ``.cmd`` resolves its interpreter at runtime via the
    ``where python.exe`` / ``py -3`` rungs, which is portable by construction
    -- the same reason the hand-authored static ``.cmd`` templates
    (``machine-local.cmd`` et al.) travel safely with their ``__PYTHON_BIN__``
    token left unsubstituted.

    Requests the CONSOLE interpreter (``prefer_windowless=False``) -- ``python3.cmd``
    is a general-purpose shim invoked by arbitrary callers, including ones that pipe
    a live stdin (e.g. ``cmd.exe /c python3.cmd`` spawned with an inherited pipe). A
    windowless (``/SUBSYSTEM:WINDOWS``) interpreter baked into that shim gives such a
    child a null/invalid stdin handle -- observed on a real box as a silent,
    permanent hang (a loud fast failure became a 10-minute wedge) rather than the
    console-flash the windowless preference exists to suppress elsewhere. See
    ``resolve_python_bin()``'s ``prefer_windowless`` docstring for the general
    rationale.

    Defense in depth: even with ``prefer_windowless=False`` requested, REJECT any
    resolved path whose basename is a windowless twin (``pythonw.exe``/``pyw.exe``)
    so a future resolver change cannot silently re-bake a windowless interpreter into
    this general-purpose shim. On rejection, fall back to the console sibling in the
    same install dir if present; otherwise fall through to the existing "" (no-bake
    -> runtime ``py -3`` ladder) with the existing-style warning.
    """
    return _resolve_baked_python_bin_detail()[0]


def _resolve_baked_python_bin_detail() -> Tuple[str, _BakedPythonBinReason]:
    """Same resolution as ``_resolve_baked_python_bin``, plus WHY a ``""``
    result was returned -- see ``_BakedPythonBinReason`` for the case
    catalogue and why the split exists. One probe, reported in full; callers
    that only need the bake value use the thin wrapper above rather than
    duplicating this function's probing logic.
    """
    if os.name != "nt":
        return "", _BakedPythonBinReason.NOT_WINDOWS
    try:
        from coordinator_core.pyresolve import (
            _WINDOWLESS_BASENAMES,
            _console_sibling,
            resolve_python_bin,
        )

        py_bin, py_args = resolve_python_bin(prefer_windowless=False)
        if not py_bin:
            return "", _BakedPythonBinReason.UNRESOLVED
        if py_bin in ("py", "pyw") or py_args:
            return "", _BakedPythonBinReason.LAUNCHER_ONLY
        # `py_bin` is a WINDOWS-shaped path (this branch only runs when
        # `os.name == "nt"`, real or monkeypatched); parse it with `ntpath`
        # explicitly rather than `os.path` for the same host-independence
        # reason as `_console_sibling` above.
        if ntpath.basename(py_bin).lower() in _WINDOWLESS_BASENAMES:
            sibling = _console_sibling(py_bin)
            if sibling:
                return sibling, _BakedPythonBinReason.RESOLVED
            print(
                f"[install-substrate] WARNING: python3.cmd interpreter resolution "
                f"returned a windowless binary ('{py_bin}') with no console sibling "
                "on disk; baked wrapper falls back to `py -3` launcher at runtime",
                file=sys.stderr,
            )
            return "", _BakedPythonBinReason.NO_CONSOLE_SIBLING
        return py_bin, _BakedPythonBinReason.RESOLVED
    except Exception as exc:
        print(
            f"[install-substrate] WARNING: python3.cmd interpreter resolution failed "
            f"({exc}); baked wrapper falls back to `py -3` launcher at runtime",
            file=sys.stderr,
        )
        return "", _BakedPythonBinReason.RESOLUTION_ERROR


def resolve_hook_python_bin() -> str:
    """Resolve the interpreter path to write into ``settings.json``'s ``env``
    block as ``COORDINATOR_PYTHON_BIN`` (the value hook commands reference by
    variable, never repeat into command text -- see C2).

    Returns the absolute interpreter path, or ``""`` when nothing should be
    resolved -- this is a *contractually valid* result, not a failure (plan
    AC3): the caller emits the bare ``python3`` token in the command string
    and the install proceeds.

    Policy (revised 2026-08-14, docs/plans/2026-08-14-the-venv-fallback-stops-
    being-something.md C2): resolve the MACHINE interpreter -- the OS-detect
    tier ``resolve_python_bin()`` itself calls tier 3, never its tier 1/2 pin
    (``COORDINATOR_PYTHON`` env var / machine-local ``coordinator.python``).
    That pin is purpose (a)/(b) territory (``coordinator_whoami``, the shared
    fleet venv) and, post-C1, an explicit machine-level-install-failure
    opt-in -- none of those are "a hook needs yaml", and taking that pin
    unconditionally meant a hook baked on a box that merely HAS a venv for an
    unrelated purpose got pointed at it regardless.

    The previous policy ("prefer the venv interpreter, unconditionally, on
    every platform") rested on ``yaml`` being "a venv-only package". That was
    false when it landed: ``PyYAML>=6`` became a declared
    ``[project].dependencies`` entry in ``912c1648b`` (2026-07-27) --
    installed machine-level by ``scripts/setup.py::provision_deps`` -- a full
    week before this resolver landed in ``520c175ce`` (2026-08-03). Three
    live hook scripts import ``yaml`` (DoE's
    ``coordinator/hooks/scripts/enforce-agent-dispatch-mode.py`` at MODULE
    level, unguarded; ``coordinator_core/hooks/nudge_unrouted_sizing.py``
    lazily; ``coordinator_core/hooks/scripts/_oss_operative_strings.py``
    guarded/fail-open, unaffected either way -- named here so this count of
    three is self-verifying, review: code-reviewer F3 2026-08-03) but none of
    that motivates pointing at the venv specifically: the machine interpreter
    ``provision_deps`` provisions already has ``yaml`` importable from its own
    site-packages (verified by execution, 2026-08-14).

    DoE friction-log F7 -- a baked venv path deadlocking a venv rebuild on a
    live Windows box -- was accepted as a counter-cost of pointing at the
    venv. Under this policy it is no longer a counter-cost to weigh: hook
    commands resolving to the machine interpreter cannot deadlock a venv
    rebuild in the first place, because they never named the venv. The
    incident stays recorded here as evidence FOR this change, not deleted --
    a future reader must see why the venv-preferring policy was retired, not
    just that it was.

    NO non-Windows gate, unlike ``_resolve_baked_python_bin``: that gate is
    correct there because every artifact it feeds is a ``.cmd``, inert on
    macOS/Linux, but hook commands run on every platform this resolver's
    caller runs on, so this resolves unconditionally.

    Requests the CONSOLE interpreter (``prefer_windowless=False``): hooks
    receive their JSON payload on STDIN, the exact live-stdin case
    ``_resolve_baked_python_bin``'s own docstring records as a silent
    ten-minute wedge on real hardware when a windowless bake is used.

    A non-empty ``python_args`` (the ``py -3`` launcher-plus-flag shape)
    resolves to ``""`` -- the ``env`` value is a single executable path, not a
    launcher/flag pair, and there is no way to express ``py -3`` as one env
    value.

    A resolution exception degrades to ``""``, mirroring
    ``_resolve_baked_python_bin``'s handling exactly: surfaced as a warning on
    stderr, never aborting.

    Negative-spec: the returned path is for the GENERATING machine. Nothing
    here may assume the generating machine is the executing machine -- C2's
    widened ``[ -x ]`` / ``Test-Path`` existence-guard is what makes that safe.
    """
    try:
        from coordinator_core.pyresolve import resolve_machine_python_bin

        py_bin, py_args = resolve_machine_python_bin(prefer_windowless=False)
        if not py_bin or py_args:
            return ""
        return py_bin
    except Exception as exc:
        print(
            f"[install-substrate] WARNING: hook interpreter resolution failed "
            f"({exc}); hook commands fall back to bare `python3` at runtime",
            file=sys.stderr,
        )
        return ""


def run(setup_only: bool = False, check_only: bool = False) -> int:
    """Entry point mirroring the full bash script body. Returns a process
    exit code (0 success, 1 FATAL)."""
    plugin_root_env = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if not plugin_root_env:
        print(
            "install-substrate: CLAUDE_PLUGIN_ROOT is required (no BASH_SOURCE "
            "self-derivation exists for this Python module — see module docstring)",
            file=sys.stderr,
        )
        return 1
    plugin_root = Path(plugin_root_env)

    # DoE-side precondition only — templates/ is the last surface DoE-claude's
    # CLAUDE_PLUGIN_ROOT still owns (b644d5a9 relocated lib/ and bin/ into
    # claude-klabauter's own coordinator/ tree; requiring lib/ here as well would be a
    # vestigial check that always passes on a post-relocation DoE checkout and
    # never catches anything real).
    if not (plugin_root / "templates").is_dir():
        print(
            "install-substrate: CLAUDE_PLUGIN_ROOT does not have expected layout "
            "(templates/ must exist)",
            file=sys.stderr,
        )
        print(f"  Resolved root: {plugin_root}", file=sys.stderr)
        return 1

    # claude-klabauter-side precondition — coordinator/lib/ and coordinator/bin/ now live
    # in the claude-klabauter checkout, resolved via the canonical resolver, never by
    # __file__-walking or a hardcoded sibling name.
    try:
        _claude_klabauter_root_str, _resolution_class = coordinator_claude_klabauter_root_with_class()
        claude_klabauter_root = Path(_claude_klabauter_root_str)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    claude_klabauter_lib = claude_klabauter_root / "coordinator" / "lib"
    if not claude_klabauter_lib.is_dir():
        print(
            f"install-substrate: claude-klabauter-side {claude_klabauter_lib} not found "
            f"(resolved CLAUDE_KLABAUTER_ROOT={claude_klabauter_root}).\n"
            "The claude-klabauter checkout is broken/incomplete, or repos.claude_klabauter "
            "resolves to the wrong tree. Remediation:\n"
            "  (a) confirm 'machine-local get repos.claude_klabauter' points at a real "
            "claude-klabauter checkout,\n"
            "  (b) git fetch/pull the existing claude-klabauter checkout to restore "
            "coordinator/lib/ — do not re-clone a shared working tree.",
            file=sys.stderr,
        )
        return 1

    ml_templates = plugin_root / "templates" / "machine-local"
    ml_bin = plugin_root / "templates" / "bin"
    ch_bin = claude_klabauter_lib / "claude-home"
    setup_src = plugin_root / "templates" / "setup"

    for required in (ml_templates, ml_bin, ch_bin, setup_src):
        if not required.is_dir():
            print(
                f"Phase 3 FATAL: required directory not found at {required}.\n"
                "Cannot lay down machine-local substrate or percolation mechanism, "
                "and downstream skills depend on it.\n"
                "The coordinator plugin install (or the claude-klabauter checkout) is "
                "broken or incomplete. Remediation:\n"
                "  (a) reinstall the coordinator plugin via the marketplace,\n"
                "  (b) verify CLAUDE_PLUGIN_ROOT resolves correctly (echo $CLAUDE_PLUGIN_ROOT),\n"
                "  (c) verify CLAUDE_KLABAUTER_ROOT / repos.claude_klabauter resolves to a complete "
                "claude-klabauter checkout,\n"
                "  (d) if this is a meta-repo dev checkout, confirm the missing dir is present.",
                file=sys.stderr,
            )
            return 1

    try:
        setup_files, setup_exec_files, setup_hook_files = _load_setup_template_manifest(claude_klabauter_root)
    except SubstrateFatalError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    check_only = check_only or bool(os.environ.get("CHECK_ONLY"))

    # --- One-time idempotent migration (C1): before mkdir-p and seed ---
    try:
        install_base = require_home("install-substrate")
    except RequireHomeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    claude_base = Path(install_base) / ".claude"
    migrate_rc = migrate_substrate_to_settings_home(claude_base, settings_home(), check_only=check_only)
    if migrate_rc != 0:
        print(
            "install-substrate: migration helper failed "
            "— resolve the conflict in the output above and re-run install",
            file=sys.stderr,
        )
        return 1

    # --- Resolve install destination ---
    settings_home_path = settings_home()
    ml_dst = settings_home_path / "machine-local"
    bin_dst = settings_home_path / "bin"
    if not check_only:
        ml_dst.mkdir(parents=True, exist_ok=True)
        bin_dst.mkdir(parents=True, exist_ok=True)

    # --- Step 2: tracked machine-local files ---
    if check_only:
        missing = [f for f in _TRACKED_ML_FILES if not (ml_dst / f).is_file()]
        if missing:
            raise SubstrateFatalError(
                f"install-substrate: check failed: tracked machine-local file(s) absent in "
                f"{ml_dst}: {', '.join(missing)} (would seed from {ml_templates})"
            )
        print(f"[install-substrate] check: tracked machine-local files present in {ml_dst} (no-op)")
    else:
        for f in _TRACKED_ML_FILES:
            src = ml_templates / f
            dst = ml_dst / f
            if not dst.is_file():
                shutil.copyfile(src, dst)
            elif not filecmp.cmp(src, dst, shallow=False):
                print(f"[machine-local] operator-customized {f} preserved; template at {src} for diff reference")

    # --- Step 2b: concern baseline files ---
    if not check_only and not (ml_dst / _ML_UNREAL_TOML_NAME).is_file():
        shutil.copyfile(ml_templates / f"{_ML_UNREAL_TOML_NAME}.example", ml_dst / _ML_UNREAL_TOML_NAME)
        print("[machine-local] installed unreal.toml baseline (schema-only; add values to unreal.local.toml)")

    # --- Step 2c: seed live registry.toml on first install ---
    if not check_only and not (ml_dst / _ML_REGISTRY_TOML_NAME).is_file():
        shutil.copyfile(ml_templates / f"{_ML_REGISTRY_TOML_NAME}.example", ml_dst / _ML_REGISTRY_TOML_NAME)
        print("[machine-local] seeded live registry.toml from example")

    # --- Step 2c-notice: cockpit emit identity keys ---
    if (ml_dst / _ML_REGISTRY_TOML_NAME).is_file():
        meta_slug_set = False
        # Rung 1: the installer's OWN canonical settings-home machine-local
        # CLI (bin_dst), not the compat mirror — but `_install_bin_resolvers`
        # (which writes bin_dst/"machine-local") runs AFTER this block on a
        # fresh install, so this rung is a best-effort probe that tolerates
        # absence; rungs 2/3 below already handle that case unconditionally.
        # `is_executable` accepts the bare extension-less name on Windows on
        # the strength of a PATHEXT sibling (`machine-local.cmd`) — but
        # CreateProcess cannot exec the bare file itself (WinError 193), so the
        # argv has to name the sibling. `resolve_launchable` is that mapping;
        # same constraint as the Step C10a-2 probe below.
        machine_local_bin = bin_dst / "machine-local"
        if is_executable(machine_local_bin):
            val = _quiet_output([*resolve_launchable(str(machine_local_bin)), "get", "cockpit.meta_repo_slug"])
            if val:
                meta_slug_set = True
        # Rung 2: registry.local.toml, via the canonical registry_get reader
        # (handles both the nested-[cockpit]-table and flat-quoted-dotted-key
        # write forms — a raw regex on the flat form alone missed a nested
        # table entirely). MACHINE_LOCAL_REGISTRY_DIR is pinned to ml_dst so
        # this reads the FRESHLY SEEDED registry this function just wrote,
        # not a stale ambient settings-home the caller's own env might point
        # elsewhere (install-verification correctness, not just style).
        if not meta_slug_set:
            env_override = os.environ.get("MACHINE_LOCAL_REGISTRY_DIR")
            os.environ["MACHINE_LOCAL_REGISTRY_DIR"] = str(ml_dst)
            try:
                if machine_resolver.registry_get("cockpit.meta_repo_slug"):
                    meta_slug_set = True
            finally:
                if env_override is None:
                    os.environ.pop("MACHINE_LOCAL_REGISTRY_DIR", None)
                else:
                    os.environ["MACHINE_LOCAL_REGISTRY_DIR"] = env_override
        # Rung 3: an EXPLICIT empty-string declaration in the tracked
        # registry.toml is the only trigger for the notice — a key simply
        # ABSENT from registry.toml (e.g. a trimmed template) must stay
        # silent. This is a deliberately different predicate from rung 2's
        # "resolved to a real value" — do not collapse the two.
        if not meta_slug_set:
            reg_flat = machine_resolver.load_flat_registry_file(ml_dst / "registry.toml")
            if reg_flat.get("cockpit.meta_repo_slug", None) == "":
                print("[machine-local] NOTICE: cockpit emit key unset — set this before using cockpit emit:")
                print('[machine-local]   machine-local set cockpit.meta_repo_slug "<owner/repo, e.g. myowner/my-meta-repo>"')

    # --- Step 3a: python3.cmd baked-interpreter rendering ---
    python3_cmd_resolved_bin, _python3_cmd_bake_reason = _resolve_baked_python_bin_detail()

    try:
        _install_bin_resolvers(
            ml_bin, ch_bin, bin_dst,
            check_only,
            python3_cmd_resolved_bin=python3_cmd_resolved_bin,
            python3_cmd_bake_reason=_python3_cmd_bake_reason,
        )

        # --- Step 3c-ii: settings-manifest.md ---
        manifest_src = plugin_root / "templates" / _SETTINGS_MANIFEST_FILENAME
        if manifest_src.is_file():
            _install_one(manifest_src, settings_home_path / _SETTINGS_MANIFEST_FILENAME, False, "machine-local", check_only)

        # --- Steps 3d + 3e: skipped under --setup-only ---
        if not setup_only:
            _percolation_and_path_steps(
                setup_src, setup_files, setup_exec_files, setup_hook_files,
                install_base, bin_dst, check_only,
            )

        # --- Step 3f: hardware concern baseline ---
        if not check_only and not (ml_dst / _ML_HARDWARE_TOML_NAME).is_file():
            shutil.copyfile(ml_templates / f"{_ML_HARDWARE_TOML_NAME}.example", ml_dst / _ML_HARDWARE_TOML_NAME)
            print("[machine-local] installed hardware.toml baseline (schema-only; values written by detect-hardware.sh)")

        # --- Step 3g: register hardware concern ---
        registry_live = ml_dst / _ML_REGISTRY_TOML_NAME
        if not check_only and registry_live.is_file():
            _register_hardware_concern(registry_live)

        # --- Step 3h: hardware audit ---
        _run_hardware_audit(check_only)

        # --- Steps C10a-1/2/3: whoami relocation, registry key, venv rebuild ---
        rc = _c10a_steps(install_base, settings_home_path, plugin_root, bin_dst, check_only)
        if rc != 0:
            return rc

        # --- Step C10b: settings-home seed-wiki population ---
        _install_seed_wikis(plugin_root, settings_home_path, check_only)

        if setup_only:
            seeded_verb = "would be seeded" if check_only else "seeded"
            if _is_windows_shell():
                print(
                    f"[install-substrate] --setup-only: forwarders {seeded_verb}, but {bin_dst} "
                    "was NOT added to the Windows user PATH (that step only runs in the full "
                    "install). Bare-name coordinator CLI invocation will not resolve until "
                    "you run the full install: "
                    "python3 -m coordinator_core.install.substrate (same env, without --setup-only).",
                    file=sys.stderr,
                )
            else:
                print(f"[install-substrate] --setup-only: machine-local substrate {seeded_verb}; skipping fnm/Windows machine-env steps")
            return 0

        _fnm_step(check_only)

        if not _is_windows_shell():
            if os.name == "nt":
                print(
                    f"[install-substrate] WARNING: Windows PATH integration skipped — "
                    f"os.name is 'nt' but OSTYPE/OS did not identify a Windows shell. "
                    f"{bin_dst} will not be on PATH; bare-name CLI invocation will fail. "
                    "Set OS=Windows_NT (or OSTYPE=msys/cygwin) and re-run install.",
                    file=sys.stderr,
                )
            return 0

        _windows_health_steps(bin_dst, check_only)
    except SubstrateFatalError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


def _resolve_agent_cmd_dest_collisions(agent_helper_target_map: "dict[str, str]") -> "dict[str, str]":
    """Resolve `.cmd`-destination-name collisions among installed
    agent-helper forwarder names into a definite installed-name ->
    cmd-dest-name map, ONE entry per WINNING installed name — a losing name
    gets no `.cmd` written for it at all.

    ``_agent_cmd_dest_name`` strips the installed name's own extension, so
    two installed names can collapse onto the same destination filename —
    e.g. ``render-handoff-tracker`` and ``render-handoff-tracker.js`` both
    map to ``render-handoff-tracker.cmd``. Under the retired copy-gated
    ``.cmd`` leg this collision was latent (usually only one side had a
    source-tree `.cmd` twin to copy). Unconditional generation (every
    forwarder now gets a `.cmd` — see ``_write_agent_cmd_forwarder``'s
    docstring) turns it ACTIVE: whichever name happened to sort last in the
    installer's own ``sorted(agent_helper_target_map.items())`` loop
    silently won, last-write-wins and installer-iteration-order-dependent —
    verified against the live tree: a handful of real collisions, all
    ``<name>`` vs ``<name>.js`` pairs where the ``.js`` name sorted last.
    (The exact entry count drifts as concurrent sessions add CLIs to
    ``coordinator/bin/`` — not pinned here; re-derive it from disk if it
    matters.) See
    cross-repo/inbox/2026-07-23-claude-central-em-cmd-forwarder-install-break.md.

    Deterministic precedence: the non-``.js`` name wins over a ``.js``
    twin — this repo's query/read layer went fully native 2026-07-22 (the
    de-node cutover; see project CLAUDE.md § Runtime conventions), so
    wherever a `.js` CLI and a non-`.js` sibling both resolve to the same
    installed slot, the `.js` one is the legacy side. Any collision NOT
    resolvable by that rule (zero or more than one non-``.js`` candidate in
    the group) is a ``SubstrateFatalError`` at install time — a new
    colliding pair must never land as a silent overwrite; it needs an
    explicit PM ruling extending this precedence.
    """
    by_dest: "dict[str, list[str]]" = defaultdict(list)
    for name in agent_helper_target_map:
        by_dest[_agent_cmd_dest_name(name)].append(name)

    resolved: "dict[str, str]" = {}
    for dest, names in sorted(by_dest.items()):
        if len(names) == 1:
            resolved[names[0]] = dest
            continue
        non_js = [n for n in names if not n.endswith(".js")]
        if len(non_js) == 1:
            resolved[non_js[0]] = dest
            continue
        raise SubstrateFatalError(
            "install-substrate: agent-helper .cmd destination collision at "
            f"{dest!r} among installed names {sorted(names)!r} -- the "
            "non-.js-wins precedence rule does not resolve this pairing "
            "(zero or multiple non-.js candidates).\n"
            "Remediation: rename one of the colliding CLIs in "
            "coordinator/bin/ so their installed names no longer collapse "
            "onto the same .cmd filename, or get a PM ruling extending the "
            "precedence rule in _resolve_agent_cmd_dest_collisions -- see "
            "cross-repo/inbox/2026-07-23-claude-central-em-cmd-forwarder-install-break.md."
        )
    return resolved


# `_CH_FAMILY_FILES` and `_RM_FAMILY_FILES` are sourced from CLAUDE-KLABAUTER'S OWN
# tree (`coordinator/lib/claude-home/` and `coordinator/lib/resolve-claude-klabauter/`
# respectively) — NOT DoE's `templates/bin/` — so they are out of scope for
# `coordinator/lib/bin-templates-manifest.py` (C12) by construction and stay
# hand-maintained here. See that manifest's own negative-spec for why. The
# `_ML_FAMILY_FILES` / `_ML_EXPLICIT_FILES` / `_PLATFORM_LOCALIZE_FILES`
# tuples that USED to live here (all three sourced from `ml_bin`, DoE's
# `templates/bin/`) are now declared in that manifest instead — see
# `_load_bin_templates_manifest` above and `_install_bin_resolvers` below.
_CH_FAMILY_FILES = (
    ("claude-home", True), ("_claude_home.py", False), ("claude-home.cmd", False),
)
# rm_family writes this one name per dir; kept as its own tuple purely for
# uniformity with the constant above (it's also independently excluded
# from the sweep by its leading underscore, same as the derivation scan's
# own exclusion — see `_derive_agent_helper_target_map`).
_RM_FAMILY_FILES = ("_resolve_claude_klabauter.py",)


def _static_bin_family_names(claude_klabauter_root: "Optional[Path]" = None) -> "frozenset[str]":
    """Complete set of filenames `_install_bin_resolvers` installs into
    `bin_dst` this run from a STATIC source —
    i.e. every family except the dynamically-derived agent-helper
    forwarders (whose membership varies with `coordinator/bin/`'s current
    listing and is supplied separately by the caller). Sourced from
    `coordinator/lib/bin-templates-manifest.py`'s
    `ML_FAMILY_FILES`/`ML_EXPLICIT_FILES`/`PLATFORM_LOCALIZE_FILES` groups
    (the same manifest `_install_bin_resolvers`'s write loops read) plus the
    `_CH_FAMILY_FILES`/`_RM_FAMILY_FILES` constants above, so it cannot
    drift out of sync with what a given run actually writes.

    `claude_klabauter_root`: resolved via `_resolve_bin_templates_manifest_root()`
    (co-located rung first, zero subprocess/env dependency on this repo's
    own checkout) when the caller doesn't already have one on hand — see
    that resolver's docstring. `_install_bin_resolvers` passes its own
    already-resolved `claude_klabauter_root_resolved` explicitly instead of paying
    for a second resolution.

    This is the completeness half of `_sweep_orphaned_agent_helpers`'s
    provenance check: a marker-carrying file is an orphan only if its name
    is absent from BOTH this static set AND the current agent-helper maps.
    Without this, a DoE-owned/other-family file that happens to also carry
    a launcher marker (``platform-localize.cmd`` and
    ``resolve-coordinator-clone.cmd`` both do, on the live tree — they were
    generated by the same ``gen-launcher-shim.py`` tool as every CLI in
    ``coordinator/bin/``) reads as an orphan under a check that only
    consults the agent-helper maps, and gets deleted — exactly the
    far-worse-bug shape the sweep exists to avoid.
    """
    root = claude_klabauter_root if claude_klabauter_root is not None else _resolve_bin_templates_manifest_root()
    manifest = _load_bin_templates_manifest(root)
    names: "set[str]" = set()
    names.update(e.name for e in manifest.install_bin_resolvers_entries())
    names.update(f for f, _ in _CH_FAMILY_FILES)
    names.update(_RM_FAMILY_FILES)
    return frozenset(names)


# AC18 — general install-time prune (second instance of the class flagged in
# cross-repo/archive/2026-07-23-claude-central-em-cmd-forwarder-install-break.md,
# first instance mint-deliverable-id.sh.cmd; this one is the
# platform-localize.sh directory bug's generalization, see this chunk's
# uninstall_legs.py fix above). `_sweep_orphaned_agent_helpers` already
# solves this for the DYNAMIC agent-helper family (marker-content
# provenance), but a STATIC-family rename (e.g. the 2026-07-22
# platform-localize.sh -> {.py,.cmd} rename) leaves an orphan with no
# `_AGENT_FORWARDER_MARKER`/`_AGENT_CMD_FORWARDER_MARKER`/`_LEGACY_CMD_MARKER`
# to key off, so that sweep correctly leaves it alone. Fixing every future
# rename by hand-adding a literal to a legacy-name tuple (uninstall_legs.py's
# `coord_bin_names`, or `_static_bin_family_names`) is exactly the growing-
# literal-list failure mode this prune exists to end.
#
# Provenance guard, per PM ruling (F8): a manifest THIS installer itself
# writes and reads back, not "present in bin_dst" and not a name-shape
# heuristic. Each real (non-check-only) run persists the CURRENT complete
# write-set (`_static_bin_family_names() | derived agent-helper names`) to
# `_BIN_MANIFEST_FILENAME` in `bin_dst`. On the NEXT run, any name recorded
# in the PREVIOUS manifest that is no longer in the current write-set is a
# renamed/retired orphan THIS installer put there — and only such a name is
# eligible for removal. A dotfile is excluded from every other sweep in this
# module by convention (see `_sweep_orphaned_agent_helpers`'s leading-`.`
# skip), so the manifest itself is never mistaken for a prune candidate. An
# operator's own file sharing `bin_dst` was never in a prior manifest and is
# therefore never touched, however its name shapes up.
_BIN_MANIFEST_FILENAME = ".coordinator-bin-manifest.json"


def _read_bin_manifest(bin_dst: Path) -> "frozenset[str]":
    """Names this installer wrote into `bin_dst` as of its last real
    (non-check-only) run. Absent/unreadable/malformed manifest reads as
    empty -- the prune below then removes nothing, which is always the safe
    direction (a missing manifest must never manufacture deletions)."""
    manifest_path = bin_dst / _BIN_MANIFEST_FILENAME
    if not manifest_path.is_file():
        return frozenset()
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return frozenset()
    names = data.get("names") if isinstance(data, dict) else None
    if not isinstance(names, list):
        return frozenset()
    return frozenset(n for n in names if isinstance(n, str))


def _write_bin_manifest(bin_dst: Path, names: "frozenset[str]") -> None:
    manifest_path = bin_dst / _BIN_MANIFEST_FILENAME
    manifest_path.write_text(
        json.dumps({"names": sorted(names)}, indent=2) + "\n", encoding="utf-8"
    )


def _prune_orphaned_static_bin_names(
    bin_dst: Path, current_names: "frozenset[str]", check_only: bool
) -> None:
    """Removes any file this installer's OWN prior manifest recorded that
    this run's `current_names` no longer includes (a renamed/retired
    static-family or agent-helper name) -- making renames self-cleaning with
    no growing hardcoded literal list. Never touches a file absent from the
    prior manifest, regardless of its name -- that is the provenance guard
    against deleting an operator's own file that happens to share the
    directory (AC18)."""
    if not bin_dst.is_dir():
        # WRITE_SURFACE clause 14 (`_CLAUSE_PRUNE_ORPHANED_STATIC`): the
        # destination doesn't exist yet — "never got there", not "resolved
        # to nothing" — so no journal row for this run (see the matching
        # note in `_sweep_orphaned_agent_helpers`).
        return
    previous_names = _read_bin_manifest(bin_dst)
    pruned: "list[WriteSurfaceEntry]" = []
    for name in sorted(previous_names - current_names):
        target = bin_dst / name
        if not target.is_file():
            continue
        if check_only:
            print(f"[install-substrate] would: prune orphaned coordinator bin file {name} ({target})")
            continue
        blocked = _refuse_machine_mutation(
            str(target), what=f"prune orphaned coordinator bin file {name}", check_temp_path=False,
        )
        if blocked:
            print(f"[install-substrate] REFUSED: {blocked}", file=sys.stderr)
            continue
        try:
            target.unlink()
            pruned.append(WriteSurfaceEntry(kind="file-path", path=str(target), effect="delete"))
            print(f"[install-substrate] pruned orphaned coordinator bin file {name} ({target})")
        except OSError as exc:
            print(f"[install-substrate] WARNING: failed to prune orphaned bin file {target}: {exc}", file=sys.stderr)
    if not check_only:
        # A guard-refused or failed-unlink entry never lands in `pruned` —
        # only entries actually removed are journaled (clause 14 contract).
        resolution_journal.record_resolution(_WRITER_ID, _CLAUSE_PRUNE_ORPHANED_STATIC, pruned)
        _write_bin_manifest(bin_dst, current_names)


def _unblock_files(paths: "list[Path]") -> None:
    """Delete the ``Zone.Identifier`` alternate data stream on every path in
    ``paths`` (C7b, ruled IN on the parent plan's spine row and relayed to
    this baton as AC14). Caller is required to have already checked
    ``_refuse_machine_mutation`` -- this function performs no consent gating
    of its own and unconditionally attempts the mutation.

    Best-effort: a file that carries no ``Zone.Identifier`` stream (measured
    true for every ``.ps1`` on this box's ``~/.claude`` sync path -- see the
    plan's Problem section) has nothing to delete, so a missing stream is a
    harmless no-op, not an error, swallowed rather than escalated into a
    failed install: the artifact this pass emitted is unaffected either way,
    and the POST-emission policy-gate verification that follows is what
    actually decides whether the ``.ps1`` legs survive."""
    if not paths:
        return
    for p in paths:
        try:
            os.remove(f"{p}:Zone.Identifier")
        except OSError:
            pass  # no ADS present -- the common case, a no-op, not an error


def _emit_and_verify_ps1_forwarders(
    bin_dst: Path,
    agent_cmd_dest_map: "dict[str, str]",
    check_only: bool,
    *,
    python3_cmd_resolved_bin: str,
) -> "Optional[PolicyGateVerdict]":
    """Emit the `.ps1` leg of every agent-helper forwarder, driven off the
    RESOLVED `.cmd` map (``agent_cmd_dest_map``, `_resolve_agent_cmd_dest_
    collisions`'s return value) exactly as `_install_bin_resolvers` already
    drives `.cmd` emission -- never the raw `agent_helper_target_map` (AC1):
    that map is keyed by every candidate installed name BEFORE collision
    resolution, so emitting `.ps1` from it independently could let a
    collision LOSER win the `.ps1` slot while a different name wins the
    `.cmd` slot for the same bare name -- the same bare name resolving to
    different implementations under PowerShell and cmd.exe, on one box.

    Sequence, in order: emit every `.ps1` -> `Unblock-File` what this pass
    just wrote (AC14, gated by `_refuse_machine_mutation`, BEFORE the gate
    so the verdict reflects the artifact as it will finally sit rather than
    a mid-treatment state) -> verify via C2's `evaluate_policy_gate()`
    against those real emitted launchers at their final destination paths
    -> keep on GREEN, roll back every `.ps1` this pass wrote on RED (AC7,
    zero left behind is the goal -- a half-emitted state is the hazard, not
    a fallback). Rollback is best-effort: a per-path `unlink()` failure is
    reported loudly to stderr rather than swallowed, but is not retried --
    a transient handle/lock (see below) can still leave a launcher behind,
    reported rather than silent, but not guaranteed removed.

    Verification is POST-emission specifically because: `AllSigned` rejects
    an unsigned throwaway probe file exactly as readily as an unsigned
    emitted launcher, so soundness requires verifying by EXECUTING the real
    artifact rather than inspecting or probing a stand-in; the destination
    path's own path/ACL/landing conditions apply to the real file and not
    to a probe written elsewhere; and rollback (AC7) only means something
    once emission has actually happened -- there is nothing to roll back
    for a launcher that was never written. (Not Mark-of-the-Web: that was
    measured ABSENT on this box's `~/.claude` sync path -- 0 of 5,513 files
    carry a `Zone.Identifier` stream -- marking is a transport property,
    not a volume property, so it argues nothing about emission ordering.
    See the plan's Problem section, Consequence paragraph.)

    On RED the install chain still SUCCEEDS (AC8) -- this function never
    raises on a RED verdict and never touches the `.cmd` leg, which stays
    installed and usable regardless of this function's outcome.

    Returns the computed `PolicyGateVerdict` so the caller (and C4, which
    lands the loud-skip message and durable skip record right after this
    chunk in the same file) can render `verdict.reason` to the operator on
    RED. Returns `None` when nothing was emitted -- an empty resolved map,
    or `check_only` mode PROVIDED every destination is already up to date
    (no write happens; `_write_agent_ps1_forwarder` alone reports staleness)
    -- because there is then nothing to verify or roll back. `check_only`
    with a stale/absent destination does not reach `return None`:
    `_write_agent_ps1_forwarder` raises `SubstrateFatalError` first.

    NO `resolution_journal.record_resolution` call for this leg (C4
    decision, against the sibling-clause precedent): every other
    `WRITE_SURFACE` clause journals what it wrote once, right after
    writing it, and that write is final. This one is not -- `written` is
    provisional until the gate below returns, and on RED every path in it
    is unlinked a few lines down. Journaling `written` before the gate
    would record paths that no longer exist the moment this function
    returns; journaling it only on the GREEN branch would still misname a
    provisional set as a resolution. `WRITE_SURFACE` clause 4's own
    comment (this module, near `_CLAUSE_AGENT_HELPER_FORWARDERS`) already
    declares the `.ps1` leg as part of its SHAPED surface, which is the
    durable declaration this repo asks for; the resolution journal is a
    per-run log of what a writer actually committed, and a rolled-back
    write was never a commitment.
    """
    if check_only:
        for name, cmd_dest in sorted(agent_cmd_dest_map.items()):
            ps1_dst = bin_dst / _agent_ps1_dest_name(cmd_dest)
            _write_agent_ps1_forwarder(
                name, ps1_dst, True, python3_cmd_resolved_bin=python3_cmd_resolved_bin,
            )
        return None

    written: "list[Path]" = []
    for name, cmd_dest in sorted(agent_cmd_dest_map.items()):
        ps1_dst = bin_dst / _agent_ps1_dest_name(cmd_dest)
        _write_agent_ps1_forwarder(
            name, ps1_dst, False, python3_cmd_resolved_bin=python3_cmd_resolved_bin,
        )
        written.append(ps1_dst)

    if not written:
        return None

    blocked = _refuse_machine_mutation(
        str(bin_dst), what="Unblock-File the .ps1 launchers this pass emitted",
        check_temp_path=False,
    )
    if blocked:
        print(f"[install-substrate] REFUSED: {blocked}", file=sys.stderr)
    else:
        _unblock_files(written)

    verdict = evaluate_policy_gate()
    if verdict.green:
        return verdict

    for ps1_dst in written:
        try:
            ps1_dst.unlink()
        except OSError as exc:
            # Not harmless the way substrate.py:2194's scratch-file cleanup
            # is: a leftover here is PROTECTED from the orphan sweep on a
            # later run by `protected_names` membership (its bare name's
            # `.cmd` is in the resolved dest map, so the sweep's limb-2
            # derivation adds this `.ps1`), checked before the marker branch
            # ever runs -- carrying `_AGENT_PS1_FORWARDER_MARKER` is what
            # would make it eligible for deletion if it were NOT otherwise
            # protected, not what protects it (see
            # `_sweep_orphaned_agent_helpers`) -- silence would turn
            # a failed rollback into an invisible, permanent half-emitted
            # state. Report loudly, matching how a refused machine mutation
            # is reported above, and continue rolling back the rest.
            print(
                f"[install-substrate] ROLLBACK FAILED: could not remove "
                f"{ps1_dst} after RED verdict: {exc}", file=sys.stderr,
            )
    return verdict


_PS1_POLICY_STATUS_FILENAME = "ps1-policy-gate-status.json"
"""Durable, findable-later surface for the `.ps1` execution-policy verdict
(AC13). Lives at `<settings-home>/ps1-policy-gate-status.json` --
`bin_dst.parent`, i.e. one level above `<settings-home>/bin/`, since this is
a status record about that directory's contents, not another entry in it."""


def _ps1_policy_status_path(bin_dst: Path) -> Path:
    return bin_dst.parent / _PS1_POLICY_STATUS_FILENAME


def _ps1_policy_repair_message(bin_dst: Path) -> str:
    """AC9's mandated fallback: invoking the co-located EXTENSIONLESS
    forwarder directly via `python` bypasses `.ps1`/`.cmd` launcher
    resolution entirely, so it runs regardless of PowerShell execution
    policy on either host. `<command-name>` names the pattern rather than
    every forwarder in `agent_cmd_dest_map` -- the same fallback applies to
    every name this pass skipped, and enumerating all of them here would
    drift the moment `coordinator/bin/` gains or loses a CLI."""
    return (
        f"python {bin_dst / '<command-name>'} <args...>  "
        "(the extensionless forwarder next to the .cmd/.ps1 launchers -- "
        "preserves argv intact regardless of PowerShell execution policy)"
    )


def _write_ps1_policy_status(bin_dst: Path, verdict: "PolicyGateVerdict") -> None:
    """AC13: write the verdict this install pass computed to the durable
    surface -- EVERY non-check-only pass that reached the gate, GREEN or
    RED, not just RED. This is deliberate, not scope creep on AC9 (which
    stays RED-only, see `_report_ps1_policy_gate_skip`): the population
    this file exists for is the operator whose host was GREEN at install
    time and only had policy tightened afterward (AC10's post-install
    mutability residual, documented in `policy_gate`'s module docstring).
    That operator gets no install-time stdout at all to recall regardless
    of wording, because nothing was wrong yet -- the only thing that can
    possibly reach them later is something already sitting on disk. A
    GREEN write, not a RED-only one, is what makes that reachable.
    """
    path = _ps1_policy_status_path(bin_dst)
    payload = {
        "green": verdict.green,
        "reason": verdict.reason,
        "repair_entrypoint": _ps1_policy_repair_message(bin_dst),
        "note": (
            "If a .ps1 launcher under this bin/ directory fails to run "
            "with a PowerShell execution-policy error, the "
            "repair_entrypoint above works regardless of policy. "
            "'green' reflects the dual-host execution-policy verdict as "
            "of the most recent install run only -- policy is mutable "
            "afterward and this file is not re-checked between installs. "
            "See coordinator_core/install/policy_gate.py's module "
            "docstring, AC10."
        ),
    }
    try:
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"[install-substrate] WARNING: could not write {path}: {exc}", file=sys.stderr)


def _report_ps1_policy_gate_skip(verdict: "PolicyGateVerdict", bin_dst: Path) -> None:
    """AC9: the loud skip. RED only -- the GREEN path prints nothing here
    (AC13's second half; the durable-surface write above is not this
    warning and happens on both verdicts). Names the reason, WHICH host(s)
    failed (`verdict.reason` is `evaluate_policy_gate`'s '; '-joined RED-
    host reasons), and the repair entrypoint that works under any policy.
    A silent skip is indistinguishable from a silent fix -- see this
    plan's Problem section; this is the message that closes that gap at
    install time.
    """
    status_path = _ps1_policy_status_path(bin_dst)
    print(
        "[install-substrate] SKIPPED .ps1 launcher emission this pass -- "
        "PowerShell execution-policy gate reported RED:",
        file=sys.stderr,
    )
    print(f"[install-substrate]   {verdict.reason}", file=sys.stderr)
    print(
        "[install-substrate]   The .cmd launchers remain installed and "
        "usable -- only the .ps1 leg was rolled back. Fallback (works "
        "under any execution policy):",
        file=sys.stderr,
    )
    print(f"[install-substrate]   {_ps1_policy_repair_message(bin_dst)}", file=sys.stderr)
    print(f"[install-substrate]   Recorded at {status_path}", file=sys.stderr)


def _handle_ps1_gate_verdict(verdict: "PolicyGateVerdict", bin_dst: Path) -> None:
    """C4's seam, called once per install pass with the non-`None`
    `PolicyGateVerdict` `_emit_and_verify_ps1_forwarders` computed. Always
    writes the durable surface (AC13, both verdicts); reports the loud
    stdout skip only on RED (AC9; AC13's green-path-prints-nothing half).
    Split out from `_install_bin_resolvers` so both branches are directly
    unit-testable without standing up that function's full fixture set.
    """
    _write_ps1_policy_status(bin_dst, verdict)
    if not verdict.green:
        _report_ps1_policy_gate_skip(verdict, bin_dst)


def _write_python_bin_sidecar(bin_dst: Path, python3_cmd_resolved_bin: str) -> None:
    """Write ``<settings-home>/bin/.python-bin`` with the resolved interpreter.

    AC6's durable half (docs/plans/2026-08-16-registry-read-stops-costing-a-process.md).
    The in-file ``__PYTHON_BIN__`` bake the static families now receive is NOT
    durable: DoE-claude's ``coordinator/hooks/scripts/_bin_impl_drift.py`` sweep
    byte-copies template content verbatim on a genuine template change and
    performs no re-bake, so it overwrites that substitution. This sidecar is the
    durable surface precisely because the sweep only iterates DoE's own
    ``templates/bin/`` and never touches a generated file outside it.

    Negative spec: writes only when the content would change, so a second
    consecutive install stays a byte-level no-op (AC2). Skipped entirely when
    the interpreter did not resolve — on non-Windows hosts that is the normal
    case (there is no ``.cmd`` rung and ``_resolve_baked_python_bin`` returns
    ``""``), and confirming that the POSIX forwarder reads the path the Windows
    shim writes is AC5m's, tracked in the debt backlog rather than assumed here.
    The shim's own first-run probe still writes this file; install writing it
    removes the one cold resolution a fresh or freshly-swept box would pay.
    """
    if not python3_cmd_resolved_bin:
        return
    sidecar = bin_dst / ".python-bin"
    eol = "\r\n" if os.name == "nt" else "\n"
    payload = (python3_cmd_resolved_bin + eol).encode("utf-8")
    try:
        if sidecar.is_file():
            existing = sidecar.read_bytes().decode("utf-8", "replace").strip()
            if existing == python3_cmd_resolved_bin:
                return
    except OSError:
        pass
    atomic_write_bytes(sidecar, payload, preserve_mode=True)


def _write_agent_helper_forwarders(
    agent_helper_target_map: "dict[str, str]",
    agent_cmd_dest_map: "dict[str, str]",
    bin_dst: Path,
    check_only: bool,
    *,
    python3_cmd_resolved_bin: str,
) -> "list[WriteSurfaceEntry]":
    """Step 3b's forwarder-write loop proper, extracted out of
    ``_install_bin_resolvers`` so a second caller (the missing-forwarder
    self-heal path — ``coordinator_core.install.forwarder_self_heal``) can
    write the SAME forwarder bodies through the SAME two writers
    (``_write_agent_forwarder``/``_write_agent_cmd_forwarder``) without a
    second, drift-prone implementation.

    Pure refactor of ``_install_bin_resolvers``'s Step 3b body — same two
    maps in, same per-entry writer calls, same ``WriteSurfaceEntry`` list
    out. Deliberately does NOT call
    ``resolution_journal.record_resolution`` itself: that call is an
    install-RUN concept (it journals into a run-scoped path from
    ``RESOLUTION_JOURNAL_ENV_VAR`` for ``receipt.build_receipt`` to read
    back), and the self-heal caller is not an install run — it has no
    receipt to contribute to. ``_install_bin_resolvers`` still performs
    that journal call itself, at the original call site, using this
    function's return value; behaviour there is unchanged.

    Also deliberately excludes the ``.ps1`` leg
    (``_emit_and_verify_ps1_forwarders``/``_handle_ps1_gate_verdict``),
    ``_sweep_orphaned_agent_helpers``, platform-localize, and the ml/ch
    families — none of those are forwarder-loop concerns, and the ``.ps1``
    leg in particular spawns ``powershell.exe`` for its execution-policy
    gate, which is out of scope for a self-heal path that must never spawn
    a subprocess. Callers needing those must still go through
    ``_install_bin_resolvers``/``run()``.

    Concurrency: ``_write_agent_forwarder``/``_write_agent_cmd_forwarder``
    write via a plain in-place ``Path.write_text`` (see their docstrings),
    not atomic-temp-and-rename, so two writers racing the SAME destination
    can interleave and leave a torn file a concurrent reader (a peer
    session executing that forwarder, or `forwarder_self_heal`'s own
    writer) can observe mid-write. `forwarder_self_heal.py` already takes
    `coordinator_core.locked_write.held_lock` on this same `bin_dst`
    before calling these two writers directly; this, the full-installer
    caller of the identical writers, previously took no lock at all --
    a real install run (which a human can trigger by hand at any moment
    per CLAUDE.md § Load norm, alongside a dozen concurrent sessions'
    session-boot self-heal) raced the self-heal path and any concurrent
    installer run on every entry. Mirrors that same lock here, scoped to
    the write loop only -- `check_only` mode never writes and is left
    unlocked (pure read/compare, safe to race). A `LockTimeout` here
    propagates (unlike self-heal's silent best-effort swallow): this is
    the fail-loud installer path, not a best-effort session-boot heal.
    """
    if check_only:
        agent_helper_resolved: "list[WriteSurfaceEntry]" = []
        for f, target in sorted(agent_helper_target_map.items()):
            py_dst = bin_dst / f
            _write_agent_forwarder(f, py_dst, check_only, target=target)
            agent_helper_resolved.append(WriteSurfaceEntry(kind="file-path", path=str(py_dst)))
            cmd_dest = agent_cmd_dest_map.get(f)
            if cmd_dest is not None:
                cmd_dst = bin_dst / cmd_dest
                _write_agent_cmd_forwarder(
                    f, cmd_dst, check_only,
                    python3_cmd_resolved_bin=python3_cmd_resolved_bin,
                    target=target,
                )
                agent_helper_resolved.append(WriteSurfaceEntry(kind="file-path", path=str(cmd_dst)))
        return agent_helper_resolved

    agent_helper_resolved = []
    with held_lock(bin_dst, holder_label="install-substrate-forwarders"):
        for f, target in sorted(agent_helper_target_map.items()):
            py_dst = bin_dst / f
            _write_agent_forwarder(f, py_dst, check_only, target=target)
            agent_helper_resolved.append(WriteSurfaceEntry(kind="file-path", path=str(py_dst)))
            cmd_dest = agent_cmd_dest_map.get(f)
            if cmd_dest is not None:
                cmd_dst = bin_dst / cmd_dest
                _write_agent_cmd_forwarder(
                    f, cmd_dst, check_only,
                    python3_cmd_resolved_bin=python3_cmd_resolved_bin,
                    target=target,
                )
                agent_helper_resolved.append(WriteSurfaceEntry(kind="file-path", path=str(cmd_dst)))
    return agent_helper_resolved


def _install_bin_resolvers(
    ml_bin: Path, ch_bin: Path, bin_dst: Path,
    check_only: bool,
    *, python3_cmd_resolved_bin: str,
    python3_cmd_bake_reason: Optional[_BakedPythonBinReason] = None,
) -> None:
    # Resolved FIRST (hoisted ahead of the former Step 3b position) because
    # the bin-templates manifest (C12) — claude-klabauter's own coordinator/lib/,
    # loaded below via `claude_klabauter_root_resolved` — must be available before
    # Step 3's write loops, not just before Step 3b's agent-helper
    # derivation that already needed this same value. Purely a reordering:
    # same resolution, same failure mode, just paid once, earlier.
    try:
        _claude_klabauter_root_resolved_str, _resolution_class = coordinator_claude_klabauter_root_with_class()
        claude_klabauter_root_resolved = Path(_claude_klabauter_root_resolved_str)
    except RuntimeError as exc:
        raise SubstrateFatalError(f"install-substrate: {exc}") from exc

    bin_manifest = _load_bin_templates_manifest(claude_klabauter_root_resolved)

    # --- C2 (AC6/AC8): static-family interpreter substitution ---
    # The static bin-resolver families (ml_family, ml_explicit, ch_family,
    # platform_localize) were, before C2, a byte-verbatim `_install_one`
    # copy with no substitution step at all — the five hand-authored `.cmd`
    # shims (machine-local.cmd, coordinator-settings-home.cmd,
    # platform-localize.cmd, resolve-coordinator-clone.cmd, claude-home.cmd)
    # were structurally incapable of ever baking `__PYTHON_BIN__`, falling
    # to the slow `where python.exe` + `findstr` runtime tier forever. This
    # reuses `python3_cmd_resolved_bin` — the SAME resolved-interpreter
    # value the dynamic agent-helper forwarders already receive — rather
    # than re-deriving a second resolution.
    #
    # The POSIX extensionless siblings in these same families (e.g.
    # `machine-local`, `resolve-coordinator-clone`, `coordinator-settings-
    # home`, `claude-home`, without a `.cmd` suffix) carry no `__PYTHON_BIN__`
    # token at all — they resolve their interpreter via `#!/usr/bin/env
    # python3` plus their own runtime probe, never a baked token — so
    # threading the same substitution value through every entry in these
    # families is a no-op there by construction, not a platform branch this
    # module has to maintain.
    #
    # AC8: on Windows, `python3_cmd_resolved_bin == ""` is ambiguous by
    # itself — see `_BakedPythonBinReason`. Only `UNRESOLVED` (no
    # interpreter reachable by ANY route) is a genuine failure; a bare
    # py/pyw launcher, a windowless-with-no-console-sibling, or a swallowed
    # resolution error all have a working `py -3` runtime fallback and are,
    # per `_resolve_baked_python_bin_detail`'s own docstring, contractually
    # valid, not a failure. This is the corrected half of AC8 (previously
    # gated on bare emptiness, which hard-failed install on a box whose only
    # Python entry point is the `py` launcher — a normal, supported
    # configuration that previously degraded gracefully via the shim's own
    # runtime probing; see docs/plans/2026-08-16-registry-read-stops-
    # costing-a-process.md AC8's corrected Status cell).
    #
    # `python3_cmd_bake_reason` defaults to `None` (gate never fires) so
    # callers that pass a placeholder `python3_cmd_resolved_bin=""` without
    # having gone through the resolver at all (this function's many
    # non-AC8-focused test callers) are unaffected — the ONLY production
    # caller (`run()`'s Step 3a) always supplies the real reason. Do not
    # "simplify" this back to gating on bare emptiness: that is the exact
    # regression this fix removes.
    #
    # Non-Windows hosts never bake this family at all: there is no `.cmd`
    # rung there, so `python3_cmd_resolved_bin` is unconditionally
    # ""/never consulted (`_resolve_baked_python_bin` itself returns ""
    # for `os.name != "nt"`) and that is not a failure to report.
    if (
        os.name == "nt"
        and not python3_cmd_resolved_bin
        and python3_cmd_bake_reason is _BakedPythonBinReason.UNRESOLVED
    ):
        raise SubstrateFatalError(
            "install-substrate: could not resolve an absolute Python "
            "interpreter to bake into the static bin-resolver shims "
            "(machine-local.cmd, claude-home.cmd, coordinator-settings-home.cmd, "
            "platform-localize.cmd, resolve-coordinator-clone.cmd). Remediation: "
            "ensure python.exe (or the 'py' launcher) is discoverable on PATH, "
            "then re-run coordinator:install."
        )
    static_python_bin_substitution: Optional[str] = (
        python3_cmd_resolved_bin if os.name == "nt" else None
    )

    def ml_family(dst_dir: Path, prefix: str) -> "list[WriteSurfaceEntry]":
        resolved: "list[WriteSurfaceEntry]" = []
        for entry in bin_manifest.ml_family:
            src = ml_bin / entry.name
            dst = dst_dir / entry.name
            _install_one(
                src, dst, entry.exec_bit, prefix, check_only, force_overwrite=True,
                python_bin_substitution=static_python_bin_substitution,
            )
            resolved.append(WriteSurfaceEntry(kind="file-path", path=str(dst)))
        return resolved

    def ch_family(dst_dir: Path, prefix: str) -> None:
        for f, exec_bit in _CH_FAMILY_FILES:
            _install_one(
                ch_bin / f, dst_dir / f, exec_bit, prefix, check_only, force_overwrite=True,
                python_bin_substitution=static_python_bin_substitution,
            )

    def rm_family(dst_dir: Path, prefix: str) -> None:
        # _resolve_claude_klabauter.py is installed ONCE per bin dir, alongside every
        # emitted forwarder — see its own module docstring for why the
        # resolve-claude-klabauter-bin ladder now lives here instead of duplicated
        # inline in each forwarder body.
        _install_one(resolve_claude_klabauter_lib / "_resolve_claude_klabauter.py", dst_dir / "_resolve_claude_klabauter.py", False, prefix, check_only)

    # --- Step 3: bin/ resolvers (<settings-home>/bin/) ---
    # C0: this family (ml_family + ch_family + ml_explicit) force-overwrites
    # on content diff via `_install_one` — a peer session's session-boot
    # self-heal, or a concurrent installer run, can be reading/writing the
    # same `bin_dst` files at the same instant (50-70 concurrent LLMs is
    # this box's average, per CLAUDE.md § Load norm). `held_lock` serialises
    # that against every other holder of the SAME `bin_dst` sidecar lock —
    # `_write_agent_helper_forwarders` below takes its own, separate,
    # sequential acquisition of the identical target for its own write loop;
    # the two are never held concurrently in this process (held_lock is not
    # re-entrant — see its module docstring), so no self-deadlock. Skipped
    # under check_only: that mode never writes, so there is nothing to
    # serialise against (pure read/compare, safe to race).
    if check_only:
        ml_family_resolved = ml_family(bin_dst, "machine-local")
        ch_family(bin_dst, "claude-home")
        ml_explicit_resolved: "list[WriteSurfaceEntry]" = []
        for entry in bin_manifest.ml_explicit:
            dst = bin_dst / entry.name
            _install_one(
                ml_bin / entry.name, dst, entry.exec_bit, "machine-local", check_only,
                force_overwrite=True, python_bin_substitution=static_python_bin_substitution,
            )
            ml_explicit_resolved.append(WriteSurfaceEntry(kind="file-path", path=str(dst)))
    else:
        with held_lock(bin_dst, holder_label="install-substrate-bin-family"):
            ml_family_resolved = ml_family(bin_dst, "machine-local")
            ch_family(bin_dst, "claude-home")
            ml_explicit_resolved = []
            for entry in bin_manifest.ml_explicit:
                dst = bin_dst / entry.name
                _install_one(
                    ml_bin / entry.name, dst, entry.exec_bit, "machine-local", check_only,
                    force_overwrite=True, python_bin_substitution=static_python_bin_substitution,
                )
                ml_explicit_resolved.append(WriteSurfaceEntry(kind="file-path", path=str(dst)))
            _write_python_bin_sidecar(bin_dst, python3_cmd_resolved_bin)
        resolution_journal.record_resolution(_WRITER_ID, _CLAUSE_ML_FAMILY, ml_family_resolved)
        resolution_journal.record_resolution(_WRITER_ID, _CLAUSE_ML_EXPLICIT, ml_explicit_resolved)

    # --- Step 3b: agent/skill bare-name helper forwarders ---
    # `.cmd` twins are sourced from claude-klabauter's OWN coordinator/bin/, resolved
    # here (in-process, importable) rather than in the emitted forwarder body
    # (which must stay self-contained path arithmetic — see
    # _write_agent_forwarder's docstring). `plugin_root / "bin"` (DoE-claude's
    # tree) is the now-empty, dead source this repoint replaces.
    agent_bin = claude_klabauter_root_resolved / "coordinator" / "bin"
    resolve_claude_klabauter_lib = claude_klabauter_root_resolved / "coordinator" / "lib" / "resolve-claude-klabauter"

    agent_helper_target_map = _derive_agent_helper_target_map(agent_bin)
    agent_cmd_dest_map = _resolve_agent_cmd_dest_collisions(agent_helper_target_map)

    rm_family(bin_dst, "resolve-claude-klabauter")
    agent_helper_resolved = _write_agent_helper_forwarders(
        agent_helper_target_map, agent_cmd_dest_map, bin_dst, check_only,
        python3_cmd_resolved_bin=python3_cmd_resolved_bin,
    )
    if not check_only:
        resolution_journal.record_resolution(
            _WRITER_ID, _CLAUSE_AGENT_HELPER_FORWARDERS, agent_helper_resolved,
        )

    # --- Step 3b2: `.ps1` leg of the same forwarder pair (C3 of the
    # ps1-launcher-class plan) --- driven off `agent_cmd_dest_map` (the
    # RESOLVED map), never `agent_helper_target_map`, exactly as the `.cmd`
    # loop above -- see `_emit_and_verify_ps1_forwarders`'s docstring (AC1).
    # `ps1_gate_verdict` is this run's `PolicyGateVerdict` (or `None` when
    # nothing was emitted -- `check_only`, or an empty resolved map, in
    # which case there is nothing to report). AC13's durable status write
    # runs on every non-`None` verdict; AC9's loud stdout skip is RED-only.
    ps1_gate_verdict = _emit_and_verify_ps1_forwarders(
        bin_dst, agent_cmd_dest_map, check_only,
        python3_cmd_resolved_bin=python3_cmd_resolved_bin,
    )
    if ps1_gate_verdict is not None:
        _handle_ps1_gate_verdict(ps1_gate_verdict, bin_dst)

    _sweep_orphaned_agent_helpers(bin_dst, agent_helper_target_map, agent_cmd_dest_map, check_only)

    # --- Step 3c: platform-localize hook ---
    # C0: same force-overwrite/concurrency reasoning as the ml/ch family
    # lock above — a separate, sequential `held_lock` acquisition of the
    # same `bin_dst` target (the Step 3b forwarder lock above has already
    # been released by this point, so this is not a nested/re-entrant hold).
    platform_localize_resolved: "list[WriteSurfaceEntry]" = []
    if check_only:
        for entry in bin_manifest.platform_localize:
            dst = bin_dst / entry.name
            _install_one(
                ml_bin / entry.name, dst, entry.exec_bit, "machine-local", check_only,
                force_overwrite=True, python_bin_substitution=static_python_bin_substitution,
            )
            platform_localize_resolved.append(WriteSurfaceEntry(kind="file-path", path=str(dst)))
    else:
        with held_lock(bin_dst, holder_label="install-substrate-bin-family"):
            for entry in bin_manifest.platform_localize:
                dst = bin_dst / entry.name
                _install_one(
                    ml_bin / entry.name, dst, entry.exec_bit, "machine-local", check_only,
                    force_overwrite=True, python_bin_substitution=static_python_bin_substitution,
                )
                platform_localize_resolved.append(WriteSurfaceEntry(kind="file-path", path=str(dst)))
        resolution_journal.record_resolution(
            _WRITER_ID, _CLAUSE_PLATFORM_LOCALIZE, platform_localize_resolved,
        )

    # --- Step 3e: general orphan prune (AC18) ---
    from coordinator_core.install.ensure_venv import (  # local import: avoid import cost on --help
        SITEPACKAGES_POINTER_NAME,
    )

    # `SITEPACKAGES_POINTER_NAME` is folded into this union rather than into
    # `_static_bin_family_names()` -- that function is DERIVED (manifest +
    # `_CH_FAMILY_FILES` + `_RM_FAMILY_FILES`) and feeds
    # `test_bin_family_freshness.py`'s `src_by_name` from the same three
    # sources, which KeyErrors on an installer-synthesized file with no
    # template source. Registering it here instead means a future RENAME of
    # the pointer is self-cleaning via `_prune_orphaned_static_bin_names`
    # (see `ensure_venv.SITEPACKAGES_POINTER_NAME`'s docstring) --
    # `_prune_orphaned_static_bin_names` only considers names dropped from
    # THIS union across runs, so an unregistered name is never a prune
    # candidate in the first place.
    all_current_names = (
        _static_bin_family_names(claude_klabauter_root_resolved)
        | set(agent_helper_target_map) | set(agent_cmd_dest_map.values())
        | {SITEPACKAGES_POINTER_NAME}
    )
    _prune_orphaned_static_bin_names(bin_dst, all_current_names, check_only)


def _percolation_and_path_steps(
    setup_src: Path, setup_files: List[str], setup_exec_files: List[str],
    setup_hook_files: List[str], install_base: str, bin_dst: Path, check_only: bool,
) -> None:
    # --- Step 3d: percolation mechanism (~/.claude/setup/) ---
    setup_dest = Path(install_base) / ".claude" / "setup"
    if not check_only:
        setup_dest.mkdir(parents=True, exist_ok=True)

    # C6/AC6/AC6a/AC7: resolve git identity ONCE for this destination
    # DIRECTORY (one `git ls-files` spawn total, never per file) to get the
    # set of paths TRACKED under it. `None` means the probe is UNAVAILABLE
    # (no git on PATH) — degrade every overwrite decision to "refuse" below;
    # cold creation is entirely unaffected either way (see
    # `_write_strategy_for`). The manifest set (AC7 — from
    # SETUP_TEMPLATE_FILES + SETUP_TEMPLATE_HOOK_FILES, never a hardcoded
    # count) is also the blast-radius allowlist `_careful_write` enforces.
    tracked_set = _resolve_directory_tracked_set(setup_dest)
    manifest_relative_paths: "FrozenSet[str]" = frozenset(setup_files) | frozenset(setup_hook_files)
    install_base_path = Path(install_base)

    def _write_strategy_for(relative_path: str) -> str:
        # SCOPE THE GUARD TO THE OVERWRITE BRANCH ONLY: cold creation
        # (destination does not yet exist) ALWAYS proceeds via "force",
        # unconditionally, regardless of tracked-ness or repo membership —
        # a guard that also blocked cold creation would silently ship an
        # empty setup/ on first install (the maintainer-shape failure this
        # plan's Anti-scope forbids).
        if not (setup_dest / relative_path).exists():
            return "force"
        if tracked_set is None:
            return "refuse"
        if relative_path in tracked_set:
            return "careful"
        return "force"

    careful_backups: "list[WriteSurfaceEntry]" = []

    setup_files_resolved: "list[WriteSurfaceEntry]" = []
    for f in setup_files:
        exec_bit = f in setup_exec_files
        # SETUP_TEMPLATE_FILES is not flat — it carries nested entries (lib/*.sh),
        # so the parent must be created here exactly as the hook-file loop below
        # does. Without this, a cold install dies on the first nested entry with
        # FileNotFoundError from shutil.copyfile.
        if not check_only:
            (setup_dest / f).parent.mkdir(parents=True, exist_ok=True)
        backup_path = _install_one(
            setup_src / f, setup_dest / f, exec_bit, "machine-local", check_only,
            write_strategy=_write_strategy_for(f),
            careful_manifest_relative_paths=manifest_relative_paths,
            careful_relative_path=f,
            careful_install_base=install_base_path,
        )
        if not check_only:
            # Journaled unconditionally for every processed manifest entry —
            # a "careful" preserve or a "refuse" degrade still leaves this
            # destination genuinely governed by clause 5 this run, not just
            # a force-overwrite (WRITE_SURFACE clause 5's own docstring: the
            # write_strategy varies per entry, the managed-path fact does not).
            setup_files_resolved.append(WriteSurfaceEntry(kind="file-path", path=str(setup_dest / f)))
        if backup_path is not None:
            careful_backups.append(WriteSurfaceEntry(kind="file-path", path=str(backup_path)))
    if not check_only:
        resolution_journal.record_resolution(_WRITER_ID, _CLAUSE_SETUP_FILES, setup_files_resolved)

    setup_hook_files_resolved: "list[WriteSurfaceEntry]" = []
    for hf in setup_hook_files:
        if not check_only:
            (setup_dest / hf).parent.mkdir(parents=True, exist_ok=True)
        # Doctrine-tracked templates sourced from the coordinator-claude/DoE
        # tree, not operator config — force-overwrite on re-install so a
        # stale destination gets repaired rather than silently preserved
        # (see `_install_one`'s docstring § force_overwrite).
        backup_path = _install_one(
            setup_src / hf, setup_dest / hf, False, "machine-local", check_only,
            force_overwrite=True,
            write_strategy=_write_strategy_for(hf),
            careful_manifest_relative_paths=manifest_relative_paths,
            careful_relative_path=hf,
            careful_install_base=install_base_path,
        )
        if not check_only:
            setup_hook_files_resolved.append(WriteSurfaceEntry(kind="file-path", path=str(setup_dest / hf)))
        if backup_path is not None:
            careful_backups.append(WriteSurfaceEntry(kind="file-path", path=str(backup_path)))
    if not check_only:
        resolution_journal.record_resolution(_WRITER_ID, _CLAUSE_SETUP_HOOK_FILES, setup_hook_files_resolved)
        # WRITE_SURFACE clause 15 (`_CLAUSE_CAREFUL_BACKUP`): accumulated
        # across BOTH loops above (either can trigger a careful write) and
        # journaled once here — `record_resolution` is last-write-wins per
        # (writer_id, clause_index), so two separate calls (one per loop)
        # would silently drop whichever ran first.
        resolution_journal.record_resolution(_WRITER_ID, _CLAUSE_CAREFUL_BACKUP, careful_backups)

    # --- Step 3e-bin: provision settings-home/bin onto the POSIX PATH ---
    # F1/AC11 (P0): UNCONDITIONAL, with zero dependency on the claude-CLI
    # probe below finding a standalone `claude` binary under
    # <install_base>/.local/bin. Placing this after that probe's early
    # returns would silently skip bin_dst provisioning on any machine where
    # `claude` was installed via Homebrew/npm/the macOS app instead — the
    # exact silent-failure class this plan exists to end, reintroduced
    # inside its own fix. Appends (never prepends — AC5) so a colliding
    # system binary always wins the tie. No-ops on native Windows
    # (`_windows_health_steps` already owns Windows PATH provisioning for
    # this same directory — see AC10, unchanged by this leg).
    blocked = None if check_only else _refuse_machine_mutation(
        str(bin_dst), what="write settings-home bin dir PATH block into profile files",
        check_temp_path=False,
    )
    if blocked:
        print(f"[install-substrate] REFUSED: {blocked}", file=sys.stderr)
    else:
        write_path_entry_guard_blocks(
            path_entry=str(bin_dst),
            sentinel_id="SETTINGS_HOME_BIN",
            position="append",
            home=Path(install_base),
            check_only=check_only,
        )

    # --- Step 3e: ensure the standalone `claude` CLI dir is on the user PATH ---
    claude_bin = ""
    for cand in (
        Path(install_base) / ".local" / "bin" / "claude",
        Path(install_base) / ".local" / "bin" / "claude.exe",
    ):
        if cand.is_file():
            claude_bin = str(cand)
            break

    if not claude_bin:
        print(f"[setup] note: no standalone `claude` CLI found at {install_base}/.local/bin —")
        print("[setup]   if `claude` is not on your terminal PATH, install the CLI "
              "(https://docs.anthropic.com/en/docs/claude-code) so non-app shells can run it.")
        return

    if _is_windows_shell():
        claude_dir_win = _cygpath_w(str(Path(claude_bin).parent))
        if not claude_dir_win:
            print("[setup] WARNING: cygpath unavailable; cannot resolve Windows path for the claude CLI dir; skipping PATH integration", file=sys.stderr)
            return
        win_path = _win_user_path_entries()
        if win_path is None:
            print("[setup] WARNING: could not read Windows user PATH; skipping claude-CLI PATH integration", file=sys.stderr)
        else:
            entries, raw, value_type = win_path
            target = claude_dir_win.rstrip("\\")
            already = any(
                e.rstrip("\\").lower() == target.lower()
                or os.path.expandvars(e).rstrip("\\").lower() == target.lower()
                for e in entries
            )
            if not already:
                if check_only:
                    print(f"[install-substrate] would: add {claude_dir_win} (claude CLI) to Windows user PATH")
                else:
                    blocked = _refuse_machine_mutation(
                        str(Path(claude_bin).parent),
                        what="add claude CLI dir to Windows user PATH",
                    )
                    if blocked:
                        print(f"[setup] REFUSED: {blocked}", file=sys.stderr)
                    else:
                        _win_user_path_prepend(claude_dir_win, raw, value_type)
                        print(f"[setup] added {claude_dir_win} (claude CLI) to Windows user PATH — open a new shell for it to take effect")
        return

    # macOS / Linux: idempotent sentinel-guarded PATH block, written to
    # EVERY applicable profile/rc file (AC3 — fixes the trap where a single
    # `$SHELL`-picked file misses the interactive default shell), via the
    # generalized writer. Ordering (prepend — claude CLI wins ties) is
    # unchanged from the pre-generalization writer; only the file-selection
    # and sentinel form (BEGIN/END pair, not a one-line sentinel) change —
    # see the module docstring's § Two calling shapes for why the one-line
    # sentinel could never be cleanly removed on uninstall.
    claude_dir = str(Path(claude_bin).parent)
    if not check_only:
        blocked = _refuse_machine_mutation(
            claude_dir, what="write claude-CLI PATH block into profile files",
            check_temp_path=False,
        )
        if blocked:
            print(f"[setup] REFUSED: {blocked}", file=sys.stderr)
            return
    result = write_path_entry_guard_blocks(
        path_entry=claude_dir,
        sentinel_id="CLAUDE_CLI_PATH",
        position="prepend",
        home=Path(install_base),
        check_only=check_only,
    )
    if check_only:
        if result["already_present"]:
            print(f"[install-substrate] check: claude-CLI PATH block present in all applicable profile files under {install_base} (no-op)")
            return
        # Review: code-reviewer (Finding 5, nit) — `already_present is False`
        # covers both "sentinel genuinely absent" and "sentinel present but
        # stale" (content mismatch, e.g. after a COORDINATOR_SETTINGS_HOME
        # relocation — see shell_rc_guard's § Relocation self-heal). Thread
        # `stale_present` per file so the message says which, rather than
        # unconditionally claiming "absent" in the stale case too.
        absent = sorted(
            path for path, per_file in result["results"].items()
            if not per_file["already_present"] and not per_file.get("stale_present")
        )
        stale = sorted(
            path for path, per_file in result["results"].items()
            if not per_file["already_present"] and per_file.get("stale_present")
        )
        parts = []
        if absent:
            parts.append(f"sentinel absent from {', '.join(absent)}")
        if stale:
            parts.append(f"sentinel present but stale (needs update) in {', '.join(stale)}")
        raise SubstrateFatalError(
            "install-substrate: check failed: claude-CLI PATH "
            f"{'; '.join(parts)} (would prepend PATH block for {claude_dir})"
        )
    if result["modified"]:
        print(f"[setup] added {claude_dir} (claude CLI) to PATH — open a new shell to use `claude`")


def _c10a_steps(
    install_base: str, settings_home_path: Path, plugin_root: Path, bin_dst: Path, check_only: bool,
) -> int:
    legacy_whoami = Path(install_base) / ".claude" / _WHOAMI_DIRNAME
    dst_whoami = settings_home_path / _WHOAMI_DIRNAME

    if legacy_whoami.is_dir() and not is_pointer(legacy_whoami):
        src_whoami = legacy_whoami
    elif (plugin_root / "whoami").is_dir():
        src_whoami = plugin_root / "whoami"
    else:
        src_whoami = None
        # No legacy dir and no plugin-side whoami/ — a genuinely checked,
        # known fact (not "never got there"): clause 22 resolves to zero
        # entries this run.
        if not check_only:
            resolution_journal.record_resolution(_WRITER_ID, _CLAUSE_WHOAMI_COPY, [])

    if src_whoami is not None:
        # Mirror bash's exclusion-aware emptiness probe precisely.
        dst_has_files = dst_whoami.is_dir() and any(True for _ in _iter_whoami_files(dst_whoami))
        if not dst_has_files:
            if check_only:
                print(f"[install-substrate] would: relocate coordinator-whoami/ from {src_whoami} to {dst_whoami}")
            else:
                dst_whoami.mkdir(parents=True, exist_ok=True)
                whoami_copied: "list[WriteSurfaceEntry]" = []
                try:
                    for rel in _iter_whoami_files(src_whoami):
                        _c10a_copy_one(src_whoami / rel, dst_whoami / rel)
                        whoami_copied.append(WriteSurfaceEntry(kind="file-path", path=str(dst_whoami / rel)))
                    for dirpath, dirnames, filenames in os.walk(src_whoami):
                        dirnames[:] = [d for d in dirnames if d not in _WHOAMI_EXCLUDE_DIRS and not d.endswith(".egg-info")]
                        if not dirnames and not filenames and Path(dirpath) != src_whoami:
                            rel = Path(dirpath).relative_to(src_whoami)
                            (dst_whoami / rel).mkdir(parents=True, exist_ok=True)
                except SubstrateFatalError as exc:
                    # Journal whatever copies genuinely completed before the
                    # divergent-file abort — each entry in `whoami_copied`
                    # really did land on disk; only the remainder never
                    # happened. Never phantom, never silently dropped.
                    resolution_journal.record_resolution(_WRITER_ID, _CLAUSE_WHOAMI_COPY, whoami_copied)
                    print(str(exc), file=sys.stderr)
                    return 1
                resolution_journal.record_resolution(_WRITER_ID, _CLAUSE_WHOAMI_COPY, whoami_copied)
        else:
            # dst_whoami already has files — this run genuinely resolves
            # clause 22 to "nothing to copy" (a known, meaningful zero),
            # distinct from src_whoami being None below (never got there).
            if not check_only:
                resolution_journal.record_resolution(_WRITER_ID, _CLAUSE_WHOAMI_COPY, [])

    if legacy_whoami.is_dir() and not is_pointer(legacy_whoami) and dst_whoami.is_dir():
        legacy_whoami_blocked = None if check_only else _refuse_machine_mutation(
            str(legacy_whoami), what="remove/replace legacy coordinator-whoami directory",
            check_temp_path=False,
        )
        if check_only:
            print(f"[install-substrate] would: replace {legacy_whoami} (real dir) with compat pointer → {dst_whoami}")
        elif legacy_whoami_blocked:
            print(f"[install-substrate] REFUSED: {legacy_whoami_blocked}", file=sys.stderr)
        else:
            platform = _quiet_output(["uname", "-s"])
            if platform.startswith(("MINGW", "MSYS", "CYGWIN")):
                if not shutil.which("cygpath"):
                    print(
                        "install-substrate C10a: FATAL — cygpath not found on Windows host; "
                        "cannot create coordinator-whoami junction.",
                        file=sys.stderr,
                    )
                    print("  Remediation: ensure cygpath is on PATH (provided by MSYS2, Cygwin, or Git-for-Windows).", file=sys.stderr)
                    return 1
                win_legacy = _cygpath_w(str(legacy_whoami))
                win_dst = _cygpath_w(str(dst_whoami))
                try:
                    legacy_whoami.rmdir()
                except OSError:
                    shutil.rmtree(legacy_whoami, ignore_errors=True)
                proc = _run(["cmd", "/c", "mklink", "/J", win_legacy, win_dst], capture_output=True)
                if proc.returncode != 0:
                    print("install-substrate C10a: FATAL — mklink /J failed for coordinator-whoami.", file=sys.stderr)
                    print(f"  Link path: {win_legacy}", file=sys.stderr)
                    print(f"  Target   : {win_dst}", file=sys.stderr)
                    print("  Note: mklink /J does not require elevation or Developer Mode.", file=sys.stderr)
                    return 1
                print(f"[install-substrate] installed coordinator-whoami compat junction: {legacy_whoami} → {dst_whoami}")
            else:
                shutil.rmtree(legacy_whoami, ignore_errors=True)
                legacy_whoami.symlink_to(dst_whoami)
                print(f"[install-substrate] installed coordinator-whoami compat symlink: {legacy_whoami} → {dst_whoami}")

    # Step C10a-2: register coordinator.whoami_src registry key.
    # Windows CreateProcess cannot exec an extension-less shebang script (WinError
    # 193). The substrate deliberately delivers a `machine-local.cmd` alongside the
    # POSIX wrapper for exactly this reason — prefer it when it exists. Keyed on
    # os.name, NOT _is_windows_shell(): the constraint is the OS exec loader, which
    # applies under Git Bash just the same.
    ml_cli = bin_dst / "machine-local"
    if os.name == "nt" and (bin_dst / "machine-local.cmd").is_file():
        ml_cli = bin_dst / "machine-local.cmd"
    # is_executable() handles the Windows PATHEXT-sibling case (e.g. a bare
    # "machine-local" whose launchable form is "machine-local.cmd") itself,
    # so no separate os.name == "nt" carve-out is needed here.
    if ml_cli.is_file() and is_executable(ml_cli):
        cur = _quiet_output([str(ml_cli), "get", "coordinator.whoami_src"])
        if check_only:
            if cur != str(dst_whoami):
                print(f"[install-substrate] would: set coordinator.whoami_src → {dst_whoami}")
        elif cur != str(dst_whoami):
            _run([str(ml_cli), "set", "coordinator.whoami_src", str(dst_whoami)])
            print(f"[install-substrate] set coordinator.whoami_src → {dst_whoami}")
    else:
        print(f"[install-substrate] WARNING: machine-local CLI not found at {ml_cli}; coordinator.whoami_src not persisted", file=sys.stderr)

    # Step C10a-3: venv rebuild + legacy venv removal (native — coordinator_core.install.ensure_venv).
    from coordinator_core.install.ensure_venv import (  # local import: avoid import cost on --help
        EnsureVenvError,
        _venv_healthy,
        ensure_coordinator_venv,
        venv_python_path,
    )

    legacy_venv = Path(install_base) / ".claude" / _LEGACY_VENV_DIRNAME
    venv_py = venv_python_path(settings_home_path / _LEGACY_VENV_DIRNAME)

    has_viable_whoami = (
        (dst_whoami / "pyproject.toml").is_file() or (dst_whoami / "setup.py").is_file()
        or (plugin_root / "whoami" / "pyproject.toml").is_file()
        or (plugin_root / "whoami" / "setup.py").is_file()
    )
    has_fallback_venv = (settings_home_path / ".coordinator-venv").is_dir() or legacy_venv.is_dir()
    claude_home_env = os.environ.get("CLAUDE_HOME")

    if check_only:
        if has_viable_whoami:
            try:
                status = ensure_coordinator_venv(
                    plugin_root, settings_home_path, claude_home=claude_home_env, check_only=True,
                )
                print(f"[install-substrate] venv check: {status}")
            except EnsureVenvError as exc:
                print(f"[install-substrate] WARNING: venv check failed: {exc}", file=sys.stderr)
        else:
            print("[install-substrate] WARNING: no valid coordinator_whoami package source found; skipping venv check", file=sys.stderr)
        if legacy_venv.is_dir():
            print(f"[install-substrate] would: remove legacy venv at {legacy_venv} (after settings-home venv health probe)")
    elif has_viable_whoami:
        try:
            status = ensure_coordinator_venv(
                plugin_root, settings_home_path, claude_home=claude_home_env, check_only=False,
                # Opt into pin-clearing here (unlike the default): this call
                # site's own disposition is fatal-with-fallback -- a dangling
                # pin left in place would hard-fail every subsequent
                # coordinator invocation via pyresolve, not just this one
                # install run. See ensure_coordinator_venv's docstring.
                clear_pin_on_failure=True,
            )
        except EnsureVenvError as exc:
            if has_fallback_venv:
                print(f"[install-substrate] WARNING: venv rebuild failed ({exc}) — substrate seeded; fallback venv retained.", file=sys.stderr)
                print("[install-substrate]   Transient failure — re-run install or it self-heals at next session-init.", file=sys.stderr)
            else:
                print(f"install-substrate C10a: venv rebuild failed ({exc}); no fallback venv available", file=sys.stderr)
                return 1
        else:
            print(f"[install-substrate] venv: {status}")
            # Review: code-reviewer (Finding 3) — use the B2 both-imports health
            # oracle (coordinator_whoami AND pydantic), not a narrower
            # coordinator_whoami-only probe, before treating this as the gate
            # that clears deletion of the legacy venv fallback.
            venv_healthy = _venv_healthy(venv_py)
            if not venv_healthy:
                if has_fallback_venv:
                    print(f"[install-substrate] WARNING: venv health probe failed (coordinator_whoami/pydantic not importable under {venv_py}); fallback venv retained.", file=sys.stderr)
                    print("[install-substrate]   Re-run install or it self-heals at next session-init.", file=sys.stderr)
                else:
                    print(f"install-substrate C10a: venv health probe failed — coordinator_whoami/pydantic not importable under {venv_py}", file=sys.stderr)
                    return 1
            elif legacy_venv.is_dir():
                blocked = _refuse_machine_mutation(
                    str(legacy_venv), what="remove legacy .coordinator-venv directory",
                    check_temp_path=False,
                )
                if blocked:
                    print(f"[install-substrate] REFUSED: {blocked}", file=sys.stderr)
                else:
                    shutil.rmtree(legacy_venv)
                    print(f"[install-substrate] removed legacy venv at {legacy_venv} (settings-home venv healthy)")
    else:
        print(
            f"[install-substrate] WARNING: no valid coordinator_whoami package source found "
            f"(neither {dst_whoami} nor {plugin_root}/whoami has pyproject.toml/setup.py); "
            "skipping venv rebuild — venv builds automatically once whoami source is in place (re-run install)",
            file=sys.stderr,
        )

    return 0


def _load_seed_wiki_manifest(plugin_root: Path) -> "list[str]":
    """Load ``<plugin_root>/schemas/seed-wikis.json``'s ratified
    ``seed_wikis`` list — the single source of truth for which
    ``coordinator/docs/wiki/`` pages are resolvable cross-repo/cross-machine
    via the settings home. NEVER glob ``<plugin_root>/docs/wiki/*.md``
    directly: a dev clone holds 200+ internal doctrine pages, and a
    wholesale glob would make every one of them resolve locally while still
    404-ing for an OSS/sibling-repo reader — the precise
    works-here-broken-there defect this manifest exists to close.

    Format (schema_version 1, PINNED):
    ``{"schema_version": 1, "seed_wikis": ["name.md", "..."]}``.
    """
    manifest = plugin_root / "schemas" / "seed-wikis.json"
    if not manifest.is_file():
        raise SubstrateFatalError(
            f"install-substrate: seed-wikis.json not found at {manifest} — "
            "cannot resolve which wiki pages are ratified for cross-repo citation"
        )
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SubstrateFatalError(
            f"install-substrate: seed-wikis.json at {manifest} is malformed ({exc})"
        ) from exc

    pages = data.get("seed_wikis") if isinstance(data, dict) else None
    if not isinstance(pages, list) or not pages:
        raise SubstrateFatalError(
            f"install-substrate: seed-wikis.json at {manifest} does not declare a "
            "non-empty 'seed_wikis' list — corrupt or wrong schema"
        )
    if not all(isinstance(p, str) and p for p in pages):
        raise SubstrateFatalError(
            f"install-substrate: seed-wikis.json at {manifest} 'seed_wikis' entries "
            "must all be non-empty strings"
        )
    return pages


def _install_seed_wikis(plugin_root: Path, settings_home_path: Path, check_only: bool) -> None:
    """Copy the ratified seed wiki pages (``schemas/seed-wikis.json``) from
    ``<plugin_root>/docs/wiki/`` into
    ``<settings_home>/coordinator-claude/docs/wiki/`` — the only path by
    which a percolating prompt surface's wiki citation is resolvable from a
    sibling repo, another operator's machine, or an OSS install. See
    ``_load_seed_wiki_manifest`` for why this is manifest-driven rather than
    a directory glob.

    UNLIKE ``coordinator-whoami`` (operator-customized, preserve-on-diff —
    see ``_c10a_copy_one``): these pages are a derived cache of doctrine
    content this settings-home copy doesn't author, so a stale destination
    is overwritten unconditionally on every re-run rather than preserved as
    if it were operator customization. Do not "fix" this into
    preserve-semantics — that would let a destination silently drift stale
    against the manifest.

    A manifest entry with no corresponding file under
    ``<plugin_root>/docs/wiki/`` is reported by name (never a silent skip,
    never a partial-install abort) so the remaining pages still install.
    """
    pages = _load_seed_wiki_manifest(plugin_root)
    src_wiki = plugin_root / "docs" / "wiki"
    dst_wiki = settings_home_path / "coordinator-claude" / "docs" / "wiki"

    missing: "list[str]" = []
    if not check_only:
        dst_wiki.mkdir(parents=True, exist_ok=True)

    for name in pages:
        src = src_wiki / name
        dst = dst_wiki / name
        if not src.is_file():
            missing.append(name)
            print(
                f"[install-substrate] WARNING: seed wiki page {name!r} is named in "
                f"seed-wikis.json but absent at {src} — skipping",
                file=sys.stderr,
            )
            continue
        if check_only:
            if dst.is_file() and filecmp.cmp(src, dst, shallow=False):
                print(f"[install-substrate] check: {name} up to date -> {dst} (no-op)")
            else:
                print(f"[install-substrate] would: copy seed wiki {name} -> {dst}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    if missing:
        print(
            f"[install-substrate] {len(missing)} seed wiki page(s) named in "
            f"seed-wikis.json missing on disk: {', '.join(missing)}",
            file=sys.stderr,
        )


def _fnm_curl_leg_declined() -> bool:
    """True when the `curl https://fnm.vercel.app/install | bash` leg must not
    run. Prints the reason and the manual alternative; never raises.

    Two independent reasons:

    1. Native Windows. fnm's official shell installer rejects this platform
       outright (`OS MSYS_NT-10.0-26200 is not supported`), so the leg could
       only ever fetch a remote script, execute it, and fail. `winget install
       Schniz.fnm` is the Windows path.
    2. No consent. This leg downloads a remote script and pipes it into a
       shell — an external, unpinned code-execution step, for an OPTIONAL
       dependency (per-repo Node pinning; core substrate is unaffected without
       it). It is opt-in: set COORDINATOR_INSTALL_FNM=1, or answer the prompt
       on an interactive terminal. Silence means declined, and declining costs
       the operator nothing that matters.
    """
    fnm_manual = (
        "install fnm manually if you need per-repo Node pinning: "
        "https://github.com/Schniz/fnm#installation"
    )
    if os.name == "nt":
        print(
            "[setup] fnm: skipping the curl installer — not supported on Windows. "
            "Install with: winget install Schniz.fnm",
            file=sys.stderr,
        )
        return True

    if os.environ.get("COORDINATOR_INSTALL_FNM") == "1":
        return False

    interactive = sys.stdin.isatty() and os.environ.get("COORDINATOR_NON_INTERACTIVE") != "1"
    if interactive:
        print(
            "[setup] fnm is absent. Installing it runs a remote script "
            "(https://fnm.vercel.app/install) through bash.",
        )
        try:
            consent = input("[setup] Fetch and run it? [y/N] ")
        except EOFError:
            consent = ""
        if consent[:1] in ("y", "Y"):
            return False
        print(f"[setup] fnm: declined — optional, core substrate unaffected; {fnm_manual}")
        return True

    print(
        f"[setup] fnm: absent, and its installer is a remote script — not fetched without "
        f"consent. Set COORDINATOR_INSTALL_FNM=1 to opt in, or {fnm_manual}",
        file=sys.stderr,
    )
    return True


def _fnm_step(check_only: bool) -> None:
    if check_only:
        fnm_path = shutil.which("fnm")
        if fnm_path:
            print(f"[install-substrate] check: fnm already present at {fnm_path} (no-op)")
        else:
            print(
                "[install-substrate] check: fnm absent (would install via brew, or via the "
                "remote curl installer only with consent / COORDINATOR_INSTALL_FNM=1, and "
                "never on Windows; optional, core substrate unaffected)"
            )
        return
    fnm_path = shutil.which("fnm")
    if fnm_path:
        print(f"[setup] fnm already installed at {fnm_path} — skipping binary install")
        return
    fnm_manual = "install fnm manually if you need per-repo Node pinning: https://github.com/Schniz/fnm#installation"
    blocked = _refuse_machine_mutation("fnm", what="install fnm via brew/curl")
    if blocked:
        print(f"[install-substrate] REFUSED: {blocked}", file=sys.stderr)
        return
    if shutil.which("brew"):
        print("[setup] installing fnm via brew...", flush=True)
        proc = _run(["brew", "install", "fnm"], timeout=300)
        if proc.returncode == 0:
            print("[setup] fnm installed via brew")
        else:
            print(f"[setup] WARNING: brew install fnm failed — optional, core substrate unaffected; {fnm_manual}", file=sys.stderr)
    elif not shutil.which("curl"):
        print(f"[setup] WARNING: cannot install fnm — neither brew nor curl available; optional, core substrate unaffected; {fnm_manual}", file=sys.stderr)
    elif _fnm_curl_leg_declined():
        # _fnm_curl_leg_declined printed the reason and the manual alternative.
        pass
    else:
        # flush=True: the subprocess writes straight to the console, so an
        # unflushed "installing..." line landed AFTER the installer's own
        # failure output — the run read as if the failure preceded the attempt.
        print("[setup] installing fnm via official curl installer...", flush=True)
        try:
            curl_proc = subprocess.run(["curl", "-fsSL", "https://fnm.vercel.app/install"], capture_output=True, timeout=60, **_NO_CONSOLE)
            if curl_proc.returncode != 0:
                # Review: coordinator:code-reviewer — a failed/partial curl must
                # never feed its (possibly empty/garbage) stdout into `bash -s`;
                # short-circuit before spawning bash rather than joint-checking
                # both return codes only after both have already run.
                ok = False
            else:
                install_proc = subprocess.run(["bash", "-s", "--", "--skip-shell"], input=curl_proc.stdout, timeout=300, **_NO_CONSOLE)
                ok = install_proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        if ok:
            print("[setup] fnm installed via curl installer")
        else:
            print(f"[setup] WARNING: curl installer for fnm failed — optional, core substrate unaffected; {fnm_manual}", file=sys.stderr)


def _windows_health_steps(bin_dst: Path, check_only: bool) -> None:
    # 3b: ensure the resolved bin dir on Windows user PATH.
    bin_dst_win = _cygpath_w(str(bin_dst))
    if not bin_dst_win:
        print(
            f"[setup] WARNING: cygpath unavailable; cannot resolve Windows path for {bin_dst}; "
            "skipping PATH integration — bare-name CLI invocation will fail. Install cygpath "
            "(provided by MSYS2, Cygwin, or Git-for-Windows) and re-run install.",
            file=sys.stderr,
        )
    else:
        win_path = _win_user_path_entries()
        if win_path is None:
            print(
                "[setup] WARNING: could not read Windows user PATH from "
                "HKCU\\Environment; skipping PATH integration — bare-name CLI "
                f"invocation will fail. Add {bin_dst_win} to your user PATH "
                "manually, or re-run install.",
                file=sys.stderr,
            )
        else:
            entries, raw, value_type = win_path
            target = bin_dst_win.rstrip("\\")
            already = any(
                e.rstrip("\\").lower() == target.lower()
                or os.path.expandvars(e).rstrip("\\").lower() == target.lower()
                for e in entries
            )
            if not already:
                if check_only:
                    print(f"[install-substrate] would: add {bin_dst_win} to Windows user PATH")
                else:
                    blocked = _refuse_machine_mutation(
                        str(bin_dst), what="add settings-home bin dir to Windows user PATH",
                    )
                    if blocked:
                        print(f"[setup] REFUSED: {blocked}", file=sys.stderr)
                    else:
                        _win_user_path_prepend(bin_dst_win, raw, value_type)
                        print(f"[setup] added {bin_dst_win} to Windows user PATH — open a new shell/Claude session for it to take effect")

    # 3c-1: orphan AppX stub detection.
    local_app_data = os.environ.get("LOCALAPPDATA")
    for stub_name in ("python.exe", "python3.exe"):
        if not local_app_data:
            break
        candidate = Path(local_app_data) / "Microsoft" / "WindowsApps" / stub_name
        stub_path = str(candidate) if _orphan_appx_stub(str(candidate)) else ""
        if stub_path:
            print(f"[setup] Detected orphan AppX stub: {stub_path}")
            print("[setup]   Zero-byte reparse-point from an uninstalled Store Python package.")
            print("[setup]   Intercepts python3/python invocations via AppX App-Execution-Alias,")
            print("[setup]   popping the 'Select an app' picker (PATH lookup never runs).")
            print("[setup]   Regenerates if Store Python is reinstalled.")
            blocked = _refuse_machine_mutation(stub_path, what="delete orphan AppX python stub")
            if blocked:
                # Checked BEFORE the consent prompt below fires — prompting an
                # operator for permission to do something already refused is
                # worse than either outcome alone (Coordinator ruling on
                # state/bug-backlog/2026-08-06-coordinator-disable-machine-
                # mutation-cov-70b1bc2d3e77.yaml, follow-up). The message must
                # read as "the disable var is set", never as a failed probe or
                # a declined consent — _refuse_machine_mutation's own reason
                # string already carries that framing.
                print(f"[setup] REFUSED: {blocked}", file=sys.stderr)
            elif sys.stdin.isatty() and os.environ.get("COORDINATOR_NON_INTERACTIVE") != "1":
                try:
                    consent = input("[setup] Delete this orphan stub? [y/N] ")
                except EOFError:
                    consent = ""
                if consent[:1] in ("y", "Y"):
                    try:
                        os.remove(stub_path)
                    except OSError as exc:
                        print(f"[setup]   Could not delete: {exc}", file=sys.stderr)
                    else:
                        print("[setup]   Deleted.")
            else:
                print("[setup]   (non-interactive context: skipping deletion; re-run in interactive shell to clean up)")

    # 3c-2: store-alias-on-PATH warning
    py_resolved = shutil.which("python3") or shutil.which("python") or ""
    if "WindowsApps" in py_resolved:
        print(f"[setup] WARNING: python/python3 resolves under WindowsApps: {py_resolved}")
        print("[setup]   Install Python from python.org OR disable App Execution Aliases via")
        print("[setup]   Settings → Apps → Advanced app settings → App execution aliases.")

    # 3c-3: no-Python-at-all detection
    have_py = "yes" if shutil.which("py") else "no"
    if have_py != "yes" and not py_resolved:
        print("[setup] WARNING: neither py.exe nor python/python3 found.")
        print("[setup]   Install Python 3 from https://www.python.org/downloads/windows/ —")
        print("[setup]   the installer ships py.exe by default. Without it, python3.cmd has nothing to call.")


_WRITER_ID = "install-substrate"
"""`WRITE_SURFACE.writer_id`, extracted so the resolution-journal call
sites below (which run long before `WRITE_SURFACE` itself is assigned,
but always AFTER module load completes) and `WRITE_SURFACE` read one
spelling rather than risking drift between the two."""

# `resolution_journal.record_resolution`'s `clause_index` for each of this
# module's ten `ShapedClause` declarations — the position of that clause
# within `WRITE_SURFACE.clauses` below (0-indexed). Named here, read at
# each clause's write site, so a future clause insertion/reorder in
# `WRITE_SURFACE` is the ONE place that needs updating, not N scattered
# magic-number call sites.
_CLAUSE_AGENT_HELPER_FORWARDERS = 3  # clause 4 (comment numbering below)
_CLAUSE_SETUP_FILES = 4  # clause 5
_CLAUSE_SETUP_HOOK_FILES = 5  # clause 6
_CLAUSE_ML_FAMILY = 6  # clause 7
_CLAUSE_ML_EXPLICIT = 7  # clause 8
_CLAUSE_PLATFORM_LOCALIZE = 8  # clause 9
_CLAUSE_ORPHAN_SWEEP = 12  # clause 13
_CLAUSE_PRUNE_ORPHANED_STATIC = 13  # clause 14
_CLAUSE_CAREFUL_BACKUP = 14  # clause 15
_CLAUSE_WHOAMI_COPY = 21  # clause 22


WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id=_WRITER_ID,
    source_module="coordinator_core.install.substrate",
    clauses=(
        # Clause 1 — Windows-only: `_percolation_and_path_steps` adds the
        # standalone claude-CLI directory to the Windows user PATH via
        # `[Environment]::SetEnvironmentVariable('PATH', ..., 'User')`.
        # Registry-backed but env-var SHAPED — `os-env-var`, not
        # `machine-local-key`. Distinct call site/value from clause 2 below;
        # collapsing the two would lose which directory a given machine got.
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="os-env-var",
                    key="PATH",
                    reason=(
                        "Windows user PATH: claude-CLI directory, added in "
                        "_percolation_and_path_steps (native-Windows branch only)"
                    ),
                ),
            ),
        ),
        # Clause 2 — Windows-only: `_windows_health_steps` adds the
        # resolved settings-home bin directory to the Windows user PATH via
        # the same registry-backed `SetEnvironmentVariable` call, at a
        # separate call site with a separate value from clause 1.
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="os-env-var",
                    key="PATH",
                    reason=(
                        "Windows user PATH: settings-home bin directory, added "
                        "in _windows_health_steps (native-Windows branch only)"
                    ),
                ),
            ),
        ),
        # Clause 3 — Windows-only, consent-gated: `_windows_health_steps`
        # deletes an orphan zero-byte AppX `python(3).exe` reparse-point
        # stub under `%LOCALAPPDATA%\Microsoft\WindowsApps\`, only after an
        # interactive "Delete this orphan stub? [y/N]" prompt. `effect=
        # "delete"` per DoE's ruling that consent-gated machine mutations
        # must carry a paper trail even when they remove rather than write.
        StaticClause(
            effect="delete",
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path="%LOCALAPPDATA%\\Microsoft\\WindowsApps\\python.exe",
                    effect="delete",
                    reason="consent-gated orphan AppX stub removal (_windows_health_steps)",
                ),
                WriteSurfaceEntry(
                    kind="file-path",
                    path="%LOCALAPPDATA%\\Microsoft\\WindowsApps\\python3.exe",
                    effect="delete",
                    reason="consent-gated orphan AppX stub removal (_windows_health_steps)",
                ),
            ),
        ),
        # Clause 4 — the agent-helper forwarder TRIPLE generator: ONE
        # discovery mechanism (`_derive_agent_helper_target_map`, scanning
        # `coordinator/bin/`) feeding ONE fixed `.py`/`.cmd`/`.ps1` forwarder
        # template set per discovered CLI. Was a pair before the
        # ps1-launcher-class plan's C3/C4 (`.py`+`.cmd` only); the `.ps1`
        # leg is now a third emission off the SAME discovery mechanism and
        # the SAME resolved-collision map (`_resolve_agent_cmd_dest_
        # collisions`) the `.cmd` leg already used, via
        # `_emit_and_verify_ps1_forwarders` — one more suffix out of the one
        # discovery mechanism, not a second clause. SHAPED, not a frozen
        # count of static entries — the map's size varies with
        # `coordinator/bin/`'s contents and is not enumerable in source.
        #
        # The `.ps1` leg is also, uniquely among this clause's three
        # suffixes, sometimes UNDONE seconds after being written: on a RED
        # `evaluate_policy_gate()` verdict, `_emit_and_verify_ps1_forwarders`
        # unlinks every `.ps1` path this SAME clause's SAME call just wrote
        # (AC7). That rollback still belongs here, not in a clause of its
        # own — unlike clause 13's orphan sweep (`_CLAUSE_ORPHAN_SWEEP`),
        # which deletes files a PRIOR install run wrote and therefore earns
        # its own clause and its own resolution-journal entry, this delete
        # only ever removes what this clause's own write loop emitted
        # moments earlier in the SAME call. A clause describes one writer's
        # declared surface, not a append-only journal of every write that
        # ever landed — see `_emit_and_verify_ps1_forwarders`'s own
        # resolution_journal note for why no journal entry is recorded for
        # this leg either.
        ShapedClause(
            discovered_by="_derive_agent_helper_target_map",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path="<agent-bin-dir>/<forwarder-name>[.py|.cmd|.ps1]",
            ),
        ),
        # Clause 5 — percolation, operator-preservable half:
        # `_percolation_and_path_steps` copies every entry of
        # `SETUP_TEMPLATE_FILES` (from `_load_setup_template_manifest`,
        # keyed off `_MANIFEST_ATTRS[0]`) into `<install_base>/.claude/
        # setup/`, a tree (not flat — nested entries like `lib/*.sh` land at
        # their relative sub-path). SHAPED: the manifest is read at runtime
        # from `coordinator/lib/setup-templates-manifest.py`, so the entry
        # set is neither static nor safe to flatten here (this plan's
        # anti-scope). `SETUP_TEMPLATE_EXEC_FILES` is not a second
        # destination set — it only flags the exec bit on entries already
        # in `SETUP_TEMPLATE_FILES`, so it earns no clause of its own.
        ShapedClause(
            discovered_by="_load_setup_template_manifest (SETUP_TEMPLATE_FILES)",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path="<install_base>/.claude/setup/<relative-template-path>",
                reason=(
                    "write_strategy is per-entry (_write_strategy_for): "
                    "force on cold creation, else careful/refuse depending "
                    "on a git-tracked-ness probe of the destination — "
                    "operator edits under source control are preserved, "
                    "never force-overwritten"
                ),
            ),
        ),
        # Clause 6 — percolation, doctrine-tracked half: the same
        # destination tree, but for `SETUP_TEMPLATE_HOOK_FILES`
        # (`_MANIFEST_ATTRS[2]`), which `_percolation_and_path_steps`
        # always installs with `force_overwrite=True` — a genuinely
        # different overwrite story from clause 5 (repaired on re-install
        # rather than preserved), so it is kept as its own clause rather
        # than merged with clause 5's operator-preservable entries.
        ShapedClause(
            discovered_by="_load_setup_template_manifest (SETUP_TEMPLATE_HOOK_FILES)",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path="<install_base>/.claude/setup/<relative-hook-path>",
                reason=(
                    "force_overwrite=True unconditionally (doctrine-tracked "
                    "template, repaired rather than preserved on "
                    "re-install); write_strategy (_write_strategy_for) "
                    "still gates the underlying copy the same as clause 5"
                ),
            ),
        ),
        # Clause 7 — the `<settings-home>/bin/` static manifest-driven ML
        # family: `_install_bin_resolvers`'s `ml_family()` closure copies
        # every `bin_manifest.ml_family` entry (from
        # `_load_bin_templates_manifest`'s `ML_FAMILY_FILES` group,
        # `coordinator/lib/bin-templates-manifest.py`) into `bin_dst`.
        # SHAPED: the manifest is read at runtime, same reasoning as clause
        # 5/6 above — never flattened here.
        ShapedClause(
            discovered_by=f"_load_bin_templates_manifest ({_BIN_TEMPLATE_MANIFEST_GROUP_ATTRS[0]})",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path="<settings-home>/bin/<ml-family-filename>",
                reason="force_overwrite=True unconditionally (ml_family closure)",
            ),
        ),
        # Clause 8 — the same `<settings-home>/bin/` surface, but for the
        # `ML_EXPLICIT_FILES` group, installed at Step 3 by
        # `_install_bin_resolvers`'s direct loop over
        # `bin_manifest.ml_explicit` — a separate manifest attribute, and a
        # separate (inline, non-closure) call site from clause 7, so kept
        # as its own clause per the discovered-by discriminator.
        ShapedClause(
            discovered_by=f"_load_bin_templates_manifest ({_BIN_TEMPLATE_MANIFEST_GROUP_ATTRS[1]})",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path="<settings-home>/bin/<ml-explicit-filename>",
                reason="force_overwrite=True unconditionally (Step 3 ml_explicit loop)",
            ),
        ),
        # Clause 9 — Step 3c's platform-localize hook: the
        # `PLATFORM_LOCALIZE_FILES` group, installed by its own loop in
        # `_install_bin_resolvers` (distinct call site/manifest attribute
        # from clauses 7/8).
        ShapedClause(
            discovered_by=f"_load_bin_templates_manifest ({_BIN_TEMPLATE_MANIFEST_GROUP_ATTRS[2]})",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path="<settings-home>/bin/<platform-localize-filename>",
                reason="force_overwrite=True unconditionally (Step 3c platform-localize loop)",
            ),
        ),
        # Clause 10 — the claude-home family: a STATIC, hand-maintained
        # 3-entry tuple (`_CH_FAMILY_FILES`), sourced from claude-klabauter's own
        # `coordinator/lib/claude-home/` (NOT DoE's `templates/bin/`, so
        # deliberately out of `bin-templates-manifest.py`/clauses 7-9 by
        # construction — see `_CH_FAMILY_FILES`'s own comment). Genuinely
        # enumerable in source, unlike the manifest-driven groups above, so
        # this is the STATIC form, not SHAPED.
        StaticClause(
            entries=tuple(
                WriteSurfaceEntry(
                    kind="file-path",
                    path=f"<settings-home>/bin/{name}",
                    reason="force_overwrite=True unconditionally (ch_family closure)",
                )
                for name, _exec_bit in _CH_FAMILY_FILES
            ),
        ),
        # Clause 11 — the resolve-claude-klabauter family: a STATIC 1-entry tuple
        # (`_RM_FAMILY_FILES`), sourced from
        # `coordinator/lib/resolve-claude-klabauter/` — a distinct source tree/family
        # from clause 10, kept separate per the plan's per-family-not-
        # collapsed guidance.
        StaticClause(
            entries=tuple(
                WriteSurfaceEntry(
                    kind="file-path",
                    path=f"<settings-home>/bin/{name}",
                    reason="rm_family closure, installed once per bin dir",
                )
                for name in _RM_FAMILY_FILES
            ),
        ),
        # Clause 12 — `_write_bin_manifest`: the installer's own provenance
        # ledger, `<settings-home>/bin/.coordinator-bin-manifest.json`,
        # rewritten wholesale on every real (non-check-only) run with the
        # current complete write-set. Earns its own clause (rather than
        # folding into clauses 7-11) because clauses 13/14's delete legs
        # key directly off this file's prior contents.
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path=f"<settings-home>/bin/{_BIN_MANIFEST_FILENAME}",
                    reason="installer's own provenance ledger (_write_bin_manifest)",
                ),
            ),
        ),
        # Clause 13 — `_sweep_orphaned_agent_helpers`: deletes marker-
        # carrying `<settings-home>/bin/` forwarders whose provenance test
        # (marker match AND absence from this run's complete agent-helper
        # write-set) both hold — orphaned launcher pairs left behind when a
        # CLI is deleted/renamed in `coordinator/bin/`. effect="delete";
        # SHAPED because the eligible name set is discovered per run, never
        # enumerable in source.
        ShapedClause(
            effect="delete",
            discovered_by="_sweep_orphaned_agent_helpers (marker-provenance orphan sweep)",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path="<settings-home>/bin/<orphaned-forwarder-name>[.py|.cmd]",
                effect="delete",
                reason=(
                    "positive content-marker match "
                    "(_AGENT_FORWARDER_MARKER/_AGENT_CMD_FORWARDER_MARKER/"
                    "_LEGACY_CMD_MARKER) AND absence from this run's "
                    "agent-helper write set"
                ),
            ),
        ),
        # Clause 14 — `_prune_orphaned_static_bin_names`: deletes any
        # `<settings-home>/bin/` file named in the PREVIOUS
        # `.coordinator-bin-manifest.json` (clause 12) but absent from this
        # run's complete write-set — the general renamed/retired-name prune
        # (AC18), distinct provenance mechanism from clause 13's marker
        # sweep (a diff against this installer's own prior manifest, not a
        # content marker), so kept as its own delete clause.
        ShapedClause(
            effect="delete",
            discovered_by="_prune_orphaned_static_bin_names (previous-manifest diff)",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path="<settings-home>/bin/<orphaned-bin-name>",
                effect="delete",
                reason=(
                    "name recorded in the PREVIOUS "
                    f"{_BIN_MANIFEST_FILENAME} but absent from this run's "
                    "current write-set"
                ),
            ),
        ),
        # Clause 15 — `_careful_write`'s disposable pre-overwrite backup
        # tree: `<install_base>/.claude/<_OVERWRITE_BACKUP_SUBDIR>/<relative
        # -path>.pre-install-<TIMESTAMP>.bak`, written only when
        # `write_strategy == "careful"` (clause 5's foreign-tracked branch)
        # AND content differs. SHAPED: the timestamp and relative-path
        # segment are runtime-computed, never enumerable in source — see
        # `_careful_write_backup_path`.
        ShapedClause(
            discovered_by="_careful_write_backup_path",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path=(
                    f"<install_base>/.claude/{_OVERWRITE_BACKUP_SUBDIR}/"
                    "<relative-template-path>.pre-install-<TIMESTAMP>.bak"
                ),
                reason=(
                    "disposable pre-overwrite backup, written before every "
                    "clause-5 'careful' write; never itself subject to the "
                    "next clobber (sibling of setup/, not inside it)"
                ),
            ),
        ),
        # Clause 16 — `run` Step 2: the four tracked `<settings-home>/
        # machine-local/` template files (`_TRACKED_ML_FILES`) — seeded
        # once when absent, preserved (with a diff-reference notice, no
        # overwrite) when present and operator-customized. STATIC: the
        # four names are a fixed, enumerable source constant.
        StaticClause(
            entries=tuple(
                WriteSurfaceEntry(
                    kind="file-path",
                    path=f"<settings-home>/machine-local/{name}",
                    reason=(
                        "seed-if-absent from the shipped template; "
                        "preserve-on-diff (notice only, never overwritten) "
                        "once an operator-customized copy exists"
                    ),
                )
                for name in _TRACKED_ML_FILES
            ),
        ),
        # Clause 17 — `run` Step 2b: `unreal.toml` concern-baseline file,
        # seeded once from `unreal.toml.example` when absent, never
        # subsequently touched.
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path=f"<settings-home>/machine-local/{_ML_UNREAL_TOML_NAME}",
                    reason="seed-if-absent from unreal.toml.example (Step 2b); never overwritten thereafter",
                ),
            ),
        ),
        # Clause 18 — `run` Step 2c: the live `registry.toml`, seeded once
        # from `registry.toml.example` when absent. Distinct from clause 21
        # below, which mutates this SAME file's `concerns` array in place
        # on every run once it exists — a seed vs. a structured-key merge,
        # kept as separate clauses per their separate mechanisms.
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path=f"<settings-home>/machine-local/{_ML_REGISTRY_TOML_NAME}",
                    reason="seed-if-absent from registry.toml.example (Step 2c); never overwritten thereafter",
                ),
            ),
        ),
        # Clause 19 — `run` Step 3f: `hardware.toml` concern-baseline file,
        # seeded once from `hardware.toml.example` when absent; values are
        # subsequently written by `detect-hardware.sh`, not by this module.
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path=f"<settings-home>/machine-local/{_ML_HARDWARE_TOML_NAME}",
                    reason="seed-if-absent from hardware.toml.example (Step 3f); never overwritten thereafter",
                ),
            ),
        ),
        # Clause 20 — `run` Step 3c-ii: `settings-manifest.md`, installed
        # via `_install_one` with `force_overwrite=False` — same preserve-
        # on-diff policy as any non-code/non-forced template.
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path=f"<settings-home>/{_SETTINGS_MANIFEST_FILENAME}",
                    reason="_install_one, force_overwrite=False (preserve-on-diff, Step 3c-ii)",
                ),
            ),
        ),
        # Clause 21 — `_register_hardware_concern` (Step 3g): merges
        # `"hardware"` into `registry.toml`'s `concerns` array in place,
        # preserving every other entry already present — a
        # `structured-file-key` merge (tmp-write + `os.replace`), never a
        # whole-file overwrite, distinct from clause 18's one-time seed.
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="structured-file-key",
                    key="concerns[]=hardware",
                    path=f"<settings-home>/machine-local/{_ML_REGISTRY_TOML_NAME}",
                    reason=(
                        "_register_hardware_concern inserts \"hardware\" into "
                        "the concerns array (inline or multiline form), "
                        "preserving pre-existing entries; tmp-write + "
                        "os.replace, idempotent no-op if already present"
                    ),
                ),
            ),
        ),
        # Clause 22 — `_c10a_steps` Step C10a-1: the `coordinator-whoami/`
        # tree copy from either the legacy install-base location or the
        # plugin's `whoami/` source into
        # `<settings-home>/coordinator-whoami/`. SHAPED: the file set is
        # discovered per run via `_iter_whoami_files`, never enumerable in
        # source.
        ShapedClause(
            discovered_by="_iter_whoami_files (_c10a_copy_one)",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path=f"<settings-home>/{_WHOAMI_DIRNAME}/<relative-whoami-path>",
                reason="only when destination has no files yet (dst_has_files probe); preserve-on-diff per-file via _c10a_copy_one",
            ),
        ),
        # Clause 23 — `_c10a_steps` Step C10a-1: removal of the legacy
        # `<install_base>/.claude/coordinator-whoami` REAL directory once
        # the settings-home copy (clause 22) exists, ahead of clause 24
        # replacing it with a compat pointer. effect="delete", gated by
        # `_refuse_machine_mutation`.
        StaticClause(
            effect="delete",
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path=f"<install-base>/.claude/{_WHOAMI_DIRNAME}",
                    effect="delete",
                    reason="removed (rmdir/rmtree) only when it is a real dir, not already a compat pointer, and the settings-home copy exists; gated by _refuse_machine_mutation",
                ),
            ),
        ),
        # Clause 24 — `_c10a_steps` Step C10a-1: the compat pointer
        # replacing the removed legacy directory (clause 23) —
        # `mklink /J` junction on MSYS/MINGW/CYGWIN (via `cygpath`),
        # `Path.symlink_to` on POSIX. Same fixed destination path either
        # way; the creation mechanism is platform-branched, not the
        # surface itself.
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path=f"<install-base>/.claude/{_WHOAMI_DIRNAME}",
                    reason="compat pointer to <settings-home>/coordinator-whoami/: mklink /J junction (MSYS/MINGW/CYGWIN, via cygpath) or symlink_to (POSIX)",
                ),
            ),
        ),
        # Clause 25 — `_c10a_steps` Step C10a-2: `coordinator.whoami_src`
        # machine-local key, set to the settings-home whoami destination
        # path when the currently-registered value differs.
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="machine-local-key",
                    key="coordinator.whoami_src",
                    reason="set to <settings-home>/coordinator-whoami when the registered value differs (Step C10a-2, via the settings-home machine-local CLI)",
                ),
            ),
        ),
        # Clause 26 — `_c10a_steps` Step C10a-3: removal of the LEGACY
        # `<install_base>/.claude/.coordinator-venv` directory once the
        # CURRENT settings-home venv passes its health probe. Distinct
        # from `ensure_venv`'s own declared surface: `ensure_venv` owns the
        # current `<settings-home>/.coordinator-venv` tree (creation,
        # interpreter-pin key, build-lock sidecar) — this module owns only
        # the legacy delete leg, gated by `_refuse_machine_mutation`.
        StaticClause(
            effect="delete",
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path=f"<install-base>/.claude/{_LEGACY_VENV_DIRNAME}",
                    effect="delete",
                    reason="shutil.rmtree once the settings-home venv (ensure_venv's surface, not this module's) is healthy; gated by _refuse_machine_mutation",
                ),
            ),
        ),
        # Clause 27 — `_fnm_step`: an unbounded third-party installer
        # invocation (`brew install fnm`, or `curl https://fnm.vercel.app/
        # install | bash -s -- --skip-shell` when brew is absent), gated by
        # `_refuse_machine_mutation` (landed after this writer's own audit,
        # commit 1720a985 — not this clause's concern to change). No kind
        # in the frozen eight-kind vocabulary honestly expresses "ran a
        # third-party installer whose own filesystem footprint is not
        # enumerable from here" — `file-path` is the least-dishonest
        # choice (fnm ends up somewhere on disk), and the free-text
        # `reason` carries the actual truth rather than a flattering
        # kind. This is the stated-reason escape hatch, not an invented
        # ninth kind.
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path="<unknown — brew-managed or fnm.vercel.app installer-chosen fnm install location>",
                    reason=(
                        "installs the third-party `fnm` binary via `brew "
                        "install fnm` if brew is present, else `curl -fsSL "
                        "https://fnm.vercel.app/install | bash -s -- "
                        "--skip-shell`; that curl leg is opt-in only "
                        "(interactive consent or COORDINATOR_INSTALL_FNM=1) "
                        "and never runs on Windows, where the installer "
                        "rejects the platform outright; gated by "
                        "_refuse_machine_mutation (commit 1720a985); "
                        "skipped entirely if fnm is "
                        "already on PATH; failure is a WARNING, not fatal "
                        "(optional, core substrate unaffected); this "
                        "writer cannot enumerate or reverse what either "
                        "installer actually does to the machine — declared "
                        "via the stated-reason escape hatch rather than "
                        "left silently undeclared"
                    ),
                ),
            ),
        ),
        # Clause 28 — `_write_ps1_policy_status` (ps1-launcher-class plan,
        # C4): the `.ps1` execution-policy verdict's durable, findable-later
        # surface (AC13). A single STATIC file at
        # `<settings-home>/ps1-policy-gate-status.json` — `bin_dst.parent`,
        # deliberately BESIDE `<settings-home>/bin/` rather than inside it,
        # since this is a status record ABOUT that directory's `.ps1`
        # contents (clause 4), not another entry in it; clause 4's SHAPED
        # `<agent-bin-dir>/...` template does not reach a path one level up
        # and this clause exists precisely because it does not. Written on
        # EVERY non-check-only install pass that reached the policy gate —
        # GREEN as well as RED — not gated on the skip: AC13's durable
        # record has to outlive a GREEN install, since the operator it
        # protects is the one whose host was clean at install time and had
        # policy tightened afterward (AC10's post-install mutability
        # residual), and that operator gets no install-time stdout to
        # recall regardless of wording. Same shape as clause 20
        # (`settings-manifest.md`) — a single named file directly under
        # settings-home — kept as its own clause for that same reason: one
        # file, one call site, one STATIC entry.
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path=f"<settings-home>/{_PS1_POLICY_STATUS_FILENAME}",
                    reason=(
                        "written by _write_ps1_policy_status on every "
                        "non-check-only install pass that reached "
                        "evaluate_policy_gate(), regardless of verdict "
                        "(GREEN and RED both write) — AC13's durable, "
                        "findable-later surface for the .ps1 "
                        "execution-policy skip reason and repair "
                        "entrypoint; best-effort, an OSError warns rather "
                        "than raising"
                    ),
                ),
            ),
        ),
        # Clause 29 — the site-packages pointer, RETIRED (docs/plans/
        # 2026-08-18-retire-coordinator-venv.md chunk C2; originally
        # 2026-08-10-interpreter-surface-four-asks.md chunk C5):
        # `ensure_coordinator_venv` used to publish
        # `<settings-home>/bin/hook-sitepackages.txt` on every real success
        # exit for a sibling repo's hook bootstrap to read. That authorship
        # is retired outright, not repointed — every rung of DoE's
        # `_hook_venv_inject.py::_resolve_site_packages` ladder is
        # unreachable for a machine interpreter (see ensure_venv.py's
        # module docstring). Entry kept (not removed) as a STATIC clause,
        # unchanged in shape, so `_prune_orphaned_static_bin_names` still
        # recognizes and prunes a stale pointer left by a pre-migration box
        # — see the Step 3e union above and
        # `ensure_venv.SITEPACKAGES_POINTER_NAME`.
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path="<settings-home>/bin/hook-sitepackages.txt",
                    reason=(
                        "RETIRED 2026-08-18: no longer written by "
                        "ensure_coordinator_venv (chunk C2 deleted "
                        "_write_sitepackages_pointer outright, not "
                        "repointed — every DoE ladder rung is unreachable "
                        "for a machine interpreter). This entry stays "
                        "declared, unwritten, so a stale pointer left by a "
                        "pre-migration box remains a recognized prune "
                        "candidate for _prune_orphaned_static_bin_names "
                        "rather than an orphan outside that mechanism"
                    ),
                ),
            ),
        ),
    ),
)
"""This writer's declared surface — 29 clauses (C3c/C3c2's original six,
the follow-on dispatch's nine (7-15): the `<settings-home>/bin/`
static-manifest-driven families (clauses 7-9, SHAPED), the two
hand-maintained static families (clauses 10-11, STATIC), the installer's
own provenance ledger (clause 12), its two delete legs (clauses 13-14), and
the `_careful_write` disposable backup tree (clause 15) — plus a further
dispatch's eleven (16-26), closing
``state/debt-backlog/2026-08-06-write-surface-declarations-must-live-wit-e49b9cfd8ad1.yaml``:
the `<settings-home>/machine-local/` seeding family (clauses 16-19: tracked
templates, unreal.toml, registry.toml, hardware.toml), `settings-manifest.md`
(clause 20), the `concerns[]` structured-key merge (clause 21), the
`_c10a_steps` whoami/venv group (clauses 22-26: tree copy, legacy-dir
delete, compat-pointer creation, `coordinator.whoami_src` key, legacy-venv
delete — NOT `ensure_venv`'s own current-venv surface, which stays that
module's declaration), clause 27 (`_fnm_step`'s brew/curl third-party `fnm`
installer leg, declared via the stated-reason escape hatch rather than left
silently undeclared — no kind in the frozen eight-kind vocabulary honestly
expresses an unenumerable third-party installer footprint) — see
``state/audits/2026-08-06-install-substrate-write-surface-completeness.md``
refs C/D/E/F/G/I/W/X/Y/Z/AB for the census this closed. Clause 28
(`_write_ps1_policy_status`'s `<settings-home>/ps1-policy-gate-status.json`
status file, ps1-launcher-class plan C4, AC13), then clause 29
(`ensure_coordinator_venv`'s `<settings-home>/bin/hook-sitepackages.txt`
site-packages pointer, 2026-08-10-interpreter-surface-four-asks.md chunk
C5) is the current closing clause. `write_strategy` (`_write_strategy_for`,
force/careful/refuse) has
no field of its own on `WriteSurfaceEntry` — it is expressed via the
free-text `reason` on clauses 5, 6, and 15 rather than left unstated, since
those are the entries it actually attaches to."""


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="install-substrate")
    parser.add_argument("--setup-only", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        return run(setup_only=args.setup_only, check_only=args.check_only)
    except SubstrateFatalError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
