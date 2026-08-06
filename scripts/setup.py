#!/usr/bin/env python3
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
Resolver reference: coordinator/lib/coordinator-claude-klabauter-root.sh (the CLAUDE_KLABAUTER_ROOT
  ladder the example-doctrine-repo command veneers + coordinator_core.invoke entrypoint share)

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
  --coordinator-root flag -> COORDINATOR_CLAUDE_ROOT env -> sibling-dir default,
  so a packaging installer (e.g. Example-os-repo) can inject the location instead of
  relying on side-by-side git clone placement.
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


def _resolve_coordinator_claude_root(repo_root: Path, args: Args) -> tuple[Path, str]:
    """Resolve the coordinator-claude sibling root and describe the source
    used: --coordinator-root flag -> COORDINATOR_CLAUDE_ROOT env -> sibling-
    dir default. Shared by `check_coordinator_claude_dep` and
    `register_claude_klabauter_root` so both resolve the SAME candidate root
    regardless of whether the (hard) dep-check ran (e.g. --skip-dep-check) —
    Review: code-reviewer 2026-07-21 Finding 3 (P1), extracted so
    `register_claude_klabauter_root` can resolve a plugin_root for
    `resolve_machine_local_cli` without duplicating this ladder."""
    if args.coordinator_root:
        return Path(args.coordinator_root), "--coordinator-root flag"
    if os.environ.get("COORDINATOR_CLAUDE_ROOT"):
        return Path(os.environ["COORDINATOR_CLAUDE_ROOT"]), "COORDINATOR_CLAUDE_ROOT env"
    return repo_root.parent / "coordinator-claude", "sibling-dir default"


def _resolve_plugin_root_for_machine_local(coord_path: Path) -> Path | None:
    """Map a resolved coordinator-claude root onto the `plugin_root` shape
    `coordinator_core.install._shared.resolve_machine_local_cli` expects
    (the dir directly containing `templates/bin/_machine_local.py` and
    `bin/machine-local`) — a coordinator-claude root can be shaped two ways:
    (a) the OSS mirror clone, plugin_root == coord_path itself, or (b) a example-doctrine-repo
    dev-clone, where the coordinator plugin source (and its bin/) lives
    under a `coordinator/` subdir. Returns None if neither shape matches,
    in which case `resolve_machine_local_cli` falls back to an on-PATH
    lookup only."""
    if (coord_path / "coordinator" / "CLAUDE.md").is_file():
        return coord_path / "coordinator"
    if (coord_path / ".claude-plugin" / "plugin.json").is_file():
        return coord_path
    return None


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

    print(f"coordinator-claude root source: {coord_source}")
    print(f"coordinator-claude candidate: {coord_path} (source: {coord_source})")

    # functional_probe: a "coordinator-claude" root can be shaped two ways —
    # (a) the OSS mirror clone, which carries .claude-plugin/plugin.json at its
    #     root, or (b) a example-doctrine-repo dev-clone (e.g. Example-doctrine-repo), where the coordinator
    #     plugin source lives under a coordinator/ subdir and CLAUDE.md marks it
    #     (coordinator/CLAUDE.md). Both are valid, functioning coordinator-claude
    #     roots -- PASS if either probe file is present. A single hardcoded probe
    #     path here previously false-WARNed on the dev-clone shape (F9).
    if (coord_path / ".claude-plugin" / "plugin.json").is_file() or (coord_path / "coordinator" / "CLAUDE.md").is_file():
        print(f"PASS [hard] coordinator-claude — present at {coord_path}")
        return

    print(f"ERROR [hard] coordinator-claude — not found at {coord_path} (exit {EXIT_HARD_DEP_MISSING})", file=sys.stderr)
    print("  coordinator-claude is a HARD dep (PM ruling 2026-08-03): claude-klabauter is not usable", file=sys.stderr)
    print("  without it to wire into.", file=sys.stderr)
    print(file=sys.stderr)
    if coord_source == "sibling-dir default":
        print("  To install coordinator-claude:", file=sys.stderr)
        print(f"    git clone https://github.com/dbc-example-operator/coordinator-claude {coord_path}", file=sys.stderr)
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
      - claude-klabauter: machine-local set repos.claude_klabauter ONLY —
        neither claude_klabauter key is written. Per the agreed cross-repo
        contract (cross-repo/inbox/2026-08-05-example-doctrine-repo-em-klabauter-
        location-belongs-in-the-registry-not-a-pointer-file.md), the
        registry carries the published engine's location and an absent key
        makes a consumer fall open to the live-tree rung — writing a
        claude_klabauter key from a klabauter clone would classify the
        *published* engine as a *working repo*, inverting that model.
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

    Guard (both identities): if machine-local is absent (coordinator-claude
    hard-dep not installed), fail loud (exit 90) UNLESS the --skip-dep-check +
    --accept-missing-deps-risk override pair was supplied — in which case
    `check_coordinator_claude_dep` was never called (see `main`) and this is
    the operator's already-accepted risk surfacing again downstream, so
    degrade gracefully instead (advisory, exit success; no key is
    registered). Fail loud ONLY when machine-local IS present but
    registration fails, or when the override pair is absent — that contract
    covers every key for the resolved identity identically: a second key
    (`engine.working_repos.claude_klabauter`) is not a separate registration
    with its own fallback, it is a second write inside the same guard.

    `engine.working_repos.*` is example-doctrine-repo's key-namespace (schema authored on their
    plane, `machine-local-registry.md` §324); our half is this install-time
    write of our own key, nothing else. See
    state/memo-outbox/sent/working-repos-adopted-count-confirmed-12-not-13.md
    for why `repos.claude_klabauter` alone is not the working-repo signal.
    """
    identity = resolve_repo_identity(repo_root)
    if identity == "claude-klabauter":
        keys = ("repos.claude_klabauter",)
    elif identity == "claude-klabauter":
        keys = ("repos.claude_klabauter", "engine.working_repos.claude_klabauter")
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

    keys_desc = " + ".join(keys)
    print()
    print(f"--- Registration ({identity}): {keys_desc} ---")
    print(f"CLAUDE_KLABAUTER_ROOT source: {claude_klabauter_root_source}")
    print(f"CLAUDE_KLABAUTER_ROOT resolved: {claude_klabauter_root_resolved}")

    # Review: code-reviewer 2026-07-21 Finding 3 (P1) — resolved via the
    # canonical, Windows-hardened `resolve_machine_local_cli` (which knows to
    # prefer a `templates/bin/_machine_local.py` python shim, and to avoid the
    # extension-less `bin/machine-local` shim on Windows, WinError 193)
    # instead of a naive `shutil.which` + bare subprocess.
    from coordinator_core.install._shared import resolve_machine_local_cli

    coord_path, _ = _resolve_coordinator_claude_root(repo_root, args)
    plugin_root = _resolve_plugin_root_for_machine_local(coord_path)
    machine_local_argv = resolve_machine_local_cli(str(plugin_root) if plugin_root else None)
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
            print(f"    machine-local set {key} {claude_klabauter_root_resolved}")
        return claude_klabauter_root_resolved

    # machine-local IS present -> coordinator-claude is installed; the example-doctrine-repo command
    # veneers resolve CLAUDE_KLABAUTER_ROOT through this key to locate coordinator_core. Fail
    # loud so a missing key surfaces at install time rather than breaking the
    # veneers later. Every key in `keys` sits inside the same guard — a failure
    # to write any one of them is a registration failure under this contract.
    for key in keys:
        try:
            proc = subprocess.run(
                machine_local_argv + ["set", key, str(claude_klabauter_root_resolved)],
                timeout=15,
                **_NO_CONSOLE,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(file=sys.stderr)
            print(f"ERROR: 'machine-local set {key}' failed to launch: {exc}", file=sys.stderr)
            print(f"  Tried to register: {claude_klabauter_root_resolved}", file=sys.stderr)
            print("  Remediation: run manually:", file=sys.stderr)
            print(f"    machine-local set {key} {claude_klabauter_root_resolved}", file=sys.stderr)
            sys.exit(1)
        if proc.returncode != 0:
            print(file=sys.stderr)
            print(f"ERROR: 'machine-local set {key}' failed.", file=sys.stderr)
            print(f"  Tried to register: {claude_klabauter_root_resolved}", file=sys.stderr)
            print("  Remediation: run manually:", file=sys.stderr)
            print(f"    machine-local set {key} {claude_klabauter_root_resolved}", file=sys.stderr)
            print("  Reference: ~/.claude/plugins/coordinator-claude/coordinator/lib/coordinator-claude-klabauter-root.sh", file=sys.stderr)
            sys.exit(1)
        print(f"PASS [registration] {key} = {claude_klabauter_root_resolved}")
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

        if args.skip_dep_check:
            print()
            print("[SKIP] dep check bypassed (--skip-dep-check + --accept-missing-deps-risk).")
        else:
            check_coordinator_claude_dep(repo_root, args)

    claude_klabauter_root_resolved = register_claude_klabauter_root(claude_klabauter_root_resolved, claude_klabauter_root_source, repo_root, args)
    verify_coordinator_core_importable(claude_klabauter_root_resolved, engine_py, import_names)
    probe_hard_failure = run_health_probe(claude_klabauter_root_resolved, engine_py, args.agent_mode)

    if not args.register_only:
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
