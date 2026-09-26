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
names look like `-Users-example-operator-X-DoE-claude`, Tier A's naive decode produces a
backslash path that will not exist on disk, and there is no greedy-decode
fallback for that shape (the oracle's own comment calls this "out of scope").
Tier A is effectively Windows-only in practice; Tier A.5 and Tier B are the
functioning discovery paths on POSIX. Do not silently "fix" this — it is a
faithful repro of the pre-port oracle's documented gap.

Output contract (two deliberate departures from the oracle, 2026-08-14):

  1. Every emitted path uses forward slashes. The oracle preserved each repo's
     first-seen native form, so a single Windows run mixed `X:/example-os-repo`
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
from coordinator_core.path_identity import dir_identity
from coordinator_core.machine_resolver import (
    merged_flat_registry as _merged_flat_registry,
    registry_value as _registry_value,
)
from coordinator_core.win_portability import no_console_creationflags


_CREATIONFLAGS = no_console_creationflags()


_MSYS_DRIVE_RE = re.compile(r"^/([A-Za-z])(/.*)?$")

BACKSLASH = chr(92)


def _fs_probe_path(p: str) -> str:
    if os.name != "nt":
        return p
    m = _MSYS_DRIVE_RE.match(p)
    if not m:
        return p
    drive = m.group(1)
    tail = m.group(2) or "/"
    return f"{drive}:{tail}"


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


def _is_git_root(posix_dir: str) -> bool:
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


_DRIVE_FORM_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def _to_posix_key(p: str) -> str:
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
    p = p.replace(BACKSLASH, "/")
    while p.endswith("/") and p != "/":
        p = p[:-1]
    return p


def _publish_mirror_keys() -> set:
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


def _identities(keys: Iterable[str]) -> set:
    return {dir_identity(_fs_probe_path(k), fallback=k) for k in keys}


def _gate_and_dedup(lines: Iterable[str], mirror_keys: Optional[set] = None) -> Iterator[str]:
    if mirror_keys is None:
        mirror_keys = _publish_mirror_keys()
    mirror_ids = _identities(mirror_keys)
    seen: set = set()
    for line in lines:
        if not line:
            continue
        key = _to_posix_key(line)
        if key in mirror_keys:
            continue
        probe = _fs_probe_path(key)
        if not _is_git_root(probe):
            continue
        identity = dir_identity(probe, fallback=key)
        if identity in mirror_ids:
            continue
        if identity in seen:
            continue
        seen.add(identity)
        yield _emit_form(line)


def _tier_a_posix(win: str, fs_root: str) -> str:
    if fs_root:
        idx = win.find(":\\")
        tail = win[idx + 2:] if idx != -1 else win
        tail = tail.replace("\\", "/").lower()
        return f"{fs_root}/{tail}"
    converted = re.sub(r"^([A-Za-z]):\\", r"/\1/", win)
    converted = converted.replace("\\", "/")
    return converted.lower()


def _tier_a_greedy_decode(rest: str, drive: str, fs_root: str) -> Optional[str]:
    root = fs_root if fs_root else f"/{drive.lower()}"
    tokens = rest.split("-")
    n = len(tokens)
    if n > 40:
        return None
    cur = root
    segs: List[str] = []
    i = 0
    while i < n:
        matched = False
        for j in range(n, i, -1):
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
    return f"{drive}:" + "".join(f"\\{seg}" for seg in segs)


def encode_projects_dir_name(root: str) -> str:
    normalized = str(root).replace("\\", "/").replace(":", "-").replace(".", "-")
    return normalized.replace("/", "-")


def _decode_projects_dir_name(base: str) -> tuple:
    m = re.match(r"^[A-Za-z]--(.*)$", base)
    if m:
        drive = base[0]
        rest = base[3:]
        decoded = f"{drive}:\\" + rest.replace("-", "\\")
        return drive, rest, decoded
    drive = ""
    rest = base
    decoded = base.replace("-", "\\")
    return drive, rest, decoded


def _tier_a() -> List[str]:
    fs_root = os.environ.get("COORDINATOR_TIER_A_FS_ROOT", "")
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
            emitted.append(decoded)
        elif drive:
            greedy = _tier_a_greedy_decode(rest, drive, fs_root)
            if greedy:
                emitted.append(greedy)

    filtered = [line for line in emitted if not _TIER_A_EXCLUDE_RE.search(line)]
    return _sort_unique(filtered)[:20]


def _tier_a5() -> List[str]:
    flat = _merged_flat_registry()
    results: List[str] = []
    for key in flat:
        if not key.startswith("repos."):
            continue
        val = _registry_value(key, flat)
        val = (val or "").strip()
        if not val:
            continue
        posix = re.sub(r"^([A-Za-z]):[\\/]", r"/\1/", val)
        posix = posix.replace("\\", "/").lower()
        if os.path.isdir(posix) or os.path.isdir(val):
            results.append(val)
    return _sort_unique(results)


def _tier_b() -> List[str]:
    results: List[str] = []
    for cand in _TIER_B_CANDIDATES:
        cand_expanded = os.path.expanduser(cand)
        if not os.path.isdir(cand_expanded):
            continue
        depth1 = os.path.join(cand_expanded, ".git")
        if os.path.isdir(depth1) or os.path.isfile(depth1):
            results.append(cand_expanded)
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


def discover_repo_paths() -> List[str]:
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
    mirror_keys = _publish_mirror_keys()

    if a_out:
        combined = list(a_out) + list(a5_out)
        return _sort_unique(_gate_and_dedup(combined, mirror_keys))

    try:
        b_out = _tier_b()
    except Exception as exc:  # noqa: BLE001 — never-block contract
        print(f"discover-working-repos.sh: Tier B failed: {exc}", file=sys.stderr)
        b_out = []

    if b_out or a5_out:
        combined = list(b_out) + list(a5_out)
        return _sort_unique(_gate_and_dedup(combined, mirror_keys))

    return []


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
    del argv
    for line in discover_repo_paths():
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
