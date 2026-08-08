"""
coordinator_core.install._shared — shared path/decision seams for the install
and uninstall hubs.

Port of the resolution logic duplicated (by original bash design) between
``coordinator/lib/install-substrate.sh`` (example-doctrine-repo 6fb5fb37, 2026-07-22) and
``coordinator/lib/uninstall-legs.sh`` (example-doctrine-repo bd5b5a96, 2026-07-19) [example-doctrine-repo
repo]. The bash pairing deliberately kept these hand-synced (uninstall-legs
re-derived install path decisions rather than sharing code) — the hitlist
that seeded this port names that as a footgun to FIX, not reproduce, so both
consumers here import from one module.

Also carries the Port of: ``coordinator/lib/settings-hook-identity.sh`` (example-doctrine-repo
c187f5b9, 2026-07-21) ``settings_hook_identity_inverse_strip`` (jq-based in
bash; pure-Python JSON manipulation here — no jq subprocess).

Spec backlink: docs/plans/2026-07-08-coordinator-uninstall.md § C2-C6.

Negative-spec: does NOT re-implement ``coordinator_settings_home()`` — that
seam already exists at ``coordinator_core._settings_home.settings_home`` (C1,
frozen contract) and is imported here, not re-derived.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterator, Optional, Tuple, Union

from coordinator_core._settings_home import settings_home  # noqa: F401  (re-exported)
from coordinator_core.machine_resolver import registry_get
from coordinator_core.win_portability import no_console_creationflags


@contextlib.contextmanager
def env_overlay(overlay: Dict[str, str]) -> Iterator[None]:
    """Apply ``overlay`` onto ``os.environ`` for the duration of the block, then
    restore the prior environment exactly (including removing keys that did not
    exist before).

    Purpose: the install hubs were ported from bash scripts where a bare
    ``export`` was correct because the process was about to exit. As imported
    Python modules the same assignment persists for the life of the interpreter
    and is inherited by every later ``subprocess.run`` child — under pytest that
    leaks across tests (see the 2026-07-21 interpreter-global-state sweep and
    ``cab185fa``). An in-process phase that must SEE a variable its subprocess
    predecessor received via ``env=`` wraps that phase in this manager instead of
    writing the variable process-wide.

    Not thread-safe: ``os.environ`` is interpreter-global, so concurrent overlays
    interleave. The install hubs are single-threaded orchestrators; do not reach
    for this from a thread pool.
    """
    saved = dict(os.environ)
    try:
        os.environ.update(overlay)
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


class RequireHomeError(RuntimeError):
    """Raised when no home env var resolves to a usable absolute path — mirrors
    the bash ``_uninstall_require_home`` fail-loud guard (code-reviewer F1): a
    stripped-env invocation must never silently resolve destructive targets
    to "" -> "/"."""


_HOME_VARS = ("CLAUDE_HOME", "HOME", "USERPROFILE")


def require_home(caller: str) -> str:
    """Return the resolved home: CLAUDE_HOME, falling back to HOME, falling back
    to USERPROFILE. Raise :class:`RequireHomeError` if none is set, or if the
    first one that IS set does not name an absolute path.

    Callers MUST call this before deriving any destructive filesystem target,
    and MUST derive from its RETURN VALUE — never re-read the env themselves.
    Re-deriving is what let the `${CLAUDE_HOME:-$HOME}` shape survive at three
    call sites in uninstall_legs.py after this guard already existed.

    The USERPROFILE rung: HOME is a POSIX convention that native Windows shells
    (PowerShell, cmd.exe) do not set, so a CLAUDE_HOME/HOME-only guard raises on
    every native-Windows invocation and the entire uninstall surface is
    unreachable there. This does NOT weaken the stripped-env guard — an
    environment with none of the three set still fails loud, which is the "" ->
    "/" case the guard was written for.

    The absolute-path check is new and strictly tightens the guard: a relative
    home ('foo', or the Windows drive-relative 'C:foo') silently anchored every
    derived target at the process cwd, which for a destructive path is the same
    class of hazard as the empty home. A malformed home is a configuration
    error, so it raises rather than falling through to the next rung — falling
    through would mask the operator's mistake and act on a different tree than
    they named.
    """
    for var in _HOME_VARS:
        value = os.environ.get(var) or ""
        if not value:
            continue
        candidate = Path(value)
        if not (candidate.is_absolute() or candidate.root):
            raise RequireHomeError(
                f"{caller}: ${var} is set to a non-absolute path {value!r} — refusing "
                "to derive destructive targets from a cwd-relative home. "
                "(On Windows, a drive letter alone is insufficient — use 'C:\\...' form.)"
            )
        return value
    raise RequireHomeError(
        f"{caller}: none of $CLAUDE_HOME, $HOME, $USERPROFILE is set — "
        "cannot resolve destructive targets"
    )


def resolve_coordinator_root(
    coordinator_root_env: Optional[str] = None,
) -> str:
    """Resolve ``<coordinator_root>`` using the SAME seam the settings.json
    hook generator uses, so a strip's identity-key prefix matches the paths
    actually baked into settings.json. Resolution order (DR-071, 2026-07-22 —
    the registry ``repos.example_doctrine_repo`` read is now direct-tomllib first, the
    ``machine-local`` CLI a fallback rung, for reset-safety: the CLI's
    reader/exec bits live under the resettable ``~/.claude/bin/``, so
    "``machine-local get`` works" is not proof the registry itself is
    reachable after a reset):

        1. ``COORDINATOR_ROOT`` env var (explicit override).
        2. machine-local registry ``repos.example_doctrine_repo`` — direct tomllib read
           via ``machine_resolver.registry_get``, falling back to the
           ``machine-local get`` CLI if that can't resolve -> ``<repo>/coordinator``.
        3. ``REPO_EXAMPLE_DOCTRINE_REPO`` env var -> ``<repo>/coordinator``.
        4. ``${CLAUDE_HOME:-$HOME}/.doe-root`` pointer file -> ``<repo>/coordinator``.

    Raises :class:`RuntimeError` (fail loud, per the Staff Engineer F7) if no resolution
    succeeds or the resolved dir does not exist on disk — callers MUST NOT
    treat an empty resolution as "strip zero groups and exit 0".
    """
    root = ""

    env_root = coordinator_root_env if coordinator_root_env is not None else os.environ.get("COORDINATOR_ROOT")
    if env_root:
        root = env_root.rstrip("/")

    if not root:
        example_doctrine_repo = registry_get("repos.example_doctrine_repo")
        if not example_doctrine_repo:
            ml = _which("machine-local")
            if ml:
                example_doctrine_repo = _run_quiet([ml, "get", "repos.example_doctrine_repo"])
        if example_doctrine_repo:
            root = example_doctrine_repo.rstrip("/") + "/coordinator"

    if not root and os.environ.get("REPO_EXAMPLE_DOCTRINE_REPO"):
        root = os.environ["REPO_EXAMPLE_DOCTRINE_REPO"].rstrip("/") + "/coordinator"

    if not root:
        try:
            claude_home = require_home("resolve_coordinator_root")
        except RequireHomeError:
            claude_home = ""
        doe_root_pointer = Path(claude_home) / ".doe-root"
        if doe_root_pointer.is_file():
            try:
                example_doctrine_repo = doe_root_pointer.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                print(f"_shared: cannot read {doe_root_pointer}: {exc}", file=sys.stderr)
                example_doctrine_repo = ""
            if "\n" in example_doctrine_repo.rstrip("\n"):
                print(
                    f"uninstall-legs: {doe_root_pointer} is malformed (multi-line) "
                    "— expected a single path",
                )
                example_doctrine_repo = ""
            example_doctrine_repo = example_doctrine_repo.strip().rstrip("/")
            if example_doctrine_repo:
                root = example_doctrine_repo + "/coordinator"

    if not root or not os.path.isdir(root):
        msg = [
            "uninstall-legs: cannot determine which hook paths are "
            "coordinator-owned — resolve coordinator root or pass --coordinator-root",
            "  Tried: COORDINATOR_ROOT env, machine-local registry repos.example_doctrine_repo",
            "         (direct read, then machine-local CLI fallback),",
            "         REPO_EXAMPLE_DOCTRINE_REPO env, ${CLAUDE_HOME:-$HOME}/.doe-root pointer.",
        ]
        if root:
            msg.append(f"  Resolved candidate does not exist on disk: {root}")
        raise RuntimeError("\n".join(msg))

    return root


def _which(name: str) -> Optional[str]:
    from shutil import which

    return which(name)


def _run_quiet(argv, env=None) -> str:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
            stdin=subprocess.DEVNULL,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"_shared._run_quiet: {argv[0] if argv else '<empty argv>'} failed: {exc}", file=sys.stderr)
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def resolve_machine_local_cli(plugin_root: Optional[str]) -> Optional[list]:
    """Resolve an invocation argv for the machine-local CLI, resolved
    relative to THIS INSTALL/UNINSTALL RUN's own coordinator plugin tree (the
    ``plugin_root`` arg — the caller passes ``CLAUDE_PLUGIN_ROOT`` or an
    equivalent resolved root), not the target coordinator root being
    installed/uninstalled (bash comment on ``_uninstall_ml_set``: using the
    target root was the original bug — it worked only by coincidence when
    running-coordinator == target-coordinator).

    Resolution order mirrors ``_uninstall_ml_set``:
        1. ``<plugin_root>/templates/bin/_machine_local.py`` via python3 (Convention B).
        2. ``<plugin_root>/bin/machine-local`` shim, if executable.
        3. ``machine-local`` on PATH.

    Returns ``None`` if no resolution path succeeds.
    """
    if plugin_root:
        py_impl = Path(plugin_root) / "templates" / "bin" / "_machine_local.py"
        if py_impl.is_file():
            import sys

            return [sys.executable, str(py_impl)]
        shim = Path(plugin_root) / "bin" / "machine-local"
        if os.name == "nt":
            # The bare shim is EXTENSION-LESS, so CreateProcess cannot exec it
            # (WinError 193) — the exact defect the py_impl rung above exists to
            # avoid, which used to survive here on the fallback path. Prefer the
            # delivered .cmd sibling; never return the bare shim on Windows.
            # os.access(X_OK) is also meaningless here (it degrades to an
            # existence check), so gate on is_file().
            # Review: code-reviewer 2026-07-20 Finding 2 (P2).
            shim_cmd = shim.with_suffix(".cmd")
            if shim_cmd.is_file():
                return [str(shim_cmd)]
        elif os.access(shim, os.X_OK):
            return [str(shim)]
    on_path = _which("machine-local")
    if on_path:
        return [on_path]
    return None


def ml_set(
    key: str,
    value: str,
    *,
    plugin_root: Optional[str] = None,
    registry_dir: Optional[str] = None,
) -> bool:
    """Clear (or set) a machine-local registry key. Mirrors bash
    ``_uninstall_ml_set``: ``machine-local set <key> ""`` clears a key by
    writing an empty-string value (a value overwrite, not a key-delete) —
    every registry reader treats an empty value as "not a hit".

    Returns True on success, False if no CLI could be resolved or the
    invocation failed (never raises — callers fail loud themselves on the
    False return, matching the bash non-zero-return contract)."""
    argv = resolve_machine_local_cli(plugin_root)
    if argv is None:
        print(
            "_uninstall_ml_set: cannot resolve a machine-local CLI (no sibling "
            "bin/machine-local, no templates/bin/_machine_local.py, and no "
            f"fallback on PATH) — cannot clear {key}"
        )
        return False
    env = dict(os.environ)
    if registry_dir:
        env["MACHINE_LOCAL_REGISTRY_DIR"] = registry_dir
    try:
        proc = subprocess.run(
            argv + ["set", key, value],
            env=env,
            timeout=15,
            stdin=subprocess.DEVNULL,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"_uninstall_ml_set: {argv[0]} set {key} failed: {exc}", file=sys.stderr)
        return False
    return proc.returncode == 0


def ml_get(
    key: str,
    *,
    plugin_root: Optional[str] = None,
    registry_dir: Optional[str] = None,
) -> str:
    """Read a machine-local registry key; returns "" if unresolvable/absent
    (mirrors bash ``"$(... get key 2>/dev/null || true)"``)."""
    argv = resolve_machine_local_cli(plugin_root)
    if argv is None:
        return ""
    env = dict(os.environ)
    if registry_dir:
        env["MACHINE_LOCAL_REGISTRY_DIR"] = registry_dir
    return _run_quiet(argv + ["get", key], env=env)


def atomic_write_bytes(
    target: Union[str, Path], data: bytes, *, preserve_mode: bool = True
) -> None:
    """Byte-level atomic-write primitive — a doctrine PORT of
    :func:`coordinator_core.install.gen_settings_hooks._atomic_write_json`'s
    write mechanics, not a simplified variant (C6, the Staff Engineer F4/D8 "quartet
    reuse — doctrine, not functions"). Same shape: a same-DIRECTORY
    ``tempfile.mkstemp`` (guarantees ``os.replace`` lands on the same
    filesystem, so the replace is atomic), write the full ``data``,
    re-``chmod`` the tempfile to the destination's PRIOR mode (captured via
    ``os.stat`` BEFORE the write — ``os.replace`` does not carry mode bits
    forward from a freshly ``mkstemp``-ed file) when ``preserve_mode`` is
    True and a destination already exists, then ``os.replace(tmp_name,
    target)``. On ANY exception the tempfile is removed and the original
    exception re-raised, leaving ``target`` untouched — no truncated or
    partially-written destination results from an interrupted write.

    Deliberately does NOT port ``_extract_preserved``/``_merge_env``
    (JSON-merge helpers with no analogue for an opaque, non-JSON destination
    — see ``coordinator_core.install.substrate``'s C6 call site for why) or
    ``_group_is_generated``/``_cmd_path`` (intra-file command-path
    predicates with no file-level analogue). This function is pure write
    MECHANICS only — it carries no destination-provenance knowledge, so a
    caller choosing WHEN to invoke it (e.g. a foreign-tracked-overwrite
    classification) stays entirely the caller's decision, never this
    function's."""
    target = Path(target)
    out_dir = target.parent if str(target.parent) else Path(".")
    out_dir.mkdir(parents=True, exist_ok=True)
    prior_mode = None
    if preserve_mode and target.is_file():
        prior_mode = os.stat(target).st_mode
    fd, tmp_name = tempfile.mkstemp(prefix=".atomic-write.", dir=str(out_dir))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if prior_mode is not None:
            os.chmod(tmp_name, prior_mode)
        os.replace(tmp_name, str(target))
    except Exception:
        try:
            os.remove(tmp_name)
        except OSError:
            pass  # best-effort cleanup on an already-failing path; original exception re-raises below
        raise


def is_pointer(path) -> bool:
    """True if ``path`` is a POSIX symlink OR a Windows directory junction.

    Review: code-reviewer (Finding 2, AC A3/A4) — ``os.path.islink()`` alone
    only detects the NTFS ``IO_REPARSE_TAG_SYMLINK`` tag; a directory
    junction created by ``mklink /J`` (what ``_install_compat_pointer``'s
    Windows branch actually creates) carries the different
    ``IO_REPARSE_TAG_MOUNT_POINT`` tag and ``islink()`` returns False for it,
    breaking idempotence on Windows re-runs. Also used by ``substrate.py``'s
    whoami-relocation pointer logic — this fix must be correct for both
    callers.
    """
    if os.path.islink(str(path)):
        return True
    return _is_windows_junction(str(path))


def _is_windows_junction(path: str) -> bool:
    """Best-effort detection of an NTFS directory junction
    (``IO_REPARSE_TAG_MOUNT_POINT``). No-op (returns False) on non-Windows
    platforms and on any resolution failure — mirrors ``is_pointer``'s
    fail-safe (never raises, never blocks the caller's guard)."""
    if os.name != "nt":
        return False
    if not os.path.isdir(path):
        return False
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None:  # Python 3.12+
        try:
            return isjunction(path)
        except OSError:
            return False
    return _is_windows_junction_ctypes(path)


def _is_windows_junction_ctypes(path: str) -> bool:
    """Pre-3.12 fallback: read the file's reparse-tag via
    ``GetFileAttributesW`` + ``FSCTL_GET_REPARSE_POINT`` and compare against
    ``IO_REPARSE_TAG_MOUNT_POINT``. Windows-only; imports ``ctypes`` lazily
    so this module stays importable on POSIX hosts."""
    import ctypes

    FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
    IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
    FSCTL_GET_REPARSE_POINT = 0x900A8
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    OPEN_EXISTING = 3
    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x1
    FILE_SHARE_WRITE = 0x2
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    attrs = ctypes.windll.kernel32.GetFileAttributesW(path)  # type: ignore[attr-defined]
    if attrs == INVALID_FILE_ATTRIBUTES:
        return False
    if not (attrs & FILE_ATTRIBUTE_REPARSE_POINT):
        return False

    handle = ctypes.windll.kernel32.CreateFileW(  # type: ignore[attr-defined]
        path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        return False
    try:
        buf_size = 16 * 1024
        buf = ctypes.create_string_buffer(buf_size)
        bytes_returned = ctypes.wintypes.DWORD(0) if hasattr(ctypes, "wintypes") else ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.DeviceIoControl(  # type: ignore[attr-defined]
            handle,
            FSCTL_GET_REPARSE_POINT,
            None,
            0,
            buf,
            buf_size,
            ctypes.byref(bytes_returned),
            None,
        )
        if not ok:
            return False
        reparse_tag = int.from_bytes(buf.raw[0:4], byteorder="little")
        return reparse_tag == IO_REPARSE_TAG_MOUNT_POINT
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Pure-Python inverse-strip (no jq subprocess)
# ---------------------------------------------------------------------------


# Interpreter prefixes a generated hook command may start with (bare form)
# or a guarded form may invoke (see `_GUARDED_POSIX_RE`/`_GUARDED_PS_RE`
# below) — one list, shared by `_cmd_path` and `_split_hook_command` so the
# two never drift.
_INTERPRETER_PREFIXES = ("bash ", "node ", "python3 ", "python ")

# Recognizes the exit-code-hygiene guarded shape `wrap_hook_command_guarded`
# emits (see that function below) and pulls the inner `<interpreter>
# "<script_path>"<rest_args>` invocation back out, so `_cmd_path` can
# classify a guarded command exactly like the bare-portable/legacy forms it
# already handled. POSIX: `[ -n "<env_expr>" ] && [ -f "<path>" ] && exec
# <interpreter> "<path>"<rest> || exit 127`. PowerShell: `if (<env_expr> -and
# (Test-Path -LiteralPath "<path>" -PathType Leaf)) { <interpreter>
# "<path>"<rest>; exit $LASTEXITCODE } else { exit 127 }`. Both are anchored
# at the START of the command and require the FULL leading guard-condition
# literal, not just the trailing invocation shape — an unanchored,
# trailing-only match would unwrap any hand-authored hook that happens to
# END in the same shape, and if the unwrapped path then starts with the
# portable prefix, `_group_is_generated` would silently classify (and
# `settings_hook_identity_inverse_strip` would silently DROP) an operator's
# own hook on the next regeneration (code-reviewer F1, 2026-07-28).
#
# shell-doc-ok: the two guard-condition literals quoted above are the real
# POSIX and PowerShell command text `wrap_hook_command_guarded` emits and the
# two regexes below match byte-for-byte; a prose re-render would decouple the
# comment from the patterns it exists to explain.
_GUARDED_POSIX_RE = re.compile(r'^\[ -n "[^"]*" \] && \[ -f "[^"]*" \] && exec (\S+) "([^"]+)"(.*) \|\| exit 127$')
_GUARDED_PS_RE = re.compile(
    r'^if \(\S+ -and \(Test-Path -LiteralPath "[^"]*" -PathType Leaf\)\) '
    r'\{ (\S+) "([^"]+)"(.*); exit \$LASTEXITCODE \} else \{ exit 127 \}$'
)

# Fourth era (plan C2): the interpreter-existence-widened guard shape
# `wrap_hook_command_guarded` emits when `python_bin_resolved=True` — a
# THIRD bracket/Test-Path test for the interpreter itself, and the
# interpreter token quoted as a resolved `$COORDINATOR_PYTHON_BIN` reference
# rather than a bare `python3`/`python` word (see that function's own
# docstring for the exact strings). A dedicated pair of patterns, not a
# loosened middle-test on the two above (dispatch brief: do not relax the
# existing anchored, full-literal patterns to an "optional middle test"
# shape that would also match arbitrary bracket chains).
#
# The interpreter leg is captured but discarded here (`[^"]*`, no group) —
# it is always the single fixed `$COORDINATOR_PYTHON_BIN`/`$env:...` literal,
# never the bare `python3`/`python` word `_INTERPRETER_PREFIXES` strips, so
# `_cmd_path` below skips the prefix-strip step entirely for this era and
# takes the captured script path directly. A backreference (`\1`) requires
# the `-f`/`Test-Path` leg's path and the `exec`/`&`-invoked path to be the
# IDENTICAL string, matching what `wrap_hook_command_guarded` actually
# emits (`script_path` spliced into both positions verbatim).
#
# shell-doc-ok: same convention as the pair above — the literals here are
# real command text, not a paraphrase.
_GUARDED_POSIX_PYBIN_RE = re.compile(
    r'^\[ -n "[^"]*" \] && \[ -x "[^"]*" \] && \[ -f "([^"]+)" \] && '
    r'exec "[^"]*" "\1"(.*) \|\| exit 127$'
)
_GUARDED_PS_PYBIN_RE = re.compile(
    r'^if \(\S+ -and \(Test-Path -LiteralPath "[^"]*" -PathType Leaf\) '
    r'-and \(Test-Path -LiteralPath "([^"]+)" -PathType Leaf\)\) '
    r'\{ & \S+ "\1"(.*); exit \$LASTEXITCODE \} else \{ exit 127 \}$'
)


def _cmd_path(command: str) -> str:
    """Strip a leading interpreter prefix (bash/node/python3/python) from a
    command string. Mirrors the jq ``cmd_path`` def.

    Understands FOUR eras of generated command shape, all of which must
    classify identically for `_group_is_generated`/`_stray_check` identity
    purposes (see those callers): legacy baked-absolute
    (``python3 /abs/coordinator/hooks/scripts/x.py``), bare-portable
    (``python3 $COORDINATOR_CONTENT_ROOT/hooks/scripts/x.py``), the guarded
    form (POSIX ``exec``-based or PowerShell ``$LASTEXITCODE``-based — see
    `wrap_hook_command_guarded`), and the plan-C2 resolved-interpreter
    guarded form (a widened guard that also existence-tests the interpreter,
    invoked via a quoted ``$COORDINATOR_PYTHON_BIN``/``$env:...`` reference
    rather than a bare ``python3``/``python`` word).

    A guarded command is first unwrapped back to its bare-portable-equivalent
    shape: the resolved-interpreter era via `_GUARDED_POSIX_PYBIN_RE`/
    `_GUARDED_PS_PYBIN_RE` (which yield the script path directly — the
    matched interpreter is a fixed env-var literal, never one of
    `_INTERPRETER_PREFIXES`, so there is no prefix left to strip), the
    classic guarded era via `_GUARDED_POSIX_RE`/`_GUARDED_PS_RE` (which yield
    ``<interpreter> <path><rest_args>``, interpreter prefix stripped below
    exactly as for the other two eras) — so a caller comparing the RESULT's
    prefix (e.g. against `hook_root_env_expr(...) + "/hooks/"`) sees the same
    string regardless of which era produced the original command."""
    m = _GUARDED_POSIX_PYBIN_RE.search(command) or _GUARDED_PS_PYBIN_RE.search(command)
    if m:
        script_path, rest_args = m.group(1), m.group(2)
        return f"{script_path}{rest_args}"
    m = _GUARDED_POSIX_RE.search(command) or _GUARDED_PS_RE.search(command)
    if m:
        interpreter, script_path, rest_args = m.group(1), m.group(2), m.group(3)
        command = f"{interpreter} {script_path}{rest_args}"
    for prefix in _INTERPRETER_PREFIXES:
        if command.startswith(prefix):
            return command[len(prefix):]
    return command


def _split_hook_command(command: str) -> Tuple[str, str, str]:
    """Split an UN-guarded, already-CPR-rewritten hook command (bare-portable
    or legacy baked-absolute shape — ``<interpreter> <script_path>
    [args...]``) into ``(interpreter, script_path, rest_args)``.
    ``interpreter`` carries no trailing space; ``rest_args`` carries its own
    LEADING space when non-empty (``""`` when there are no additional args),
    so a caller can splice it directly after a closing quote around
    ``script_path`` without a separate space-join step.

    Raises :class:`ValueError` if ``command`` does not start with one of
    `_INTERPRETER_PREFIXES` — this is a CLOSED, stated contract
    (code-reviewer F2, 2026-07-28): `hooks.json` command entries may only
    use ``bash``/``node``/``python3``/``python`` as their interpreter word.
    This is not a bug in this module — it is a deliberate fail-loud posture
    so an unrecognized interpreter (e.g. a future ``pwsh``/``sh``/
    ``python3.11`` entry) aborts `generate()` loudly for every hook rather
    than silently mis-classifying just the offending one. Adding a new
    interpreter to `hooks.json` is a legitimate act — do it by adding the
    prefix to `_INTERPRETER_PREFIXES` above, not by working around this
    error."""
    for prefix in _INTERPRETER_PREFIXES:
        if command.startswith(prefix):
            interpreter = prefix.rstrip()
            remainder = command[len(prefix):]
            break
    else:
        raise ValueError(
            f"_split_hook_command: unrecognized interpreter in hooks.json command "
            f"(not one of {_INTERPRETER_PREFIXES!r}): {command!r} — add the new "
            "interpreter's prefix to _INTERPRETER_PREFIXES if this is intentional"
        )
    parts = remainder.split(" ", 1)
    script_path = parts[0]
    rest_args = f" {parts[1]}" if len(parts) > 1 else ""
    return interpreter, script_path, rest_args


# Env-block key the generator writes `coordinator_root` into ONCE (settings.json
# `env`, already documented — and independently corroborated by third-party
# guides, since the official doc's own wording ("subprocesses Claude Code
# spawns from it") doesn't name hooks specifically — to reach "every hook
# script"), rather than baking the value into all ~37 generated hook commands.
#
# **Superseded design, kept as history for the "why not `$(...)`" question:**
# an earlier version of this fix rewrote `${CLAUDE_PLUGIN_ROOT}` to a
# `$(python3 .../resolve-coordinator-clone --content-root)` command
# substitution — portable, but it spawns a SECOND process on every single
# hook fire. With ~37 hooks across PreToolUse/PostToolUse, that roughly
# doubles per-hook process spawns — precisely the cost class
# `~/.claude/.coordinator-hooks-disabled` was armed for ("Windows
# process-spawn tax makes the bash hook suite unusable"). A portability fix
# that doubles spawn count could never be safely re-enabled, so it doesn't
# count as a fix. This env-var design costs ZERO extra spawns: plain
# variable expansion, not command substitution.
COORDINATOR_CONTENT_ROOT_ENV_KEY = "COORDINATOR_CONTENT_ROOT"

# Sibling env-block key (plan C2, superseding this chunk's original
# command-string-bake design — PM ruling 2026-08-03): the resolved hook
# interpreter, written once into `env` and referenced by every generated
# `python3`/`python` hook command BY VARIABLE, never repeated into command
# text — same rationale as `COORDINATOR_CONTENT_ROOT_ENV_KEY` above.
COORDINATOR_PYTHON_BIN_ENV_KEY = "COORDINATOR_PYTHON_BIN"


def hook_python_env_expr(*, windows: bool) -> str:
    """The zero-subprocess-spawn expression for referencing
    :data:`COORDINATOR_PYTHON_BIN_ENV_KEY` inside a settings.json hook
    ``command`` string. Same POSIX-vs-``$env:`` split, same rationale, as
    :func:`hook_root_env_expr` — see that function's docstring; not
    re-litigated here."""
    return f"$env:{COORDINATOR_PYTHON_BIN_ENV_KEY}" if windows else f"${COORDINATOR_PYTHON_BIN_ENV_KEY}"


def hook_root_env_expr(*, windows: bool) -> str:
    """The zero-subprocess-spawn expression for referencing
    :data:`COORDINATOR_CONTENT_ROOT_ENV_KEY` inside a settings.json hook
    ``command`` string, in the syntax the shell THIS command will actually
    be parsed by expects.

    There is no spelling valid in both — PowerShell reads an OS environment
    variable ONLY via the ``$env:NAME`` prefix; a bare ``$NAME`` there is a
    (normally-unset) POWERSHELL variable, not the env var, and silently
    stringifies to empty rather than failing loud. POSIX shells (`sh`,
    Git-Bash) use plain ``$NAME``. This same POSIX-vs-`$env:` split is
    already established, repo-wide convention — see e.g.
    `coordinator/docs/wiki/multi-channel-claim-discipline.md`'s own
    "shell `$REPO_*` env vars; PowerShell `$env:REPO_*`" row and
    `coordinator/docs/wiki/portable-code-substrate.md`'s PowerShell
    snippets, all of which use `$env:`, never a bare `$VAR`, to read an
    OS env var from PowerShell.

    Resolved PER-GENERATING-MACHINE (``windows`` — call with
    ``os.name == "nt"``), not baked as one universal constant, because
    `gen-settings-hooks` already only ever runs locally for the machine
    whose settings.json it writes — matching `claude-code-platform-gotchas.md`
    § "hooks.json `command` runs via `sh` on Unix, PowerShell on Windows"."""
    return f"$env:{COORDINATOR_CONTENT_ROOT_ENV_KEY}" if windows else f"${COORDINATOR_CONTENT_ROOT_ENV_KEY}"


def wrap_hook_command_guarded(
    command: str, *, windows: bool, python_bin_resolved: bool = False
) -> str:
    """Wrap an already-CPR-rewritten hook command (``<interpreter>
    <script_path>[ args...]``, e.g. ``python3
    $COORDINATOR_CONTENT_ROOT/hooks/scripts/x.py --flag``) so an
    unresolvable :data:`COORDINATOR_CONTENT_ROOT_ENV_KEY` — unset, empty, or
    pointing at a script that does not exist on disk — exits **127**
    instead of falling through to the interpreter's own file-not-found exit
    (which measures as **2** on this harness for a missing script, and 2 is
    the PreToolUse hook contract's BLOCKING-DENY sentinel). Every hook whose
    command is unresolvable would otherwise deny in lock-step with every
    other hook wired the same way — including Bash/Write/Edit — bricking
    the very tools needed to repair the settings.json that caused it. This
    guard exists to make that collision structurally impossible: a
    resolution failure now reads as "hook absent" (127, non-blocking
    warning), never as "hook denies" (2).

    ``python_bin_resolved`` (plan C2) widens the guard to ALSO existence-test
    the interpreter itself, but ONLY when ``interpreter`` (from
    `_split_hook_command`) is ``python3`` or ``python`` AND this flag is
    True — ``bash``/``node`` pass through this function completely
    unmodified regardless of the flag, and an unresolved interpreter
    (``python_bin_resolved=False``) falls through to today's exact bare-token
    two-test shape (plan AC3's degradation path — a correct outcome, not a
    failure). This is a pure keyword-only parameter, not a resolver call:
    the caller resolves once per `generate()` run and threads the same
    boolean into every command, so this function stays unit-testable
    without monkeypatching a resolver (required by C4's tests).

    Widened shape's ``[ -x ... ]`` / ``Test-Path -PathType Leaf`` leg closes
    the PowerShell-``$null``-exit-0 hole (a bare ``$env:...`` reference in
    statement position is echoed, not executed, so `` & `` before it is
    REQUIRED) but does NOT catch an existence-tested-but-present-and-wrong
    interpreter (mismatched Python version, a rebuilt venv missing the
    hook's imports) — that lands on the interpreter's own exit 1, an
    accepted residual named in plan AC2.

    POSIX shells (`sh`, Git-Bash) get zero extra process spawns: the
    ``exec`` builtin REPLACES the guard shell with the target interpreter
    rather than forking a child, so the guard costs nothing beyond the
    ``[ ... ]`` test builtins already running in the same shell. PowerShell
    has no `exec` equivalent — a guarded PowerShell hook therefore costs ONE
    extra process relative to the POSIX form (the guard's own
    ``powershell``/``pwsh`` process stays alive to read ``$LASTEXITCODE``
    and propagate it, rather than being replaced). Flagged, not hidden: this
    is a real asymmetry between the two platforms' guard cost, not achieved
    parity.

    The script-path quoting (``"<script_path>"``) is a spaces-in-root
    safety measure — a ``COORDINATOR_CONTENT_ROOT`` containing a space
    (e.g. a Windows ``C:\\Users\\Jane Doe\\...`` clone location) would
    otherwise split the ``[ -f ... ]``/``exec``/``Test-Path`` arguments
    apart unquoted.

    Round-trips through `_cmd_path` (via `_GUARDED_POSIX_RE`/
    `_GUARDED_PS_RE`) — a caller extracting the original script path back
    out of a guarded command gets the same result as for the bare-portable
    form this wraps. The widened ``python_bin_resolved=True`` shape emitted
    above round-trips through its own dedicated fourth-era pair instead —
    `_GUARDED_POSIX_PYBIN_RE`/`_GUARDED_PS_PYBIN_RE` — checked before the
    classic pair (review: code-reviewer F1, 2026-08-03)."""
    interpreter, script_path, rest_args = _split_hook_command(command)
    env_expr = hook_root_env_expr(windows=windows)
    if python_bin_resolved and interpreter in ("python3", "python"):
        python_expr = hook_python_env_expr(windows=windows)
        if windows:
            # The `&` call operator before `$env:COORDINATOR_PYTHON_BIN` is
            # REQUIRED — see docstring's PowerShell-echo note.
            return (
                f'if ({env_expr} -and (Test-Path -LiteralPath "{python_expr}" -PathType Leaf) '
                f'-and (Test-Path -LiteralPath "{script_path}" -PathType Leaf)) '
                f'{{ & {python_expr} "{script_path}"{rest_args}; exit $LASTEXITCODE }} '
                f'else {{ exit 127 }}'
            )
        return (
            f'[ -n "{env_expr}" ] && [ -x "{python_expr}" ] && [ -f "{script_path}" ] && '
            f'exec "{python_expr}" "{script_path}"{rest_args} || exit 127'
        )
    if windows:
        # Costs one extra process vs. the POSIX `exec` form — see docstring.
        return (
            f'if ({env_expr} -and (Test-Path -LiteralPath "{script_path}" -PathType Leaf)) '
            f'{{ {interpreter} "{script_path}"{rest_args}; exit $LASTEXITCODE }} '
            f'else {{ exit 127 }}'
        )
    return (
        f'[ -n "{env_expr}" ] && [ -f "{script_path}" ] && '
        f'exec {interpreter} "{script_path}"{rest_args} || exit 127'
    )


# Spec backlink: coordinator/docs/wiki/external-plugin-live-resolution.md
#   § Hook-delivery — SOLVED via settings.json (CPR is unset for
#   settings.json-registered hooks — bug #38699/#24529 — so baking SOME
#   value is unavoidable; the `env` block is where it gets baked, once,
#   instead of into every hook command).
#
# **Residual gap, flagged rather than silently claimed solved:** this
# narrows the 2026-07-28 cross-machine-settings.json-sync incident from "37
# independently-vulnerable baked paths" to "one env value, written by
# whichever machine last regenerated the file" — it does NOT make the file
# immune to being overwritten wholesale by a peer machine's sync (a
# Windows-shaped `COORDINATOR_CONTENT_ROOT` value or `$env:`-syntax command
# landing on macOS, or vice versa, would still break, just in ONE place
# instead of 37 and under a self-explanatory variable name instead of a
# silently-wrong path). The operational backstop is settings.json being
# regenerated fresh per-machine and never synced/shared — already true
# structurally (untracked, gitignored) but not yet enforced against the
# specific race that caused the 2026-07-28 incident; that enforcement is a
# separate leg (see the `guard-foreign-platform-paths.py` detect-only hook
# and the native git pre-commit check other agents are building concurrently
# for the settings.json-tracking-into-git angle of that same incident).
#
# **Unverified, flagged rather than silently assumed:** whether the settings.json
# `env` block actually reaches hook subprocess environments was NOT
# empirically fired against a live harness for this fix (the kill-switch is
# armed, and touching the real `~/.claude/settings.json` to test was
# explicitly out of scope) — it rests on the official docs' general claim
# ("applied to every session and to subprocesses Claude Code spawns from
# it") plus independent third-party-guide corroboration naming hooks
# specifically ("every hook script can read these variables"; "the hooks
# that fire" are named among the subprocess classes that inherit `env`).
# Unlike the CPR claim (which this same investigation found directly
# CONTRADICTED by a 2026-07-04 empirical test on this exact harness), no
# contradicting evidence or tracked bug was found for `env`-block
# propagation to hooks specifically. That is a materially different
# confidence level than the CPR case, but it is still not a first-party
# empirical fire on this harness — verify with a real hook (e.g. a
# throwaway SessionStart hook that writes `$COORDINATOR_CONTENT_ROOT`/
# `$env:COORDINATOR_CONTENT_ROOT` to a log file) before fully trusting this
# path in production.
def _group_is_generated(group, generated_hooks_dir: str) -> bool:
    """Mirrors the jq ``group_is_generated`` def: True iff ANY command hook
    in ``group.hooks`` resolves (after interpreter-prefix strip) to a path
    under ``<generated_hooks_dir>/`` (the LEGACY baked-absolute-path form,
    still recognized so a pre-portability-fix settings.json strips/
    regenerates cleanly) OR under the current machine's
    :func:`hook_root_env_expr`-anchored ``/hooks/`` prefix (the current
    portable form)."""
    legacy_prefix = generated_hooks_dir.rstrip("/") + "/"
    portable_prefix = hook_root_env_expr(windows=(os.name == "nt")) + "/hooks/"
    for hook in group.get("hooks", []) or []:
        if hook.get("type") == "command":
            cmd_path = _cmd_path(hook.get("command", ""))
            if cmd_path.startswith(legacy_prefix) or cmd_path.startswith(portable_prefix):
                return True
    return False


def settings_hook_identity_inverse_strip(
    settings_json_path: str, coordinator_root: str, out_path: str
) -> None:
    """Write to ``out_path`` a copy of ``settings_json_path`` with every
    generated hook group removed (identity: :func:`_group_is_generated`) and
    every other group — including all non-hook top-level keys such as
    ``.enabledPlugins`` — preserved untouched. Pure-Python port of
    ``settings_hook_identity_inverse_strip`` (bash, jq-based).

    Raises on missing input / malformed JSON — callers must not swallow the
    exception (fails loud, no output written, matching the bash contract)."""
    if not settings_json_path:
        raise ValueError("settings_hook_identity_inverse_strip: missing <settings_json_path>")
    if not coordinator_root:
        raise ValueError("settings_hook_identity_inverse_strip: missing <coordinator_root>")
    if not out_path:
        raise ValueError("settings_hook_identity_inverse_strip: missing <out_path>")
    if not os.path.isfile(settings_json_path):
        raise FileNotFoundError(f"settings_hook_identity_inverse_strip: not found: {settings_json_path}")

    generated_hooks_dir = coordinator_root.rstrip("/") + "/hooks"

    with open(settings_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    hooks = data.get("hooks") or {}
    preserved = {}
    for event, groups in hooks.items():
        kept = [g for g in groups if not _group_is_generated(g, generated_hooks_dir)]
        if kept:
            preserved[event] = kept

    data = dict(data)
    data["hooks"] = preserved

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
