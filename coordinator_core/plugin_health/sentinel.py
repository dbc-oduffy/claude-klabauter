"""
coordinator_core.plugin_health.sentinel — coordinator-doctor probe suite (P-1..P-19
plus P-20/P-21/P-23, minus the pre-existing P-16/P-22 manifest/sentinel skew
documented below) and the
--full-mode `doctor-last-run.json` sentinel writer that scan-addon-health.sh
(coordinator_core.plugin_health.scan) consumes.

Purpose: docs/wiki/coordinator-doctor.md defines runnable probes for the substrate
downstream plugins depend on (machine-local registry, coordinator_whoami, mcpServers
config, bin/ resolvers). This module is the non-skill primitive that fires the
probes on cadence (from /workday-start Step 1.10 --full) and writes
~/.claude/plugins/coordinator-claude/data/doctor-last-run.json.

Port of DoE-claude coordinator/bin/coordinator-doctor-sentinel.sh (989 lines, bash).
The selection grammar already delegated to a Python selector
(coordinator_core.plugin_health.probe_select) via subprocess under the bash oracle —
this port converts that into an in-process import, closing a spawn site that fired
on every single invocation. P-9/P-11/P-13/P-18 formerly shelled out to their own
DoE-owned `.sh` sibling scripts (verify-ue-overrides.sh, verify-templates-setup-
sync.sh, probe-onboarding-currency.sh, check-install-singularity.sh); DoE's W4a
rename (b5a4192c) turned each of those into a thin polyglot trampoline over an
already-native claude-klabauter module, so these 4 probes now call that module's `main()`
directly in-process (via `_call_native_main`) — no bash spawn, no subprocess at
all. These 4 probes formerly gated on an on-disk sibling-script presence check
(`_sibling_present`, retired) before making that in-process call — a leftover
from the bash-oracle era when the sibling script WAS the implementation. Once
the call became in-process against an unconditionally-imported native module,
the presence check proved nothing about whether the call could run, and a
relocated/renamed sibling (as happened in b644d5a9) silently degraded the
probe to a false GREEN instead of surfacing as amber-inconclusive. Each of
these 4 probes now calls its native module directly, unconditionally, with
`_NativeCallFailed -> _inconclusive(...)` as the sole degradation path — see
`docs/wiki/doctor-probe-design.md` § `inconclusive` Is a First-Class Probe
Status. P-15/P-17 formerly shelled
out to their own DoE-owned bash sibling (scripts/lib/prereq_probe.sh, sourced via
`bash -c 'source ...; <fn>'`) — DR-079's prereq-probe-debash-complete-migration
repoint (2026-07-21) retired that bridge in favor of in-process calls to the
already-landed native port `coordinator_core.install.prereq_probe` (which the
DoE bash file's own SSOT header still documents as a PARALLEL Python-native
implementation, not a trampoline over the bash — see that module's docstring).
`_run_prereq_probe_function` now dispatches `func_name` to the corresponding
`prereq_probe` module-level callable in-process; no bash spawn remains for
P-15/P-17. Claude-klabauter does not delete or otherwise touch DoE's or
Example-retrieval-repo-ue-addon's vendored bash copy — only claude-klabauter's own bridge is retired.

P-5/P-6/P-6s (coordinator_whoami probes) deliberately KEEP their subprocess-to-a-
resolved-interpreter shape (coordinator_core.pyresolve.resolve_python_bin(), same
pin precedence as the bash oracle's resolve-python.sh) — this is not a spawn-tax bug
but the actual point of the probe: verifying package importability under the
operator's resolved Python, which may differ from this engine's own interpreter.
"Fixing" this into an in-process import would only ever test this engine's own venv,
defeating the probe's purpose. P-2's tomllib-availability check is preserved as a
subprocess against the same resolved interpreter for the identical reason (the
probe's whole point is testing the RESOLVED interpreter's tomllib availability, not
this engine's own). P-7's mcpServers/enabledPlugins JSON validation, formerly an
inline `python -c` heredoc, collapses to a direct in-process function — pure data
validation with no interpreter-dependent behavior.

Spawn budget (hitlist G8, 2026-08-21). Every subprocess this suite makes derives its
bound from `_PROBE_SPAWN_BUDGET_SECS` — no probe carries a hand-typed timeout, and
`coordinator_core/plugin_health/tests/test_sentinel.py ::
test_no_probe_spawn_carries_a_hand_typed_timeout` fails on a new literal. P-3 no longer
spawns `machine-local keys` at all: it asked the registry a DATA question and paid a
Python interpreter cold start to have the CLI re-read a TOML this process can parse,
duplicating the identical spawn P-4 makes in the same run. It now reads the registry
in-process via `machine_resolver.merged_flat_registry()`, resolved once per run and
shared. P-4 and P-10 keep one spawn each because the resolver CLI is what those two
probes are about — that is a subject, not a lookup.

Manifest/sentinel skew, now closed. doctor-probes.toml declared probes the bash
oracle (coordinator-doctor-sentinel.sh @ HEAD, 989 lines) never carried a body for,
and this port reproduced that skew faithfully rather than fixing it inside a
parity-preserving scope: P-16 (hardware SSOT) inherited it, and P-20/P-21/P-22 were
added to the manifest post-port and acquired it, so `--full` silently never evaluated
any of them. All four are wired now (probe_p16/probe_p20/probe_p21/probe_p22 below;
P-20/P-21 2026-08-17, P-16/P-22 2026-09-02) and `_UNWIRED_PROBE_IDS` is empty. A
manifest id added without a body here is a defect the ledger's guard catches, not a
state to settle into.

Contract to preserve — LOAD-BEARING, cross-plugin (not one of the 8 T0-frozen
contracts, but equally so): the sentinel JSON schema (ran_at, verdict, red_probes,
amber_probes, advisory_notes, hint, machine, plugin) is consumed by
coordinator_core.plugin_health.scan AND, per docs/wiki/addon-health-sentinel.md, by
every other plugin's own doctor writing to the same schema at their own
<plugin>/data/doctor-last-run.json path. Field names, JSON indent=2 + trailing
newline formatting are preserved byte-for-byte.

Self-registration: importing this module calls register_op("plugin_health.sentinel",
...) as a side-effect (same pattern as plugin_health.drift / plugin_health.scan).

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T3a-g2/T3b
Port of: coordinator-doctor-sentinel.sh (DoE b5a4192c, 2026-07-20)
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from coordinator_core._settings_home import normalize_native_path, settings_home
from coordinator_core.doe_root_pointer import read_doe_root_pointer_file
from coordinator_core.install import check_install_singularity
from coordinator_core.install._shared import require_home
from coordinator_core.ipc import CEREMONY_BUDGET_SECS, register_op
from coordinator_core.machine_resolver import merged_flat_registry
from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root
from coordinator_core.ops import probe_onboarding_currency, verify_templates_setup_sync, verify_ue_overrides
from coordinator_core.plugin_health.probe_select import id_to_cluster, load_probes, resolve_active_probes
from coordinator_core.pyresolve import PythonPinInvalid, resolve_python_bin
from coordinator_core.win_portability import is_executable

# Generator-provenance declaration (generator_provenance.py). This sentinel writes
# only `~/.claude/plugins/coordinator-claude/data/doctor-last-run.json` (settings-home
# / operator home directory) -- never a path inside claude-klabauter's own tracked tree.
GENERATES = []

_PROG = "coordinator-doctor-sentinel.sh"

# Every subprocess this suite spawns derives its bound from ONE budget rather
# than carrying its own literal (DR-349 § Decision 1; the shape three
# `ops/ceremony/*` modules already ship). Before this constant existed the suite
# carried ten hand-typed bounds across two values, 15 and 30 -- both members of
# the three house numbers that make up 48% of every dial in the tree, and
# neither derived from anything. Nothing here is allowed to cost seconds: the
# widest thing under this bound is one interpreter cold start, and CLAUDE.md
# § Load norm rules a process over 2s a defect on every machine under every
# load. A breach is therefore a defect report about the probe, which is exactly
# how each call site already routes it -- `_inconclusive(...)`, never a
# fabricated verdict.
#
# Negative-spec: do NOT reintroduce a per-site literal here, and do NOT raise
# this by widening `CEREMONY_BUDGET_SECS` -- that constant is a ratchet
# (DR-348) whose blast radius is every ceremony op in the engine.
_PROBE_SPAWN_BUDGET_SECS: float = CEREMONY_BUDGET_SECS

# severity in {"red", "amber", "advisory"} — advisory NEVER contributes to verdict.
ProbeNote = namedtuple("ProbeNote", ["id", "severity", "message"])

_USAGE = f"Usage: {_PROG} [--triage|--full|--cluster NAME|--probe ID|--symptom TEXT]"

_MCP_SERVING_PLUGINS = {
    "example-retrieval-repo": "example-retrieval-repo",
    "example-game-repo-control": "example-game-repo-control",
    "example-game-repo": "example-game-repo-control",
    "notebooklm": "notebooklm",
}

# Review: code-reviewer (nit) — ndjson rows are now parsed with json.loads
# instead of unanchored regexes; json.loads is free in Python (unlike the
# bash oracle, which had no JSON parser and grepped fields out with sed/grep).
# Regex extraction was fragile against nested objects/escaped quotes.


class _UsageError(RuntimeError):
    """Argument-parsing error — mirrors the bash oracle's _usage_error (exit 2)."""


# ---------------------------------------------------------------------------
# DoE-side sibling script root resolution
# ---------------------------------------------------------------------------


def _doe_coordinator_root() -> Optional[Path]:
    """Resolve the DoE-side coordinator/ root housing this sentinel's still-bash
    sibling probe scripts (P-9/P-11/P-12/P-13/P-15/P-17/P-18/P-19's dependencies).

    Resolution chain:
      1. COORDINATOR_BIN_ROOT env var (test isolation — names coordinator/bin
         directly, mirroring the bash oracle's _SCRIPT_DIR variable). Kept
         first-rung: this is sentinel's own documented test-isolation seam,
         distinct from (and taking precedence over) the shared DoE-root ladder.
      2. coordinator_core.ops.coordinator_doe_root.coordinator_doe_root() — the
         same DOE_ROOT/REPO_DOE_CLAUDE/machine-local ladder every other
         DoE-root-dependent script in the doe-root-sweep wave resolves through
         (Review: code-reviewer — sentinel previously read ~/.claude/.doe-root
         directly and never consulted DOE_ROOT/REPO_DOE_CLAUDE, silently
         diverging from every other consumer in the wave).

    Returns None (never raises) when neither resolves — every dependent probe
    below degrades to its existing "sibling script absent -> silent skip / amber
    inconclusive" branch, exactly as the bash oracle does when a sibling script
    file is missing on disk.
    """
    override = os.environ.get("COORDINATOR_BIN_ROOT")
    if override:
        p = normalize_native_path(override)
        return p.parent if p.name == "bin" else p
    root = coordinator_doe_root()
    if not root:
        return None
    return normalize_native_path(root) / "coordinator"


def _claude_klabauter_bin_root() -> Path:
    """This module's own repo root's `coordinator/bin/` — where
    `doctor-probes.toml` actually lives as of the b644d5a9 executable-surface
    migration (2026-07-22, DoE-claude -> claude-klabauter).

    sentinel.py already runs from INSIDE the resolved claude-klabauter root (no DoE-side
    trampoline required to reach this import), so — mirroring
    `coordinator_core.ops.check_claude_klabauter_doctor_sentinel._claude_klabauter_root` and
    `coordinator_core.ops.render_template_tree`'s co-located-sibling
    resolution for the same migration — the manifest's home is simply this
    file's own repo root, three parents up
    (plugin_health/ -> coordinator_core/ -> <claude_klabauter_root>), no subprocess, no
    machine-local registry lookup. This is NOT a cross-repo `__file__`-walk:
    sentinel.py and the manifest are co-located in the SAME repo post-migration.

    Negative-spec: does NOT consult `coordinator_core.ops.coordinator_doe_root`
    or `coordinator_core.engine_root.coordinator_engine_root()` for this
    default — the manifest is claude-klabauter-native data now, so resolving it through
    a sibling-repo pointer (REPO_DOE_CLAUDE / machine-local `repos.doe_claude`)
    would still be wrong-repo-shaped even where it happens to resolve; the
    fix is to stop treating manifest location as a DoE-root question at all.
    """
    return Path(__file__).resolve().parents[2] / "coordinator" / "bin"


def _default_manifest_path(bin_dir_sibling: Optional[Path]) -> Path:
    """Resolve `doctor-probes.toml`'s location when `DOCTOR_PROBES_MANIFEST`
    is unset.

    Precedence (unchanged override ladder — only the terminal default moved):
      1. `COORDINATOR_BIN_ROOT` test-isolation override, if set — already
         folded into `bin_dir_sibling` by `_doe_coordinator_root()` (its own
         rung 1); honored here exactly as it was before this fix, so existing
         test-isolation usage is unaffected.
      2. Default: claude-klabauter's own `coordinator/bin/doctor-probes.toml`
         (`_claude_klabauter_bin_root()`) — NOT the DoE-root ladder
         (`coordinator_doe_root()` / REPO_DOE_CLAUDE / machine-local
         `repos.doe_claude`), which is what `bin_dir_sibling` resolves to for
         every OTHER caller of `_doe_coordinator_root()` (P-9/P-11/P-12/P-13's
         still-DoE-owned sibling scripts, left untouched by this fix).

    Bug this closes: prior to this fix, the manifest's non-override default
    was `bin_dir_sibling / "doctor-probes.toml"` — i.e. the DoE-claude clone's
    `coordinator/bin/`. That directory stopped housing the manifest after the
    b644d5a9 migration moved the executable surface (including
    doctor-probes.toml) into claude-klabauter; every triage/full run with no
    `DOCTOR_PROBES_MANIFEST` override and no `COORDINATOR_BIN_ROOT` override
    hard-failed at the selector with "manifest not found", before a single
    probe fired.
    """
    if bin_dir_sibling is not None and os.environ.get("COORDINATOR_BIN_ROOT"):
        return bin_dir_sibling / "doctor-probes.toml"
    return _claude_klabauter_bin_root() / "doctor-probes.toml"


# Manifest ids with no probe body in this module. Until 2026-08-15 these were
# simply absent from the dispatch list below, so `--full` reported GREEN having
# never evaluated them -- a fabricated pass, which is the one thing
# docs/wiki/doctor-probe-design.md § `inconclusive` Is a First-Class Probe Status
# forbids. They now report `inconclusive` instead, and the honest amber stands
# until somebody writes the bodies.
#
# The ledger is EMPTY, and that is the intended terminal state. P-16 (hardware SSOT)
# arrived as a byte-parity skew inherited from the bash port; P-20/P-21/P-22 were
# added to the manifest with no matching wiring here and went unnoticed because
# nothing compared the two sets. All four now have bodies and have been dropped from
# this list.
#
# This list is a debt ledger, not an escape hatch: shrink it by wiring a probe,
# never grow it to silence a new one. An entry added here is a promise to a reader
# that the amber is honest — it is never the cheaper way past a red.
# Guard: coordinator/tests/test_doctor_manifest_ssot.py
_UNWIRED_PROBE_IDS: tuple = ()


def _is_runnable_file(path: Path) -> bool:
    """Is `path` a file this platform's exec loader can be handed?

    os.access(X_OK) is NOT meaningful on Windows — it degrades to F_OK and
    returns True for every existing file, so a POSIX-only X_OK gate is at best
    a no-op there and at worst (for paths that ARE readable but not
    CreateProcess-able) a false precondition. Gate on is_file() on nt and keep
    the X_OK check on POSIX where it carries real information.

    Mirrors coordinator_core.install.substrate's Step C10a-2 gate — keep the
    two in sync.
    """
    return path.is_file() and (os.name == "nt" or is_executable(path))


def _resolve_cli(sh_bin: Path, bin_dir: Path, name: str) -> Optional[str]:
    """Resolve a resolver CLI (machine-local / claude-home) by the same
    settings-home-bin-first, compat-forwarder-second, PATH-last order as the
    bash oracle's ml_cmd/ch_cmd resolution.

    Windows: the substrate deliberately delivers a `<name>.cmd` alongside the
    extension-less POSIX wrapper (see coordinator_core.install.substrate Step
    C10a-2) because CreateProcess cannot exec an extension-less shebang script.
    Prefer the .cmd when present. Keyed on os.name, NOT on whether the calling
    shell is Git Bash: the constraint is the OS exec loader, which applies
    under Git Bash just the same.
    """
    for candidate_dir in (sh_bin, bin_dir):
        if os.name == "nt":
            cmd_candidate = candidate_dir / f"{name}.cmd"
            if cmd_candidate.is_file():
                return str(cmd_candidate)
        candidate = candidate_dir / name
        if _is_runnable_file(candidate):
            return str(candidate)
    return shutil.which(name)


def _registry_keys(ml_dir: Path) -> Optional[List[str]]:
    """The machine-local registry's flattened dotted keys, read once per run.

    Delegates to `machine_resolver.merged_flat_registry()` -- the tree's one
    registry.local.toml-over-registry.toml merge, already
    `MACHINE_LOCAL_REGISTRY_DIR`-aware -- rather than parsing the TOML a
    second way here, and rather than spawning the `machine-local` CLI to ask
    the same files the same question.

    Returns None (distinct from an empty list) when neither registry file is
    present under `ml_dir`, so a caller can tell "the registry declares no
    such key" from "there is no registry to ask" and leave the second case to
    P-1/P-2, whose finding it is.
    """
    if not any((ml_dir / name).is_file() for name in ("registry.toml", "registry.local.toml")):
        return None
    return sorted(merged_flat_registry())


def _inconclusive(probe_id: str, detail: str) -> List[ProbeNote]:
    """"I could not run this check" — NOT "the thing I check is broken".

    Doctrine: docs/wiki/doctor-probe-design.md § `inconclusive` Is a
    First-Class Probe Status — "when a probe cannot reach the state it checks,
    it emits `inconclusive` with the reason — never a fabricated pass or fail."

    Carried on the `amber` severity because the sentinel JSON schema
    (red_probes / amber_probes / advisory_notes) is a load-bearing cross-plugin
    contract that cannot grow a fourth bucket here; the `inconclusive(...)`
    message prefix is the honest-status vocabulary, matching what
    probe-onboarding-currency.sh already emits and what probe_p13 already maps
    to amber. Amber (not red) is the right verdict weight: an unrunnable probe
    is a warning about the probe, not a verdict about the substrate.
    """
    return [ProbeNote(probe_id, "amber", f"inconclusive({detail})")]


def _exec_detail(exc: BaseException) -> str:
    """Render an exec failure for an inconclusive() message — the observed
    error, never an invented cause."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"exec: timed out after {exc.timeout}s"
    return f"exec: {type(exc).__name__}: {exc}"


class _NativeCallFailed(Exception):
    """Raised by _call_native_main when a ported probe module's main() raises
    (or exits with a non-int SystemExit code). Callers route this to the same
    _inconclusive(...) path the old subprocess-exec-failure branch used — a
    raising module must never crash the probe suite."""


def _call_native_main(fn, argv: List[str], **kwargs) -> Tuple[int, str, str]:
    """Call a ported probe module's main(argv, **kwargs) IN-PROCESS (no
    subprocess, no bash spawn — the whole point of this port). Captures
    stdout and stderr into separate buffers so the module's own print()s
    never reach the sentinel's own output stream, and returns both texts so
    callers that consumed a subprocess's merged stdout+stderr (P-18's
    diagnostic first-line) or plain stdout (P-13's ndjson-shaped status line)
    can reconstruct whichever shape they need.

    Returns (rc, stdout_text, stderr_text). A SystemExit is caught and mapped
    to an rc inline (never re-raised as _NativeCallFailed): an int code passes
    through as-is, a bare `sys.exit()` / `SystemExit(None)` — the conventional
    Python/POSIX success spelling — maps to 0, and any other non-int code
    (e.g. a string message) maps to 1. _NativeCallFailed is raised only for a
    non-SystemExit exception escaping fn().
    """
    out_buf, err_buf = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            rc = fn(argv, **kwargs)
    except SystemExit as exc:
        if exc.code is None:
            rc = 0
        elif isinstance(exc.code, int):
            rc = exc.code
        else:
            rc = 1
    except Exception as exc:  # noqa: BLE001 — isolate a raising module; never crash the suite
        raise _NativeCallFailed(f"{type(exc).__name__}: {exc}") from exc
    return rc, out_buf.getvalue(), err_buf.getvalue()


@contextlib.contextmanager
def _temp_env(**overrides):
    """Temporarily set (or, for a None value, unset) env vars for the
    duration of an in-process native probe call, restoring the prior state
    (including "was not set at all", distinct from "was set to empty") on
    exit. Used in place of the subprocess-era `env=dict(os.environ, ...)`
    pattern now that these probes call their target module's main()
    in-process rather than spawning a child with its own environment."""
    _unset = object()
    saved = {k: os.environ.get(k, _unset) for k in overrides}
    try:
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, old in saved.items():
            if old is _unset:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


# ---------------------------------------------------------------------------
# Selector invocation (in-process; captures selector stderr for fail-loud
# propagation with the exact "[coordinator-doctor] selector error: ..." prefix
# the bash oracle's subprocess-capture path emitted)
# ---------------------------------------------------------------------------


def _select_active_probes(
    mode: str, arg: Optional[str], manifest_path: Optional[Path]
) -> Tuple[Optional[List[str]], int, str]:
    """Returns (active_ids_or_None, exit_code, captured_stderr_text)."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            ids = resolve_active_probes(mode, arg, manifest_path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        return None, code, buf.getvalue().strip()
    return ids, 0, ""


# ---------------------------------------------------------------------------
# Python interpreter resolution + identity
# ---------------------------------------------------------------------------


def _py_ident(py_bin: str, py_args: List[str]) -> str:
    """Deliberate isolation boundary, not a candidate for an in-process
    import — ``py_bin`` names a RESOLVED candidate python, not
    ``sys.executable``; identifying it requires asking that candidate
    interpreter for its own version, which is by construction not
    importable in-process. See
    ``state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md``
    for the recorded verdict."""
    py_path = shutil.which(py_bin) or py_bin
    try:
        from coordinator_core.win_portability import no_console_creationflags

        proc = subprocess.run(
            [py_bin, *py_args, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=_PROBE_SPAWN_BUDGET_SECS,
            **no_console_creationflags(),
        )
        lines = (proc.stdout or "").splitlines()
        version_line = lines[0] if lines else ""
    except (OSError, subprocess.TimeoutExpired):
        version_line = ""
    return f"{version_line} at {py_path}"


def _whoami_importable(py_bin: str, py_args: List[str]) -> Optional[bool]:
    """Deliberate isolation boundary, not a candidate for an in-process
    import — ``py_bin`` is a resolved candidate interpreter, distinct from
    the one running this module, and import-state isolation is also
    required: a failed ``import coordinator_whoami`` under a candidate must
    not land in this process's own ``sys.modules``. See
    ``state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md``
    for the recorded verdict.

    Three-valued, not two: True/False are OBSERVED import outcomes, and None
    means the check could not be run at all (the candidate interpreter never
    exec'd, or exceeded the spawn budget). Collapsing None into False — which
    this returned before the budget clamp made the timeout branch genuinely
    reachable — makes P-5 assert RED "coordinator_whoami not importable"
    about an import it never performed, the fabricated verdict
    ``docs/wiki/doctor-probe-design.md`` § `inconclusive` forbids."""
    try:
        from coordinator_core.win_portability import no_console_creationflags

        proc = subprocess.run(
            [py_bin, *py_args, "-c", "import coordinator_whoami"],
            capture_output=True,
            timeout=_PROBE_SPAWN_BUDGET_SECS,
            **no_console_creationflags(),
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return None


class _Lazy:
    """Compute-once memo — mirrors the bash oracle's _whoami_checked/_whoami_ok
    caching (import check runs at most once even when P-5/P-6/P-6s are all active)."""

    def __init__(self, fn):
        self._fn = fn
        self._done = False
        self._val = None

    def get(self):
        if not self._done:
            self._val = self._fn()
            self._done = True
        return self._val


# ---------------------------------------------------------------------------
# prereq_probe.sh bridge (P-15 / P-17)
# ---------------------------------------------------------------------------


_PREREQ_PROBE_NATIVE_DISPATCH = {
    # func_name (the bash oracle's function name, still the caller-facing
    # vocabulary probe_p15/probe_p17 pass in) -> the native
    # coordinator_core.install.prereq_probe callable it now dispatches to,
    # in-process, with no bash spawn. probe_all() returns List[str] (one
    # NDJSON line per probe, newline-terminated); probe_shell_login_env()
    # returns a single NDJSON line (str) — _run_prereq_probe_function joins
    # the former and passes the latter through, so both collapse to the same
    # (state, ndjson: str) contract callers already expect.
    "_co_prereq_probe_all": "probe_all",
    "_co_probe_shell_login_env": "probe_shell_login_env",
}


def _run_prereq_probe_function(scripts_lib_dir: Path, func_name: str) -> Tuple[str, str]:
    """Call the native prereq_probe port in-process for func_name.

    Returns (state, ndjson) where state in {"missing", "source_failed", "ok"} —
    the exact contract the retired `bash -c 'source prereq_probe.sh; func_name'`
    bridge returned, preserved byte-for-byte for probe_p15/probe_p17.

    DR-079 repoint (2026-07-21): func_name is dispatched to the corresponding
    coordinator_core.install.prereq_probe module-level callable instead of
    shelling out to DoE's bash SSOT. "missing" now means "the native
    module/entrypoint is unavailable" (import failure, or an unrecognized
    func_name) rather than "the bash lib file is absent at scripts_lib_dir" —
    scripts_lib_dir is accepted for call-site/signature compatibility with
    probe_p15/probe_p17 (whose OWN `if scripts_lib_dir is None: return []`
    gate is untouched, preserving the pre-repoint skip-when-unresolved
    behavior) but is no longer consulted here: the native callable runs
    in-process regardless of where (or whether) DoE's coordinator_root
    resolved. "source_failed" now covers a raising native callable — same
    "never let a probe crash the suite" contract _call_native_main enforces
    elsewhere in this module (see also probe_p19's identical
    ImportError/Exception-to-fallback-state remap, the Staff Engineer F6).
    """
    del scripts_lib_dir
    target = _PREREQ_PROBE_NATIVE_DISPATCH.get(func_name)
    if target is None:
        return "missing", ""
    try:
        from coordinator_core.install import prereq_probe

        native_fn = getattr(prereq_probe, target)
    except (ImportError, AttributeError):
        return "missing", ""
    try:
        result = native_fn()
    except Exception as exc:  # noqa: BLE001 — isolate a raising probe; never crash the suite
        # Review: code-reviewer (nit) — surface the exception type/message in
        # the amber note instead of a bare "raised" so a future
        # signature-mismatch coding defect (as opposed to a genuine
        # environment-probe failure) is at least visible in the sentinel
        # JSON, mirroring _NativeCallFailed's f"{type(exc).__name__}: {exc}"
        # convention elsewhere in this module.
        return "source_failed", f"{type(exc).__name__}: {exc}"
    ndjson = "".join(result) if isinstance(result, list) else result
    return "ok", ndjson or ""


# ---------------------------------------------------------------------------
# Probe bodies — each returns 0 or more ProbeNote. Logic preserved verbatim
# from the bash oracle (docs/wiki/coordinator-doctor.md severity rules).
# ---------------------------------------------------------------------------


def probe_p1(ml_dir: Path) -> List[ProbeNote]:
    if not ml_dir.is_dir():
        return [ProbeNote("P-1", "red", "machine-local/ absent — run /coordinator:install Phase 3")]
    return []


def probe_p2(registry_path: Path, ml_dir: Path, py_bin: str, py_args: List[str]) -> List[ProbeNote]:
    """Deliberate isolation boundary, not a candidate for an in-process
    import — validates the registry against ``py_bin``, a resolved
    candidate interpreter distinct from the one running this module, so
    the health probe reflects that interpreter's own environment (its
    ``tomllib`` availability included), not this process's. See
    ``state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md``
    for the recorded verdict."""
    if registry_path.is_file():
        script = (
            "import os, pathlib\n"
            "try:\n"
            "    import tomllib\n"
            "except ImportError:\n"
            "    import sys; sys.exit(2)\n"
            "d = tomllib.loads(pathlib.Path(os.environ['DOCTOR_REG']).read_text())\n"
            "assert d.get('schema') == 1, 'schema mismatch'\n"
        )
        env = dict(os.environ)
        env["DOCTOR_REG"] = str(registry_path)
        try:
            from coordinator_core.win_portability import no_console_creationflags

            proc = subprocess.run(
                [py_bin, *py_args, "-c", script],
                env=env,
                capture_output=True,
                text=True,
                timeout=_PROBE_SPAWN_BUDGET_SECS,
                **no_console_creationflags(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            # Could not run the validator at all — that says nothing about
            # registry.toml. Do not assert a cause we did not observe.
            return _inconclusive(
                "P-2", f"could not run tomllib validator under {py_bin} — {_exec_detail(exc)}"
            )
        if proc.returncode == 2:
            return [
                ProbeNote(
                    "P-2", "amber", "Python < 3.11 lacks tomllib — cannot validate registry.toml"
                )
            ]
        if proc.returncode != 0:
            return [ProbeNote("P-2", "red", "registry.toml unparseable or wrong schema")]
        return []
    if ml_dir.is_dir():
        return [ProbeNote("P-2", "red", "registry.toml missing from machine-local/")]
    return []


def probe_p3(registry_keys: Optional[Sequence[str]]) -> List[ProbeNote]:
    """Is any `repos.*` key populated in the machine-local registry?

    Reads the registry IN-PROCESS. This probe's subject is the registry's
    DATA, not the resolver CLI that happens to be one way of reading it, so
    it no longer spawns `machine-local keys` -- a whole Python interpreter
    cold start to read a TOML file this process can parse for free. P-4 keeps
    the one spawn the suite still needs, because the CLI itself is what P-4
    is about; the two questions were being answered by two identical spawns
    of the same command in the same run.

    `registry_keys` is the flattened dotted-key set `_run` resolves once via
    `machine_resolver.merged_flat_registry()` (registry.local.toml over
    registry.toml, `MACHINE_LOCAL_REGISTRY_DIR`-aware -- the same directory
    ladder P-1/P-2 probe). `None` means the registry files are absent or
    unreadable, which is P-1's and P-2's finding to report, not this one's:
    claiming "no repos.* keys populated" about a registry nobody could read
    would be a verdict this probe did not observe.

    Negative-spec: do NOT re-add a `machine-local keys` subprocess here. The
    CLI reads the same two files through the same precedence; a spawn buys
    no information and costs an interpreter start.
    """
    if registry_keys is None:
        return []
    if not any(key.startswith("repos.") for key in registry_keys):
        return [
            ProbeNote(
                "P-3",
                "amber",
                "no repos.* keys populated — run machine-local set repos.<name> <path>",
            )
        ]
    return []


def probe_p4(ml_cmd: Optional[str], sh_bin: Path) -> List[ProbeNote]:
    if ml_cmd:
        try:
            from coordinator_core.win_portability import no_console_creationflags

            proc = subprocess.run(
                [ml_cmd, "keys"], capture_output=True, timeout=_PROBE_SPAWN_BUDGET_SECS,
                **no_console_creationflags(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            # Previously this claimed "registry.toml unparseable" — a cause the
            # probe never observed. An exec failure means the probe could not
            # run, full stop (P-2 parses registry.toml independently).
            return _inconclusive(
                "P-4", f"could not run {ml_cmd} keys — {_exec_detail(exc)}"
            )
        if proc.returncode != 0:
            return [
                ProbeNote(
                    "P-4",
                    "red",
                    f"machine-local CLI exited {proc.returncode} — the registry is "
                    f"unreadable by the CLI; re-run /coordinator:install Phase 3 (primary "
                    f"resolver: {sh_bin})",
                )
            ]
        return []
    return [
        ProbeNote(
            "P-4",
            "red",
            f"machine-local CLI not found on PATH — re-run /coordinator:install Phase 3 "
            f"(on macOS, {sh_bin} is reached via the coordinator/bin forwarder, not PATH "
            "directly)",
        )
    ]


def probe_p5(whoami_ok: Optional[bool], py_ident: str, sh: Path) -> List[ProbeNote]:
    if whoami_ok:
        return []
    if whoami_ok is None:
        return _inconclusive(
            "P-5", f"could not run the import check under {py_ident} — the candidate "
            "interpreter did not exec or exceeded the probe spawn budget"
        )
    return [
        ProbeNote(
            "P-5",
            "red",
            f"coordinator_whoami not importable under {py_ident} — venv is now at "
            f"{sh}/.coordinator-venv/ (run bin/ensure-coordinator-venv.sh to rebuild); "
            "editable installs are per-interpreter — install under THIS interpreter, or "
            "set COORDINATOR_PYTHON to the one that has it",
        )
    ]


#: Envelope modules under `coordinator_whoami` that are NOT per-plugin
#: implementations: the generic surfaces probed by their own probes (P-6s runs
#: `session`; the machine envelope runs `machine`). Safe to name literally --
#: neither carries a repo codename, which is precisely why both survived the
#: publish transform that broke the one below.
_WHOAMI_GENERIC_ENVELOPE_MODULES = frozenset({"session", "machine"})


def _whoami_plugin_modules(sh: Path) -> List[str]:
    """The per-plugin `coordinator_whoami` subpackages installed on this
    machine, DISCOVERED FROM DISK rather than named in source.

    WHY THIS IS NOT A LITERAL. P-6 used to spawn `-m
    coordinator_whoami.<repo-name>` with the repo name written out here.
    `coordinator_whoami` is installed under settings-home, OUTSIDE the engine
    tree, so publish's depersonalization rewrote our reference to it and
    renamed nothing at the other end -- the mirror every box resolves spawned
    an import that cannot succeed and reported it as "module crash or missing
    CLI". The engine's internal renames stay self-consistent because files move
    with their references; this one reached across a package boundary. A
    depersonalized name must never reach an identifier that has to resolve, so
    the name is now read from the installed package at runtime and appears in
    no source byte the transform can touch.

    It also matches what the package documents: per-plugin implementations live
    in subpackages and a second plugin is expected to "author a sibling
    subpackage". A hardcoded name could never have probed that sibling; this
    enumerates it.

    Gates on `__main__.py`, which is exactly what `-m` requires -- a directory
    without one is not runnable and would fail for a reason that says nothing
    about envelope health. Zero-spawn (one `iterdir`), and an unreadable or
    absent package degrades to `[]`, which the caller reports as inconclusive
    rather than a fabricated pass.
    """
    pkg = sh / "coordinator-whoami" / "coordinator_whoami"
    try:
        names = sorted(entry.name for entry in pkg.iterdir() if entry.is_dir())
    except OSError:
        return []
    return [
        name
        for name in names
        if not name.startswith("_")
        and name not in _WHOAMI_GENERIC_ENVELOPE_MODULES
        and (pkg / name / "__main__.py").is_file()
    ]


def probe_p6(
    whoami_ok: Optional[bool], py_bin: str, py_args: List[str], py_ident: str, sh: Path
) -> List[ProbeNote]:
    """Deliberate isolation boundary, not a candidate for an in-process
    import — runs each per-plugin ``coordinator_whoami`` envelope module under
    ``py_bin``, a resolved candidate interpreter distinct from the one running
    this module, so the probe reflects that interpreter's own environment, not
    this process's. See
    ``state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md``
    for the recorded verdict.

    Which modules those are is discovered, not declared — see
    ``_whoami_plugin_modules`` for why a literal name here was break-class on
    the published engine."""
    if whoami_ok is None:
        return _inconclusive("P-6", f"P-5's import check could not run under {py_ident}")
    if not whoami_ok:
        return [
            ProbeNote(
                "P-6",
                "red",
                f"coordinator_whoami not importable under {py_ident} — cannot probe "
                "per-plugin envelopes (fix P-5 first)",
            )
        ]
    modules = _whoami_plugin_modules(sh)
    if not modules:
        return _inconclusive(
            "P-6",
            f"no per-plugin coordinator_whoami envelope module found under {sh}"
            "/coordinator-whoami — nothing to probe",
        )

    notes: List[ProbeNote] = []
    for module in modules:
        target = f"coordinator_whoami.{module}"
        try:
            from coordinator_core.win_portability import no_console_creationflags

            proc = subprocess.run(
                [py_bin, *py_args, "-m", target],
                capture_output=True,
                text=True,
                timeout=_PROBE_SPAWN_BUDGET_SECS,
                **no_console_creationflags(),
            )
            out = proc.stdout or ""
        except (OSError, subprocess.TimeoutExpired) as exc:
            notes.extend(
                _inconclusive(
                    "P-6", f"could not run {py_bin} -m {target} — {_exec_detail(exc)}"
                )
            )
            continue
        if not out.strip():
            notes.append(
                ProbeNote(
                    "P-6",
                    "red",
                    f"{target} produced no output — module crash or missing CLI",
                )
            )
            continue
        try:
            d = json.loads(out)
            if not isinstance(d, dict) or d.get("contract_version") != 1:
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            notes.append(
                ProbeNote(
                    "P-6",
                    "red",
                    f"{target} envelope invalid — check registry keys + contract docs",
                )
            )
    return notes


def probe_p6s(
    whoami_ok: Optional[bool], py_bin: str, py_args: List[str], py_ident: str
) -> List[ProbeNote]:
    """Deliberate isolation boundary, not a candidate for an in-process
    import — runs ``coordinator_whoami.session`` under ``py_bin``, a
    resolved candidate interpreter distinct from the one running this
    module, so the probe reflects that interpreter's own environment, not
    this process's. See
    ``state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md``
    for the recorded verdict."""
    if whoami_ok is None:
        return _inconclusive("P-6s", f"P-5's import check could not run under {py_ident}")
    if not whoami_ok:
        return [
            ProbeNote(
                "P-6s",
                "red",
                f"coordinator_whoami not importable under {py_ident} — cannot probe session "
                "envelope (fix P-5 first)",
            )
        ]
    try:
        from coordinator_core.win_portability import no_console_creationflags

        proc = subprocess.run(
            [py_bin, *py_args, "-m", "coordinator_whoami.session"],
            capture_output=True,
            text=True,
            timeout=_PROBE_SPAWN_BUDGET_SECS,
            **no_console_creationflags(),
        )
        out = proc.stdout or ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _inconclusive(
            "P-6s", f"could not run {py_bin} -m coordinator_whoami.session — {_exec_detail(exc)}"
        )
    if not out.strip():
        return [
            ProbeNote(
                "P-6s",
                "red",
                "coordinator_whoami.session produced no output — module crash or missing CLI",
            )
        ]
    try:
        d = json.loads(out)
        if (
            not isinstance(d, dict)
            or d.get("contract_version") != 1
            or d.get("plugin_name") != "coordinator-session"
        ):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        return [
            ProbeNote(
                "P-6s",
                "red",
                "coordinator_whoami.session envelope invalid — expected contract_version=1, "
                "plugin_name=coordinator-session",
            )
        ]
    return []


def probe_p7(claude_home: Path) -> List[ProbeNote]:
    cfg_path = Path(str(claude_home) + ".json")
    mcp_servers: object = {}
    cfg_parse_failed = False
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            mcp_servers = cfg.get("mcpServers") or {}
        except Exception:
            # Review: code-reviewer (P2) — a parse/read failure on an
            # EXISTING config file is a genuinely broken config, not "no
            # MCP servers registered". Surface amber instead of silently
            # falling back to {} (which the isinstance(dict) guard below
            # can never catch, since {} passes the shape check).
            mcp_servers = {}
            cfg_parse_failed = True

    settings_path = claude_home / "settings.json"
    enabled_plugins: object = {}
    settings_parse_failed = False
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            enabled_plugins = settings.get("enabledPlugins") or {}
        except Exception:
            enabled_plugins = {}
            settings_parse_failed = True

    if cfg_parse_failed or settings_parse_failed:
        broken = ", ".join(
            str(p)
            for p, failed in ((cfg_path, cfg_parse_failed), (settings_path, settings_parse_failed))
            if failed
        )
        return [
            ProbeNote(
                "P-7",
                "amber",
                f"failed to read/parse {broken} — cannot verify MCP-plugin registration "
                "(malformed JSON, encoding error, or unreadable file)",
            )
        ]

    try:
        if not isinstance(mcp_servers, dict) or not isinstance(enabled_plugins, dict):
            return [
                ProbeNote(
                    "P-7",
                    "amber",
                    "enabledPlugins or mcpServers has an unexpected (non-object) shape in "
                    "settings.json/.claude.json — cannot verify MCP-plugin registration",
                )
            ]

        missing = []
        for key, is_enabled in enabled_plugins.items():
            if not is_enabled:
                continue
            plugin_name = key.split("@", 1)[0]
            server_key = _MCP_SERVING_PLUGINS.get(plugin_name)
            if server_key and server_key not in mcp_servers:
                missing.append((plugin_name, server_key))

        if missing:
            detail = ", ".join(
                f'{p} (expects mcpServers["{s}"])' for p, s in sorted(set(missing))
            )
            return [
                ProbeNote(
                    "P-7",
                    "amber",
                    f"MCP-serving plugin enabled but absent from ~/.claude.json mcpServers: "
                    f"{detail} — re-run plugin install",
                )
            ]
    except (AttributeError, TypeError):
        return [
            ProbeNote(
                "P-7",
                "amber",
                "enabledPlugins or mcpServers has an unexpected (non-object) shape in "
                "settings.json/.claude.json — cannot verify MCP-plugin registration",
            )
        ]
    return []


def probe_p8(plugins_root: Path) -> List[ProbeNote]:
    count = 0
    if plugins_root.is_dir():
        for entry in plugins_root.iterdir():
            if (entry / "data" / "doctor-last-run.json").is_file():
                count += 1
    if count == 0:
        return [
            ProbeNote(
                "P-8",
                "amber",
                "no prior plugin sentinels found — this sentinel will satisfy P-8 on the "
                "next run",
            )
        ]
    return []


def probe_p9(sh_bin: Path) -> List[ProbeNote]:
    """Verify UE-context plugin overrides (via verify_ue_overrides), always
    in-process — no on-disk sibling-script presence gate (retired; see module
    docstring). `sh_bin` is passed through as `script_dir` purely for
    verify_ue_overrides' own co-located-machine-local-binary fallback rung
    (settings-home bin is the primary resolver location); it is not a
    precondition for making the call."""
    try:
        rc, _, _ = _call_native_main(verify_ue_overrides.main, [], script_dir=str(sh_bin))
    except _NativeCallFailed as exc:
        return _inconclusive("P-9", str(exc))
    if rc != 0:
        return [
            ProbeNote(
                "P-9",
                "amber",
                "verify-ue-overrides.py emitted remediation — run it (no flags) to see "
                "which UE-context settings.json drifted",
            )
        ]
    return []


def probe_p10(ch_cmd: Optional[str], sh_bin: Path) -> List[ProbeNote]:
    if ch_cmd:
        try:
            from coordinator_core.win_portability import no_console_creationflags

            proc = subprocess.run(
                [ch_cmd, "plugins"], capture_output=True, text=True,
                timeout=_PROBE_SPAWN_BUDGET_SECS,
                **no_console_creationflags(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _inconclusive(
                "P-10", f"could not run {ch_cmd} plugins — {_exec_detail(exc)}"
            )
        out = (proc.stdout or "").strip() if proc.returncode == 0 else ""
        if not out or not Path(out).is_dir():
            return [
                ProbeNote(
                    "P-10",
                    "red",
                    "claude-home plugins did not resolve to a directory — resolver drift",
                )
            ]
        return []
    return [
        ProbeNote(
            "P-10",
            "red",
            f"claude-home resolver not found — re-run /coordinator:install Phase 3 (on "
            f"macOS, {sh_bin} is the primary resolver location reached via the "
            "coordinator/bin forwarder)",
        )
    ]


def probe_p11(plugins_root: Path, coordinator_root: Optional[Path] = None) -> List[ProbeNote]:
    """Verify templates/setup drift (via verify_templates_setup_sync), always
    in-process — no on-disk sibling-script presence gate (retired; see module
    docstring).

    Two-rung CLAUDE_PLUGIN_ROOT choice (mirrors probe_p13's
    _currency_plugin_root): on a source-is-live dev-clone host,
    <plugins_root>/coordinator-claude/coordinator/ holds only data/ — no
    bin/ — so the marketplace-shaped path is the wrong plugin root there.
    Prefer coordinator_root (the DoE clone _doe_coordinator_root() already
    resolves) when given; otherwise fall back to the historical
    plugins_root-derived path so the marketplace case is unaffected. This is
    a root-selection preference, not a precondition for making the call —
    the native module is always invoked either way.
    """
    plugin_root = _doe_payload_root(coordinator_root, plugins_root, "templates/setup")
    try:
        with _temp_env(CLAUDE_PLUGIN_ROOT=str(plugin_root)):
            rc, _, _ = _call_native_main(verify_templates_setup_sync.main, [])
    except _NativeCallFailed as exc:
        return _inconclusive("P-11", str(exc))
    if rc != 0:
        return [
            ProbeNote(
                "P-11",
                "amber",
                "templates/setup drift detected — run verify-templates-setup-sync.py (no "
                "flags, inspect-only) to see which files. Read NOT_PRESENT rows as benign "
                "(neither side exists yet); only MISMATCH rows are drift. Since the "
                "runtime-root resolver prefers a resolved DoE clone over the shared "
                "~/.claude/setup/ copy (see coordinator_percolate_runtime_root()), a "
                "MISMATCH does not reach the resolved truth on a machine with a DoE clone "
                "— real, but not urgent. RE-RUNNING THE INSTALLER WILL NOT CLEAR THIS, and "
                "that is by design, not a bug: substrate.py's percolation step classifies a "
                "drifted destination as operator-customized and PRESERVES it, logging "
                "`[machine-local] operator-customized <file> preserved; template at <path> "
                "for diff reference`. It cannot tell a deliberate local edit from stale "
                "content, so it refuses to guess. Verified 2026-08-12 by perturbing the "
                "live file, running install-maximalist.py to completion (rc=0), and "
                "observing the drift survive. Do not `cp` over the destination by hand "
                "either: it is foreign-repo-tracked (the ~/.claude meta-repo, "
                "coordinator-claude plane), so an unattributed overwrite travels to every "
                "machine that pulls. Resolve it as a decision, not a sync: diff the live "
                "copy against the template the log line names, decide which side is "
                "authoritative, and re-pin deliberately with a commit that says why — or "
                "route it to the coordinator-claude owner via "
                "coordinator/bin/cross-repo-memo.py if the call is theirs.",
            )
        ]
    return []


def probe_p12(sibling_bin_dir: Optional[Path], claude_home: Path) -> List[ProbeNote]:
    if sibling_bin_dir is None:
        return []
    try:
        # Deferred import + broad except: an ImportError (corrupted/partial
        # install), manifest-not-locatable, or any other failure maps to the
        # existing graceful-absent [] path -- this probe must never raise
        # (AC D2 -- native dry-run replaces the bash-script-absent check).
        from coordinator_core.install.scaffold_structure import scaffold_canonical_structure

        result = scaffold_canonical_structure(claude_home, sibling_bin_dir.parent, dry_run=True)
    except Exception:  # noqa: BLE001 — advisory probe: never raise (graceful-absent, AC D2)
        return []
    if result.would_create_count() >= 1:
        return [
            ProbeNote(
                "P-12",
                "amber",
                f"canonical structure incomplete at {claude_home} — run "
                "python3 -c \"from coordinator_core.install.scaffold_structure import "
                f"scaffold_canonical_structure; scaffold_canonical_structure('{claude_home}', "
                "'<coordinator-root>')\" to restore; or re-run /coordinator:install",
            )
        ]
    return []


def _currency_plugin_root(coordinator_root: Optional[Path], plugins_root: Path) -> Path:
    """Resolve COORDINATOR_CURRENCY_PLUGIN_ROOT for P-13.

    The probe needs the directory holding the actual coordinator plugin payload
    — specifically the `coordinator-schema-version` file it diffs the repo's
    stamp against. On a DoE dev-clone host the marketplace-shaped path
    <plugins_root>/coordinator-claude/coordinator/ holds only data/; the live
    payload is the DoE clone that _doe_coordinator_root() already resolves (and
    that main() already hands to P-19).

    Fallback chain rather than a straight swap: the DoE-clone value is verified
    correct for the dev-clone layout but NOT for a marketplace install, where
    the plugins_root derivation may be the right answer. Prefer the DoE root
    only when it actually carries the schema-version file; otherwise keep the
    historical plugins_root path so the marketplace case does not regress.
    """
    return _doe_payload_root(coordinator_root, plugins_root, "coordinator-schema-version")


def _doe_payload_root(
    coordinator_root: Optional[Path], plugins_root: Path, marker: str
) -> Path:
    """Resolve the coordinator root that actually carries `marker`.

    Shared by P-11 (marker `templates/setup`) and P-13 (marker
    `coordinator-schema-version`). Both probes need the DoE-side payload, and
    both were reaching for it the same broken way.

    Three rungs, marker-verified rather than assumed:

    1. `coordinator_root` when it carries the marker. On a marketplace install
       this is the right answer and stays first.
    2. The DoE-root ladder (`coordinator_doe_root()`), when THAT carries it.
       This is the rung the CLI path needs and did not have: `coordinator_root`
       arrives from `_doe_coordinator_root()`, whose first rung is
       COORDINATOR_BIN_ROOT — which the CLI entry sets to CLAUDE-KLABAUTER's bin. So the
       CLI hands these probes `<claude-klabauter>/coordinator`, which carries neither
       marker: `coordinator-schema-version` stayed in DoE when the probe
       migrated here (see coordinator/bin/probe-onboarding-currency.py's
       plugin-root note), and `templates/setup` is DoE-owned outright.
    3. The historical marketplace path, unchanged, so that case cannot regress.

    Why this matters beyond a wrong path: on a live-source install the
    marketplace directory holds only `data/`, so falling through to rung 3 made
    P-13 report `inconclusive(schema constant unreadable)` and P-11 report
    templates/setup DRIFT — each naming a remedy for a condition that was not
    the real one. A probe that misdiagnoses is worse than one that fails: P-11's
    remedy text talks the reader out of urgency ("largely defanged"), and P-13's
    pointed at a file the operator was never going to find.
    """
    if coordinator_root is not None and (coordinator_root / marker).exists():
        return coordinator_root
    try:
        doe = coordinator_doe_root()
        if doe:
            candidate = normalize_native_path(str(doe)) / "coordinator"
            if (candidate / marker).exists():
                return candidate
    except Exception:
        # Never raise from a probe's root resolution — fall through to the
        # historical path and let the probe report its own honest status.
        pass
    return plugins_root / "coordinator-claude" / "coordinator"


def probe_p13(
    claude_home: Path,
    plugins_root: Path,
    coordinator_root: Optional[Path] = None,
) -> List[ProbeNote]:
    """Verify onboarding-stamp currency (via probe_onboarding_currency), always
    in-process — no on-disk sibling-script presence gate (retired; see module
    docstring)."""
    env_overrides = {
        "COORDINATOR_CURRENCY_REPO_ROOT": str(claude_home),
        "COORDINATOR_CURRENCY_PLUGIN_ROOT": str(
            _currency_plugin_root(coordinator_root, plugins_root)
        ),
    }
    try:
        with _temp_env(**env_overrides):
            rc, stdout_text, _ = _call_native_main(probe_onboarding_currency.main, [])
    except _NativeCallFailed as exc:
        return _inconclusive("P-13", str(exc))
    out = stdout_text.strip()

    if out in ("current", "source_is_live"):
        return []
    if out.startswith("unstamped"):
        return [
            ProbeNote(
                "P-13",
                "amber",
                "repo predates onboarding currency feature — run /repo-setup to stamp",
            )
        ]
    if out.startswith("drift"):
        return [
            ProbeNote(
                "P-13", "amber", f"onboarding stamp is stale: {out} — re-run /repo-setup to refresh"
            )
        ]
    if out.startswith("inconclusive"):
        return [ProbeNote("P-13", "amber", f"onboarding currency check inconclusive: {out}")]
    if out == "":
        return [
            ProbeNote(
                "P-13",
                "amber",
                "onboarding currency probe produced no output — check "
                "probe-onboarding-currency.py",
            )
        ]
    return [ProbeNote("P-13", "amber", f"onboarding currency probe returned unexpected status: {out}")]


# Review: code-reviewer (F1) — operator-facing note on the DR-079 semantic
# change: before the repoint, P-15/P-17 "missing" fired whenever DoE's
# scripts/lib/prereq_probe.sh was absent at scripts_lib_dir (silent skip,
# []). After the repoint, that file's on-disk presence is no longer
# consulted at all — "missing" now fires only if the native
# coordinator_core.install.prereq_probe module fails to import, which
# essentially never happens. Net effect: a machine where coordinator_root
# resolves but the bash sibling file was absent can newly see a RED/AMBER
# P-15/P-17 finding after upgrading through this repoint, with zero change
# in actual prerequisite state. This is intended (the old behavior masked
# genuinely-checkable state) — if you land here from a fresh post-pull RED,
# it is very likely this, not a real regression.
def probe_p15(scripts_lib_dir: Optional[Path]) -> List[ProbeNote]:
    if scripts_lib_dir is None:
        return []
    state, ndjson = _run_prereq_probe_function(scripts_lib_dir, "_co_prereq_probe_all")
    if state == "missing":
        return []
    if state == "source_failed":
        detail = f" ({ndjson})" if ndjson else ""
        return [
            ProbeNote(
                "P-15",
                "amber",
                f"coordinator_core.install.prereq_probe.probe_all() raised{detail} — inconclusive; "
                "re-run /coordinator:install",
            )
        ]
    if not ndjson.strip():
        return [
            ProbeNote(
                "P-15", "red", "prereq_probe_all emitted no output — check coordinator_core.install.prereq_probe"
            )
        ]
    hard_fails: List[str] = []
    for row in ndjson.splitlines():
        row = row.strip()
        if not row:
            continue
        try:
            rec = json.loads(row)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"[{_PROG}] P-15: unparsable prereq_probe_all row, skipping: {exc}", file=sys.stderr)
            continue
        if not isinstance(rec, dict):
            continue
        name, status, sev = rec.get("name"), rec.get("status"), rec.get("severity")
        if not name or not status or not sev:
            continue
        if status == "fail" and sev == "hard":
            hard_fails.append(name)
    if hard_fails:
        return [
            ProbeNote(
                "P-15",
                "red",
                f"hard system-prerequisite(s) absent: {', '.join(hard_fails)} — install via "
                "coordinator:install or see remediation",
            )
        ]
    return []


def probe_p17(scripts_lib_dir: Optional[Path]) -> List[ProbeNote]:
    if scripts_lib_dir is None:
        return []
    state, ndjson = _run_prereq_probe_function(scripts_lib_dir, "_co_probe_shell_login_env")
    if state == "missing":
        return []
    if state == "source_failed":
        detail = f" ({ndjson})" if ndjson else ""
        return [
            ProbeNote(
                "P-17",
                "amber",
                f"coordinator_core.install.prereq_probe.probe_shell_login_env() raised{detail} — "
                "inconclusive; re-run /coordinator:install",
            )
        ]
    row = ndjson.strip()
    if not row:
        # Review: code-reviewer (P2) — matches P-15's severity for the
        # identical "sourced fine but no output" shape (red, not amber).
        # The bash oracle's severity table for this case is unavailable to
        # confirm, so pick the fail-loud direction: a probe that produced no
        # output at all is a broken probe script, not a soft advisory.
        return [
            ProbeNote(
                "P-17",
                "red",
                "shell_login_env probe emitted no output — check coordinator_core.install.prereq_probe",
            )
        ]
    try:
        rec = json.loads(row)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[{_PROG}] P-17: unparsable shell_login_env row, treating as no-status: {exc}", file=sys.stderr)
        rec = {}
    status = rec.get("status", "") if isinstance(rec, dict) else ""
    detail = rec.get("detail", "") if isinstance(rec, dict) else ""
    if status == "fail":
        return [
            ProbeNote(
                "P-17", "red", f"bash login-shell orphan: {detail or 'orphaned ~/.local/bin or claude not found'}"
            )
        ]
    return []


def probe_p14(ml_cmd: Optional[str], ch_cmd: Optional[str], sh_bin: Path) -> List[ProbeNote]:
    """Assert the settings-home forwarder FILE exists for each bare-name
    resolver — not that the bareword resolves on PATH.

    `<settings-home>/bin` is added to shell PATH on Windows only (by design;
    coordinator_core/install/substrate.py Step C10a/C10a-2); on macOS/Linux
    there is no PATH entry and none is meant to exist, so a `shutil.which()`
    bareword-resolution check would red this probe on every healthy POSIX
    machine. `ml_cmd`/`ch_cmd` are already resolved by `_resolve_cli` (the
    same settings-home-bin-first, compat-forwarder-second, PATH-last order
    used by every other probe here) — `None` means the forwarder file itself
    is genuinely missing, which is the actual install defect worth flagging.

    Negative-spec: do NOT swap this back to `shutil.which("machine-local")`
    / `shutil.which("claude-home")` — that was the pre-fix bug (see
    docs/plans § three-surfaces-print-dead-end-remediation fix 2).
    """
    missing = []
    if ml_cmd is None:
        missing.append("machine-local")
    if ch_cmd is None:
        missing.append("claude-home")
    if missing:
        return [
            ProbeNote(
                "P-14",
                "red",
                f"resolver forwarder file(s) missing: {', '.join(missing)} — expected under "
                f"{sh_bin}; re-run /coordinator:install (Phase 3 repairs Windows PATH, all "
                "platforms repair the missing forwarder file)",
            )
        ]
    return []


def probe_p18(original_claude_home: Optional[str]) -> List[ProbeNote]:
    """Verify a single-install singularity (via check_install_singularity),
    always in-process — no on-disk sibling-script presence gate (retired;
    see module docstring). A gate-miss here used to assert "plugin may be
    corrupted; reinstall coordinator" — an unsatisfiable instruction
    inferring corruption from a path the probe merely mislocated. Now the
    only degradation path is `_NativeCallFailed -> _inconclusive`, which
    reports the actual failure the native call raised, never a fabricated
    diagnosis."""
    # Review: code-reviewer (nit) — omit the key entirely when the operator
    # never set CLAUDE_HOME, rather than forcing it to "". Matches the
    # codebase-wide ":-" (empty-or-unset) convention exactly instead of
    # introducing a third "explicitly empty" state that
    # coordinator_core.install.check_install_singularity wasn't confirmed to
    # treat identically to "absent". _temp_env(CLAUDE_HOME=None) pops the key
    # entirely, same as the retired env.pop(...) call.
    try:
        with _temp_env(CLAUDE_HOME=original_claude_home):
            rc, stdout_text, stderr_text = _call_native_main(check_install_singularity.main, [])
    except _NativeCallFailed as exc:
        # Was RED "install singularity check failed" — asserting a split
        # install the probe never observed. Not running != failing.
        return _inconclusive("P-18", str(exc))
    if rc != 0:
        # check_install_singularity writes its FAIL diagnostics to stderr and
        # any INFO lines to stdout (see its own run()) — the old subprocess
        # call merged both streams (stderr=STDOUT) specifically so this
        # first-line extraction would see the actual failure text rather
        # than an unrelated INFO line; concatenate stdout+stderr here to
        # preserve that merged-order semantics.
        first_line = (stdout_text + stderr_text).split("\n", 1)[0]
        return [
            ProbeNote(
                "P-18",
                "red",
                f"install singularity check failed: {first_line} — reconcile to a single "
                "~/.claude install; re-run /coordinator:install",
            )
        ]
    return []


def probe_p19(lib_dir: Optional[Path], coordinator_root: Optional[Path]) -> List[ProbeNote]:
    _absent = [
        ProbeNote(
            "P-19",
            "advisory",
            "release-currency lib absent or failed to source — release currency check skipped",
        )
    ]
    if lib_dir is None:
        return _absent

    root = str(coordinator_root) if coordinator_root else ""
    plugin, repo = "coordinator", "dbc-oduffy/coordinator-claude"
    try:
        # Deferred import: an ImportError here (e.g. a corrupted/partial install)
        # maps to the existing _absent ProbeNote — the native "logic unavailable"
        # remap of the pre-port bash-absent / _SOURCE_FAIL_MARKER path (the Staff Engineer F6).
        from coordinator_core.plugin_health.release_currency import (
            release_currency_probe,
        )

        result = release_currency_probe(plugin, repo, coordinator_root) or "offline"
    except ImportError:
        return _absent
    except Exception:  # noqa: BLE001 — advisory contract: never raise out of the probe (the Staff Engineer F6)
        return _absent

    if result in ("current", "source_is_live"):
        return []
    if result == "offline":
        # "offline" previously collapsed into the same [] as "current", so a
        # doctor-last-run.json consumer could not tell "checked, up to date"
        # from "network unreachable, never checked" — the network-outage case
        # silently masqueraded as currency. Advisory severity (never affects
        # verdict) but now a distinguishable ProbeNote instead of nothing.
        return [
            ProbeNote(
                "P-19",
                "advisory",
                f"{plugin} release-currency check could not reach network — currency unverified",
            )
        ]
    if result.startswith("behind-clone"):
        parts = result.split()
        n = parts[1] if len(parts) > 1 else "?"
        ref = parts[2] if len(parts) > 2 else "?"
        return [
            ProbeNote(
                "P-19",
                "advisory",
                f"{plugin} clone is {n} commits behind {ref} — git pull in {root} to update",
            )
        ]
    if result.startswith("behind"):
        parts = result.split()
        frm = parts[1] if len(parts) > 1 else "?"
        to = parts[2] if len(parts) > 2 else "?"
        return [
            ProbeNote(
                "P-19",
                "advisory",
                f"{plugin} {frm}→{to} behind latest release — run `/coordinator-update` to review",
            )
        ]
    if result.startswith("differs"):
        parts = result.split()
        to = parts[1] if len(parts) > 1 else "?"
        return [
            ProbeNote(
                "P-19",
                "advisory",
                f"{plugin} differs from latest release ({to}) — run `/coordinator-update` to review",
            )
        ]
    return []


def probe_p16(ml_dir: Path) -> List[ProbeNote]:
    """Verify the hardware SSOT (`hardware.cores` / `hardware.ram_gb`) is
    provisioned in machine-local.

    Those two keys are the input every worker-cap calculation in the fleet
    divides by; absent, the callers fall back to a conservative or simply
    wrong parallelism budget, which is why the manifest rates this
    `degraded` rather than `error` — the coordinator's own substrate keeps
    working, the capacity budgets downstream of it do not.

    Reads the `hardware` concern files directly rather than spawning
    `machine-local get`: this probe is `weight = "cheap"` and sits in a
    suite whose whole port exists to stay spawn-free. Precedence mirrors
    every other machine-local read — the per-machine `.local` file wins over
    the tracked baseline, and an empty string is not-found (the baseline
    deliberately declares no keys so "not provisioned yet" cannot be masked
    as "provisioned to empty"; see hardware.toml's own header).

    An unreadable concern file is `inconclusive`, never a fail: this probe
    reports on provisioning, and cannot distinguish an absent key from a
    file it could not parse.
    """
    from coordinator_core.machine_resolver import load_flat_registry_file

    flat: dict = {}
    for fname in ("hardware.toml", "hardware.local.toml"):
        path = ml_dir / fname
        if not path.is_file():
            continue
        try:
            flat.update(load_flat_registry_file(path))
        except Exception as exc:  # noqa: BLE001 — a probe never crashes the suite
            return _inconclusive(
                "P-16", f"could not read {fname} — {type(exc).__name__}: {exc}"
            )

    missing = [
        key
        for key in ("hardware.cores", "hardware.ram_gb")
        if not str(flat.get(key, "")).strip()
    ]
    if missing:
        return [
            ProbeNote(
                "P-16",
                "amber",
                f"hardware SSOT incomplete — {', '.join(missing)} absent from "
                "machine-local; downstream worker-cap budgets fall back to a guess",
            )
        ]
    return []


def probe_p22(claude_klabauter_root: Path) -> List[ProbeNote]:
    """Verify the DoE-tracked `setup/publish-targets.portable` has not
    diverged from the live-install copy (via
    `verify-publish-targets-portable-sync.py`, in-process).

    That checker is a `coordinator/bin/` CLI rather than a
    `coordinator_core.ops` module, so unlike P-9/P-11 it cannot simply be
    imported by name — it is loaded from its path under `claude_klabauter_root` and
    its `main()` called through `_call_native_main`, keeping the no-spawn
    contract the rest of this suite holds. `coordinator/bin` goes onto
    `sys.path` because the CLI bootstraps its own `lib/` sibling from there.

    Exit 0 covers both a clean comparison AND the documented clean skip (no
    live-install copy resolvable on this machine), which is the correct
    verdict for either: there is no drift to report. A non-zero exit is the
    divergence the manifest's remediation describes.

    AN ABSENT CHECKER IS NORMAL ON A PUBLISHED ENGINE, and the manifest ships
    to engines that will never carry it. The checker is DENIED from the mirror
    on purpose (`setup/publish-allowlist-declarations.yaml`: it reads
    `setup/publish-targets.portable` against the live install, "meaningless
    without the publish machinery it audits"). Reporting that as inconclusive
    would put a permanent amber on every published box for a file that is
    absent by design — the same shape as P-9 demanding a repo key most machines
    correctly lack, and the reason this probe is scoped rather than skipped
    wholesale.

    The discriminator is the publish DRIVER, denied from the mirror in the same
    breath as the checker: no driver means this engine cannot publish, so there
    is no publish tree to audit and nothing was missed. A driver present
    WITHOUT its checker is a real gap in an authoring tree, and stays
    inconclusive. Two `is_file` calls, no spawn.
    """
    bin_dir = claude_klabauter_root / "coordinator" / "bin"
    script = bin_dir / "verify-publish-targets-portable-sync.py"
    if not script.is_file():
        if not (bin_dir / "publish.py").is_file():
            return []
        return _inconclusive(
            "P-22",
            f"publish driver is present under {bin_dir} but its "
            "publish-targets checker is not — an authoring tree missing one of a pair",
        )

    bin_dir = str(script.parent)
    added = bin_dir not in sys.path
    if added:
        sys.path.insert(0, bin_dir)
    try:
        spec = importlib.util.spec_from_file_location(
            "_doctor_p22_verify_publish_targets_portable_sync", script
        )
        if spec is None or spec.loader is None:
            return _inconclusive("P-22", f"could not build an import spec for {script}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rc, out, err = _call_native_main(module.main, [])
    except _NativeCallFailed as exc:
        return _inconclusive("P-22", str(exc))
    except Exception as exc:  # noqa: BLE001 — a probe never crashes the suite
        return _inconclusive(
            "P-22", f"could not load {script.name} — {type(exc).__name__}: {exc}"
        )
    finally:
        if added:
            try:
                sys.path.remove(bin_dir)
            except ValueError:
                pass

    if rc != 0:
        detail = (err or out or "").strip().splitlines()
        first = detail[0] if detail else f"exit {rc}"
        return [ProbeNote("P-22", "amber", f"publish-targets.portable diverged — {first}")]
    return []


def probe_p20() -> List[ProbeNote]:
    """Verify the bash resolved FIRST on PATH is version >= 4 (macOS ships 3.2
    at /bin/bash, ahead of any homebrew bash unless PATH is fixed).

    Deliberately judges `shutil.which("bash")` — the same PATH-resolution
    order any shell script or `bash -c` caller gets — never `sys.executable`
    or the interpreter running this probe suite; the whole point is what a
    caller invoking bare `bash` would get, not what this Python process
    happens to run under (see docs/wiki/doctor-probe-design.md § A Probe
    Must Walk the Same Resolution Path as the Runtime Tool It Vouches For).

    No bash on PATH is not the same finding as an OLD bash on PATH: on
    Windows there is legitimately no bash on PATH by default (no coordinator
    tooling requires one there), so absence there is a clean pass, not a
    degradation. On POSIX, bash is near-universally present (it is the
    macOS/Linux default shell binary); an absence there is unusual enough
    that this probe cannot assert either "old" or "modern" and reports
    `inconclusive` rather than fabricating a verdict for a condition (no
    bash at all) the manifest's remediation text does not describe.
    """
    bash_path = shutil.which("bash")
    if bash_path is None:
        if os.name == "nt":
            return []
        return _inconclusive("P-20", "no bash found on PATH — cannot judge its version")
    try:
        from coordinator_core.win_portability import no_console_creationflags

        proc = subprocess.run(
            [bash_path, "-c", 'printf "%s" "${BASH_VERSINFO[0]}"'],
            capture_output=True,
            text=True,
            timeout=_PROBE_SPAWN_BUDGET_SECS,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _inconclusive("P-20", f"could not run {bash_path} -c ... — {_exec_detail(exc)}")
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        return _inconclusive(
            "P-20", f"{bash_path} produced no usable BASH_VERSINFO output (rc={proc.returncode})"
        )
    try:
        major = int(out.splitlines()[0].strip())
    except ValueError:
        return _inconclusive("P-20", f"could not parse bash version output from {bash_path}: {out!r}")
    if major < 4:
        return [
            ProbeNote(
                "P-20",
                "red",
                f"bash resolved first on PATH ({bash_path}) is version {major} (<4) — "
                "install a modern bash (brew install bash) and ensure its install prefix "
                "precedes /bin on PATH; re-run /coordinator:install to re-verify",
            )
        ]
    return []


def probe_p21() -> List[ProbeNote]:
    """Verify a durable coordinator-root pointer exists (DR-072).

    Delegates to `coordinator_core.doe_root_pointer.read_doe_root_pointer_file()`
    — the shared durable-then-legacy file-rungs reader every other `.doe-root`
    pointer consumer in this codebase already uses — rather than re-deriving
    the two candidate paths (`<settings-home>/machine-local/.doe-root`, then
    `${CLAUDE_HOME:-$HOME}/.claude/.doe-root`) here. That helper checks the
    durable location FIRST and returns as soon as it finds non-empty content
    there, so a pass on the durable location structurally never reaches the
    legacy fallback.

    Deliberately the file-rungs-only reader, not the registry-first
    `read_doe_root_pointer()`: this probe's manifest-declared symptom is the
    absence of the pointer FILE itself (DR-072), not the resolved coordinator
    root by any means — `repos.doe_claude` registry presence is a different
    condition covered by P-3/P-4, not this probe's concern.
    """
    if read_doe_root_pointer_file():
        return []
    return [
        ProbeNote(
            "P-21",
            "red",
            "no durable coordinator-root pointer found: checked "
            f"{settings_home() / 'machine-local' / '.doe-root'} (DR-072 durable location) "
            "and the legacy fallback ${CLAUDE_HOME:-$HOME}/.claude/.doe-root — re-run "
            "/coordinator:install to re-seed the durable coordinator-root pointer",
        )
    ]


def _resolve_wrapper_home() -> Path:
    """Resolve the home probe_p23 checks the installed wrapper against,
    through the SAME ladder `_install_claude_doe_wrapper`
    (coordinator_core/install/maximalist.py) uses to pick
    `claude_home_dir` — `require_home()`'s `CLAUDE_HOME -> HOME ->
    USERPROFILE` order — rather than re-deriving a second copy of that
    ladder here.

    Review: code-reviewer (P1) — the prior derivation was
    `Path(os.environ.get("CLAUDE_HOME") or str(Path.home()))`, which falls
    through to `Path.home()` (stdlib `ntpath.expanduser`, USERPROFILE
    before HOME on Windows) once CLAUDE_HOME is unset, instead of
    `require_home`'s HOME-before-USERPROFILE order. Under a git-bash
    session where HOME and USERPROFILE diverge — routine on this box —
    the two ladders resolve to different directories, so the probe was
    checking a path the installer never wrote its wrapper to: a probe
    reporting GREEN about a file nobody installs. A second private copy
    of the installer's ladder is exactly what let this drift in the first
    place, so this reuses `require_home` directly rather than
    re-encoding the order a second time.

    Raises `RequireHomeError` (via `require_home`) when none of the three
    vars is set — `_run_probe`'s catch-all already turns any exception
    raised by a probe body into a red `"<id> probe crashed"` note, which
    is the correct fail-loud outcome here: an environment where the
    installer itself could not have resolved a home is not a case this
    probe can silently degrade past.
    """
    return Path(require_home("plugin_health.sentinel.probe_p23"))


def probe_p23(claude_klabauter_root: Path, wrapper_home: Path, sh_bin: Path) -> List[ProbeNote]:
    """Verify the installed claude-doe wrapper (`~/.local/bin/claude-doe`) has
    not drifted from its repo source, `<claude_klabauter_root>/coordinator/bin/claude-doe.py`.

    Mirrors `_install_claude_doe_wrapper`'s (coordinator_core/install/maximalist.py)
    own POSIX/Windows split. POSIX installs `wrapper_dst` as a SYMLINK onto
    `<settings_bin>/claude-doe` — which `_install_bin_resolvers` regenerates
    from the same `wrapper_src` on every install pass — so staleness there is
    structurally impossible; a symlink resolving to that well-known target (or
    straight to `wrapper_src`) is an unconditional PASS, no content read
    needed. Windows has no equivalent-cost native symlink story, so
    `_install_claude_doe_wrapper` falls back to a `shutil.copy2` byte-copy
    that can silently drift from `wrapper_src` on any edit until the next
    install pass — this probe is that gap's regression net, closed by a
    direct byte comparison against `wrapper_src` (cheap: two file reads, no
    subprocess spawn).

    `claude_klabauter_root` is resolved by the caller from this module's own on-disk
    location (`Path(__file__).resolve().parents[2]`) rather than through the
    `coordinator_engine_root()` machine-local ladder — this probe already
    runs from inside a live claude-klabauter checkout, so a self-relative resolution is
    both cheap (no subprocess) and immune to a stale/unset `repos.claude_klabauter`
    registry entry that would otherwise misreport an unrelated resolver gap as
    wrapper drift.
    """
    wrapper_src = claude_klabauter_root / "coordinator" / "bin" / "claude-doe.py"
    wrapper_dst = wrapper_home / ".local" / "bin" / "claude-doe"
    # Review: code-reviewer (P1) — `wrapper_home` MUST reach here via the
    # installer's own require_home() ladder (see _resolve_wrapper_home
    # below), never a re-derived Path.home(): the installer's
    # CLAUDE_HOME -> HOME -> USERPROFILE order can diverge from
    # Path.home()'s stdlib priority (USERPROFILE before HOME on Windows,
    # ntpath.expanduser) under a git-bash session where HOME and
    # USERPROFILE point at different places — this probe would otherwise
    # check a path the installer never wrote to and report a false GREEN.

    if not wrapper_src.is_file():
        # Not this probe's business — an absent source is a broken install
        # elsewhere (_install_claude_doe_wrapper's own FATAL gate covers it).
        # Degrade gracefully rather than false-flagging the wrapper as drifted.
        return _inconclusive("P-23", f"wrapper source not found: {wrapper_src}")

    if wrapper_dst.is_symlink():
        link_target = sh_bin / "claude-doe"
        try:
            resolved = Path(os.readlink(wrapper_dst))
        except OSError as exc:
            return _inconclusive(
                "P-23", f"could not read symlink {wrapper_dst} — {_exec_detail(exc)}"
            )
        if resolved in (link_target, wrapper_src):
            return []
        return [
            ProbeNote(
                "P-23",
                "amber",
                f"claude-doe wrapper symlink at {wrapper_dst} points to {resolved}, "
                f"not {link_target} — resolver drift",
            )
        ]

    if not wrapper_dst.is_file():
        return [
            ProbeNote(
                "P-23",
                "amber",
                f"claude-doe wrapper not installed at {wrapper_dst}",
            )
        ]

    try:
        src_bytes = wrapper_src.read_bytes()
        dst_bytes = wrapper_dst.read_bytes()
    except OSError as exc:
        return _inconclusive("P-23", f"could not read wrapper files — {_exec_detail(exc)}")

    if src_bytes == dst_bytes:
        return []
    return [
        ProbeNote(
            "P-23",
            "amber",
            f"claude-doe wrapper at {wrapper_dst} has drifted from its repo source "
            f"{wrapper_src} — Windows has no symlink equivalent, so edits to "
            "claude-doe.py are inert until the wrapper is re-published (Step 3.5b, "
            "python scripts/setup.py)",
        )
    ]


def _fetch_machine_json(whoami_ok: Optional[bool], py_bin: str, py_args: List[str]) -> dict:
    """Deliberate isolation boundary, not a candidate for an in-process
    import — runs ``coordinator_whoami.machine`` under ``py_bin``, a
    resolved candidate interpreter distinct from the one running this
    module, so the registry read reflects that interpreter's own
    environment. See
    ``state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md``
    for the recorded verdict."""
    if not whoami_ok:
        return {}
    try:
        from coordinator_core.win_portability import no_console_creationflags

        proc = subprocess.run(
            [py_bin, *py_args, "-m", "coordinator_whoami.machine"],
            capture_output=True,
            text=True,
            timeout=_PROBE_SPAWN_BUDGET_SECS,
            **no_console_creationflags(),
        )
        out = (proc.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[{_PROG}] could not run coordinator_whoami.machine — {exc} (machine field will be empty)", file=sys.stderr)
        out = ""
    if not out:
        return {}
    try:
        parsed = json.loads(out)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        print(f"[{_PROG}] coordinator_whoami.machine emitted unparsable JSON — {exc} (machine field will be empty)", file=sys.stderr)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_sentinel(
    sentinel_path: Path,
    red_probes: List[str],
    amber_probes: List[str],
    advisory_notes: List[str],
    verdict: str,
    hint: str,
    machine: dict,
) -> bool:
    payload = {
        "ran_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "verdict": verdict,
        "red_probes": red_probes,
        "amber_probes": amber_probes,
        "advisory_notes": advisory_notes,
        "hint": hint,
        "machine": machine,
        "plugin": "coordinator-claude",
    }
    try:
        sentinel_path.parent.mkdir(parents=True, exist_ok=True)
        # Review: code-reviewer (P1) — atomic write (mkstemp in the same dir +
        # os.replace) so a crash/kill mid-write can never leave a truncated
        # doctor-last-run.json for downstream json.loads() consumers.
        fd, tmp = tempfile.mkstemp(dir=str(sentinel_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(payload, f, indent=2)
                f.write("\n")
            os.replace(tmp, sentinel_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                # Best-effort tmp-file cleanup during error unwind -- the
                # original exception is re-raised below regardless, so a
                # failure to remove the orphaned tempfile must not mask it.
                pass
            raise
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Argument parsing — selection grammar
# ---------------------------------------------------------------------------


def _parse_args(argv: Sequence[str]) -> Tuple[str, str]:
    """Returns (mode, arg). Raises _UsageError on bad args (caller maps to exit 2)."""
    if len(argv) == 0:
        return "triage", ""
    if len(argv) == 1:
        a = argv[0]
        if a == "--triage":
            return "triage", ""
        if a == "--full":
            return "full", ""
        if a in ("--cluster", "--probe", "--symptom"):
            raise _UsageError(f"{a} requires an argument")
        if a.startswith("--"):
            raise _UsageError(f"unknown flag: {a}")
        raise _UsageError(f"unknown argument: {a}")
    if len(argv) == 2:
        a, b = argv[0], argv[1]
        if a == "--cluster":
            return "cluster", b
        if a == "--probe":
            return "probe", b
        if a == "--symptom":
            return "symptom", b
        if a in ("--triage", "--full"):
            raise _UsageError(f"too many arguments for {a}")
        raise _UsageError(f"unknown flag: {a}")
    if len(argv) >= 3 and argv[0] == "--symptom":
        return "symptom", " ".join(argv[1:])
    raise _UsageError("too many arguments (pass --symptom TEXT for multi-word symptoms)")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _resolve_claude_home(claude_home_env: Optional[str]) -> Path:
    """Resolve CLAUDE_HOME per machine-local-registry.md §4a: CLAUDE_HOME is a
    $HOME SUBSTITUTE, not the .claude dir itself — the install lives at
    $CLAUDE_HOME/.claude/. Falls back to the real $HOME when unset/empty,
    mirroring the bash oracle's `${CLAUDE_HOME:-$HOME}/.claude/` convention.
    Never reassigns CLAUDE_HOME itself, so there is no double-suffix path
    (the retired bash bug reassigned CLAUDE_HOME to `${CLAUDE_HOME:-$HOME/.claude}`,
    which self-referentially became the .claude dir and double-suffixed once a
    forwarder appended /.claude again).
    """
    return Path(claude_home_env or str(Path.home())) / ".claude"


def _this_claude_klabauter_root() -> Path:
    """Self-relative claude-klabauter-live-root resolution for probe_p23 — this module lives
    at `<claude_klabauter_root>/coordinator_core/plugin_health/sentinel.py`, so a
    running instance of this probe suite already IS inside the claude-klabauter
    checkout whose wrapper source it needs to read. Deliberately NOT
    `coordinator_core.engine_root.coordinator_engine_root()`: that ladder's
    Rung 2 shells out to `machine-local get repos.claude_klabauter` (a
    subprocess spawn this `weight = "cheap"` probe must not incur) and can
    fail on a machine where that registry key drifted — a resolver gap
    unrelated to wrapper drift that this probe has no business surfacing as
    P-23 amber.
    """
    return Path(__file__).resolve().parents[2]


def _run(mode: str, arg: str) -> Tuple[List[str], List[str], int]:
    """Fire the selected probe(s) and produce (stdout_lines, stderr_lines, exit_code).

    Mirrors the bash oracle's mode-dispatch + verdict-synthesis + output shape
    exactly, including probe evaluation ORDER (P-1..P-13, P-15, P-17, P-14, P-18,
    P-19 — matches the physical order of `is_active` blocks in the bash source,
    which determines hint-line ordering in the sentinel JSON's `hint` field).
    """
    stdout_lines: List[str] = []
    stderr_lines: List[str] = []

    coordinator_root = _doe_coordinator_root()
    bin_dir_sibling = coordinator_root / "bin" if coordinator_root else None
    lib_dir_sibling = coordinator_root / "lib" if coordinator_root else None
    if os.environ.get("COORDINATOR_PREREQ_PROBE_LIB_DIR"):
        scripts_lib_dir: Optional[Path] = Path(os.environ["COORDINATOR_PREREQ_PROBE_LIB_DIR"])
    elif coordinator_root:
        scripts_lib_dir = coordinator_root / "scripts" / "lib"
    else:
        scripts_lib_dir = None

    manifest_override = os.environ.get("DOCTOR_PROBES_MANIFEST")
    if manifest_override:
        manifest_path: Optional[Path] = Path(manifest_override)
    else:
        manifest_path = _default_manifest_path(bin_dir_sibling)

    active_ids, sel_exit, sel_err = _select_active_probes(mode, arg or None, manifest_path)
    if sel_exit != 0:
        stderr_lines.append(f"[coordinator-doctor] selector error: {sel_err}")
        return stdout_lines, stderr_lines, sel_exit
    if not active_ids:
        stderr_lines.append(
            "[coordinator-doctor] ERROR: selector returned empty probe set (vacuous-GREEN guard)"
        )
        return stdout_lines, stderr_lines, 2

    active = set(active_ids)
    probes = load_probes(manifest_path)  # already validated to parse above; re-read for id_to_cluster

    try:
        py_bin, py_args = resolve_python_bin()
    except PythonPinInvalid as exc:
        stderr_lines.append(f"[coordinator-doctor] ERROR: {exc}")
        return stdout_lines, stderr_lines, 2
    if not py_bin:
        stderr_lines.append(
            "[coordinator-doctor] ERROR: no Python interpreter found — cannot run selector or probes"
        )
        return stdout_lines, stderr_lines, 2
    py_ident = _py_ident(py_bin, py_args)

    original_claude_home = os.environ.get("CLAUDE_HOME")
    claude_home = _resolve_claude_home(original_claude_home)
    plugins_root = (
        Path(os.environ["COORDINATOR_PLUGINS_ROOT"])
        if os.environ.get("COORDINATOR_PLUGINS_ROOT")
        else claude_home / "plugins"
    )
    sentinel_dir = plugins_root / "coordinator-claude" / "data"
    sentinel_path = sentinel_dir / "doctor-last-run.json"
    bin_dir = claude_home / "bin"

    sh = settings_home()
    sh_bin = sh / "bin"

    # Review: code-reviewer (P2) — mkdir moved out of the unconditional path.
    # _write_sentinel() already does sentinel_path.parent.mkdir(parents=True,
    # exist_ok=True) before writing, so only "full" mode (the only mode that
    # writes the sentinel) needs the directory to exist. triage/cluster/probe/
    # symptom modes no longer create <plugins_root>/coordinator-claude/data/
    # as an unrequested side effect.

    if os.environ.get("MACHINE_LOCAL_REGISTRY_DIR"):
        ml_dir = Path(os.environ["MACHINE_LOCAL_REGISTRY_DIR"])
    else:
        ml_dir = sh / "machine-local"

    ml_cmd = _resolve_cli(sh_bin, bin_dir, "machine-local")
    ch_cmd = _resolve_cli(sh_bin, bin_dir, "claude-home")

    whoami_lazy = _Lazy(lambda: _whoami_importable(py_bin, py_args))
    # Resolved once per run and shared, so a second probe asking the registry
    # the same question costs nothing (G8's verdict). Lazy because P-3 is not
    # in the triage set: a bare triage run must not pay even two file reads it
    # will not consult.
    registry_keys_lazy = _Lazy(lambda: _registry_keys(ml_dir))

    # Review: code-reviewer (P2) — each probe call is isolated in its own
    # try/except. An unexpected exception in any single probe must not abort
    # the whole run before _write_sentinel executes; otherwise the other
    # (passing) probes never get to update the sentinel and a stale sentinel
    # silently persists. A crashing probe is itself reported as red so the
    # failure is visible rather than swallowed.
    notes: List[ProbeNote] = []

    def _run_probe(probe_id: str, fn) -> None:
        if probe_id not in active:
            return
        try:
            notes.extend(fn())
        except Exception as exc:  # noqa: BLE001 - isolate probe crashes, never abort the run
            notes.append(
                ProbeNote(
                    probe_id,
                    "red",
                    f"{probe_id} probe crashed: {type(exc).__name__}: {exc}",
                )
            )

    _run_probe("P-1", lambda: probe_p1(ml_dir))
    _run_probe("P-2", lambda: probe_p2(ml_dir / "registry.toml", ml_dir, py_bin, py_args))
    _run_probe("P-3", lambda: probe_p3(registry_keys_lazy.get()))
    _run_probe("P-4", lambda: probe_p4(ml_cmd, sh_bin))
    _run_probe("P-5", lambda: probe_p5(whoami_lazy.get(), py_ident, sh))
    _run_probe("P-6", lambda: probe_p6(whoami_lazy.get(), py_bin, py_args, py_ident, sh))
    _run_probe("P-6s", lambda: probe_p6s(whoami_lazy.get(), py_bin, py_args, py_ident))
    _run_probe("P-7", lambda: probe_p7(claude_home))
    _run_probe("P-8", lambda: probe_p8(plugins_root))
    _run_probe("P-9", lambda: probe_p9(sh_bin))
    _run_probe("P-10", lambda: probe_p10(ch_cmd, sh_bin))
    _run_probe("P-11", lambda: probe_p11(plugins_root, coordinator_root))
    _run_probe("P-12", lambda: probe_p12(bin_dir_sibling, claude_home))
    _run_probe("P-13", lambda: probe_p13(claude_home, plugins_root, coordinator_root))
    _run_probe("P-15", lambda: probe_p15(scripts_lib_dir))
    _run_probe("P-16", lambda: probe_p16(ml_dir))
    _run_probe("P-17", lambda: probe_p17(scripts_lib_dir))
    _run_probe("P-14", lambda: probe_p14(ml_cmd, ch_cmd, sh_bin))
    _run_probe("P-18", lambda: probe_p18(original_claude_home))
    _run_probe("P-19", lambda: probe_p19(lib_dir_sibling, coordinator_root))
    _run_probe("P-20", lambda: probe_p20())
    _run_probe("P-21", lambda: probe_p21())
    _run_probe("P-22", lambda: probe_p22(_this_claude_klabauter_root()))
    _run_probe(
        "P-23",
        lambda: probe_p23(
            _this_claude_klabauter_root(),
            _resolve_wrapper_home(),
            sh_bin,
        ),
    )

    for _unwired in _UNWIRED_PROBE_IDS:
        if _unwired in active:
            notes.extend(
                _inconclusive(
                    _unwired,
                    "declared in doctor-probes.toml but no probe body is wired here — "
                    "this check did NOT run",
                )
            )

    red_probes = [n.id for n in notes if n.severity == "red"]
    amber_probes = [n.id for n in notes if n.severity == "amber"]
    advisory_notes = [f"{n.id}: {n.message}" for n in notes if n.severity == "advisory"]
    hint_lines = [f"{n.id}: {n.message}" for n in notes if n.severity in ("red", "amber")]

    if red_probes:
        verdict = "RED"
    elif amber_probes:
        verdict = "AMBER"
    else:
        verdict = "GREEN"

    hint = "All coordinator-doctor probes passed." if verdict == "GREEN" else " | ".join(hint_lines)

    if mode == "full":
        machine_json = _fetch_machine_json(whoami_lazy.get(), py_bin, py_args)
        ok = _write_sentinel(
            sentinel_path, red_probes, amber_probes, advisory_notes, verdict, hint, machine_json
        )
        if not ok:
            stderr_lines.append(
                f"[coordinator-doctor] WARN: failed to write sentinel at {sentinel_path} — check "
                "disk space and permissions"
            )
            return stdout_lines, stderr_lines, 0

        if verdict != "GREEN":
            stdout_lines.append(f"[coordinator-doctor] {verdict} ({sentinel_path})")
            stdout_lines.append(f"  {hint}")

        if advisory_notes:
            stdout_lines.append("[coordinator-doctor] INFO (advisory — verdict unchanged):")
            for adv in advisory_notes:
                stdout_lines.append(f"  [INFO] {adv}")

        return stdout_lines, stderr_lines, 0

    # --- Subset mode (triage / cluster / probe / symptom) ---
    stdout_lines.append(f"[coordinator-doctor] MODE={mode} verdict={verdict}")

    # Presentation-only split (verdict/JSON schema untouched): an
    # inconclusive(...) note is carried on the amber severity bucket
    # DELIBERATELY (see _inconclusive's docstring — the sentinel JSON schema
    # cannot grow a fourth bucket), but "Failing" is the wrong word for a
    # probe that never ran at all. List genuinely-failing ids separately from
    # inconclusive-only ones so a triage run whose only notes are
    # inconclusive(...) never claims anything failed.
    failing_probe_ids: List[str] = []
    inconclusive_probe_ids: List[str] = []
    for pid in red_probes + amber_probes:
        if pid in failing_probe_ids or pid in inconclusive_probe_ids:
            continue
        msgs = [n.message for n in notes if n.id == pid and n.severity in ("red", "amber")]
        if msgs and all(m.startswith("inconclusive(") for m in msgs):
            inconclusive_probe_ids.append(pid)
        else:
            failing_probe_ids.append(pid)

    if failing_probe_ids or inconclusive_probe_ids:
        if failing_probe_ids:
            stdout_lines.append(f"  Failing: {' '.join(failing_probe_ids)}")
        if inconclusive_probe_ids:
            stdout_lines.append(f"  Inconclusive (did not run): {' '.join(inconclusive_probe_ids)}")
        stdout_lines.append(f"  Hint: {hint}")
    else:
        stdout_lines.append("  All selected probes passed.")

    if advisory_notes:
        stdout_lines.append("  Advisory (informational — verdict unchanged):")
        for adv in advisory_notes:
            stdout_lines.append(f"    [INFO] {adv}")

    if mode == "triage":
        if red_probes or amber_probes:
            failing_ids = red_probes + amber_probes
            rec_clusters: List[str] = []
            for fid in failing_ids:
                try:
                    cluster = id_to_cluster(probes, fid)
                except SystemExit:
                    print(
                        f"[{_PROG}] {fid} failed but has no cluster in the manifest — "
                        "omitting from --cluster recommendation",
                        file=sys.stderr,
                    )
                    continue
                if cluster and cluster not in rec_clusters:
                    rec_clusters.append(cluster)

            stdout_lines.append("")
            stdout_lines.append("  RECOMMENDATION: triage found failing probes.")
            for rc in rec_clusters:
                stdout_lines.append(f"    Run: {_PROG} --cluster {rc}")
            stdout_lines.append(f"    Or run: {_PROG} --full  (to run all probes and write the health sentinel)")
        else:
            stdout_lines.append("")
            stdout_lines.append(
                f"  RECOMMENDATION: triage probes all passed. Run '{_PROG} --full' for "
                "complete health check."
            )

    return stdout_lines, stderr_lines, 0


def main(argv: Sequence[str]) -> int:
    try:
        mode, arg = _parse_args(argv)
    except _UsageError as exc:
        print(f"[coordinator-doctor] ERROR: {exc}", file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        return 2

    stdout_lines, stderr_lines, exit_code = _run(mode, arg)
    for line in stdout_lines:
        print(line)
    for line in stderr_lines:
        print(line, file=sys.stderr)
    return exit_code


@register_op("plugin_health.sentinel")
async def _plugin_health_sentinel(params: dict, repo_root=None) -> dict:
    """JSON-RPC "plugin_health.sentinel" handler.

    Params: mode (optional str, one of triage/full/cluster/probe/symptom; defaults
    to "triage"), arg (optional str — required for cluster/probe/symptom, ignored
    otherwise). repo_root is accepted for handler-signature parity but IGNORED —
    this op inspects/writes the operator's OWN machine-local plugin health sentinel
    (settings-home + CLAUDE_HOME resolved), not the caller's repo (same "none"-scope
    class as plugin_health.drift / plugin_health.scan).

    Returns {"exit_code": int, "lines": [...], "stderr_lines": [...]}.
    """
    params = params or {}
    mode = str(params.get("mode") or "triage")
    if mode not in ("triage", "full", "cluster", "probe", "symptom"):
        mode = "triage"
    arg = str(params.get("arg") or "")
    stdout_lines, stderr_lines, exit_code = _run(mode, arg)
    return {"exit_code": exit_code, "lines": stdout_lines, "stderr_lines": stderr_lines}


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
