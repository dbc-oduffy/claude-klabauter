"""
coordinator_core.ops.gen_claude_doe_launcher — Port of: gen-claude-doe-launcher.sh
(example-doctrine-repo b5a4192c, 2026-07-20).

Purpose: on Windows, the Git-for-Windows shell bin directories and %USERPROFILE%\\bin
(jq, used by gen-settings-hooks on every launch) are NOT on the Windows PATH, so the
canonical POSIX `claude-doe` wrapper (installed by coordinator/scripts/install-maximalist.py Step 3.5b) is
not bare-invocable from PowerShell/cmd/Windows Terminal. This module renders a
first-class Windows launcher pair (claude-doe.cmd, claude-doe.ps1) from the coordinator
templates into `${CLAUDE_HOME:-$HOME}/.local/bin/`. The templates themselves discover
the Git-for-Windows install root at runtime (no hardcoded path); this module does not
duplicate that discovery logic — it only copies the template bytes to the destination.

Spec backlink: tasks/2026-07-14-install-dogfood-friction.md section F5 — the hand-made
stopgap this replaces (a claude-doe.cmd written during the 2026-07-14 dogfood session)
hardcoded a fixed Git-for-Windows install path, which breaks on a non-default Git
install location. This generator's rendered templates discover the Git-for-Windows root
dynamically instead (see the templates themselves for the discovery order).

Windows-only: a clean no-op (exit 0) on macOS/Linux, detected the same way the bash
oracle did (MSYSTEM / OS=Windows_NT / uname MINGW*-MSYS*-CYGWIN* — see is_windows()
below). Non-Windows installs are unaffected; coordinator/scripts/install-maximalist.py
calls this step unconditionally and it skips itself.

Idempotency: safe to re-run — atomic writes (tmp-file + rename); re-running with
unchanged templates is a content-identical rewrite (cheap, no drift).

Dry-run safety: --check-only renders each template to a discarded temp path, reports
whether the live destination is already up to date (byte-identical) or would change,
and never touches the live launcher files.

Post-render smoke: after a real (non --check-only) render succeeds, each rendered
launcher is executed once with a single benign `--version` argument (see
smoke_launcher()) to catch defects that only surface at execution time (e.g. a
cmd.exe paren-in-block parse failure, or an `exit /b 1` silently promoted to an
unconditional `exit 1`) — a clean render is not proof the launcher runs. Any smoke
that runs and returns non-zero is a hard failure (main() returns 1); a silent,
output-free non-zero exit is treated the same as a loud one, deliberately, since
that is exactly the defect class this exists to catch. The smoke is skipped
(exit 0, one `[smoke] skipped: <reason>` line on stderr) when: --no-smoke was
passed; the host is not Windows (os.name != "nt"); or the `claude` CLI the
launchers wrap is not on PATH. --check-only never smokes (it never writes a live
file to execute). Default is smoke ON (opt out with --no-smoke).

Spec backlink: X:/example-doctrine-repo/tasks/2026-07-20-install-dogfood-friction.md section F17

Negative-spec:
    - Does NOT implement the bash >= 4 version guard from the oracle (DR-148
      defense-in-depth) — meaningless for a pure-Python module. Omitted intentionally.
    - Does NOT reimplement Git-for-Windows root discovery — that logic lives entirely
      in the rendered templates (claude-doe-launcher.{cmd,ps1}.tmpl), copied verbatim.
    - Faithfully reproduces an oracle quirk: `--template-dir` with a missing value
      argument is a hard failure (exit 1) with a usage-style stderr message, matching
      the oracle's `set -u` unbound-variable crash's observable effect (no launcher
      written, nonzero exit) without reproducing bash's raw unbound-variable traceback
      text verbatim (the outward contract — exit code + "no write occurred" — is what
      callers depend on, not that literal wording).
"""

from __future__ import annotations

import filecmp
import os
import shutil
import subprocess
import sys
import tempfile
from typing import List, Optional, Tuple

from coordinator_core.session.declared_writes import declare_write

_PROG = "gen-claude-doe-launcher.sh"  # literal program-name prefix, matches the example-doctrine-repo filename

_USAGE = """\
Usage: gen-claude-doe-launcher.sh [OPTIONS]

Renders the Windows-native claude-doe launcher(s) (claude-doe.cmd,
claude-doe.ps1) from the coordinator templates into
${CLAUDE_HOME:-$HOME}/.local/bin/. Windows-only: cleanly skips (exit 0) on
macOS/Linux.

Options:
  --check-only        Validate without mutating any live file (renders to a
                      temp path, discards it, exits 0 on success)
  --template-dir <path>  Override the template source directory (default:
                      auto-resolved relative to this script; useful for tests)
  --smoke             Execute each rendered launcher once with --version after
                      a real render, to confirm it actually runs (default: ON)
  --no-smoke          Skip the post-render smoke check
  -h, --help          Show this help and exit

Environment:
  CLAUDE_HOME            Claude home BASE directory, i.e. the parent of .claude/
                         (default: $HOME; launchers land at $CLAUDE_HOME/.local/bin/)
"""


def is_windows() -> bool:
    """Windows-signal detection, mirrored from the bash oracle's is_windows().

    MSYSTEM is set by Git-Bash/MSYS2 shells (MINGW64/MINGW32/MSYS). OS=Windows_NT
    is set natively by cmd.exe/PowerShell-spawned environments. `uname -s` (when
    available) reports MINGW*/MSYS*/CYGWIN* on Windows POSIX layers. Any one signal
    is sufficient.
    """
    if os.environ.get("MSYSTEM"):
        return True
    if os.environ.get("OS") == "Windows_NT":
        return True
    uname_path = shutil.which("uname")
    if uname_path is not None:
        import subprocess

        try:
            result = subprocess.run(
                [uname_path, "-s"], capture_output=True, text=True, check=False
            )
        except OSError:
            result = None
        if result is not None:
            sysname = result.stdout.strip()
            if sysname.startswith(("MINGW", "MSYS", "CYGWIN")):
                return True
    return False


def smoke_launcher(dest: str) -> Tuple[int, str]:
    """Execute the rendered launcher once and return (returncode, combined_output).

    Runs `dest` with a single benign argument list ["--version"], capturing
    stdout+stderr combined. A `.ps1` destination is invoked via the `powershell`
    interpreter (`-NoProfile -ExecutionPolicy Bypass -File <dest> --version`);
    any other destination (i.e. `.cmd`) is invoked directly. Kept as a thin,
    separately-testable seam so callers/tests can monkeypatch it without ever
    really executing a launcher.

    Deliberate isolation boundary — do not convert to an in-process call.
    Mechanism: crash containment — smoke-executes a just-generated launcher,
    which may be malformed. See
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md.
    """
    if dest.lower().endswith(".ps1"):
        powershell = shutil.which("powershell")
        if powershell is None:
            return (127, "powershell not found on PATH")
        argv = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", dest, "--version"]
    else:
        argv = [dest, "--version"]

    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(f"skip: smoke_launcher: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return (124, f"smoke of {dest} timed out after 60s")
    except OSError as err:
        print(f"skip: smoke_launcher: result = subprocess.run( failed: {err}", file=sys.stderr)
        return (126, str(err))

    combined = (result.stdout or "") + (result.stderr or "")
    return (result.returncode, combined)


def main(argv: List[str]) -> int:
    """CLI entry: arg parse, Windows gate, render (or --check-only dry-run)."""
    check_only = False
    template_dir_override: Optional[str] = None
    smoke = True

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--check-only":
            check_only = True
            i += 1
        elif arg == "--smoke":
            smoke = True
            i += 1
        elif arg == "--no-smoke":
            smoke = False
            i += 1
        elif arg == "--template-dir":
            if i + 1 >= len(argv):
                print(
                    f"{_PROG}: --template-dir requires a value. Run with --help for usage.",
                    file=sys.stderr,
                )
                return 1
            template_dir_override = argv[i + 1]
            i += 2
        elif arg in ("-h", "--help"):
            print(_USAGE, file=sys.stderr)
            return 0
        else:
            print(
                f"{_PROG}: Unknown argument: {arg}. Run with --help for usage.",
                file=sys.stderr,
            )
            return 1

    if not is_windows():
        print(
            f"{_PROG}: not a Windows environment (no MSYSTEM/OS=Windows_NT/uname "
            "MINGW-MSYS-CYGWIN signal) -- skipping (no-op).",
            file=sys.stderr,
        )
        return 0

    claude_home_base = os.environ.get("CLAUDE_HOME") or os.path.expanduser("~")
    local_bin_dir = os.path.join(claude_home_base, ".local", "bin")
    cmd_dest = os.path.join(local_bin_dir, "claude-doe.cmd")
    ps1_dest = os.path.join(local_bin_dir, "claude-doe.ps1")

    if template_dir_override:
        tmpl_dir = template_dir_override
    else:
        # This module has no co-located example-doctrine-repo-side script path to derive the oracle's
        # `${_script_dir}/../templates/bin` default from — the example-doctrine-repo trampoline resolves
        # that default itself (relative to its own on-disk location) and always passes
        # `--template-dir` explicitly, so this branch is reached only when a caller
        # invokes main() directly without one. Fail loud rather than guess a cwd-relative
        # path that would silently pick up the wrong templates in the wrong directory.
        print(
            f"{_PROG}: --template-dir not supplied and no default resolvable "
            "(the example-doctrine-repo trampoline should always pass one explicitly).",
            file=sys.stderr,
        )
        return 1

    if not os.path.isdir(tmpl_dir):
        print(f"{_PROG}: ERROR: Template not found: {tmpl_dir}", file=sys.stderr)
        return 1
    tmpl_dir = os.path.realpath(tmpl_dir)

    cmd_tmpl = os.path.join(tmpl_dir, "claude-doe-launcher.cmd.tmpl")
    ps1_tmpl = os.path.join(tmpl_dir, "claude-doe-launcher.ps1.tmpl")

    if not os.path.isfile(cmd_tmpl):
        print(f"{_PROG}: ERROR: Template not found: {cmd_tmpl}", file=sys.stderr)
        return 1
    if not os.path.isfile(ps1_tmpl):
        print(f"{_PROG}: ERROR: Template not found: {ps1_tmpl}", file=sys.stderr)
        return 1

    if check_only:
        for kind, tmpl, dest in (("cmd", cmd_tmpl, cmd_dest), ("ps1", ps1_tmpl, ps1_dest)):
            with open(tmpl, "rb") as fh:
                lines = fh.read().count(b"\n")
            # Review: code-reviewer (2026-07-17 Finding 1) — hardcoded POSIX "/tmp"
            # fallback crashed --check-only on real Windows (no TMPDIR there).
            # tempfile.gettempdir() resolves TMPDIR/TEMP/TMP per-platform.
            tmp_fd, tmp_out = tempfile.mkstemp(
                prefix=f"claude-doe-launcher-check-{kind}.",
                dir=tempfile.gettempdir(),
            )
            os.close(tmp_fd)
            try:
                shutil.copyfile(tmpl, tmp_out)
                if os.path.isfile(dest) and filecmp.cmp(tmp_out, dest, shallow=False):
                    print(
                        f"[check-only] {kind}: up to date ({tmpl}, {lines} lines) -> "
                        f"{dest} (no-op)",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"[check-only] {kind}: template valid ({tmpl}, {lines} lines) -> "
                        f"would write {dest}",
                        file=sys.stderr,
                    )
            finally:
                try:
                    os.remove(tmp_out)
                except OSError:
                    print(f"skip: main: os.remove(tmp_out) failed: {sys.exc_info()[1]}", file=sys.stderr)
                    pass
        return 0

    os.makedirs(local_bin_dir, exist_ok=True)

    for tmpl, dest in ((cmd_tmpl, cmd_dest), (ps1_tmpl, ps1_dest)):
        tmp_dest = f"{dest}.tmp.{os.getpid()}"
        try:
            shutil.copyfile(tmpl, tmp_dest)
            os.replace(tmp_dest, dest)
            # DR-276: declared AFTER the write lands, never before — the
            # contract is a report of what was ACTUALLY written, not of an
            # intended surface.
            declare_write(dest)
        except OSError:
            try:
                os.remove(tmp_dest)
            except OSError:
                print(f"skip: main: os.remove(tmp_dest) failed: {sys.exc_info()[1]}", file=sys.stderr)
                pass
            raise
        print(f"Wrote launcher: {dest}", file=sys.stderr)

    if not smoke:
        print("[smoke] skipped: --no-smoke was passed", file=sys.stderr)
    elif os.name != "nt":
        print(
            f"[smoke] skipped: not running on Windows (os.name={os.name!r})",
            file=sys.stderr,
        )
    elif shutil.which("claude") is None:
        print(
            "[smoke] skipped: the `claude` CLI the launchers wrap is not on PATH "
            "(the install does not itself provide it -- prerequisite gap, not a "
            "launcher defect)",
            file=sys.stderr,
        )
    else:
        for dest in (cmd_dest, ps1_dest):
            if dest == ps1_dest and shutil.which("powershell") is None:
                print(
                    f"[smoke] skipped: {dest} -- no `powershell` found on PATH",
                    file=sys.stderr,
                )
                continue
            rc, output = smoke_launcher(dest)
            if rc != 0:
                print(
                    f"[smoke] FAILED: {dest} (exit {rc})",
                    file=sys.stderr,
                )
                if output:
                    print(output, file=sys.stderr)
                print(
                    "  Remediation: edit the template, not the rendered copy -- "
                    "coordinator/templates/bin/claude-doe-launcher."
                    f"{'ps1' if dest == ps1_dest else 'cmd'}.tmpl (in the example-doctrine-repo clone).",
                    file=sys.stderr,
                )
                return 1
            print(f"[smoke] ok: {dest} (exit 0)", file=sys.stderr)

    path_env = os.environ.get("PATH", "")
    if f":{local_bin_dir}:" not in f":{path_env}:":
        print(
            f"  NOTE: {local_bin_dir} is not yet on this shell PATH -- the persistent Windows",
            file=sys.stderr,
        )
        print(
            "  PATH wiring done by install-substrate.sh covers new terminals; open a new",
            file=sys.stderr,
        )
        print(
            f"  terminal (or add {local_bin_dir} to PATH) to invoke claude-doe bare.",
            file=sys.stderr,
        )

    print(
        f"Done. claude-doe.cmd (cmd/PowerShell) and claude-doe.ps1 (PowerShell) are ready "
        f"at {local_bin_dir}.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
