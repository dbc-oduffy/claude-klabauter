"""
coordinator_core.ops.cruft_sweep — Layer 1 cruft-pruner engine (T3a-g1a port).

Purpose: claude-klabauter-native port of the four sweep phases in the example-doctrine-repo-owned
`coordinator/bin/cruft-sweep.sh` trampoline (DR-047 contract-vs-engine split).
This module owns the pure, fully-resolved-config sweep logic; the example-doctrine-repo
trampoline owns CLI parsing, machine-local/registry resolution, and the
frozen `coordinator_state_root` seam — none of that lives here.

Four phase functions, each taking fully-resolved config and returning
`(total_bytes, total_items)`, mirroring the bash `_sweep_*` functions'
global-accumulator contract byte-for-byte in behavior:

    sweep_harness(projects_root, file_history_root, days, blocklist, *,
                  apply, json_mode, quiet, log_path=None) -> (bytes, items)
    sweep_scratch(repo_root, scratch_age_days, *,
                  apply, json_mode, quiet, log_path=None) -> (bytes, items)
    sweep_subagent_sandbox_files(repo_root, *,
                  apply, json_mode, quiet, log_path=None) -> (bytes, items)
    sweep_orphans(parent_roots, whitelist, *,
                  apply, json_mode, quiet, log_path=None) -> (bytes, items)

A fifth phase function, `sweep_empty_toplevel_dirs`, is **NET-NEW — it has
NO bash-oracle counterpart and is NOT part of the cruft-sweep.sh byte-parity
port** (see "Net-new phase" section below). It shares this module's
signature contract (`(..., *, apply, json_mode, quiet, log_path=None,
watchdog_ceiling_secs=None, emit_fn=None) -> (bytes, items)`) and reuses the
same shared helpers (`_Watchdog`, `_banner`, `emit_jsonl`, `_delete_path`,
`_resolve_repo_root`, `_get_mtime`) rather than growing private twins, but a
future reader auditing this module against the bash oracle line-for-line
should NOT go looking for the bash it was "ported" from — there isn't one.

Net-new phase — sweep_empty_toplevel_dirs (added 2026-07-28, incident-driven):
  Catches, by structure rather than by name, a class of cruft the four
  ported phases cannot see: a top-level child directory of the repo root
  that contains zero files anywhere in its subtree. Git is structurally
  blind to such directories (nothing to track, so `git status
  --untracked-files=all` never surfaces them and `.gitignore` never gets a
  say), and `sweep_orphans`'s name-literal + sonnet-fingerprint gate only
  catches a directory that both matches a known name AND contains a
  recognizable fingerprint file — an arbitrary empty dir (e.g. a stray
  `mkdir`'d prose word, or a fake-$HOME skeleton with ten levels of nested
  empty directories and zero files anywhere) satisfies neither. The
  predicate: a depth-1 child of the repo root, empty (zero files
  recursively, symlinks never followed and always disqualifying), whose
  subtree max-mtime is older than the `_MTIME_FLOOR_SECS` (24h) floor, not
  git-ignored, and not hard-excluded/whitelisted. Scoped to depth-1 children
  only (never scans for empty dirs at arbitrary depth) and fails closed
  (skips the whole phase, deletes nothing) whenever `repo_root` is not
  inside a git work tree or `git` itself is unavailable — this phase can
  destroy data no other mechanism protects, so an unresolvable git context
  must not silently default to "sweep anyway."
  Golden-diff / byte-parity exclusion: no exclusion was needed, because no
  cruft-sweep byte-parity harness exists in this tree — the four ported
  phases' oracle fidelity is asserted via phase-specific behavioral tests,
  not a literal golden-diff runner (`coordinator_core/percolate/tests/
  test_parity_doe.py` is the tree's only "parity" test and targets an
  unrelated seam, the example-doctrine-repo percolate mirror). Recorded so a future reader
  neither hunts for a harness that does not exist nor builds one that
  silently swallows this phase: if such a harness is ever added, this phase
  MUST be excluded from it — there is no bash to diff against, and a parity
  fixture would either fail on the (correct) absence of a bash counterpart
  or force a fabricated one into existence.
  Tests live alongside the other four phases' tests in
  `coordinator_core/ops/test_cruft_sweep.py`, asserted purely against this
  module's own Python behavior, never against a bash byte-parity oracle.

Each accepts an optional `emit_fn: Callable[[dict], None]` — defaults to
`print(json.dumps(rec))` on stdout (bash-parity), but a caller (golden-diff
test, or the example-doctrine-repo trampoline in --json mode) may pass a list-appending
callable to capture records without a subprocess/stdout round-trip.

Self-registration: importing this module calls register_op("cruft_sweep.run",
_run_handler) as a side-effect — see central-reg fragment
Example-doctrine-repo/scratch/subagent-sandbox/bash-to-python-engine-migration/central-reg/T3a-g1a.txt
for the OP_MODULE_MAP / OP_CLASSIFICATION additions this self-registration
still needs (central-registry deferral — this module does NOT edit those
shared files itself).

Byte-parity target: cruft-sweep.sh (example-doctrine-repo 6fb5fb37, 2026-07-22, bash oracle).

Design decisions pinned per EM ruling (recipe § cruft-sweep, Q5/Q6/Q8):
  - Q5: the example-doctrine-repo-side trampoline keeps the `.sh` extension (72 live doc refs).
    Not this module's concern, noted for context only.
  - Q6: this module does NOT resolve COORDINATOR_CONTENT_ROOT /
    resolve-coordinator-clone.sh / coordinator-trusted-root-guard.sh — those
    stay example-doctrine-repo-side unchanged; this module is handed fully-resolved paths.
  - Q8: `_dir_size_bytes` approximates the bash oracle's `du -sk <dir> * 1024`
    ALLOCATED-disk-usage semantic (not apparent byte size) by summing
    `st_blocks * 512` over the directory root and every file/dir yielded by
    `os.walk`, then floor-dividing by 1024 and re-multiplying by 1024 —
    reproducing the oracle's exact two-step KB rounding. `st_blocks` is a
    POSIX-only stat field; on platforms where it is absent (Windows) this
    falls back to summing `st_size` (apparent size), which will NOT byte-match
    `du -sk` — documented divergence, not a golden-diff target on Windows.

Negative-spec:
  - Does NOT resolve `machine-local`, `coordinator_state_root`, or any
    registry/env-config value — every path/threshold argument must already
    be resolved by the caller (example-doctrine-repo trampoline, or a test fixture).
  - Does NOT build the UUID blocklist from a `--handoffs-glob` flag string —
    `build_uuid_blocklist` takes an already-resolved handoffs directory Path.
  - Does NOT implement `--parent-root` default-derivation (`_default_parent_roots`
    in the bash oracle, which shells to `machine-local keys`) — the example-doctrine-repo
    trampoline resolves `parent_roots` and passes the resolved list in.
  - Does NOT read/write the machine-local `parent_whitelist` TOML array —
    the example-doctrine-repo trampoline resolves `whitelist` (upgraded to `tomllib.load()`
    per recipe) and passes the resolved list in.
  - Does NOT emit the grand-total / run-marker log rows — those span all
    four phases and CLASS dispatch, which is trampoline-owned orchestration,
    not a single sweep phase's concern. The per-phase log-append row IS
    engine-owned (each sweep_* function appends its own row when apply=True
    and items>0, matching the bash oracle's per-function log-append block).
  - Does NOT invoke `cc_invoke()`'s JSON-RPC subprocess-envelope seam
    internally; the registered `cruft_sweep.run` op is a convenience façade
    for JSON-RPC callers, not the primary call path (the example-doctrine-repo trampoline
    imports and calls the phase functions in-process — see recipe § example-doctrine-repo-side
    work item 5).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from coordinator_core.ipc import register_op
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.wire_paths import rel_id
from coordinator_core.win_portability import no_console_creationflags

# ---------------------------------------------------------------------------
# Constants — mirror bash oracle literals
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

_SCRATCH_AUTO_PRUNE_NAMES = {"tmp-cc", "nonexistent", "fake"}
_SCRATCH_CONFIRM_NEEDED_NAMES = {"tmp", "scratch", "output"}
_NEGATIVE_SPEC_COMPONENTS = {
    "archive", "tasks", "state", "docs", "node_modules", ".venv", "__pycache__",
}
_ORPHAN_NAME_MATCH_LITERALS = {
    "nonexistent", "tmp", "tmp-cc", "fake", "null", "undefined",
}
_ORPHAN_HARD_EXCLUDE_NAMES = {
    "state", "docs", "archive",
    "$RECYCLE.BIN", "System Volume Information", ".github-private",
}

# 24h hard mtime floor — RD-2 consolidated age gate and the subagent-sandbox
# file-level reap's sole gate (spec: docs/plans/2026-06-14-deep-research-workdir-out-of-killzone.md RD-2).
_MTIME_FLOOR_SECS = 86400

# Watchdog wall-clock ceiling (stall arm intentionally dormant — every bash
# call site advances its counter every iteration, per the recipe's confirmed
# reading of the source). Injectable via kwarg or env override so a golden-net
# test can force a bail without a real 300s sleep.
_WATCHDOG_CEILING_SECS_DEFAULT = 300

# 60s cap per delete — matches bash `cs_timeout 60 -- rm -rf ...`.
_DELETE_TIMEOUT_SECS = 60

EmitFn = Callable[[dict], None]


# ---------------------------------------------------------------------------
# Watchdog — ceiling-only (stall arm dormant at every call site in the oracle)
# ---------------------------------------------------------------------------


class _Watchdog:
    """Cooperative wall-clock ceiling bail. Mirrors cs_watchdog_check's
    ceiling-only operative behavior (stall-detection arm is dormant in every
    oracle call site, per recipe confirmation)."""

    def __init__(self, ceiling_secs: Optional[float] = None):
        if ceiling_secs is None:
            env_val = os.environ.get("CRUFT_SWEEP_WATCHDOG_CEILING_SECS")
            ceiling_secs = (
                float(env_val) if env_val else _WATCHDOG_CEILING_SECS_DEFAULT
            )
        self._ceiling = ceiling_secs
        self._start = time.monotonic()

    def check(self) -> bool:
        """Return True to continue, False to bail (ceiling exceeded)."""
        return (time.monotonic() - self._start) < self._ceiling


# ---------------------------------------------------------------------------
# Stat helpers
# ---------------------------------------------------------------------------


def _get_mtime(path: Path) -> int:
    """Mirrors bash `_get_mtime`: mtime as epoch seconds, 0 on failure."""
    try:
        return int(path.stat().st_mtime)
    except OSError:
        print(f"_get_mtime: stat failed for {path}: {sys.exc_info()[1]}", file=sys.stderr)
        return 0


def _file_size(path: Path) -> int:
    """Mirrors bash `_file_size` (wc -c): file size in bytes, 0 on failure."""
    try:
        return path.stat().st_size
    except OSError:
        print(f"_file_size: stat failed for {path}: {sys.exc_info()[1]}", file=sys.stderr)
        return 0


def _dir_size_bytes(path: Path, budget_secs: float = 5.0) -> int:
    """Mirrors bash `_dir_size_bytes` (`du -sk <dir>` * 1024, KB rounding).

    See module docstring Q8 for the st_blocks*512 allocated-disk-usage
    rationale and the Windows (st_blocks-absent) fallback caveat.

    Drift-audit D3 (docs/research/2026-07-28-cruft-sweep-duplicate-port-drift-audit.md):
    wall-clock-bounded at `budget_secs` (default 5.0s), breaking out of the walk past
    the deadline rather than walking an arbitrarily large/NFS-backed subtree to
    completion. Ported from the sibling `coordinator/bin/cruft-sweep` trampoline's own
    `_dir_size_bytes`, which added this exact budget to fix a real production hang
    (state/bug-backlog/2026-07-19-cruft-sweep-sh-dir-size-bytes-du-sk-hang-a052d734d210.yaml)
    — this module previously had no such budget. The budget only truncates the total
    (an under-count on a huge/slow subtree, same direction as the pre-existing
    unreadable-subtree under-count below); it never changes a prune/skip decision,
    since those are driven by mtime, not size.
    """
    total_512blocks = 0
    have_st_blocks = True
    try:
        st = path.stat()
        if hasattr(st, "st_blocks"):
            total_512blocks += st.st_blocks
        else:
            have_st_blocks = False
    except OSError:
        print(f"_dir_size_bytes: stat failed for {path}: {sys.exc_info()[1]}", file=sys.stderr)
        pass

    total_size_fallback = 0
    deadline = time.monotonic() + budget_secs
    # NOTE: bare os.walk(), no onerror= -- an unreadable nested subtree
    # silently drops out of the total (same idiom this session's fixes
    # eliminated elsewhere; see Finding 3, 2026-07-22 slice1 review). Kept
    # bare here deliberately: this is a best-effort human-readable-MB
    # estimate feeding banners/JSONL size_bytes fields, not a prune/no-prune
    # gate, so it under-counts on an unreadable subtree rather than warning.
    for root, dirs, files in os.walk(path):
        if time.monotonic() > deadline:
            break
        for name in list(dirs) + list(files):
            p = os.path.join(root, name)
            try:
                st = os.lstat(p)
            except OSError:
                print(f"_dir_size_bytes: lstat failed for {p}: {sys.exc_info()[1]}", file=sys.stderr)
                continue
            if have_st_blocks and hasattr(st, "st_blocks"):
                total_512blocks += st.st_blocks
            else:
                have_st_blocks = False
                total_size_fallback += st.st_size

    if have_st_blocks:
        total_bytes = total_512blocks * 512
    else:
        # Fallback path (Windows / no st_blocks): apparent-size sum. Documented
        # divergence from `du -sk` — not a golden-diff parity target off-POSIX.
        total_bytes = total_size_fallback

    kb = total_bytes // 1024
    return kb * 1024


# ---------------------------------------------------------------------------
# JSONL emission
# ---------------------------------------------------------------------------


def emit_jsonl(
    class_: str,
    path: str,
    name: str,
    size_bytes: int,
    mtime: int,
    disposition: str,
    evidence: str,
    *,
    emit_fn: Optional[EmitFn] = None,
) -> None:
    """Mirrors bash `_emit_jsonl`. Default emit_fn prints one JSON line to
    stdout (bash-parity); pass a list-appending callable to capture records
    in-process (golden-diff tests, or the example-doctrine-repo trampoline's --json mode)."""
    rec = {
        "class": class_,
        "path": path,
        "name": name,
        "size_bytes": int(size_bytes),
        "mtime": int(mtime),
        "disposition": disposition,
        "evidence": evidence,
    }
    if emit_fn is not None:
        emit_fn(rec)
    else:
        print(json.dumps(rec))


def _banner(msg: str, *, quiet: bool) -> None:
    if not quiet:
        print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# Delete primitive — subprocess rm -rf with 60s timeout, best-effort continue.
# Mirrors bash `cs_timeout 60 -- rm -rf "$target" 2>/dev/null || true`: a
# genuinely wedged (D-state/NFS) delete can be timed out and abandoned the
# same way cs_timeout abandons the bash rm; shutil.rmtree cannot be hard-killed.
# ---------------------------------------------------------------------------


def _delete_path(target: Path) -> bool:
    """Best-effort `rm -rf` with a 60s timeout. Returns True iff `target` is
    confirmed gone afterward — callers MUST gate prune counting/logging on
    this return value rather than assuming success from a non-raising call.

    --- Tier 2 (behaviour change -- PM sign-off required) ---
    BEHAVIOUR CHANGE (2026-07-22, break-class fix): previously returned None
    and swallowed a nonzero returncode as well as both exceptions, so a
    failed delete (e.g. permission-denied) was still counted/logged as
    pruned. Now returncode is checked and existence is confirmed.
    --- end Tier 2 ---
    """
    try:
        result = subprocess.run(
            ["rm", "-rf", str(target)],
            timeout=_DELETE_TIMEOUT_SECS,
            capture_output=True,
            **no_console_creationflags(),
        )
        if result.returncode != 0:
            return False
    except (subprocess.TimeoutExpired, OSError):
        print(f"_delete_path: rm -rf failed for {target}: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    return not target.exists()


def _delete_file(target: Path) -> bool:
    """Best-effort file delete. Returns True iff `target` is confirmed gone
    afterward.

    --- Tier 2 (behaviour change -- PM sign-off required) ---
    See _delete_path docstring for the same behaviour-change note.
    --- end Tier 2 ---
    """
    try:
        target.unlink()
    except OSError:
        print(f"_delete_file: unlink failed for {target}: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    return not target.exists()


# ---------------------------------------------------------------------------
# Concurrency lock — non-blocking try-once-and-yield (NOT locked_write.py's
# locked_rmw, which blocks-with-timeout-and-mutates; wrong primitive here).
# ---------------------------------------------------------------------------


def try_acquire_lock(lock_dir: Path) -> bool:
    """Atomic try-lock via mkdir. Returns True on acquisition, False on
    contention (caller should exit 0 silently, matching the bash oracle)."""
    try:
        os.mkdir(lock_dir)
        return True
    except FileExistsError:
        print(f"try_acquire_lock: {lock_dir} already exists — another run owns it", file=sys.stderr)
        return False


def release_lock(lock_dir: Path) -> None:
    try:
        os.rmdir(lock_dir)
    except OSError:
        print(f"release_lock: rmdir failed for {lock_dir}: {sys.exc_info()[1]}", file=sys.stderr)
        pass


# ---------------------------------------------------------------------------
# Log-append helper — per-phase row, apply-mode + items-gated (matches every
# bash `_sweep_*` function's own log-append block).
# ---------------------------------------------------------------------------


def _append_log_row(log_path: Optional[Path], class_label: str, total_bytes: int, total_items: int) -> None:
    if log_path is None:
        return
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"| {ts} | {class_label} | {total_bytes} bytes | {total_items} items |\n")
        declare_write(log_path)
    except OSError:
        print(f"_append_log_row: could not write log row to {log_path}: {sys.exc_info()[1]}", file=sys.stderr)
        pass


# ---------------------------------------------------------------------------
# Shared name/path predicates
# ---------------------------------------------------------------------------


def _is_auto_prune_name(name: str) -> bool:
    if name in _SCRATCH_AUTO_PRUNE_NAMES:
        return True
    return len(name) == 1 and "a" <= name <= "z"


def _is_confirm_needed_name(name: str) -> bool:
    return name in _SCRATCH_CONFIRM_NEEDED_NAMES


def _is_backup_name(name: str) -> bool:
    if name.startswith("_"):
        return True
    if ".bak" in name:
        return True
    if "-bak-" in name:
        return True
    if ".preisource-bak-" in name:
        return True
    return False


def _has_git_boundary(path: str) -> bool:
    norm = path.replace("\\", "/")
    return ".git" in norm.split("/")


def _has_negative_spec_component(path: str) -> bool:
    norm = path.replace("\\", "/")
    parts = set(norm.split("/"))
    return bool(parts & _NEGATIVE_SPEC_COMPONENTS)


def _is_pruned_child(dir_str: str, pruned_parents: List[str]) -> bool:
    """True if `dir_str` is `parent` itself or nested under an already-queued-
    for-prune `parent` (i.e. an `rm -rf` of the parent already deleted it).

    Drift-audit D4 (docs/research/2026-07-28-cruft-sweep-duplicate-port-drift-audit.md):
    checks BOTH path separators, not just "/" — `os.walk()` yields native
    (backslash) paths on Windows, so a forward-slash-only prefix check can never
    match there, letting an already-deleted child be "found" again on its own
    turn in the loop and double-counted as a fresh prune (`_delete_path`'s
    `rm -rf` on an absent target still confirms non-existence). Ported from the
    sibling `coordinator/bin/cruft-sweep` trampoline's own `_is_pruned_child`,
    which already handled this; this module's prior `any(dir_str.startswith(parent
    + "/") ...)` check did not.
    """
    for parent in pruned_parents:
        if dir_str == parent:
            return True
        if dir_str.startswith(parent + "/") or dir_str.startswith(parent + "\\"):
            return True
    return False


def _is_untracked(repo_root: Path, path: Path) -> bool:
    """Mirrors bash `_is_untracked`: True if untracked-or-not-a-git-repo."""
    import shutil as _shutil

    if _shutil.which("git") is None:
        return True
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--git-dir"],
            capture_output=True, timeout=10,
            **no_console_creationflags(),
        )
        if r.returncode != 0:
            return True
    except (OSError, subprocess.TimeoutExpired):
        print(f"_is_untracked: git rev-parse --git-dir failed for {repo_root}: {sys.exc_info()[1]}", file=sys.stderr)
        return True

    try:
        # Forward-slash: `rel` is a git PATHSPEC argument below. git only ever
        # speaks '/' paths, so a native-separator pathspec is a cross-process
        # wire value with the wrong shape on Windows.
        rel = rel_id(path, repo_root)
    except ValueError:
        rel = str(path)

    try:
        r = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "--error-unmatch", rel],
            capture_output=True, timeout=10,
            **no_console_creationflags(),
        )
        if r.returncode == 0:
            return False  # tracked
    except (OSError, subprocess.TimeoutExpired):
        print(f"_is_untracked: git ls-files failed for {rel}: {sys.exc_info()[1]}", file=sys.stderr)
        pass

    # Not tracked (either check-ignore or plain untracked) — both treated as
    # untracked per the oracle's fall-through.
    return True


def _batch_is_untracked_dirs(repo_root: Path, dirs: Sequence[Path]) -> dict:
    """Batched replacement for a per-directory `_is_untracked` loop.

    Query shape established first: `sweep_scratch`'s per-candidate question
    is "is ANY tracked file present under this directory pathspec" — a
    worktree-index membership question, not a range/reachability one. `git
    ls-files -- <pathspec1> <pathspec2> ...` resolves each pathspec
    independently (object/pathspec batching, safe per § Anti-scope 1/2/4 —
    this is not `git rev-list A..B C..D`'s forbidden single set-expression
    shape), so every directory's tracked-or-not answer can be read off one
    combined `git ls-files` invocation instead of one `git ls-files
    --error-unmatch <dir>` spawn per directory.

    A `dirs` entry absent from the tracked-file output is read as
    "untracked" — the same reconciliation discipline as
    `emit/sections/handoffs.py::_resolve_shipped_in_dates` (prefix-match
    plus an explicit membership decision, § Anti-scope 25): here "absent"
    correctly means "no tracked file under this dir", matching the
    single-item oracle's own fall-through (a git failure, or a
    --error-unmatch miss, both read as untracked).

    Returns {str(dir): is_untracked_bool} for every entry in `dirs`.
    """
    import shutil as _shutil

    if not dirs:
        return {}
    if _shutil.which("git") is None:
        return {str(d): True for d in dirs}
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--git-dir"],
            capture_output=True, timeout=10,
            **no_console_creationflags(),
        )
        if r.returncode != 0:
            return {str(d): True for d in dirs}
    except (OSError, subprocess.TimeoutExpired):
        print(f"_batch_is_untracked_dirs: git rev-parse --git-dir failed for {repo_root}: {sys.exc_info()[1]}", file=sys.stderr)
        return {str(d): True for d in dirs}

    rels: dict = {}
    for d in dirs:
        try:
            rels[str(d)] = rel_id(d, repo_root)
        except ValueError:
            rels[str(d)] = str(d)

    pathspecs = sorted(set(rels.values()))
    tracked_files: List[str] = []
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "--"] + pathspecs,
            capture_output=True, text=True, timeout=30,
            **no_console_creationflags(),
        )
        if r.returncode == 0:
            tracked_files = r.stdout.splitlines()
        else:
            print(
                f"_batch_is_untracked_dirs: git ls-files exited {r.returncode} for {repo_root}: "
                f"{(r.stderr or '').strip()}",
                file=sys.stderr,
            )
    except (OSError, subprocess.TimeoutExpired):
        print(f"_batch_is_untracked_dirs: git ls-files failed for {repo_root}: {sys.exc_info()[1]}", file=sys.stderr)
        # tracked_files stays [] — every dir below is then reported
        # untracked, mirroring the single-item oracle's own except-clause
        # fall-through ("Not tracked ... per the oracle's fall-through").

    result: dict = {}
    for d in dirs:
        rel = rels[str(d)]
        prefix = rel.rstrip("/") + "/"
        is_tracked = any(f == rel or f.startswith(prefix) for f in tracked_files)
        result[str(d)] = not is_tracked
    return result


def _is_orphan_name_match(name: str) -> bool:
    if name in _ORPHAN_NAME_MATCH_LITERALS:
        return True
    if name.startswith("untitled"):
        return True
    return len(name) == 1 and "a" <= name <= "z"


def _has_sonnet_fingerprint(child: Path) -> bool:
    if (child / "vector" / "store" / "chroma.sqlite3").is_file():
        return True
    if (child / "project" / "Saved" / "ProjectRag" / "vector_store" / "chroma.sqlite3").is_file():
        return True
    jsonl = child / "mcp_queries.jsonl"
    if jsonl.is_file():
        try:
            count = sum(1 for _ in child.iterdir())
        except OSError:
            count = 1
        if count <= 1:
            return True
    return False


# ---------------------------------------------------------------------------
# Net-new predicates for sweep_empty_toplevel_dirs (see module docstring's
# "Net-new phase" section — no bash-oracle counterpart for any of these).
# ---------------------------------------------------------------------------


def _git_available() -> bool:
    import shutil as _shutil

    return _shutil.which("git") is not None


def _is_inside_git_work_tree(repo_root: Path) -> bool:
    """True iff `repo_root` resolves inside a git work tree. False (never
    raises) on any git failure/absence/timeout — callers MUST treat False as
    "cannot verify, skip" rather than "definitely not a repo, proceed"."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=10,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and r.stdout.strip() == "true"


def _is_git_ignored(repo_root: Path, name: str) -> bool:
    """True iff `name` (a top-level child of repo_root) matches a
    .gitignore pattern. False on any git failure — an unignorable-to-verify
    path is treated as NOT ignored (falls through to the other gates, which
    still require the 24h age floor and zero-files-recursively predicate
    before anything is ever deleted)."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_root), "check-ignore", "-q", "--", name],
            capture_output=True, timeout=10,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _batch_git_ignored_names(repo_root: Path, names: Sequence[str]) -> set:
    """Batched replacement for a per-child `_is_git_ignored` loop.

    Query shape established first: `sweep_empty_toplevel_dirs`'s per-child
    question is "does this top-level name match a `.gitignore` pattern" —
    `git check-ignore` is natively stdin-fed (`--stdin`, one name per line),
    so this is a single-call primitive, not `check-ignore -q -- <name>` run
    once per child. This is object/name batching, not range batching — each
    stdin line resolves independently, so § Anti-scope 1/2/4's forbidden
    set-expression shape does not apply here.

    `git check-ignore --stdin` prints ONLY the names that ARE ignored (one
    per line) — per § Anti-scope 25, a `names` entry absent from that output
    is read as "not ignored", matching the single-item oracle's own
    fail-open-to-"not ignored" behavior on any git failure (see
    `_is_git_ignored`'s docstring: "an unignorable-to-verify path is treated
    as NOT ignored"). Non-fatal `check-ignore` failure (including the
    documented "no name matched" returncode 1) returns an empty set for the
    same reason.

    Returns the set of `names` entries git-ignore-matched.
    """
    import shutil as _shutil

    if not names:
        return set()
    if _shutil.which("git") is None:
        return set()
    try:
        # bytes I/O, NOT text=True: on Windows, text-mode input translates a
        # bare "\n" to "\r\n" on write, and `check-ignore --stdin` reads
        # stdin as literal newline-delimited pathnames -- a trailing "\r"
        # left on each line either fails to match a real ignore pattern or
        # (worse) is silently absorbed into a directory-pattern match,
        # corrupting the ignored-name set either way. Encode/decode by hand
        # to force LF regardless of platform.
        r = subprocess.run(
            ["git", "-C", str(repo_root), "check-ignore", "--stdin"],
            input=("\n".join(names) + "\n").encode("utf-8"),
            capture_output=True, timeout=30,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"_batch_git_ignored_names: git check-ignore --stdin failed for {repo_root}: {sys.exc_info()[1]}", file=sys.stderr)
        return set()
    # returncode 0 = at least one match, 1 = no matches (not an error), any
    # other code = treated as a failure -> fail open to "not ignored", same
    # posture as the single-item helper.
    if r.returncode not in (0, 1):
        print(
            f"_batch_git_ignored_names: git check-ignore --stdin exited {r.returncode} for {repo_root}: "
            f"{(r.stderr or b'').decode('utf-8', errors='replace').strip()}",
            file=sys.stderr,
        )
        return set()
    stdout_text = r.stdout.decode("utf-8", errors="replace")
    return {line for line in stdout_text.splitlines() if line}


def _scan_empty_subtree(root: Path) -> Tuple[bool, int]:
    """Return (is_empty, max_mtime_epoch) for `root`'s recursive subtree.

    "Empty" means zero real files anywhere beneath `root` — not
    `len(os.listdir()) == 0`, since the incident this phase exists to catch
    (a fake-$HOME skeleton) had ten levels of nested empty directories.
    Symlinks (to a file OR a directory) count as entries that disqualify
    emptiness — git can track a symlink as a real blob, so a directory
    containing one is not safely git-invisible cruft — but are never
    followed/descended-into (os.walk's default `followlinks=False`), per the
    "symlinks count as entries, not as files-to-follow" pin.

    An unreadable nested subtree is treated conservatively as NOT empty
    (skip rather than risk deleting content this scan could not fully
    inspect) — same fail-closed posture as every delete gate in this module.
    """
    max_mtime = _get_mtime(root)
    has_content = False
    unreadable = False

    def _onerror(exc: OSError) -> None:
        nonlocal unreadable
        unreadable = True

    for dirpath, dirnames, filenames in os.walk(root, onerror=_onerror, followlinks=False):
        dp = Path(dirpath)
        try:
            max_mtime = max(max_mtime, int(dp.stat().st_mtime))
        except OSError:
            pass

        for fname in filenames:
            has_content = True
            try:
                max_mtime = max(max_mtime, int((dp / fname).lstat().st_mtime))
            except OSError:
                pass

        for dname in dirnames:
            dpath = dp / dname
            if dpath.is_symlink():
                has_content = True
                try:
                    max_mtime = max(max_mtime, int(dpath.lstat().st_mtime))
                except OSError:
                    pass

    return (not has_content and not unreadable), max_mtime


_PREDECESSOR_RE = re.compile(
    r"^predecessor:\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.MULTILINE,
)


class BlocklistIncompleteError(RuntimeError):
    """Raised when a harness sweep is asked to `apply` against a UUID
    blocklist that `build_uuid_blocklist` could not fully scan. A partial
    blocklist is a silently-narrowed protected set: a UUID whose handoff was
    unreadable would fall out of the blocklist and `_is_blocked` would wrongly
    return False for it, letting the sweep delete session/harness state that
    a live handoff still references. That is unrecoverable, so callers MUST
    fail closed (abort --apply) rather than proceed on incomplete knowledge.
    """


def build_uuid_blocklist(handoffs_dir: Path) -> Tuple[set, bool]:
    """Mirrors bash `_build_uuid_blocklist`: scan *.md files directly under
    handoffs_dir for `predecessor: <uuid>` lines, return the set of UUIDs.

    Takes an already-resolved directory Path (NOT a glob string) — glob
    resolution / --handoffs-glob parsing is the example-doctrine-repo trampoline's concern.
    Empty set (not error) when handoffs_dir doesn't exist.

    Returns (blocklist, complete). `complete` is False when at least one
    *.md file under handoffs_dir could not be read — the returned blocklist
    is then a lower bound, not the true protected set. Callers that gate a
    destructive sweep on this blocklist under apply=True MUST fail closed
    when complete is False (see BlocklistIncompleteError) rather than treat
    the narrowed set as authoritative.

    BEHAVIOUR CHANGE (2026-07-22, break-class fix): previously an unreadable
    file was silently skipped with a bare `continue` and the return type was
    a plain `set` with no signal that the scan was incomplete.
    """
    blocklist: set = set()
    complete = True
    if not handoffs_dir.is_dir():
        return blocklist, complete
    # NOTE: uses iterdir(), NOT glob("*.md") — Path.glob()'s selector silently
    # swallows PermissionError while walking (an unreadable handoffs_dir itself
    # yields an empty iterator, no exception), which would make an
    # `except OSError` around the glob() call dead code for the exact
    # permission-denied case it's meant to guard. iterdir() raises OSError
    # as expected, so an unreadable directory now trips complete=False
    # instead of silently returning (set(), True).
    try:
        entries = list(handoffs_dir.iterdir())
    except OSError as exc:
        # --- Tier 2 (behaviour change -- PM sign-off required) ---
        print(
            f"[cruft-sweep] WARNING: cannot scan handoffs directory "
            f"(protected set is now a LOWER BOUND): {handoffs_dir}: {exc}",
            file=sys.stderr,
        )
        return blocklist, False
        # --- end Tier 2 ---
    for md_file in entries:
        if not (md_file.suffix == ".md" and md_file.is_file()):
            continue
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(
                f"[cruft-sweep] WARNING: unreadable handoff file excluded from "
                f"UUID blocklist scan (protected set is now a LOWER BOUND): {md_file}: {exc}",
                file=sys.stderr,
            )
            complete = False
            continue
        for m in _PREDECESSOR_RE.finditer(content):
            blocklist.add(m.group(1))
    return blocklist, complete


def _is_blocked(uuid: str, blocklist: Iterable[str]) -> bool:
    if not uuid:
        return False
    return uuid in blocklist


# ---------------------------------------------------------------------------
# Phase A: harness retention sweep
# ---------------------------------------------------------------------------


def sweep_harness(
    projects_root: Path,
    file_history_root: Path,
    days: int,
    blocklist: Iterable[str],
    *,
    apply: bool,
    json_mode: bool,
    quiet: bool,
    log_path: Optional[Path] = None,
    watchdog_ceiling_secs: Optional[float] = None,
    emit_fn: Optional[EmitFn] = None,
) -> Tuple[int, int]:
    """Sweep projects/<repo>/<uuid>/ dirs, projects/<repo>/<uuid>.jsonl files,
    and file-history/<uuid>/ dirs older than `days`, skipping any UUID present
    in `blocklist`. Returns (total_bytes, total_items)."""
    blocklist = set(blocklist)
    now = int(time.time())
    threshold_sec = days * 86400

    pruned_dirs = 0
    pruned_jsonl = 0
    pruned_fh_dirs = 0
    total_bytes = 0
    skipped_blocked = 0

    wd = _Watchdog(watchdog_ceiling_secs)
    wd_uuid_bail = False
    wd_uuid_cnt = 0

    if projects_root.is_dir():
        for repo_dir in sorted(p for p in projects_root.iterdir() if p.is_dir()):
            if wd_uuid_bail:
                continue

            for uuid_dir in sorted(p for p in repo_dir.iterdir() if p.is_dir()):
                if not wd.check():
                    _banner(
                        f"[cruft-sweep] wall-clock ceiling bail on harness uuid sweep "
                        f"after {wd_uuid_cnt} candidates examined; current repo jsonl "
                        f"and subsequent repos skipped — will finish next run",
                        quiet=quiet,
                    )
                    wd_uuid_bail = True
                    break
                wd_uuid_cnt += 1

                dir_name = uuid_dir.name
                if not _UUID_RE.match(dir_name):
                    continue

                mtime = _get_mtime(uuid_dir)
                age_sec = now - mtime
                if age_sec <= threshold_sec:
                    continue

                if _is_blocked(dir_name, blocklist):
                    skipped_blocked += 1
                    if json_mode:
                        size_bytes = _dir_size_bytes(uuid_dir)
                        emit_jsonl("harness", str(uuid_dir), dir_name, size_bytes, mtime,
                                   "skip", "predecessor uuid in active handoff", emit_fn=emit_fn)
                    continue

                size_bytes = _dir_size_bytes(uuid_dir)

                # BEHAVIOUR CHANGE (2026-07-22): only count/log as pruned once
                # the delete is confirmed — a failed rm no longer inflates the
                # reported reclaimed bytes/items.
                if apply:
                    if not _delete_path(uuid_dir):
                        _banner(f"[cruft-sweep] WARNING: delete failed, not counted as pruned: {uuid_dir}", quiet=quiet)
                        if json_mode:
                            emit_jsonl("harness", str(uuid_dir), dir_name, size_bytes, mtime,
                                       "prune-failed", f"projects dir mtime {age_sec}s > threshold {threshold_sec}s; delete did not confirm removal",
                                       emit_fn=emit_fn)
                        continue

                total_bytes += size_bytes
                pruned_dirs += 1

                if json_mode:
                    emit_jsonl("harness", str(uuid_dir), dir_name, size_bytes, mtime,
                               "auto-prune", f"projects dir mtime {age_sec}s > threshold {threshold_sec}s",
                               emit_fn=emit_fn)

            if not wd_uuid_bail:
                for jsonl_file in sorted(repo_dir.glob("*.jsonl")):
                    file_name = jsonl_file.stem
                    if not _UUID_RE.match(file_name):
                        continue

                    mtime = _get_mtime(jsonl_file)
                    age_sec = now - mtime
                    if age_sec <= threshold_sec:
                        continue

                    if _is_blocked(file_name, blocklist):
                        skipped_blocked += 1
                        if json_mode:
                            fsize = _file_size(jsonl_file)
                            emit_jsonl("harness", str(jsonl_file), f"{file_name}.jsonl", fsize, mtime,
                                       "skip", "predecessor uuid in active handoff", emit_fn=emit_fn)
                        continue

                    fsize = _file_size(jsonl_file)

                    if apply:
                        if not _delete_file(jsonl_file):
                            _banner(f"[cruft-sweep] WARNING: delete failed, not counted as pruned: {jsonl_file}", quiet=quiet)
                            if json_mode:
                                emit_jsonl("harness", str(jsonl_file), f"{file_name}.jsonl", fsize, mtime,
                                           "prune-failed", f"transcript mtime {age_sec}s > threshold {threshold_sec}s; delete did not confirm removal",
                                           emit_fn=emit_fn)
                            continue

                    total_bytes += fsize
                    pruned_jsonl += 1

                    if json_mode:
                        emit_jsonl("harness", str(jsonl_file), f"{file_name}.jsonl", fsize, mtime,
                                   "auto-prune", f"transcript mtime {age_sec}s > threshold {threshold_sec}s",
                                   emit_fn=emit_fn)

    wd2 = _Watchdog(watchdog_ceiling_secs)
    wd_fh_cnt = 0
    if file_history_root.is_dir():
        for fh_dir in sorted(p for p in file_history_root.iterdir() if p.is_dir()):
            if not wd2.check():
                _banner(
                    f"[cruft-sweep] wall-clock ceiling bail on harness fh-dir sweep "
                    f"after {wd_fh_cnt} candidates examined; will finish next run",
                    quiet=quiet,
                )
                break
            wd_fh_cnt += 1

            dir_name = fh_dir.name
            if not _UUID_RE.match(dir_name):
                continue

            mtime = _get_mtime(fh_dir)
            age_sec = now - mtime
            if age_sec <= threshold_sec:
                continue

            if _is_blocked(dir_name, blocklist):
                skipped_blocked += 1
                if json_mode:
                    size_bytes = _dir_size_bytes(fh_dir)
                    emit_jsonl("harness", str(fh_dir), dir_name, size_bytes, mtime,
                               "skip", "predecessor uuid in active handoff", emit_fn=emit_fn)
                continue

            size_bytes = _dir_size_bytes(fh_dir)

            if apply:
                if not _delete_path(fh_dir):
                    _banner(f"[cruft-sweep] WARNING: delete failed, not counted as pruned: {fh_dir}", quiet=quiet)
                    if json_mode:
                        emit_jsonl("harness", str(fh_dir), dir_name, size_bytes, mtime,
                                   "prune-failed", f"file-history dir mtime {age_sec}s > threshold {threshold_sec}s; delete did not confirm removal",
                                   emit_fn=emit_fn)
                    continue

            total_bytes += size_bytes
            pruned_fh_dirs += 1

            if json_mode:
                emit_jsonl("harness", str(fh_dir), dir_name, size_bytes, mtime,
                           "auto-prune", f"file-history dir mtime {age_sec}s > threshold {threshold_sec}s",
                           emit_fn=emit_fn)

    total_items = pruned_dirs + pruned_jsonl + pruned_fh_dirs
    total_mb = total_bytes // 1048576

    if not json_mode and not quiet:
        mode_label = "APPLY" if apply else "DRY-RUN"
        skip_suffix = f", {skipped_blocked} skipped (active handoff)" if skipped_blocked else ""
        _banner(
            f"[cruft-sweep] harness ({mode_label}, >{days}d): {total_items} items "
            f"({pruned_dirs} dirs + {pruned_jsonl} jsonl + {pruned_fh_dirs} fh-dirs), "
            f"~{total_mb} MB reclaimable{skip_suffix}",
            quiet=False,
        )

    if apply and total_items > 0:
        _append_log_row(log_path, "harness", total_bytes, total_items)

    return total_bytes, total_items


# ---------------------------------------------------------------------------
# Phase B: in-repo scratch sweep
# ---------------------------------------------------------------------------


def _resolve_repo_root(repo_root: Optional[Path]) -> Path:
    if repo_root is not None:
        return repo_root
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
            **no_console_creationflags(),
        )
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip())
        if r.returncode != 0:
            print(
                f"WARNING: _resolve_repo_root: git rev-parse exited {r.returncode}, "
                f"falling back to cwd: {(r.stderr or '').strip()}",
                file=sys.stderr,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"WARNING: _resolve_repo_root: git rev-parse failed, falling back to cwd: {exc}", file=sys.stderr)
    return Path.cwd()


def sweep_scratch(
    repo_root: Path,
    scratch_age_days: int,
    *,
    apply: bool,
    json_mode: bool,
    quiet: bool,
    log_path: Optional[Path] = None,
    watchdog_ceiling_secs: Optional[float] = None,
    emit_fn: Optional[EmitFn] = None,
) -> Tuple[int, int]:
    """Sweep name-anchored scratch dirs under repo_root. Returns (bytes, items)."""
    repo_root = _resolve_repo_root(repo_root)
    now = int(time.time())
    threshold_sec = scratch_age_days * 86400

    total_bytes = 0
    pruned_items = 0
    confirm_needed_count = 0

    # NOTE: uses os.walk(onerror=...), NOT a bare os.walk() -- a bare
    # os.walk() with no onerror= silently declines to descend into an
    # unreadable subtree (no exception, no signal), which is the identical
    # silent-skip idiom build_uuid_blocklist's iterdir() conversion and
    # time_transform.py's discover_files()/discover_persona_slug_leak_files()
    # both eliminated in this same session. A scratch dir sitting inside an
    # unreadable subtree here would otherwise be silently never discovered,
    # never pruned, and never reported as unscanned.
    # Review: code-reviewer -- Finding 1 (2026-07-22 slice1 review): this walk
    # was the one unfixed sibling of the defect this commit exists to patch.
    all_dirs: List[Path] = []
    walk_errors: List[str] = []

    def _onerror(exc: OSError) -> None:
        walk_errors.append(f"{getattr(exc, 'filename', repo_root)}: {exc}")

    for root, dirs, _files in os.walk(repo_root, onerror=_onerror):
        for d in dirs:
            all_dirs.append(Path(root) / d)
    all_dirs.sort(key=lambda p: str(p))

    if walk_errors:
        # --- Tier 2 (behaviour change -- PM sign-off required) ---
        print(
            f"[cruft-sweep] WARNING: {len(walk_errors)} subtree(s) under {repo_root} "
            "could not be walked and were NOT scanned for scratch dirs (unreadable "
            "subtree -- pruning is incomplete this run): " + "; ".join(walk_errors),
            file=sys.stderr,
        )
        # --- end Tier 2 ---

    pruned_parents: List[str] = []
    wd = _Watchdog(watchdog_ceiling_secs)
    wd_cnt = 0

    # One batched `git ls-files` pass over every auto-prune-named candidate,
    # instead of one `git ls-files --error-unmatch` spawn per directory once
    # the loop below reaches its tracked-by-git gate. Over-inclusive by
    # design (a dir later skipped by an earlier gate, or dropped by the
    # watchdog, still has an entry here) — that costs nothing beyond a dict
    # lookup, and keeps this a single query shape independent of loop state.
    untracked_by_dir = _batch_is_untracked_dirs(
        repo_root, [d for d in all_dirs if _is_auto_prune_name(d.name)]
    )

    for dir_path in all_dirs:
        if not wd.check():
            _banner(
                f"[cruft-sweep] wall-clock ceiling bail on scratch sweep after "
                f"{wd_cnt} candidates examined; will finish next run",
                quiet=quiet,
            )
            break
        wd_cnt += 1

        dir_str = str(dir_path)
        if _is_pruned_child(dir_str, pruned_parents):
            continue

        dir_name = dir_path.name

        if _has_git_boundary(dir_str):
            continue
        if _has_negative_spec_component(dir_str):
            continue

        if _is_backup_name(dir_name):
            if _is_auto_prune_name(dir_name) or _is_confirm_needed_name(dir_name):
                mtime = _get_mtime(dir_path)
                size_bytes = _dir_size_bytes(dir_path)
                if json_mode:
                    emit_jsonl("scratch", dir_str, dir_name, size_bytes, mtime,
                               "skip", "legitimate-backup name class", emit_fn=emit_fn)
            continue

        if _is_confirm_needed_name(dir_name):
            mtime = _get_mtime(dir_path)
            size_bytes = _dir_size_bytes(dir_path)
            if json_mode:
                emit_jsonl("scratch", dir_str, dir_name, size_bytes, mtime,
                           "confirm-needed", "name in confirm-list — Layer 2 owns", emit_fn=emit_fn)
            confirm_needed_count += 1
            continue

        if _is_auto_prune_name(dir_name):
            mtime = _get_mtime(dir_path)

            if mtime == 0:
                _banner(f"[cruft-sweep] mtime-resolution-failed skip: {dir_str}", quiet=quiet)
                continue

            age_sec = now - mtime
            effective_threshold = max(threshold_sec, _MTIME_FLOOR_SECS)

            if age_sec <= effective_threshold:
                if json_mode:
                    size_bytes = _dir_size_bytes(dir_path)
                    if age_sec <= _MTIME_FLOOR_SECS:
                        emit_jsonl("scratch", dir_str, dir_name, size_bytes, mtime,
                                   "skip", f"mtime {age_sec}s <= 86400s (mtime-floor)", emit_fn=emit_fn)
                    else:
                        emit_jsonl("scratch", dir_str, dir_name, size_bytes, mtime,
                                   "skip", f"mtime {age_sec}s <= threshold {threshold_sec}s (age-threshold)",
                                   emit_fn=emit_fn)
                continue

            if not untracked_by_dir.get(dir_str, True):
                if json_mode:
                    size_bytes = _dir_size_bytes(dir_path)
                    emit_jsonl("scratch", dir_str, dir_name, size_bytes, mtime,
                               "skip", "tracked by git", emit_fn=emit_fn)
                continue

            size_bytes = _dir_size_bytes(dir_path)

            if apply:
                if not _delete_path(dir_path):
                    _banner(f"[cruft-sweep] WARNING: delete failed, not counted as pruned: {dir_str}", quiet=quiet)
                    if json_mode:
                        emit_jsonl("scratch", dir_str, dir_name, size_bytes, mtime,
                                   "prune-failed",
                                   f"name in auto-prune list; mtime {age_sec}s > effective_threshold {effective_threshold}s; delete did not confirm removal",
                                   emit_fn=emit_fn)
                    continue

            total_bytes += size_bytes
            pruned_items += 1
            pruned_parents.append(dir_str)

            if json_mode:
                emit_jsonl("scratch", dir_str, dir_name, size_bytes, mtime,
                           "auto-prune",
                           f"name in auto-prune list; mtime {age_sec}s > effective_threshold {effective_threshold}s",
                           emit_fn=emit_fn)

    total_mb = total_bytes // 1048576
    if not json_mode and not quiet:
        mode_label = "APPLY" if apply else "DRY-RUN"
        _banner(
            f"[cruft-sweep] scratch ({mode_label}, >{scratch_age_days}d): {pruned_items} items "
            f"auto-pruned, {confirm_needed_count} confirm-needed, ~{total_mb} MB reclaimable",
            quiet=False,
        )
        if confirm_needed_count > 0:
            _banner(
                f"[cruft-sweep] scratch: {confirm_needed_count} confirm-needed item(s) "
                f"require Layer 2 review (run with --class scratch --json to enumerate)",
                quiet=False,
            )

    if apply and pruned_items > 0:
        _append_log_row(log_path, "scratch", total_bytes, pruned_items)

    return total_bytes, pruned_items


# ---------------------------------------------------------------------------
# File-level reap: scratch/subagent-sandbox/*.md
# ---------------------------------------------------------------------------


def sweep_subagent_sandbox_files(
    repo_root: Path,
    *,
    apply: bool,
    json_mode: bool,
    quiet: bool,
    log_path: Optional[Path] = None,
    emit_fn: Optional[EmitFn] = None,
) -> Tuple[int, int]:
    """Reap stale scratch/subagent-sandbox/*.md files (24h hard mtime floor,
    no configurable threshold). NEVER prunes the sandbox dir itself."""
    repo_root = _resolve_repo_root(repo_root)
    sandbox_dir = repo_root / "scratch" / "subagent-sandbox"

    if not sandbox_dir.is_dir():
        return 0, 0

    now = int(time.time())
    total_bytes = 0
    reaped_items = 0

    for file_path in sorted(sandbox_dir.glob("*.md")):
        if not file_path.is_file():
            continue
        file_name = file_path.name
        mtime = _get_mtime(file_path)

        if mtime == 0:
            _banner(f"[cruft-sweep] mtime-resolution-failed skip: {file_path}", quiet=quiet)
            continue

        age_sec = now - mtime
        if age_sec <= _MTIME_FLOOR_SECS:
            if json_mode:
                size_bytes = _file_size(file_path)
                emit_jsonl("scratch", str(file_path), file_name, size_bytes, mtime,
                           "skip", f"mtime {age_sec}s <= 86400s (mtime-floor)", emit_fn=emit_fn)
            continue

        size_bytes = _file_size(file_path)

        if apply:
            # subprocess rm w/ 60s cap, matches oracle's cs_timeout rm -f
            if not _delete_path(file_path):
                _banner(f"[cruft-sweep] WARNING: delete failed, not counted as pruned: {file_path}", quiet=quiet)
                if json_mode:
                    emit_jsonl("scratch", str(file_path), file_name, size_bytes, mtime,
                               "prune-failed",
                               f"mtime {age_sec}s > 86400s (mtime-floor); subagent-sandbox file-level reap; delete did not confirm removal",
                               emit_fn=emit_fn)
                continue

        total_bytes += size_bytes
        reaped_items += 1

        if json_mode:
            emit_jsonl("scratch", str(file_path), file_name, size_bytes, mtime,
                       "auto-prune",
                       f"mtime {age_sec}s > 86400s (mtime-floor); subagent-sandbox file-level reap",
                       emit_fn=emit_fn)

    if not json_mode and not quiet:
        mode_label = "APPLY" if apply else "DRY-RUN"
        total_mb = total_bytes // 1048576
        _banner(
            f"[cruft-sweep] subagent-sandbox ({mode_label}, >24h): {reaped_items} file(s) "
            f"reaped, ~{total_mb} MB reclaimable",
            quiet=False,
        )

    if apply and reaped_items > 0:
        _append_log_row(log_path, "subagent-sandbox", total_bytes, reaped_items)

    return total_bytes, reaped_items


# ---------------------------------------------------------------------------
# Phase C: parent-altitude orphan sweep
# ---------------------------------------------------------------------------


def sweep_orphans(
    parent_roots: Sequence[Path],
    whitelist: Iterable[str],
    *,
    apply: bool,
    json_mode: bool,
    quiet: bool,
    log_path: Optional[Path] = None,
    watchdog_ceiling_secs: Optional[float] = None,
    settings_home: Optional[Path] = None,
    emit_fn: Optional[EmitFn] = None,
) -> Tuple[int, int]:
    """Sweep top-level children of each parent root for orphaned sonnet-default
    artifacts (name + fingerprint match). `settings_home`, if given, activates
    the C3 forward-guard hard-exclude of the install-baton rendezvous folder
    (`<settings_home>/state/handoffs` and `<settings_home>/state`)."""
    whitelist_set = set(whitelist)
    total_bytes = 0
    pruned_items = 0
    skipped_name_match = 0

    rendezvous_dir = None
    settings_state_dir = None
    if settings_home is not None:
        sh = str(settings_home).rstrip("/")
        rendezvous_dir = f"{sh}/state/handoffs"
        settings_state_dir = f"{sh}/state"

    wd = _Watchdog(watchdog_ceiling_secs)
    wd_bail = False
    wd_cnt = 0

    for root in parent_roots:
        if wd_bail:
            continue
        root = Path(root)
        if not root.is_dir():
            continue

        for child_path in sorted(p for p in root.iterdir() if p.is_dir()):
            if not wd.check():
                _banner(
                    f"[cruft-sweep] wall-clock ceiling bail on orphan sweep after "
                    f"{wd_cnt} candidates examined; will finish next run",
                    quiet=quiet,
                )
                wd_bail = True
                break
            wd_cnt += 1

            child_name = child_path.name

            if child_name in _ORPHAN_HARD_EXCLUDE_NAMES:
                continue

            child_norm = str(child_path).rstrip("/")
            if rendezvous_dir is not None and child_norm in (rendezvous_dir, settings_state_dir):
                continue

            if child_name in whitelist_set:
                continue

            if (child_path / "CLAUDE.md").is_file() or (child_path / "CLAUDE.local.md").is_file():
                continue

            if not _is_orphan_name_match(child_name):
                continue

            size_bytes = _dir_size_bytes(child_path)
            mtime = _get_mtime(child_path)

            if not _has_sonnet_fingerprint(child_path):
                skipped_name_match += 1
                if json_mode:
                    emit_jsonl("orphans", str(child_path), child_name, size_bytes, mtime,
                               "skip", "name matched but no sonnet-fingerprint contents — Layer 2 broader scan owns",
                               emit_fn=emit_fn)
                continue

            if apply:
                if not _delete_path(child_path):
                    _banner(f"[cruft-sweep] WARNING: delete failed, not counted as pruned: {child_path}", quiet=quiet)
                    if json_mode:
                        emit_jsonl("orphans", str(child_path), child_name, size_bytes, mtime,
                                   "prune-failed", "name in orphan cruft list; sonnet-fingerprint contents confirmed; delete did not confirm removal",
                                   emit_fn=emit_fn)
                    continue

            total_bytes += size_bytes
            pruned_items += 1

            if json_mode:
                emit_jsonl("orphans", str(child_path), child_name, size_bytes, mtime,
                           "auto-prune", "name in orphan cruft list; sonnet-fingerprint contents confirmed",
                           emit_fn=emit_fn)

    total_mb = total_bytes // 1048576
    if not json_mode and not quiet:
        mode_label = "APPLY" if apply else "DRY-RUN"
        _banner(
            f"[cruft-sweep] orphans ({mode_label}): {pruned_items} items auto-pruned, "
            f"{skipped_name_match} name-matched-no-fingerprint, ~{total_mb} MB reclaimable",
            quiet=False,
        )

    if apply and pruned_items > 0:
        _append_log_row(log_path, "orphans", total_bytes, pruned_items)

    return total_bytes, pruned_items


# ---------------------------------------------------------------------------
# Phase E (net-new, no bash-oracle counterpart — see module docstring):
# top-level empty-directory sweep.
# ---------------------------------------------------------------------------


def sweep_empty_toplevel_dirs(
    repo_root: Optional[Path],
    whitelist: Iterable[str] = (),
    *,
    apply: bool,
    json_mode: bool,
    quiet: bool,
    log_path: Optional[Path] = None,
    watchdog_ceiling_secs: Optional[float] = None,
    emit_fn: Optional[EmitFn] = None,
) -> Tuple[int, int]:
    """Sweep top-level (depth-1) children of `repo_root` that are
    directories containing zero files anywhere in their subtree, older than
    the `_MTIME_FLOOR_SECS` (24h) floor, not git-ignored, and not
    hard-excluded/whitelisted/dot-prefixed. Returns (total_bytes,
    total_items) — total_bytes is always 0 by construction (an empty
    subtree has no bytes to reclaim; the item count is the signal here).

    Fails closed: if `repo_root` is not inside a git work tree, or `git`
    itself is unavailable, this phase skips entirely (no deletions, a
    non-fatal banner) rather than proceeding without git as a safety
    backstop for the check-ignore gate.

    NET-NEW — see module docstring's "Net-new phase" section. No bash-oracle
    counterpart; excluded from any byte-parity/golden-diff harness.
    """
    repo_root = _resolve_repo_root(repo_root)
    whitelist_set = set(whitelist)
    total_bytes = 0
    pruned_items = 0

    if not _git_available():
        _banner(
            "[cruft-sweep] empty-dirs: git not available — skipping phase "
            "(fail-closed, no deletions)",
            quiet=quiet,
        )
        return 0, 0

    if not _is_inside_git_work_tree(repo_root):
        _banner(
            f"[cruft-sweep] empty-dirs: {repo_root} is not inside a git work "
            "tree — skipping phase (fail-closed, no deletions)",
            quiet=quiet,
        )
        return 0, 0

    try:
        children = sorted(
            p for p in repo_root.iterdir() if p.is_dir() and not p.is_symlink()
        )
    except OSError as exc:
        _banner(f"[cruft-sweep] empty-dirs: cannot list {repo_root}: {exc}", quiet=quiet)
        return 0, 0

    now = int(time.time())
    wd = _Watchdog(watchdog_ceiling_secs)
    wd_cnt = 0

    # One batched `git check-ignore --stdin` pass over every candidate name
    # not already excluded by the cheap in-memory gates above, instead of
    # one `check-ignore -q -- <name>` spawn per top-level child inside the
    # loop below. Over-inclusive is harmless here too — a name later
    # dropped by the watchdog still has an entry, costing only a set lookup.
    ignore_candidate_names = [
        c.name for c in children
        if not c.name.startswith(".")
        and c.name not in _ORPHAN_HARD_EXCLUDE_NAMES
        and c.name not in whitelist_set
    ]
    ignored_names = _batch_git_ignored_names(repo_root, ignore_candidate_names)

    for child in children:
        if not wd.check():
            _banner(
                f"[cruft-sweep] wall-clock ceiling bail on empty-dirs sweep "
                f"after {wd_cnt} candidates examined; will finish next run",
                quiet=quiet,
            )
            break
        wd_cnt += 1

        name = child.name

        if name.startswith("."):
            continue
        if name in _ORPHAN_HARD_EXCLUDE_NAMES or name in whitelist_set:
            continue
        if name in ignored_names:
            continue

        is_empty, max_mtime = _scan_empty_subtree(child)
        if not is_empty:
            continue

        age_sec = now - max_mtime
        if age_sec <= _MTIME_FLOOR_SECS:
            if json_mode:
                emit_jsonl("empty-dirs", str(child), name, 0, max_mtime,
                           "skip", f"mtime {age_sec}s <= 86400s (mtime-floor)",
                           emit_fn=emit_fn)
            continue

        if apply:
            if not _delete_path(child):
                _banner(f"[cruft-sweep] WARNING: delete failed, not counted as pruned: {child}", quiet=quiet)
                if json_mode:
                    emit_jsonl("empty-dirs", str(child), name, 0, max_mtime,
                               "prune-failed",
                               f"top-level dir, zero files recursively, mtime {age_sec}s > 86400s (mtime-floor); delete did not confirm removal",
                               emit_fn=emit_fn)
                continue

        pruned_items += 1

        if json_mode:
            # Drift-audit D10 (docs/research/2026-07-28-cruft-sweep-duplicate-port-
            # drift-audit.md): a name also in Phase B's own auto-prune vocabulary
            # (_is_auto_prune_name) is Phase B's to report — relabel rather than
            # emit an independent "auto-prune" so a JSON/dry-run consumer summing
            # auto-prune records per class does not double-count one physical
            # directory under both "scratch" and "empty-dirs". Apply-mode delete
            # behavior is UNCHANGED by this relabel (this phase still deletes the
            # directory above exactly as before) — this only affects which
            # disposition string a dry-run/--json record carries.
            if _is_auto_prune_name(name):
                emit_jsonl("empty-dirs", str(child), name, 0, max_mtime,
                           "duplicate-of-scratch",
                           f"top-level dir, zero files recursively, mtime {age_sec}s > "
                           "86400s (mtime-floor); name is also in Phase B's scratch "
                           "auto-prune-name set — see scratch class for the authoritative record",
                           emit_fn=emit_fn)
            else:
                emit_jsonl("empty-dirs", str(child), name, 0, max_mtime,
                           "auto-prune",
                           f"top-level dir, zero files recursively, mtime {age_sec}s > 86400s (mtime-floor)",
                           emit_fn=emit_fn)

    if not json_mode and not quiet:
        mode_label = "APPLY" if apply else "DRY-RUN"
        _banner(
            f"[cruft-sweep] empty-dirs ({mode_label}): {pruned_items} items "
            "auto-pruned (zero-file top-level dirs, >24h)",
            quiet=False,
        )

    if apply and pruned_items > 0:
        _append_log_row(log_path, "empty-dirs", total_bytes, pruned_items)

    return total_bytes, pruned_items


# ---------------------------------------------------------------------------
# Registered op — convenience JSON-RPC façade over the four phase functions.
# Primary call path remains the example-doctrine-repo trampoline's in-process import (recipe §
# example-doctrine-repo-side work item 5); this registration exists for op-registry parity /
# any future JSON-RPC caller, per the T3a-g1a build brief's "NEW op" framing.
# ---------------------------------------------------------------------------


def _run_all_phases(
    class_: str,
    apply: bool,
    json_mode: bool,
    quiet: bool,
    days: int,
    scratch_age_days: int,
    projects_root: Path,
    file_history_root: Path,
    handoffs_dir: Optional[Path],
    log_path: Optional[Path],
    repo_root_override: Optional[Path],
    parent_roots: List[Path],
    whitelist: Iterable[str],
    settings_home: Optional[Path],
    emit_fn: Optional[EmitFn],
) -> dict:
    """Run every requested sweep phase (blocking) and return the totals dict.

    Module-level (not a nested closure of `_run_handler`) precisely so
    `_run_handler` invokes it through exactly one `asyncio.to_thread` hop
    (see call site) — a nested `def` here would still be visible to the
    async-handler-discipline gate's one-level indirection scan, which walks
    an `AsyncFunctionDef`'s full AST subtree (nested function bodies
    included) regardless of a later, separate `asyncio.to_thread` wrapping.
    """
    harness_bytes = harness_items = 0
    scratch_bytes = scratch_items = 0
    sandbox_bytes = sandbox_items = 0
    orphans_bytes = orphans_items = 0

    if class_ in ("harness", "all"):
        blocklist: set = set()
        if handoffs_dir:
            blocklist, blocklist_complete = build_uuid_blocklist(handoffs_dir)
            # --- Tier 2 (behaviour change -- PM sign-off required) ---
            # BEHAVIOUR CHANGE (2026-07-22, break-class fix): fail closed
            # rather than silently sweeping with a narrowed protected set.
            if not blocklist_complete and apply:
                raise BlocklistIncompleteError(
                    f"UUID blocklist scan of {handoffs_dir} was incomplete "
                    "(unreadable handoff file(s) — see stderr warnings); "
                    "aborting harness --apply rather than deleting state "
                    "on an incomplete protected-UUID set"
                )
            # --- end Tier 2 ---
        harness_bytes, harness_items = sweep_harness(
            projects_root, file_history_root, days, blocklist,
            apply=apply, json_mode=json_mode, quiet=quiet,
            log_path=log_path, emit_fn=emit_fn,
        )
    if class_ in ("scratch", "all"):
        scratch_bytes, scratch_items = sweep_scratch(
            repo_root_override, scratch_age_days,
            apply=apply, json_mode=json_mode, quiet=quiet,
            log_path=log_path, emit_fn=emit_fn,
        )
        sandbox_bytes, sandbox_items = sweep_subagent_sandbox_files(
            repo_root_override,
            apply=apply, json_mode=json_mode, quiet=quiet,
            log_path=log_path, emit_fn=emit_fn,
        )
    if class_ in ("orphans", "all"):
        orphans_bytes, orphans_items = sweep_orphans(
            parent_roots, whitelist,
            apply=apply, json_mode=json_mode, quiet=quiet,
            log_path=log_path, settings_home=settings_home, emit_fn=emit_fn,
        )
    empty_dirs_bytes = empty_dirs_items = 0
    if class_ in ("empty-dirs", "all"):
        # NET-NEW class (see module docstring) — no bash-oracle
        # counterpart, so unlike SUBAGENT_SANDBOX above there is no
        # oracle-omission precedent to faithfully reproduce: folded into
        # the grand total by this façade's own design, not the oracle's.
        empty_dirs_bytes, empty_dirs_items = sweep_empty_toplevel_dirs(
            repo_root_override, whitelist,
            apply=apply, json_mode=json_mode, quiet=quiet,
            log_path=log_path, emit_fn=emit_fn,
        )

    return {
        "harness": {"bytes": harness_bytes, "items": harness_items},
        "scratch": {"bytes": scratch_bytes + sandbox_bytes, "items": scratch_items + sandbox_items},
        "orphans": {"bytes": orphans_bytes, "items": orphans_items},
        "empty_dirs": {"bytes": empty_dirs_bytes, "items": empty_dirs_items},
        # Grand-total components, kept separate from the display-oriented
        # "scratch" bucket above. cruft-sweep.sh's grand-total banner
        # (lines 1525-1526) is HARNESS + SCRATCH + ORPHANS only — it never
        # folds SUBAGENT_SANDBOX_BYTES/ITEMS in, even though the sandbox
        # reap runs alongside every scratch/all invocation and gets its
        # own separate log row. Review: code-reviewer (ops-records-cruft-
        # hierarchy F4) — total_bytes/total_items previously derived from
        # the sandbox-folded "scratch" bucket, diverging from the oracle's
        # 1 GB reclaimable-space advisory threshold. EMPTY_DIRS is folded
        # into this facade's grand total (its own net-new design choice,
        # not an oracle mirror).
        "_grand_total_bytes": harness_bytes + scratch_bytes + orphans_bytes + empty_dirs_bytes,
        "_grand_total_items": harness_items + scratch_items + orphans_items + empty_dirs_items,
    }


@register_op("cruft_sweep.run")
async def _run_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC cruft_sweep.run handler — MUTATING when apply=True.

    Dispatches ONE class ("harness" | "scratch" | "orphans" | "empty-dirs" |
    "all") against fully-resolved params. Every path/threshold/list value
    must already be resolved by the caller — this handler does NOT touch
    machine-local, coordinator_state_root, or any registry. "empty-dirs" is
    the net-new phase (see module docstring) — reuses `repo_root_override`
    and `whitelist`, no dedicated params of its own.

    Params:
        class_ (str, default "all")
        apply (bool, default False)
        json_mode (bool, default False) — when True, emitted JSONL records are
            collected into the returned "records" list instead of printed.
        quiet (bool, default False)
        days (int, default 14) — harness retention threshold
        scratch_age_days (int, default 7)
        projects_root, file_history_root, handoffs_dir, log_path,
        repo_root_override — str paths (all optional; sensible defaults below)
        parent_roots (list[str], default [])
        whitelist (list[str], default [])
        settings_home (str, optional)
    """
    class_ = params.get("class_", params.get("class", "all"))
    apply = bool(params.get("apply", False))
    json_mode = bool(params.get("json_mode", False))
    quiet = bool(params.get("quiet", False))
    days = int(params.get("days", 14))
    scratch_age_days = int(params.get("scratch_age_days", 7))

    home = Path.home()
    projects_root = Path(params.get("projects_root", str(home / ".claude" / "projects")))
    file_history_root = Path(params.get("file_history_root", str(home / ".claude" / "file-history")))
    handoffs_dir = Path(params["handoffs_dir"]) if params.get("handoffs_dir") else None
    log_path = Path(params["log_path"]) if params.get("log_path") else None
    repo_root_override = Path(params["repo_root_override"]) if params.get("repo_root_override") else repo_root
    parent_roots = [Path(p) for p in params.get("parent_roots", [])]
    whitelist = params.get("whitelist", [])
    settings_home = Path(params["settings_home"]) if params.get("settings_home") else None
    # Exclusive lock — mirrors cruft-sweep.sh's `mkdir "$LOCK_DIR"` + EXIT-trap
    # rmdir wrapping the ENTIRE invocation (lines 279-285 of the bash oracle).
    # This façade is a legitimate alternate call path into the same mutating
    # phase functions the example-doctrine-repo trampoline drives, so it must hold the same
    # single-instance-serialization guarantee, not just the trampoline.
    # Review: code-reviewer (ops-records-cruft-hierarchy F3) — try_acquire_lock/
    # release_lock were defined but never called anywhere in this module.
    lock_dir = Path(
        params.get("lock_dir", str(Path.home() / ".claude" / "state" / "cruft-sweep.lock.d"))
    )

    records: List[dict] = []
    emit_fn = (lambda rec: records.append(rec)) if json_mode else None

    if not try_acquire_lock(lock_dir):
        # Contention — another run owns the work; exit "0 silently" (bash
        # oracle semantics) rather than raising or blocking.
        empty_totals = {
            "harness": {"bytes": 0, "items": 0},
            "scratch": {"bytes": 0, "items": 0},
            "orphans": {"bytes": 0, "items": 0},
            "empty_dirs": {"bytes": 0, "items": 0},
        }
        empty_result = {"totals": empty_totals, "total_bytes": 0, "total_items": 0}
        if json_mode:
            empty_result["records"] = []
        return empty_result

    try:
        # AC-3 (async-handler-discipline): `_run_all_phases` is a module-level
        # (not nested) plain `def` so the whole phase dispatch — including its
        # own bare `sweep_scratch(...)` os.walk-reaching call — is reached ONLY
        # through this single `asyncio.to_thread` hop, never via a closure the
        # gate's one-level indirection scan would otherwise walk into as if it
        # were still directly in `_run_handler`'s body.
        totals = await asyncio.to_thread(
            _run_all_phases,
            class_, apply, json_mode, quiet, days, scratch_age_days,
            projects_root, file_history_root, handoffs_dir, log_path,
            repo_root_override, parent_roots, whitelist, settings_home,
            emit_fn,
        )
    finally:
        release_lock(lock_dir)

    total_bytes = totals.pop("_grand_total_bytes")
    total_items = totals.pop("_grand_total_items")

    result = {
        "totals": totals,
        "total_bytes": total_bytes,
        "total_items": total_items,
    }
    if json_mode:
        result["records"] = records
    return result
