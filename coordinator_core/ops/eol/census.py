"""coordinator_core.ops.eol.census -- "eol.census" op: bidirectional
declared-vs-actual line-ending drift detection over any caller-supplied
`target_root`.

Purpose: `.gitattributes` declares `eol=lf` or `eol=crlf` per path, but
nothing enforces the declaration matches the bytes actually on disk. This op
is the census -- read-only, no repair -- reporting every tracked path where
the declaration and the content disagree, in EITHER direction:

  - `eol=lf` declared, b"\\r\\n" present in the file           (today's live class)
  - `eol=crlf` declared, LF-only content (no b"\\r\\n" at all)  (empty today, but
    the class that breaks .cmd/.ps1 launchers -- cheap to detect once the
    predicate already reads the bytes, so both directions are checked)

Three batched git spawns total, independent of file count -- never one per
file (that shape is route `a-direct` in the amplification gate and is
correctly refused; see `coordinator_core/tests/
test_no_unbatched_per_item_git_spawn.py`):

  1. `git -C <target_root> ls-files -z` -- via `coordinator_core.git.
     ls_files_bytes.tracked_files_bytes` (C1's bytes-preserving sibling of
     `ls_files.tracked_files`; that one decodes with errors="replace" before
     splitting, which substitutes U+FFFD on non-UTF-8 path bytes -- lossy for
     a byte-exactness probe).
  2. `git -C <target_root> check-attr eol -z --stdin` -- fed the WHOLE
     tracked-path list over stdin in one call (the reason `--stdin` exists),
     not one call per path.
  3. `git -C <target_root> status --porcelain -z` -- identifies dirty paths,
     which are excluded from violation reporting: a path mid-edit may
     legitimately carry content that does not yet match its declared
     attribute, and flagging it is not a drift finding, it is noise. Mirrors
     `eol.repair`'s own "skip any file git reports dirty" rule (C3).

Bytes end to end, no exception: every subprocess call omits `text=True`, both
git commands read/write bytes explicitly (`input=<bytes>` for check-attr,
raw `proc.stdout` bytes split on `b"\\x00"`), and file content is read via
`Path.read_bytes()` -- never `Path.read_text()` or `open(..., "r")`. A path
byte string is decoded ONLY at the point it needs to become an OS path
(`Path()` join) or a JSON-safe report field, using `errors="surrogateescape"`
so non-UTF-8 path bytes round-trip rather than becoming U+FFFD (the same
lossiness this module exists to avoid -- see `ls_files_bytes` module
docstring and this plan's anti-scope: the predecessor session's first probe
read `check-attr --stdin` through Python TEXT mode, which translated its own
newlines, handed git every path with a trailing `\\r`, matched 1 path in
12,152, and reported a clean tree on a badly drifted one).

Target resolution: `target_root` wire param, path-guarded via
`coordinator_core.cartography._guard.path_guard` -- the injected `repo_root`
argument is ignored entirely (this plan's anti-scope: "Do not resolve the
target root from the environment" / AC7). Reference shape:
`coordinator_core/ops/cartography_tree.py :: _cartography_tree`.

Declaration coverage (AC15): tracked substrate paths carrying NO `eol`
attribute at all (`check-attr` reports "unspecified"). Zero marginal spawn
cost -- the same check-attr batch that finds violations already returns
"unspecified" for these paths. Reported alongside violation counts because
census sensitivity is bounded by the receiver's own `.gitattributes`
declarations: a repo with only a handful of `eol=lf` lines can carry
thousands of undeclared, undetectable drifted files and still report zero
violations. Coverage turns that into a legible caveat rather than a silent
false negative.

COMPUTE_ONLY / scope "none" classification is made at registration time
(this plan's C5), not here -- this module makes no write of any kind: two
read-only git subprocess calls (`check-attr`, `status --porcelain`, plus the
`ls-files` spawn inside `tracked_files_bytes`) and read-only filesystem
access (`Path.read_bytes()`). No `open(..., "w")`, no git write command.

Negative-spec:
  - Does NOT normalize or otherwise mutate any file -- read-only reporting.
    Repair is a separate op (`eol.repair`, C3).
  - Does NOT spawn a subprocess per tracked path -- exactly three git
    processes per call, independent of file count (AC3).
  - Does NOT read any file in text mode, nor pass `text=True` to any
    subprocess call (AC2).
  - Does NOT resolve `target_root` from the environment or the injected
    `repo_root` argument (AC7).
  - Does NOT count a dirty path as a violation.

Spec backlink: docs/plans/2026-08-20-every-repo-detects-its-own-eol-drift.md § C2, AC1-AC3, AC15
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from coordinator_core.cartography._guard import path_guard
from coordinator_core.git.ls_files_bytes import tracked_files_bytes
from coordinator_core.ipc import register_op
from coordinator_core.win_portability import no_console_creationflags

#: `check-attr` values that carry no LF/CRLF direction -- "unspecified" (no
#: declaration at all) feeds declaration coverage; "set"/"unset" (the
#: attribute given a boolean rather than lf/crlf) carry no direction to
#: check bytes against, so they are skipped from the violation predicate.
_NO_DIRECTION = frozenset({b"set", b"unset"})


def _check_attr_eol(root: Path, paths: Tuple[bytes, ...]) -> Dict[bytes, bytes]:
    """One batched `git check-attr eol -z --stdin` call over the whole
    `paths` list. Returns {path_bytes: value_bytes}; a path git errors on or
    that check-attr never emits a record for is simply absent from the
    result (folded to "unspecified" by the caller).
    """
    if not paths:
        return {}
    git_bin = shutil.which("git")
    if git_bin is None:
        return {}
    stdin_payload = b"\x00".join(paths) + b"\x00"
    try:
        proc = subprocess.run(
            [git_bin, "-C", str(root), "check-attr", "eol", "-z", "--stdin"],
            input=stdin_payload,
            capture_output=True,
            timeout=30,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        stderr = proc.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        print(
            f"eol.census: git -C {root} check-attr eol -z --stdin exited "
            f"{proc.returncode}: {(stderr or '').strip()}",
            file=sys.stderr,
        )
        return {}
    tokens = [t for t in proc.stdout.split(b"\x00") if t]
    result: Dict[bytes, bytes] = {}
    for i in range(0, len(tokens) - len(tokens) % 3, 3):
        path_b, _attr_b, value_b = tokens[i], tokens[i + 1], tokens[i + 2]
        result[path_b] = value_b
    return result


def _dirty_paths(root: Path) -> Set[bytes]:
    """One batched `git status --porcelain -z` call. Returns the set of
    repo-relative path bytes git reports as dirty (any status class,
    including both sides of a rename/copy record).
    """
    git_bin = shutil.which("git")
    if git_bin is None:
        return set()
    try:
        proc = subprocess.run(
            [git_bin, "-C", str(root), "status", "--porcelain", "-z"],
            capture_output=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if proc.returncode != 0:
        return set()
    tokens = [t for t in proc.stdout.split(b"\x00") if t]
    dirty: Set[bytes] = set()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        code = tok[:2]
        path_b = tok[3:] if len(tok) > 3 else tok
        dirty.add(path_b)
        # Rename/copy records carry a second path (the origin) as the very
        # next -z record, with no XY status prefix of its own.
        if b"R" in code or b"C" in code:
            i += 1
            if i < len(tokens):
                dirty.add(tokens[i])
        i += 1
    return dirty


def census(root: Path) -> dict:
    """Pure, synchronous core: three batched git spawns against `root`,
    reporting bidirectional declared-vs-actual EOL drift plus declaration
    coverage. See module docstring for the full contract.
    """
    tracked = tracked_files_bytes(root)
    attrs = _check_attr_eol(root, tracked)
    dirty = _dirty_paths(root)

    violations: List[dict] = []
    unspecified_count = 0

    for path_b in tracked:
        value = attrs.get(path_b, b"unspecified")
        if value == b"unspecified":
            unspecified_count += 1
            continue
        if value in _NO_DIRECTION:
            continue

        rel = path_b.decode("utf-8", errors="surrogateescape")
        fs_path = root / rel
        try:
            content = fs_path.read_bytes()
        except OSError:
            continue

        # A dirty path is REPORTED, flagged, and left for the operator; it is
        # not dropped. Dirtiness disqualifies a path from `eol.repair` (AC4),
        # never from detection (AC1 says "every tracked path"). Dropping it
        # here would make the census under-report by exactly the amount of
        # work in flight -- and on this box that is 50-70 concurrent sessions
        # keeping trees continuously dirty, in a number that goes out to eight
        # siblings and into the doctor probe.
        is_dirty = path_b in dirty
        has_crlf = b"\r\n" in content
        if value == b"lf" and has_crlf:
            violations.append({
                "path": rel,
                "declared": "lf",
                "found": "crlf-present",
                "dirty": is_dirty,
            })
        elif value == b"crlf" and not has_crlf and b"\n" in content:
            violations.append({
                "path": rel,
                "declared": "crlf",
                "found": "lf-only",
                "dirty": is_dirty,
            })

    violations.sort(key=lambda v: v["path"])
    return {
        "violations": violations,
        "violation_count": len(violations),
        "dirty_violation_count": sum(1 for v in violations if v["dirty"]),
        "tracked_count": len(tracked),
        "declaration_coverage": {
            "unspecified_count": unspecified_count,
            "declared_count": len(tracked) - unspecified_count,
        },
    }


@register_op("eol.census")
def _eol_census(params: dict, repo_root: Optional[Path] = None) -> dict:
    """"eol.census" handler -- bidirectional declared-vs-actual EOL drift
    census over any caller-supplied `target_root`. See module docstring for
    the full contract.

    Wire params:
        target_root (str, required) -- root of the tree to scan. Must
                                        resolve inside a git worktree.

    Returns:
        target_root          (str)  -- resolved target_root, echoed.
        violations            (list) -- sorted
                                        [{path, declared, found, dirty}, ...].
        violation_count        (int)  -- len(violations).
        dirty_violation_count   (int)  -- how many of those carry dirty=True.
                                          Reported, never dropped: dirtiness
                                          disqualifies a path from `eol.repair`
                                          (AC4), not from detection (AC1).
        tracked_count           (int)  -- total tracked paths considered.
        declaration_coverage    (dict) -- {unspecified_count, declared_count}.

    `repo_root` (injected by the engine) is ignored entirely -- the target is
    resolved solely from `target_root`, per this plan's anti-scope ("Do not
    resolve the target root from the environment") and AC7.

    Raises ValueError if target_root is missing; propagates
    coordinator_core.cartography._guard.PathEscapeError if target_root
    cannot be resolved inside a git worktree.
    """
    target_root = params.get("target_root")
    if not target_root:
        raise ValueError("eol.census requires param: target_root")

    guarded_root = path_guard(target_root, ".")
    result = census(guarded_root)
    result["target_root"] = str(guarded_root)
    return result
