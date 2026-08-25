"""
coordinator_core.ops.bootstrap_orchestrate — Discovery-to-per-repo orchestration
for /coordinator:repo-setup --batch (fleet scaffolding).

Port of: bootstrap-orchestrate.sh (DoE 0fc2b3ba, 2026-07-22).

Purpose: reads `~/.claude/working-repos.yaml`, resolves the repo list,
presents the EXPRESS/CUSTOM two-choice surface, then delegates each selected
repo to `coordinator_core.ops.bootstrap_repo.main` (the already-ported Wave-1
primitive — called by DIRECT in-process import, not subprocess: both this
module and bootstrap_repo now live in the same makima process, so a subprocess
hop buys nothing). After a successful bootstrap, stamps coordinator currency
natively (C19 — see "Currency stamp — native port" below; no longer shells
out to the DoE-resident `lib/coordinator-currency.sh`).

Spec backlink: docs/plans/2026-05-29-it-just-works-agentic-install-currency.md § Chunk 4
             + docs/plans/2026-07-16-bash-clean-slate-residual-migration.md (BIG_PORT wave)

HOOK CONTRACT — target-repo commit hooks are RESPECTED:
  bootstrap_repo.main (Wave-1) does NOT pass --no-verify; it runs the target
  repo's hooks exactly as the user would. If a hook exits non-zero, this
  orchestrator surfaces the failure in the per-repo status row. This
  orchestrator ALSO does not pass --no-verify on the currency-stamp commit —
  both commits (bootstrap scaffold + currency stamp) honour the target repo's
  hooks.

Currency stamp — native port (C19): `lib/coordinator-currency.sh`'s public
`coordinator_currency_write <repo_root> <plugin_root>` function is reimplemented
natively as `_coordinator_currency_write` below — same observable contract
(idempotent stamp write: read `<plugin_root>/coordinator-schema-version`,
no-op if `<repo_root>/docs/coordinator-currency.yaml` already carries that
version, else atomic temp-file + rename write of the two-line YAML stamp),
zero subprocess, zero bash dependency. This retires the
`bash -c 'source <currency_sh> && coordinator_currency_write <repo>
<plugin_root>'` bridge the previous port used (a bash process could not be
avoided there because a Python process cannot source a bash function — the
fix is reimplementing the function body itself, not the sourcing mechanism).
The read/probe path is independently reimplemented by
`coordinator_core.ops.probe_onboarding_currency`.

Language-migration simplification (NOT a scope-drop): the bash oracle checked
for `bootstrap-repo.sh`'s existence on disk before invoking it as a subprocess
(and `chmod +x`'d it first). Since this port calls
`coordinator_core.ops.bootstrap_repo.main` via direct in-process import rather
than subprocess, the file-existence/executable-bit precondition is structurally
replaced by an import-success precondition (see `_import_bootstrap_repo_main`)
— the requirement ("the sibling primitive must be usable before we start")
is preserved, just satisfied a different way.

Exit codes (must match `main()`'s actual returns byte-for-byte — do not drift):
  0  All selected repos bootstrapped successfully (or dry-run completed).
  1  Usage error, missing prerequisite, or working-repos.yaml unreadable.
  2  working-repos.yaml absent — operator must run /setup first.
  3  One or more repos failed to bootstrap; partial success. Per-repo status
     table is printed; summary distinguishes succeeded/failed/skipped counts.
     Also returned when a malformed repo-path entry was rejected during
     parsing even though every selected repo otherwise succeeded (partial
     success, not a silent 0 that hides the skipped entry).

Idempotency:
  Re-running with the same repo list is a no-op (bootstrap_repo.main + scaffold
  are idempotent; the currency-stamp write is idempotent). EXPRESS re-run = no
  changes.

Out-of-scope (this module does NOT):
  - Modify ~/.claude/working-repos.yaml or any Wave-1 library file.
  - Discover repos (delegates discovery entirely to working-repos.yaml).
  - Author handoffs, spinoffs, or session artifacts.
  - Commit into ~/.claude or any coordinator-managed repo.
  - Run git operations against the coordinator meta-repo itself.

Destructive-action prohibition:
  This module MUST NOT run git reset, git rm, git push --force, or any
  destructive git operation. It MUST NOT delete or overwrite user files outside
  the bootstrap_repo.main scaffold contract. All mutations are reversible via
  `git revert HEAD` in the target repo.

Negative-spec (faithful oracle-bug repro — do NOT "fix" mid-port):
  - The YAML parser is a hand-rolled line-by-line state machine (no yq/PyYAML
    dependency), mirroring the bash oracle's own no-dependency constraint.
    It only understands the exact `  - path: <value>` shape under a top-level
    `repos:` block; anything else (nested lists, flow-style YAML, multi-line
    scalars) is silently NOT collected, exactly as the bash oracle behaved.
  - Inline-comment stripping only fires on a WHITESPACE-preceded '#' (space or
    tab); a bare '#' with no leading whitespace is kept as a literal path
    character (this was itself a fix landed in the bash oracle for a prior
    tab-comment regression — preserved here, not re-introduced as a bug).
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from coordinator_core.win_portability import no_console_creationflags


_CREATIONFLAGS = no_console_creationflags()

# Generator-provenance declaration (generator_provenance.py).
# _coordinator_currency_write stamps docs/coordinator-currency.yaml under
# `actual_path`, the target repo being bootstrapped by
# /coordinator:repo-setup --batch (a fleet repo, not makima itself) --
# caller-supplied target repo outside makima's own tree.
GENERATES = []

_GIT_TIMEOUT_SECS = 30
_SCHEMA_VERSION_RE = re.compile(r"^[1-9][0-9]*$")
_STAMP_LINE_RE = re.compile(r"^schema_version:\s*(.*)$", re.MULTILINE)

_HELP_TEXT = """
coordinator/lib/bootstrap-orchestrate.sh — Discovery-to-per-repo orchestration for
/coordinator:repo-setup --batch (fleet scaffolding; consolidated 2026-06-08 from
the former /bootstrap-repos). Script filename kept for git-mv history continuity;
the implementation logic and execution path are unchanged from its bootstrap-repos origin; only user-visible command names (in printf strings, summary headers, and error messages) were updated.

Purpose: reads ~/.claude/working-repos.yaml, resolves the repo list, presents
the EXPRESS/CUSTOM two-choice surface, then delegates each selected repo to the
existing Wave-1 primitive lib/bootstrap-repo.sh. After a successful bootstrap
stamps coordinator currency via lib/coordinator-currency.sh::coordinator_currency_write.

Spec backlink: docs/plans/2026-05-29-it-just-works-agentic-install-currency.md § Chunk 4

HOOK CONTRACT — target-repo commit hooks are RESPECTED:
  bootstrap-repo.sh (Wave-1) does NOT pass --no-verify; it runs the target repo's
  hooks exactly as the user would. If a hook exits non-zero, bootstrap-repo.sh exits
  non-zero and this orchestrator surfaces the failure in the per-repo status row.
  This orchestrator ALSO does not pass --no-verify on the currency-stamp commit —
  both commits (bootstrap scaffold + currency stamp) honour the target repo's hooks.

Usage (direct shell — command doc delegates to this script):
  bootstrap-orchestrate.sh [--non-interactive] [--check-only] [--help]

Flags:
  --non-interactive   No AskUserQuestion; selects EXPRESS path automatically.
                      Documented per-site fallback: choose EXPRESS (bootstrap all repos).
                      Under --check-only this flag is a no-op (check-only already suppresses all writes).
  --check-only        Dry-run: produce the per-repo action list, write nothing.
                      Implies --non-interactive (check-only never prompts).
                      bootstrap-repo.sh called with --dry-run; currency stamp skipped.
  --help | -h         Show this help and exit.

Exit codes:
  0  All selected repos bootstrapped successfully (or dry-run completed).
  1  Usage error, missing prerequisite, or working-repos.yaml unreadable.
  2  working-repos.yaml absent — operator must run /setup first.
  3  One or more repos failed to bootstrap; partial success. Per-repo status
     table is printed; summary distinguishes succeeded/failed/skipped counts.

Idempotency:
  Re-running with the same repo list is a no-op (bootstrap-repo.sh + scaffold
  are idempotent; currency stamp write is idempotent). EXPRESS re-run = no
  changes.

Out-of-scope (this script does NOT):
  - Modify ~/.claude/working-repos.yaml or any Wave-1 library file.
  - Discover repos (delegates discovery entirely to working-repos.yaml).
  - Author handoffs, spinoffs, or session artifacts.
  - Commit into ~/.claude or any coordinator-managed repo.
  - Run git operations against the coordinator meta-repo itself.

Destructive-action prohibition:
  This script MUST NOT run git reset, git rm, git push --force, or any
  destructive git operation. It MUST NOT delete or overwrite user files outside
  the bootstrap-repo.sh scaffold contract. All mutations are reversible via
  `git revert HEAD` in the target repo.
""".strip(
    "\n"
)


# ---------------------------------------------------------------------------
# DoE sibling-root resolution (copied ladder, per the convention already used
# by coordinator_core.ops.bootstrap_repo / learn_lessons_roots — a local copy,
# not a shared import, per those modules' own stated convention)
# ---------------------------------------------------------------------------


def _resolve_coordinator_root() -> str:
    """Resolve the DoE coordinator/ root.

    The caller sets `COORDINATOR_ROOT` in the environment before invoking
    this module's `main()` — this is a fast-path env-override read, not a
    re-derivation of the discovery ladder.
    """
    override = os.environ.get("COORDINATOR_ROOT") or os.environ.get("CLAUDE_PLUGIN_ROOT")
    if override:
        return override
    # Best-effort fallback if invoked without the trampoline's env seed (e.g.
    # directly from a test): unconditional flat-layout default, matching the
    # sibling ports' rung-4 fallback.
    claude_home = os.path.join(
        os.environ.get("CLAUDE_HOME")
        or os.environ.get("HOME")
        or os.environ.get("USERPROFILE")
        or os.path.expanduser("~"),
        ".claude",
    )
    return os.path.join(claude_home, "plugins", "coordinator-claude", "coordinator")


def _import_bootstrap_repo_main():
    """Import coordinator_core.ops.bootstrap_repo.main (direct in-process import).

    Replaces the bash oracle's subprocess-shape ("$BOOTSTRAP_REPO" --root ...)
    with a plain function call — both modules are makima-resident, so the
    subprocess hop the bash oracle needed (crossing the DoE/makima process
    boundary) no longer applies here. Raises ImportError on failure, mirroring
    the oracle's own "bootstrap-repo.sh not found" prerequisite-check exit 1.
    """
    from coordinator_core.ops.bootstrap_repo import main as _bootstrap_repo_main

    return _bootstrap_repo_main


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _print(*args, **kwargs) -> None:
    print(*args, **kwargs)


def _normalize_path(p: str) -> str:
    """Convert Windows-style X:\\foo -> POSIX /x/foo for existence checks.

    Lowercase ONLY the drive letter; preserve path case for POSIX filesystems.
    """
    out = p.replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", out)
    if m:
        drive_lc = m.group(1).lower()
        return f"/{drive_lc}/{m.group(2)}"
    return out


def _which_git() -> bool:
    try:
        proc = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _which_git: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    return proc.returncode == 0


def _git(args: List[str], root: str, timeout: int = _GIT_TIMEOUT_SECS) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git", "-C", root] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        **_CREATIONFLAGS,
    )


# ---------------------------------------------------------------------------
# working-repos.yaml parsing (unit1) — hand-rolled state machine, no
# yq/PyYAML dependency, mirroring the bash oracle's own no-dependency
# constraint (see module docstring Negative-spec).
# ---------------------------------------------------------------------------

_PATH_LINE_RE = re.compile(r"^[ \t]*-[ \t]*path:[ \t]*(.+)$")
_TOP_LEVEL_KEY_RE = re.compile(r"^[A-Za-z_]")
_LEADING_WS_RE = re.compile(r"^[ \t]")
_WS_HASH_RE = re.compile(r"[ \t]#")


def _strip_yaml_value(raw_path: str) -> str:
    """Strip a whitespace-preceded inline '#' comment, then trailing whitespace.

    A bare '#' with no leading whitespace is a literal path character, not a
    comment (negative-spec: do not truncate `/repos/a#b` to `/repos/a`).
    """
    m = _WS_HASH_RE.search(raw_path)
    if m:
        raw_path = raw_path[: m.start()]
    return raw_path.rstrip(" \t")


def _parse_repo_paths(yaml_path: str) -> Tuple[List[str], bool]:
    """Parse `  - path: <value>` entries from the `repos:` block only.

    Returns (repo_paths, any_path_rejected). A leading-'-' value is rejected
    (loudly, to stderr) rather than silently collected — it would otherwise be
    misread as a CLI flag by bootstrap_repo.main's `--root` consumer.
    """
    repo_paths: List[str] = []
    any_rejected = False
    in_repos_block = False
    in_out_of_tree = False

    with open(yaml_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")

            if line.startswith("repos:"):
                in_repos_block = True
                in_out_of_tree = False
                continue
            if line.startswith("out_of_tree:"):
                in_repos_block = False
                in_out_of_tree = True
                continue
            if _TOP_LEVEL_KEY_RE.match(line) and not _LEADING_WS_RE.match(line):
                in_repos_block = False
                in_out_of_tree = False

            if in_repos_block and not in_out_of_tree:
                m = _PATH_LINE_RE.match(line)
                if m:
                    raw_path = _strip_yaml_value(m.group(1))
                    if raw_path.startswith("-"):
                        _print(
                            f"bootstrap-orchestrate.sh: rejecting repo path '{raw_path}' "
                            "from working-repos.yaml — leading '-' is parsed as a flag, "
                            "not a path.",
                            file=sys.stderr,
                        )
                        any_rejected = True
                        continue
                    if raw_path:
                        repo_paths.append(raw_path)

    return repo_paths, any_rejected


# ---------------------------------------------------------------------------
# Currency stamp delegation (cross-boundary — see module docstring)
# ---------------------------------------------------------------------------


def _currency_read_schema_version(plugin_root: str) -> Optional[str]:
    """Read the schema-version stamp from `plugin_root/coordinator-schema-version`."""
    ver_file = os.path.join(plugin_root, "coordinator-schema-version")
    try:
        with open(ver_file, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        print(f"skip: _currency_read_schema_version: with open(ver_file, \"r\", encoding=\"utf-8\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    ver = re.sub(r"\s+", "", raw)
    if _SCHEMA_VERSION_RE.match(ver):
        return ver
    return None


def _currency_read_stamp(stamp_path: str) -> Optional[str]:
    """Read the current schema-version value out of an existing currency stamp file."""
    try:
        with open(stamp_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        print(f"skip: _currency_read_stamp: with open(stamp_path, \"r\", encoding=\"utf-8\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    match = _STAMP_LINE_RE.search(text)
    if not match:
        return None
    ver = re.sub(r"\s+", "", match.group(1))
    if _SCHEMA_VERSION_RE.match(ver):
        return ver
    return None


def _coordinator_currency_write(actual_path: str, plugin_root: str) -> bool:
    """Port of: coordinator-currency.sh::coordinator_currency_write (DoE 9cc1d315, 2026-07-21).

    Idempotent: no-ops (returns True, no file change) if the stamp already
    carries the current schema version. Otherwise writes
    `<repo_root>/docs/coordinator-currency.yaml` via temp-file + atomic
    rename, mirroring the bash oracle's `mktemp` + `mv` sequence. Prints
    diagnostics to stdout/stderr exactly as the bash function did (it never
    redirected its own output into a variable either). Returns False on any
    failure (unreadable schema-version file, directory-create failure,
    write failure) — same fail-loud contract as the bash oracle's `return 1`.
    """
    current_version = _currency_read_schema_version(plugin_root)
    if current_version is None:
        _print(
            "  currency: coordinator_currency_write: cannot read "
            f"COORDINATOR_SCHEMA_VERSION from '{plugin_root}/coordinator-schema-version'",
            file=sys.stderr,
        )
        return False

    stamp_path = os.path.join(actual_path, "docs", "coordinator-currency.yaml")
    stamp_dir = os.path.dirname(stamp_path)

    if os.path.isfile(stamp_path):
        existing_version = _currency_read_stamp(stamp_path)
        if existing_version == current_version:
            return True  # already current — no file change

    try:
        os.makedirs(stamp_dir, exist_ok=True)
    except OSError as exc:
        _print(
            f"  currency: coordinator_currency_write: failed to create directory '{stamp_dir}': {exc}",
            file=sys.stderr,
        )
        return False

    stamp_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    contents = (
        "# coordinator-currency.yaml — per-repo coordinator schema currency stamp.\n"
        "#\n"
        "# Written by: _coordinator_currency_write "
        "(coordinator_core.ops.bootstrap_orchestrate; native port of\n"
        "# lib/coordinator-currency.sh::coordinator_currency_write)\n"
        "# Spec backlink: docs/plans/2026-05-29-it-just-works-agentic-install-currency.md § Chunk 1\n"
        "#\n"
        "# MUTABLE — updated on refresh. Separate from coordinator-setup-state.yaml (set-once).\n"
        "# Read by: doctor probe P-13, Wave-2 Chunk 3 currency detector.\n"
        f"schema_version: {current_version}\n"
        f"stamped_at: {stamp_date}\n"
    )

    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=os.path.basename(stamp_path) + ".", dir=stamp_dir
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(contents)
            # Review: code-reviewer — mkstemp hardcodes 0600 regardless of
            # umask; os.replace preserves that mode across the rename, so
            # without this chmod the stamp ends up owner-only instead of the
            # 644 a bash `printf > file` redirect (the retired oracle's
            # behavior) would have produced.
            os.chmod(tmp_path, 0o644)
        except OSError:
            os.close(fd)
            raise
    except OSError as exc:
        _print(
            f"  currency: coordinator_currency_write: failed to write temp stamp file: {exc}",
            file=sys.stderr,
        )
        return False

    try:
        os.replace(tmp_path, stamp_path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        _print(
            f"  currency: coordinator_currency_write: failed to move temp stamp to '{stamp_path}': {exc}",
            file=sys.stderr,
        )
        return False

    return True


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: List[str]) -> int:
    # ---- arg parsing ---------------------------------------------------
    non_interactive = False
    check_only = False

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--help", "-h"):
            _print(_HELP_TEXT)
            return 0
        elif arg == "--non-interactive":
            non_interactive = True
        elif arg == "--check-only":
            check_only = True
            non_interactive = True  # check-only implies non-interactive
        elif arg.startswith("-"):
            _print(f"bootstrap-orchestrate.sh: unknown flag: {arg}", file=sys.stderr)
            return 1
        else:
            _print(f"bootstrap-orchestrate.sh: unexpected argument: {arg}", file=sys.stderr)
            return 1
        i += 1

    # ---- prerequisite checks --------------------------------------------
    coordinator_root = _resolve_coordinator_root()
    plugin_root = coordinator_root

    try:
        bootstrap_repo_main = _import_bootstrap_repo_main()
    except ImportError as exc:
        _print(
            f"bootstrap-orchestrate.sh: coordinator_core.ops.bootstrap_repo not importable: {exc}",
            file=sys.stderr,
        )
        return 1

    if not _which_git():
        _print("bootstrap-orchestrate.sh: git is not available on PATH", file=sys.stderr)
        return 1

    # ---- resolve working-repos.yaml -------------------------------------
    home = (
        os.environ.get("CLAUDE_HOME")
        or os.environ.get("HOME")
        or os.environ.get("USERPROFILE")
        or os.path.expanduser("~")
    )
    working_repos_yaml = os.path.join(home, ".claude", "working-repos.yaml")

    if not os.path.isfile(working_repos_yaml):
        _print("", file=sys.stderr)
        _print("bootstrap-orchestrate.sh: ~/.claude/working-repos.yaml not found.", file=sys.stderr)
        _print("", file=sys.stderr)
        _print(
            "  /repo-setup --batch requires the working-repos list produced by /setup Phase 2 Step 4.",
            file=sys.stderr,
        )
        _print(
            "  Run /setup first (or /coordinator:install) to discover and record your working repos.",
            file=sys.stderr,
        )
        _print("", file=sys.stderr)
        return 2

    # ---- parse repo paths -------------------------------------------------
    repo_paths, any_path_rejected = _parse_repo_paths(working_repos_yaml)

    if not repo_paths:
        _print(
            "bootstrap-orchestrate.sh: no repo paths found in ~/.claude/working-repos.yaml",
            file=sys.stderr,
        )
        _print("  The file exists but the repos: block is empty or malformed.", file=sys.stderr)
        return 1

    # ---- filter to repos that exist on disk --------------------------------
    existing_repos: List[str] = []
    missing_repos: List[str] = []

    for repo in repo_paths:
        posix = _normalize_path(repo)
        if os.path.isdir(posix) or os.path.isdir(repo):
            existing_repos.append(repo)
        else:
            missing_repos.append(repo)

    if not existing_repos:
        _print(
            "bootstrap-orchestrate.sh: none of the repos in working-repos.yaml exist on disk.",
            file=sys.stderr,
        )
        _print(
            "  Either the repo paths are wrong or this machine has no local checkouts.",
            file=sys.stderr,
        )
        _print(f"  Repos listed: {' '.join(repo_paths)}", file=sys.stderr)
        return 1

    if missing_repos:
        _print("")
        _print(
            f"Note: {len(missing_repos)} repo(s) in working-repos.yaml not found on disk "
            "(will be skipped):"
        )
        for m in missing_repos:
            _print(f"  - {m}")

    # ---- check-only: print action list and exit ----------------------------
    if check_only:
        _print("")
        _print("=== /repo-setup --batch --check-only: per-repo action list ===")
        _print("")
        _print(f"Repos that WOULD be bootstrapped ({len(existing_repos)}):")
        for repo in existing_repos:
            posix = _normalize_path(repo)
            actual_path = posix if os.path.isdir(posix) else repo
            _print("")
            _print(f"  Repo: {repo}")
            # Capture bootstrap_repo_main's merged stdout+stderr and prefix each line
            # with 4 spaces — matches the bash oracle's `2>&1 | sed 's/^/    /'` piping
            # of the sibling subprocess's output into the action list.
            _buf = io.StringIO()
            with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
                bootstrap_repo_main(["--root", actual_path, "--non-interactive", "--dry-run"])
            for _line in _buf.getvalue().splitlines():
                _print(f"    {_line}")
            _print("  [currency] would stamp coordinator currency (lib/coordinator-currency.sh)")
        if missing_repos:
            _print("")
            _print(f"Repos SKIPPED (not on disk) ({len(missing_repos)}):")
            for m in missing_repos:
                _print(f"  - {m}")
        _print("")
        _print("=== check-only complete — no changes made ===")
        if any_path_rejected:
            _print(
                "  Note: one or more malformed repo paths were rejected during parsing "
                "(see warnings above).",
                file=sys.stderr,
            )
            return 3
        return 0

    # ---- SELECT: EXPRESS or CUSTOM -----------------------------------------
    selected_repos: List[str] = []

    if non_interactive:
        _print("")
        _print(
            f"repo-setup: --non-interactive — selecting EXPRESS path "
            f"(bootstrap all {len(existing_repos)} repo(s))."
        )
        selected_repos = list(existing_repos)
    else:
        _print("")
        _print("=== /repo-setup --batch — Coordinator Bootstrap ===")
        _print("")
        _print(f"  Repos discovered in ~/.claude/working-repos.yaml ({len(existing_repos)} on disk):")
        for r in existing_repos:
            _print(f"    - {r}")
        _print("")
        _print("  How would you like to proceed?")
        _print("")
        _print("  [EXPRESS]  Bootstrap all repos above (recommended — safe to re-run, fully reversible)")
        _print("  [CUSTOM]   Choose repos one by one")
        _print("")
        choice = _read_line("  Choice [E/C, default E]: ") or "E"

        upper_choice = choice.upper()
        if upper_choice in ("E", "EXPRESS"):
            selected_repos = list(existing_repos)
            _print(f"  Express selected — bootstrapping all {len(existing_repos)} repo(s).")
        elif upper_choice in ("C", "CUSTOM"):
            _print("")
            _print("  Per-repo selection:")
            for repo in existing_repos:
                repo_choice = _read_line(f"  Bootstrap '{repo}'? [Y/n] ") or "Y"
                if repo_choice.upper() in ("Y", "YES"):
                    selected_repos.append(repo)
                    _print("    Added.")
                else:
                    _print("    Skipped.")
        else:
            _print(f"  Unrecognised choice '{choice}' — defaulting to EXPRESS.")
            selected_repos = list(existing_repos)

    if not selected_repos:
        _print("")
        _print("repo-setup: no repos selected. Nothing to do.")
        # Review: code-reviewer — F6, this early-return bypassed the
        # any_path_rejected check the success path (below) honors, silently
        # losing the exit-3 malformed-path-rejection signal the module
        # docstring's exit-code table documents.
        if any_path_rejected:
            _print(
                "  Note: one or more malformed repo paths were rejected during parsing "
                "(see warnings above).",
                file=sys.stderr,
            )
            return 3
        return 0

    # ---- bootstrap each selected repo --------------------------------------
    succeeded: List[str] = []
    failed: List[str] = []
    # Review: code-reviewer — F7, renamed from skipped_already_current (the
    # only path that populates it is bs_exit == 2, "not a git repo" — the
    # old name implied "already up to date, nothing to do").
    skipped_not_git_repo: List[str] = []

    for repo in selected_repos:
        posix = _normalize_path(repo)
        actual_path = posix if os.path.isdir(posix) else repo

        _print("")
        _print(f"--- Bootstrapping: {repo}")

        bs_exit = bootstrap_repo_main(["--root", actual_path, "--non-interactive"])

        if bs_exit == 0:
            _print("  bootstrap: OK")
            stamp_written = _coordinator_currency_write(actual_path, plugin_root)
            if stamp_written:
                stamp_path = os.path.join(actual_path, "docs", "coordinator-currency.yaml")
                if os.path.isfile(stamp_path):
                    stamp_status = _git(
                        ["status", "--porcelain", "--", "docs/coordinator-currency.yaml"],
                        root=actual_path,
                    )
                    stamp_dirty = (stamp_status.stdout or "").strip() if stamp_status.returncode == 0 else ""
                    if stamp_dirty:
                        _git(["add", "--", "docs/coordinator-currency.yaml"], root=actual_path)
                        commit_proc = subprocess.run(
                            [
                                "git",
                                "-C",
                                actual_path,
                                "commit",
                                "-m",
                                "chore(coordinator): record currency stamp",
                            ],
                            timeout=_GIT_TIMEOUT_SECS,
                            stdin=subprocess.DEVNULL,
                            **_CREATIONFLAGS,
                        )
                        if commit_proc.returncode != 0:
                            _print(
                                "  currency: FAILED — target repo hook rejected currency-stamp "
                                f"commit (exit {commit_proc.returncode})"
                            )
                            _print(
                                f"  currency: resolve the hook failure in '{repo}' and re-run "
                                "/repo-setup --batch."
                            )
                            failed.append(repo)
                            continue
                        _print("  currency: stamped and committed")
                    else:
                        _print("  currency: stamp already current (no commit needed)")
                else:
                    _print("  currency: stamp write skipped (coordinator-schema-version may be absent)")
            else:
                _print("  currency: stamp failed (non-fatal — bootstrap succeeded)")
            succeeded.append(repo)
        elif bs_exit == 2:
            _print("  bootstrap: SKIPPED (not a git repo — run interactively to be offered git init)")
            skipped_not_git_repo.append(repo)
        else:
            _print(f"  bootstrap: FAILED (bootstrap-repo.sh exit {bs_exit})")
            failed.append(repo)

    # ---- summary table ------------------------------------------------------
    _print("")
    _print("=== /repo-setup --batch summary ===")
    _print("")
    _print(f"  Succeeded:  {len(succeeded)}")
    _print(f"  Failed:     {len(failed)}")
    _print(f"  Skipped:    {len(skipped_not_git_repo)}")
    _print(f"  Not on disk: {len(missing_repos)}")
    _print("")

    if succeeded:
        _print("  Bootstrapped:")
        for r in succeeded:
            _print(f"    OK  {r}")

    if skipped_not_git_repo:
        _print("  Skipped:")
        for r in skipped_not_git_repo:
            _print(f"    --  {r}")

    if failed:
        _print("  Failed:")
        for r in failed:
            _print(f"    XX  {r}")
        _print("")
        _print("  One or more repos failed to bootstrap. Check per-repo output above.")
        _print("  bootstrap-repo.sh exits:")
        _print("    3 — dirty working tree (commit or stash first)")
        _print("    4 — conflict-warn gate (consumer-modified files detected)")
        _print("    1 — usage/prerequisite error")
        return 3

    _print("  /repo-setup --batch complete.")
    _print("  Revert any bootstrap commit: git -C <repo> revert HEAD")
    if any_path_rejected:
        _print(
            "  Note: one or more malformed repo paths were rejected during parsing "
            "(see warnings above).",
            file=sys.stderr,
        )
        return 3
    return 0


def _read_line(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        print(f"skip: _read_line: return input(prompt).strip() failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
