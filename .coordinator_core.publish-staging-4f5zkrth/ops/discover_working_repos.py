"""
coordinator_core.ops.discover_working_repos — Port of:
discover-working-repos.sh (DoE 6fb5fb37, 2026-07-22, DOE-PORT variant #1 —
direct-import trampoline, no registered op).

Purpose: three-tier working-repo discovery for `/setup` Phase 2 Step 4. Prints
discovered repo paths, one per line. Empty stdout means no repos discovered —
the caller (setup skill) falls through to Tier C (operator prompt,
interactive, NOT implemented here).

Tier A (preferred):  ~/.claude/projects/ activity record.
Tier A.5 (registry): machine-local registry `repos.*` enumeration.
Tier B (fallback):   common dev-folder layouts.
Tier C:              caller-handled interactive prompt (NOT in this module).

Stops at first non-empty tier (A takes priority over B); Tier A.5 always runs
alongside whichever of A/B fires, to close registry-only gaps. Filters
meta-repo / AppData-Local-Temp / bare drive roots. Never-block contract:
every path through `main()` returns 0 — this is a best-effort discovery
helper, not a gate; an unresolvable environment (no git, no machine-local, no
matches) degrades to silent empty stdout, exactly like the bash oracle.

Known oracle gap (faithfully preserved, NOT fixed in this port — see
`_decode_projects_dir_name`): the projects-dir basename decode always
backslash-joins hyphen-split tokens, even for non-drive-letter (POSIX-form)
entries — i.e. on macOS/Linux, where Claude Code activity-record directory
names look like `-Users-oduffy-X-DoE-claude`, Tier A's naive decode produces a
backslash path that will not exist on disk, and there is no greedy-decode
fallback for that shape (the oracle's own comment calls this "out of scope").
Tier A is effectively Windows-only in practice; Tier A.5 and Tier B are the
functioning discovery paths on POSIX. Do not silently "fix" this — it is a
faithful repro of the pre-port oracle's documented gap.

Output contract (two deliberate departures from the oracle, 2026-08-14):

  1. Every emitted path uses forward slashes. The oracle preserved each repo's
     first-seen native form, so a single Windows run mixed `X:/DelphiOS`
     (registry-sourced, Tier A.5) with `X:\\DoE-claude` (filesystem-discovered,
     Tier A) on adjacent lines. The consumer writes those verbatim into
     double-quoted YAML scalars in `~/.claude/working-repos.yaml`, where
     `\\D` is an invalid escape — `yaml.safe_load` raised ScannerError and
     every downstream consumer of that file crashed. Normalizing here fixes it
     for every consumer at once rather than per-writer. Negative-spec: do NOT
     restore native-separator emission for oracle parity.

  2. Publish mirrors are never emitted. A mirror registered under the
     machine-local `publish.mirrors.*.path` namespace is a publish target, not
     a working tree — doctrine forbids working in one or addressing a memo to
     one, so enumerating it as a discovered working repo invites both.

"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Sequence

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.machine_resolver import (
    merged_flat_registry as _merged_flat_registry,
    registry_value as _registry_value,
)
from coordinator_core.win_portability import no_console_creationflags


_CREATIONFLAGS = no_console_creationflags()


_MSYS_DRIVE_RE = re.compile(r"^/([A-Za-z])(/.*)?$")

#: Named so `_emit_form` reads without an escape-in-an-escape.
BACKSLASH = chr(92)


def _fs_probe_path(p: str) -> str:
    """Convert a path to a form the *running interpreter* can actually stat.

    Every existence probe in this module is built in MSYS/POSIX form
    ("/x/foo", "/c/Users/..."). That form is valid under Git Bash — the
    environment the bash oracle ran in — but native Windows Python cannot
    stat it: `os.path.isdir('/c/Users/<username>')` is False while
    `os.path.isdir('C:/Users/<username>')` is True (forward slashes are fine;
    the `/c/` drive-letter-as-directory form is not). On Windows, convert an
    MSYS drive path ("/x/foo" or bare "/x") back to a form Python can stat
    ("x:/foo" / "x:/") and leave an already-native path ("X:\\foo" or
    "X:/foo") alone — it won't match the MSYS pattern, so it round-trips as
    identity. On non-Windows hosts this is a pure identity function — POSIX
    paths are POSIX paths there, so today's behavior is unchanged.

    Spec backlink: X:/DoE-claude/tasks/2026-07-20-install-dogfood-friction.md
    """
    if os.name != "nt":
        return p
    m = _MSYS_DRIVE_RE.match(p)
    if not m:
        return p
    drive = m.group(1)
    tail = m.group(2) or "/"
    return f"{drive}:{tail}"


# grep -vE '(AppData[\\/]Local[\\/]Temp|^[A-Za-z]:\\?$|/\.claude$)' — Tier A's
# meta-repo/scratch/bare-drive-root exclusion filter. Ported byte-for-byte
# from the bash ERE (see module docstring for the greedy-decode context).
_TIER_A_EXCLUDE_RE = re.compile(r"(AppData[\\/]Local[\\/]Temp|^[A-Za-z]:\\?$|/\.claude$)")

_TIER_B_CANDIDATES: List[str] = [
    "~/dev", "~/Dev", "~/code", "~/Code", "~/src", "~/Source",
    "~/Projects", "~/projects", "~/workspace", "~/repos",
    "~/Documents/GitHub", "/c/dev", "/d/dev", "/e/dev", "/x",
]

_SORT_TIMEOUT_SECS = 10


def _sort_unique(lines: Iterable[str]) -> List[str]:
    """Byte-parity shim for the oracle's `sort -u`.

    `sort -u`'s ordering is LC_COLLATE-dependent (case-insensitive-ish
    collation on typical locales) and does NOT match Python's `sorted()`
    (plain ordinal/byte comparison — uppercase sorts before lowercase). Shell
    out to the real `sort` binary so trampoline stdout is byte-identical to
    the bash oracle's, whatever the runtime locale is. `sort` ships with
    coreutils on macOS/Linux and with Git for Windows (git-bash) — the same
    environments this codebase already assumes for `git`/`machine-local`
    subprocess calls. Degrades to Python's locale-naive `sorted(set(...))`
    (never raises) if `sort` is unavailable or times out — approximate
    ordering, never a block, matching the never-block contract.
    """
    lines = list(lines)
    if not lines:
        return []
    try:
        proc = subprocess.run(
            ["sort", "-u"],
            input="\n".join(lines) + "\n",
            capture_output=True,
            text=True,
            timeout=_SORT_TIMEOUT_SECS,
            check=False,
            **_CREATIONFLAGS,
        )
        if proc.returncode == 0:
            return [line for line in proc.stdout.split("\n") if line]
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _sort_unique: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        pass
    return sorted(set(lines))


# ---------------------------------------------------------------------------
# Gate: is posix_dir the root of a real git repo?
# ---------------------------------------------------------------------------

def _is_git_root(posix_dir: str) -> bool:
    """Returns True iff posix_dir is the root of a real git repo.

    Non-spawning seam, not a per-candidate `git -C <dir> rev-parse
    --show-toplevel`: delegates to `coordinator_core.git.repo_root.
    show_toplevel`, which WALKS up from `posix_dir` looking for a `.git`
    entry (dir or file — worktree-safe, same as the direct-spawn form this
    replaces) and only falls back to a single spawn per distinct resolved
    cwd if the walk finds nothing, memoized process-lifetime by that module
    (see its docstring). `_gate_and_dedup` can call this once per candidate
    dir it gates (spec backlink: this chunk's brief, C12 — "stops probing
    `git rev-parse --show-toplevel` per candidate dir") — every candidate
    that IS a real repo root resolves via the walk alone, zero spawns; only
    candidates with no `.git` anywhere on their own ancestor chain (bogus
    scratch paths, bare parent dirs) fall through to `show_toplevel`'s
    single spawn fallback, exactly mirroring what a direct `git rev-parse`
    there would have done anyway.

    Not realpath — DR-148 (BSD portability of the bash oracle);
    `os.path.realpath` is the direct Python equivalent of `pwd -P` here
    (both physically resolve symlinks), so fidelity is preserved.
    Worktree-safe: the walk (and its spawn fallback) succeeds on both
    .git-dir repos and .git-file worktrees. Subdirectory-safe: if
    posix_dir is a subdir of a repo, `toplevel` identifies the repo root,
    not the subdir, so identity fails and this returns False.

    Identity is established via `os.path.samefile(toplevel, canon)`, not
    plain string `==` — `show_toplevel` always emits POSIX (forward-slash)
    separators on its spawn-fallback leg (mirroring `git rev-parse
    --show-toplevel`), while `os.path.realpath` emits native separators
    (backslashes on Windows), so a plain `==` never holds on Windows even
    when the two paths name the same directory on disk (`toplevel=
    'X:/DoE-claude'` vs `canon='X:\\DoE-claude'`). `samefile` resolves
    separators, drive-letter case, and 8.3 short names via the filesystem,
    which is the identity check actually intended here. Falls back to a
    normcase/normpath string comparison if `samefile` raises `OSError`
    (e.g. one of the two paths vanished between resolution and the
    comparison — a real race on a shared tree); returns False only if both
    approaches fail to establish identity.

    Spec backlink: X:/DoE-claude/tasks/2026-07-20-install-dogfood-friction.md
    """
    if not os.path.isdir(posix_dir):
        return False
    try:
        canon = os.path.realpath(posix_dir)
    except OSError:
        print(f"skip: _is_git_root: canon = os.path.realpath(posix_dir) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    toplevel = show_toplevel(posix_dir)
    if not toplevel:
        return False
    try:
        return os.path.samefile(toplevel, canon)
    except OSError:
        return os.path.normcase(os.path.normpath(toplevel)) == os.path.normcase(
            os.path.normpath(canon)
        )


# ---------------------------------------------------------------------------
# Cross-tier dedup key normalization.
# ---------------------------------------------------------------------------

_DRIVE_FORM_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def _to_posix_key(p: str) -> str:
    """Normalize a path (native "X:\\a\\b", "X:/a/b", or POSIX "/x/a/b") to a
    POSIX dedup KEY with no trailing slash. The key is the existence-test path
    AND the cross-tier dedup identity — it collapses the native/POSIX form
    mismatch that otherwise survives dedup when the same repo surfaces in two
    tiers (e.g. Tier A native + Tier A.5 registry form). Only the DRIVE LETTER
    is lowercased (X: and x: are the same drive) — the rest of the path keeps
    its case so this does not corrupt case-sensitive POSIX paths.
    """
    m = _DRIVE_FORM_RE.match(p)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2).replace("\\", "/")
        p = f"/{drive}/{rest}"
    else:
        p = p.replace("\\", "/")
    while p.endswith("/") and p != "/":
        p = p[:-1]
    return p


def _emit_form(p: str) -> str:
    """The single output form for a discovered repo path: forward slashes, no
    trailing slash, original drive-letter case. Distinct from `_to_posix_key`,
    which additionally lowercases the drive and rewrites `X:/a` to `/x/a` — that
    is the internal dedup/existence-probe identity, not something a consumer
    should ever see.
    """
    p = p.replace(BACKSLASH, "/")
    while p.endswith("/") and p != "/":
        p = p[:-1]
    return p


def _publish_mirror_keys() -> set:
    """Dedup keys (`_to_posix_key` form) of every path registered under the
    machine-local `publish.mirrors.*.path` namespace.

    Best-effort, matching this module's never-block contract: an unreadable
    registry yields an empty set (discovery proceeds unfiltered) rather than
    an error.
    """
    flat = _merged_flat_registry()
    mirrors: set = set()
    for key in flat:
        if not (key.startswith("publish.mirrors.") and key.endswith(".path")):
            continue
        val = _registry_value(key, flat)
        s = (val or "").strip()
        if s:
            mirrors.add(_to_posix_key(s))
    return mirrors


def _gate_and_dedup(lines: Iterable[str], mirror_keys: Optional[set] = None) -> Iterator[str]:
    """Filter stdin-equivalent lines to real git roots, deduped by normalized
    POSIX key, emitting each surviving repo in `_emit_form` (forward slashes).
    The oracle's preserve-first-seen-native-form behavior is deliberately gone
    — see the module docstring's Output contract § 1.

    Three filters in one pass:
      (1) `.git` gate — drops bare parent dirs / scratch paths that pass a
          plain existence test but are not repos (the Tier-A leak).
      (2) cross-tier form dedup — collapses native vs POSIX duplicates of one
          repo.
      (3) publish-mirror exclusion — a `publish.mirrors.*.path` tree is a
          publish target, never a working repo (see module docstring).
    """
    if mirror_keys is None:
        mirror_keys = _publish_mirror_keys()
    seen: set = set()
    for line in lines:
        if not line:
            continue
        key = _to_posix_key(line)
        if key in mirror_keys:
            continue
        if not _is_git_root(_fs_probe_path(key)):
            continue
        if key in seen:
            continue
        seen.add(key)
        yield _emit_form(line)


# ---------------------------------------------------------------------------
# Tier A — Claude Code's own activity record.
# ---------------------------------------------------------------------------

def _tier_a_posix(win: str, fs_root: str) -> str:
    """Convert a native Windows-form path ("X:\\a\\b") to its POSIX
    existence-test path (lowercased). With fs_root set, the drive root is
    replaced by fs_root — a hermetic test seam; empty in production.
    """
    if fs_root:
        idx = win.find(":\\")
        tail = win[idx + 2:] if idx != -1 else win
        tail = tail.replace("\\", "/").lower()
        return f"{fs_root}/{tail}"
    converted = re.sub(r"^([A-Za-z]):\\", r"/\1/", win)
    converted = converted.replace("\\", "/")
    return converted.lower()


def _tier_a_greedy_decode(rest: str, drive: str, fs_root: str) -> Optional[str]:
    """Greedy filesystem-walk disambiguation of a lossy projects-dir
    remainder.

    Args: rest = post-drive remainder (e.g. "dev-fifa-stats"), drive = drive
    letter, fs_root = optional POSIX walk root (test seam; defaults to
    /<drive>). At each level, consume the LONGEST run of remaining
    `-`-tokens that names an existing directory; descend; repeat. This
    resolves hyphen-as-literal vs hyphen-as-separator by what actually exists
    on disk. Returns the reconstructed native Windows path on a full
    resolution, None otherwise (fail-safe — a miss never emits a wrong path).
    """
    root = fs_root if fs_root else f"/{drive.lower()}"
    tokens = rest.split("-")
    n = len(tokens)
    # Bound the walk so a pathological all-hyphen name can't blow up the search.
    if n > 40:
        return None
    cur = root
    segs: List[str] = []
    i = 0
    while i < n:
        matched = False
        for j in range(n, i, -1):
            # cand is rebuilt fresh each j-iteration (longest run first).
            cand = "-".join(tokens[i:j])
            if not cand:
                continue
            cand_lc = cand.lower()
            candidate_path = f"{cur}/{cand_lc}"
            if os.path.isdir(_fs_probe_path(candidate_path)):
                segs.append(cand)
                cur = candidate_path
                i = j
                matched = True
                break
        if not matched:
            return None
    if not segs:
        return None
    # Reconstruct the native Windows path (Tier A's output contract): drive +
    # backslash-joined segments, as cased in the encoded name.
    return f"{drive}:" + "".join(f"\\{seg}" for seg in segs)


def _decode_projects_dir_name(base: str) -> tuple:
    """Decode one ~/.claude/projects/ basename. Returns (drive, rest, decoded).

    Path encoding: `:` `\\` `/` `.` -> `-`. Drive root "X:\\Foo" -> "X--Foo".
    The encoding is LOSSY: a literal hyphen inside a path segment and a
    structural separator both encode to `-`. We keep the naive decode as a
    zero-cost fast path (caller falls back to greedy disambiguation on miss).
    """
    m = re.match(r"^[A-Za-z]--(.*)$", base)
    if m:
        drive = base[0]
        rest = base[3:]
        decoded = f"{drive}:\\" + rest.replace("-", "\\")
        return drive, rest, decoded
    # Non-drive-letter entries (POSIX-form, not produced by Claude Code on
    # Windows): naive decode only, no greedy fallback — out of scope (see
    # module docstring "Known oracle gap").
    drive = ""
    rest = base
    decoded = base.replace("-", "\\")
    return drive, rest, decoded


def _tier_a() -> List[str]:
    # Optional hermetic-test seam: POSIX filesystem root that decoded paths
    # are existence-tested against. Empty in production (real drive roots).
    fs_root = os.environ.get("COORDINATOR_TIER_A_FS_ROOT", "")
    # Optional hermetic-test seam: overrides the activity-record directory
    # itself. Empty in production (real `~/.claude/projects`) — without this,
    # `main()` always reads the live machine's activity record, which is
    # exactly the non-hermeticity that kept the whole-op spawn count
    # unmeasured (see this module's spawn-budget test).
    projects_dir_override = os.environ.get("COORDINATOR_TIER_A_PROJECTS_DIR", "")
    projects_dir = (
        Path(projects_dir_override)
        if projects_dir_override
        else Path.home() / ".claude" / "projects"
    )
    try:
        entries = [p for p in projects_dir.iterdir() if p.is_dir()]
    except OSError:
        entries = []
    entries.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    entries = entries[:50]

    emitted: List[str] = []
    for p in entries:
        base = p.name
        drive, rest, decoded = _decode_projects_dir_name(base)
        posix = _tier_a_posix(decoded, fs_root)
        if os.path.isdir(_fs_probe_path(posix)):
            # Fast path: naive decode resolved (no literal hyphens in segments).
            emitted.append(decoded)
        elif drive:
            # Naive decode missed — disambiguate the lossy encoding against disk.
            greedy = _tier_a_greedy_decode(rest, drive, fs_root)
            if greedy:
                emitted.append(greedy)

    filtered = [line for line in emitted if not _TIER_A_EXCLUDE_RE.search(line)]
    return _sort_unique(filtered)[:20]


# ---------------------------------------------------------------------------
# Tier A.5 — machine-local registry repos.* enumeration.
# ---------------------------------------------------------------------------

def _tier_a5() -> List[str]:
    """Closes the gap where an operator has registered sibling repos in
    registry.local.toml but no activity record exists yet (Tier A miss) AND
    the path doesn't match the dev-folder probe layouts (Tier B miss).
    Defensive fallback: silently no-op if the registry is unreadable — see
    `_merged_flat_registry`'s never-block contract.
    """
    flat = _merged_flat_registry()
    results: List[str] = []
    for key in flat:
        if not key.startswith("repos."):
            continue
        val = _registry_value(key, flat)
        val = (val or "").strip()
        if not val:
            continue
        # Normalize to POSIX form for the existence test (registry values are
        # commonly stored as native paths like "X:/foo" or "X:\foo").
        posix = re.sub(r"^([A-Za-z]):[\\/]", r"/\1/", val)
        posix = posix.replace("\\", "/").lower()
        if os.path.isdir(posix) or os.path.isdir(val):
            results.append(val)
    return _sort_unique(results)


# ---------------------------------------------------------------------------
# Tier B — common dev-folder layouts.
# ---------------------------------------------------------------------------

def _tier_b() -> List[str]:
    """Accept .git as directory OR file (worktrees use a `.git` file
    containing `gitdir: <path>` — a dir-only test alone misses them).
    Mirrors `find "$cand" -maxdepth 2 -name .git ... | sed 's|/\\.git$||'`.
    """
    results: List[str] = []
    for cand in _TIER_B_CANDIDATES:
        cand_expanded = os.path.expanduser(cand)
        if not os.path.isdir(cand_expanded):
            continue
        # maxdepth 1 relative to cand: cand/.git
        depth1 = os.path.join(cand_expanded, ".git")
        if os.path.isdir(depth1) or os.path.isfile(depth1):
            results.append(cand_expanded)
        # maxdepth 2 relative to cand: cand/*/.git
        try:
            with os.scandir(cand_expanded) as it:
                for entry in it:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    depth2 = os.path.join(entry.path, ".git")
                    if os.path.isdir(depth2) or os.path.isfile(depth2):
                        results.append(entry.path)
        except OSError:
            print(f"skip: _tier_b: with os.scandir(cand_expanded) as it: failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue
    return _sort_unique(results)[:30]


# ---------------------------------------------------------------------------
# Dispatch.
# ---------------------------------------------------------------------------

def main(argv: Sequence[str]) -> int:
    """Port of discover-working-repos.sh's top-level tier dispatch.

    Exit-code contract: ALWAYS returns 0 — this is a best-effort discovery
    helper (never a gate); the caller (`/setup` Phase 2 Step 4) falls
    through to an interactive Tier-C prompt on empty stdout, so there is no
    failure signal to distinguish via exit code. Matches the bash oracle,
    which has no non-zero exit path at all. An unexpected internal error is
    swallowed to stderr rather than propagated, preserving that contract
    (advisory / never-block posture per PORTER-BRIEF-ADDENDUM.md § 3b).
    """
    del argv  # no CLI flags — mirrors the bash oracle (no arg parsing)
    try:
        a_out = _tier_a()
    except Exception as exc:  # noqa: BLE001 — never-block contract
        print(f"discover-working-repos.sh: Tier A failed: {exc}", file=sys.stderr)
        a_out = []
    try:
        a5_out = _tier_a5()
    except Exception as exc:  # noqa: BLE001 — never-block contract
        print(f"discover-working-repos.sh: Tier A.5 failed: {exc}", file=sys.stderr)
        a5_out = []

    # Tier A.5 always runs ALONGSIDE the first non-empty tier (A or B). Its
    # purpose is to close gaps in Tier A — an operator may have registered a
    # sibling repo in registry.local.toml but lack an activity record for
    # it, so a strict stop-at-first-non-empty A would mask the registered
    # repo. Merge + dedup.
    mirror_keys = _publish_mirror_keys()

    if a_out:
        combined = list(a_out) + list(a5_out)
        for line in _sort_unique(_gate_and_dedup(combined, mirror_keys)):
            print(line)
        return 0

    try:
        b_out = _tier_b()
    except Exception as exc:  # noqa: BLE001 — never-block contract
        print(f"discover-working-repos.sh: Tier B failed: {exc}", file=sys.stderr)
        b_out = []

    if b_out or a5_out:
        combined = list(b_out) + list(a5_out)
        for line in _sort_unique(_gate_and_dedup(combined, mirror_keys)):
            print(line)
        return 0

    # All tiers empty — exit 0 with no stdout; caller handles Tier C interactively.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
