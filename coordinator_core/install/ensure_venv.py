"""
coordinator_core.install.ensure_venv — Port of:
``coordinator/bin/ensure-coordinator-venv.sh`` (example-doctrine-repo e19314de, 2026-07-17)
[example-doctrine-repo repo].

Purpose: idempotently ensure a coordinator-owned Python venv exists with
``coordinator_whoami``, ``pydantic``, and ``psutil`` importable (the
acceptance oracle, AC B2 — see ``_VENV_IMPORT_PROBES``/``_VENV_PIP_DEPS``),
pin it via the machine-local registry (``coordinator.python``), and report a
status word
(``ready`` / ``rebuilt`` / ``would-rebuild`` / ``would-write``) that three
call sites — ``substrate.py`` ``_c10a_steps``, ``first_run.py`` Step 4b,
``maximalist.py`` Step 6 — each interpret under their own disposition
(fallback-retain-vs-fatal, fatal, advisory respectively). This module owns
ONLY the venv-ensure mechanics; the surrounding disposition stays at each
call site (do not collapse the three sites' error handling here).

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
  - Does NOT re-derive settings-home resolution — callers pass
    ``settings_home_path`` (from ``coordinator_core._settings_home.settings_home()``).
  - Does NOT use ``locked_rmw`` — that helper is a file-*content*
    read-modify-write wrapper; a venv build lock has no content to RMW.
  - Does NOT poll-with-backoff on lock contention (``_acquire_flock``'s
    shape) — a 30-60s peer build can outlast a short poll window, and
    polling changes the externally-observable latency contract.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from coordinator_core.locked_write import _plat_try_lock, _plat_unlock
from coordinator_core.trusted_root_guard import coordinator_trusted_root_guard
from coordinator_core.win_portability import is_executable
from coordinator_core.install.write_surface import (
    ShapedClause,
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_NO_CONSOLE = {"creationflags": _CREATE_NO_WINDOW} if os.name == "nt" else {}

_NETWORK_ERROR_RE = re.compile(
    r"Could not find a version|ConnectionError|TimeoutError|"
    r"Temporary failure in name resolution|Network is unreachable|"
    r"Failed to establish a new connection",
    re.IGNORECASE,
)

#: Single source of truth for the venv's non-editable pip deps and the
#: matching health-probe import names — ``_install_deps`` and
#: ``_venv_healthy`` both derive from these two tuples so the installed dep
#: set and the acceptance oracle cannot drift apart (a venv missing a dep the
#: oracle doesn't probe for silently passes health and never rebuilds).
#: ``coordinator_whoami`` is installed separately (editable, from
#: ``whoami_pkg``) but probed here alongside the pip-installed deps.
_VENV_PIP_DEPS = ("pydantic>=2", "psutil>=5.9")
_VENV_IMPORT_PROBES = ("coordinator_whoami", "pydantic", "psutil")

#: Single source of truth for the machine-local registry key `_set_pin`
#: writes and `_clear_dangling_pin` deletes -- both read this constant
#: rather than restating the literal, and `WRITE_SURFACE` below declares
#: against it too, so all three cannot drift apart independently.
_PIN_KEY = "coordinator.python"

_VENV_TREE_CLAUSE_INDEX = 0
"""Index of `WRITE_SURFACE`'s sole SHAPED clause (the venv tree itself) —
the only clause `ensure_coordinator_venv` journals against; the pin-key and
build-lock clauses are `StaticClause`s and need no resolution."""


def _record_resolution(clause_index: int, entries) -> None:
    """Deferred-import wrapper over `resolution_journal.record_resolution`
    — see `clone_sibling_repo._record_resolution`'s docstring for why a
    module-level import of `resolution_journal` is not used here (this
    module is transitively reachable from `coordinator_core.ops`'s eager
    op-registration walk via its own downstream import graph)."""
    from coordinator_core.install import resolution_journal

    resolution_journal.record_resolution("ensure-venv", clause_index, entries)

WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="ensure-venv",
    source_module="coordinator_core.install.ensure_venv",
    clauses=(
        # Clause 1 -- the venv tree itself. `ensure_coordinator_venv` creates
        # (`_create_venv`) and pip-populates (`_install_deps`) a virtualenv
        # rooted at `<settings_home>/.coordinator-venv/`; its contents depend
        # on what gets installed (`_VENV_PIP_DEPS` plus the editable
        # `coordinator_whoami` package), so this is SHAPED -- a discovery
        # mechanism naming the tree, not an enumerated site-packages listing.
        ShapedClause(
            discovered_by="ensure_coordinator_venv (settings_home_path / '.coordinator-venv')",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path="<settings-home>/.coordinator-venv/",
            ),
        ),
        # Clause 2 -- the machine-local interpreter-pin key, written
        # idempotently by `_set_pin` once the venv is healthy/rebuilt.
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="machine-local-key",
                    key=_PIN_KEY,
                    reason="interpreter pin, written by _set_pin",
                ),
            ),
        ),
        # Clause 3 -- the same key, DELETED by `_clear_dangling_pin` when a
        # build fails and `clear_pin_on_failure=True` (substrate.py's C10a-3
        # call site opts in; an advisory caller like maximalist.py does not).
        # A dangling pin pointing at a just-removed venv is worse than no
        # pin, so this is a genuine `effect="delete"` surface in its own
        # right, not folded into clause 2.
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
        # Clause 4 -- the build-lock sidecar. `ensure_coordinator_venv` opens
        # `<venv_dir>.lock` with `O_CREAT` and never unlinks it: the advisory
        # flock releases on close, but the FILE persists on the machine after
        # a successful install. A transient lock cleaned up in-run would be
        # out of this manifest's remit; one that outlives the run is a
        # durable surface uninstall has to account for.
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
(`_create_venv`/`_install_deps`), a single machine-local PIN KEY
(`_set_pin` writes it, `_clear_dangling_pin` deletes it under
`clear_pin_on_failure=True`), and a build-lock sidecar FILE
(`<venv_dir>.lock`, O_CREAT'd and never unlinked). Both write and delete on
the pin key are declared as separate clauses -- `validate()` and the C4
emission op both key on `effect`, and collapsing them would hide that this
writer can also remove state, not merely add it. `clear_pin_on_failure`
reaches no surface beyond the pin key: it gates only the
`_clear_dangling_pin` call, which reads and writes nothing else.
"""


class EnsureVenvError(RuntimeError):
    """Mirrors a bash ``exit 1`` failure in the ported oracle."""


class EnsureVenvContention(EnsureVenvError):
    """Raised when the build lock is already held by another process.

    Fail-loud, immediate (no polling) — mirrors the bash mkdir-mutex
    contention contract exactly (see module docstring)."""


def _is_windows_shell() -> bool:
    return (
        os.environ.get("OSTYPE") in ("msys", "cygwin")
        or os.environ.get("OS") == "Windows_NT"
    )


def _run(argv, **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("timeout", 60)
    return subprocess.run(argv, **_NO_CONSOLE, **kwargs)


def _quiet_output(argv) -> str:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=15, **_NO_CONSOLE
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"ensure-coordinator-venv: {argv[0] if argv else '<empty argv>'} failed: {exc}", file=sys.stderr)
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def venv_python_path(venv_dir: Path) -> Path:
    """Cross-platform VENV_PY: ``Scripts/python.exe`` on Windows, ``bin/python`` on POSIX."""
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
            **_NO_CONSOLE,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Routine on a fresh/rebuilding venv (exec missing, probe hangs) —
        # False here just means "not healthy yet", which the caller rebuilds;
        # not worth a diagnostic on every normal first-install run.
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


def _resolve_whoami_pkg(plugin_root: Path, ml_cli: Optional[list]) -> Path:
    """WHOAMI_PKG seam: registry ``coordinator.whoami_src`` -> dir, else
    ``plugin_root/whoami`` (AC B8, incl. stale-key warning)."""
    seam = _ml_get(ml_cli, "coordinator.whoami_src") if ml_cli is not None else ""
    if seam:
        seam_path = Path(seam)
        if seam_path.is_dir():
            return seam_path
        print(
            f"[ensure-coordinator-venv] WARNING: coordinator.whoami_src='{seam}' is not "
            f"a directory; falling back to {plugin_root / 'whoami'}",
            file=sys.stderr,
        )
    return plugin_root / "whoami"


def _set_pin(ml_cli: Optional[list], venv_py: Path) -> None:
    """Idempotent pin write; graceful degradation when the CLI is absent
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
    current = _ml_get(ml_cli, _PIN_KEY)
    if current == venv_py_str:
        return
    if "/.claude/.claude/" in current:
        print(
            f"[ensure-coordinator-venv] self-healing doubled venv pin: "
            f"'{current}' → '{venv_py_str}'",
            file=sys.stderr,
        )
    _ml_set(ml_cli, _PIN_KEY, venv_py_str)


def _clear_dangling_pin(ml_cli: Optional[list], venv_py: Path) -> None:
    """Invalidate a ``coordinator.python`` pin left pointing at ``venv_py``
    after a rebuild attempt failed and the (partial) venv was removed.

    A dangling pin is worse than no pin at all: ``pyresolve.resolve_python_bin``
    treats a found-but-broken pin as a hard failure and never falls through to
    OS-detect (deliberately, so a genuinely misconfigured pin fails loud rather
    than silently). Left in place, a single failed rebuild would turn every
    subsequent coordinator invocation into a hard failure instead of degrading
    to the pre-venv OS-detect fallback. Clearing (not merely leaving) the pin
    restores that fallback. Graceful degradation when the CLI is absent (same
    contract as ``_set_pin``) — this is advisory cleanup, never a raise.
    """
    if ml_cli is None:
        return
    venv_py_str = str(venv_py)
    if _ml_get(ml_cli, _PIN_KEY) != venv_py_str:
        return
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


def _resolve_base_python() -> Optional[str]:
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _create_venv(base_py: str, venv_dir: Path) -> None:
    proc = _run([base_py, "-m", "venv", str(venv_dir)], timeout=120)
    if proc.returncode != 0:
        raise EnsureVenvError(
            f"[ensure-coordinator-venv] ERROR: venv creation failed (exit {proc.returncode})."
        )


def _install_deps(venv_py: Path, whoami_pkg: Path) -> None:
    """One pip invocation, network-vs-generic failure classification
    preserved (AC B6). Caller removes the partial venv on failure.

    Deliberate isolation boundary, not a candidate for an in-process
    import — this runs ``pip install`` against the target venv's own
    interpreter (``venv_py``), which is by construction a different
    interpreter/venv than the one running this module; there is no
    in-process equivalent of installing into another interpreter's
    site-packages. See
    ``state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md``
    for the recorded verdict."""
    try:
        proc = subprocess.run(
            [str(venv_py), "-m", "pip", "install", "-e", f"{whoami_pkg}/", *_VENV_PIP_DEPS],
            capture_output=True,
            text=True,
            timeout=600,
            **_NO_CONSOLE,
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
    """Idempotently ensure the coordinator venv exists and is healthy.

    Returns one of ``"ready"``, ``"rebuilt"``, ``"would-rebuild"``,
    ``"would-write"`` (AC B3). Raises :class:`EnsureVenvError` (or its
    :class:`EnsureVenvContention` subclass on lock contention) on any
    failure — callers decide their own disposition (fatal / fallback /
    advisory); this function never exits the process itself.

    ``clear_pin_on_failure`` (default ``False``): whether a build failure may
    blank an existing ``coordinator.python`` registry pin that names the
    just-destroyed venv (see ``_clear_dangling_pin``). Defaults OFF because
    this call's own disposition-neutral module docstring promises "the
    surrounding disposition stays at each call site" — a mutation of
    persistent registry state on failure is exactly such a disposition, so it
    must be opted INTO by a caller that has actually reasoned about it, not
    fire unconditionally underneath every caller including advisory ones.
    Pass ``True`` from a genuinely fatal-disposition caller (no fallback venv
    to fall back on) where leaving the dangling pin in place would otherwise
    turn one failed rebuild into every subsequent coordinator invocation
    hard-failing (see ``_clear_dangling_pin``'s own docstring) — this was
    ``ensure_coordinator_venv``'s only behavior prior to this parameter, and
    ``substrate.py``'s C10a-3 call site still opts in for exactly that
    reason. An advisory caller (e.g. ``maximalist.py`` Step 6, which reports
    the failure as non-fatal and continues either way) should NOT opt in --
    an advisory phase failing is not a decision to degrade persisted
    registry state (2026-07-28 install-dogfood friction log, finding F7's
    "second-order damage").
    """
    plugin_root = Path(plugin_root)
    settings_home_path = Path(settings_home_path)

    # Trusted-root guard runs before ANY venv mutation, incl. --check
    # (mirrors the bash script's unconditional guard placement) (AC B9).
    coordinator_trusted_root_guard(mode="fail-loud", root=str(plugin_root), site=site)

    # CLAUDE_HOME /.claude-suffix guard (fail loud) — doubled-path precondition.
    # Separator-agnostic: a Windows CLAUDE_HOME arrives backslash-separated
    # (e.g. "...\.claude"), so a bare "/.claude" suffix check silently misses
    # the doubled-path precondition on Windows.
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
                current = _ml_get(ml_cli, _PIN_KEY)
                if current != str(venv_py):
                    return "would-write"
            return "ready"
        return "would-rebuild"

    # Fast path: already healthy — no mutation.
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

        # Re-check health after acquiring the lock — another session may
        # have finished building while we waited for it.
        if _venv_healthy(venv_py):
            _set_pin(ml_cli, venv_py)
            _record_resolution(
                _VENV_TREE_CLAUSE_INDEX, (WriteSurfaceEntry(kind="file-path", path=str(venv_dir)),)
            )
            return "ready"

        if venv_dir.is_dir():
            shutil.rmtree(venv_dir)

        # Any failure from here on leaves venv_dir gone (or partial) — clear a
        # pre-existing pin that pointed at venv_py rather than leave it
        # dangling (see _clear_dangling_pin docstring).
        try:
            base_py = _resolve_base_python()
            if not base_py:
                raise EnsureVenvError(
                    "[ensure-coordinator-venv] ERROR: no python3 or python found in PATH.\n"
                    "[ensure-coordinator-venv]   Install Python 3.10+ and ensure it is on PATH."
                )
            _create_venv(base_py, venv_dir)

            whoami_pkg = _resolve_whoami_pkg(plugin_root, ml_cli)
            _install_deps(venv_py, whoami_pkg)
        except EnsureVenvError:
            shutil.rmtree(venv_dir, ignore_errors=True)
            if clear_pin_on_failure:
                _clear_dangling_pin(ml_cli, venv_py)
            # The rebuild attempt failed and the (partial) tree was just
            # removed above — the venv tree genuinely resolved to nothing
            # this run, not "we never got there" (we DID get there, and it
            # ended in no tree on disk). Review: coordinator:code-reviewer
            # (2026-08-06, rcpt-R3-writer-wiring) compared this against
            # `clone_sibling_repo.py`'s opposite choice (unreported on a
            # failed clone) as two nearest-analogous sites disagreeing — the
            # difference is principled, not an inconsistency: THIS module
            # actively confirms empty-tree via the `shutil.rmtree` line
            # directly above before journaling `()`, where
            # `clone_sibling_repo.py` never inspects or cleans up whatever a
            # failed `git clone` may have left behind, so it cannot make the
            # same "confirmed empty" claim (see that module's matching note).
            _record_resolution(_VENV_TREE_CLAUSE_INDEX, ())
            raise

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
                pass  # best-effort unlock; os.close(fd) below and process exit release it regardless
        os.close(fd)
