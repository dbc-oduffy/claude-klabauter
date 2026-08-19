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
     is useless without DoE/coordinator-claude to wire into, it's a 100% hard dep for
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
  4b. Verification: PowerShell dialect guard ARMED state (`check_dialect_guard_armed`) —
      makes a disarmed `_dialect.py` (ImportError -> SILENT) loud at install time,
      naming the interpreter(s) probed. Non-fatal; does not arm the guard (see C2 of
      docs/plans/2026-08-17-machine-first-install-surface.md).
  5. Post-install health probe (bin/claude-klabauter-doctor-probe.py --step-zero) as best-effort.
  6. Install claude-klabauter's OWN `.git/hooks/pre-commit` gate chain (staged-rollback
     detector; see coordinator_core.ops.install_claude_klabauter_precommit_hook) as best-effort —
     skipped in --register-only mode, and a clean no-op on any checkout that isn't
     claude-klabauter itself (the op's own identity guard).
  7. Advisory: pip-install `coordinator_whoami` editable under the operator's
     `coordinator.python` general pin, when healthy and distinct from wherever it
     already imports (`provision_whoami_under_general_pin`, invoked after step 5's
     bin-forwarder install so C10a has already relocated the whoami source tree).
     This is the machine-first replacement for
     `coordinator_core.install.ensure_venv._ensure_whoami_under_general_pin`, whose
     only call site (`ensure_coordinator_venv`) is reachable solely via
     --allow-venv-fallback — see that function's docstring and this step's own for
     the full rationale. Never fatal; skipped under --register-only.

Spec backlink: pln-claude-klabauter-install-doctor-system-f-537d61 § C2
Spec backlink: pln-claude-klabauter-pure-python-shop-retire-0f8aee § C6
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
                            [--allow-venv-fallback] [--with-test-deps]
                            [--register-only] [--check] [--help]

Negative-spec:
  coordinator_core is NOT stdlib-only — pyproject.toml's [project].dependencies
  array declares its REQUIRED deps. This script DERIVES that list from
  pyproject.toml at run time (tomllib) rather than hardcoding it, so the
  provisioned set cannot drift from the declared set as deps are added/removed.
  MACHINE-FIRST (PM ruling 2026-08-17, superseding DR-307's healthy-venv
  prior-consent branch and the retired --break-system-packages flag): the
  installer enumerates the PREDICTABLE set of interpreters declared
  consumers actually resolve to (at minimum its own resolved interpreter and
  bare `python3` on PATH — see `enumerate_provisioning_candidates`) and
  plain `pip install`s the declared deps AND the claude-klabauter package itself
  (`-e .`, so `[project.scripts]` console entrypoints materialise) into
  EACH, with NO override flag ever passed. A candidate found PEP-668
  externally-managed is a DESIGNED REFUSAL (exit 96,
  EXIT_INTERPRETER_UNSUPPORTED), naming that interpreter and the consumer
  that resolves to it — never an automatic fallback and never
  --break-system-packages, which no longer exists. The settings-home
  coordinator venv (coordinator_core.install.ensure_venv) remains reachable
  ONLY via the explicit --allow-venv-fallback opt-in, and only for a
  genuine machine-level install failure that is NOT a PEP-668 refusal — a
  guarded interpreter is swapped, never fallen back from. See §
  Dependency provisioning below.
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
  pointer -> engine.working_repos.doe_claude registry key -> settings-home
  .doe-root sentinel -> sibling-dir default, so a packaging installer (e.g.
  example-os-repo) can inject the location instead of relying on side-by-side git
  clone placement. The sibling-dir default is the only rung that guesses, and
  it is existence-gated: an unverified guess comes back flagged in the source
  string, never as if it had resolved. See `_resolve_coordinator_claude_root`
  for each rung's rationale and precedence.
  Does NOT shell out to bash/PowerShell anywhere in this file — the whole
  installer is naked Python (subprocess is used only to invoke OTHER Python
  interpreters/venvs, the `machine-local` / `pip` executables, and — only
  after explicit interactive consent — `brew` to remove a guarded Homebrew
  Python, see `_offer_homebrew_removal`).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
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

# Exit-code 96: a DESIGNED REFUSAL, not a failure. The installer must provision
# the predictable set of interpreters declared consumers actually resolve to --
# including the bare `python3` DoE-claude's `hooks.json` registers hooks under --
# and one of them is externally-managed (PEP 668), so provisioning it would
# require an override this installer will never pass. Refusing IS the correct
# outcome; the remediation names a supported interpreter.
#
# WHY A RESERVED CODE RATHER THAN STDERR PROSE: DoE-claude's settings-home
# post-condition (the declare half of the 4b declare/prove split, memo
# 2026-08-17-doe-claude-em-coordinator-install-entry-resolve-from-manifest.md
# § Question 2) is phrased outcome-conditional -- provisioned, OR a designed
# refusal -- and both conform. A test can only tell those apart from a genuine
# break if the discriminator is machine-readable: prose drifts, so a test pinned
# to a phrase fails on a wording change and passes on a real break. Declared in
# `docs/install/agent-install-manifest.json`'s entry_point_contract as
# `refusal_exit_code` so the contract carries it, not just this constant.
#
# NEGATIVE SPEC: never reuse this for a genuine provisioning failure. A caller
# that cannot distinguish "refused by design, box needs a supported interpreter"
# from "the install broke" is exactly what this code exists to prevent, and
# widening it to mean both restores the ambiguity at the price of having spent a
# code on it.
#
# EMITTER: the machine-first-install-surface plan's C2 owns the raise site (that
# plan carries the PM ruling this refusal implements). Reserved and contracted
# here so C2 uses this value rather than minting a second one, and so DoE can
# write their post-condition against a literal today.
EXIT_INTERPRETER_UNSUPPORTED = 96

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
  --allow-venv-fallback               Explicit opt-in, break-glass only: on a machine-level
                                      dependency install failure that is NOT a PEP-668 refusal,
                                      fall back to the settings-home coordinator venv instead of
                                      exiting non-zero. Never reached automatically, and never
                                      reached for a PEP-668 externally-managed interpreter — that
                                      is always a designed refusal (exit 96), not a failure to
                                      fall back from.
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
        self.allow_venv_fallback = False
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
        elif tok == "--allow-venv-fallback":
            args.allow_venv_fallback = True
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


def _git_version_tuple(timeout: float = 10.0) -> tuple[int, ...] | None:
    """Behavior-verify the `git` on PATH and parse its version, or return None
    on any probe failure. Never raises, never blocks install — this repo's
    engine has no hard git-version floor, only a soft one (see caller)."""
    try:
        proc = subprocess.run(
            ["git", "--version"], timeout=timeout, capture_output=True, text=True, **_NO_CONSOLE,
        )
        if proc.returncode != 0:
            return None
        match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", proc.stdout)
        if not match:
            return None
        return tuple(int(g) for g in match.groups() if g is not None)
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return None


def check_git_version() -> None:
    """WARN-only, non-blocking: coordinator_core's commit ceremony (git_native.py)
    and the prepare-commit-msg hook both call `git interpret-trailers
    --no-divider`, which requires git >= 2.28 (2020). Both call sites already
    fail clean on an unrecognized flag (hook: swallowed by its top-level
    try/except, message left unmodified; git_native.py: the failure result is
    returned up the chain, not raised) — so this is advance notice for an
    operator on an old git, not a gate. Review: code-reviewer 2026-08-15,
    finding on commit 72c6d188980b (P2, deferred-but-tradeoff-free, folded)."""
    version = _git_version_tuple()
    if version is None:
        print("WARN [soft] git — could not determine version; commit-trailer stamping needs git >= 2.28.")
        return
    if version < (2, 28):
        print(
            f"WARN [soft] git — {'.'.join(str(v) for v in version)} found, >= 2.28 needed for "
            "commit-trailer stamping (git interpret-trailers --no-divider). Trailers will "
            "silently stop being stamped on commits; nothing else breaks."
        )
    else:
        print(f"PASS [soft] git — {'.'.join(str(v) for v in version)}")


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


@dataclass(frozen=True)
class InterpreterCandidate:
    """One member of the PREDICTABLE set of interpreters a declared
    consumer resolves to (PM ruling 2026-08-17: "'every' might be too
    strong a word, but 'predictable/likely' is straightforward") — a
    label for print output, the resolved interpreter path, and every
    consumer that resolves to it.

    `consumers` is a TUPLE, never a single string: realpath-deduping (see
    `enumerate_provisioning_candidates`) can merge two independently-named
    roles — e.g. the installer's own interpreter AND bare `python3` on
    PATH — into ONE candidate, on a box where both names resolve to the
    same file. Review (team-lead, 2026-08-17): collapsing to a single
    `consumer` field silently dropped every consumer but the first-merged
    one from BOTH the refusal output and the Homebrew-removal offer's
    pre-offer enumeration — on exactly the box where the dropped consumer
    (hooks.json's bare `python3`, running the dialect guard) is the whole
    reason this plan exists. A candidate must carry every consumer that
    resolves to it, and every print site must render all of them, not the
    first."""

    label: str
    path: str
    consumers: tuple[str, ...]


def enumerate_provisioning_candidates(installer_py: str) -> list[InterpreterCandidate]:
    """The predictable, consumer-resolved interpreter set this installer
    provisions — at minimum the installer's own resolved interpreter and
    bare `python3` on PATH (what coordinator-claude's `hooks.json`
    registers every bash-guard hook under, and what the PowerShell dialect
    guard `coordinator_core/bash_guards/_dialect.py` runs beneath — see
    `check_dialect_guard_armed`, which checks the same two interpreters for
    the same reason).

    Deduped by realpath: a box where both names resolve to the SAME
    interpreter is provisioned once, not twice — but the merge UNIONS
    every role's label and consumer text into that one candidate rather
    than keeping only the first-added role's. First-wins would silently
    drop a consumer from view exactly when dedup matters most (a shared
    realpath is common — Homebrew symlinks its versioned Cellar binary
    under both `/opt/homebrew/bin/python3` and, often, the resolved
    installer interpreter itself)."""
    order: list[str] = []
    paths: dict[str, str] = {}
    labels: dict[str, list[str]] = {}
    consumers: dict[str, list[str]] = {}

    def _add(path: str, label: str, consumer: str) -> None:
        real = os.path.realpath(path)
        if real not in paths:
            order.append(real)
            paths[real] = path
            labels[real] = []
            consumers[real] = []
        if label not in labels[real]:
            labels[real].append(label)
        if consumer not in consumers[real]:
            consumers[real].append(consumer)

    _add(
        installer_py,
        "installer interpreter",
        "scripts/setup.py's own resolved interpreter (resolve_python())",
    )

    bare_python3 = shutil.which("python3")
    if bare_python3:
        _add(
            bare_python3,
            "bare python3 on PATH",
            "hooks.json (coordinator-claude) registers every bash-guard hook "
            "under bare `python3`; the PowerShell dialect guard "
            "(coordinator_core/bash_guards/_dialect.py) runs beneath it",
        )

    return [
        InterpreterCandidate(
            label=" + ".join(labels[real]),
            path=paths[real],
            consumers=tuple(consumers[real]),
        )
        for real in order
    ]


def _is_externally_managed(interpreter: str, timeout: float = 10.0) -> bool:
    """PEP 668 marker-file probe under `interpreter` — checked BEFORE any
    install is attempted, per this chunk's ruling ("resolve interpreter ->
    verify NOT externally-managed -> install"), not inferred after the
    fact from a failed pip install's stderr. A side-effect-free stat, not
    a write attempt. Fails OPEN (False) on any probe error — the real pip
    install in `provision_deps` is still the ultimate authority, and a
    mid-install PEP-668 refusal this probe missed is still caught and
    still refuses there."""
    program = (
        "import os, sysconfig\n"
        "stdlib = sysconfig.get_path('stdlib')\n"
        "print(1 if os.path.exists(os.path.join(stdlib, 'EXTERNALLY-MANAGED')) else 0)\n"
    )
    try:
        proc = subprocess.run(
            [interpreter, "-c", program],
            timeout=timeout,
            capture_output=True,
            text=True,
            **_NO_CONSOLE,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "1"
    except (OSError, subprocess.TimeoutExpired):
        return False


def _is_homebrew_python(interpreter: str) -> bool:
    """True iff `interpreter` resolves (through symlinks) into a Homebrew
    Cellar — the guarded-by-default shape on macOS this ruling's Homebrew-
    removal offer exists for."""
    resolved = os.path.realpath(interpreter)
    return "/Cellar/python" in resolved


def _homebrew_python_formula(interpreter: str) -> str | None:
    """The Homebrew formula name (e.g. `python@3.14`) owning `interpreter`,
    parsed from its resolved Cellar path, or `None` if it is not a
    Homebrew-Cellar-rooted interpreter."""
    resolved = os.path.realpath(interpreter)
    match = re.search(r"Cellar/([^/]+)/", resolved)
    return match.group(1) if match else None


_HOMEBREW_REMOVALS_FILENAME = "homebrew-python-removals.json"


def _record_homebrew_removal(
    settings_home_path: Path, formula: str, interpreter: str, consumers: list[str]
) -> None:
    """Append-only JSON log of what `_offer_homebrew_removal` actually
    removed, so the removal is recoverable knowledge even though the
    package itself is gone — the PM ruling's "records what it removed in
    the install record" requirement. Deliberately a small, self-contained
    log file rather than routed through `coordinator_core.install.receipt`
    — that module's WriteSurfaceDeclaration/ClauseResolution machinery is
    built for the install-chain's own declared, repeatable writers; this is
    a single, rare, consent-gated destructive action with no writer of its
    own, and reusing that machinery here would be more apparatus than the
    fact warrants. Advisory: a write failure here must not fail the install
    that already happened."""
    log_path = settings_home_path / _HOMEBREW_REMOVALS_FILENAME
    try:
        existing = json.loads(log_path.read_text(encoding="utf-8")) if log_path.is_file() else []
    except (OSError, json.JSONDecodeError):
        existing = []
    if not isinstance(existing, list):
        existing = []
    existing.append(
        {
            "formula": formula,
            "interpreter": interpreter,
            "consumers": consumers,
            "removed_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"  [ADVISORY] could not write Homebrew-removal record to {log_path}: {exc}", file=sys.stderr)


def _offer_homebrew_removal(
    candidate: InterpreterCandidate, settings_home_path: Path
) -> bool:
    """Offer, interactively, to `brew uninstall` a guarded Homebrew Python
    that a declared consumer resolves to. PM-authorized 2026-08-17,
    INCLUDING on `--i-am-agent` — the offer still fires there; it does not
    require a human at an interactive terminal, only an explicit affirmative
    answer to proceed, which `--i-am-agent`'s typically-closed stdin already
    defaults to declining via the EOFError branch below.

    Defaults to NO: silence, EOF, or any non-affirmative response declines
    — an explicit affirmative token (`y`/`yes`) is required to proceed.
    Enumerates EVERY declared consumer resolving to this interpreter BEFORE
    asking (`candidate.consumers` — a realpath-deduped candidate can carry
    more than one; see `InterpreterCandidate`'s docstring for why this must
    never collapse to just the first), so the answer is informed rather
    than reflexive. Records what was removed (`_record_homebrew_removal`,
    all consumers) so the action is recoverable knowledge. Never removes an
    interpreter it did not offer on, and never removes more than the one
    formula named — no transitive/dependency cleanup.

    Returns True iff the operator explicitly affirmed AND the removal
    itself succeeded."""
    if not _is_homebrew_python(candidate.path):
        return False
    formula = _homebrew_python_formula(candidate.path)
    if formula is None:
        return False

    print()
    print(f"  {candidate.path} is a Homebrew-managed Python (formula: {formula}).")
    print("  Declared consumer(s) resolving to it:")
    for consumer in candidate.consumers:
        print(f"    - {consumer}")
    print("  This installer did not install this Python. Removing it is destructive")
    print(f"  and irreversible ('brew uninstall {formula}').")
    try:
        answer = input(f"  Uninstall Homebrew's {formula} now? [y/N]: ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        print("  Declined — leaving Homebrew Python in place.")
        return False

    try:
        proc = subprocess.run(
            ["brew", "uninstall", formula],
            timeout=120,
            capture_output=True,
            text=True,
            **_NO_CONSOLE,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  FAIL: brew uninstall failed to run: {exc}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        print(f"  FAIL: brew uninstall {formula} exited {proc.returncode}:", file=sys.stderr)
        print(f"  {proc.stdout}\n{proc.stderr}", file=sys.stderr)
        return False

    print(f"  PASS: Homebrew {formula} uninstalled.")
    _record_homebrew_removal(settings_home_path, formula, candidate.path, list(candidate.consumers))
    return True


def _engine_installed(interpreter: str, import_names: list[str]) -> bool:
    """True iff BOTH the declared deps AND the claude-klabauter package itself
    (`coordinator_core`, pip-installed as a distribution — not merely
    importable via a sys.path insert elsewhere) are present under
    `interpreter`.

    Supersedes a bare `deps_importable` fast path. After this chunk removes
    the healthy-venv prior-consent branch, "deps importable, package not
    pip-installed" is the COMMON upgrade state on every EXISTING box — the
    machine interpreter already has the dependency set provisioned from a
    prior `provision_deps` run, but no prior run ever `pip install -e .`'d
    the claude-klabauter package itself. A fast path keyed on import alone would
    report PASS and skip the install that materializes `[project.scripts]`
    console entrypoints (C3's dependency), on an exit-0 install."""
    if not deps_importable(interpreter, import_names):
        return False
    program = (
        "import importlib.metadata as m\n"
        "try:\n"
        "    m.version('coordinator_core')\n"
        "except m.PackageNotFoundError:\n"
        "    raise SystemExit(1)\n"
    )
    try:
        proc = subprocess.run(
            [interpreter, "-c", program],
            timeout=10.0,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_NO_CONSOLE,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _fallback_to_venv(
    claude_klabauter_root: Path,
    settings_home_path: Path,
    venv_dir: Path,
    venv_py: Path,
    dep_specs: list[str],
    import_names: list[str],
) -> str:
    """Provision the settings-home coordinator venv (purpose (c),
    `docs/reference/shared-fleet-venv-contract.md`) with the DECLARED
    THIRD-PARTY DEPS ONLY — deliberately NOT `-e <claude_klabauter_root>`. The
    contract doc is explicit that an editable `coordinator_core` in this
    venv is "NOT guaranteed" and "a rebuild drops it"; this stays true
    here rather than being contradicted by a new install-chain call site.
    Reaching this function at all means machine-first provisioning failed
    for a NON-PEP-668 reason with `--allow-venv-fallback` passed — a
    guarded (PEP-668) interpreter never reaches here (see
    `provision_deps`; a guarded interpreter is a designed refusal, exit 96,
    with no fallback of any kind).

    Idempotent: an already-healthy venv (deps importable there) is left
    alone rather than re-installed on every run. This also covers the case
    where `provision_deps`'s per-candidate loop calls this function more
    than once in a single run (one call per failing non-first candidate,
    all sharing the one settings-home `venv_py`) — the second call's
    `deps_importable` check sees the venv the first call just provisioned
    and no-ops, so repeated calls converge rather than re-installing."""
    if venv_py.exists() and deps_importable(str(venv_py), import_names):
        print(f"PASS [deps] fallback venv already provisioned ({venv_dir}) — no-op.")
        return str(venv_py)

    if str(claude_klabauter_root) not in sys.path:
        sys.path.insert(0, str(claude_klabauter_root))
    from coordinator_core.install.ensure_venv import EnsureVenvError, ensure_coordinator_venv

    try:
        venv_status = ensure_coordinator_venv(
            claude_klabauter_root, settings_home_path, claude_home=os.environ.get("CLAUDE_HOME")
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
    if venv_pip.returncode != 0 or not deps_importable(str(venv_py), import_names):
        print("FAIL [deps] venv fallback install failed — see output above.", file=sys.stderr)
        sys.exit(1)
    print(f"PASS [deps] venv fallback install succeeded ({venv_dir}).")
    return str(venv_py)


def provision_deps(claude_klabauter_root: Path, py: str, allow_venv_fallback: bool) -> tuple[str, list[str]]:
    """Machine-first dependency provisioning (PM ruling 2026-08-17,
    superseding DR-307's healthy-venv prior-consent branch and retiring
    `--break-system-packages` entirely — see this file's module docstring
    Negative-spec and docs/decisions/DR-3xx-machine-first-install-surface.md).

    Order: enumerate the predictable, consumer-resolved interpreter set
    (`enumerate_provisioning_candidates`) -> verify NONE is PEP-668
    externally-managed (`_is_externally_managed`, checked BEFORE any
    install attempt) -> plain `pip install` of the declared deps AND the
    claude-klabauter package itself (`-e <claude_klabauter_root>`, so `[project.scripts]`
    console entrypoints materialise — C3's dependency) into EACH candidate
    -> verify (`_engine_installed`). No override flag is ever passed.

    A guarded (PEP-668) candidate is a DESIGNED REFUSAL: exits
    `EXIT_INTERPRETER_UNSUPPORTED` (96) naming the interpreter and the
    consumer that resolves to it. This is unconditional — `allow_venv_fallback`
    does NOT reach the venv here; the anti-scope is explicit ("when an
    interpreter raises a guard, swap the interpreter", never narrow the
    blast radius with an override flag). On macOS with Homebrew's guarded
    `python3`, the operator is offered (interactively, PM-authorized,
    including on `--i-am-agent`) to `brew uninstall` it —
    `_offer_homebrew_removal` — but the refusal for THIS run stands either
    way; a fresh supported interpreter still needs to be installed and the
    installer re-run.

    `allow_venv_fallback` survives as explicit break-glass ONLY for a
    genuine machine-level install failure that is NOT PEP-668 (permission-
    denied, network, disk) — `_fallback_to_venv` provisions ONLY the
    third-party deps there, deliberately never `-e .` (see that function's
    docstring); `coordinator-invoke`/`coordinator-cockpit-emit-schema`
    remain unmaterialized on that degraded path, which is the accepted
    shape of a break-glass fallback, not a defect.

    Idempotence keys on `_engine_installed` (deps importable AND
    `coordinator_core` pip-installed as a distribution), not on import
    alone — see that function's docstring for why a bare import check would
    silently skip the `pip install -e .` this chunk exists to run.

    `claude_klabauter_root` is the FLAG -> ENV -> repo-root resolved CLAUDE_KLABAUTER_ROOT (see
    `resolve_claude_klabauter_root`), not necessarily the script's own on-disk
    location — Review: code-reviewer 2026-07-21 Finding 7 (P2).

    Negative-spec: every machine-level attempt below is a PLAIN `pip
    install` with NO `--user` flag — see
    docs/decisions/2026-07-21-coordinator-core-dependency-and-environment-boundary.md
    § D2 for why `--user` is HOME-dependent and therefore wrong here.

    Returns `(engine_python, import_names)` — `engine_python` is the
    resolved interpreter for the FIRST candidate (the installer's own),
    which is what every downstream verification/PATH step in `main()`
    uses; every OTHER candidate (e.g. bare `python3`) is still provisioned
    in the loop below even though its resolved interpreter is not returned,
    because arming the dialect guard under it (ruling 1) does not require
    reporting it back to the caller."""
    print()
    print("--- Dependency provisioning (machine-first, derived from pyproject.toml) ---")

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
    from coordinator_core.install.ensure_venv import venv_python_path

    # Review: code-reviewer 2026-07-21 Finding 6 (P2) — settings_home()
    # resolves through `CLAUDE_HOME`/`Path.home()`, and `Path.home()` raises
    # `RuntimeError` when neither `HOME` (POSIX) nor `USERPROFILE` (Windows)
    # is resolvable. Fail loud with an actionable message instead of a bare
    # traceback.
    try:
        settings_home_path = settings_home()
    except RuntimeError as exc:
        print(f"FAIL [deps] cannot resolve settings-home: {exc}", file=sys.stderr)
        print("  Remediation: set CLAUDE_HOME (or HOME on POSIX / USERPROFILE on Windows) and re-run.", file=sys.stderr)
        sys.exit(1)
    venv_dir = settings_home_path / ".coordinator-venv"
    venv_py = venv_python_path(venv_dir)

    candidates = enumerate_provisioning_candidates(py)
    print(f"Predictable consumer-resolved interpreter set ({len(candidates)}):")
    for candidate in candidates:
        print(f"  - {candidate.label}: {candidate.path}")
        print("      consumers:")
        for consumer in candidate.consumers:
            print(f"        - {consumer}")

    guarded = [c for c in candidates if _is_externally_managed(c.path)]
    if guarded:
        print()
        for c in guarded:
            print(f"  GUARDED (PEP 668 externally-managed): {c.label} ({c.path})", file=sys.stderr)
            print("    consumers:", file=sys.stderr)
            for consumer in c.consumers:
                print(f"      - {consumer}", file=sys.stderr)

        removed_any = False
        for c in guarded:
            if _offer_homebrew_removal(c, settings_home_path):
                removed_any = True

        print(file=sys.stderr)
        print(
            "FAIL [deps] refusing to provision a PEP-668 externally-managed interpreter — "
            "no override flag is ever passed.",
            file=sys.stderr,
        )
        print(
            "  Remediation: install a supported (non-externally-managed) interpreter for the "
            "consumer(s) named above — a python.org release or a uv-managed Python — then re-run "
            "this installer. On stock Linux, distro python3 is externally-managed by policy; this "
            "is the expected default there too, not an edge case.",
            file=sys.stderr,
        )
        if removed_any:
            print(
                "  A guarded Homebrew Python was removed above at your consent — install its "
                "replacement, then re-run.",
                file=sys.stderr,
            )
        if allow_venv_fallback:
            print(
                "  --allow-venv-fallback was also passed: this refusal is unconditional and does "
                "not honour it — a PEP-668 guard is swapped, never fallen back from.",
                file=sys.stderr,
            )
        sys.exit(EXIT_INTERPRETER_UNSUPPORTED)

    engine_py: str | None = None
    for index, candidate in enumerate(candidates):
        if _engine_installed(candidate.path, import_names):
            print(
                f"PASS [deps] {' '.join(import_names)} and coordinator_core already installed "
                f"under {candidate.label} ({candidate.path}) — no-op."
            )
            resolved = candidate.path
        else:
            print(f"Provisioning {candidate.label} ({candidate.path}) — machine-level, no --user, no override flag.")
            try:
                pip_proc = _run_pip([candidate.path, "-m", "pip", "install", *dep_specs, "-e", str(claude_klabauter_root)])
            except subprocess.TimeoutExpired:
                print(f"FAIL [deps] pip install timed out after 600s under {candidate.path}.", file=sys.stderr)
                sys.exit(1)
            print(pip_proc.stdout, end="")

            pip_output = pip_proc.stdout.lower()
            if "externally-managed-environment" in pip_output:
                # The marker-file probe above said unguarded, but pip itself
                # refused mid-install — still a designed refusal, never an
                # override, never a fallback (see docstring).
                print(
                    f"FAIL [deps] {candidate.label} ({candidate.path}) refused: PEP 668 "
                    "externally-managed (discovered mid-install) — no override flag is ever passed.",
                    file=sys.stderr,
                )
                sys.exit(EXIT_INTERPRETER_UNSUPPORTED)

            if pip_proc.returncode != 0 or not _engine_installed(candidate.path, import_names):
                if not allow_venv_fallback:
                    print(
                        f"FAIL [deps] machine-level install failed under {candidate.path} — "
                        "see output above.",
                        file=sys.stderr,
                    )
                    print(
                        "  Remediation — pick ONE: fix the machine-level install, or re-run with "
                        f"--allow-venv-fallback (settings-home venv at {venv_dir}, third-party deps only).",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                print(
                    f"  --allow-venv-fallback passed — falling back to the settings-home "
                    f"coordinator venv at {venv_dir} for {candidate.label}.",
                    file=sys.stderr,
                )
                resolved = _fallback_to_venv(
                    claude_klabauter_root, settings_home_path, venv_dir, venv_py, dep_specs, import_names
                )
            else:
                print(f"PASS [deps] {candidate.label} ({candidate.path}) provisioned and verified.")
                resolved = candidate.path

        if index == 0:
            engine_py = resolved

    assert engine_py is not None  # candidates always has >=1 entry (the installer itself)
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


def _install_test_deps(engine_py: str, specs: list[str]) -> None:
    """pip-install the test extra under `engine_py` — the interpreter
    `provision_deps` already resolved and verified (a machine-first
    candidate, or the settings-home fallback venv when every machine
    candidate was unusable and `--allow-venv-fallback` was passed).

    Deliberately does NOT repeat provision_deps' venv-bootstrap ladder, and
    that is a settings-path constraint before it is a DRY one. The fallback
    venv location is owned by `coordinator_core._settings_home.settings_home()`
    + `coordinator_core.install.ensure_venv` (the contract-sanctioned durable
    prefix, CLAUDE.md § durable-data plane); re-deriving it here would mean a
    SECOND resolver for a path that already has exactly one, and any drift
    between them lands test tooling under an interpreter the suite never runs
    from. Taking `engine_py` as given is what keeps this function free of any
    home/settings-path resolution of its own — no `expanduser`, no
    `Path.home()`, no hand-built `~/...`.

    No PEP-668 retry: `--break-system-packages` no longer exists anywhere in
    this installer (machine-first-install-surface plan, C2) — a guarded
    interpreter is swapped, never bypassed with an override flag, and that
    applies to this extra the same as the required set `provision_deps`
    already refuses on.

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
        print("FAIL [test-deps] test-extra install failed — see output above.", file=sys.stderr)
        if is_pep668:
            print(
                "  Machine Python is externally-managed (PEP 668). No override flag is ever "
                "passed — provision under a supported (non-externally-managed) interpreter "
                "instead (a python.org release or a uv-managed Python), then re-run with "
                "--with-test-deps.",
                file=sys.stderr,
            )
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
        _install_test_deps(engine_py, specs)

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


#: Structural markers under a DoE dev-clone's ``coordinator/`` that identify it as
#: a real coordinator-claude source checkout. ANY one matching is sufficient.
#:
#: negative-spec: do NOT narrow this back to a single path. It was
#: ``coordinator/CLAUDE.md`` alone until DoE retired that file (`e8f9051db`,
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
    which also ships plugin.json), or the DoE dev-clone shape (see
    `_DEV_CLONE_DISTINCTIVE_MARKERS`/`_DEV_CLONE_GENERIC_MARKERS` above —
    NOT `coordinator/CLAUDE.md`, which DoE retired; see the negative-spec
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
    carries `machine-local/.doe-root` naming the DoE dev clone. This rung reads
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
    """Read `engine.working_repos.doe_claude` — the DR-132-ratified registry
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

        registry_val = registry_get("engine.working_repos.doe_claude")
        if not registry_val:
            return None
        return _resolve_plugin_root_for_machine_local(Path(registry_val))
    except Exception as exc:
        print(f"[ADVISORY] registry resolution failed ({exc}); skipping engine.working_repos.doe_claude rung.", file=sys.stderr)
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
    .doe-root pointer (durable + legacy) -> engine.working_repos.doe_claude
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

    Registry rung (2026-08-07, DR-132): `engine.working_repos.doe_claude` is
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
            candidate, rung, display = registry_root, CoordSourceRung.REGISTRY, "engine.working_repos.doe_claude registry key"
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

    Spec backlink: cross-repo/inbox/2026-08-05-doe-claude-em-klabauter-
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
        print("  (an OSS source clone not registered under publish.mirrors.*.path, or a DoE-style", file=sys.stderr)
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
    #     everything else), or (b) a DoE dev-clone (e.g. DoE-claude), where the
    #     coordinator plugin source lives under a coordinator/ subdir and the
    #     _DEV_CLONE_*_MARKERS constants mark it (NOT coordinator/CLAUDE.md,
    #     which DoE retired -- see the negative-spec on those constants).
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


def _git_current_branch(tree: Path) -> str | None:
    """The tree's checked-out local branch name, or `None` on any failure
    (git absent, not a work tree, detached HEAD) — advisory-only caller
    (Finding 2, staff-eng C8 review): a `None` here means "say nothing",
    never a refusal."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(tree), "symbolic-ref", "--short", "-q", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            **_NO_CONSOLE,
        )
    except OSError:
        return None
    if proc is None or proc.returncode != 0 or not proc.stdout:
        return None
    branch = proc.stdout.strip()
    return branch or None


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
        below). C8: when the klabauter checkout is discovered, a FURTHER
        write lands in the same pass — `engine.target = "candidate"` (PM
        ruling 2026-08-16: a claude-klabauter developer's box defaults to the nightly
        channel). `publish.mirrors.claude_klabauter.track_ref =
        "origin/candidate"` (the fact that actually selects which channel
        the mirror tracks — `engine.target` alone is inert on DoE's
        resolver, see C8's plan chunk) is declared ONLY when the discovered
        checkout is ALSO this box's already-registered publish mirror
        (`publish.mirrors.claude_klabauter.path`, `same_path`-equal) —
        the installer never inspects or moves a git tree it merely
        discovered, so it never declares a track_ref it cannot vouch for
        (staff-eng C8 review, Finding 2). When the write lands and the
        mirror's actual checked-out branch disagrees, an advisory line
        names the exact runnable reconciliation
        (`klabauter-channel.py --set candidate`) — never a failure, never a
        move performed here. A claude-klabauter box with no discoverable klabauter
        checkout, or one that is not this box's registered mirror,
        acquires no track_ref.
      - claude-klabauter: machine-local set repos.claude_klabauter AND
        `engine.target = "main"` (PM ruling 2026-08-16: installing klabauter
        itself targets the full release) — neither claude_klabauter key nor
        `track_ref` is written; a consumer never publishes, so `track_ref`
        stays publish-side only. The dual-boot auto-arm above does NOT apply
        here — this branch is otherwise unchanged in behaviour and output.
        ASSUMES a fresh clone checked out on the remote default (`main`) —
        very probably true for the fresh-install case this branch targets,
        silently false for an install from an existing checkout on a
        feature branch (staff-eng C8 review, Finding 13). Not enforced:
        nothing refuses, nothing writes a new key; an advisory line prints
        the clone's actual ref when it disagrees with the "main" being
        declared, turning the silent assumption visible.
        Per the agreed cross-repo contract
        (cross-repo/inbox/2026-08-05-doe-claude-em-klabauter-location-
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
    fallback path.

    NOT atomic (staff-eng C8 review, Finding 5): the writes are a
    sequential per-key `machine-local set` subprocess loop that exits(1) on
    the first failure — ordered fail-loud with a safe partial state, not an
    all-or-nothing transaction. `key_values`' insertion order is chosen so
    a partial failure always leaves the safer residue: `engine.target` (and
    `track_ref`, when declared) are written BEFORE `repos.claude_klabauter`
    on both identity branches, so a mid-loop failure leaves target-without-
    mirror (inert) rather than mirror-without-target (a false positive on
    the DR-132 gate). The probe added alongside this chunk
    (`bin/claude-klabauter-doctor-probe.py`) is the backstop for that residue, not a
    substitute for the ordering.

    `engine.working_repos.*` is DoE's key-namespace (schema authored on their
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

    # Deferred advisory callables (Findings 2 + 13, staff-eng C8 review):
    # each spawns `git` to compare a tree's actual ref against what is
    # about to be declared. Deliberately NOT invoked until AFTER the
    # machine-local-present check below -- the degrade path (machine-local
    # absent, --skip-dep-check --accept-missing-deps-risk) must spawn
    # NOTHING (existing contract, scripts/test_setup.py), and an advisory
    # about a key that was never actually written would be noise anyway.
    pending_advisories: list = []

    identity = resolve_repo_identity(repo_root)
    if identity == "claude-klabauter":
        # Review: staff-eng 2026-08-16 C8 Finding 13 — "installing
        # klabauter targets main" rests on an
        # UNSTATED assumption -- that a fresh clone is checked out on the
        # remote default (`main`). Nothing here inspects the clone's
        # actual ref or writes a track_ref (correctly -- a consumer never
        # publishes), so an install from an existing checkout on a feature
        # branch would silently declare `main` while the box runs
        # something else. Made visible, not fixed: print the clone's
        # actual checked-out ref so the assumption is a line an operator
        # can see, not a silent guess.
        key_values = {
            "engine.target": "main",
            "repos.claude_klabauter": str(claude_klabauter_root_resolved),
        }

        def _klabauter_identity_advisory() -> None:
            actual_branch = _git_current_branch(claude_klabauter_root_resolved)
            if actual_branch is not None and actual_branch != "main":
                print()
                print(
                    f"[ADVISORY] this klabauter checkout is on {actual_branch!r}, not "
                    "'main' -- engine.target is being declared 'main' per the "
                    "install-class default regardless (a fresh clone assumption; "
                    "see register_claude_klabauter_root's docstring)."
                )

        pending_advisories.append(_klabauter_identity_advisory)
    elif identity == "claude-klabauter":
        key_values = {
            "repos.claude_klabauter": str(claude_klabauter_root_resolved),
            "engine.working_repos.claude_klabauter": str(claude_klabauter_root_resolved),
        }
        discovered_klabauter = _discover_klabauter_root(repo_root, plugin_root_str)
        if discovered_klabauter:
            key_values["engine.target"] = "candidate"
            key_values["repos.claude_klabauter"] = discovered_klabauter
            # Review: staff-eng 2026-08-16 C8 Finding 2 — only declare a
            # track_ref for a tree that IS this
            # box's registered publish mirror -- a track_ref written for
            # an undiscovered/mismatched mirror is a dangling declaration
            # that breaks the next publish round (a registered mirror) or
            # targets a mirror that was never registered at all. Never
            # move the tree here -- moving a discovered git checkout the
            # installer merely found is a publish-plane action this
            # installer does not own.
            from coordinator_core.machine_resolver import registry_get
            from coordinator_core.win_portability import same_path

            mirror_path = registry_get("publish.mirrors.claude_klabauter.path")
            if mirror_path and same_path(discovered_klabauter, mirror_path):
                key_values["publish.mirrors.claude_klabauter.track_ref"] = "origin/candidate"

                def _track_ref_advisory(discovered_klabauter=discovered_klabauter) -> None:
                    current_branch = _git_current_branch(Path(discovered_klabauter))
                    if current_branch is not None and current_branch != "candidate":
                        print()
                        print(
                            "[ADVISORY] publish.mirrors.claude_klabauter.track_ref now "
                            f"declares 'origin/candidate', but {discovered_klabauter} is "
                            f"checked out on {current_branch!r}. Reconcile with:"
                        )
                        print("    python coordinator/bin/klabauter-channel.py --set candidate")

                pending_advisories.append(_track_ref_advisory)
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

    # machine-local IS present -> coordinator-claude is installed; the DoE command
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
    for advisory in pending_advisories:
        advisory()

    # docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md C1 —
    # PREREQUISITE FOR that plan's C4 (fail-closed on an unstamped engine
    # root): a fresh box with no discoverable klabauter checkout has no
    # mirror and no way to get one once C4 lands. This is the AUTHORITATIVE
    # call site (this script, not first_run.py's best-effort re-run path —
    # see `provision_stamped_engine`'s own docstring) because this is where
    # `claude_klabauter_root_resolved` is reliably known. Best-effort/advisory only:
    # a failure here prints a WARNING and does not fail the install (Hard
    # constraint 1/2 — no new escape hatch, deliberate invocation stays
    # working; this is neither).
    if identity == "claude-klabauter" and not discovered_klabauter:
        if str(claude_klabauter_root_resolved) not in sys.path:
            sys.path.insert(0, str(claude_klabauter_root_resolved))
        from coordinator_core.install.first_run import provision_stamped_engine

        provision_stamped_engine(claude_klabauter_root_resolved)

    return claude_klabauter_root_resolved


def offer_warm_opt_in(repo_root: Path, args: Args) -> None:
    """Install-time warm-engine opt-in, written to `engine.warm.enabled` in
    the machine-local TOML registry — the same registry `register_claude_klabauter_root`
    writes `engine.working_repos.*`/`repos.*` into, under a namespace
    `coordinator_core.warm.settings.is_warm_enabled` (this chunk's other
    file) resolves at read time.

    DEFAULT ON (PM ruling 2026-08-15, overriding this chunk's first draft,
    which had it off). `--i-am-agent` and every other non-interactive path
    through this installer take the ON branch WITHOUT prompting — the same
    `args.agent_mode` signal `run_health_probe`/`install_precommit_hook`
    already gate their own prompts on — because a non-interactive install
    must never block on a question. An interactive run prompts, framed in
    the operator's terms (heavy agentic engineering vs. occasional use),
    never in milliseconds, and defaults to ON on bare Enter/EOF.

    Do not re-derive an off default from a safety argument: the safety is
    carried by three things that already exist elsewhere in this plan, not
    by this prompt's default. Nothing starts on device boot (SessionStart-
    triggered), an idle server self-terminates after 15 minutes
    (`coordinator_core.warm.idle`), and `COORDINATOR_WARM=0` always wins
    over this registry key at read time (`is_warm_enabled`'s own
    precedence). Those three are the safety argument.

    Registry write is best-effort, mirroring `register_claude_klabauter_root`'s
    graceful-degrade shape: machine-local absent means coordinator-claude
    is not installed, which is `check_coordinator_claude_dep`'s failure to
    surface, not this optional feature's — an advisory is printed and the
    install proceeds. `is_warm_enabled` already defaults to off with no
    registry key present, so a skipped write here fails safe."""
    from coordinator_core.install._shared import resolve_machine_local_cli

    coord_path, _ = _resolve_coordinator_claude_root(repo_root, args)
    plugin_root = _resolve_plugin_root_for_machine_local(coord_path)
    plugin_root_str = str(plugin_root) if plugin_root else None

    if args.agent_mode:
        want_warm = True
    else:
        print()
        print("--- Warm engine ---")
        print("A warm engine keeps a resident process ready between agent sessions so tool")
        print("calls don't each pay a fresh startup. Recommended ON for a machine doing heavy")
        print("agentic engineering (many concurrent sessions); OFF for occasional use. Nothing")
        print("starts until a session begins, and an idle server shuts itself down after 15")
        print("minutes with no invocation.")
        try:
            answer = input("  Run a warm engine on this machine? [Y/n]: ")
        except EOFError:
            answer = ""
        want_warm = answer.strip().lower() not in ("n", "no")

    machine_local_argv = resolve_machine_local_cli(plugin_root_str)
    if machine_local_argv is None:
        print()
        print("[ADVISORY] machine-local not found — coordinator-claude absent.")
        print(f"  engine.warm.enabled registration skipped (recommended: {str(want_warm).lower()}).")
        print("  When coordinator-claude is installed, register with:")
        print(f"    machine-local set engine.warm.enabled {str(want_warm).lower()}")
        return

    value = "true" if want_warm else "false"
    try:
        proc = subprocess.run(
            machine_local_argv + ["set", "engine.warm.enabled", value],
            timeout=15,
            **_NO_CONSOLE,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[ADVISORY] 'machine-local set engine.warm.enabled' failed to launch: {exc}", file=sys.stderr)
        print(f"  Remediation: run manually: machine-local set engine.warm.enabled {value}", file=sys.stderr)
        return
    if proc.returncode != 0:
        print(f"[ADVISORY] 'machine-local set engine.warm.enabled' failed.", file=sys.stderr)
        print(f"  Remediation: run manually: machine-local set engine.warm.enabled {value}", file=sys.stderr)
        return
    print(f"PASS [registration] engine.warm.enabled = {value}")

    if want_warm:
        start_warm_engine(repo_root)


def start_warm_engine(repo_root: Path) -> None:
    """Bring a warm server up as part of the install and PROVE it serves,
    rather than leaving `engine.warm.enabled = true` as an unbacked claim.

    WHY THIS EXISTS. Registering the key only makes a warm engine
    *permitted*; it does not make one *exist*. Every path that creates one
    is demand-driven — the client's lazy `_spawn_once` on its first
    FileNotFoundError, and (once coordinator-claude ships it) a SessionStart
    trigger — so a freshly installed box has no resident engine until some
    later op happens to miss the pipe, and that op pays the full ~500 ms
    spawn cold. Worse, the install reports PASS either way, so a warm engine
    that can never come up on this box (an unreachable engine root, a
    generation that rotates faster than a server boots) is indistinguishable
    at install time from one that works. This step collapses that gap: it
    spawns, then dispatches a real `ping` through the ordinary client and
    only claims success on a served response.

    Best-effort by construction — an install must never fail over an
    optional performance feature, so every failure here degrades to an
    advisory naming the runnable remediation and returns. `is_warm_enabled`
    stays true regardless: a box that could not start one now still starts
    one lazily on demand.

    NEGATIVE-SPEC:
      - Does NOT decide whether warmth is wanted — `offer_warm_opt_in` owns
        that and calls this only on the ON branch.
      - Does NOT keep the server alive or supervise it: idle demotion
        (`coordinator_core.warm.idle`) still retires it after its deadline,
        which is the intended lifecycle, not a failure of this step.
      - Does NOT fabricate a warm result from a successful spawn — an
        unserved ping is reported as an advisory, never as PASS.
    """
    try:
        from coordinator_core.ops.ceremony.detached_spawn import spawn_detached
        from coordinator_core.warm.client import SERVER_ENTRY_SCRIPT, try_warm_dispatch
    except Exception as exc:  # noqa: BLE001 — never fail an install on an import
        print(f"[ADVISORY] warm engine not started (engine import failed: {exc!r}).", file=sys.stderr)
        return

    engine_root = str(repo_root)
    try:
        spawn_detached(engine_root, SERVER_ENTRY_SCRIPT)
    except Exception as exc:  # noqa: BLE001 — spawn_detached is best-effort itself
        print(f"[ADVISORY] warm engine spawn failed: {exc!r}", file=sys.stderr)
        print(f"  Remediation: run manually: python {SERVER_ENTRY_SCRIPT}", file=sys.stderr)
        return

    # A server takes ~0.5-1 s to bind its pipe; poll rather than sleeping a
    # fixed worst case, and stop at the first served response.
    deadline = time.monotonic() + 15.0
    served = None
    while time.monotonic() < deadline:
        served = try_warm_dispatch({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
        if served is not None:
            break
        time.sleep(0.25)

    if served is None:
        print("[ADVISORY] warm engine started but did not serve a ping within 15s.", file=sys.stderr)
        print("  Warmth stays enabled; a server will be started lazily by the first op that misses the pipe.", file=sys.stderr)
        print(f"  Remediation: run manually: python {SERVER_ENTRY_SCRIPT}", file=sys.stderr)
        return

    print("PASS [warm engine] resident server started and served a ping")


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


def check_dialect_guard_armed(claude_klabauter_root_resolved: Path, engine_py: str) -> None:
    """Install-time ARMED check for the PowerShell dialect guard
    (`coordinator_core/bash_guards/_dialect.py`) — makes a disarmed guard
    LOUD at install time instead of leaving it silently degraded.

    `_dialect.py`'s ImportError -> SILENT routing stays legal AT RUNTIME (a
    guard may decline to rule on PowerShell rather than crash the hot
    path). What must never stay quiet is an INSTALL that leaves that state
    undetected. This function does not arm the guard — that's a later
    chunk's job, provisioning the predictable set of consumer-resolved
    interpreters (docs/plans/2026-08-17-machine-first-install-surface.md
    § C2) — it only reports the current state, loudly, naming the
    interpreter(s) probed.

    Reuses `_dialect.probe_armed`, which exercises the guard's OWN code
    path (`_powershell_tokens`) rather than opening a second, parallel
    signal path — a disarmed result durably logs through the existing
    `_log_dialect_parser_unavailable` observability record exactly as it
    would in production.

    Checks TWO interpreters, both load-bearing for the defect this closes:
      - `engine_py`, the interpreter dependency provisioning resolved above.
      - bare `python3` on PATH, because `hooks.json` registers every bash
        guard hook under exactly that bare name — checking only `engine_py`
        would miss the actual disarmed case on a box where `engine_py`
        happens to be a healthy fallback venv (the exact shape measured
        2026-08-17: ARMED under `.coordinator-venv`, SILENT under bare
        `python3`).

    Never fatal: no exit-code change. Anti-scope forbids verifying a
    dependency by importing it under the WRONG interpreter — `probe_armed`
    always subprocesses into the named interpreter rather than checking
    `sys.executable`/the current process.
    """
    print()
    print("--- Verification: PowerShell dialect guard ARMED state ---")

    if str(claude_klabauter_root_resolved) not in sys.path:
        sys.path.insert(0, str(claude_klabauter_root_resolved))
    from coordinator_core.bash_guards._dialect import (
        dialect_parser_unavailable_log_path,
        probe_armed,
    )

    # Grouped by RESOLVED interpreter path, not label: bare `python3` and
    # `engine_py` are the same file on a box where the fallback venv IS the
    # engine interpreter, and probing that path twice wastes a real child
    # spawn for a foregone result. Dedup shape mirrors `bin/claude-klabauter-doctor-
    # probe.py::_run_probe_dialect_guard_armed`'s `bare_python3 !=
    # sys.executable` guard (reviewer finding) -- but unlike that probe's
    # two-entry dict, BOTH consumer labels must still reach the printed
    # line when they collapse onto one path, since each names a distinct
    # reason this check exists (dependency-provisioning target vs. what
    # `hooks.json` actually runs guards under) — collapsing the labels
    # themselves, not just the probe call, would silently drop the second
    # reason from the record.
    interpreter_labels: dict[str, list[str]] = {}
    interpreter_labels.setdefault(engine_py, []).append("engine (dependency-provisioning) interpreter")
    bare_python3 = shutil.which("python3")
    if bare_python3:
        interpreter_labels.setdefault(bare_python3, []).append(
            "bare python3 on PATH (what hooks.json runs guards under)"
        )
    else:
        print(
            "  [ADVISORY] bare python3 not found on PATH — cannot probe the "
            "interpreter hooks.json resolves guard hooks under."
        )

    any_disarmed = False
    any_missing_package = False
    for interpreter, labels in interpreter_labels.items():
        label = " / ".join(labels)
        armed, detail = probe_armed(interpreter, claude_klabauter_root_resolved)
        if armed:
            print(f"PASS [dialect-guard]  {label} ({interpreter}): {detail}")
        else:
            any_disarmed = True
            if "cause: missing-package" in detail:
                any_missing_package = True
            print(f"WARN [dialect-guard]  {label} ({interpreter}): {detail}", file=sys.stderr)

    if any_disarmed:
        print(file=sys.stderr)
        print(
            "  The PowerShell dialect guard is DISARMED under at least one interpreter "
            "named above — PowerShell command classification silently stops there "
            "(runtime SILENT routing, legal by design; see _dialect.py).",
            file=sys.stderr,
        )
        print(f"  Durable record: {dialect_parser_unavailable_log_path()}", file=sys.stderr)
        if any_missing_package:
            print(
                "  Remediation: install tree_sitter and tree_sitter_pwsh under the named "
                "interpreter(s), e.g.: <interpreter> -m pip install tree_sitter tree_sitter_pwsh",
                file=sys.stderr,
            )
        else:
            print(
                "  Cause is not a missing package (see the cause tag in the detail above) — "
                "inspect the reported cause before assuming a package install fixes this.",
                file=sys.stderr,
            )


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
        substrate_argv = [engine_py, "-m", "coordinator_core.install.substrate", "--setup-only"]
        if args.allow_venv_fallback:
            substrate_argv.append("--allow-venv-fallback")
        proc = subprocess.run(
            substrate_argv,
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


def _whoami_importable_under(python_bin: str) -> bool:
    """True iff ``coordinator_whoami`` imports under ``python_bin``.

    Isolation-boundary probe, not an in-process import — ``python_bin`` is,
    in the case this function exists for, a DIFFERENT interpreter than the
    one running this script (the operator's ``coordinator.python`` general
    pin), so there is no in-process substitute for asking it directly.
    Mirrors ``coordinator_core.install.ensure_venv._whoami_importable_under``
    (pre-relocation original) without importing it — this script runs before
    ``coordinator_core``'s own third-party deps are guaranteed provisioned.
    """
    try:
        proc = subprocess.run(
            [python_bin, "-c", "import coordinator_whoami"],
            capture_output=True,
            timeout=30,
            **_NO_CONSOLE,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _resolve_whoami_pkg_dir(plugin_root: Path) -> Path:
    """WHOAMI_PKG seam: registry ``coordinator.whoami_src`` -> dir, else
    ``plugin_root/whoami`` — same fallback shape as
    ``coordinator_core.install.ensure_venv._resolve_whoami_pkg``, reimplemented
    here (not imported) so this advisory step stays reachable even when
    ``coordinator_core``'s own deps are not yet importable under THIS
    script's interpreter."""
    from coordinator_core.machine_resolver import registry_get

    seam = registry_get("coordinator.whoami_src")
    if seam:
        seam_path = Path(seam)
        if seam_path.is_dir():
            return seam_path
        print(
            f"[ADVISORY] coordinator.whoami_src='{seam}' is not a directory; "
            f"falling back to {plugin_root / 'whoami'}",
            file=sys.stderr,
        )
    return plugin_root / "whoami"


def _advise_manual_whoami_install(python_bin: str, whoami_pkg: Path, reason: str, stderr: str) -> None:
    """The degradation path: name the exact command a human would run,
    mirroring ``ensure_venv._advise_manual_whoami_install``'s remediation
    text (pre-relocation original)."""
    print(
        f"[ADVISORY] could not install coordinator_whoami under the coordinator.python "
        f"pin ({python_bin}) — {reason}.",
        file=sys.stderr,
    )
    print(
        f"  probe_p5 will read red until it is installed. Run:\n"
        f"    {python_bin} -m pip install -e {whoami_pkg}/",
        file=sys.stderr,
    )
    tail = "\n".join(stderr.splitlines()[-20:])
    if tail:
        print(tail, file=sys.stderr)


def provision_whoami_under_general_pin(
    repo_root: Path, engine_py: str, claude_klabauter_root_resolved: Path, args: Args
) -> None:
    """Advisory install-chain step: pip-installs ``coordinator_whoami``
    editable under the operator's ``coordinator.python`` general pin, when
    that interpreter is healthy but does not yet have it importable.

    NAMED MECHANISM (AC1 of docs/plans/2026-08-18-retire-coordinator-venv.md):
    this is the machine-first install path's replacement for
    ``coordinator_core.install.ensure_venv._ensure_whoami_under_general_pin``,
    which only ever ran as a side effect of ``ensure_coordinator_venv`` — a
    call site that C4 (same plan) reduces to break-glass-only
    (``--allow-venv-fallback``). Without this relocation, a fresh box that
    never opts into the venv fallback would never install
    ``coordinator_whoami`` under ANY interpreter the operator's general pin
    names, regressing the exact defect that function was written to close:
    ``probe_p5`` (``coordinator_core.plugin_health.sentinel``) reads red at
    first doctor run and stays red until a human runs the pip line by hand
    (cross-repo/archive/2026-08-10-doe-claude-em-general-pin-is-self-
    sufficient-here-fresh-install-is-not.md).

    Placement: invoked from ``main()`` AFTER ``install_bin_forwarders`` (not
    inside ``provision_deps``) for two reasons. (1) Ordering:
    ``install_bin_forwarders`` shells out to ``substrate.py --setup-only``,
    which relocates the whoami SOURCE tree to
    ``<settings-home>/coordinator-whoami`` (its own docstring: "Steps 1-3,
    C10a venv/whoami, seed-wikis") — provisioning whoami before its source
    tree exists would fail on the exact fresh-box case this function exists
    to close. ``provision_deps`` runs even earlier (top of ``main()``), so
    folding this in there is doubly wrong. (2) This function needs
    ``plugin_root`` (to resolve ``ml_cli``-equivalent registry reads and the
    whoami source fallback), which only becomes resolvable once
    ``_resolve_coordinator_claude_root``/``_resolve_plugin_root_for_machine_local``
    are reachable — a post-registration-step concern, not a
    ``provision_deps(claude_klabauter_root, py, allow_venv_fallback)``-signature concern.
    ``coordinator_whoami`` is also coordinator-claude's package, not one of
    claude-klabauter's declared deps — folding it into ``provision_deps`` would muddy
    that function's documented "declared deps AND the claude-klabauter package itself"
    contract.

    ADVISORY-ONLY, NEVER FATAL — mirrors
    ``_ensure_whoami_under_general_pin``'s contract exactly: no exception
    escapes this function, and it never changes ``main()``'s status word/exit
    code. Skipped entirely under ``--register-only`` (see caller in
    ``main()``), matching every other post-registration step it sits beside.
    """
    print()
    print("--- Install: coordinator_whoami under the coordinator.python general pin ---")

    if str(claude_klabauter_root_resolved) not in sys.path:
        sys.path.insert(0, str(claude_klabauter_root_resolved))

    try:
        from coordinator_core.machine_resolver import registry_get

        coord_path, coord_source = _resolve_coordinator_claude_root(repo_root, args)
        if coord_source.is_publish_mirror_rejected:
            print(
                "[ADVISORY] coordinator-claude root resolved to a publish mirror — "
                "skipping coordinator_whoami provisioning.",
                file=sys.stderr,
            )
            return
        plugin_root = _resolve_plugin_root_for_machine_local(coord_path)
        if plugin_root is None or not (plugin_root / "templates").is_dir():
            print(
                f"[ADVISORY] coordinator-claude plugin root not resolvable from {coord_path} "
                "(no templates/ dir) — skipping coordinator_whoami provisioning.",
                file=sys.stderr,
            )
            return

        general = registry_get("coordinator.python") or ""
        if not general:
            print("[ADVISORY] coordinator.python is unset — skipping coordinator_whoami provisioning.")
            return
        if not Path(general).exists():
            print(
                f"[ADVISORY] coordinator.python='{general}' does not exist on disk — "
                "skipping coordinator_whoami provisioning.",
                file=sys.stderr,
            )
            return
        if _whoami_importable_under(general):
            print(
                f"PASS [whoami] coordinator_whoami already importable under coordinator.python "
                f"({general}) — no-op."
            )
            return

        whoami_pkg = _resolve_whoami_pkg_dir(plugin_root)
        proc = subprocess.run(
            [general, "-m", "pip", "install", "-e", f"{whoami_pkg}/"],
            capture_output=True,
            text=True,
            timeout=600,
            **_NO_CONSOLE,
        )
        if proc.returncode == 0:
            print(
                f"PASS [whoami] installed coordinator_whoami under the coordinator.python "
                f"pin ({general})."
            )
            return
        _advise_manual_whoami_install(general, whoami_pkg, f"pip exited {proc.returncode}", proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        print(
            f"[ADVISORY] coordinator_whoami install under the coordinator.python pin timed "
            f"out: {exc}",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001 — advisory surface, never fatal
        print(
            "[ADVISORY] coordinator_whoami provisioning under the coordinator.python pin "
            f"failed unexpectedly ({type(exc).__name__}: {exc}).",
            file=sys.stderr,
        )


def migrate_whoami_pin_off_venv(repo_root: Path, args: Args) -> None:
    """Advisory install-chain step: fires the one-time `coordinator.whoami_python`
    repoint leg on boxes whose pin still names the retired `.coordinator-venv`.

    NAMED MECHANISM (AC1 of docs/plans/2026-08-18-retire-coordinator-venv.md):
    `coordinator_core.install.migrations.whoami_pin_migration` is written to be
    idempotent and refusal-safe, but a migration no call site invokes never runs
    anywhere — it repoints exactly the box its author happened to run it on, by
    hand, which is the "no hand-edit instruction to operators" clause AC1 rules
    out. This is that call site.

    Ordering: invoked AFTER `provision_whoami_under_general_pin`, deliberately.
    The migration REFUSES to repoint onto an interpreter that cannot import
    `coordinator_whoami` (never repoint blind), so the provisioning step that
    makes it importable has to have run first; inverting the two turns a healthy
    box into a refusal on first install.

    Advisory and never fatal, matching the step it sits beside: a box that cannot
    be migrated keeps its old pin and says so, rather than failing the install.
    """
    if args.register_only:
        return
    try:
        from coordinator_core.install._shared import resolve_machine_local_cli
        from coordinator_core.install.migrations.whoami_pin_migration import (
            migrate_whoami_pin,
        )

        coord_path, coord_source = _resolve_coordinator_claude_root(repo_root, args)
        if coord_source.is_publish_mirror_rejected:
            print(
                "[ADVISORY] coordinator-claude root resolved to a publish mirror — "
                "skipping the coordinator.whoami_python migration.",
                file=sys.stderr,
            )
            return
        plugin_root = _resolve_plugin_root_for_machine_local(coord_path)
        ml_cli = resolve_machine_local_cli(str(plugin_root) if plugin_root else None)
        migrate_whoami_pin(ml_cli)
    except Exception as exc:  # noqa: BLE001 — advisory surface, never fatal
        print(
            "[ADVISORY] the coordinator.whoami_python migration failed unexpectedly "
            f"({type(exc).__name__}: {exc}); the pin is left as it was.",
            file=sys.stderr,
        )


#: Dependency-ordered claude-doe launcher chain: (label, CLI relpath under
#: coordinator/bin/, extra argv). Order matters -- the root pointer must
#: exist before anything that resolves through it at RUNTIME (the wrapper's
#: `--print-plugin-dir` reads it, transitively, via the doe-root ladder), and
#: the wrapper/launcher (the artifacts the shim's rc block invokes BY PATH)
#: must be installed before the shim (which wires the rc/profile sentinel
#: that dot-sources/invokes them). See docs/reference/coordinator-plugin-
#: load-chain.md § 4 for the full artifact -> generator -> source-of-truth
#: table this constant mirrors.
_CLAUDE_DOE_CHAIN_STEPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("doe-root pointer", "gen-doe-root-pointer.py", ("--graceful-skip-unresolved",)),
    ("claude-doe wrapper", "install-claude-doe-wrapper.py", ()),
    ("claude-doe launcher", "gen-claude-doe-launcher.py", ()),
    ("claude-doe shim (rc/profile sentinel)", "gen-claude-doe-shim.py", ()),
)


def install_claude_doe_launcher_chain(repo_root: Path, engine_py: str, claude_klabauter_root_resolved: Path, args: Args) -> None:
    """Best-effort install-chain step: runs the four `coordinator/bin/*claude-doe*`
    generators that render/wire the interactive `claude-doe` launch chain
    (`.doe-root` pointer -> `claude-doe` wrapper -> `claude-doe.{cmd,ps1}`
    launcher -> the rc/profile sentinel that dot-sources/invokes them --
    see docs/reference/coordinator-plugin-load-chain.md § 4).

    Root-cause fix, 2026-08-15 (sizing dlv-claude-doe-launcher-generators-are-
    absen-bb685e): `scripts/setup.py`/the manifest never called any of these
    four generators, so a fresh clean install left claude-klabauter installed and
    coordinator SILENTLY absent from every session -- no doctrine, no hooks,
    no skills, no error. Mirrors `install_bin_forwarders`/
    `install_precommit_hook`'s ADVISORY shape: a launcher-chain failure must
    never abort the rest of setup, but per this fix's own point (the prior
    failure mode was SILENT), each step's outcome is printed loudly --
    PASS/ADVISORY, never swallowed -- rather than folded into a single
    pass/fail line that could hide which of the four steps actually failed.

    Idempotent by construction, not by a call-site guard: all four generators
    are ALREADY idempotent (sentinel-guarded rc edits, skip-if-current
    renders, `cp -p`-then-verify installs -- see each op module's own
    docstring), so a re-run over an already-working install is a clean no-op
    at each step; this function adds no de-duplication of its own because
    none is needed (CLAUDE.md: make the call site idempotent only when the
    generator ISN'T -- these are).

    Each step is run independently (a failure in one does not skip the
    others) since only the RUNTIME resolution chain is order-dependent, not
    generation itself -- an operator who fixes just the failing step later
    does not need to re-run the whole chain.

    Skipped entirely under `--register-only` (no coordinator-claude/plugin-
    root resolution attempted in that mode, matching the other post-
    registration steps it sits beside in `main`) -- callers gate that, not
    this function.
    """
    print()
    print("--- Install: claude-doe launcher chain (coordinator/bin/*claude-doe*) ---")

    env = dict(os.environ)
    env["CLAUDE_KLABAUTER_ROOT"] = str(claude_klabauter_root_resolved)

    any_failed = False
    for label, cli_name, extra_argv in _CLAUDE_DOE_CHAIN_STEPS:
        cli = repo_root / "coordinator" / "bin" / cli_name
        if not cli.is_file():
            any_failed = True
            print(
                f"[ADVISORY] {cli} not found — skipping {label} install. "
                "Coordinator will NOT load in any interactive session on this box until this is fixed.",
                file=sys.stderr,
            )
            continue

        argv = [engine_py, str(cli), *extra_argv]
        try:
            proc = subprocess.run(
                argv,
                cwd=str(claude_klabauter_root_resolved),
                env=env,
                capture_output=True, text=True,
                **_NO_CONSOLE,
            )
        except OSError as exc:
            any_failed = True
            print(
                f"[ADVISORY] {label} install could not even be spawned ({exc}) — "
                "coordinator will NOT load in any interactive session on this box until this is fixed.",
                file=sys.stderr,
            )
            print(f"  Re-run manually: {' '.join(argv)}", file=sys.stderr)
            continue

        output = (proc.stdout + proc.stderr).strip()
        # Review: coordinatorcode-reviewer-7ca32c22 — `gen-doe-root-pointer.py
        # --graceful-skip-unresolved` exits 0 on a genuine skip (repos.doe_claude
        # not yet resolved), so returncode alone can't distinguish "wrote it"
        # from "gave up". Detect the `<label>: skipped` contract row and treat
        # it as its own ADVISORY outcome — never PASS — with the explanation
        # printed unconditionally (skip lines must survive agent_mode, unlike
        # the general output-echo above, or a scripted install sees only a
        # bare ADVISORY line with no reason).
        skipped = any(
            line.strip().endswith(": skipped") or ": skipped (" in line
            for line in output.splitlines()
            if line.strip().startswith("doe_root_pointer:")
        )
        if not args.agent_mode and output and not skipped:
            print(output)
        if skipped:
            print(
                f"[ADVISORY] {label} install skipped (see reason below) — "
                "not an error, but coordinator will NOT load in any interactive session "
                "on this box until this step completes.",
                file=sys.stderr,
            )
            if output:
                print(output, file=sys.stderr)
            print(f"  Re-run manually once resolved: {' '.join(argv)}", file=sys.stderr)
            continue
        if proc.returncode != 0:
            any_failed = True
            print(
                f"[ADVISORY] {label} install reported a non-zero exit (code {proc.returncode}) — "
                "coordinator will NOT load in any interactive session on this box until this is fixed.",
                file=sys.stderr,
            )
            print(f"  Re-run manually: {' '.join(argv)}", file=sys.stderr)
            continue
        print(f"PASS [claude-doe-chain] {label}")

    if any_failed:
        print(
            "[ADVISORY] claude-doe launcher chain incomplete — see the per-step ADVISORY lines above. "
            "A session started on this box may look like vanilla Claude Code with no error.",
            file=sys.stderr,
        )


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

    Spec backlink: pln-claude-klabauter-oss-release-e-50bd5d § C12
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
    target.write_text(_percolate_identity_template(hints), encoding="utf-8", newline="\n")
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


def install_host_sampler_task(repo_root: Path, claude_klabauter_root_resolved: Path) -> None:
    """Install-chain step: register the Windows Task Scheduler entry that
    fires ``coordinator_core.telemetry.host_sampler`` on a fixed cadence.

    ADVISORY, non-fatal (mirrors `install_precommit_hook`/
    `install_percolate_identity`/`install_machine_identity`'s shape) — a
    non-Windows host, an unavailable `schtasks.exe`, or a registration
    failure must fall through to a printed [ADVISORY], never fail the
    install. See `coordinator_core.install.host_sampler_scheduler` for the
    registration mechanism, why it must survive an engine death, and the
    open shell-out-carve-out question this site raises (schtasks.exe is not
    yet a named class in docs/reference/shell-out-carve-outs.md).

    Idempotent: `register_host_sampler_task` uses `schtasks /Create ... /F`,
    which overwrites an existing task of the same name in place rather than
    duplicating it.
    """
    if str(claude_klabauter_root_resolved) not in sys.path:
        sys.path.insert(0, str(claude_klabauter_root_resolved))
    from coordinator_core.install.host_sampler_scheduler import (
        register_host_sampler_task,
    )

    register_host_sampler_task(repo_root)


def _seed_fleet_env_root_from_klabauter(repo_root: Path, claude_klabauter_root_resolved: Path, args: Args) -> None:
    """C4: seed `fleet_env.root` from the already-registered `repos.claude_klabauter`
    (`register_claude_klabauter_root` ran earlier in this same `main()` pass — see call order
    in `main`) so the fleet environment lands at the contract's documented location
    (`<klabauter-root>/.fleet-env`, `docs/reference/fleet-shared-environment-contract.md`
    § "The environment's location") without requiring a hand-run `machine-local set`.

    Seeds, never overwrites: an operator-set (or previously-seeded) `fleet_env.root`
    is left untouched — this function only fills the key when it reads absent, which
    is the normal day-one state this contract already names (§ "The day-one absent-key
    property"). No new fallback rung: `coordinator_core/install/fleet_env_resolve.py`'s
    ladder is unchanged; this only populates rung 1's registry candidate before that
    ladder ever runs, so it need not fall to rung 2 (`<settings-home>/.fleet-env`).

    ADVISORY, non-fatal (same shape as `install_machine_identity`) — a missing
    `machine-local` CLI or an unregistered `repos.claude_klabauter` (no discoverable
    klabauter checkout on this box) both leave the key unset rather than erroring;
    `ensure_fleet_env`'s own C5 fallback still resolves a usable root in that case.
    """
    from coordinator_core.install._shared import resolve_machine_local_cli
    from coordinator_core.machine_resolver import registry_get

    if registry_get("fleet_env.root"):
        return  # operator-set or already seeded — never overwritten here.
    klabauter_root = registry_get("repos.claude_klabauter")
    if not klabauter_root:
        return  # no discoverable klabauter checkout yet — nothing to seed from.

    fleet_env_root = str(Path(klabauter_root) / ".fleet-env")

    coord_path, _ = _resolve_coordinator_claude_root(repo_root, args)
    plugin_root = _resolve_plugin_root_for_machine_local(coord_path)
    machine_local_argv = resolve_machine_local_cli(str(plugin_root) if plugin_root else None)
    if machine_local_argv is None:
        print("[ADVISORY] machine-local not found — cannot seed fleet_env.root.")
        print(f"  Set it manually: machine-local set fleet_env.root {fleet_env_root}")
        return

    try:
        proc = subprocess.run(
            machine_local_argv + ["set", "fleet_env.root", fleet_env_root],
            timeout=15,
            **_NO_CONSOLE,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[ADVISORY] fleet_env.root seeding failed to launch: {exc}", file=sys.stderr)
        print(f"  Set it manually: machine-local set fleet_env.root {fleet_env_root}", file=sys.stderr)
        return
    if proc.returncode != 0:
        print(f"[ADVISORY] fleet_env.root seeding failed (exit {proc.returncode}).", file=sys.stderr)
        print(f"  Set it manually: machine-local set fleet_env.root {fleet_env_root}", file=sys.stderr)
        return
    print(f"PASS [fleet-env] seeded fleet_env.root = {fleet_env_root} (from repos.claude_klabauter)")


def install_fleet_shared_environment(repo_root: Path, claude_klabauter_root_resolved: Path, args: Args) -> None:
    """Install-chain step: provision the fleet shared Python environment
    (`coordinator_core.install.fleet_env.ensure_fleet_env`, C4/C6).

    Seeds `fleet_env.root` from `repos.claude_klabauter` first (C4 —
    `_seed_fleet_env_root_from_klabauter`), so a fresh install lands the
    environment at the contract's documented location on the FIRST run
    instead of falling to C5's settings-home rung and requiring a hand
    fix-up. Runs before `ensure_fleet_env()` unconditionally — seeding is a
    fast registry read/write, never gated behind the provisioning outcome.

    ADVISORY, non-fatal (mirrors `install_host_sampler_task`/
    `install_precommit_hook`'s shape) — the environment is multi-GB and this
    step can hit no network, no disk, or a read-only install location; a
    provisioning failure must fall through to a printed [ADVISORY], never
    fail the rest of setup. Re-run remediation names a runnable script
    (cold-path convention), not a slash command, since no session exists at
    this point in the install chain.
    """
    print()
    print("--- Install: fleet shared Python environment ---")

    if str(claude_klabauter_root_resolved) not in sys.path:
        sys.path.insert(0, str(claude_klabauter_root_resolved))
    from coordinator_core.install.fleet_env import FleetEnvError, ensure_fleet_env

    _seed_fleet_env_root_from_klabauter(repo_root, claude_klabauter_root_resolved, args)

    try:
        status = ensure_fleet_env()
    except FleetEnvError as exc:
        print(f"[ADVISORY] fleet environment provisioning failed: {exc}", file=sys.stderr)
        print(
            f"  Re-run manually: {sys.executable} -m coordinator_core.install.fleet_env",
            file=sys.stderr,
        )
        return
    print(f"fleet shared environment: {status}")


def install_verify_settings_home(claude_klabauter_root_resolved: Path) -> None:
    """Install-chain step: report whether `<settings-home>` is actually
    complete, not merely whether each of its individual population steps
    (bin-forwarder install, `.percolate-identity`, machine identity
    registry, `.claude-klabauter-root` pointer, etc.) exited 0 earlier in this same
    `main()` pass.

    docs/plans/2026-08-17-machine-first-install-surface.md § C5: population
    was previously emergent — each contributor lands its own piece with no
    single verified statement that the whole is complete. This step is that
    statement, using `coordinator_core.install.settings_home_report` (shared
    with `bin/claude-klabauter-doctor-probe.py`'s `claude-klabauter.settings_home.complete`
    probe, so a cold re-check after this process exits uses the same
    oracle, not a second one that can drift).

    ADVISORY, non-fatal (mirrors every other post-registration step in
    `main`) — an incomplete settings home does not abort the rest of setup;
    it is reported so the operator (or the doctor probe, later) can act on
    it, per the plan's "checkable, not silently emergent" ask.
    """
    print()
    print("--- Install: settings-home completeness ---")

    if str(claude_klabauter_root_resolved) not in sys.path:
        sys.path.insert(0, str(claude_klabauter_root_resolved))
    from coordinator_core._settings_home import settings_home
    from coordinator_core.install.settings_home_report import (
        check_settings_home,
        format_report_lines,
    )

    try:
        settings_home_path = settings_home()
    except RuntimeError as exc:
        print(f"[ADVISORY] cannot resolve settings-home — skipping completeness check: {exc}", file=sys.stderr)
        return

    report = check_settings_home(settings_home_path, claude_klabauter_root_resolved)
    for line in format_report_lines(report):
        print(line)

    if report.complete:
        print(f"PASS [settings-home] complete at {settings_home_path}")
    else:
        print(
            f"[ADVISORY] settings-home at {settings_home_path} is incomplete — "
            "re-run scripts/setup.py to fill the gaps reported above.",
            file=sys.stderr,
        )


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
        check_git_version()

    engine_py, import_names = provision_deps(
        claude_klabauter_root_resolved, py, args.allow_venv_fallback
    )

    if not args.register_only:
        handle_test_tooling(claude_klabauter_root_resolved, engine_py, args)
        print_symbols_extra_hint(claude_klabauter_root_resolved)

        if args.skip_dep_check:
            print()
            print("[SKIP] dep check bypassed (--skip-dep-check + --accept-missing-deps-risk).")
        else:
            check_coordinator_claude_dep(repo_root, args)

    claude_klabauter_root_resolved = register_claude_klabauter_root(claude_klabauter_root_resolved, claude_klabauter_root_source, repo_root, args)
    offer_warm_opt_in(repo_root, args)
    verify_coordinator_core_importable(claude_klabauter_root_resolved, engine_py, import_names)
    check_dialect_guard_armed(claude_klabauter_root_resolved, engine_py)
    probe_hard_failure = run_health_probe(claude_klabauter_root_resolved, engine_py, args.agent_mode)

    if not args.register_only:
        install_bin_forwarders(repo_root, engine_py, claude_klabauter_root_resolved, args)
        provision_whoami_under_general_pin(repo_root, engine_py, claude_klabauter_root_resolved, args)
        migrate_whoami_pin_off_venv(repo_root, args)
        install_claude_doe_launcher_chain(repo_root, engine_py, claude_klabauter_root_resolved, args)
        install_precommit_hook(repo_root, engine_py, args.agent_mode)
        install_percolate_identity(repo_root, claude_klabauter_root_resolved)
        install_machine_identity(repo_root, claude_klabauter_root_resolved, args)
        install_host_sampler_task(repo_root, claude_klabauter_root_resolved)
        install_fleet_shared_environment(repo_root, claude_klabauter_root_resolved, args)
        install_verify_settings_home(claude_klabauter_root_resolved)

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
