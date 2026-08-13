"""scripts/setup.py — claude-klabauter standalone setup script (cross-platform).

AUTHORITATIVE REGISTRATION SURFACE for the agent-install contract of BOTH
Claude-klabauter AND claude-klabauter — docs/install/agent-install-manifest.json
(this repo) and docs/install/klabauter-agent-install-manifest.json (the
published mirror's INSTALL.md instructs the same invocation) both point
``standalone_setup_script.posix``/``.windows`` at this one file (see § C6
backlink below for why one script replaced the former POSIX/PowerShell
twins). `resolve_repo_identity` tells the two trees apart at runtime so
registration (Responsibility 3) writes the right key for whichever repo is
actually running it.

Responsibilities:
  1. Step Zero system prerequisite checks (Python 3.11+, behavior-verified).
  2. coordinator-claude sibling-dep check (hard — PM ruling 2026-08-03: "klabauter-claude-klabauter
     is useless without example-doctrine-repo/coordinator-claude to wire into, it's a 100% hard dep for
     us"). Fails loud (exit 90) when missing/broken, unless the
     --skip-dep-check + --accept-missing-deps-risk override pair is supplied.
  3. AUTHORITATIVE idempotent registration, branched on `resolve_repo_identity`:
     running from claude-klabauter, machine-local set repos.claude_klabauter AND
     machine-local set engine.working_repos.claude_klabauter (same resolved value,
     unchanged from before the branch existed); running from claude-klabauter,
     machine-local set repos.claude_klabauter ONLY (neither claude_klabauter key is
     written — see `register_claude_klabauter_root`'s docstring for why). Neither identity
     resolvable: fail loud (exit 95), never guessed. Guard (both identities): if
     machine-local is absent (coordinator-claude hard-dep not installed) AND
     the override pair was NOT supplied, fail loud (exit 90) — the same contract as
     the dep check above, since step 2 already exited before reaching here in the
     un-overridden case; this guard only matters when the override pair WAS supplied
     (step 2 skipped) and coordinator-claude is genuinely absent, in which case
     registration degrades gracefully (advisory, exit 0) — the operator already
     accepted the risk. Fail loud ONLY when machine-local IS present but the set
     fails, or when the override pair was not supplied.
  4. Verification: `import coordinator_core` from CLAUDE_KLABAUTER_ROOT.
  5. Post-install health probe (bin/claude-klabauter-doctor-probe.py --step-zero) as best-effort.
  6. Install claude-klabauter's OWN `.git/hooks/pre-commit` gate chain (staged-rollback
     detector; see coordinator_core.ops.install_claude_klabauter_precommit_hook) as best-effort —
     skipped in --register-only mode, and a clean no-op on any checkout that isn't
     claude-klabauter itself (the op's own identity guard).

Spec backlink: docs/plans/2026-07-04-claude-klabauter-install-and-doctor-system.md § C2
Spec backlink: docs/plans/2026-07-21-claude-klabauter-pure-python-shop-retire-all-bash.md § C6
  (this file replaces scripts/setup.sh + scripts/setup.ps1 — one cross-platform
  naked-Python installer instead of a bash/PowerShell twin pair, per the
  project's pure-Python-shop mandate; DR-047/DR-059).
Spec backlink: ~/.claude/plugins/coordinator-claude/coordinator/docs/wiki/agent-install-contract.md
Resolver reference: this file's own `resolve_claude_klabauter_root` (CLAUDE_KLABAUTER_ROOT ladder)
  and `_resolve_coordinator_claude_root` (coordinator-claude sibling ladder) —
  the bash `coordinator-claude-klabauter-root.sh` reference this docstring previously
  named is retired (pure-Python-shop mandate, DR-047/DR-059; § C6 above).

Usage:
  python3 scripts/setup.py [--i-am-agent] [--skip-dep-check --accept-missing-deps-risk]
                            [--claude-klabauter-root <path>] [--coordinator-root <path>]
                            [--break-system-packages] [--with-test-deps]
                            [--register-only] [--check] [--help]

Negative-spec:
  coordinator_core is NOT stdlib-only — pyproject.toml's [project].dependencies
  array declares its REQUIRED deps. This script DERIVES that list from
  pyproject.toml at run time (tomllib) rather than hardcoding it, so the
  provisioned set cannot drift from the declared set as deps are added/removed.
  It installs them at MACHINE level by default (so any interpreter can invoke
  coordinator_core without resolving a venv); the settings-home coordinator
  venv (coordinator_core.install.ensure_venv) is a FALLBACK ONLY, used when
  the machine Python is PEP-668 externally-managed and --break-system-packages
  was not passed. See § Dependency provisioning below.
  Does NOT install the [project.optional-dependencies].test extra by default —
  the installer's job is the ENGINE, not the dev loop, and pytest plugins
  auto-load into every OTHER repo's pytest run on the same interpreter, so
  provisioning them unasked mutates machine state well outside this repo's
  blast radius. What it DOES do unconditionally is CHECK the declared test
  floor and print the exact one-line remediation when the machine is behind it
  (§ Test tooling below) — the F2 hole was undeclared tooling, and a
  declaration nobody probes is the same hole with better paperwork. Pass
  --with-test-deps to actually install it. Both the check and the install
  derive from the SAME [project.optional-dependencies].test array; there is no
  second hardcoded list.
  Does NOT seed harness plugin-enablement state into settings.local.json — that seed
  step was removed; ~/.claude / coordinator surfaces remain out of scope for this
  script.
  Does NOT hardcode CLAUDE_KLABAUTER_ROOT — resolves via flag -> env -> repo-root ladder.
  Does NOT hardcode the coordinator-claude sibling-dir — resolves via
  --coordinator-root flag -> COORDINATOR_CLAUDE_ROOT env -> shared .doe-root
  pointer -> engine.working_repos.example_doctrine_repo registry key -> settings-home
  .doe-root sentinel -> sibling-dir default, so a packaging installer (e.g.
  example-os-repo) can inject the location instead of relying on side-by-side git
  clone placement. The sibling-dir default is the only rung that guesses, and
  it is existence-gated: an unverified guess comes back flagged in the source
  string, never as if it had resolved. See `_resolve_coordinator_claude_root`
  for each rung's rationale and precedence.
  Does NOT shell out to bash/PowerShell anywhere in this file — the whole
  installer is naked Python (subprocess is used only to invoke OTHER Python
  interpreters/venvs and the `machine-local` / `pip` executables).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

# Exit-code 93 is the agent-install contract's reserved code for the
# --skip-dep-check / --accept-missing-deps-risk flag-pair violation.
EXIT_FLAG_PAIR_VIOLATION = 93

# Exit-code 90 is the agent-install contract's reserved code for a hard dep
# missing/broken with no override supplied — the SAME code
# coordinator_core.install.dep_check's consent-gate and
# coordinator_core.ops.setup_chain_walker.consent_gate raise for the
# identical condition (contract § exit-code 90), so a caller distinguishing
# entrypoints by exit code sees one contract, not two.
EXIT_HARD_DEP_MISSING = 90

# Exit-code 94: a `severity: "hard"` post-install health probe (bin/claude-klabauter-
# doctor-probe.py --step-zero) reported a non-"pass" status -- e.g.
# `claude-klabauter.root.resolve` genuinely failing to resolve CLAUDE_KLABAUTER_ROOT after
# registration. Distinct from EXIT_HARD_DEP_MISSING (90, a missing
# DEPENDENCY caught before installation proceeds) -- this fires AFTER
# registration/import have already succeeded, when the post-install
# self-check itself finds the install did not actually take. Reserved here
# (not yet in any external contract doc) since `run_health_probe` returning
# a bool is new; if a downstream consumer starts depending on this code,
# document it alongside EXIT_HARD_DEP_MISSING/EXIT_FLAG_PAIR_VIOLATION.
EXIT_HEALTH_PROBE_HARD_FAILURE = 94

# Exit-code 95: this script is BOTH claude-klabauter's AND claude-klabauter's
# standalone installer (docs/plans/2026-07-31-claude-klabauter-oss-release.md
# §150/§626; docs/install/klabauter-agent-install-manifest.json's
# standalone_setup_script), but the two trees need different registrations --
# see `resolve_repo_identity`. Raised when neither identity's positive marker
# is present, so registration has no reliable signal to act on. Distinct from
# EXIT_HARD_DEP_MISSING (90, a missing DEPENDENCY): this is a missing SELF-
# IDENTITY, and guessing here would poison the working-repo discriminant
# rather than merely fail an install step, so this failure is never
# degradable via --skip-dep-check/--accept-missing-deps-risk the way the
# dependency checks are.
EXIT_REPO_IDENTITY_UNRESOLVED = 95

# Windows-only: suppresses the console-popup a subprocess spawn otherwise
# triggers when this installer is invoked from a headless/GUI parent
# (agent dispatch, packaging installer). getattr(...) resolves to 0 (no-op)
# on macOS/Linux, where CREATE_NO_WINDOW does not exist.
_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}

HELP_TEXT = """\
Claude-klabauter installer — sets up the coordinator control-plane engine.

Checks prerequisites (Python 3.11+), installs coordinator_core's declared
dependencies, registers this repo in the machine-local registry, verifies the
engine imports, and runs a health probe. Safe to re-run at any time.

Usage: python3 scripts/setup.py [OPTIONS]     (Windows: python scripts\\setup.py)

Options:
  --i-am-agent                       Suppress interactive prompts (non-interactive agent dispatch)
  --skip-dep-check                   Skip dependency checking (pair with --accept-missing-deps-risk)
  --accept-missing-deps-risk         Accept hallucination risk from missing deps (pair with --skip-dep-check)
  --claude-klabauter-root <path>               Explicit CLAUDE_KLABAUTER_ROOT override (default: CLAUDE_KLABAUTER_ROOT env -> repo-root)
  --coordinator-root <path>          Explicit coordinator-claude root override (default: COORDINATOR_CLAUDE_ROOT env -> sibling-dir)
  --break-system-packages            Explicit opt-in: pass pip --break-system-packages on a PEP-668
                                      externally-managed machine, instead of falling back to a venv
  --with-test-deps                   Also install the declared test extra (pytest + plugins). Off by
                                      default: the installer provisions the engine, not the dev loop
  --register-only                    Skip Step Zero + dep check; run registration + verification only
  --check                            Smoke-test that the script is present and executable; exits 0
  --help                             Show this message
"""

IMPORT_NAME_OVERRIDES = {
    "pyyaml": "yaml",
    "pillow": "PIL",
    "beautifulsoup4": "bs4",
    "pytest-xdist": "xdist",
}

# The [project.optional-dependencies] key holding the test tier's tooling.
# Named once so the check path and the --with-test-deps install path cannot
# read different arrays.
TEST_EXTRA = "test"

# The [project.optional-dependencies] key holding the non-Python symbol
# extraction extra. Named once for actual parity with TEST_EXTRA above —
# Review: code-reviewer 2026-08-08 (P3) — print_symbols_extra_hint previously
# hardcoded the literal "symbols" three times while its docstring claimed
# "same discipline as TEST_EXTRA above," which was untrue until this constant
# existed.
SYMBOLS_EXTRA = "symbols"


def quote_specs(specs: list[str]) -> str:
    """Render pip specs as a copy-pasteable argument list, quoted for EVERY
    shell this installer's remediation lines can be pasted into.

    shell-doc-ok: this passage documents the redirect hazard itself and must show
the unsafe spelling next to the safe one to make the contrast legible.

A bare `pytest>=9.1` in a remediation line is a REDIRECT the moment anyone
    pastes it into a shell — `>=9.1` is eaten and a file named `9.1` is
    silently created instead of a version floor reaching pip. This repo's own
    working tree carries the scar tissue: a zero-byte `3.11` at the root (the
    shape of `requires-python = ">=3.11"` pasted unquoted).

    DOUBLE quotes, not single, and that is the cross-platform point rather
    than a style choice: `"pytest>=9.1"` is correct in sh/bash AND PowerShell
    AND cmd.exe, whereas `'pytest>=9.1'` is correct in the first two but is
    passed to pip with the quote characters still attached by cmd.exe. Neither
    Windows nor POSIX is the fallback case here — one form is simply right on
    both. Nothing in a PEP 508 specifier is interpolated inside double quotes
    by any of the four ($ and backtick cannot appear in a version specifier)."""
    return " ".join(f'"{spec}"' if any(ch in spec for ch in "<>=~[ ") else spec for spec in specs)


def setup_invocation() -> str:
    """This script's own invocation string, in the form the HOST platform's
    shell actually accepts — `python scripts\\setup.py` on Windows (where
    `python3` is routinely an App-Execution-Alias stub and the separator is a
    backslash), `python3 scripts/setup.py` elsewhere. Remediation output is
    meant to be pasted, so it is rendered for the machine printing it rather
    than for POSIX with a parenthetical Windows footnote."""
    if os.name == "nt":
        return "python scripts\\setup.py"
    return "python3 scripts/setup.py"


class ArgError(Exception):
    """A malformed CLI invocation — caller prints .args[0] to stderr and exits 1."""


class Args:
    """Parsed CLI flags — mirrors the flag surface of the retired setup.sh/setup.ps1 twins."""

    def __init__(self) -> None:
        self.agent_mode = False
        self.skip_dep_check = False
        self.accept_risk = False
        self.break_system_packages = False
        self.with_test_deps = False
        self.register_only = False
        self.check = False
        self.help = False
        self.claude_klabauter_root: str = ""
        self.coordinator_root: str = ""


def parse_args(argv: list[str]) -> Args:
    """Hand-rolled parser (not argparse) so unknown-flag / missing-value errors match
    the retired bash/PowerShell twins' exact exit-1 contract rather than argparse's
    exit-2 usage-error convention."""
    args = Args()
    i = 0
    n = len(argv)
    while i < n:
        tok = argv[i]
        if tok == "--i-am-agent":
            args.agent_mode = True
        elif tok == "--skip-dep-check":
            args.skip_dep_check = True
        elif tok == "--accept-missing-deps-risk":
            args.accept_risk = True
        elif tok == "--break-system-packages":
            args.break_system_packages = True
        elif tok == "--with-test-deps":
            args.with_test_deps = True
        elif tok == "--register-only":
            args.register_only = True
        elif tok == "--check":
            args.check = True
        elif tok == "--help":
            args.help = True
        elif tok == "--claude-klabauter-root":
            i += 1
            if i >= n or argv[i].startswith("--"):
                got = argv[i] if i < n else ""
                raise ArgError(f"ERROR: --claude-klabauter-root requires a path argument (got: '{got}')")
            args.claude_klabauter_root = argv[i]
        elif tok == "--coordinator-root":
            i += 1
            if i >= n or argv[i].startswith("--"):
                got = argv[i] if i < n else ""
                raise ArgError(f"ERROR: --coordinator-root requires a path argument (got: '{got}')")
            args.coordinator_root = argv[i]
        else:
            raise ArgError(
                f"ERROR: unknown flag: {tok}\n"
                "  Run 'python3 scripts/setup.py --help' for usage."
            )
        i += 1
    return args


def _python_version_ok(executable: str, timeout: float = 10.0) -> bool:
    """Behavior-verify (not name-order) that `executable` resolves, runs, and
    reports Python >= 3.11. Timeout-guarded: on Windows a `python3` name is
    frequently a non-executable shim (Git-Bash wrapper, App-execution-alias
    stub) that neither honors the shebang nor exits cleanly — a bare
    subprocess call would hang rather than fail, stalling the whole installer
    (observed on a real install 2026-07-14, install-friction F1)."""
    try:
        proc = subprocess.run(
            [executable, "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"],
            timeout=timeout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_NO_CONSOLE,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return False


def resolve_python() -> str:
    """Resolve a working Python 3.11+ interpreter.

    This script is itself Python, so the common case (a healthy interpreter
    already ran it) needs no probing: `sys.executable` already satisfies the
    floor once this module's own top-level `from __future__ import` line ran
    without a SyntaxError, since tomllib (used below) requires 3.11 to import
    at all. We still behavior-verify explicitly for a clear, actionable
    error message rather than a bare ImportError traceback, and — for parity
    with the retired twins — fall back to probing `python3`/`python` on PATH
    for the rare case this file was invoked via an older interpreter that
    somehow reached this line (e.g. `python2 scripts/setup.py`, which would
    already have failed on `from __future__ import annotations` syntax before
    reaching here on truly ancient interpreters, but not on 3.x < 3.11).

    Review: code-reviewer 2026-07-21 Finding 1 (P1) — a found candidate is
    re-exec'd into via `os.execvp` BEFORE returning, not merely returned by
    name: the host process is still the original sub-3.11 interpreter at this
    point, and `derive_deps` below does `import tomllib` (3.11+ stdlib-only)
    in whichever process actually keeps running — returning the name without
    re-exec left that import to crash in the still-old host, defeating this
    function's own "clear error, not a bare traceback" purpose."""
    if sys.version_info >= (3, 11):
        return sys.executable

    script_path = Path(__file__).resolve()
    for candidate in ("python3", "python"):
        if _python_version_ok(candidate):
            os.execvp(candidate, [candidate, str(script_path), *sys.argv[1:]])
            return candidate  # pragma: no cover — os.execvp never returns on success

    print(
        f"FAIL [hard] python — Python 3.11+ required (got {sys.version.split()[0]}).",
        file=sys.stderr,
    )
    print("  Remediation: install Python 3.11+ from https://www.python.org/downloads/", file=sys.stderr)
    print(
        "  On Windows: disable App Execution Alias stubs (Settings > Apps > App execution "
        "aliases) before installing.",
        file=sys.stderr,
    )
    sys.exit(1)


def derive_deps(pyproject_path: Path, extra: str | None = None) -> tuple[list[str], list[str]]:
    """Read a dependency array from pyproject.toml and return (pip specs,
    import-probe names). PyPI distribution names and import names are not
    guaranteed to match (e.g. PyYAML -> yaml) — IMPORT_NAME_OVERRIDES is the
    explicit, minimal exception table for known mismatches; anything absent
    from it falls back to a normalized (lowercased, hyphen->underscore) form
    of the distribution name, which is correct for every dependency this repo
    declares today.

    `extra=None` (the default) reads [project].dependencies — the REQUIRED
    runtime set. Passing an extra name reads
    [project.optional-dependencies].<extra> instead, which is how the test
    tooling is derived rather than hardcoded a second time here; an absent
    extra returns empty lists so the CALLER decides whether that is fatal (it
    is under --with-test-deps, advisory otherwise)."""
    import tomllib

    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    if extra is None:
        deps = data["project"]["dependencies"]
    else:
        deps = data["project"].get("optional-dependencies", {}).get(extra)
        if not deps:
            return [], []

    specs: list[str] = []
    import_names: list[str] = []
    for dep in deps:
        dep = dep.strip()
        m = re.match(r"^([A-Za-z0-9_.-]+)", dep)
        if not m:
            print(f"could not parse dependency entry: {dep!r}", file=sys.stderr)
            sys.exit(1)
        dist_name = m.group(1)
        specs.append(dep)
        normalized = dist_name.lower().replace("-", "_")
        import_names.append(IMPORT_NAME_OVERRIDES.get(dist_name.lower(), normalized))
    return specs, import_names


def deps_importable(interpreter: str, import_names: list[str]) -> bool:
    """True iff every name in `import_names` imports cleanly under `interpreter`.

    Review: code-reviewer 2026-07-21 Finding 4 (P1) — `timeout=` added to the
    cross-interpreter probe. `interpreter` can be a bare `python3`/`python`
    name resolved via the Finding-1 fallback path (a Windows App-Execution-
    Alias stub / non-executable shim), the same category `_python_version_ok`
    was given `timeout=10.0` to survive after a real hang was observed
    (2026-07-14, install-friction F1) — this call site had no timeout and
    could reintroduce that exact hang."""
    if interpreter == sys.executable:
        return all(importlib.util.find_spec(name) is not None for name in import_names)
    import_expr = ",".join(import_names)
    try:
        proc = subprocess.run(
            [interpreter, "-c", f"import {import_expr}"],
            timeout=10.0,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_NO_CONSOLE,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _run_pip(argv: list[str]) -> subprocess.CompletedProcess:
    """Run a pip subprocess with a 600s timeout (matching ensure_venv.py's
    `_install_deps` convention — Review: code-reviewer 2026-07-21 Finding 5,
    P2: an unbounded pip subprocess can hang forever on a stalled network).
    `LC_ALL=C` is pinned so the PEP-668 substring match in `provision_deps`
    below stays valid regardless of the invoking environment's locale
    (Finding 10, nit). stderr is merged into stdout via `stderr=STDOUT`
    rather than captured separately and concatenated after the fact, so
    printed output preserves the original stream interleaving (Finding 11,
    nit). Raises `subprocess.TimeoutExpired` on timeout — callers handle it."""
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    return subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=600,
        env=env,
        **_NO_CONSOLE,
    )


def provision_deps(claude_klabauter_root: Path, py: str, break_system_packages: bool) -> tuple[str, list[str]]:
    """Derive coordinator_core's declared deps from pyproject.toml and ensure
    they're importable — machine-level pip install by default, falling back
    to the settings-home coordinator venv on ANY install failure (PEP-668
    externally-managed refusal, or any other failure e.g. permission-denied
    on a locked-down system interpreter), unless --break-system-packages
    opts into retrying the machine-level install on PEP-668 refusal
    specifically.
    Returns (engine_python, import_names).

    `claude_klabauter_root` is the FLAG -> ENV -> repo-root resolved CLAUDE_KLABAUTER_ROOT (see
    `resolve_claude_klabauter_root`), not necessarily the script's own on-disk location
    — Review: code-reviewer 2026-07-21 Finding 7 (P2): a packaging installer
    that stages this script somewhere other than the real CLAUDE_KLABAUTER_ROOT and
    passes `--claude-klabauter-root` needs dependency derivation/sys.path insertion to
    follow that override, not silently fall back to script-location-derived
    repo_root.

    Negative-spec: the machine-level attempt below is a PLAIN `pip install`
    with NO `--user` flag. `--user` targets `site.getusersitepackages()`,
    which is resolved from `HOME` (POSIX) / `USERPROFILE` (Windows) at
    interpreter-startup time, not baked into the interpreter's own install
    location — empirically, `HOME=/tmp/x python3 -c "import site;
    print(site.getusersitepackages())"` returns a DIFFERENT path than the
    same command with the real HOME, for the identical interpreter. Any real
    entry point invoked under a HOME other than the one active at install
    time (a hooked/sandboxed subprocess, a CI runner, a service account —
    the production trampoline is example-doctrine-repo's cc_invoke.py, which spawns
    `[sys.executable, "-m", "coordinator_core.invoke", ...]` using whatever
    HOME the CALLING process has) would resolve an EMPTY site-packages dir
    and crash with ModuleNotFoundError on a machine where `--user` had
    "succeeded." A plain (non---user) install lands under `sys.prefix`,
    which is fixed to the interpreter's own install path and is therefore
    HOME-independent; the settings-home venv fallback below is likewise
    HOME-independent (anchored to `coordinator_core._settings_home.settings_home()`,
    not repo_root — claude-klabauter does not own a `<claude-klabauter>/.venv`, D3/D4). Neither
    branch may reintroduce a HOME-resolved target. See
    docs/decisions/2026-07-21-coordinator-core-dependency-and-environment-boundary.md
    § D2 (machine-level install is the sanctioned primary path)."""
    print()
    print("--- Dependency provisioning (derived from pyproject.toml) ---")

    pyproject = claude_klabauter_root / "pyproject.toml"
    if not pyproject.is_file():
        print(f"FAIL [deps] pyproject.toml not found at {pyproject} — cannot derive dependency list.", file=sys.stderr)
        sys.exit(1)

    dep_specs, import_names = derive_deps(pyproject)
    print(f"Derived dependency specs:  {' '.join(dep_specs)}")
    print(f"Derived import-probe list: {' '.join(import_names)}")

    # coordinator_core.install.ensure_venv is stdlib-only (see its own module
    # docstring), so it's importable here even before coordinator_core's
    # third-party deps (pydantic, jsonschema, ...) are provisioned — but it
    # is NOT necessarily pip-installed under the interpreter running THIS
    # script, so resolve it against claude_klabauter_root's local source tree directly.
    if str(claude_klabauter_root) not in sys.path:
        sys.path.insert(0, str(claude_klabauter_root))
    from coordinator_core._settings_home import settings_home
    from coordinator_core.install.ensure_venv import (
        EnsureVenvError,
        ensure_coordinator_venv,
        venv_python_path,
    )

    engine_py = py
    # Fallback venv: the settings-home `.coordinator-venv` (shared with
    # coordinator-claude's own venv, via coordinator_core.install.ensure_venv)
    # — NOT a `<claude-klabauter>/.venv`. Claude-klabauter does not own its own venv (D3/D4,
    # DR-commit 286f94b7); see docs/decisions/2026-07-21-coordinator-core-dependency-and-environment-boundary.md.
    #
    # Review: code-reviewer 2026-07-21 Finding 6 (P2) — settings_home()
    # resolves through `CLAUDE_HOME`/`Path.home()`, and `Path.home()` raises
    # `RuntimeError` when neither `HOME` (POSIX) nor `USERPROFILE` (Windows)
    # is resolvable — exactly the HOME-stripped-sandbox shape this fix set
    # out to be robust against. Fail loud with an actionable message instead
    # of a bare traceback (not a design change — D3/D4 still forces
    # settings-home as the fallback venv location).
    try:
        settings_home_path = settings_home()
    except RuntimeError as exc:
        print(f"FAIL [deps] cannot resolve settings-home: {exc}", file=sys.stderr)
        print("  Remediation: set CLAUDE_HOME (or HOME on POSIX / USERPROFILE on Windows) and re-run.", file=sys.stderr)
        sys.exit(1)
    venv_dir = settings_home_path / ".coordinator-venv"
    venv_py = venv_python_path(venv_dir)

    if deps_importable(py, import_names):
        print(f"PASS [deps] {' '.join(import_names)} already importable under {py} — no-op.")
        return engine_py, import_names

    if venv_py.exists() and deps_importable(str(venv_py), import_names):
        print(f"PASS [deps] {' '.join(import_names)} already importable under existing fallback venv ({venv_dir}) — no-op.")
        return str(venv_py), import_names

    print(f"{' '.join(import_names)} not importable under {py} — attempting machine-level install.")
    # No --user: HOME-independent by construction (see docstring negative-spec
    # above). Lands under sys.prefix, not a HOME-resolved user-site dir.
    try:
        pip_proc = _run_pip([py, "-m", "pip", "install", *dep_specs])
    except subprocess.TimeoutExpired:
        print(f"FAIL [deps] pip install timed out after 600s under {py}.", file=sys.stderr)
        sys.exit(1)
    print(pip_proc.stdout, end="")

    if pip_proc.returncode == 0:
        print("PASS [deps] machine-level install (pip, HOME-independent) succeeded.")
    else:
        pip_output = pip_proc.stdout.lower()
        is_pep668 = "externally-managed-environment" in pip_output

        if is_pep668 and break_system_packages:
            print()
            print(f"  pip refused: PEP 668 externally-managed environment ({py}).", file=sys.stderr)
            print("  --break-system-packages passed — retrying with explicit consent.", file=sys.stderr)
            try:
                pip2 = _run_pip([py, "-m", "pip", "install", "--break-system-packages", *dep_specs])
            except subprocess.TimeoutExpired:
                print(f"FAIL [deps] pip install --break-system-packages timed out after 600s under {py}.", file=sys.stderr)
                sys.exit(1)
            print(pip2.stdout, end="")
            if pip2.returncode != 0:
                print("FAIL [deps] pip install --break-system-packages also failed — see output above.", file=sys.stderr)
                sys.exit(1)
            print("PASS [deps] machine-level install (--break-system-packages, explicit consent) succeeded.")
        else:
            # Fallback to the settings-home coordinator venv, HOME-independent
            # by construction — not just on PEP 668 refusal, but on ANY
            # machine-level install failure (e.g. permission-denied on a
            # locked-down system interpreter that isn't PEP-668-flagged). The
            # prior version only fell back on a detected PEP-668 string match
            # and hard-failed on other errors; that left a
            # install-succeeds-once-with-a-HOME-dependent-`--user`-flag path
            # as the only recourse on such machines, reintroducing the very
            # HOME-dependence this fix removes. Falling back here instead
            # keeps every surviving path HOME-independent.
            print()
            if is_pep668:
                print("  Machine Python is externally-managed (PEP 668) — plain pip install is blocked.")
            else:
                print("  Machine-level pip install failed for a reason other than PEP 668 (see output above).")
            print(f"  Falling back to the settings-home coordinator venv at {venv_dir}. This is a")
            print("  FALLBACK, not the primary mechanism — no consumer should need to resolve this venv.")
            print()
            if is_pep668:
                print("  To install at machine level instead, on the next run pick ONE of:")
                print("    - python3 scripts/setup.py --break-system-packages   (explicit opt-in; we will not do this silently)")
                print("    - pipx (https://pipx.pypa.io) if your interpreter is pipx-managed")
                print("    - your OS/distro package manager")
                print()
            try:
                venv_status = ensure_coordinator_venv(
                    claude_klabauter_root,
                    settings_home_path,
                    claude_home=os.environ.get("CLAUDE_HOME"),
                )
            except EnsureVenvError as exc:
                print(f"FAIL [deps] settings-home coordinator venv provisioning failed: {exc}", file=sys.stderr)
                sys.exit(1)
            print(f"  settings-home coordinator venv: {venv_status}")

            try:
                venv_pip = _run_pip([str(venv_py), "-m", "pip", "install", *dep_specs])
            except subprocess.TimeoutExpired:
                print(f"FAIL [deps] venv fallback pip install timed out after 600s under {venv_py}.", file=sys.stderr)
                sys.exit(1)
            print(venv_pip.stdout, end="")
            if venv_pip.returncode != 0:
                print("FAIL [deps] venv fallback install failed — see output above.", file=sys.stderr)
                sys.exit(1)
            engine_py = str(venv_py)
            print(f"PASS [deps] venv fallback install succeeded ({venv_dir}).")

    # Verify, don't trust — an install that reports exit 0 but leaves a module
    # unimportable (partial install, wrong target interpreter) is exactly the
    # failure class this defect was: don't report success on an unverified install.
    if not deps_importable(engine_py, import_names):
        print(f"FAIL [deps] {' '.join(import_names)} still not importable under {engine_py} after install reported success.", file=sys.stderr)
        print(f"  Remediation: run manually: {engine_py} -m pip install {quote_specs(dep_specs)}", file=sys.stderr)
        sys.exit(1)
    print(f"PASS [deps] verified importable under {engine_py}.")
    return engine_py, import_names


# ---------------------------------------------------------------------------
# Test tooling — declared, probed, opt-in to install
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS. pytest and its plugins were ungoverned ambient machine state:
# pyproject.toml declared no test extra and no requirements file, so the
# documented fast/full tiers ran against whatever was installed. A clean
# Windows install on pytest 9.0.3 hit 4800 collection errors and an unrunnable
# suite (state/audits/2026-07-28-windows-install-dogfood-friction.md § F1/F2).
#
# WHY THE INSTALL IS OPT-IN BUT THE CHECK IS NOT. Installing is a machine
# mutation outside this repo's blast radius — pytest plugins auto-load into
# EVERY pytest run on the same interpreter, so an unasked-for plugin changes
# unrelated repos' suites. The version SKEW, by contrast, costs nothing to
# observe and is the thing that actually broke: so the floor is checked on
# every run and reported with the one command that fixes it, per CLAUDE.md
# § North star ("make the correct path cheaper than the wrong one" /
# design-as-offers — lead with the better alternative, not the violation).


def _parse_floor(spec: str) -> tuple[str, tuple[int, ...] | None]:
    """Split a pip spec into (distribution name, `>=` floor as an int tuple).

    Reads ONLY the `>=` form, which is every spec the test extra uses. Any
    other operator (`==`, `~=`, `<`, a marker, an extras suffix) yields a None
    floor and is reported as present/absent without a version comparison —
    deliberately narrow, because a half-correct PEP 440 reimplementation that
    silently mis-compares is worse than declining to compare. `packaging` is
    not available here: this script must run before ANY dependency is
    provisioned, so it is stdlib-only by construction."""
    m = re.match(r"^([A-Za-z0-9_.-]+)", spec.strip())
    dist = m.group(1) if m else spec.strip()
    floor_match = re.search(r">=\s*([0-9]+(?:\.[0-9]+)*)", spec)
    if not floor_match:
        return dist, None
    return dist, tuple(int(part) for part in floor_match.group(1).split("."))


def _version_tuple(version: str) -> tuple[int, ...] | None:
    """Leading numeric release segments of a version string, or None when the
    string does not start with one (a VCS/local build, say). Pre-release and
    local suffixes are truncated rather than ordered — the check below only
    ever asks "is this clearly BELOW the floor", and truncation cannot turn a
    satisfying version into a failing one."""
    m = re.match(r"^([0-9]+(?:\.[0-9]+)*)", version.strip())
    if not m:
        return None
    return tuple(int(part) for part in m.group(1).split("."))


def _probe_dist_versions(interpreter: str, dist_names: list[str]) -> dict[str, str | None]:
    """Installed version of each distribution under `interpreter`, or None when
    it is not installed there. Metadata rather than an import probe, because
    the floor check needs the VERSION — and because a pytest plugin is resolved
    by entry point, not by anyone importing it.

    Same timeout discipline as `deps_importable` above: `interpreter` may be a
    bare name resolved via the resolve_python fallback (a Windows
    App-Execution-Alias stub), the shape that hung a real install on
    2026-07-14."""
    program = (
        "import json,sys\n"
        "from importlib.metadata import version, PackageNotFoundError\n"
        "out={}\n"
        "for name in sys.argv[1:]:\n"
        "    try: out[name]=version(name)\n"
        "    except PackageNotFoundError: out[name]=None\n"
        "print(json.dumps(out))\n"
    )
    try:
        proc = subprocess.run(
            [interpreter, "-c", program, *dist_names],
            timeout=30.0,
            capture_output=True,
            text=True,
            **_NO_CONSOLE,
        )
        if proc.returncode != 0:
            return {name: None for name in dist_names}
        return json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return {name: None for name in dist_names}


def _install_test_deps(engine_py: str, specs: list[str], break_system_packages: bool) -> None:
    """pip-install the test extra under `engine_py` — the interpreter
    `provision_deps` already resolved and verified (the machine interpreter, or
    the settings-home fallback venv when the machine one was unusable).

    Deliberately does NOT repeat provision_deps' venv-bootstrap ladder, and
    that is a settings-path constraint before it is a DRY one. The fallback
    venv location is owned by `coordinator_core._settings_home.settings_home()`
    + `coordinator_core.install.ensure_venv` (the contract-sanctioned durable
    prefix, CLAUDE.md § durable-data plane); re-deriving it here would mean a
    SECOND resolver for a path that already has exactly one, and any drift
    between them lands test tooling under an interpreter the suite never runs
    from. Taking `engine_py` as given is what keeps this function free of any
    home/settings-path resolution of its own — no `expanduser`, no
    `Path.home()`, no hand-built `~/...`. The PEP-668 retry is kept because
    --break-system-packages must mean the same thing for both arrays.

    Fails loud: this path only runs when the operator explicitly passed
    --with-test-deps, and silently half-installing the tooling is how F2
    happened in the first place."""
    print(f"Installing test extra under {engine_py}: {quote_specs(specs)}")
    try:
        proc = _run_pip([engine_py, "-m", "pip", "install", *specs])
    except subprocess.TimeoutExpired:
        print(f"FAIL [test-deps] pip install timed out after 600s under {engine_py}.", file=sys.stderr)
        sys.exit(1)
    print(proc.stdout, end="")

    if proc.returncode != 0:
        is_pep668 = "externally-managed-environment" in proc.stdout.lower()
        if not (is_pep668 and break_system_packages):
            print("FAIL [test-deps] test-extra install failed — see output above.", file=sys.stderr)
            if is_pep668:
                print(
                    "  Machine Python is externally-managed (PEP 668). Re-run with "
                    "--with-test-deps --break-system-packages to consent explicitly.",
                    file=sys.stderr,
                )
            sys.exit(1)
        print(f"  pip refused: PEP 668 externally-managed environment ({engine_py}).", file=sys.stderr)
        print("  --break-system-packages passed — retrying with explicit consent.", file=sys.stderr)
        try:
            retry = _run_pip([engine_py, "-m", "pip", "install", "--break-system-packages", *specs])
        except subprocess.TimeoutExpired:
            print(f"FAIL [test-deps] pip install --break-system-packages timed out after 600s under {engine_py}.", file=sys.stderr)
            sys.exit(1)
        print(retry.stdout, end="")
        if retry.returncode != 0:
            print("FAIL [test-deps] --break-system-packages retry also failed — see output above.", file=sys.stderr)
            sys.exit(1)


def handle_test_tooling(claude_klabauter_root: Path, engine_py: str, args: Args) -> None:
    """Check (always) and optionally install (--with-test-deps) the declared
    test extra, derived from pyproject.toml — never a second hardcoded list.

    Verify-don't-trust applies to the install too: the post-install report is
    the SAME metadata probe used for the check, so an install that exits 0 but
    lands nothing under `engine_py` is visible rather than reported as success.
    """
    print()
    print(f"--- Test tooling ([project.optional-dependencies].{TEST_EXTRA}) ---")

    pyproject = claude_klabauter_root / "pyproject.toml"
    specs, _ = derive_deps(pyproject, extra=TEST_EXTRA)
    if not specs:
        print(f"[ADVISORY] no [project.optional-dependencies].{TEST_EXTRA} declared — nothing to check.")
        if args.with_test_deps:
            print(
                f"FAIL [test-deps] --with-test-deps passed but pyproject.toml declares no "
                f"'{TEST_EXTRA}' extra at {pyproject}.",
                file=sys.stderr,
            )
            sys.exit(1)
        return

    print(f"Derived test specs: {' '.join(specs)}")

    if args.with_test_deps:
        _install_test_deps(engine_py, specs, args.break_system_packages)

    floors = [_parse_floor(spec) for spec in specs]
    installed = _probe_dist_versions(engine_py, [dist for dist, _ in floors])

    unmet: list[str] = []
    for dist, floor in floors:
        have = installed.get(dist)
        if have is None:
            print(f"ADVISORY [test-deps] {dist} — not installed under {engine_py}")
            unmet.append(dist)
            continue
        have_tuple = _version_tuple(have)
        if floor is None or have_tuple is None:
            print(f"PASS [test-deps] {dist} {have} (no comparable floor declared)")
            continue
        floor_str = ".".join(str(part) for part in floor)
        if have_tuple < floor:
            print(f"ADVISORY [test-deps] {dist} {have} is BELOW the declared floor >={floor_str}")
            unmet.append(dist)
        else:
            print(f"PASS [test-deps] {dist} {have} (>= {floor_str})")

    if not unmet:
        return

    print()
    if args.with_test_deps:
        print(
            "WARN [test-deps] the install above reported success but the extra is still "
            f"unsatisfied under {engine_py} — see the advisories above.",
            file=sys.stderr,
        )
        return
    print("  The engine install is unaffected — this is the TEST tier only, and it is advisory.")
    print("  It is reported because running the documented test command against tooling below")
    print("  the declared floor is what produced 4800 collection errors on 2026-07-28.")
    print("  To provision it:")
    print(f"    {setup_invocation()} --with-test-deps")
    print(f"  Or directly:  {engine_py} -m pip install {quote_specs(specs)}")


def print_symbols_extra_hint(claude_klabauter_root: Path) -> None:
    """Print ONE advisory line pointing at the optional `symbols` extra
    (non-Python symbol extraction, coordinator_core/ops/foreign_symbols.py).

    Deliberately never checked, probed, or installed here -- unlike
    `handle_test_tooling` above, there is no version-floor drift risk to
    report (the dependency is either present or absent, not skewed), and the
    package resolves against a PRIVATE git ref. Most current install targets
    already have credentials for that repo by convention and can install this
    extra straightforwardly; a genuine credential-less minority exists too,
    and for that machine attempting to import-probe or pip-check the extra
    here would be indistinguishable from a real failure -- so this stays a
    single informational line rather than a checked/verified step. Advisory-
    only and MUST NOT affect the exit code or be treated as a verification
    step (PM ruling 2026-08-08: "fail gracefully if the user doesn't have
    access to example-retrieval-repo" -- the credential-less case is real but a
    minority, not the expected-default path). Reads the extra name from
    pyproject.toml rather than hardcoding it twice, same discipline as
    TEST_EXTRA above; silently no-ops if the extra is absent (nothing to hint
    at)."""
    pyproject = claude_klabauter_root / "pyproject.toml"
    specs, _ = derive_deps(pyproject, extra=SYMBOLS_EXTRA)
    if not specs:
        return
    print()
    print("[INFO] Non-Python symbol extraction (TypeScript, etc.) is available via the optional")
    print(f"  '{SYMBOLS_EXTRA}' extra -- requires access to the private example-retrieval-repo repo. To install:")
    print(f"    {_symbols_extra_install_hint()}")


def _symbols_extra_install_hint() -> str:
    """Render the pip-install line for `print_symbols_extra_hint`'s
    remediation line, matching `setup_invocation`'s host-shell-aware
    convention (this repo's editable install target is `.[symbols]` from the
    repo root, not a PyPI package name -- coordinator_core is not published)."""
    if os.name == "nt":
        return "python -m pip install -e .[symbols]"
    return 'python3 -m pip install -e ".[symbols]"'


def _is_publish_mirror(path: Path) -> bool:
    """True iff `path` resolves under (or equals) any registered
    `publish.mirrors.*.path` entry in the machine-local registry — the same
    mechanism the cross-repo write guard uses to identify the mirror
    (`coordinator_core.bash_guards._write_bump_applicability.
    target_is_publish_destination`), reused rather than re-derived here.

    Fails open (`False`) when `coordinator_core` is not importable yet (this
    function may run before `sys.path` carries it — see call sites), when
    the registry has no `publish.mirrors.*` entries at all (the OSS case,
    a pure no-op), or on any other resolution failure."""
    try:
        from coordinator_core.bash_guards._write_bump_applicability import (
            target_is_publish_destination,
        )
    except ImportError:
        return False
    try:
        return target_is_publish_destination(str(path))
    except Exception:
        return False


#: Structural markers under a example-doctrine-repo dev-clone's ``coordinator/`` that identify it as
#: a real coordinator-claude source checkout. ANY one matching is sufficient.
#:
#: negative-spec: do NOT narrow this back to a single path. It was
#: ``coordinator/CLAUDE.md`` alone until example-doctrine-repo retired that file (`e8f9051db`,
#: "finish the coordinator/CLAUDE.md retirement and re-point every citation"), at
#: which point the real dev clone stopped being recognised as a source checkout at
#: all, `_resolve_coordinator_claude_root` fell through to a sibling-dir guess that
#: does not exist, and `scripts/setup.py` aborted with exit 90 on every run. A
#: one-file probe makes a sibling repo's ordinary retirement into our outage; the
#: any-of set is the fix, so keep it plural.
# Review: code-reviewer 2026-08-07 Finding 5 (P3) — split into distinctive
# vs generic markers rather than one flat `any(...)` tuple. `commands/`,
# `hooks/`, `skills/` are common-enough directory names that any ONE of them
# existing under an arbitrary `coordinator/` subdirectory (not necessarily a
# coordinator-claude checkout at all) was previously sufficient on its own
# to satisfy `has_dev_clone_shape`. The distinctive markers stay
# sufficient alone (they don't collide with an unrelated project's
# scaffold); the generic ones now require at least 2-of-3 co-occurring —
# this only narrows acceptance (a real coordinator-claude dev clone still
# has all six), never widens it.
_DEV_CLONE_DISTINCTIVE_MARKERS = (
    "canonical-structure.yaml",
    "artifact-shape-contract",
    "cockpit-contract",
)
_DEV_CLONE_GENERIC_MARKERS = (
    "commands",
    "hooks",
    "skills",
)
_DEV_CLONE_GENERIC_MIN_COUNT = 2


def _looks_like_coordinator_claude_source(path: Path) -> bool:
    """Positive evidence `path` is an actual coordinator-claude checkout,
    either shape: the OSS mirror/source-clone shape (`.claude-plugin/
    plugin.json` PLUS at least one of `commands/`, `hooks/` — plugin.json
    alone does not distinguish a real source clone from a publish mirror,
    which also ships plugin.json), or the example-doctrine-repo dev-clone shape (see
    `_DEV_CLONE_DISTINCTIVE_MARKERS`/`_DEV_CLONE_GENERIC_MARKERS` above —
    NOT `coordinator/CLAUDE.md`, which example-doctrine-repo retired; see the negative-spec
    on those constants)."""
    has_oss_shape = (path / ".claude-plugin" / "plugin.json").is_file() and (
        (path / "commands").is_dir() or (path / "hooks").is_dir()
    )
    coordinator_dir = path / "coordinator"
    has_distinctive_marker = coordinator_dir.is_dir() and any(
        (coordinator_dir / marker).exists()
        for marker in _DEV_CLONE_DISTINCTIVE_MARKERS
    )
    generic_marker_count = sum(
        (coordinator_dir / marker).exists()
        for marker in _DEV_CLONE_GENERIC_MARKERS
    ) if coordinator_dir.is_dir() else 0
    has_dev_clone_shape = has_distinctive_marker or generic_marker_count >= _DEV_CLONE_GENERIC_MIN_COUNT
    return has_oss_shape or has_dev_clone_shape


def _coordinator_root_from_settings_home() -> "Path | None":
    """Read the coordinator-claude source root recorded in the settings home.

    The settings home is the standing read surface for every resolved path (it
    is durable and Anthropic-independent, unlike `~/.claude`), and it already
    carries `machine-local/.doe-root` naming the example-doctrine-repo dev clone. This rung reads
    it so the resolver stops guessing a sibling directory that was never where
    the checkout lives on this machine.

    Returns None when the sentinel is absent, empty, or does not resolve to a
    directory that still looks like a source checkout — a stale sentinel must
    fall through to the remaining rungs, never pin the resolver to a dead path.

    negative-spec: this rung deliberately does NOT read
    `publish.mirrors.coordinator_claude.path`. That key names a generated
    downstream publish mirror, which `_is_publish_mirror` exists to reject; the
    hard-dep gate has to walk the SOURCE checkout. The two being different
    places on the same machine is the normal case, not a misconfiguration.
    """
    # Review: code-reviewer 2026-08-07 Finding 4 (P2) — narrowed from a bare
    # `except Exception` so a broken resolution-machinery failure (an
    # ImportError on `coordinator_core._settings_home`, or `settings_home()`
    # itself misconfigured) surfaces instead of looking identical to "no
    # sentinel recorded yet." Only the stale/absent-sentinel case (a missing
    # file or an unreadable one) is meant to fall through silently to the
    # sibling-dir rung; a resolver-machinery failure is printed to stderr
    # before falling through, so it is visible without being fatal.
    try:
        from coordinator_core._settings_home import settings_home

        sentinel = settings_home() / "machine-local" / ".doe-root"
    except Exception as exc:
        # Kept non-fatal (print + None, not a re-raise): letting an
        # ImportError/misconfiguration propagate here would abort the rest
        # of setup over a rung that is itself best-effort (falls through to
        # a sibling-dir guess). Printed so it is visible instead of looking
        # identical to "no sentinel recorded yet" — see finding for the
        # narrower alternative considered (letting it surface) and why this
        # print-and-continue was picked instead.
        print(f"[ADVISORY] settings-home resolution failed ({exc}); cannot read .doe-root sentinel.", file=sys.stderr)
        return None
    try:
        if not sentinel.is_file():
            return None
        recorded = sentinel.read_text(encoding="utf-8").strip()
    except OSError as exc:
        print(f"[ADVISORY] could not read .doe-root sentinel at {sentinel} ({exc}).", file=sys.stderr)
        return None
    if not recorded:
        return None
    candidate = Path(recorded)
    if not candidate.is_dir() or not _looks_like_coordinator_claude_source(candidate):
        return None
    return candidate


def _coordinator_root_from_doe_root_pointer() -> "Path | None":
    """Read the shared `.doe-root` pointer file via
    `read_doe_root_pointer.py::coordinator_read_doe_root_pointer()` — private
    dev-clone layout at `coordinator/lib/read_doe_root_pointer.py`, published
    payload layout (flattened, see C1F) at `lib/read_doe_root_pointer.py` —
    the durable settings-home sentinel first
    (`${settings-home}/machine-local/.doe-root`), then the legacy cold-readable
    fallback (`${CLAUDE_HOME:-$HOME}/.claude/.doe-root`).

    Ranked ahead of BOTH the registry rung and the settings-home-only rung
    below (2026-08-07, C1Cc). This does NOT reverse `da7cd333a`'s decision: it
    ranked the registry rung above the settings-home SENTINEL rung; this new
    rung is the shared pointer read (sentinel + legacy fallback) placed ahead
    of both, not a reordering of the two `da7cd333a` already ordered.

    Review: staff-eng 2026-08-08 MINOR-4 — the ordering was previously
    justified as "setup.py runs before a registry is necessarily populated on
    a fresh box," but nothing in THIS installer ever writes the
    `.doe-root` sentinel either (checked every install-chain step:
    `install_bin_forwarders`, `install_precommit_hook`,
    `install_percolate_identity`, `install_machine_identity` — none write
    `.doe-root`), so that justification buys nothing on the fresh-box path it
    names. The real justification: a pointer written by coordinator-claude's
    OWN installer is stronger evidence than a key written by an arbitrary
    peer installer (the registry rung), which in turn is stronger than a
    locally-recorded breadcrumb (the settings-home sentinel). The ordering
    only actually changes behavior on a box where coordinator-claude's own
    installer already ran and wrote the pointer — i.e. a box where the
    registry is likely populated too.

    The shared lib dir (private `coordinator/lib`, published `lib`) is
    stdlib-only and dependency-free (mirrors the `sys.path.insert` pattern
    C1's landed ladder uses for the same helper — see
    `coordinator/bin/lib/coordinator_registry.py::
    _mp_doe_root_pointer_rung`), so importing it here does not violate this
    script's bootstrap-before-deps-provisioned discipline.

    Returns None when the pointer is empty, does not resolve to a directory,
    or does not look like a real coordinator-claude source checkout — a
    stale/foreign pointer must fall through to the remaining rungs, never pin
    the resolver to a dead or wrong path. Fails open (prints an advisory,
    returns None) if the helper itself is unimportable."""
    lib_dir = Path(__file__).resolve().parent.parent / "coordinator" / "lib"
    if not (lib_dir / "read_doe_root_pointer.py").is_file():
        # Published payload flattens: the mirror ships the helper at
        # "<repo root>/lib" with no "coordinator/" segment. Probed as a
        # fallback — private tree wins first.
        lib_dir = Path(__file__).resolve().parent.parent / "lib"
    added = str(lib_dir) not in sys.path
    if added:
        sys.path.insert(0, str(lib_dir))
    try:
        from read_doe_root_pointer import coordinator_read_doe_root_pointer
    except Exception as exc:
        # Swallows: helper missing at both probed dirs, import error inside
        # the helper itself — advisory-only, callers fall through to the
        # remaining rungs (see docstring: fails open).
        print(f"[ADVISORY] could not import read_doe_root_pointer helper ({exc}); skipping .doe-root pointer rung.", file=sys.stderr)
        return None
    finally:
        if added:
            try:
                sys.path.remove(str(lib_dir))
            except ValueError:
                pass
    try:
        recorded = coordinator_read_doe_root_pointer()
    except Exception as exc:
        print(f"[ADVISORY] .doe-root pointer read failed ({exc}).", file=sys.stderr)
        return None
    if not recorded:
        return None
    candidate = Path(recorded)
    if not candidate.is_dir() or not _looks_like_coordinator_claude_source(candidate):
        return None
    return candidate


def _coordinator_root_from_registry() -> "Path | None":
    """Read `engine.working_repos.example_doctrine_repo` — the DR-132-ratified registry
    namespace each repo's own installer writes as an identity assertion for
    "where is a coordinator-claude WORKING checkout" — via
    `coordinator_core.machine_resolver.registry_get` (never a `machine-local`
    subprocess shell-out; C1b's engine-side twin in
    `coordinator_core.ops.setup_chain_walker._resolve_coordinator_root_ladder`
    uses the same seam).

    The raw registered value is a repo root whose plugin source lives one
    directory down (verified live: it carries no `.claude-plugin/plugin.json`,
    no `commands/`, no `hooks/` at its own top level), so it is passed through
    C1a's shape derivation (`_resolve_plugin_root_for_machine_local`, this
    file's thin wrapper over `coordinator_core.coordinator_root`) before being
    offered as a candidate — mirroring the engine-side twin exactly.

    Fails open (`None`) on an absent key, an empty value, or a derivation that
    returns `None` — the caller falls through to the next rung, never treats a
    registry miss as an error.

    Imported in-function, not at module scope: this script is the bootstrap
    installer and runs before `coordinator_core`'s third-party deps are
    provisioned (same discipline as `_resolve_plugin_root_for_machine_local`
    above).

    Review: staff-eng 2026-08-08 MINOR-6 — this was the only rung with no
    failure path: a corrupt/unreadable machine-local registry file, or a
    `settings_home()` RuntimeError under a HOME-stripped environment, would
    propagate out of this best-effort rung and abort the whole installer
    with a bare traceback. Wrapped in the same print-advisory-and-return-None
    shape the pointer and settings-home rungs already use."""
    try:
        from coordinator_core.machine_resolver import registry_get

        registry_val = registry_get("engine.working_repos.example_doctrine_repo")
        if not registry_val:
            return None
        return _resolve_plugin_root_for_machine_local(Path(registry_val))
    except Exception as exc:
        print(f"[ADVISORY] registry resolution failed ({exc}); skipping engine.working_repos.example_doctrine_repo rung.", file=sys.stderr)
        return None


class CoordSourceRung(Enum):
    """Identity of the rung `_resolve_coordinator_claude_root` resolved a
    candidate from — the undecorated discriminant callers MUST branch on.

    Defect class fix 2026-08-08 (see
    state/debt-backlog/2026-08-08-a-decorated-string-is-used-as-a-control-
    91d15e71174a.yaml): `_resolve_coordinator_claude_root` used to return only
    a human-readable `coord_source` string, and all four call sites branched
    on that string (`==`, `.startswith`, `in`) against literals. A diagnostic
    suffix appended to the string for display purposes (` [UNRESOLVED --
    PATH DOES NOT EXIST]`) silently broke an exact-`==` branch, disabling the
    `git clone` remediation on precisely the fresh-OSS-box case it exists
    for; a later fix widened the check to `.startswith`, which is still a
    string-shape dependency and remains breakable the same way by a future
    annotation. `CoordSourceRung` carries no display text at all — nothing
    about it can be broken by decorating a message — so branching on it is
    Structurally immune to this defect class. `CoordSourceResolution` below
    pairs a rung with the display string that print statements want, so the
    two purposes (control-flow vs. presentation) can never be reconflated."""

    FLAG = auto()
    ENV = auto()
    DOE_ROOT_POINTER = auto()
    REGISTRY = auto()
    SETTINGS_HOME = auto()
    SIBLING_DIR_DEFAULT = auto()


@dataclass(frozen=True)
class CoordSourceResolution:
    """Identity + presentation for a resolved coordinator-claude root.

    `rung` is the undecorated control-flow discriminant — callers branch on
    it, never on `display`. `display` is free-form human-readable text meant
    ONLY for printing; it may be annotated/decorated at will (e.g. a
    publish-mirror rejection or an unresolved-path note) without ever risking
    a control-flow branch, because no branch may read it.

    Negative-spec: no field on this dataclass is compared with `==`,
    `.startswith`, or `in` against a string literal anywhere in this file.
    `is_publish_mirror_rejected` and `is_unresolved` are their own booleans
    for exactly the two conditions that used to be encoded as suffixes on
    the display string."""

    rung: CoordSourceRung
    display: str
    is_publish_mirror_rejected: bool = False
    is_unresolved: bool = False


def _resolve_coordinator_claude_root(repo_root: Path, args: Args) -> tuple[Path, CoordSourceResolution]:
    """Resolve the coordinator-claude sibling root and describe the source
    used: --coordinator-root flag -> COORDINATOR_CLAUDE_ROOT env -> shared
    .doe-root pointer (durable + legacy) -> engine.working_repos.example_doctrine_repo
    registry key -> settings-home .doe-root sentinel -> sibling-dir default
    (now honesty-gated, see below). Shared by `check_coordinator_claude_dep`
    and `register_claude_klabauter_root` so both resolve the SAME candidate root
    regardless of whether the (hard) dep-check ran (e.g. --skip-dep-check) —
    Review: code-reviewer 2026-07-21 Finding 3 (P1), extracted so
    `register_claude_klabauter_root` can resolve a plugin_root for
    `resolve_machine_local_cli` without duplicating this ladder.

    Returns `(candidate_path, CoordSourceResolution)` — see that dataclass's
    docstring for why identity (`.rung`) and presentation (`.display`) are
    kept apart (defect-class fix 2026-08-08).

    Pointer rung (2026-08-07, C1Cc): `_coordinator_root_from_doe_root_pointer`
    outranks the registry rung specifically for this installer — see that
    function's docstring for why (setup.py runs before a registry is
    necessarily populated). It does NOT reverse the ordering `da7cd333a`
    established between the registry rung and the settings-home sentinel
    rung; those two keep their relative order below it.

    Registry rung (2026-08-07, DR-132): `engine.working_repos.example_doctrine_repo` is
    the generic, cross-fleet answer to "where is a coordinator-claude WORKING
    checkout" — each repo's own installer writes it as an identity assertion.
    It outranks the `.doe-root` sentinel (a locally-recorded breadcrumb) and
    the sibling-dir default (merely a directory that happens to sit next
    door): better evidence outranks weaker evidence. Explicit operator
    overrides (flag/env) still outrank everything, including the registry.
    On a box where BOTH a registered working checkout and a sibling dir
    exist, this CHANGES which one wins — that is the intended fix, not a
    side effect. See `_coordinator_root_from_registry` for the read/derive
    mechanics.

    Defect fix 2026-08-07: the sibling-dir default is NOT accepted blind —
    the returned `CoordSourceResolution.is_publish_mirror_rejected` is True
    whenever the resolved candidate (from ANY rung, including the registry
    rung and an explicit override) is a registered `publish.mirrors.*.path`
    entry (a generated downstream copy, never the source checkout the
    hard-dep gate must walk). This function still returns the candidate path
    unconditionally — callers that need to fail loud on the rejection
    (`check_coordinator_claude_dep`) inspect the boolean; callers that merely
    display the source (this docstring's own callers) surface the rejection
    via `.display` in their own printed diagnostics for free.

    Defect fix 2026-08-07 (C1Cc): the sibling-dir default previously
    fabricated a guess and returned it whether or not it existed on disk —
    the ONLY rung in this ladder with no failure path at all, a silent
    wrong-answer hazard on a clean OSS box where the guessed sibling
    directory does not exist. It is now `os.path.isdir`-verified; when the
    guess does not exist, `CoordSourceResolution.is_unresolved` is True (same
    signalling purpose as the publish-mirror rejection above, kept as its own
    boolean rather than a display-string suffix) instead of being returned as
    if it were a verified answer. The path itself is still returned
    unconditionally (same contract as every other rung) so existing callers
    that print/inspect `coord_path` keep working; `check_coordinator_claude_dep`
    already fails loud with an actionable git-clone remediation whenever
    `_looks_like_coordinator_claude_source(coord_path)` is False, which an
    unresolved guess always is."""
    # Review: staff-eng 2026-08-08 MINOR-5 — the three lower rungs used to be
    # evaluated eagerly, unconditionally, ahead of the flag/env override
    # check below. Each can print an [ADVISORY] to stderr, and this function
    # is called 4x per run, so an operator who passed --coordinator-root
    # explicitly (and is entitled to no resolution chatter at all) could see
    # the same advisory printed up to four times. Short-circuited: the lower
    # rungs are only evaluated once no explicit override is present.
    is_unresolved = False
    if args.coordinator_root:
        candidate, rung, display = Path(args.coordinator_root), CoordSourceRung.FLAG, "--coordinator-root flag"
    elif os.environ.get("COORDINATOR_CLAUDE_ROOT"):
        candidate, rung, display = Path(os.environ["COORDINATOR_CLAUDE_ROOT"]), CoordSourceRung.ENV, "COORDINATOR_CLAUDE_ROOT env"
    else:
        doe_root_pointer_root = _coordinator_root_from_doe_root_pointer()
        registry_root = _coordinator_root_from_registry()
        settings_home_root = _coordinator_root_from_settings_home()
        if doe_root_pointer_root is not None:
            candidate, rung, display = doe_root_pointer_root, CoordSourceRung.DOE_ROOT_POINTER, "shared .doe-root pointer"
        elif registry_root is not None:
            candidate, rung, display = registry_root, CoordSourceRung.REGISTRY, "engine.working_repos.example_doctrine_repo registry key"
        elif settings_home_root is not None:
            candidate, rung, display = settings_home_root, CoordSourceRung.SETTINGS_HOME, "settings-home .doe-root sentinel"
        else:
            candidate = repo_root.parent / "coordinator-claude"
            rung, display = CoordSourceRung.SIBLING_DIR_DEFAULT, "sibling-dir default"
            if not os.path.isdir(candidate):
                is_unresolved = True
                display = f"{display} [UNRESOLVED -- PATH DOES NOT EXIST]"
    is_publish_mirror_rejected = _is_publish_mirror(candidate)
    if is_publish_mirror_rejected:
        display = f"{display} [PUBLISH MIRROR -- REJECTED]"
    return candidate, CoordSourceResolution(
        rung=rung,
        display=display,
        is_publish_mirror_rejected=is_publish_mirror_rejected,
        is_unresolved=is_unresolved,
    )


def _resolve_plugin_root_for_machine_local(coord_path: Path) -> Path | None:
    """Thin wrapper over `coordinator_core.coordinator_root`'s implementation
    (moved there so it's importable from a stdlib-only module — see that
    module's docstring). Imported INSIDE the function, not at module scope:
    this script is the bootstrap installer and runs before `coordinator_core`'s
    third-party deps are provisioned, mirroring the established pattern at
    this file's `_ensure_venv`-adjacent call site."""
    from coordinator_core.coordinator_root import (
        _resolve_plugin_root_for_machine_local as _impl,
    )

    return _impl(coord_path)


#: repo_root/AGENTS.md's first line in a claude-klabauter clone -- the
#: klabauter payload's own entry-point file, staged today at
#: dist/klabauter-toplevel/AGENTS.md and flat-mirrored to the published repo
#: root (setup/publish-targets.portable row `claude-klabauter-publish-repo-
#: toplevel`; dist/klabauter-toplevel/.percolate-ignore explicitly documents
#: AGENTS.md as shipped, never destination-native). Claude-klabauter's own repo
#: root carries no AGENTS.md at all (verified).
_KLABAUTER_AGENTS_MD_MARKER = "# claude-klabauter"

#: docs/install/agent-install-manifest.json's `repo_id` value in a
#: claude-klabauter working tree -- an authored contract field (agent-install-
#: contract.md), read here as claude-klabauter's own positive identity assertion. The
#: klabauter counterpart (docs/install/klabauter-agent-install-manifest.json)
#: is NOT YET staged into dist/klabauter-toplevel (no publish-targets.portable
#: row admits it), so a real claude-klabauter clone carries no
#: docs/install/agent-install-manifest.json today -- this repo_id check must
#: stay claude-klabauter-only until that gap closes.
_CLAUDE_KLABAUTER_MANIFEST_REPO_ID = "claude-klabauter"


def resolve_repo_identity(repo_root: Path) -> str | None:
    """Determine whether `repo_root` is a claude-klabauter working tree or a
    published claude-klabauter clone -- this script is the standalone
    installer for BOTH repos (see module docstring), and the two need
    DIFFERENT machine-local registrations (see `register_claude_klabauter_root`).

    Returns "claude-klabauter", "claude-klabauter", or None when neither
    tree's positive marker is present.

    Deliberately asserts identity from a marker each payload SHIPS, never
    from the other payload's absence -- klabauter is a content mirror of
    claude-klabauter, so anything identical in both trees (e.g. `[project] name =
    "coordinator_core"` in pyproject.toml, verified identical) is useless as
    a signal, and "claude-klabauter's file is missing" is equally true on a
    partial/corrupt checkout of either repo. Checked in this order because
    only the klabauter marker is unambiguous on its own tree; the claude-klabauter
    manifest check runs second and is scoped by `_CLAUDE_KLABAUTER_MANIFEST_REPO_ID`
    rather than mere file presence for the same reason.

    Spec backlink: cross-repo/inbox/2026-08-05-example-doctrine-repo-em-klabauter-
    location-belongs-in-the-registry-not-a-pointer-file.md
    """
    agents_md = repo_root / "AGENTS.md"
    if agents_md.is_file():
        try:
            first_line = agents_md.read_text(encoding="utf-8").splitlines()[0]
        except (OSError, IndexError):
            first_line = ""
        if first_line.strip().startswith(_KLABAUTER_AGENTS_MD_MARKER):
            return "claude-klabauter"

    manifest = repo_root / "docs" / "install" / "agent-install-manifest.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if data.get("repo_id") == _CLAUDE_KLABAUTER_MANIFEST_REPO_ID:
            return "claude-klabauter"

    return None


def check_coordinator_claude_dep(repo_root: Path, args: Args) -> None:
    """coordinator-claude sibling-dep check (hard, PM ruling 2026-08-03) — fails
    loud (exit 90) when missing/broken. `main()` only calls this function when
    the --skip-dep-check + --accept-missing-deps-risk override pair was NOT
    supplied (see `main`'s `if args.skip_dep_check: ... else:
    check_coordinator_claude_dep(...)` branch), so there is no separate
    override check to make here — reaching this function at all means the
    override was not accepted, and a missing/broken dep is fatal."""
    print()
    print("--- Dep check: coordinator-claude (hard) ---")

    coord_path, coord_source = _resolve_coordinator_claude_root(repo_root, args)

    print(f"coordinator-claude root source: {coord_source.display}")
    print(f"coordinator-claude candidate: {coord_path} (source: {coord_source.display})")

    if coord_source.is_publish_mirror_rejected:
        print(
            f"ERROR [hard] coordinator-claude — {coord_path} is a registered PUBLISH MIRROR "
            f"(publish.mirrors.*.path), not the source checkout (exit {EXIT_HARD_DEP_MISSING})",
            file=sys.stderr,
        )
        print("  A publish mirror is a generated downstream copy — the hard-dep gate must walk", file=sys.stderr)
        print("  the real coordinator-claude SOURCE checkout, never a mirror.", file=sys.stderr)
        print(file=sys.stderr)
        print("  Point --coordinator-root / $COORDINATOR_CLAUDE_ROOT at the actual working checkout", file=sys.stderr)
        print("  (an OSS source clone not registered under publish.mirrors.*.path, or a example-doctrine-repo-style", file=sys.stderr)
        print("  dev clone with the coordinator/ dev-clone markers -- see _looks_like_coordinator_claude_source).", file=sys.stderr)
        print(file=sys.stderr)
        print("  To proceed anyway, accept the risk explicitly (both flags together):", file=sys.stderr)
        print("    --skip-dep-check --accept-missing-deps-risk", file=sys.stderr)
        print(file=sys.stderr)
        sys.exit(EXIT_HARD_DEP_MISSING)

    # functional_probe: a "coordinator-claude" root can be shaped two ways —
    # (a) the OSS mirror clone, which carries .claude-plugin/plugin.json at its
    #     root PLUS commands/ or hooks/ (plugin.json alone does not distinguish
    #     a real source clone from a publish mirror, which also ships
    #     plugin.json — the mirror-rejection check above is what actually
    #     closes that gap; this shape check is the positive-evidence floor for
    #     everything else), or (b) a example-doctrine-repo dev-clone (e.g. Example-doctrine-repo), where the
    #     coordinator plugin source lives under a coordinator/ subdir and the
    #     _DEV_CLONE_*_MARKERS constants mark it (NOT coordinator/CLAUDE.md,
    #     which example-doctrine-repo retired -- see the negative-spec on those constants).
    #     Both are valid, functioning coordinator-claude roots -- PASS if
    #     either shape matches. A single hardcoded probe path here previously
    #     false-WARNed on the dev-clone shape (F9).
    if _looks_like_coordinator_claude_source(coord_path):
        print(f"PASS [hard] coordinator-claude — present at {coord_path}")
        return

    print(f"ERROR [hard] coordinator-claude — not found at {coord_path} (exit {EXIT_HARD_DEP_MISSING})", file=sys.stderr)
    print("  coordinator-claude is a HARD dep (PM ruling 2026-08-03): claude-klabauter is not usable", file=sys.stderr)
    print("  without it to wire into.", file=sys.stderr)
    print(file=sys.stderr)
    # Defect class fix 2026-08-08 (state/debt-backlog/2026-08-08-a-decorated-
    # string-is-used-as-a-control-91d15e71174a.yaml): was a string comparison
    # against `coord_source` (first `==`, then `.startswith` after MAJOR-1),
    # either of which a future annotation to the display text could silently
    # break again. `coord_source.rung` is the undecorated identity — no
    # amount of decorating `.display` can change it.
    if coord_source.rung is CoordSourceRung.SIBLING_DIR_DEFAULT:
        print("  To install coordinator-claude:", file=sys.stderr)
        print(f"    git clone https://github.com/dbc-oduffy/coordinator-claude {coord_path}", file=sys.stderr)
    else:
        print(f"  coordinator-claude not found at the provided --coordinator-root/COORDINATOR_CLAUDE_ROOT location: {coord_path}", file=sys.stderr)
    print("  To proceed anyway, accept the risk explicitly (both flags together):", file=sys.stderr)
    print("    --skip-dep-check --accept-missing-deps-risk", file=sys.stderr)
    print(file=sys.stderr)
    sys.exit(EXIT_HARD_DEP_MISSING)


def resolve_claude_klabauter_root(repo_root: Path, args: Args) -> tuple[Path, str]:
    """Resolve CLAUDE_KLABAUTER_ROOT and describe the source used: --claude-klabauter-root flag
    -> CLAUDE_KLABAUTER_ROOT env -> repo-root auto-discovery. This resolution feeds
    BOTH dependency provisioning (which pyproject.toml / sys.path tree to
    read) and registration/verification — Review: code-reviewer 2026-07-21
    Finding 7 (P2): --claude-klabauter-root previously redirected registration/
    verification but was silently ignored by dependency provisioning, which
    always derived from the script's own on-disk location."""
    if args.claude_klabauter_root:
        return Path(args.claude_klabauter_root), "--claude-klabauter-root flag"
    if os.environ.get("CLAUDE_KLABAUTER_ROOT"):
        return Path(os.environ["CLAUDE_KLABAUTER_ROOT"]), "CLAUDE_KLABAUTER_ROOT env var"
    return repo_root, "git-root auto-discovery"


def _discover_klabauter_root(repo_root: Path, plugin_root: str | None) -> str | None:
    """Discover an existing klabauter checkout so a `claude-klabauter` install
    can auto-arm DR-132's published-mirror key (AC1) — NEVER guess one
    (AC2). Each candidate must exist on disk AND contain `coordinator_core/`
    — the same usability predicate `_resolve_published_engine` applies.
    Checked in this order, first hit wins:

      1. an existing `repos.claude_klabauter` registry value, PROVIDED it
         still passes the same validity check below (dir exists AND
         contains `coordinator_core/`) — the common case, and why seeding
         is idempotent (AC4): if this hits, the value written back is the
         value already registered, never a different one. A stale/invalid
         existing value does NOT hit here: "existing" in AC4 means a value
         that still resolves. Such a value falls through to steps 2/3 and
         is self-healed to a working path — deliberate (an unreachable
         pointer is not a value worth preserving), asserted by
         `test_discover_klabauter_root_rejects_candidate_without_
         coordinator_core` in scripts/test_setup.py — Review:
         code-reviewer 2026-08-12 (P2): AC4's wording and this docstring
         previously did not distinguish "any existing value" from "valid
         existing value".
      2. an existing `publish.mirrors.claude_klabauter.path` registry
         value — a real and likely signal (observed live 2026-08-12: this
         box had it set while the resolution key was absent).
      3. the conventional sibling layout beside the claude-klabauter checkout
         (`../claude-klabauter`).

    No hit -> None, and the caller writes nothing (AC2) — a claude-klabauter
    developer with no klabauter clone is a legitimate configuration.
    """
    from coordinator_core.install._shared import ml_get

    candidates = [
        ml_get("repos.claude_klabauter", plugin_root=plugin_root),
        ml_get("publish.mirrors.claude_klabauter.path", plugin_root=plugin_root),
        str(repo_root.parent / "claude-klabauter"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        candidate_path = Path(candidate)
        if candidate_path.is_dir() and (candidate_path / "coordinator_core").is_dir():
            return str(candidate_path)
    return None


def register_claude_klabauter_root(
    claude_klabauter_root_resolved: Path, claude_klabauter_root_source: str, repo_root: Path, args: Args
) -> Path:
    """AUTHORITATIVE idempotent registration, branched on WHICH repo this
    script is installing (`resolve_repo_identity` — this file is both
    claude-klabauter's and claude-klabauter's standalone installer, see module
    docstring):

      - claude-klabauter: machine-local set repos.claude_klabauter AND
        machine-local set engine.working_repos.claude_klabauter, both to the
        same resolved value — unchanged from before this branch existed.
        ALSO auto-arms DR-132's two-tier gate: when a klabauter checkout is
        discoverable (`_discover_klabauter_root`; never guessed), a THIRD
        write seeds machine-local set repos.claude_klabauter to the
        discovered path (AC1). No discoverable checkout -> that key is left
        untouched, not an error (AC2). PM ruling 2026-08-12: a claude-klabauter
        developer's machine is auto-armed for the dual-boot; a klabauter
        install user must never encounter the concept (see the branch
        below).
      - claude-klabauter: machine-local set repos.claude_klabauter ONLY —
        neither claude_klabauter key is written, and the dual-boot auto-arm
        above does NOT apply here — this branch is unchanged in behaviour
        and output. Per the agreed cross-repo contract
        (cross-repo/inbox/2026-08-05-example-doctrine-repo-em-klabauter-location-
        belongs-in-the-registry-not-a-pointer-file.md), the registry
        carries the published engine's location and an absent key makes a
        consumer fall open to the live-tree rung — writing a claude_klabauter
        key from a klabauter clone would classify the *published* engine as
        a *working repo*, inverting that model.
      - neither identity resolvable: fail loud (exit 95,
        EXIT_REPO_IDENTITY_UNRESOLVED) — a wrong guess here poisons the
        working-repo discriminant, which is worse than not registering.

    `claude_klabauter_root_resolved`/`claude_klabauter_root_source` come from `resolve_claude_klabauter_root`
    (flag -> env -> repo-root ladder), resolved once in `main` and shared with
    dependency provisioning. `repo_root` (the script's own on-disk location)
    is used for the coordinator-claude sibling-dir default probe AND as the
    identity-resolution root — a `--claude-klabauter-root` override may point somewhere
    with no sibling layout (or AGENTS.md/manifest) at all, but the actual
    on-disk checkout running this script still does.

    `coord_path`/`plugin_root` below are resolved ONCE, before the identity
    branch, and shared by both `_discover_klabauter_root` (its registry
    reads) and the later `resolve_machine_local_cli` call — not independent
    resolutions, even though unused on the `claude-klabauter` branch until
    that later call (no observable effect there — Review: code-reviewer
    2026-08-12 nit; confirmed by
    `test_register_claude_klabauter_root_klabauter_identity_never_calls_discover`).

    Guard (both identities): if machine-local is absent (coordinator-claude
    hard-dep not installed), fail loud (exit 90) UNLESS the --skip-dep-check +
    --accept-missing-deps-risk override pair was supplied — in which case
    `check_coordinator_claude_dep` was never called (see `main`) and this is
    the operator's already-accepted risk surfacing again downstream, so
    degrade gracefully instead (advisory, exit success; no key is
    registered). Fail loud ONLY when machine-local IS present but
    registration fails, or when the override pair is absent — that contract
    covers every key for the resolved identity identically: neither
    `engine.working_repos.claude_klabauter` nor the auto-armed
    `repos.claude_klabauter` is a separate registration with its own
    fallback — each is a further write inside the same guard.

    `engine.working_repos.*` is example-doctrine-repo's key-namespace (schema authored on their
    plane, `machine-local-registry.md` §324); our half is this install-time
    write of our own key, nothing else. See
    state/memo-outbox/sent/working-repos-adopted-count-confirmed-12-not-13.md
    for why `repos.claude_klabauter` alone is not the working-repo signal.
    """
    # Review: code-reviewer 2026-07-21 Finding 3 (P1) — resolved via the
    # canonical, Windows-hardened `resolve_machine_local_cli` (which knows to
    # prefer a `templates/bin/_machine_local.py` python shim, and to avoid the
    # extension-less `bin/machine-local` shim on Windows, WinError 193)
    # instead of a naive `shutil.which` + bare subprocess.
    from coordinator_core.install._shared import resolve_machine_local_cli

    coord_path, _ = _resolve_coordinator_claude_root(repo_root, args)
    plugin_root = _resolve_plugin_root_for_machine_local(coord_path)
    plugin_root_str = str(plugin_root) if plugin_root else None

    identity = resolve_repo_identity(repo_root)
    if identity == "claude-klabauter":
        key_values = {"repos.claude_klabauter": str(claude_klabauter_root_resolved)}
    elif identity == "claude-klabauter":
        key_values = {
            "repos.claude_klabauter": str(claude_klabauter_root_resolved),
            "engine.working_repos.claude_klabauter": str(claude_klabauter_root_resolved),
        }
        discovered_klabauter = _discover_klabauter_root(repo_root, plugin_root_str)
        if discovered_klabauter:
            key_values["repos.claude_klabauter"] = discovered_klabauter
    else:
        print(file=sys.stderr)
        print(
            f"ERROR [hard] cannot determine repo identity at {repo_root} "
            f"(exit {EXIT_REPO_IDENTITY_UNRESOLVED}).",
            file=sys.stderr,
        )
        print(
            "  This script installs BOTH claude-klabauter and claude-klabauter, and each",
            file=sys.stderr,
        )
        print(
            "  needs a different machine-local registration — neither identity's",
            file=sys.stderr,
        )
        print(
            "  positive marker (repo_root/AGENTS.md for claude-klabauter, "
            "docs/install/agent-install-manifest.json's repo_id for claude-klabauter) was found.",
            file=sys.stderr,
        )
        print("  Guessing here would write a wrong key into the working-repo discriminant, so", file=sys.stderr)
        print("  this is not overridable via --skip-dep-check/--accept-missing-deps-risk.", file=sys.stderr)
        print(f"  Checked: {repo_root}", file=sys.stderr)
        sys.exit(EXIT_REPO_IDENTITY_UNRESOLVED)

    keys = tuple(key_values)
    keys_desc = " + ".join(keys)
    print()
    print(f"--- Registration ({identity}): {keys_desc} ---")
    print(f"CLAUDE_KLABAUTER_ROOT source: {claude_klabauter_root_source}")
    print(f"CLAUDE_KLABAUTER_ROOT resolved: {claude_klabauter_root_resolved}")

    machine_local_argv = resolve_machine_local_cli(plugin_root_str)
    if machine_local_argv is None:
        override_pair = args.skip_dep_check and args.accept_risk
        if not override_pair:
            print(file=sys.stderr)
            print(f"ERROR [hard] machine-local not found — coordinator-claude absent (exit {EXIT_HARD_DEP_MISSING}).", file=sys.stderr)
            print(f"  coordinator-claude is a HARD dep (PM ruling 2026-08-03): {keys_desc}", file=sys.stderr)
            print("  cannot be registered without it.", file=sys.stderr)
            print("  To proceed anyway, accept the risk explicitly (both flags together):", file=sys.stderr)
            print("    --skip-dep-check --accept-missing-deps-risk", file=sys.stderr)
            sys.exit(EXIT_HARD_DEP_MISSING)
        print()
        print("[ADVISORY] machine-local not found — coordinator-claude absent.")
        print(f"  {keys_desc} registration skipped (--skip-dep-check --accept-missing-deps-risk accepted).")
        print("  When coordinator-claude is installed, register with:")
        for key in keys:
            print(f"    machine-local set {key} {key_values[key]}")
        return claude_klabauter_root_resolved

    # machine-local IS present -> coordinator-claude is installed; the example-doctrine-repo command
    # veneers resolve CLAUDE_KLABAUTER_ROOT through this key to locate coordinator_core. Fail
    # loud so a missing key surfaces at install time rather than breaking the
    # veneers later. Every key in `keys` sits inside the same guard — a failure
    # to write any one of them is a registration failure under this contract.
    for key in keys:
        value = key_values[key]
        try:
            proc = subprocess.run(
                machine_local_argv + ["set", key, value],
                timeout=15,
                **_NO_CONSOLE,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(file=sys.stderr)
            print(f"ERROR: 'machine-local set {key}' failed to launch: {exc}", file=sys.stderr)
            print(f"  Tried to register: {value}", file=sys.stderr)
            print("  Remediation: run manually:", file=sys.stderr)
            print(f"    machine-local set {key} {value}", file=sys.stderr)
            sys.exit(1)
        if proc.returncode != 0:
            print(file=sys.stderr)
            print(f"ERROR: 'machine-local set {key}' failed.", file=sys.stderr)
            print(f"  Tried to register: {value}", file=sys.stderr)
            print("  Remediation: run manually:", file=sys.stderr)
            print(f"    machine-local set {key} {value}", file=sys.stderr)
            sys.exit(1)
        print(f"PASS [registration] {key} = {value}")
    return claude_klabauter_root_resolved


def verify_coordinator_core_importable(claude_klabauter_root_resolved: Path, engine_py: str, import_names: list[str]) -> None:
    """Verification — coordinator_core importable from CLAUDE_KLABAUTER_ROOT.

    Uses engine_py (the interpreter dependency provisioning resolved above —
    the machine interpreter on a successful machine-level install, or the
    fallback venv's interpreter otherwise). If this import fails, the repo
    checkout is incomplete, CLAUDE_KLABAUTER_ROOT is wrong, or a declared dependency is
    missing under engine_py.
    """
    print()
    print("--- Verification: coordinator_core importable ---")

    proc = subprocess.run(
        [engine_py, "-c", "import coordinator_core"],
        cwd=str(claude_klabauter_root_resolved),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **_NO_CONSOLE,
    )
    if proc.returncode != 0:
        print(file=sys.stderr)
        print("FAIL: coordinator_core is not importable from CLAUDE_KLABAUTER_ROOT.", file=sys.stderr)
        print(f"  CLAUDE_KLABAUTER_ROOT: {claude_klabauter_root_resolved}", file=sys.stderr)
        print(f"  Interpreter: {engine_py}", file=sys.stderr)
        print("  Remediation:", file=sys.stderr)
        print("    1. Ensure claude-klabauter is fully cloned — coordinator_core/ must exist at:", file=sys.stderr)
        print(f"       {claude_klabauter_root_resolved}/coordinator_core/__init__.py", file=sys.stderr)
        print("    2. If CLAUDE_KLABAUTER_ROOT is wrong, re-run with the correct root:", file=sys.stderr)
        print("       python3 scripts/setup.py --claude-klabauter-root /path/to/claude-klabauter", file=sys.stderr)
        print(f"    3. If {' '.join(import_names)} are missing under {engine_py}, re-run this script —", file=sys.stderr)
        print("       dependency provisioning above should have installed them.", file=sys.stderr)
        sys.exit(1)
    print(f"PASS [verification] coordinator_core importable from {claude_klabauter_root_resolved} ({engine_py})")


def run_health_probe(claude_klabauter_root_resolved: Path, engine_py: str, agent_mode: bool) -> bool:
    """Verification — post-install health probe (bin/claude-klabauter-doctor-probe.py).

    Runs the out-of-band post-install health probe in --step-zero mode.
    Non-fatal for `severity: "advisory"` probes: the probe's own output
    carries per-probe remediation guidance and a failure there is a nudge,
    not a break. A `severity: "hard"` probe failing (e.g. `claude-klabauter.root.
    resolve`) is different in kind — it means the thing this installer
    exists to set up (a resolvable CLAUDE_KLABAUTER_ROOT the rest of the tool chain
    can use) did not actually happen, so THIS is the check that must be
    able to fail. Returns True iff at least one hard-severity probe
    reported a non-"pass" status; the caller decides what a hard failure
    means for its own exit code (§ `main()`'s `EXIT_HEALTH_PROBE_HARD_
    FAILURE`) — this function's own job is only to detect and surface it,
    never to swallow it into an indistinguishable WARN the way every
    severity used to be treated before this fix.
    """
    print()
    print("--- Verification: post-install health probe ---")

    probe = claude_klabauter_root_resolved / "bin" / "claude-klabauter-doctor-probe.py"
    if not probe.is_file():
        print("[ADVISORY] bin/claude-klabauter-doctor-probe not found at expected path — skipping post-install health check.")
        print("  Run 'python bin/claude-klabauter-doctor-probe.py --triage' after setup completes.")
        return False

    # Invoked directly via engine_py rather than the bin/claude-klabauter-doctor-probe
    # wrapper (retired in C7) — this honors the fallback venv's dependency
    # provisioning resolved above.
    proc = subprocess.run(
        [engine_py, str(probe), "--step-zero"],
        capture_output=True, text=True,
        **_NO_CONSOLE,
    )
    probe_output = proc.stdout + proc.stderr

    if agent_mode:
        # Machine consumers depend on the raw NDJSON envelope — emit unchanged.
        print(probe_output, end="")
    else:
        # Human-readable summary for the interactive installer: one line per
        # probe (status + probe name); full detail/remediation printed only
        # for probes that are not "pass". Parse defensively — a line that is
        # not valid JSON (or lacks the expected keys) is passed through
        # verbatim rather than swallowed, so a formatting failure never hides
        # probe output.
        for line in probe_output.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                status = obj["status"]
                name = obj["name"]
            except (json.JSONDecodeError, KeyError, TypeError):
                print(line)
                continue
            print(f"{status.upper()}  {name}")
            if status != "pass":
                print(f"    detail:      {obj.get('detail', '')}")
                print(f"    remediation: {obj.get('remediation', '')}")

    # Hard-failure detection is intentionally OUTSIDE the agent_mode/human-mode
    # branch above — it must fire whether or not this call is printing a
    # human summary, so an agent-mode invocation (which skips the per-line
    # human loop entirely) does not silently lose the one signal this fix
    # exists to surface. Re-parses `probe_output` a second time rather than
    # threading a flag out of the loop above, since that loop's own `except`
    # arm intentionally passes malformed lines through unchanged instead of
    # raising — this pass must independently tolerate the same malformed
    # lines without depending on the other loop having run at all.
    hard_failure = False
    for line in probe_output.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            if obj.get("severity") == "hard" and obj.get("status") != "pass":
                hard_failure = True
        except (json.JSONDecodeError, TypeError):
            continue

    if proc.returncode != 0:
        print(file=sys.stderr)
        if hard_failure:
            print(
                "ERROR: post-install health probe reported a HARD-severity failure — "
                "see output above for remediation.",
                file=sys.stderr,
            )
        else:
            print("WARN: post-install health probe reported failures — see output above for remediation.", file=sys.stderr)
        print("  Run 'python bin/claude-klabauter-doctor-probe.py --triage' for guided remediation.", file=sys.stderr)
        # Advisory-only failures remain non-fatal here (coordinator_core is
        # importable; the hard gate above already passed) -- a hard-severity
        # failure's fatality is the CALLER's decision (see EXIT_HEALTH_PROBE_
        # HARD_FAILURE), not this function's, since this function's contract
        # is detection, not exit-code policy.

    return hard_failure


def install_precommit_hook(repo_root: Path, engine_py: str, agent_mode: bool) -> None:
    """Best-effort install-chain step: wires claude-klabauter's own
    `.git/hooks/pre-commit` gate chain via the
    `coordinator_core.ops.install_claude_klabauter_precommit_hook` op (through its
    `coordinator/bin/install-claude-klabauter-precommit-hook.py` CLI trampoline).

    Non-fatal by design, mirroring `run_health_probe`'s ADVISORY shape: a
    hook-install failure must never abort the rest of setup — the op's own
    identity guard already makes this a clean no-op skip on any checkout
    that isn't claude-klabauter (relevant for a `--claude-klabauter-root` pointing
    elsewhere), and the op itself is idempotent, so re-running setup never
    duplicates gate blocks.
    """
    print()
    print("--- Install: pre-commit gate chain ---")

    cli = repo_root / "coordinator" / "bin" / "install-claude-klabauter-precommit-hook.py"
    if not cli.is_file():
        print("[ADVISORY] coordinator/bin/install-claude-klabauter-precommit-hook.py not found — skipping pre-commit gate install.")
        return

    proc = subprocess.run(
        [engine_py, str(cli), str(repo_root)],
        capture_output=True, text=True,
        **_NO_CONSOLE,
    )
    output = (proc.stdout + proc.stderr).strip()
    if not agent_mode and output:
        print(output)
    if proc.returncode != 0:
        print(
            "[ADVISORY] pre-commit gate install reported a non-zero exit — hook may not be installed.",
            file=sys.stderr,
        )
        print(f"  Re-run manually: {engine_py} {cli} {repo_root}", file=sys.stderr)
        # Non-fatal: setup must still complete even if this step failed.


def install_bin_forwarders(repo_root: Path, engine_py: str, claude_klabauter_root_resolved: Path, args: Args) -> None:
    """Best-effort install-chain step: lands the `<settings-home>/bin`
    agent-helper forwarders (`coordinator_core.install.substrate`'s
    `_install_bin_resolvers` leg) via that module's own `--setup-only` CLI
    mode.

    Root-cause fix, 2026-08-07 (`check-auto-memory-drained` un-runnable at
    its documented settings-home invocation): this script's Responsibilities
    1-6 never included the bin-forwarder install at all -- Responsibility 5
    (health probe) reads `bin/claude-klabauter-doctor-probe.py` but never WRITES the
    forwarder set, and `_install_bin_resolvers` (substrate.py) was reached
    ONLY by the separate `install-maximalist` chain (`/coordinator:setup`).
    A box whose only "reinstall" has ever been `scripts/setup.py` therefore
    never gets `coordinator/bin/*` forwarded into settings-home/bin at all --
    any correctly-declared entry there, not just this one file. Confirmed
    live: `check-auto-memory-drained.py` (added 2026-07-31, `4123af7d4`) was
    absent from settings-home/bin despite `python3 scripts/setup.py
    --i-am-agent` having been run same-day per
    `state/audits/2026-08-07-claude-klabauter-install-dogfood-friction.md` line 14; the
    two forwarders that WERE present (`lessons-outbox-drain`,
    `probe-memory-headroom`) both predate that gap (added 2026-07-22 bulk
    adoption, `8a28a6cac`) and are residue of an earlier `install-maximalist`
    run, not evidence this script ever reached the leg.

    `--setup-only` (substrate.py's own CLI flag, previously wired to nothing
    but `install-maximalist`'s full `run(setup_only=False, ...)` call) is the
    right-sized leg here: it lands the machine-local + bin-resolver substrate
    (Steps 1-3, C10a venv/whoami, seed-wikis) and explicitly skips the
    heavier fnm/Windows-machine-env steps (`_fnm_step`, `_windows_health_steps`)
    that belong to the full maximalist chain, not this standalone installer's
    scope (see module docstring's Negative-spec on `--with-test-deps` for the
    same "engine, not the dev loop" boundary).

    Best-effort/advisory, mirroring `install_precommit_hook`'s shape: a
    substrate failure here must not abort the rest of setup (the same
    reasoning as that function's own docstring), and this whole step is
    skipped under `--register-only` (no coordinator-claude/plugin-root
    resolution attempted in that mode, matching the other post-registration
    steps it sits beside in `main`).
    """
    print()
    print("--- Install: settings-home bin/ forwarders (coordinator/bin/*) ---")

    coord_path, coord_source = _resolve_coordinator_claude_root(repo_root, args)
    if coord_source.is_publish_mirror_rejected:
        print("[ADVISORY] coordinator-claude root resolved to a publish mirror — skipping bin-forwarder install.", file=sys.stderr)
        return
    plugin_root = _resolve_plugin_root_for_machine_local(coord_path)
    if plugin_root is None or not (plugin_root / "templates").is_dir():
        print(
            f"[ADVISORY] coordinator-claude plugin root not resolvable from {coord_path} "
            "(no templates/ dir) — skipping bin-forwarder install.",
            file=sys.stderr,
        )
        return

    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    env["CLAUDE_KLABAUTER_ROOT"] = str(claude_klabauter_root_resolved)

    # Review: code-reviewer 2026-08-07 Finding 2 (P2) — mirror
    # install_precommit_hook's try/except-around-subprocess.run shape so a
    # child-spawn failure (transient engine_py unavailability, OSError/
    # PermissionError, a locked/broken interpreter path on Windows)
    # downgrades to an ADVISORY like every other failure branch in this
    # function, instead of propagating and aborting the rest of setup.
    try:
        proc = subprocess.run(
            [engine_py, "-m", "coordinator_core.install.substrate", "--setup-only"],
            cwd=str(claude_klabauter_root_resolved),
            env=env,
            capture_output=True, text=True,
            **_NO_CONSOLE,
        )
    except OSError as exc:
        print(
            f"[ADVISORY] settings-home bin/ forwarder install could not even be spawned ({exc}) — "
            "some coordinator/bin/ CLIs may be unreachable at their documented settings-home path.",
            file=sys.stderr,
        )
        print(f"  Re-run manually: {engine_py} -m coordinator_core.install.substrate --setup-only "
              f"(CLAUDE_PLUGIN_ROOT={plugin_root})", file=sys.stderr)
        return
    output = (proc.stdout + proc.stderr).strip()
    if not args.agent_mode and output:
        print(output)
    if proc.returncode != 0:
        print(
            "[ADVISORY] settings-home bin/ forwarder install reported a non-zero exit — "
            "some coordinator/bin/ CLIs may be unreachable at their documented settings-home path.",
            file=sys.stderr,
        )
        print(f"  Re-run manually: {engine_py} -m coordinator_core.install.substrate --setup-only "
              f"(CLAUDE_PLUGIN_ROOT={plugin_root})", file=sys.stderr)
        # Non-fatal: setup must still complete even if this step failed.
        return

    # Review: code-reviewer 2026-08-07 Finding 3 (P2) — a 0 exit code alone
    # doesn't prove a forwarder actually landed on disk (the exact
    # "verifier reporting success while looking at nothing" shape this
    # commit exists to fix); cheaply confirm settings-home/bin actually
    # received at least one forwarder rather than trusting the child's exit
    # code unconditionally.
    try:
        from coordinator_core._settings_home import settings_home

        bin_dir = settings_home() / "bin"
        landed = bin_dir.is_dir() and any(bin_dir.iterdir())
    except Exception:
        landed = None
    if landed is False:
        print(
            "[ADVISORY] settings-home bin/ forwarder install exited 0 but "
            f"{bin_dir} is empty or missing — no coordinator/bin/ CLIs appear "
            "to have landed.",
            file=sys.stderr,
        )
        print(f"  Re-run manually: {engine_py} -m coordinator_core.install.substrate --setup-only "
              f"(CLAUDE_PLUGIN_ROOT={plugin_root})", file=sys.stderr)


def _derive_identity_hints(repo_root: Path) -> dict[str, str]:
    """Best-effort, non-interactive derivation of hints for the generated
    `.percolate-identity` template: the operator's git `user.name`, the
    GitHub org slug parsed from the `origin` remote URL, and this machine's
    hostname slug. Any hint that cannot be derived falls back to an explicit
    placeholder token (`YOUR_NAME_HERE` etc.) rather than an empty string, so
    the generated template never silently ships a blank pattern.

    Never raises: every probe is best-effort and swallows its own failure,
    since a broken git config or an unresolvable hostname must not block the
    rest of setup."""
    hints = {
        "author_name": "YOUR_NAME_HERE",
        "org_slug": "your-github-org",
        "hostname_slug": "your-machine-hostname",
    }

    try:
        proc = subprocess.run(
            ["git", "config", "--get", "user.name"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            **_NO_CONSOLE,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            hints["author_name"] = proc.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass

    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            **_NO_CONSOLE,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            m = re.search(r"[:/]([A-Za-z0-9_.-]+)/[A-Za-z0-9_.-]+?(?:\.git)?/?$", proc.stdout.strip())
            if m:
                hints["org_slug"] = m.group(1)
    except (OSError, subprocess.TimeoutExpired):
        pass

    try:
        import socket

        host = socket.gethostname().strip()
        if host:
            hints["hostname_slug"] = re.sub(r"[^A-Za-z0-9_-]", "-", host.split(".")[0].lower())
    except OSError:
        pass

    return hints


def _percolate_identity_template(hints: dict[str, str]) -> str:
    """Render the `.percolate-identity` template body.

    Mirrors the pinned shape sourced by `publish.sh` at startup
    (`PERSONAL_EXPECTED_PATTERNS` / `PERSONAL_REVIEW_PATTERNS` /
    `PERSONAL_ALLOW_TOKENS` bash arrays) — GENERATED per machine from
    best-effort hints, never shipped as tracked config: this file's entire
    purpose is to hold the operator's real identity values as INPUT to
    depersonalization, so committing a populated copy would publish exactly
    the strings it exists to scrub.

    Spec backlink: docs/plans/2026-07-31-claude-klabauter-oss-release.md § C12
    """
    return (
        "#!/bin/bash\n"
        "# .percolate-identity — per-operator identity tokens for publish.sh audit.\n"
        "# GENERATED by scripts/setup.py — gitignored, machine-specific. Sourced by\n"
        "# publish.sh at startup.\n"
        "#\n"
        "# EDIT ME: entries below are best-effort derived (git config / hostname) or\n"
        "# placeholder-shaped where derivation was not possible non-interactively.\n"
        "# Review every array before trusting a publish audit against it.\n"
        "\n"
        "# Tokens the audit SKIPS (expected, legit) — e.g. author name + org slug for\n"
        "# intentional attribution.\n"
        "PERSONAL_EXPECTED_PATTERNS=(\n"
        f"  '{hints['author_name']}'\n"
        f"  '{hints['org_slug']}'\n"
        ")\n"
        "\n"
        "# Patterns the audit FLAGS for review — machine-runtime identity that must\n"
        "# not ship. Add every machine slug, custom hostname, or local-only token you\n"
        "# use.\n"
        "PERSONAL_REVIEW_PATTERNS=(\n"
        f"  '\\b{hints['hostname_slug']}\\b'\n"
        ")\n"
        "\n"
        "# Allow-forms for the bare-identifier scan: legit KEEP shapes containing your\n"
        "# org slug (public org/repo slugs, install URLs).\n"
        "PERSONAL_ALLOW_TOKENS=(\n"
        f"  '{hints['org_slug']}'\n"
        ")\n"
    )


def ensure_percolate_identity(settings_home_path: Path, repo_root: Path) -> tuple[Path, str]:
    """Idempotent generator: create `<settings-home>/.percolate-identity` from
    a derived template when absent. NEVER overwrites an existing file — it
    may hold the operator's reviewed, hand-edited identity-audit config, and
    clobbering it on a re-run would silently discard that review.

    Returns (path, "created"|"exists").
    """
    target = settings_home_path / ".percolate-identity"
    if target.exists():
        return target, "exists"
    hints = _derive_identity_hints(repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_percolate_identity_template(hints), encoding="utf-8")
    return target, "created"


def install_percolate_identity(repo_root: Path, claude_klabauter_root_resolved: Path) -> None:
    """Best-effort install-chain step: generates the settings-home
    `.percolate-identity` publish-audit config on a fresh machine, where it
    would otherwise be absent entirely (DR-046) — a clean install cannot run
    `publish.sh` without it. Non-fatal by design, mirroring
    `run_health_probe`/`install_precommit_hook`'s ADVISORY shape.
    """
    print()
    print("--- Install: .percolate-identity (publish audit config) ---")

    if str(claude_klabauter_root_resolved) not in sys.path:
        sys.path.insert(0, str(claude_klabauter_root_resolved))
    from coordinator_core._settings_home import settings_home

    try:
        settings_home_path = settings_home()
    except RuntimeError as exc:
        print(f"[ADVISORY] cannot resolve settings-home — skipping .percolate-identity: {exc}", file=sys.stderr)
        return

    try:
        path, status = ensure_percolate_identity(settings_home_path, repo_root)
    except OSError as exc:
        print(f"[ADVISORY] could not write .percolate-identity: {exc}", file=sys.stderr)
        return

    if status == "created":
        print(f"PASS [percolate-identity] created template at {path} — edit before running publish.")
    else:
        print(f"PASS [percolate-identity] already exists at {path} — left untouched.")


def install_machine_identity(repo_root: Path, claude_klabauter_root_resolved: Path, args: Args) -> None:
    """Best-effort install-chain step: resolve `coordinator.machine_slug` /
    `coordinator.contributor_slug` ONCE at install time and write them to the
    machine-local registry — "pay the find-out-where-we-are tax once" (PM
    directive, 2026-08-05 de-bash spawn-amplification hardening) so
    `coordinator_core.machine_resolver.compute_machine`/`compute_contributor`
    can serve a local registry read at runtime instead of a `git config
    user.email` subprocess spawn on the session-start / daily-branch /
    commit-ceremony hot path.

    ADVISORY, non-fatal (mirrors `install_precommit_hook`/
    `install_percolate_identity`'s shape) — a missing machine-local CLI, or a
    value that cannot be resolved live yet (no git identity configured),
    must fall through to `machine_resolver`'s own live-resolution fallback at
    runtime; this function deliberately WRITES NOTHING in that case rather
    than a wrong-but-confident placeholder (an "unknown" live-resolution
    result is skipped, never persisted as if it were real). Idempotent:
    re-running just re-sets the same key(s) to the freshly-resolved live
    value(s) — `machine-local set` has no accumulation/duplication mode to
    guard against.
    """
    print()
    print("--- Install: coordinator.machine_slug / coordinator.contributor_slug (identity registry) ---")

    if str(claude_klabauter_root_resolved) not in sys.path:
        sys.path.insert(0, str(claude_klabauter_root_resolved))
    from coordinator_core import machine_resolver
    from coordinator_core.install._shared import resolve_machine_local_cli

    coord_path, _ = _resolve_coordinator_claude_root(repo_root, args)
    plugin_root = _resolve_plugin_root_for_machine_local(coord_path)
    machine_local_argv = resolve_machine_local_cli(str(plugin_root) if plugin_root else None)
    if machine_local_argv is None:
        print("[ADVISORY] machine-local not found — skipping identity registry population.")
        print("  compute_machine()/compute_contributor() will fall back to live resolution")
        print("  (hostname / git config user.email) at runtime instead.")
        return

    for key, value in (
        ("coordinator.machine_slug", machine_resolver.compute_machine_live()),
        ("coordinator.contributor_slug", machine_resolver.compute_contributor_live()),
    ):
        if not value or value == "unknown":
            print(
                f"[ADVISORY] {key} — could not be resolved live yet (no git identity/"
                "hostname available) — leaving unset; runtime live-fallback will handle it."
            )
            continue
        try:
            proc = subprocess.run(
                machine_local_argv + ["set", key, value],
                timeout=15,
                **_NO_CONSOLE,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"[ADVISORY] '{key}' registry write failed to launch: {exc}", file=sys.stderr)
            print("  Runtime will fall back to live resolution instead.", file=sys.stderr)
            continue
        if proc.returncode != 0:
            print(f"[ADVISORY] '{key}' registry write failed (exit {proc.returncode}).", file=sys.stderr)
            print("  Runtime will fall back to live resolution instead.", file=sys.stderr)
            continue
        print(f"PASS [identity] {key} = {value}")


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
    except ArgError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.check:
        print("check mode: setup.py is present and executable")
        return 0

    if args.help:
        print(HELP_TEXT, end="")
        return 0

    # Override flag pair gate (exit 93 on single-flag violation — contract § exit-code 93)
    if args.skip_dep_check != args.accept_risk:
        print(
            "ERROR (exit 93): Both --skip-dep-check AND --accept-missing-deps-risk must be "
            "passed together. Passing only one is not valid.",
            file=sys.stderr,
        )
        return EXIT_FLAG_PAIR_VIOLATION

    script_path = Path(__file__).resolve()
    repo_root = script_path.parent.parent
    claude_klabauter_root_resolved, claude_klabauter_root_source = resolve_claude_klabauter_root(repo_root, args)

    if not args.register_only:
        print("=== claude-klabauter setup (standalone) ===")
        print(f"Repo root: {repo_root}")
        print(f"Script:    {script_path}")
        print()
        print("--- Step Zero: system prerequisites ---")

    py = resolve_python()

    if not args.register_only:
        version_proc = subprocess.run(
            [py, "--version"], capture_output=True, text=True, **_NO_CONSOLE,
        )
        py_version = (version_proc.stdout or version_proc.stderr).strip().split()[-1]
        print(f"PASS [hard] python — {py_version} ({py})")

    engine_py, import_names = provision_deps(claude_klabauter_root_resolved, py, args.break_system_packages)

    if not args.register_only:
        handle_test_tooling(claude_klabauter_root_resolved, engine_py, args)
        print_symbols_extra_hint(claude_klabauter_root_resolved)

        if args.skip_dep_check:
            print()
            print("[SKIP] dep check bypassed (--skip-dep-check + --accept-missing-deps-risk).")
        else:
            check_coordinator_claude_dep(repo_root, args)

    claude_klabauter_root_resolved = register_claude_klabauter_root(claude_klabauter_root_resolved, claude_klabauter_root_source, repo_root, args)
    verify_coordinator_core_importable(claude_klabauter_root_resolved, engine_py, import_names)
    probe_hard_failure = run_health_probe(claude_klabauter_root_resolved, engine_py, args.agent_mode)

    if not args.register_only:
        install_bin_forwarders(repo_root, engine_py, claude_klabauter_root_resolved, args)
        install_precommit_hook(repo_root, engine_py, args.agent_mode)
        install_percolate_identity(repo_root, claude_klabauter_root_resolved)
        install_machine_identity(repo_root, claude_klabauter_root_resolved, args)

    print()
    if probe_hard_failure:
        print("=== claude-klabauter setup: complete, but a HARD-severity health probe failed ===")
    else:
        print("=== claude-klabauter setup: complete ===")
    if not args.register_only:
        print("  For the full agentic chain-walk: invoke /coordinator:setup from Claude Code.")
    if probe_hard_failure:
        return EXIT_HEALTH_PROBE_HARD_FAILURE
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
