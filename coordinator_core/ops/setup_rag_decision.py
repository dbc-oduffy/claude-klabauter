"""
coordinator_core.ops.setup_rag_decision — RAG-index decision resolver for
/repo-setup.

Purpose: resolves the RAG-index decision at repo-setup time via an explicit
three-branch decision tree. Never writes a dead "offer to index" note for a
repo that cannot be indexed.

Decision branches:
    1. UE repo + example-retrieval-repo daemon present  -> emit "offer to index" text
       (caller surfaces it; nothing written to disk).
    2. Non-UE repo (any daemon state)        -> write tripwire block into
       target CLAUDE.md.
    3. UE repo + no daemon present            -> write tripwire block into
       target CLAUDE.md.

Non-UE upstream limitation (branch 2): the "offer to index" branch is
currently UE-only because an upstream `example-retrieval-repo-ue-addon`
.uproject-abstention defect blocks indexing of non-UE repos. This is a known
upstream limitation in example-retrieval-repo-ue-addon (tracked via cross-repo memo
Example-retrieval-repo-ue-addon-em); widen to non-UE only after that defect is resolved.

Detect-then-fail-loud: if UE-vs-non-UE or daemon-presence cannot be
determined unambiguously, main() returns non-zero with a remediation message
rather than silently picking a branch.

Idempotent: re-running does NOT duplicate the tripwire block in CLAUDE.md.

Mockable probe: SETUP_RAG_DAEMON_PROBE can be set to a command string
(word-split, NOT shell-interpreted -- no shell metacharacter injection) that
exits 0 (daemon present) or non-zero (daemon absent). If unset, the default
probe checks `example-retrieval-repo status` on PATH.

Exit codes (parity-critical -- matches the bash oracle's contract exactly):
    0  Decision resolved and action taken (or dry-run completed).
    1  Usage error or unambiguous-detection failure (fail-loud).
    2  RESERVED for the coordinator-claude trampoline's own claude-klabauter-link/import failure
       (CLAUDE_KLABAUTER_ROOT resolution / ImportError) -- never returned by this
       module's own main(); documented here so the two layers' exit-code
       tables stay disjoint (rule: transport-failure code must not collide
       with a business code -- see docs/plans/2026-07-16-bash-clean-slate-
       residual-migration.md porter-brief addendum § 3b).

Environment overrides (for tests, mirrors the bash oracle):
    SETUP_RAG_TARGET_ROOT           -- target repo root path (overrides
                                        --root and cwd)
    SETUP_RAG_DAEMON_PROBE          -- command string to test daemon
                                        presence (word-split argv, exit 0 =
                                        present)
    SETUP_RAG_IS_UE_REPO            -- "1" to force UE, "0" to force non-UE
                                        (test injection)
    SETUP_RAG_PROBE_TIMEOUT_SECS    -- daemon-probe subprocess timeout in
                                        seconds (default 10; NEW in the
                                        Python port -- the bash oracle had no
                                        timeout on this subprocess call, an
                                        unbounded-hang class bug fixed here
                                        per the porter-brief addendum rule 2)

Port of: setup-rag-decision.sh (coordinator-claude 6fb5fb37, 2026-07-22)
Spec backlink: docs/plans/2026-06-23-setup-time-substrate-completeness.md § C1c (AC3)
               docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Negative-spec:
    - Does NOT support "sourced" invocation (the bash oracle offered a dual
      standalone/sourced CLI contract, `source setup-rag-decision.sh` then
      call `setup_rag_decision ...`). This is a deliberate, non-regressive
      scope narrowing, not a silent drop: the sole live caller
      (coordinator/skills/repo-setup/SKILL.md) invokes the coordinator-claude-side
      trampoline ONLY as a subprocess (`bash setup-rag-decision.sh --root
      ...`) and its own doc explicitly forbids sourcing ("every helper is
      fail-loud and calls exit on ambiguity, so sourcing would terminate the
      repo-setup shell... sourcing it bare silently no-ops the decision
      block") -- sourced invocation was never a live-used path. Converting
      the coordinator-claude trampoline to the sh/python polyglot shape makes sourcing
      actively unsafe (the polyglot re-exec line would replace the sourcing
      shell), so this port intentionally does not preserve it.
    - Does NOT reproduce the bash oracle's `BASH_VERSINFO[0] < 4` guard --
      that guard existed to fail loud on a stock macOS bash 3.2, a bash-only
      concern with no Python analogue; dropping it is not a behavior
      regression against any caller.
    - Bash-4+ path-separator normalisation (`root="${root//\\//}"`, for
      Git-Bash/MSYS on Windows) IS reproduced (see `_normalize_root`).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple
from coordinator_core.win_portability import no_console_creationflags


_CREATIONFLAGS = no_console_creationflags()


_TRIPWIRE_SENTINEL = "coordinator-rag-tripwire: un-indexed"

_TRIPWIRE_BLOCK = f"""<!-- {_TRIPWIRE_SENTINEL} -->
## RAG Index

**Status: un-indexed; use Tier-3 (Read/Grep/Glob)**

This repo is not indexed by example-retrieval-repo. Use Tier-3 investigation:
Read known paths, Grep specific symbols, Glob patterns.
Do not assume a semantic search index is available.
<!-- end coordinator-rag-tripwire -->"""

_HELP_TEXT = """Usage (standalone):
  setup-rag-decision.sh [--root <path>] [--dry-run] [--help]

Flags:
  --root <path>  Target repo root. Defaults to cwd.
  --dry-run      Print what would happen; write nothing.
  --help | -h    Show this help and exit.

Exit codes:
  0  Decision resolved and action taken (or dry-run completed).
  1  Usage error or unambiguous-detection failure (fail-loud)."""

_DEFAULT_PROBE_TIMEOUT_SECS = 10


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_ue_repo(root: str) -> int:
    """Return 0 if `root` is a UE repo, 1 if not, 2 if detection is ambiguous.

    Mirrors the bash oracle's tri-state _srd_is_ue_repo: scans for
    `*.uproject` files at depth 1 (immediate children only) -- deeper
    .uproject files are engine samples/sub-projects and do not make the root
    a first-class UE consumer.

    # --- Tier 2 (behaviour change -- PM sign-off required) ---
    NOTE: uses `os.scandir()`, NOT `Path.glob("*.uproject")` -- `Path.glob()`'s
    selector silently swallows `PermissionError` while walking (empirically
    re-verified: a chmod-000 root yields an empty iterator from `glob()`, no
    exception), which previously made an unreadable root read as "not a UE
    repo" (count == 0 -> return 1) rather than the ambiguous-detection code (2)
    this function's own contract reserves for exactly this "cannot determine"
    case. `os.scandir()` raises `PermissionError` (an `OSError` subclass) as
    expected, letting the existing `except OSError` branch route an unreadable
    root to the correct return-2 slot with the existing ambiguous-detection
    stderr message.
    # --- end Tier 2 ---
    """
    forced = os.environ.get("SETUP_RAG_IS_UE_REPO", "")
    if forced:
        if forced == "1":
            return 0
        if forced == "0":
            return 1
        print(
            f'setup-rag-decision: SETUP_RAG_IS_UE_REPO must be "1" or "0", got: {forced}',
            file=sys.stderr,
        )
        return 2

    try:
        with os.scandir(root) as it:
            uproject_files = [
                Path(e.path) for e in it if e.name.endswith(".uproject") and e.is_file()
            ]
    except OSError:
        print(
            f"setup-rag-decision: ambiguous UE detection: cannot read {root}: {sys.exc_info()[1]}",
            file=sys.stderr,
        )
        print(
            "  Remediation: ensure the repo root is readable, or set",
            file=sys.stderr,
        )
        print(
            "  SETUP_RAG_IS_UE_REPO=1 or SETUP_RAG_IS_UE_REPO=0 to override.",
            file=sys.stderr,
        )
        return 2

    count = len(uproject_files)
    if count == 0:
        return 1
    if count == 1:
        return 0

    print(
        f"setup-rag-decision: ambiguous UE detection: {count} .uproject files found at {root}",
        file=sys.stderr,
    )
    print(
        "  Remediation: ensure exactly one .uproject file exists at the repo root,",
        file=sys.stderr,
    )
    print(
        "  or set SETUP_RAG_IS_UE_REPO=1 or SETUP_RAG_IS_UE_REPO=0 to override.",
        file=sys.stderr,
    )
    return 2


def _daemon_present() -> bool:
    """Return True if the example-retrieval-repo daemon is reachable.

    Uses SETUP_RAG_DAEMON_PROBE if set (word-split into argv -- NOT
    shell-interpreted, mirroring the bash oracle's direct (unquoted-var)
    invocation which refuses shell-metacharacter injection); falls back to
    `example-retrieval-repo status` on PATH.

    Every subprocess call carries an explicit timeout and a stdin guard
    (porter-brief addendum rule 2) -- the bash oracle had neither, an
    unbounded-hang-class gap this port closes.
    """
    try:
        timeout = int(os.environ.get("SETUP_RAG_PROBE_TIMEOUT_SECS", str(_DEFAULT_PROBE_TIMEOUT_SECS)))
    except ValueError:
        timeout = _DEFAULT_PROBE_TIMEOUT_SECS

    probe = os.environ.get("SETUP_RAG_DAEMON_PROBE", "")
    if probe:
        try:
            argv = shlex.split(probe)
        except ValueError:
            # Unparseable probe string (e.g. unbalanced quotes) -- treat as
            # absent rather than raise; mirrors fail-safe posture elsewhere
            # in this module.
            return False
        if not argv:
            return False
        try:
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                **_CREATIONFLAGS,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
            print(f"skip: _daemon_present: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
            return False

    example_retrieval_repo = shutil.which("example-retrieval-repo")
    if not example_retrieval_repo:
        return False
    try:
        result = subprocess.run(
            [example_retrieval_repo, "status"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            **_CREATIONFLAGS,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        print(f"skip: _daemon_present: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False


def _tripwire_present(claude_md: Path) -> bool:
    """Idempotency guard: True if the tripwire sentinel already exists in `claude_md`."""
    if not claude_md.is_file():
        return False
    try:
        content = claude_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        print(f"skip: _tripwire_present: content = claude_md.read_text(encoding=\"utf-8\", errors=\"replace\") failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    return _TRIPWIRE_SENTINEL in content


def _write_tripwire(root: str, dry_run: bool) -> None:
    """Append the tripwire block to `<root>/CLAUDE.md` if not already present.

    When dry_run, print what would happen without writing.
    """
    claude_md = Path(root) / "CLAUDE.md"

    if _tripwire_present(claude_md):
        if dry_run:
            print(
                f"[setup-rag-decision dry-run] tripwire already present in {claude_md} — skipping (idempotent)"
            )
        return

    if dry_run:
        print(f"[setup-rag-decision dry-run] would append tripwire block to {claude_md}")
        return

    with claude_md.open("a", encoding="utf-8") as fh:
        fh.write(f"\n{_TRIPWIRE_BLOCK}\n")
    print(f"setup-rag-decision: tripwire block written to {claude_md}")


def _normalize_root(root: str) -> str:
    """Normalise path separators (Git-Bash/MSYS on Windows) -- mirrors the
    bash oracle's `root="${root//\\\\//}"`."""
    return root.replace("\\", "/")


def _parse_args(argv: List[str]) -> Tuple[Optional[str], bool, Optional[int], Optional[str]]:
    """Parse CLI args. Returns (root, dry_run, early_exit_code, help_text).

    early_exit_code is set (non-None) when parsing itself determines the
    outcome (--help, or a usage error) -- caller returns it directly without
    running the decision tree.
    """
    root: Optional[str] = None
    dry_run = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--help", "-h"):
            return None, False, 0, _HELP_TEXT
        if arg == "--root":
            if i + 1 >= len(argv):
                print("setup-rag-decision: --root requires an argument", file=sys.stderr)
                return None, False, 1, None
            root = argv[i + 1]
            i += 2
            continue
        if arg == "--dry-run":
            dry_run = True
            i += 1
            continue
        if arg.startswith("-"):
            print(f"setup-rag-decision: unknown flag: {arg}", file=sys.stderr)
            return None, False, 1, None
        print(f"setup-rag-decision: unexpected argument: {arg}", file=sys.stderr)
        return None, False, 1, None
    return root, dry_run, None, None


# ---------------------------------------------------------------------------
# Public: setup_rag_decision(argv) -> exit code
# ---------------------------------------------------------------------------


def setup_rag_decision(argv: List[str]) -> int:
    """Resolve the RAG-index decision and act on it. Returns an exit code."""
    root, dry_run, early_exit, help_text = _parse_args(argv)
    if early_exit is not None:
        if help_text is not None:
            print(help_text)
        return early_exit

    # Resolve target root (env override -> --root flag -> cwd).
    env_root = os.environ.get("SETUP_RAG_TARGET_ROOT", "")
    if env_root:
        root = env_root
    if not root:
        root = os.getcwd()

    root = _normalize_root(root)

    if not os.path.isdir(root):
        print(f"setup-rag-decision: target root does not exist: {root}", file=sys.stderr)
        return 1

    # --- Branch 1: UE detection ---
    ue_result = _is_ue_repo(root)
    if ue_result == 2:
        # Ambiguous -- fail loud per detect-then-fail-loud contract.
        return 1
    is_ue = ue_result == 0

    # --- Branch 2 & 3: daemon detection (only needed if UE) ---
    if is_ue:
        daemon_present = _daemon_present()

        if daemon_present:
            # Branch 1: UE repo + daemon present -> offer to index.
            if dry_run:
                print("[setup-rag-decision dry-run] branch 1: UE repo + daemon present → offer to index")
            else:
                print("setup-rag-decision: branch 1 — UE repo with example-retrieval-repo daemon detected.")
                print("  Offer: run example-retrieval-repo index to build a semantic index for this repo.")
            return 0

        # Branch 3: UE repo but no daemon -> tripwire.
        if dry_run:
            print("[setup-rag-decision dry-run] branch 3: UE repo but no daemon → tripwire")
        else:
            print("setup-rag-decision: branch 3 — UE repo detected but example-retrieval-repo daemon is absent.")
            print("  Writing un-indexed tripwire to CLAUDE.md.")
            print("  To enable indexing: install and start the example-retrieval-repo daemon, then re-run /repo-setup.")
        _write_tripwire(root, dry_run)
        return 0

    # Branch 2: non-UE repo -> tripwire (daemon state irrelevant).
    # Upstream limitation: the "offer to index" branch is currently UE-only
    # because example-retrieval-repo-ue-addon's .uproject-abstention defect blocks
    # indexing of non-UE repos (tracked via cross-repo memo
    # example-retrieval-repo-ue-addon-em). Widen after that upstream defect is resolved.
    if dry_run:
        print(
            "[setup-rag-decision dry-run] branch 2: non-UE repo → tripwire "
            "(upstream .uproject-abstention defect, cross-repo memo example-retrieval-repo-ue-addon-em)"
        )
    else:
        print("setup-rag-decision: branch 2 — non-UE repo. RAG indexing is not yet supported")
        print("  for non-UE repos: blocked by example-retrieval-repo-ue-addon .uproject-abstention defect")
        print("  (cross-repo memo example-retrieval-repo-ue-addon-em). Writing un-indexed tripwire to CLAUDE.md.")
    _write_tripwire(root, dry_run)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return setup_rag_decision(argv)


if __name__ == "__main__":
    sys.exit(main())
