"""normalize_env — Python reimplementation of coordinator/scripts/normalize-env.sh.

Idempotent environment-normalization for coordinator install Step Zero. The
SOLE writer of environment state during coordinator setup: reads probe
results from the native `coordinator_core.install.prereq_probe` port and
applies consent-gated fixes for fixable prereqs. Windows-first mutations;
macOS/Linux prints offers only (no silent mutation on non-Windows) — same
posture as the bash oracle.

Port of: normalize-env.sh (DoE ca30f76c, 2026-07-17), 1075 lines, bash 3.2-safe
oracle. This module is byte-parity-verified against the oracle's plan/dry-run and --restore
surfaces; the Windows-only mutation surfaces (winget Python install, App
Execution alias disable via embedded PowerShell) are structurally ported but
cannot be executed end-to-end on this dev machine (macOS) — see
test_normalize_env.py fresh-install-shape smoke test and its own docstring.

Probe delegation (2026-07-21 de-bash cutover — see git log): the read-only
probe layer previously bridged to DoE's scripts/lib/prereq_probe.sh via a
`bash -c 'source ...; <fn>'` subprocess per probe call. It now calls
`coordinator_core.install.prereq_probe`'s already-landed native port
in-process — no subprocess, no bash dependency, same probe SSOT (that
module's `probe_all`/`probe_longpaths`/`probe_uv`/`probe_python`/
`shell_login_env_reconstruction_source`). The oracle-era `prereq_probe_path`
/ `probes_available` file-presence gate was REMOVED on 2026-08-14: it keyed
"can this run probe?" off the on-disk presence of `scripts/lib/prereq_probe.sh`,
a file deleted on 2026-07-22. Every documented invocation therefore hard-errored
("prereq_probe.sh not found at ..."), and any invocation that bypassed the
trampoline skipped every probe and printed "All prereqs satisfied. Nothing to
do." — a green result from a run that probed nothing, which on a cold macOS box
is precisely the step meant to catch the bash floor.

Negative-spec: do NOT reintroduce a file-existence gate around the probe layer.
The probes are in-process Python; their availability is an import-time property,
not a filesystem one. If a future probe genuinely can become unavailable, the
outcome must be indeterminate (non-zero), never "all prereqs satisfied".

Negative-spec (retired bash-oracle behavior — do NOT reintroduce differently):
    - Never mutate before the consent prompt (CONSENT-INVARIANT) — the backup
      file write is informational, not a mutation of PATH/shim/alias state;
      it precedes the prompt by design (oracle comment, lines 799-802).
    - Never touch PATH/shim/alias state on macOS/Linux — offers only.
    - Blast-radius-last mutation ordering on Windows: (1) core.longpaths
      [low], (2) uv [low], (3) Python 3.12 [medium], (4) App Execution alias
      disable [riskier] — preserved verbatim; do not reorder.
    - core.longpaths and uv install are byte-stable idempotent no-ops on
      re-run; PATH/shim/alias operations converge toward target state but are
      NOT byte-stable (documented oracle caveat, preserved here).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from coordinator_core.install import prereq_probe
from coordinator_core.win_portability import no_console_creationflags

# mutates only operator machine state outside any repo: ~/.bash_profile,
# ~/.coordinator-env-backup.<ts> / ~/.bash_profile.coordinator-backup.<ts>,
# and Windows git-config-global/winget/PowerShell alias state
GENERATES = []

_PROBE_TIMEOUT_SECS = 30
_MUTATION_TIMEOUT_SECS = 300

_CREATIONFLAGS = no_console_creationflags()

# Exit codes — preserved verbatim from the bash oracle (business-code parity).
EXIT_OK = 0
EXIT_HARD_ERROR = 1  # backup-file write failure
EXIT_USAGE_ERROR = 2  # unknown arg / --restore missing file / --restore file not found
EXIT_NONINTERACTIVE_ABORT = 3  # EOF / no-tty consent gate, --yes not passed
EXIT_PARTIAL_FAILURE = 4  # one or more mutation steps failed


# ---------------------------------------------------------------------------
# Unit 1 — flags/usage, OS detection, timestamp, backup/restore path handling.
# ---------------------------------------------------------------------------

def _ne_is_windows() -> bool:
    """Mirror the oracle's uname-based check (MINGW*/MSYS*/CYGWIN*) plus the
    native-Python case (a real python.exe running natively on Windows, which
    the bash oracle — itself bash-hosted — never had to handle)."""
    if sys.platform == "win32":
        return True
    uname = shutil.which("uname")
    if not uname:
        return False
    try:
        out = subprocess.run(
            [uname, "-s"],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        print(f"skip: _ne_is_windows: out = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    return out.startswith(("MINGW", "MSYS", "CYGWIN"))


def _ne_timestamp() -> str:
    """Portable YYYYMMDDHHMMSS timestamp; '00000000000000' fallback mirrors
    the oracle's `date +%Y%m%d%H%M%S || echo fallback` shape."""
    try:
        return datetime.now().strftime("%Y%m%d%H%M%S")
    except Exception:
        print(f"skip: _ne_timestamp: return datetime.now().strftime(\"%Y%m%d%H%M%S\") failed: {sys.exc_info()[1]}", file=sys.stderr)
        return "00000000000000"


def _ne_parse_args(argv: list) -> dict:
    """Manual parse loop mirroring the oracle's case-based arg parser.

    Returns a dict with keys: dry_run, yes, restore_file, error (Optional[str]),
    error_exit (Optional[int]).
    """
    result = {"dry_run": False, "yes": False, "restore_file": None}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--dry-run":
            result["dry_run"] = True
            i += 1
        elif arg == "--yes":
            result["yes"] = True
            i += 1
        elif arg == "--restore":
            if i + 1 >= len(argv):
                raise _NeUsageError("ERROR: --restore requires a backup file argument.")
            result["restore_file"] = argv[i + 1]
            i += 2
        elif arg == "--":
            i += 1
            break
        else:
            raise _NeUsageError(
                f"ERROR: unknown argument: {arg}\n"
                f"Usage: normalize-env [--dry-run] [--yes] [--restore <backup-file>]"
            )
    return result


class _NeUsageError(Exception):
    """Raised on CLI usage errors; caller maps to EXIT_USAGE_ERROR."""


class _NeNonInteractiveAbort(Exception):
    """Raised when the consent gate hits EOF/no-tty without --yes."""


def _ne_backup_file_path(home: Path, ts: str) -> Path:
    return home / f".coordinator-env-backup.{ts}"


def _ne_bash_profile_backup_path(home: Path, ts: str) -> Path:
    return home / f".bash_profile.coordinator-backup.{ts}"


def _ne_handle_restore(restore_file: str, home: Path, out) -> int:
    """--restore <backup-file>: revert PATH/shim to saved values, or restore a
    full ~/.bash_profile snapshot. Mirrors oracle lines 150-208."""
    restore_path = Path(restore_file)
    if not restore_path.is_file():
        print(f"ERROR: backup file not found: {restore_file}", file=sys.stderr)
        return EXIT_USAGE_ERROR

    print("=== normalize-env --restore ===", file=out)
    print(f"Reading backup: {restore_file}", file=out)

    if restore_path.name.startswith(".bash_profile.coordinator-backup."):
        try:
            shutil.copyfile(restore_path, home / ".bash_profile")
        except OSError:
            print(f"ERROR: failed to copy {restore_file} onto ~/.bash_profile", file=sys.stderr)
            return EXIT_USAGE_ERROR
        print(f"  ~/.bash_profile restored from {restore_file}", file=out)
        return EXIT_OK

    saved_path = None
    saved_pymanager = None
    try:
        text = restore_path.read_text(errors="replace")
    except OSError:
        text = ""
    for line in text.splitlines():
        if line.startswith("SAVED_PATH="):
            saved_path = line[len("SAVED_PATH="):]
        elif line.startswith("SAVED_PYMANAGER_TARGET="):
            saved_pymanager = line[len("SAVED_PYMANAGER_TARGET="):]

    if not saved_path:
        print("WARNING: backup file does not contain SAVED_PATH — cannot restore PATH.", file=sys.stderr)
    else:
        print("To restore PATH, run the following in your parent shell:", file=out)
        print(f'  export PATH="{saved_path}"', file=out)

    if not saved_pymanager:
        print("  (No SAVED_PYMANAGER_TARGET in backup — pymanager shim restore skipped.)", file=out)
    else:
        print(f"  (Pymanager shim restore must be applied manually: see {restore_file})", file=out)
        print(f"  Saved shim target was: {saved_pymanager}", file=out)

    print("Restore complete. Verify with: python --version", file=out)
    return EXIT_OK


# ---------------------------------------------------------------------------
# Unit 2 — success/failure trackers, y/N consent prompt, probe dispatch table.
# ---------------------------------------------------------------------------

class NeRunner:
    """Holds the mutable step-outcome trackers the oracle carried as globals
    (_ne_steps_succeeded / _ne_steps_failed) plus the --yes flag consumed by
    the consent gate. One instance per invocation."""

    def __init__(self, yes: bool = False):
        self.yes = yes
        self.steps_succeeded: list = []
        self.steps_failed: list = []

    def record_success(self, step: str) -> None:
        self.steps_succeeded.append(step)

    def record_failure(self, step: str) -> None:
        self.steps_failed.append(step)

    def prompt_yn(self, prompt: str, out, in_stream=None) -> bool:
        """Returns True for yes. Raises _NeNonInteractiveAbort on EOF/no-tty
        without --yes (CONSENT-INVARIANT: zero mutations on that path)."""
        if self.yes:
            print(f"{prompt} [auto-yes via --yes]", file=out)
            return True

        stream = in_stream if in_stream is not None else sys.stdin
        is_tty = bool(getattr(stream, "isatty", lambda: False)())
        if not is_tty:
            print(prompt, file=out)
            print("Non-interactive input detected (no tty / EOF). Aborting — no mutations applied.", file=out)
            print("Re-run with --yes to apply in non-interactive mode, or run interactively.", file=out)
            raise _NeNonInteractiveAbort()

        print(f"{prompt} [y/N]: ", file=out, end="")
        try:
            answer = stream.readline()
        except OSError:
            answer = ""
        if answer == "":
            print("\nEOF detected. Aborting — no mutations applied.", file=out)
            raise _NeNonInteractiveAbort()
        answer = answer.rstrip("\n")
        return answer.strip().lower() in ("y", "yes")


_PROBE_FN_DISPATCH = {
    "_co_prereq_probe_all": lambda: "".join(prereq_probe.probe_all()),
    "_co_probe_longpaths": prereq_probe.probe_longpaths,
    "_co_probe_uv": prereq_probe.probe_uv,
    "_co_probe_python": prereq_probe.probe_python,
    "_co_shell_login_env_reconstruction_source": prereq_probe.shell_login_env_reconstruction_source,
}


def _ne_run_probe_fn(fn_expr: str) -> str:
    """Invoke one of the native `coordinator_core.install.prereq_probe` port's
    functions in-process, keyed by *fn_expr* (the bash-oracle function name
    this call site historically invoked via a `bash -c 'source ...; <fn>'`
    subprocess — see module docstring § Probe delegation, 2026-07-21
    de-bash cutover). No subprocess, no lib file to source, no timeout to
    bound: the former `prereq_probe_path`/`timeout` parameters were dead
    weight and are gone.
    """
    fn = _PROBE_FN_DISPATCH.get(fn_expr)
    if fn is None:
        return ""
    return fn()


def _ne_parse_probe_lines(probe_output: str) -> list:
    """Parse NDJSON probe output (one _co_pp_emit JSON object per line) into
    a list of dicts. Malformed/blank lines are skipped (mirrors the oracle's
    sed-BRE no-match tolerance)."""
    records = []
    for line in probe_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            print(f"skip: _ne_parse_probe_lines: records.append(json.loads(line)) failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue
    return records


def _ne_probe_field_for(probe_output: str, name: str, field: str, default: str = "") -> str:
    for rec in _ne_parse_probe_lines(probe_output):
        if rec.get("name") == name:
            return str(rec.get(field, default))
    return "inconclusive" if field == "status" else default


def _ne_probe_status_for(probe_output: str, name: str) -> str:
    return _ne_probe_field_for(probe_output, name, "status")


def _ne_probe_remediation_for(probe_output: str, name: str) -> str:
    return _ne_probe_field_for(probe_output, name, "remediation")


def _ne_probe_detail_for(probe_output: str, name: str) -> str:
    return _ne_probe_field_for(probe_output, name, "detail")


# ---------------------------------------------------------------------------
# Unit 3 — probe-output capture + summary rendering (pre-fix report), and the
# macOS/Linux offers-only path (including Darwin bash-profile reconstruction).
# ---------------------------------------------------------------------------

_NE_START_SENTINEL = "# coordinator-install: brew shellenv (DR-148)"
_NE_END_SENTINEL = "# coordinator-install: end (DR-148)"

_HOMEBREW_MARKERS = ("/homebrew/", "/Homebrew/")
_SYSTEM_DEFAULT_PATHS = {"/usr/bin", "/bin", "/usr/sbin", "/sbin", "/usr/local/bin", "/usr/local/sbin"}


def _ne_print_probe_summary(probe_output: str, out) -> None:
    """Faithful oracle-bug repro (porter-brief rule 7 — do NOT silently fix):
    the bash oracle captures `_co_prereq_probe_all` via `$(...)`, which strips
    the trailing newline `_co_pp_emit` always appends. It then pipes that
    string through `while IFS= read -r line; do ...; done`, and bash's `read`
    returns non-zero (loop body skipped) on the final line when that line has
    no trailing newline terminator — so the LAST probe record (always
    `shell_login_env`, the aggregator's final call) is silently dropped from
    every printed probe summary, on BOTH the macOS/Linux and Windows paths.
    Status LOOKUPS (`_ne_probe_status_for` et al., built on `grep`, not
    `read`) are unaffected — only the printed summary loses the last row.
    Verified empirically byte-for-byte against the oracle; reproduced here by
    dropping the last record from the DISPLAY list only, never from lookups.
    """
    records = _ne_parse_probe_lines(probe_output)
    if records:
        records = records[:-1]
    for rec in records:
        print(f"  [{rec.get('status', '')}] {rec.get('name', '')}: {rec.get('detail', '')}", file=out)


def _ne_split_path_entries(path_value: str) -> list:
    return [p for p in path_value.split(os.pathsep) if p]


def _ne_extra_zsh_path_entries(recon_source: str) -> list:
    """Non-homebrew, non-system-default entries from the zsh snapshot — the
    entries the reconstructed ~/.bash_profile still needs to export explicitly."""
    extras = []
    for entry in _ne_split_path_entries(recon_source):
        if any(marker in entry for marker in _HOMEBREW_MARKERS):
            continue
        if entry in _SYSTEM_DEFAULT_PATHS:
            continue
        extras.append(entry)
    return extras


def _ne_strip_managed_block(content: str) -> str:
    """Mirrors the oracle's two awk block-replace strategies (both-sentinels
    present: remove start..end inclusive; legacy start-only: remove start
    through the next bare 'fi' line). No sentinel: content unchanged."""
    lines = content.splitlines()
    if _NE_END_SENTINEL in lines:
        out_lines = []
        blanking = False
        for line in lines:
            if line == _NE_START_SENTINEL:
                blanking = True
                continue
            if line == _NE_END_SENTINEL:
                blanking = False
                continue
            if not blanking:
                out_lines.append(line)
        return "\n".join(out_lines)
    if _NE_START_SENTINEL in lines:
        out_lines = []
        blanking = False
        for line in lines:
            if line == _NE_START_SENTINEL:
                blanking = True
                continue
            if blanking and line.strip() == "fi":
                blanking = False
                continue
            if not blanking:
                out_lines.append(line)
        return "\n".join(out_lines)
    return content


def _ne_build_managed_block(recon_source: str) -> str:
    lines = [
        "",
        _NE_START_SENTINEL,
        "# --- coordinator-managed block -- do not edit between these sentinel lines ---",
        "# Boundaries:",
        "#   (a) rc functions/aliases from the prior shell are NOT carried here --",
        "#       only PATH is snapshotted from the intact zsh login environment.",
        "#   (b) Non-login-interactive bash shells read ~/.bashrc, not ~/.bash_profile",
        "#       (macOS Terminal new-tab is often non-login); consider also adding",
        "#       ~/.local/bin to ~/.bashrc for full coverage in those shells.",
        "#   (c) Reciprocal-source-loop hazard: if ~/.bash_profile sources ~/.bashrc",
        "#       and ~/.bashrc sources ~/.bash_profile, a loop results; the guard",
        "#       variable _COORDINATOR_BASH_PROFILE_SOURCED below breaks that loop.",
        '[ -n "${_COORDINATOR_BASH_PROFILE_SOURCED:-}" ] && return 2>/dev/null; export _COORDINATOR_BASH_PROFILE_SOURCED=1',
        'if [ -x /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"; elif [ -x /usr/local/bin/brew ]; then eval "$(/usr/local/bin/brew shellenv)"; fi',
    ]
    for entry in _ne_extra_zsh_path_entries(recon_source):
        lines.append(f'export PATH="{entry}:$PATH"')
    lines.append("[ -f ~/.bashrc ] && [ -r ~/.bashrc ] && . ~/.bashrc")
    lines.append(_NE_END_SENTINEL)
    return "\n".join(lines) + "\n"


def _ne_verify_bash_profile_repair(timeout: int = 15) -> bool:
    """Spawn a login bash to confirm ~/.local/bin is now in PATH AND claude
    resolves — the Staff Engineer F5a. Returns False (do NOT declare repaired) on any
    subprocess failure, matching the oracle's fail-closed verify step."""
    bash = shutil.which("bash")
    if not bash:
        return False
    script = (
        'case ":$PATH:" in *":$HOME/.local/bin:"*) '
        "command -v claude >/dev/null 2>&1 && exit 0;; esac; exit 1"
    )
    try:
        proc = subprocess.run(
            [bash, "-lc", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
    except (OSError, subprocess.SubprocessError):
        print(f"skip: _ne_verify_bash_profile_repair: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    return proc.returncode == 0


def _ne_darwin_bash_profile_repair(
    probe_output: str, home: Path, dry_run: bool, runner: NeRunner, out, in_stream=None
) -> None:
    """C2: bash login-shell PATH reconstruction (Darwin only). Spec backlink:
    docs/plans/2026-06-22-coordinator-env-normalization-step-zero.md (C2).
    Triggered when shell_login_env probe reports 'fail'."""
    if sys.platform == "win32":
        return
    shell_env_status = _ne_probe_status_for(probe_output, "shell_login_env")
    if shell_env_status != "fail":
        return

    print("--- bash login-shell PATH repair ---", file=out)
    print("  Probe shell_login_env=fail: bash login shell has orphaned ~/.local/bin.", file=out)
    print("  This step reconstructs ~/.bash_profile from the intact zsh login PATH.", file=out)
    print("  Source: zsh login shell (untouched by the bash orphan — not the broken bash).", file=out)
    print("  NO chsh is performed — only ~/.bash_profile is updated.\n", file=out)

    recon_source = _ne_run_probe_fn("_co_shell_login_env_reconstruction_source").strip()

    if not recon_source:
        print("  INCONCLUSIVE: zsh login PATH could not be probed (zsh absent or unprobeable).", file=out)
        print("  Cannot reconstruct ~/.bash_profile safely — empty PATH block would orphan tools.", file=out)
        print("  Manual repair: add the following to ~/.bash_profile:", file=out)
        print('    export PATH="$HOME/.local/bin:$PATH"\n', file=out)
        return

    print(f"  Reconstruction source (intact zsh PATH):\n    {recon_source}\n", file=out)

    if dry_run:
        print("(--dry-run) Would reconstruct ~/.bash_profile with managed block:", file=out)
        print(f"  Start sentinel: {_NE_START_SENTINEL}", file=out)
        print(f"  End sentinel:   {_NE_END_SENTINEL}", file=out)
        print("  Non-homebrew PATH entries from zsh snapshot that would be added:", file=out)
        for entry in _ne_extra_zsh_path_entries(recon_source):
            print(f'    export PATH="{entry}:$PATH"', file=out)
        print("(--dry-run: no mutations applied.)\n", file=out)
        return

    try:
        proceed = runner.prompt_yn(
            "Reconstruct ~/.bash_profile to repair bash login-shell PATH", out, in_stream
        )
    except _NeNonInteractiveAbort:
        raise
    if not proceed:
        print("  Skipped by user.", file=out)
        return

    bp_path = home / ".bash_profile"
    ts = _ne_timestamp()
    backup_path = _ne_bash_profile_backup_path(home, ts)
    runner.bash_profile_backup_path = None

    if bp_path.is_file():
        try:
            shutil.copyfile(bp_path, backup_path)
            print(f"  Backup written: {backup_path}", file=out)
            print(f"  To revert manually: cp {backup_path} {bp_path}", file=out)
            runner.bash_profile_backup_path = backup_path
        except OSError:
            print(f"  ERROR: could not write backup to {backup_path} — aborting repair.", file=sys.stderr)
            return

    try:
        prior_content = bp_path.read_text(errors="replace") if bp_path.is_file() else ""
    except OSError:
        print("  ERROR: failed to produce base content for ~/.bash_profile; prior content preserved.", file=sys.stderr)
        return

    base_content = _ne_strip_managed_block(prior_content) if prior_content else ""
    new_content = base_content + _ne_build_managed_block(recon_source)

    # Review: code-reviewer (Finding 1, A5) — mkstemp creates the temp file at mode
    # 0600; os.replace swaps the inode without carrying the destination's prior
    # permission bits forward, so a pre-existing 0644 ~/.bash_profile was silently
    # narrowed to 0600 on every successful repair. Preserve the original mode
    # (falling back to 0644 for a fresh file) before the atomic swap.
    try:
        prior_mode = os.stat(bp_path).st_mode & 0o777 if bp_path.is_file() else 0o644
    except OSError:
        prior_mode = 0o644

    tmp_fd, tmp_name = tempfile.mkstemp(prefix=".bash_profile.tmp.", dir=str(home))
    try:
        with os.fdopen(tmp_fd, "w") as fh:
            fh.write(new_content)
        os.chmod(tmp_name, prior_mode)
        os.replace(tmp_name, bp_path)
    except OSError:
        print("  ERROR: mv temp to ~/.bash_profile failed.", file=sys.stderr)
        try:
            os.unlink(tmp_name)
        except OSError:
            print(f"skip: _ne_darwin_bash_profile_repair: os.unlink(tmp_name) failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass
        runner.record_failure("$HOME/.bash_profile reconstruction (mv to target failed)")
        return

    print("  ~/.bash_profile written.", file=out)
    print("  Verifying repair via login bash...", file=out)
    if _ne_verify_bash_profile_repair():
        print("  Repair VERIFIED: login bash now has ~/.local/bin and claude resolves.", file=out)
        runner.record_success("$HOME/.bash_profile reconstructed (bash login-shell PATH repaired)")
    else:
        print("  Repair FAILED: login bash still fails the orphan predicate.", file=sys.stderr)
        print("  Do NOT declare repaired. Backup is restorable:", file=sys.stderr)
        if runner.bash_profile_backup_path:
            print(f"    cp {runner.bash_profile_backup_path} {bp_path}", file=sys.stderr)
        runner.record_failure("$HOME/.bash_profile reconstruction (verify: login bash still orphaned)")


_NE_POSIX_MANUAL_STEPS = {
    "Darwin": (
        "This script does not mutate on macOS. Manual steps to satisfy prereqs:\n\n"
        "  Python 3.12+:\n"
        "    brew install python@3.12\n\n"
        "  uv package manager:\n"
        "    brew install uv\n\n"
        "  PowerShell 7 (optional):\n"
        "    brew install --cask powershell\n\n"
        "  git core.longpaths: Windows-only — not applicable on macOS.\n"
    ),
    "Linux": (
        "This script does not mutate on Linux. Manual steps to satisfy prereqs:\n\n"
        "  Python 3.12+:\n"
        "    (Debian/Ubuntu) sudo apt-get install python3.12\n"
        "    (Fedora/RHEL)   sudo dnf install python3.12\n\n"
        "  uv package manager:\n"
        "    pipx install uv   OR   pip install uv\n\n"
        "  PowerShell 7 (optional):\n"
        "    See https://learn.microsoft.com/powershell/scripting/install/installing-powershell-on-linux\n\n"
        '  git core.longpaths: Windows-only — not applicable on Linux.\n'
    ),
}


def _ne_posix_path(
    os_name: str, dry_run: bool, yes: bool, home: Path, out, in_stream=None
) -> int:
    """macOS/Linux: print offers; Darwin additionally offers bash-profile
    repair when shell_login_env fails. Never mutates outside that one Darwin
    C2 path. Mirrors oracle lines 304-602."""
    print("=== normalize-env — macOS/Linux mode (offers only, no mutation) ===\n", file=out)

    if os_name == "Darwin":
        print(_NE_POSIX_MANUAL_STEPS["Darwin"], file=out)
    elif os_name.startswith("Linux"):
        print(_NE_POSIX_MANUAL_STEPS["Linux"], file=out)
    else:
        print(f"Unrecognized OS ({os_name}). No automated mutations available.", file=out)
        print("Ensure Python 3.11+, uv, and git are installed manually.\n", file=out)

    runner = NeRunner(yes=yes)

    probe_output = _ne_run_probe_fn("_co_prereq_probe_all")
    print("Current prereq probe results:", file=out)
    _ne_print_probe_summary(probe_output, out)
    print(file=out)

    if os_name == "Darwin":
        try:
            _ne_darwin_bash_profile_repair(probe_output, home, dry_run, runner, out, in_stream)
        except _NeNonInteractiveAbort:
            return EXIT_NONINTERACTIVE_ABORT

    if runner.steps_succeeded or runner.steps_failed:
        print("\n=== macOS Repair Summary ===", file=out)
        if runner.steps_succeeded:
            print("Succeeded:", file=out)
            for s in runner.steps_succeeded:
                print(f"  {s}", file=out)
        if runner.steps_failed:
            print("Failed:", file=out)
            for s in runner.steps_failed:
                print(f"  {s}", file=out)
            if getattr(runner, "bash_profile_backup_path", None):
                print("Restore ~/.bash_profile with:", file=out)
                print(f"  cp {runner.bash_profile_backup_path} {home / '.bash_profile'}", file=out)
            print("Run the above brew/apt/dnf commands, then re-run the coordinator install.", file=out)
            return EXIT_PARTIAL_FAILURE

    print("Run the above brew/apt/dnf commands, then re-run the coordinator install.", file=out)
    return EXIT_OK


# ---------------------------------------------------------------------------
# Unit 4 — Windows fix-plan build; Step 1 (git core.longpaths); Step 2 (uv).
# ---------------------------------------------------------------------------

class _NeWindowsPlan:
    """Fix-plan flags, mirroring the oracle's _ne_has_*_fix booleans."""

    def __init__(self):
        self.longpaths = False
        self.uv = False
        self.python = False
        self.alias = False
        self.store_python_suspected = False

    @property
    def count(self) -> int:
        return sum([self.longpaths, self.uv, self.python, self.alias])


def _ne_wa_dir_has_store_python() -> bool:
    """Heuristic Store-alias detection: WindowsApps python.exe/python3.exe
    present under %LOCALAPPDATA%. Native-Windows path only (no cygpath
    conversion needed — a real python.exe reads LOCALAPPDATA directly,
    unlike the oracle's MINGW/MSYS bash which needed a POSIX-path bridge)."""
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if not local_appdata:
        return False
    wa_dir = Path(local_appdata) / "Microsoft" / "WindowsApps"
    return (wa_dir / "python.exe").is_file() or (wa_dir / "python3.exe").is_file()


def _ne_build_windows_plan(probe_output: str) -> _NeWindowsPlan:
    plan = _NeWindowsPlan()
    if _ne_probe_status_for(probe_output, "longpaths") != "pass":
        plan.longpaths = True
    if _ne_probe_status_for(probe_output, "uv") != "pass":
        plan.uv = True
    if _ne_probe_status_for(probe_output, "python") != "pass":
        plan.python = True
        plan.store_python_suspected = _ne_wa_dir_has_store_python()
        # Oracle registers the alias-disable fix as the final step whenever
        # python fails — not independently gated on the Store heuristic.
        plan.alias = True
    return plan


def _ne_print_windows_plan(plan: _NeWindowsPlan, out) -> None:
    idx = 0
    print(f"Planned mutations ({plan.count} step(s)):\n", file=out)

    if plan.longpaths:
        idx += 1
        print(f"  {idx}. git config --global core.longpaths true", file=out)
        print("     (enables Windows long-path support; byte-stable no-op on re-run)", file=out)

    if plan.uv:
        idx += 1
        print(f"  {idx}. install uv package manager (py -m pip install uv)", file=out)
        print("     (advisory; coordinator degrades gracefully without uv; idempotent on re-run)", file=out)

    if plan.python:
        idx += 1
        print(f"  {idx}. install Python 3.12 via py launcher / winget", file=out)
        print("     (required; installs from python.org; re-runnable)", file=out)

    if plan.alias:
        idx += 1
        if plan.store_python_suspected:
            print(f"  {idx}. disable WindowsApps App Execution aliases for python/python3", file=out)
            print("     [NOTICE: it looks like you are using Windows Store python intentionally]", file=out)
            print("     [Confirm disable of App Execution aliases intentionally.]", file=out)
            print("     (converges toward target state; Windows feature updates may re-enable aliases)", file=out)
        else:
            print(f"  {idx}. disable WindowsApps App Execution aliases for python/python3", file=out)
            print("     (removes Store stub that breaks functional Python detection;", file=out)
            print("      converges toward target; Windows feature updates may re-enable aliases;", file=out)
            print("      skips silently if no aliases present)", file=out)

    print(file=out)


def _ne_write_backup_file(backup_path: Path, out) -> bool:
    """Snapshot current PATH and pymanager shim target BEFORE any mutation
    (rollback-first discipline). Hard-fails (returns False) on write failure
    — proceeding with no backup would violate the rollback safety property.

    Deliberate isolation boundary — do not convert the `py -c` probe to an
    in-process call. Mechanism: distinct interpreter — asks another python
    (the `py` launcher's resolved target) for its own `sys.executable`,
    which cannot be observed from this process. See
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md.
    """
    py_target = ""
    py_bin = shutil.which("py")
    if py_bin:
        try:
            proc = subprocess.run(
                [py_bin, "-c", "import sys; print(sys.executable)"],
                capture_output=True,
                text=True,
                timeout=15,
                stdin=subprocess.DEVNULL,
                **_CREATIONFLAGS,
            )
            py_target = proc.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            py_target = ""

    try:
        saved_at = datetime.now().ctime()
    except Exception:
        saved_at = "unknown"

    try:
        with open(backup_path, "w", encoding="utf-8") as fh:
            fh.write(f"SAVED_PATH={os.environ.get('PATH', '')}\n")
            fh.write(f"SAVED_PYMANAGER_TARGET={py_target}\n")
            fh.write(f"SAVED_AT={saved_at}\n")
    except OSError:
        print(f"ERROR: could not write backup file at {backup_path}", file=sys.stderr)
        print(f"  Ensure {backup_path.parent} is writable before running this script.", file=sys.stderr)
        print("  Aborting — no mutations applied.", file=sys.stderr)
        return False
    return True


def _ne_step1_longpaths(runner: NeRunner, out, in_stream=None) -> None:
    print("--- Step: git long-path support ---", file=out)
    try:
        proceed = runner.prompt_yn("Apply: git config --global core.longpaths true", out, in_stream)
    except _NeNonInteractiveAbort:
        raise
    if not proceed:
        print("  Skipped by user.", file=out)
        print(file=out)
        return

    git_bin = shutil.which("git")
    ok = False
    if git_bin:
        try:
            proc = subprocess.run(
                [git_bin, "config", "--global", "core.longpaths", "true"],
                capture_output=True,
                text=True,
                timeout=15,
                stdin=subprocess.DEVNULL,
                **_CREATIONFLAGS,
            )
            ok = proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False

    if ok:
        print("  Applied.", file=out)
        verify_out = _ne_run_probe_fn("_co_probe_longpaths")
        # Single-probe output has no "name" filter needed -- take the
        # (only) record's status field directly.
        recs = _ne_parse_probe_lines(verify_out)
        verify_status = recs[0].get("status", "") if recs else ""
        print(f"  Post-fix probe: longpaths = {verify_status}", file=out)
        if verify_status == "pass":
            runner.record_success("git core.longpaths=true")
        else:
            print("  WARNING: probe still non-pass after fix.", file=sys.stderr)
            runner.record_failure("git core.longpaths (probe did not flip to pass)")
    else:
        print("  ERROR: git config --global core.longpaths true failed.", file=sys.stderr)
        runner.record_failure("git core.longpaths (git config command failed)")
    print(file=out)


def _ne_step2_uv(runner: NeRunner, out, in_stream=None) -> None:
    """Install uv via `py -m pip install uv` against a resolved python/py/python3.

    Deliberate isolation boundary — do not convert the pip-install subprocess
    to an in-process import. Mechanism: distinct interpreter — pip/uv must
    run against the target interpreter (`py`/`python`/`python3`), not this
    process's own `sys.executable`. See
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md.
    """
    print("--- Step: install uv package manager ---", file=out)
    try:
        proceed = runner.prompt_yn("Apply: install uv (py -m pip install uv)", out, in_stream)
    except _NeNonInteractiveAbort:
        raise
    if not proceed:
        print("  Skipped by user.", file=out)
        print(file=out)
        return

    pip_python = None
    for candidate in ("py", "python", "python3"):
        if shutil.which(candidate):
            pip_python = candidate
            break

    if not pip_python:
        print("  ERROR: no python/py found to install uv. Install Python first.", file=sys.stderr)
        runner.record_failure("uv install (no python found for pip)")
        print(file=out)
        return

    ok = False
    try:
        proc = subprocess.run(
            [pip_python, "-m", "pip", "install", "uv"],
            capture_output=True,
            text=True,
            timeout=_MUTATION_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
        ok = proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False

    if ok:
        print("  uv install attempted.", file=out)
        verify_out = _ne_run_probe_fn("_co_probe_uv")
        recs = _ne_parse_probe_lines(verify_out)
        verify_status = recs[0].get("status", "") if recs else ""
        print(f"  Post-fix probe: uv = {verify_status}", file=out)
        if verify_status == "pass":
            runner.record_success("uv installed")
        else:
            print("  WARNING: uv probe still non-pass after install (may need PATH refresh).", file=sys.stderr)
            runner.record_failure("uv install (probe did not flip to pass; may need new shell)")
    else:
        print("  ERROR: pip install uv failed.", file=sys.stderr)
        runner.record_failure("uv install (pip command failed)")
    print(file=out)


# ---------------------------------------------------------------------------
# Unit 5 — Step 3 (Python 3.12 via winget); Step 4 (App Execution alias
# disable via embedded PowerShell); final summary; main().
# ---------------------------------------------------------------------------

_NE_PS_ALIAS_DISABLE_SCRIPT = r"""
$waDir = "$env:LOCALAPPDATA\Microsoft\WindowsApps"
$aliases = @("python.exe", "python3.exe")
$disabled = @()
$failed = @()
foreach ($alias in $aliases) {
  $aliasPath = Join-Path $waDir $alias
  if (Test-Path $aliasPath) {
    try {
      Remove-Item -Path $aliasPath -Force -ErrorAction Stop
      $disabled += $alias
    } catch {
      $failed += "$alias ($_)"
    }
  } else {
    $disabled += "$alias (already absent)"
  }
}
if ($disabled.Count -gt 0) {
  Write-Output "Disabled: $($disabled -join ", ")"
}
if ($failed.Count -gt 0) {
  Write-Error "Failed: $($failed -join ", ")"
  exit 1
}
exit 0
"""


def _ne_step3_python(runner: NeRunner, out, in_stream=None) -> None:
    print("--- Step: install Python 3.12 ---", file=out)
    print(f"  Current python probe: {_ne_probe_detail_for(runner.probe_output, 'python')}", file=out)
    print("  This step installs Python 3.12 via winget (Python.Python.3.12).", file=out)
    print("  After install, a new shell session will be needed to pick up the new PATH entry.", file=out)
    try:
        proceed = runner.prompt_yn("Apply: winget install Python.Python.3.12", out, in_stream)
    except _NeNonInteractiveAbort:
        raise
    if not proceed:
        print("  Skipped by user.", file=out)
        print(file=out)
        return

    winget = shutil.which("winget")
    if not winget:
        print("  winget not found. Manual install required.", file=sys.stderr)
        print("  Download from: https://www.python.org/downloads/", file=sys.stderr)
        runner.record_failure("Python 3.12 install (winget not available)")
        print(file=out)
        return

    ok = False
    try:
        proc = subprocess.run(
            [
                winget, "install", "--id", "Python.Python.3.12", "--source", "winget",
                "--accept-package-agreements", "--accept-source-agreements",
            ],
            capture_output=True,
            text=True,
            timeout=_MUTATION_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
        ok = proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False

    if ok:
        print("  Python 3.12 install initiated via winget.", file=out)
        verify_out = _ne_run_probe_fn("_co_probe_python")
        recs = _ne_parse_probe_lines(verify_out)
        verify_status = recs[0].get("status", "") if recs else ""
        print(f"  Post-fix probe: python = {verify_status}", file=out)
        if verify_status == "pass":
            runner.record_success("Python 3.12 installed (pass)")
        else:
            print("  NOTE: python probe still non-pass — open a new terminal and re-run.", file=sys.stderr)
            runner.record_success("Python 3.12 install dispatched (new shell needed to confirm)")
    else:
        print("  ERROR: winget install Python.Python.3.12 failed.", file=sys.stderr)
        print("  Manual fallback: https://www.python.org/downloads/", file=sys.stderr)
        runner.record_failure("Python 3.12 install (winget failed)")
    print(file=out)


def _ne_step4_alias_disable(
    runner: NeRunner, plan: _NeWindowsPlan, out, in_stream=None
) -> None:
    print("--- Step: disable WindowsApps App Execution aliases for python/python3 ---", file=out)

    if plan.store_python_suspected:
        print("  [NOTICE] it looks like you are using Windows Store python intentionally.", file=out)
        alias_prompt = "Confirm disable of App Execution aliases for python/python3 (y/N)"
    else:
        alias_prompt = "Apply: disable WindowsApps python/python3 App Execution aliases"

    print("  Purpose: Store python stubs (App Execution aliases) pass command -v but exit 49;", file=out)
    print("  they block functional Python detection. Disabling redirects python to the real install.", file=out)
    print("  (Idempotency note: Windows feature updates may re-enable aliases; re-run if needed.)", file=out)

    try:
        proceed = runner.prompt_yn(alias_prompt, out, in_stream)
    except _NeNonInteractiveAbort:
        raise
    if not proceed:
        print("  Skipped by user.", file=out)
        print(file=out)
        return

    ps_exe = shutil.which("pwsh") or shutil.which("powershell")
    if not ps_exe:
        print("  ERROR: neither pwsh nor powershell found. Cannot disable aliases automatically.", file=sys.stderr)
        print("  Manual step: Settings > Apps > Advanced app settings > App execution aliases", file=sys.stderr)
        print('  Turn off "python" and "python3" entries.', file=sys.stderr)
        runner.record_failure("App Execution alias disable (no PowerShell available)")
        print(file=out)
        return

    ok = False
    try:
        proc = subprocess.run(
            [ps_exe, "-NonInteractive", "-NoProfile", "-Command", _NE_PS_ALIAS_DISABLE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=_MUTATION_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
        ok = proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False

    if ok:
        print("  App Execution aliases for python/python3 disabled.", file=out)
        verify_out = _ne_run_probe_fn("_co_probe_python")
        recs = _ne_parse_probe_lines(verify_out)
        verify_status = recs[0].get("status", "") if recs else ""
        print(f"  Post-fix probe: python = {verify_status}", file=out)
        runner.record_success("WindowsApps python/python3 App Execution aliases disabled")
    else:
        print("  ERROR: PowerShell command to disable aliases failed.", file=sys.stderr)
        print("  Manual step: Settings > Apps > Advanced app settings > App execution aliases", file=sys.stderr)
        print('  Turn off the "python" and "python3" entries.', file=sys.stderr)
        runner.record_failure("App Execution alias disable (PowerShell command failed)")
    print(file=out)


def _ne_windows_path(
    dry_run: bool, yes: bool, home: Path, out, in_stream=None
) -> int:
    """Windows environment normalization: build fix plan, print, dry-run
    early-exit, rollback-first backup, then consent-gated Steps 1-4 in
    blast-radius-last order. Mirrors oracle lines 604-1075."""
    probe_output = _ne_run_probe_fn("_co_prereq_probe_all")

    print("\n=== normalize-env — Windows environment normalization ===\n", file=out)

    print("Current prereq probe results:", file=out)
    _ne_print_probe_summary(probe_output, out)
    print(file=out)

    plan = _ne_build_windows_plan(probe_output)

    if plan.count == 0:
        print("All prereqs satisfied. Nothing to do.", file=out)
        print("(Note: idempotency — re-running this script is safe; probe-passing prereqs", file=out)
        print(" are never touched.)", file=out)
        return EXIT_OK

    _ne_print_windows_plan(plan, out)

    if dry_run:
        print("(--dry-run: no mutations applied.)", file=out)
        return EXIT_OK

    ts = _ne_timestamp()
    backup_path = _ne_backup_file_path(home, ts)
    if not _ne_write_backup_file(backup_path, out):
        return EXIT_HARD_ERROR

    print(f"Backup written: {backup_path}", file=out)
    print("To revert, run:", file=out)
    print(f"  normalize-env --restore {backup_path}\n", file=out)

    runner = NeRunner(yes=yes)
    runner.probe_output = probe_output

    try:
        if plan.longpaths:
            _ne_step1_longpaths(runner, out, in_stream)
        if plan.uv:
            _ne_step2_uv(runner, out, in_stream)
        if plan.python:
            _ne_step3_python(runner, out, in_stream)
        if plan.alias:
            _ne_step4_alias_disable(runner, plan, out, in_stream)
    except _NeNonInteractiveAbort:
        return EXIT_NONINTERACTIVE_ABORT

    print("=== Summary ===", file=out)

    if runner.steps_succeeded:
        print("Succeeded:", file=out)
        for s in runner.steps_succeeded:
            print(f"  {s}", file=out)

    if runner.steps_failed:
        print("Failed:", file=out)
        for s in runner.steps_failed:
            print(f"  {s}", file=out)
        print("\nTo retry failed steps:", file=out)
        print("  normalize-env", file=out)
        print("(Successfully applied steps are idempotent/already-done and will be skipped.)", file=out)
        print("\nIf PATH/shim state was partially modified, restore with:", file=out)
        print(f"  normalize-env --restore {backup_path}", file=out)
        return EXIT_PARTIAL_FAILURE

    if not runner.steps_succeeded and not runner.steps_failed:
        print("No mutations applied (all steps skipped).", file=out)
        if backup_path.is_file():
            try:
                backup_path.unlink()
                print("(Backup file removed — no state changed.)", file=out)
            except OSError:
                print(f"skip: _ne_windows_path: backup_path.unlink() failed: {sys.exc_info()[1]}", file=sys.stderr)
                pass
        return EXIT_OK

    print("All planned mutations applied successfully.", file=out)
    print(f"(Backup at {backup_path} can be removed once you verify the environment.)", file=out)
    print("\nOpen a new terminal to pick up PATH changes, then verify:", file=out)
    print("  python --version", file=out)
    print("  uv --version", file=out)
    return EXIT_OK


def main(argv: list, *, out=None, in_stream=None, home: Optional[Path] = None) -> int:
    out = out if out is not None else sys.stdout
    in_stream = in_stream if in_stream is not None else sys.stdin
    home = home if home is not None else Path(os.path.expanduser("~"))

    try:
        args = _ne_parse_args(argv)
    except _NeUsageError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE_ERROR

    if args["restore_file"] is not None:
        return _ne_handle_restore(args["restore_file"], home, out)

    is_windows = _ne_is_windows()
    os_name = "Windows" if is_windows else _ne_uname_s()

    try:
        if is_windows:
            return _ne_windows_path(args["dry_run"], args["yes"], home, out, in_stream)
        return _ne_posix_path(os_name, args["dry_run"], args["yes"], home, out, in_stream)
    except _NeNonInteractiveAbort:
        return EXIT_NONINTERACTIVE_ABORT


def _ne_uname_s() -> str:
    uname = shutil.which("uname")
    if not uname:
        return "unknown"
    try:
        return subprocess.run(
            [uname, "-s"],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        ).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        print(f"skip: _ne_uname_s: return subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return "unknown"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
