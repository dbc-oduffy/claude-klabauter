"""
coordinator_core.install.ensure_venv — Port of:
``coordinator/bin/ensure-coordinator-venv.sh`` (DoE e19314de, 2026-07-17)
[DoE-claude repo].

Purpose: idempotently ensure a coordinator-owned Python venv exists with
``coordinator_whoami``, ``pydantic``, and ``psutil`` importable (the
acceptance oracle, AC B2 — see ``_VENV_IMPORT_PROBES``/``_VENV_PIP_DEPS``),
pin it via the machine-local registry (``coordinator.python``), and report a
status word
(``ready`` / ``rebuilt`` / ``would-rebuild`` / ``would-write``) that three
call sites — ``substrate.py`` ``_c10a_steps``, ``scripts/setup.py``'s
``_fallback_to_venv``, ``maximalist.py`` Step 6 — each interpret under their
own disposition (fallback-retain-vs-fatal, fatal, advisory respectively).
This module owns ONLY the venv-ensure mechanics; the surrounding
disposition stays at each call site (do not collapse the three sites' error
handling here).

Break-glass only (docs/plans/2026-08-18-retire-coordinator-venv.md chunk
C4, AC5): every one of the three call sites above now reaches
``ensure_coordinator_venv`` ONLY behind an explicit `--allow-venv-fallback`
opt-in threaded down from its own CLI. ``first_run.py``'s former Step 4b
call site (Port B) is retired outright, not flag-gated -- that module's CLI
carries no such flag, and machine-interpreter ``coordinator_whoami``
provisioning no longer depends on this module having ever built a venv (see
``scripts/setup.py``'s ``provision_whoami_under_general_pin``, chunk C10).

Port backlink: docs/plans/2026-07-17-retire-doe-bash-bridges-native-python.md
    (chunk C2 — Port B).

Public surface (pinned contract — do not change without updating consumers):

    class EnsureVenvError(RuntimeError): ...
    class EnsureVenvContention(EnsureVenvError): ...
    def venv_python_path(venv_dir: Path) -> Path: ...
    def ensure_coordinator_venv(
        plugin_root: Path, settings_home_path: Path, *,
        claude_home: Optional[str] = None, check_only: bool = False,
        site: str = "ensure-coordinator-venv",
    ) -> str: ...  # "ready" | "rebuilt" | "would-rebuild" | "would-write"

Explicit-params discipline (AC G5): every entry point here takes its
configuration (plugin root, settings-home root, CLAUDE_HOME value,
check/dry-run flag) as an explicit parameter derived from the CALLER's
locals — this module never re-reads ``os.environ`` for those values itself.
Callers resolve their own env once and pass the result in.

Build-lock mechanism — REDESIGN, not transliteration (prior-art #5: flock
per 2026-07-06). The bash original used a ``mkdir``-as-mutex with 300s
stale-lock reclaim. This port reuses ``coordinator_core.locked_write``'s
existing dual-backend advisory-lock primitive (``_plat_try_lock`` /
``_plat_unlock`` — ``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows)
directly on a ``.lock`` sidecar FILE next to the venv dir. Contention is
NB-immediate-fail (try once, fail loud on contention, no polling, no
lockless-proceed) — mirrors the bash mutex's *contention contract*
(fail-loud, non-zero, no lockless-proceed), NOT its stale-reclaim mechanism,
which the flock-family primitive makes moot by construction (a crashed
holder's OS-level lock releases automatically).

Native test seam (replaces the bash ``COORDINATOR_TEST_VENV_SCRIPT`` shim):
the module-level helpers below (``_venv_healthy``, ``_resolve_base_python``,
``_create_venv``, ``_install_deps``, ``_resolve_ml_cli``, ``_ml_get``,
``_ml_set``, ``_resolve_whoami_pkg``, ``_set_pin``) are each independently
monkeypatchable — a test substitutes a fast fake for any one of them (e.g.
``_create_venv``/``_install_deps``) instead of needing a real network pip
install or a fake shell script on disk.

Negative-spec:
  - Does NOT publish ``<settings-home>/bin/hook-sitepackages.txt`` (retired
    2026-08-18, docs/plans/2026-08-18-retire-coordinator-venv.md chunk C2).
    That pointer was rung 2 of DoE's ``_hook_venv_inject.py::
    _hook_venv_inject::_resolve_site_packages`` three-rung ladder (env var →
    pointer file → settings-home-layout join). The target of "repoint the
    pointer at the machine interpreter" is unreachable through EVERY rung,
    not just this one: ``_coord_hook_inject`` applies ``_find_venv_root`` +
    ``_version_ok`` AFTER ladder resolution and declines whenever no
    ``pyvenv.cfg`` is found within 6 parents of the resolved path — a
    machine interpreter has none, by construction. Rung 2 (this pointer)
    dies at DoE's realpath-containment check before ``_find_venv_root`` even
    runs; rung 1 (``COORDINATOR_HOOK_SITE_PACKAGES``) clears containment and
    then dies at ``_find_venv_root`` itself; rung 3 (layout) keeps resolving
    to the real venv tree for as long as that tree exists on disk (its
    removal is a separate, gated chunk — C8). None of the three is an
    available fallback, so this module stops publishing the pointer outright
    rather than repointing it: this is the deliberate, documented end-state,
    not a silent regression. Once the venv tree itself is gone,
    ``_coord_hook_inject`` writes its ``COORDINATOR HOOK SEAM: ... injection
    skipped -- missing`` stderr banner on every hook fire, leaves
    ``sys.path`` untouched, and does not repoint ``sys.executable`` --
    "no rung resolves" is the intended terminal state, chosen, not missed.
    ``SITEPACKAGES_POINTER_NAME`` (the constant) is kept, unlike the
    function that wrote through it, purely so ``substrate.py``'s orphan-prune
    union can still recognize and clean up a stale pointer left by a
    pre-migration box.
  - Does NOT re-derive settings-home resolution — callers pass
    ``settings_home_path`` (from ``coordinator_core._settings_home.settings_home()``).
  - Does NOT use ``locked_rmw`` — that helper is a file-*content*
    read-modify-write wrapper; a venv build lock has no content to RMW.
  - Does NOT poll-with-backoff on lock contention (``_acquire_flock``'s
    shape) — a 30-60s peer build can outlast a short poll window, and
    polling changes the externally-observable latency contract.
  - Does NOT pip-install an editable ``coordinator_core`` into this venv
    (deliberate, investigated 2026-08-14). A live venv was observed carrying
    ``__editable__.coordinator_core-0.1.0.pth`` pointing at this repo's
    working tree, but no install-chain site (this module, ``scripts/setup.py``,
    ``maximalist.py``) ever runs that install — it is hand-installed machine
    state, not a reproducible contract. The hook path already has its own
    resolution rung for this exact need: ``_engine_root.py``'s
    ``resolve_claude_klabauter_root_with_class()`` ladder plus the ``sys.path.insert(0,
    root)`` each hook script does in its own ``main()`` (coordinator-claude
    repo), which re-resolves every hook fire rather than pinning an editable
    install. (Corrected 2026-08-15: this block previously named
    ``resolve_claude_klabauter_root()`` "at ~line 840" as that rung. No such
    function exists in DoE-claude — verified by repo-wide grep; the only real
    ``_resolve_claude_klabauter_root`` is in the published MIRROR's
    ``cc_invoke.py`` and resolves the mirror, not the live tree. The
    negative-spec's conclusion is unaffected; only its cited mechanism was
    wrong. Sibling copy of the same error fixed in
    ``docs/reference/shared-fleet-venv-contract.md`` § 1.) This
    repo's engine is meant to execute from that live tree (project CLAUDE.md
    § "What this repo is"), so an editable ``.pth`` baked into the venv would
    itself be a latent bug the moment this repo's checkout path moves —
    pinning it here would trade a working, self-correcting resolution rung
    for a second, staler one. Left undeclared on purpose; do not add it to
    ``_install_deps`` without re-litigating this call.

Rebuild-vs-live-reader safety (``_build_dir_for``/``_swap_in_new_venv``/
``_sweep_orphaned_swap_dirs`` — the latter relocated to
``coordinator_core.install.uninstall_legs`` and imported back here, see
docs/plans/2026-08-18-retire-coordinator-venv.md chunk C3, so the uninstall
leg that also calls it survives this module's own eventual retirement):
the build-lock above only serialises BUILDERS against each other — every
other session on the box executes Python out of ``<venv_dir>/bin/python``
with no lock at all (cannot be asked to take one). A rebuild therefore
never mutates ``venv_dir`` in place, and — since 2026-08-20 — never renames
it either. ``venv_dir`` is a JUNCTION (nt) / directory symlink (posix) —
see ``coordinator_core.install.junction`` — pointing at a sibling
GENERATION directory, ``<venv_dir name>.gen-<pid>-<hex>``, that is NEVER
renamed while published. A rebuild populates a fresh generation sibling
(``_build_dir_for``), health-probes it, then publishes by retargeting the
junction (``_swap_in_new_venv``): ``junction.remove_junction(venv_dir)``
followed by ``junction.create_junction(venv_dir, new_generation)``. The
vacated old generation is then reclaimed with a plain ``shutil.rmtree``
(immediately on POSIX; best-effort with deferred reclaim via
``_sweep_orphaned_swap_dirs`` on Windows, where a reader's still-open
handle can make the delete fail). A build FAILURE never even reaches the
swap step — ``venv_dir`` is untouched until the replacement is known-good.

NEGATIVE SPEC — this module's own prior shape, and why it changed. Before
`d99fd6dc88d0`/this junction rewrite, publication was a two-``os.rename``
swap (live tree renamed aside to ``.stale-<pid>-<hex>``, then the build
tree renamed into ``venv_dir``). That shape's docstring claimed a directory
rename is metadata-only on Windows "and, unlike deleting a file, does not
require every open HANDLE inside the directory to be closed first". THAT
CLAIM WAS FALSE, measured on a real Windows host (this module's own two
`pending_fix` tests, `test_swap_in_new_venv_does_not_delete_tree_a_reader_
still_has_open` / `test_mutate_mode_rebuild_swap_survives_a_reader_holding_
the_old_tree_open`, both filed 2026-08-14/15): `os.rename` of a directory
raises `PermissionError [WinError 5]` whenever ANY plain-open file handle
exists anywhere inside it, even several path segments down, and a Python
`open()` handle on this platform is NOT opened with `FILE_SHARE_DELETE` (a
second, independent measurement — probed directly against a Windows host
2026-08-20; the file itself cannot be renamed OR unlinked while such a
handle is held, `WinError 32`, so the earlier claim's premise does not
hold at the file level either). Only the junction-retarget mechanism
above genuinely avoids touching the reader's tree at all — see
``_swap_in_new_venv``'s own docstring for the residual it still cannot
close.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional

from coordinator_core.locked_write import _plat_try_lock, _plat_unlock
from coordinator_core.trusted_root_guard import coordinator_trusted_root_guard
from coordinator_core.win_portability import is_executable, no_console_creationflags
from coordinator_core.install import junction
from coordinator_core.install.timeouts import PACKAGE_INSTALL_SECS, VENV_CREATE_SECS
from coordinator_core.install.uninstall_legs import _sweep_orphaned_swap_dirs
from coordinator_core.install.write_surface import (
    ShapedClause,
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)

SITEPACKAGES_POINTER_NAME = "hook-sitepackages.txt"


_NETWORK_ERROR_RE = re.compile(
    r"Could not find a version|ConnectionError|TimeoutError|"
    r"Temporary failure in name resolution|Network is unreachable|"
    r"Failed to establish a new connection",
    re.IGNORECASE,
)

#: ``PyYAML`` is declared here EXPLICITLY even though ``coordinator_whoami``'s
#: deps ever changing. ``jsonschema``/``rfc3339-validator`` stay UNDECLARED
_VENV_PIP_DEPS = ("pydantic>=2", "psutil>=5.9", "PyYAML>=6.0")
_VENV_IMPORT_PROBES = ("coordinator_whoami", "pydantic", "psutil", "yaml")

#: rather than restating the literal, and `WRITE_SURFACE` below declares
_PIN_KEY = "coordinator.python"

#: constant rather than restating the literal, and `WRITE_SURFACE` below
_WHOAMI_PIN_KEY = "coordinator.whoami_python"

_VENV_TREE_CLAUSE_INDEX = 0
"""Index of `WRITE_SURFACE`'s sole SHAPED clause (the venv tree itself) —
the only clause `ensure_coordinator_venv` journals against; the pin-key and
build-lock clauses are `StaticClause`s and need no resolution."""


def _record_resolution(clause_index: int, entries) -> None:
    from coordinator_core.install import resolution_journal

    resolution_journal.record_resolution("ensure-venv", clause_index, entries)

WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="ensure-venv",
    source_module="coordinator_core.install.ensure_venv",
    clauses=(
        # on what gets installed (`_VENV_PIP_DEPS` plus the editable
        ShapedClause(
            discovered_by="ensure_coordinator_venv (settings_home_path / '.coordinator-venv')",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path="<settings-home>/.coordinator-venv/",
            ),
        ),
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="machine-local-key",
                    key=_PIN_KEY,
                    reason="interpreter pin, written by _set_pin",
                ),
            ),
        ),
        StaticClause(
            effect="delete",
            entries=(
                WriteSurfaceEntry(
                    kind="machine-local-key",
                    key=_PIN_KEY,
                    effect="delete",
                    reason="dangling pin cleared on failed rebuild, by _clear_dangling_pin",
                ),
            ),
        ),
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="machine-local-key",
                    key=_WHOAMI_PIN_KEY,
                    reason="venv-owned interpreter pin, written unconditionally by _set_pin",
                ),
            ),
        ),
        StaticClause(
            effect="delete",
            entries=(
                WriteSurfaceEntry(
                    kind="machine-local-key",
                    key=_WHOAMI_PIN_KEY,
                    effect="delete",
                    reason="dangling whoami pin cleared on failed rebuild, by _clear_dangling_pin",
                ),
            ),
        ),
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path="<settings-home>/.coordinator-venv.lock",
                    reason=(
                        "build-lock sidecar, O_CREAT'd by ensure_coordinator_venv and never "
                        "unlinked; the flock releases on close but the file remains"
                    ),
                ),
            ),
        ),
    ),
)
"""This writer touches the machine in three distinct ways: a venv TREE
(`_create_venv`/`_install_deps`), two machine-local PIN KEYS
(`_set_pin` writes them, `_clear_dangling_pin` deletes them under
`clear_pin_on_failure=True`), and a build-lock sidecar FILE
(`<venv_dir>.lock`, O_CREAT'd and never unlinked).

It NO LONGER writes into the site-packages of the interpreter pinned at
`coordinator.python`. That was the one surface outside anything this
installer owned, and it existed solely to keep `coordinator_whoami`
editable-installed there for `probe_p5`. `coordinator_whoami` is retired,
so the write has no subject; the shaped clause declaring it is removed with
it rather than left describing a write that can no longer happen. Both
write and delete on the pin keys are declared as separate clauses -- `validate()` and the C4
emission op both key on `effect`, and collapsing them would hide that this
writer can also remove state, not merely add it. `clear_pin_on_failure`
reaches no surface beyond the pin keys: it gates only the
`_clear_dangling_pin` call, which reads and writes nothing else.
"""


class EnsureVenvError(RuntimeError):
    pass


class EnsureVenvContention(EnsureVenvError):
    pass


def _is_windows_shell() -> bool:
    return (
        os.environ.get("OSTYPE") in ("msys", "cygwin")
        or os.environ.get("OS") == "Windows_NT"
    )


def _run(argv, **kwargs) -> subprocess.CompletedProcess:
    """Console-suppressed `subprocess.run`, capturing by default.

    The capture default is load-bearing, not tidiness: `no_console_creationflags()`
    allocates a window-less console, and with NO std-stream kwarg CPython omits
    `STARTF_USESTDHANDLES` (`Popen._get_handles` returns all -1 when stdin, stdout and
    stderr are every one of them None), so the child binds its handles to that console
    and everything it prints is lost. Both callers here read only `returncode`, so
    nothing reached an operator before this default either — capturing makes the child's
    own diagnostic available to the failure paths instead of discarding it.

    A caller that genuinely wants passthrough passes its own `stdout=`/`stderr=` and
    overrides this; it must pass SOMETHING, and that is the point.
    """
    kwargs.setdefault("timeout", 60)
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(argv, **no_console_creationflags(), **kwargs)


def _quiet_output(argv) -> str:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=15, **no_console_creationflags()
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"ensure-coordinator-venv: {argv[0] if argv else '<empty argv>'} failed: {exc}", file=sys.stderr)
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def venv_python_path(venv_dir: Path) -> Path:
    if _is_windows_shell():
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _venv_healthy(venv_py: Path) -> bool:
    """Healthy iff VENV_PY is executable AND every module named in
    ``_VENV_IMPORT_PROBES`` imports successfully under it (the acceptance
    oracle, AC B2 — widened to include psutil so a venv missing it is
    rebuilt rather than passing the fast path).

    Deliberate isolation boundary, not a candidate for an in-process
    import — ``venv_py`` is by construction a *different* interpreter than
    the one running this module, so it is not importable in-process; the
    probe must execute under the target venv's own interpreter to answer
    the question it exists to answer. See
    ``state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md``
    for the recorded verdict."""
    if not is_executable(venv_py):
        return False
    probe = "; ".join(f"import {mod}" for mod in _VENV_IMPORT_PROBES)
    try:
        proc = subprocess.run(
            [str(venv_py), "-c", probe],
            capture_output=True,
            timeout=30,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _resolve_ml_cli(plugin_root: Path) -> Optional[list]:
    """Delegate to the canonical resolver in ``_shared``.

    This used to roll its own bin-relative-then-PATH lookup returning a Path,
    which handed subprocess an EXTENSION-LESS shebang script — unexecutable on
    Windows (``OSError: [WinError 193] %1 is not a valid Win32 application``),
    breaking the documented install. ``resolve_machine_local_cli`` already
    solves this by preferring ``[sys.executable, _machine_local.py]``, i.e. no
    shebang exec at all. Returns an argv LIST now, not a Path.
    """
    from coordinator_core.install._shared import resolve_machine_local_cli

    return resolve_machine_local_cli(str(plugin_root))


def _ml_get(ml_cli: list, key: str) -> str:
    return _quiet_output([*ml_cli, "get", key])


def _ml_set(ml_cli: list, key: str, value: str) -> None:
    _run([*ml_cli, "set", key, value])


def _resolve_whoami_pkg(
    plugin_root: Path, ml_cli: Optional[list], *, warn_on_stale_seam: bool = True
) -> Path:
    """WHOAMI_PKG seam: registry ``coordinator.whoami_src`` -> dir, else
    ``plugin_root/whoami`` (AC B8, incl. stale-key warning).

    ``warn_on_stale_seam=False`` suppresses only the stale-seam warning, never
    the fallback itself — for the general-pin leg's resolution
    (``_ensure_whoami_under_general_pin``) on a run where
    ``_ensure_coordinator_venv_impl``'s rebuild leg has ALREADY resolved this
    seam loud this run (status ``"rebuilt"``). On every other status
    (``"ready"``, ``"would-rebuild"``, ``"would-write"``) the impl never
    called this function at all, so the general-pin leg is the SOLE
    resolution for the run and must warn — suppressing unconditionally would
    silence the operator's only stale-seam diagnostic on the common
    already-healthy path (Review: coordinator:code-reviewer,
    whoami-general-pin-review, finding 2). One stale seam is one warning; the
    same seam warned about twice reads as two distinct problems.
    """
    seam = _ml_get(ml_cli, "coordinator.whoami_src") if ml_cli is not None else ""
    if seam:
        seam_path = Path(seam)
        if seam_path.is_dir():
            return seam_path
        if warn_on_stale_seam:
            print(
                f"[ensure-coordinator-venv] WARNING: coordinator.whoami_src='{seam}' is not "
                f"a directory; falling back to {plugin_root / 'whoami'}",
                file=sys.stderr,
            )
    return plugin_root / "whoami"


def _validate_general_pin(path: str) -> bool:
    if not path:
        return False
    try:
        proc = subprocess.run(
            [path, "-c", "import sys"],
            capture_output=True,
            timeout=15,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _should_write_general_pin(current: str, venv_py: Path) -> bool:
    venv_py_str = str(venv_py)
    if not current:
        return True
    if current == venv_py_str:
        return True
    if "/.claude/.claude/" in current:
        return True
    return not _validate_general_pin(current)


def _set_pin(ml_cli: Optional[list], venv_py: Path) -> None:
    """Writes ``_WHOAMI_PIN_KEY`` unconditionally (AC3 — this venv's own
    pointer, genuinely owned here), then consults
    ``_should_write_general_pin`` for ``_PIN_KEY`` (AC1/AC2): a healthy
    unrelated pin survives untouched, with an AC6 stderr advisory naming
    what held and the remedy. Graceful degradation when the CLI is absent
    (AC B5); self-heals a doubled '.claude/.claude' pin, loudly."""
    if ml_cli is None:
        print(
            "[ensure-coordinator-venv] WARNING: machine-local CLI not found; "
            "coordinator venv built but pin not persisted.",
            file=sys.stderr,
        )
        print(
            f"[ensure-coordinator-venv]   Set COORDINATOR_PYTHON={venv_py} or "
            "re-run after installing machine-local.",
            file=sys.stderr,
        )
        return
    venv_py_str = str(venv_py)
    _ml_set(ml_cli, _WHOAMI_PIN_KEY, venv_py_str)

    current = _ml_get(ml_cli, _PIN_KEY)
    if current == venv_py_str:
        return
    if not _should_write_general_pin(current, venv_py):
        print(
            f"[ensure-coordinator-venv] coordinator.python retained '{current}' "
            "— editable installs are per-interpreter; install coordinator_whoami "
            "under THIS interpreter, or set COORDINATOR_PYTHON to the one that "
            "has it.",
            file=sys.stderr,
        )
        return
    if "/.claude/.claude/" in current:
        print(
            f"[ensure-coordinator-venv] self-healing doubled venv pin: "
            f"'{current}' → '{venv_py_str}'",
            file=sys.stderr,
        )
    _ml_set(ml_cli, _PIN_KEY, venv_py_str)


def _clear_dangling_pin(ml_cli: Optional[list], venv_py: Path) -> None:
    if ml_cli is None:
        return
    venv_py_str = str(venv_py)
    if _ml_get(ml_cli, _PIN_KEY) == venv_py_str:
        print(
            f"[ensure-coordinator-venv] WARNING: coordinator venv rebuild failed; "
            f"clearing dangling coordinator.python pin (was '{venv_py_str}').",
            file=sys.stderr,
        )
        print(
            "[ensure-coordinator-venv]   Re-run ensure-coordinator-venv once the "
            "underlying failure is resolved to rebuild and re-pin.",
            file=sys.stderr,
        )
        _ml_set(ml_cli, _PIN_KEY, "")
    if _ml_get(ml_cli, _WHOAMI_PIN_KEY) == venv_py_str:
        print(
            f"[ensure-coordinator-venv] WARNING: coordinator venv rebuild failed; "
            f"clearing dangling coordinator.whoami_python pin (was '{venv_py_str}').",
            file=sys.stderr,
        )
        _ml_set(ml_cli, _WHOAMI_PIN_KEY, "")


def _resolve_base_python() -> Optional[str]:
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _build_dir_for(venv_dir: Path) -> Path:
    """A fresh GENERATION sibling a rebuild populates BEFORE ever touching
    the published name — under the junction-publish layout this directory
    is not a transient build scratch space: once it passes the health probe
    it becomes (and stays, until superseded) the tree ``venv_dir`` points
    at, so it is named ``.gen-<pid>-<hex>`` from the start rather than
    renamed at publish time. ``_sweep_orphaned_swap_dirs`` matches this same
    prefix to reclaim any generation a crashed process never published."""
    return venv_dir.parent / f"{venv_dir.name}.gen-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _swap_in_new_venv(venv_dir: Path, build_dir: Path) -> None:
    """Publish ``build_dir`` as ``venv_dir`` by retargeting the ``venv_dir``
    junction (nt) / symlink (posix) at it — never a rename or in-place
    mutation of any real directory. Mirrors
    ``coordinator_core.install.fleet_env._swap_in_new_env`` (the same
    primitive, re-derived here rather than imported — this module and
    ``fleet_env`` intentionally do not share a runtime dependency; see each
    module's own docstring).

    A reader that already resolved ``venv_dir`` before this call keeps
    reading the OLD generation through its already-open handles (unaffected
    by the link retarget); a reader that resolves ``venv_dir`` after this
    call gets the NEW generation. Only the two-syscall window strictly
    between ``remove_junction`` and ``create_junction`` is unreadable at all
    (``FileNotFoundError``) — the same accepted, documented trade
    ``fleet_env``'s module docstring records (measured there at ~1.2ms;
    not independently re-measured here, since it is the identical
    primitive).

    The restore guard: if ``create_junction`` raises AFTER
    ``remove_junction`` already succeeded, ``venv_dir`` would be left absent
    and every subsequent hook fire hard-broken until the next rebuild
    notices. This re-points ``venv_dir`` at the PREVIOUS generation and
    re-raises — it never swallows the failure. First-ever publish (no prior
    junction, no prior generation to fall back to) is the one case with
    nothing to restore.

    The vacated old generation is then reclaimed with a plain
    ``shutil.rmtree`` (never ``rmtree(venv_dir)`` — the link itself is
    retargeted in place, not removed and recreated as a directory).
    Reclaim failure is guarded exactly as it always was: a reader's
    still-open handle can make the delete raise a sharing violation on
    Windows, so failure there is best-effort and left for
    ``_sweep_orphaned_swap_dirs`` on the next rebuild.

    NOT SUPPORTED — a pre-existing REAL directory at ``venv_dir`` (the
    pre-2026-08-20 layout, or any state where ``venv_dir`` exists but is
    neither absent nor a junction). Converting that in place would require
    vacating the name via the exact ``os.rename``/``os.rmdir`` this rewrite
    exists to stop calling — measured directly against this repo's own
    two `pending_fix` tests (see module docstring's NEGATIVE SPEC): a
    Python `open()` handle on a file inside the tree is not opened with
    `FILE_SHARE_DELETE`, so neither the file nor any ancestor directory can
    be renamed or removed while that handle is held, by ANY mechanism this
    module has available (`_winapi.CreateJunction` itself also refuses --
    `WinError 183` -- when the link path already names a non-empty
    directory). This function refuses rather than attempting an unsafe
    partial mutation; a caller holding a real pre-junction tree must clear
    it out of band first. Safe to remove by hand: nothing on a current
    install reads this path directly — ``coordinator_whoami`` resolves from
    the settings-home source tree instead, and this venv is break-glass
    only.
    """
    had_prior_publish = junction.is_junction(venv_dir)
    if had_prior_publish:
        previous_target = junction.junction_target(venv_dir)
        junction.remove_junction(venv_dir)
    elif venv_dir.exists():
        raise EnsureVenvError(
            f"[ensure-coordinator-venv] {venv_dir} exists but is neither absent nor a "
            "junction; refusing to convert it in place.\n"
            f"[ensure-coordinator-venv]   Remove {venv_dir} and re-run."
        )
    else:
        previous_target = None

    try:
        junction.create_junction(venv_dir, build_dir)
    except Exception:
        if had_prior_publish and previous_target is not None:
            junction.create_junction(venv_dir, previous_target)
        raise

    if had_prior_publish and previous_target is not None and previous_target.exists():
        try:
            shutil.rmtree(previous_target)
        except OSError as exc:
            print(
                f"[ensure-coordinator-venv] WARNING: could not immediately reclaim the prior "
                f"venv generation ({type(exc).__name__}: {exc}) — a reader likely still has it "
                f"open (expected on Windows). Left at {previous_target} for reclamation on the "
                "next rebuild.",
                file=sys.stderr,
            )


def _create_venv(base_py: str, venv_dir: Path) -> None:
    proc = _run([base_py, "-m", "venv", str(venv_dir)], timeout=VENV_CREATE_SECS)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        suffix = f" -- {detail[-1]}" if detail else ""
        raise EnsureVenvError(
            f"[ensure-coordinator-venv] ERROR: venv creation failed "
            f"(exit {proc.returncode}){suffix}"
        )


def _install_deps(venv_py: Path, whoami_pkg: Path) -> None:
    try:
        proc = subprocess.run(
            [str(venv_py), "-m", "pip", "install", "-e", f"{whoami_pkg}/", *_VENV_PIP_DEPS],
            capture_output=True,
            text=True,
            timeout=PACKAGE_INSTALL_SECS,
            **no_console_creationflags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise EnsureVenvError(
            f"[ensure-coordinator-venv] ERROR: pip install timed out: {exc}"
        ) from exc
    if proc.returncode != 0:
        stderr = proc.stderr or ""
        if _NETWORK_ERROR_RE.search(stderr):
            print(
                "[ensure-coordinator-venv] coordinator venv rebuild failed — pip could not "
                "reach PyPI (check network/proxy).",
                file=sys.stderr,
            )
            print(
                "[ensure-coordinator-venv]   The venv needs PyPI access for "
                f"{', '.join(_VENV_PIP_DEPS)} and coordinator_whoami's own dependencies.",
                file=sys.stderr,
            )
            print(
                "[ensure-coordinator-venv]   Re-run ensure-coordinator-venv.sh once online.",
                file=sys.stderr,
            )
        else:
            print(
                f"[ensure-coordinator-venv] ERROR: pip install failed (exit {proc.returncode}).",
                file=sys.stderr,
            )
            tail = "\n".join(stderr.splitlines()[-20:])
            if tail:
                print(tail, file=sys.stderr)
        raise EnsureVenvError(
            f"[ensure-coordinator-venv] failed (pip exit {proc.returncode})"
        )


def ensure_coordinator_venv(
    plugin_root: Path,
    settings_home_path: Path,
    *,
    claude_home: Optional[str] = None,
    check_only: bool = False,
    site: str = "ensure-coordinator-venv",
    clear_pin_on_failure: bool = False,
) -> str:
    result = _ensure_coordinator_venv_impl(
        plugin_root,
        settings_home_path,
        claude_home=claude_home,
        check_only=check_only,
        site=site,
        clear_pin_on_failure=clear_pin_on_failure,
    )
    return result


def _ensure_coordinator_venv_impl(
    plugin_root: Path,
    settings_home_path: Path,
    *,
    claude_home: Optional[str] = None,
    check_only: bool = False,
    site: str = "ensure-coordinator-venv",
    clear_pin_on_failure: bool = False,
) -> str:
    plugin_root = Path(plugin_root)
    settings_home_path = Path(settings_home_path)

    coordinator_trusted_root_guard(mode="fail-loud", root=str(plugin_root), site=site)

    # CLAUDE_HOME /.claude-suffix guard (fail loud) — doubled-path precondition.
    # Separator-agnostic: a Windows CLAUDE_HOME arrives backslash-separated
    claude_home_norm = claude_home.replace("\\", "/") if claude_home else claude_home
    if claude_home_norm and claude_home_norm.rstrip("/").endswith("/.claude"):
        raise EnsureVenvError(
            f"[ensure-coordinator-venv] FATAL: CLAUDE_HOME='{claude_home}' ends in '/.claude'.\n"
            "  CLAUDE_HOME is a $HOME substitute, NOT the .claude directory itself — the settings\n"
            "  home resolves to $CLAUDE_HOME/.coordinator-claude-settings, so a .claude-suffixed\n"
            "  value places the venv INSIDE ~/.claude (unexpected nesting). Remediation: set\n"
            "  CLAUDE_HOME to the PARENT of .claude (e.g. CLAUDE_HOME=$HOME) or unset it."
        )

    venv_dir = settings_home_path / ".coordinator-venv"
    venv_py = venv_python_path(venv_dir)
    ml_cli = _resolve_ml_cli(plugin_root)

    if check_only:
        if _venv_healthy(venv_py):
            if ml_cli is not None:
                venv_py_str = str(venv_py)
                current_whoami = _ml_get(ml_cli, _WHOAMI_PIN_KEY)
                if current_whoami != venv_py_str:
                    return "would-write"
                current_general = _ml_get(ml_cli, _PIN_KEY)
                if (
                    current_general != venv_py_str
                    and _should_write_general_pin(current_general, venv_py)
                ):
                    return "would-write"
            return "ready"
        return "would-rebuild"

    if _venv_healthy(venv_py):
        _set_pin(ml_cli, venv_py)
        _record_resolution(
            _VENV_TREE_CLAUSE_INDEX, (WriteSurfaceEntry(kind="file-path", path=str(venv_dir)),)
        )
        return "ready"

    settings_home_path.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(venv_dir) + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    acquired = False
    try:
        acquired = _plat_try_lock(fd)
        if not acquired:
            raise EnsureVenvContention(
                "[ensure-coordinator-venv] another session is rebuilding the coordinator "
                "venv; retry in a moment"
            )

        if _venv_healthy(venv_py):
            _set_pin(ml_cli, venv_py)
            _record_resolution(
                _VENV_TREE_CLAUSE_INDEX, (WriteSurfaceEntry(kind="file-path", path=str(venv_dir)),)
            )
            return "ready"

        _sweep_orphaned_swap_dirs(venv_dir)
        build_dir = _build_dir_for(venv_dir)
        build_venv_py = venv_python_path(build_dir)

        try:
            base_py = _resolve_base_python()
            if not base_py:
                raise EnsureVenvError(
                    "[ensure-coordinator-venv] ERROR: no python3 or python found in PATH.\n"
                    "[ensure-coordinator-venv]   Install Python 3.10+ and ensure it is on PATH."
                )
            _create_venv(base_py, build_dir)

            whoami_pkg = _resolve_whoami_pkg(plugin_root, ml_cli)
            _install_deps(build_venv_py, whoami_pkg)

            if not _venv_healthy(build_venv_py):
                raise EnsureVenvError(
                    "[ensure-coordinator-venv] ERROR: freshly-built venv failed the "
                    "health probe (import check) before swap-in; discarding it."
                )
        except EnsureVenvError:
            shutil.rmtree(build_dir, ignore_errors=True)
            if clear_pin_on_failure and not venv_dir.is_dir():
                _clear_dangling_pin(ml_cli, venv_py)
            if venv_dir.is_dir():
                _record_resolution(
                    _VENV_TREE_CLAUSE_INDEX,
                    (WriteSurfaceEntry(kind="file-path", path=str(venv_dir)),),
                )
            else:
                _record_resolution(_VENV_TREE_CLAUSE_INDEX, ())
            raise

        _swap_in_new_venv(venv_dir, build_dir)
        _set_pin(ml_cli, venv_py)
        _record_resolution(
            _VENV_TREE_CLAUSE_INDEX, (WriteSurfaceEntry(kind="file-path", path=str(venv_dir)),)
        )
        return "rebuilt"
    finally:
        if acquired:
            try:
                _plat_unlock(fd)
            except OSError:
                pass
        os.close(fd)
