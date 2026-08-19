"""
coordinator_core.ops.gen_claude_doe_shim — Port of: gen-claude-doe-shim.sh
(DoE b5a4192c, 2026-07-20).

Purpose: renders .claude/shell/claude-doe-shim.sh — under CLAUDE_HOME when set,
else under the HOME env var, else under the expanded platform home — from the
coordinator template (the claude() shell-function wrapper that shadows the bare
`claude` command), then ensures exactly ONE sentinel-guarded source block exists in
the operator's interactive rc, selected from the SHELL env var (zsh picks
~/.zshrc, bash picks ~/.bashrc).

Spec backlink: DoE-claude:pln-coordinator-maximalist-install-e73afa § C2
Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Idempotency:  safe to re-run — sentinel-guarded rc block, atomic writes; re-running
              when unchanged is a no-op.
Dry-run safe: --check-only renders to a discarded temp path, never touches live files.
Migration:    detects the legacy `# --- coordinator maximalist launch ---` block and
              surfaces a one-line migration note; does NOT silently rewrite it.
Override:     --rc <path> (or the DoE trampoline's COORDINATOR_SHIM_RC env passthrough)
              selects the rc directly (escape hatch when $SHELL detection is wrong).

Shell family: --shell bash|powershell (default: platform-derived — powershell on
              native Windows, bash everywhere else including MSYS; preserving all prior
              behaviour byte-for-byte) selects BOTH the wired rc line (bash:
              guarded `source`; powershell: guarded dot-source, see
              EXPECTED_SOURCE_LINE_POWERSHELL) and the rendered shim's
              filename (.sh vs .ps1) under the same .claude/shell/ directory.
              Explicit flag chosen over --rc-extension sniffing: --rc's value
              is frequently a caller-supplied override (env var, test fixture,
              non-standard path) with no reliable extension convention, so
              inference would be a guess dressed as detection. An explicit
              flag is unambiguous, matches how --rc/--template already work
              as explicit escape hatches, and keeps the no-flag default
              behaviour trivially unaffected.

Fresh-install surface: this module writes into the operator's live interactive rc
file and is exercised by `coordinator:install` on every fresh machine — a missed
edge here hard-fails a clean install (Family-I).

Negative-spec (faithful oracle reproduction):
    - The legacy-stopgap detector reads `$HOME/.bashrc` unconditionally — NOT
      `${CLAUDE_HOME:-$HOME}/.bashrc` — mirroring the bash oracle's literal `$HOME`
      reference. This is preserved verbatim, not a bug fix: the legacy block was
      hand-written directly under the real $HOME during the 2026-07-04 bringup,
      before CLAUDE_HOME existed as a concept.
    - Does NOT implement the bash >= 4 version guard from the oracle (DR-148
      defense-in-depth) — meaningless for a pure-Python module. Omitted intentionally.
    - Does NOT reimplement the shim template's body — copies the template bytes to
      the destination verbatim, exactly like the bash oracle's `cp`.
    - The rendered shim destination is a SOURCED file (never directly executed), so —
      unlike a git-hook/launcher port — no `os.chmod`/exec-bit preservation is needed
      on the atomic rewrite; the bash oracle's own `cp` (no `-p`) does not preserve
      the source template's mode bits either, so this is faithful, not a gap.

Windows portability: uses `tempfile.mkstemp(dir=tempfile.gettempdir())` for the
--check-only scratch file, NOT a hardcoded `/tmp` path (the oracle's own `mktemp
/tmp/...` would crash a clean Windows install where /tmp does not exist — this is a
platform-crash-class fix over the literal oracle, matching the identical fix already
applied in the sibling ports gen_claude_doe_launcher.py / gen_doe_root_pointer.py).

Transport-failure exit code note (porter-brief addendum § 3b): this module's own
exit codes are pure CLI-usage/business codes (0 success, 1 failure) — it has no
notion of a "transport" failure itself (no subprocess, no claude-klabauter-internal call).
The DoE-side trampoline's CLAUDE_KLABAUTER_ROOT-resolution / import-failure path (a distinct,
outer transport-failure class) maps to a DEDICATED exit code 2 — never a reused
business rc — per addendum rule A3b, so `run_required`-style callers can tell
"the install step's own business logic failed" (1) apart from "the claude-klabauter engine
link is broken" (2).
"""

from __future__ import annotations

import filecmp
import os
import sys
import tempfile
from typing import List, Optional

from coordinator_core.session.declared_writes import declare_write

_PROG = "gen-claude-doe-shim.sh"  # literal program-name prefix, matches the DoE filename

SENTINEL_BEGIN = "# --- coordinator claude-doe shim [generated] ---"
SENTINEL_END = "# --- end coordinator claude-doe shim ---"
# The legacy stopgap written by hand during the 2026-07-04 maximalist bringup.
LEGACY_MARKER = "# --- coordinator maximalist launch ---"
# The invariant lead-in shared by every observed hand-written variant of the
# marker line above -- real-world instances append a trailing comment/padding
# suffix (e.g. "... (DoE-resident plugin source) ----------------") after
# "launch", so detection matches this as a line PREFIX (after strip), never
# the full LEGACY_MARKER string as a whole-line equality check.
LEGACY_MARKER_PREFIX = "# --- coordinator maximalist launch"

EXPECTED_SOURCE_LINE = (
    '[ -f "${CLAUDE_HOME:-$HOME}/.claude/shell/claude-doe-shim.sh" ] '
    '&& source "${CLAUDE_HOME:-$HOME}/.claude/shell/claude-doe-shim.sh"'
)

# PowerShell counterpart of EXPECTED_SOURCE_LINE, selected by --shell powershell.
# Multi-line is fine: _extract_sentinel_body/_nonblank_join both operate over the
# whole sentinel body, not a single line. Mirrors the bash guard's semantics (only
# dot-source when the shim file exists, so a missing file never errors the
# profile) using $env:CLAUDE_HOME, falling back to $HOME exactly like the bash
# oracle's ${CLAUDE_HOME:-$HOME}.
EXPECTED_SOURCE_LINE_POWERSHELL = (
    "$__claude_doe_shim_base = if ($env:CLAUDE_HOME) { $env:CLAUDE_HOME } else { $HOME }\n"
    '$__claude_doe_shim_path = Join-Path $__claude_doe_shim_base ".claude\\shell\\claude-doe-shim.ps1"\n'
    "if (Test-Path $__claude_doe_shim_path) { . $__claude_doe_shim_path }"
)

SHELL_FAMILIES = ("bash", "powershell")

# Generator-provenance: writes the rendered shim under <CLAUDE_HOME|HOME>/
# .claude/shell/claude-doe-shim.sh and wires a sentinel block into the
# operator's interactive rc (~/.bashrc or ~/.zshrc) -- entirely outside
# claude-klabauter's own tracked tree.
GENERATES = []


def _shim_filename(shell_family: str) -> str:
    """bash/zsh render a POSIX-sourced ``.sh`` shim; PowerShell dot-sources a
    ``.ps1`` counterpart at the same ``.claude/shell/`` location."""
    return "claude-doe-shim.ps1" if shell_family == "powershell" else "claude-doe-shim.sh"


def _expected_source_line(shell_family: str) -> str:
    return EXPECTED_SOURCE_LINE_POWERSHELL if shell_family == "powershell" else EXPECTED_SOURCE_LINE

_USAGE = """\
Usage: gen-claude-doe-shim.sh [OPTIONS]

Writes ~/.claude/shell/claude-doe-shim.sh from the coordinator template and
ensures exactly one marked source block in the interactive rc file.

Options:
  --rc <path>         Target interactive rc file (overrides COORDINATOR_SHIM_RC
                      env var and $SHELL-based detection)
  --check-only        Validate without mutating any live file (renders to a
                      temp path, discards it, exits 0 on success)
  --template <path>   Override template source path (default: auto-resolved
                      relative to the DoE trampoline; useful for tests)
  --shell <family>    Target shell family: "bash" (covers bash/zsh) or
                      "powershell" (selects the .ps1 shim path, the pwsh
                      profile as the rc target, and a dot-source wired line
                      instead of a bash source line).
                      Default: "powershell" on Windows outside MSYS,
                      "bash" everywhere else.
  -h, --help          Show this help and exit

Environment:
  COORDINATOR_SHIM_RC    Override target rc path (--rc flag takes precedence)
  CLAUDE_HOME            Claude home BASE directory, i.e. the parent of .claude/
                         (default: $HOME; shim lands at $CLAUDE_HOME/.claude/shell/)

Exit codes:
  0 -- success (shim written / rc wired / already-idempotent no-op, or a clean
       --check-only pass)
  1 -- any failure: unknown argument, missing --template value, template not
       found, rc sentinel block hand-modified, rc file uncreatable, or (from the
       DoE trampoline) CLAUDE_KLABAUTER_ROOT/import resolution failure
"""


def _nonblank_join(text: str) -> str:
    """Mirror the bash oracle's `awk 'NF'` filter: drop whitespace-only lines,
    keep all others verbatim, rejoin with newlines (matches `$(... | awk 'NF')`
    command-substitution semantics, which also strips the trailing newline)."""
    return "\n".join(line for line in text.split("\n") if line.strip() != "")


def _extract_sentinel_body(text: str, begin: str, end: str) -> str:
    """Mirror the bash oracle's awk sentinel-body extractor:
    `$0 == b {f=1; next} $0 == e {f=0} f {print}` — lines strictly between an
    exact-match begin marker and an exact-match end marker, both exclusive."""
    lines: List[str] = []
    capturing = False
    for line in text.split("\n"):
        if line == begin:
            capturing = True
            continue
        if line == end:
            capturing = False
            continue
        if capturing:
            lines.append(line)
    return "\n".join(lines)


def _template_family_mismatch(tmpl_path: str, shell_family: str) -> Optional[str]:
    """Diagnostic when `tmpl_path` is unmistakably the OTHER shell family's
    template, else None.

    This generator copies template bytes VERBATIM but names the destination
    from `shell_family` (`_shim_filename`). Those two inputs come from
    different places — `--template` from the caller, `shell_family` from
    `--shell` or `_default_shell_family()` — and nothing reconciled them. A
    caller that hardcodes the `.sh` template and omits `--shell` therefore
    writes bash bytes into `claude-doe-shim.ps1` on native Windows, where the
    default family is `powershell`. The profile then dot-sources a file of
    bash, and `claude()` is never defined: the operator gets a plugin-less
    session with the sentinel block present and correct, so every downstream
    check says the install is healthy.

    Detection is deliberately narrow — only an explicit opposite-family
    extension counts. Neutral or arbitrary template names (test fixtures pass
    them) are not second-guessed.
    """
    base = os.path.basename(tmpl_path).lower()
    wrong = {
        "powershell": (".sh.tmpl", "bash"),
        "bash": (".ps1.tmpl", "powershell"),
    }.get(shell_family)
    if not wrong:
        return None
    suffix, other = wrong
    if not base.endswith(suffix):
        return None
    return (
        f"--template is the {other} template ({base}) but the resolved shell family is "
        f"{shell_family}, which writes {_shim_filename(shell_family)}. "
        f"Template bytes are copied verbatim, so this would install {other} into a "
        f"{shell_family} shim. Pass the matching template, or --shell {other}."
    )


def _commented_out_source_lines(rc_text: str, expected_source_line: str) -> List[str]:
    """Lines in `rc_text` that are this generator's OWN wired source line,
    commented out — i.e. a deliberately DISABLED shim block.

    A disabled block and a never-installed rc are indistinguishable to the
    sentinel check: neither carries an exact `SENTINEL_BEGIN` line, so both
    report "sentinel absent" and both are silently repaired. They mean very
    different things to an operator. Disabling this block switches off EVERY
    coordinator surface for every session on the box, and nothing else detects
    it — coordinator's own SessionStart hooks cannot warn that coordinator
    failed to load, because they do not run when it does not load. The
    detector has to live out here, in the installer.

    Detection is exact, not heuristic: a line qualifies only when stripping its
    leading comment markers yields one of `expected_source_line`'s own
    non-blank lines verbatim. Prose that merely mentions the shim (a hand-
    written note in the operator's profile, including the remediation notes
    this generator's own diagnostics suggest) never matches.

    Spec backlink: docs/reference/interactive-launch-chain.md § 4.
    """
    wanted = {line.strip() for line in expected_source_line.split("\n") if line.strip()}
    found: List[str] = []
    for raw in rc_text.split("\n"):
        stripped = raw.strip()
        if not stripped.startswith("#"):
            continue
        if stripped.lstrip("#").strip() in wanted:
            found.append(raw.rstrip())
    return found


def _disabled_block_report(target_rc: str, evidence: List[str]) -> List[str]:
    """Operator-facing lines for a detected disabled block.

    Remediation is a runnable command, never a slash command: this fires while
    the operator has no coordinator session, so naming an agentic remedy names
    one that cannot run.
    """
    lines = [
        f"WARNING: {target_rc} contains a DISABLED coordinator shim block "
        f"({len(evidence)} commented source line(s)).",
        "  While disabled, every session on this box runs without the coordinator plugin.",
    ]
    lines.extend(f"    {line}" for line in evidence)
    lines.append(f"  Delete those commented lines from {target_rc}; a live block is written below.")
    return lines


def _resolve_home() -> str:
    return (
        os.environ.get("HOME")
        or os.environ.get("USERPROFILE")
        or os.path.expanduser("~")
    )


def _resolve_claude_home_base() -> str:
    return os.environ.get("CLAUDE_HOME") or _resolve_home()


def _default_shell_family() -> str:
    """The shell family to target when --shell is not given.

    Windows defaults to ``powershell``; everything else (including Git Bash /
    MSYS, which sets MSYSTEM) keeps ``bash``. Without this, a Windows operator
    with no ``$SHELL`` fell through the POSIX ladder to "defaulting to
    ~/.zshrc", and the generator then wrote a POSIX ``.sh`` shim and edited a
    ``~/.zshrc`` that no shell on the box reads — a silently inert install whose
    own success message told the operator to run `source`, which is not a
    PowerShell command. An explicit ``--shell`` still wins.
    """
    if os.name == "nt" and not os.environ.get("MSYSTEM"):
        return "powershell"
    return "bash"


def _powershell_profile_path() -> str:
    """The pwsh 7+ per-user profile, matching the `.ps1` shim leg.

    Documents/PowerShell/Microsoft.PowerShell_profile.ps1 is pwsh 7's location;
    Windows PowerShell 5.1 uses Documents/WindowsPowerShell/. pwsh is the
    coordinator's Windows shell of record, so 7 is what gets wired.
    """
    home = _resolve_home()
    return os.path.join(home, "Documents", "PowerShell", "Microsoft.PowerShell_profile.ps1")


def _reload_hint(shell_family: str, target_rc: str) -> str:
    """The 'pick this up now' invocation for the wired rc file. PowerShell
    dot-sources with `. <path>`; `source` is a POSIX builtin and printing it to
    a PowerShell operator is a command that does not exist."""
    verb = "." if shell_family == "powershell" else "source"
    return f"{verb} {target_rc}"


def main(argv: List[str]) -> int:
    """CLI entry: arg parse, legacy-stopgap detect, render shim, wire rc source line.

    Emits a stdout ``claude_shim: <status>`` contract row on every exit path
    (Phase 7 install-status-table row), folding install.md's former if/echo
    wrapper directly into this CLI so the doc call collapses to one line
    (docs/plans/2026-07-23-skills-carry-no-code-extirpation.md § M3/D9).

    ``--graceful-skip-unresolved`` exits 0 with a ``skipped`` row (instead of
    attempting the write) when ``repos.doe_claude`` cannot be resolved via
    ``REPO_DOE_CLAUDE``/``machine-local`` — mirrors the install.md chain-order
    guard that used to live in the doc's own ``elif -n "${DOE_CLONE:-}"``
    branch. Has no effect under --check-only (its own validation contract is
    unchanged).

    Unrecognized argv tokens (other than a flag's own required value) are
    silently ignored — matches the pass-through-tolerant convention already
    established by the sibling install op ``register_coordinator_mirror``,
    needed so install.md can forward a whole ``${ARGUMENTS}`` blob (e.g.
    containing ``--non-interactive``/``--reconfigure``) without this
    generator failing on tokens meant for other install steps. Deliberate
    loosening from the prior strict fail-loud "Unknown argument" contract.
    """
    rc_override: Optional[str] = None
    check_only = False
    template_override: Optional[str] = None
    graceful_skip_unresolved = False
    shell_family = _default_shell_family()

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--rc":
            if i + 1 >= len(argv):
                print(f"{_PROG}: --rc requires a value. Run with --help for usage.", file=sys.stderr)
                return 1
            rc_override = argv[i + 1]
            i += 2
        elif arg == "--check-only":
            check_only = True
            i += 1
        elif arg == "--graceful-skip-unresolved":
            graceful_skip_unresolved = True
            i += 1
        elif arg == "--template":
            if i + 1 >= len(argv):
                print(f"{_PROG}: --template requires a value. Run with --help for usage.", file=sys.stderr)
                return 1
            template_override = argv[i + 1]
            i += 2
        elif arg == "--shell":
            if i + 1 >= len(argv):
                print(f"{_PROG}: --shell requires a value. Run with --help for usage.", file=sys.stderr)
                return 1
            shell_family = argv[i + 1]
            if shell_family not in SHELL_FAMILIES:
                print(
                    f'{_PROG}: --shell must be one of {", ".join(SHELL_FAMILIES)}, got '
                    f'"{shell_family}". Run with --help for usage.',
                    file=sys.stderr,
                )
                return 1
            i += 2
        elif arg in ("-h", "--help"):
            print(_USAGE, file=sys.stderr)
            return 0
        else:
            # Silently ignored -- see docstring.
            i += 1

    if graceful_skip_unresolved and not check_only:
        env_override = os.environ.get("REPO_DOE_CLAUDE", "")
        doe_resolved = bool(env_override)
        if not doe_resolved:
            from coordinator_core.machine_resolver import registry_get as _registry_get

            doe_resolved = bool(_registry_get("repos.doe_claude"))
            if not doe_resolved:
                # `registry_get` alone misses the CLI's autodiscovery/
                # `path-exceptions.toml` rungs -- fall back to the CLI on a
                # registry miss so this install gate doesn't skip the shim
                # write for a DoE clone the CLI would still resolve
                # (2026-08-16 review finding, repos.* 4-rung ladder).
                import shutil
                import subprocess

                ml_bin = shutil.which("machine-local")
                if ml_bin is not None:
                    try:
                        from coordinator_core.win_portability import no_console_creationflags

                        result = subprocess.run(
                            [ml_bin, "get", "repos.doe_claude"],
                            capture_output=True,
                            text=True,
                            check=False,
                            **no_console_creationflags(),
                        )
                        doe_resolved = result.returncode == 0 and bool(result.stdout.strip())
                    except OSError:
                        doe_resolved = False
        if not doe_resolved:
            print("claude_shim: skipped (DoE clone not resolved — complete step 3.5a first)")
            return 0

    if not template_override:
        # This module has no co-located DoE-side script path to derive the oracle's
        # `${_script_dir}/../templates/shell/claude-doe-shim.sh.tmpl` default from —
        # the DoE trampoline resolves that default itself (relative to its own
        # on-disk location) and always passes --template explicitly, so this branch
        # is reached only when a caller invokes main() directly without one. Fail
        # loud rather than guess a cwd-relative path that could silently pick up the
        # wrong template.
        print(
            f"{_PROG}: --template not supplied and no default resolvable "
            "(the DoE trampoline should always pass one explicitly).",
            file=sys.stderr,
        )
        print("claude_shim: failed (see stderr for gen-claude-doe-shim.py output)")
        return 1

    tmpl_path = os.path.realpath(template_override)
    mismatch = _template_family_mismatch(tmpl_path, shell_family)
    if mismatch:
        print(f"{_PROG}: {mismatch}", file=sys.stderr)
        print("claude_shim: failed (template/--shell mismatch)")
        return 1
    if not os.path.isfile(tmpl_path):
        print(f"{_PROG}: Template not found: {tmpl_path}", file=sys.stderr)
        print("claude_shim: failed (see stderr for gen-claude-doe-shim.py output)")
        return 1

    expected_source_line = _expected_source_line(shell_family)

    claude_home_base = _resolve_claude_home_base()
    claude_dir = os.path.join(claude_home_base, ".claude")
    shell_dir = os.path.join(claude_dir, "shell")
    shim_dest = os.path.join(shell_dir, _shim_filename(shell_family))

    # ---- detect legacy stopgap (faithful: reads $HOME/.bashrc, not CLAUDE_HOME) ----
    bashrc = os.path.join(_resolve_home(), ".bashrc")
    if os.path.isfile(bashrc):
        try:
            bashrc_text = open(bashrc, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            bashrc_text = ""
        if any(
            line.strip().startswith(LEGACY_MARKER_PREFIX) for line in bashrc_text.split("\n")
        ):
            print(
                f"NOTE (migration): legacy coordinator maximalist launch claude() block "
                f"detected in {bashrc}.",
                file=sys.stderr,
            )
            print("  This block is superseded by the new shim. To clean up, remove the", file=sys.stderr)
            print(
                f'  block between "{LEGACY_MARKER}" and its end marker from {bashrc}',
                file=sys.stderr,
            )
            print(
                "  (or run coordinator-uninstall when available — it handles this surface).",
                file=sys.stderr,
            )

    # ---- resolve target rc, first hit wins: the --rc flag, then the
    # COORDINATOR_SHIM_RC env var, then SHELL-based detection ----
    if rc_override:
        target_rc = rc_override
    elif os.environ.get("COORDINATOR_SHIM_RC"):
        target_rc = os.environ["COORDINATOR_SHIM_RC"]
    else:
        home = _resolve_home()
        if shell_family == "powershell":
            target_rc = _powershell_profile_path()
        elif os.environ.get("MSYSTEM"):
            target_rc = os.path.join(home, ".bashrc")
        else:
            shell_env = os.environ.get("SHELL", "")
            if shell_env.endswith("/zsh") or shell_env.endswith("/zsh.exe"):
                target_rc = os.path.join(home, ".zshrc")
            elif shell_env.endswith("/bash") or shell_env.endswith("/bash.exe"):
                target_rc = os.path.join(home, ".bashrc")
            else:
                print(
                    f'WARN: $SHELL="{shell_env or "<unset>"}" not recognised; defaulting to '
                    "~/.zshrc. Use --rc to override.",
                    file=sys.stderr,
                )
                target_rc = os.path.join(home, ".zshrc")

    # ---- check-only path ----
    if check_only:
        tmp_fd, tmp_shim = tempfile.mkstemp(
            prefix="claude-doe-shim-check.", dir=tempfile.gettempdir()
        )
        os.close(tmp_fd)
        shim_fresh = False
        try:
            with open(tmpl_path, "rb") as fh:
                content = fh.read()
            with open(tmp_shim, "wb") as fh:
                fh.write(content)
            shim_lines = content.count(b"\n")
            if os.path.isfile(shim_dest) and filecmp.cmp(tmp_shim, shim_dest, shallow=False):
                shim_fresh = True
                print(
                    f"[check-only] {shim_dest}: up to date ({tmpl_path}, {shim_lines} lines) (no-op)",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[check-only] Template valid: {tmpl_path} ({shim_lines} lines) -> "
                    f"would write to {shim_dest} (discarded)",
                    file=sys.stderr,
                )
        finally:
            try:
                os.remove(tmp_shim)
            except OSError:
                print(f"skip: main: os.remove(tmp_shim) failed: {sys.exc_info()[1]}", file=sys.stderr)
                pass

        if os.path.isfile(target_rc):
            with open(target_rc, "r", encoding="utf-8", errors="replace") as fh:
                rc_text = fh.read()
            if SENTINEL_BEGIN in rc_text.split("\n"):
                body = _extract_sentinel_body(rc_text, SENTINEL_BEGIN, SENTINEL_END)
                body_trimmed = _nonblank_join(body)
                expected_trimmed = _nonblank_join(expected_source_line)
                if body_trimmed == expected_trimmed:
                    print(
                        f"[check-only] rc {target_rc}: sentinel block present and "
                        "unmodified (would be no-op)",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"[check-only] rc {target_rc}: sentinel block present but "
                        "HAND-MODIFIED — would fail-loud",
                        file=sys.stderr,
                    )
                    print(f"  Expected: {expected_trimmed}", file=sys.stderr)
                    print(f"  Found:    {body_trimmed}", file=sys.stderr)
                    print("claude_shim: failed in check-only validation (see stderr)")
                    return 1
            else:
                disabled = _commented_out_source_lines(rc_text, expected_source_line)
                if disabled:
                    # A DISABLED block is a live misconfiguration, not a
                    # pending install: this box is running every session
                    # without coordinator right now. check-only exists to
                    # report exactly that, so it fails rather than reporting
                    # the same "would add source block" as a clean machine.
                    for line in _disabled_block_report(target_rc, disabled):
                        print(f"[check-only] {line}", file=sys.stderr)
                    print("claude_shim: check failed: shim block is disabled (see stderr)")
                    return 1
                print(
                    f"[check-only] rc {target_rc}: sentinel absent — would add source block",
                    file=sys.stderr,
                )
        else:
            print(
                f"[check-only] rc {target_rc}: sentinel absent — would add source block",
                file=sys.stderr,
            )
        if shim_fresh:
            print(f"claude_shim: ready (no-op) ({shim_dest})")
            return 0
        print(f"claude_shim: check failed: {shim_dest} is stale or absent (would install)")
        return 1

    # ---- render shim (atomic) ----
    os.makedirs(shell_dir, exist_ok=True)
    tmp_fd, tmp_shim = tempfile.mkstemp(
        prefix=f"{os.path.basename(shim_dest)}.tmp.", dir=shell_dir
    )
    try:
        with open(tmpl_path, "rb") as fh:
            content = fh.read()
        with os.fdopen(tmp_fd, "wb") as fh:
            fh.write(content)
        os.replace(tmp_shim, shim_dest)
        # DR-276: declared AFTER the write lands, at the FINAL destination,
        # never the discarded tmp_shim.
        declare_write(shim_dest)
    except OSError:
        try:
            os.remove(tmp_shim)
        except OSError:
            print(f"skip: main: os.remove(tmp_shim) failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass
        print("claude_shim: failed (see stderr for gen-claude-doe-shim.py output)")
        raise
    print(f"Wrote shim: {shim_dest}", file=sys.stderr)

    # ---- wire source line into rc (sentinel-guarded, detect-then-fail-loud on hand-mod) ----
    if not os.path.isfile(target_rc):
        try:
            # The rc file's PARENT may not exist: a POSIX rc sits directly in
            # $HOME, but the pwsh profile lives at
            # Documents/PowerShell/Microsoft.PowerShell_profile.ps1 and pwsh does
            # not create that directory until a profile is first saved. Without
            # this the Windows leg failed with "Cannot create rc file" on any box
            # where the operator had never written a profile.
            parent = os.path.dirname(target_rc)
            if parent:
                os.makedirs(parent, exist_ok=True)
            open(target_rc, "a", encoding="utf-8", newline="\n").close()
        except OSError as exc:
            print(f"{_PROG}: ERROR: Cannot create rc file: {target_rc}: {exc}", file=sys.stderr)
            print("claude_shim: failed (see stderr for gen-claude-doe-shim.py output)")
            return 1
        print(f"Created rc file: {target_rc}", file=sys.stderr)

    with open(target_rc, "r", encoding="utf-8", errors="replace") as fh:
        rc_text = fh.read()

    rc_block_was_noop = False
    if SENTINEL_BEGIN in rc_text.split("\n"):
        body = _extract_sentinel_body(rc_text, SENTINEL_BEGIN, SENTINEL_END)
        body_trimmed = _nonblank_join(body)
        expected_trimmed = _nonblank_join(expected_source_line)
        if body_trimmed == expected_trimmed:
            print(f"rc {target_rc}: sentinel block unchanged (no-op)", file=sys.stderr)
            rc_block_was_noop = True
        else:
            print(f"ERROR: sentinel block in {target_rc} has been hand-modified.", file=sys.stderr)
            print(f"  Expected body: {expected_trimmed}", file=sys.stderr)
            print(f"  Found body:    {body_trimmed}", file=sys.stderr)
            print(
                f'  To reset: remove the block between "{SENTINEL_BEGIN}" and '
                f'"{SENTINEL_END}" from {target_rc} and re-run.',
                file=sys.stderr,
            )
            print("claude_shim: failed (see stderr for gen-claude-doe-shim.py output)")
            return 1
    else:
        # Repair path. A disabled block is repaired the same way as a missing
        # one (the live block below re-enables coordinator), but it is reported
        # first: the operator needs to know the box HAS been running without
        # coordinator, and the commented lines are theirs to delete — this
        # generator never edits an operator's own text.
        disabled = _commented_out_source_lines(rc_text, expected_source_line)
        if disabled:
            for line in _disabled_block_report(target_rc, disabled):
                print(line, file=sys.stderr)
        tmp_rc = f"{target_rc}.tmp.{os.getpid()}"
        try:
            new_block = f"\n{SENTINEL_BEGIN}\n{expected_source_line}\n{SENTINEL_END}\n"
            with open(tmp_rc, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(rc_text)
                fh.write(new_block)
            os.replace(tmp_rc, target_rc)
            # DR-276: declared AFTER the write lands, at the FINAL
            # destination, never the discarded tmp_rc.
            declare_write(target_rc)
        except OSError:
            try:
                os.remove(tmp_rc)
            except OSError:
                print(f"skip: main: os.remove(tmp_rc) failed: {sys.exc_info()[1]}", file=sys.stderr)
                pass
            print("claude_shim: failed (see stderr for gen-claude-doe-shim.py output)")
            raise
        print(f"rc {target_rc}: source block added", file=sys.stderr)

    print(
        f"Done. Open a new terminal (or run: {_reload_hint(shell_family, target_rc)}) "
        "for claude() to take effect.",
        file=sys.stderr,
    )
    print(f"claude_shim: {'ready (no-op)' if rc_block_was_noop else f'installed ({shim_dest})'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
