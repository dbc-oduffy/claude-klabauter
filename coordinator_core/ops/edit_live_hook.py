"""
coordinator_core.ops.edit_live_hook — stage/validate/atomic-swap helper for
editing a LIVE PreToolUse Bash-matcher hook without ever exposing a
syntactically-broken intermediate to the Claude Code harness's per-tool-call
`exec`.

Purpose: the harness execs each hook fresh from disk on every matching tool
call — there is no cached/compiled hook process. A multi-edit sequence
directly against a hook file that is currently registered under a
Bash-inclusive PreToolUse matcher leaves every intermediate edit state
briefly the *live enforcement code* for every concurrent agent's Bash tool
calls, not just the editor's own session. If any intermediate is
syntactically broken, the very next Bash call from ANY concurrent session
fails at the PreToolUse gate. This module implements the safe pattern: copy
the live hook to a scratch path, let the operator or agent edit the scratch
copy freely (none of those edits touch the live path), then validate the
FINAL scratch state with `bash -n` and land it via a single atomic
same-filesystem replace — there is no window where the live path is a
partially-written file.

See: docs/wiki/concurrent-em-hazards.md (example-doctrine-repo repo) § H33 for the
incident this helper was built to prevent (2026-07-09, block-illegal-filename.sh
heredoc-fix took down 4 concurrent agents' Bash tool fleet-wide).

Port of: edit-live-hook.sh (example-doctrine-repo b5a4192c, 2026-07-20, 229 lines)
Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md

Behavior-preservation notes (read alongside the bash source):
  - `stage` copies the live hook to a same-directory scratch file
    (`.{basename}.edit-live-hook.{pid}.scratch`) preserving mode+timestamps
    (bash oracle used `cp -p`), and WARNS (never blocks) if the target is
    registered under a Bash-inclusive PreToolUse matcher in hooks.json.
  - `commit` validates the scratch file with `sh -n` and REFUSES to swap on
    a syntax error, leaving both files in place — the live hook is never
    touched on a failed validation. On success it preserves the live file's
    permission bits across the swap (`os.chmod` before `os.replace`, mirroring
    the bash oracle's `chmod "$live_mode" "$scratch_path"` before `mv`) and
    performs the swap as a single atomic same-filesystem replace.
  - The bash-4 version guard in the original .sh is bash-interpreter-only and
    has no analog here (this module runs under Python, not bash) — omitted,
    not silently dropped: the guard existed to protect bash *syntax parsing*
    of the script itself, which is moot for a Python module.
  - Narrowed 2026-07-21 (plan `2026-07-21-claude-klabauter-pure-python-shop-retire-all-bash.md`,
    chunk C5b, PM ruling): the syntax-check gate moved from `bash -n` to
    `sh -n`. This module validates a git-hook artifact before an atomic live
    swap — any machine that runs git hooks at all already has the `sh` git
    itself execs hooks through, so `sh -n` adds ZERO new dependency beyond
    what git already imposes (git-hook carve-out, sanctioned residual (b), NOT
    a general bash-toolchain dependency). If `sh` is not found on PATH,
    `commit` REFUSES the swap (same fail-safe posture as a syntax error)
    rather than silently skipping validation — swapping in an unvalidated
    file defeats the entire point of this helper. This is a strictly SMALLER
    surface than the prior `bash -n` gate, not a widening: it validates only
    the specific hook file named on the command line, never any other shell
    artifact (see the plan's C16 anti-loophole clause).

Negative-spec (faithful oracle-bug reproduction):
  - `is_live_bash_matcher_hook` only checks PreToolUse entries whose
    `matcher` string contains the substring "Bash" (e.g. also matches a
    matcher like "Bash|Read") — this is the bash oracle's exact behavior
    (`[[ "Bash" != *"$matcher"* ]]`-shaped substring test), not a stricter
    exact-match, and is preserved as-is.
  - hooks.json resolution order is: (1) a script-directory-relative
    `../hooks/hooks.json` next to the invoking example-doctrine-repo-side entrypoint (passed
    via the `EDIT_LIVE_HOOK_SCRIPT_DIR` env var set by the trampoline, since
    this module has no fixed on-disk sibling to hooks.json the way the
    original bash script did), then (2) `CLAUDE_PLUGIN_ROOT`. If neither
    resolves, detection returns False (WARN-only feature; never blocks).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core.session.declared_writes import declare_write
from coordinator_core.win_portability import no_console_creationflags

PROG = "edit-live-hook.sh"  # literal program-name prefix — matches bash oracle

# Exit codes (parity-critical):
#   0 — success
#   1 — usage/argument error
#   2 — `sh -n` validation failure on commit (or `sh` unavailable) — the
#       swap did NOT happen; live hook untouched.
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_VALIDATION_FAILED = 2

_SH_N_TIMEOUT_SECS = 15


def _usage(stream=None) -> None:
    # NOTE: stream defaults to None (resolved to sys.stderr at call time),
    # NOT `stream=sys.stderr` -- a mutable/object default arg is bound once
    # at function-definition (module-import) time, which would silently
    # capture the *original* sys.stderr and bypass any later stream
    # substitution (e.g. pytest's capsys fixture monkeypatches sys.stderr
    # per-test, after this module has already been imported).
    if stream is None:
        stream = sys.stderr
    print(
        f"""Usage:
  {PROG} stage <hook-path>
  {PROG} commit <hook-path> <scratch-path>

See coordinator_core.ops.edit_live_hook module docstring for the full
stage/edit/validate/atomic-swap pattern and the H33 hazard it prevents
(docs/wiki/concurrent-em-hazards.md § H33, example-doctrine-repo repo).""",
        file=stream,
    )


def is_live_bash_matcher_hook(hook_path: str) -> bool:
    """Detection helper (offer, not a gate) — see module docstring negative-spec.

    Returns True iff hooks.json contains a PreToolUse entry whose `matcher`
    string contains "Bash" and whose hook command block references
    ``hook_path``'s basename. Never raises; any resolution/parse failure
    returns False (WARN-only feature, must never block staging).
    """
    hook_base = os.path.basename(hook_path)

    hooks_json: Optional[Path] = None
    script_dir = os.environ.get("EDIT_LIVE_HOOK_SCRIPT_DIR")
    if script_dir:
        candidate = Path(script_dir) / ".." / "hooks" / "hooks.json"
        if candidate.is_file():
            hooks_json = candidate
    if hooks_json is None:
        plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
        if plugin_root:
            candidate = Path(plugin_root) / "hooks" / "hooks.json"
            if candidate.is_file():
                hooks_json = candidate
    if hooks_json is None:
        return False

    try:
        data = json.loads(hooks_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(f"skip: is_live_bash_matcher_hook: data = json.loads(hooks_json.read_text(encoding=\"utf-8\")) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False

    for event, entries in data.get("hooks", {}).items():
        if event != "PreToolUse":
            continue
        for entry in entries:
            matcher = entry.get("matcher", "")
            if "Bash" not in matcher:
                continue
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                if hook_base in cmd:
                    return True
    return False


def cmd_stage(argv: List[str]) -> int:
    if not argv:
        print(f"{PROG}: stage requires <hook-path>", file=sys.stderr)
        return EXIT_USAGE
    hook_path = argv[0]

    if not os.path.isfile(hook_path):
        print(f"{PROG}: stage: no such file: {hook_path}", file=sys.stderr)
        return EXIT_USAGE

    hook_dir = str(Path(hook_path).resolve().parent)
    hook_base = os.path.basename(hook_path)
    scratch_path = os.path.join(
        hook_dir, f".{hook_base}.edit-live-hook.{os.getpid()}.scratch"
    )

    shutil.copy2(hook_path, scratch_path)

    if is_live_bash_matcher_hook(hook_path):
        print(
            f"{PROG}: NOTE: {hook_base} is registered under a Bash-inclusive PreToolUse matcher.",
            file=sys.stderr,
        )
        print(
            "  Editing the scratch copy below is safe -- it is NOT the live enforcement path.",
            file=sys.stderr,
        )
        print(
            "  See docs/wiki/concurrent-em-hazards.md § H33 before editing the live path directly.",
            file=sys.stderr,
        )

    print(f"Staged scratch copy: {scratch_path}")
    print("Edit the scratch copy freely, then run:", file=sys.stderr)
    print(f"  {PROG} commit {hook_path} {scratch_path}", file=sys.stderr)
    return EXIT_OK


def cmd_commit(argv: List[str]) -> int:
    if len(argv) < 2:
        print(f"{PROG}: commit requires <hook-path> <scratch-path>", file=sys.stderr)
        return EXIT_USAGE
    hook_path, scratch_path = argv[0], argv[1]

    if not os.path.isfile(scratch_path):
        print(f"{PROG}: commit: no such scratch file: {scratch_path}", file=sys.stderr)
        return EXIT_USAGE

    sh_exe = shutil.which("sh")
    if sh_exe is None:
        print(
            f"{PROG}: commit: REFUSED -- no `sh` found on PATH to run the `sh -n` "
            "syntax check.",
            file=sys.stderr,
        )
        print(
            f"  Live hook {hook_path} was NOT modified. A machine that runs git hooks "
            "at all already has `sh` (git execs hooks through it); re-run commit "
            "there.",
            file=sys.stderr,
        )
        return EXIT_VALIDATION_FAILED

    # DO NOT swap on a syntax-broken scratch file -- fail loud and leave the
    # live hook untouched. This is the entire point of the helper: a bad
    # intermediate must never become the live enforcement code.
    proc = subprocess.run(
        [sh_exe, "-n", scratch_path],
        capture_output=True,
        text=True,
        timeout=_SH_N_TIMEOUT_SECS,
        stdin=subprocess.DEVNULL,
        # Review: code-reviewer -- A4 Windows console-flash suppression, matching
        # the pattern already used in generate_exec_summary.py (same slice).
        **no_console_creationflags(),
    )
    if proc.returncode != 0:
        print(
            f"{PROG}: commit: REFUSED -- scratch copy fails `sh -n` syntax check:",
            file=sys.stderr,
        )
        print(proc.stderr, file=sys.stderr, end="" if proc.stderr.endswith("\n") else "\n")
        print(
            f"  Live hook {hook_path} was NOT modified. Fix {scratch_path} and re-run commit.",
            file=sys.stderr,
        )
        return EXIT_VALIDATION_FAILED

    hook_dir = str(Path(hook_path).resolve().parent)
    scratch_dir = str(Path(scratch_path).resolve().parent)
    if hook_dir != scratch_dir:
        print(
            f"{PROG}: commit: WARNING -- scratch ({scratch_dir}) and live hook ({hook_dir}) "
            "are on different",
            file=sys.stderr,
        )
        print(
            "  directories; the swap below may not be an atomic rename on this filesystem.",
            file=sys.stderr,
        )

    # Preserve the live file's mode (e.g. exec bit) across the swap.
    live_mode = os.stat(hook_path).st_mode
    os.chmod(scratch_path, live_mode)

    os.replace(scratch_path, hook_path)
    # DR-276: declared AFTER the swap lands, never before — the contract is a
    # report of what was ACTUALLY written, not of an intended surface. The
    # scratch copy is a caller-editable staging file, not a temp artifact of
    # this replace, so only the final destination (hook_path) is declared.
    declare_write(hook_path)
    print(f"{PROG}: commit: OK -- {hook_path} swapped in atomically (syntax-valid).")
    return EXIT_OK


def main(argv: List[str]) -> int:
    if not argv:
        _usage()
        return EXIT_USAGE

    subcommand, rest = argv[0], argv[1:]

    if subcommand == "stage":
        return cmd_stage(rest)
    if subcommand == "commit":
        return cmd_commit(rest)
    if subcommand in ("-h", "--help", "help"):
        _usage(stream=sys.stdout)
        return EXIT_OK

    print(f"{PROG}: unknown subcommand: {subcommand}", file=sys.stderr)
    _usage()
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
