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
  2. `git -C <target_root> check-attr eol text -z --stdin` -- fed the WHOLE
     tracked-path list over stdin in one call (the reason `--stdin` exists),
     not one call per path. Both attributes are requested in this SAME
     spawn (not a second call) so the pinned three-spawn budget does not
     change: `eol` for the declared direction, `text` to exclude any path
     git will never text-normalize at checkin (see the F1 note below).
  3. `git -C <target_root> status --porcelain -z` -- identifies dirty paths,
     which are excluded from violation reporting: a path mid-edit may
     legitimately carry content that does not yet match its declared
     attribute, and flagging it is not a drift finding, it is noise. Mirrors
     `eol.repair`'s own "skip any file git reports dirty" rule (C3).

Bytes end to end, no exception: both git calls run through
`coordinator_core.git.run` in BINARY mode -- `input=<bytes>` for check-attr
(which implies it) and `binary=True` for status -- and read their records off
`GitResult.stdout_bytes` split on `b"\\x00"`, never off the decoded
`GitResult.stdout`, whose `errors="replace"` is the same lossiness this
module exists to avoid. File content is read via
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

Binary-content guard (fix for a landed-review finding, not part of the
original AC set): a wildcard `.gitattributes` declaration such as
`* text=auto eol=crlf` makes `check-attr eol` answer `crlf` for EVERY
tracked path INCLUDING binaries, while `text=auto` leaves git to sniff
binary-ness per file at commit time rather than reporting a per-path
verdict through `check-attr text` (that call reports the literal `auto`
macro value, not a resolved binary verdict). Two independent guards, both
required, neither sufficient alone:
  - Any path whose `text` attribute is explicitly `unset` (a bare `-text`
    or the `binary` macro, which implies `-text`) is excluded from the
    violation predicate outright -- git never text/eol-normalizes such a
    path, so an `eol=` declaration on it carries no direction to check
    bytes against.
  - Belt-and-braces: any content containing a NUL byte is excluded from
    the violation predicate regardless of what `text` reports -- catches
    the `text=auto` case above, where `check-attr text` cannot tell us the
    per-file verdict. Imperfect (a NUL-free binary would still slip
    through) but the two guards together close the reachable corruption
    path traced in review: `eol.repair` no-ops on a clean predicate, so a
    false negative here costs a missed detection, never a bad write.
`eol.repair` relies on this predicate never firing a false positive on
binary content -- see that module's docstring.

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

import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from coordinator_core.cartography._guard import PathEscapeError, path_guard
from coordinator_core.git.ls_files_bytes import tracked_files_bytes
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.git.run import run_git

#: `check-attr` values that carry no LF/CRLF direction -- "unspecified" (no
#: declaration at all) feeds declaration coverage; "set"/"unset" (the
#: attribute given a boolean rather than lf/crlf) carry no direction to
#: check bytes against, so they are skipped from the violation predicate.
_NO_DIRECTION = frozenset({b"set", b"unset"})

#: `text` attribute value that means "git will never text/eol-normalize
#: this path" -- a bare `-text` or the `binary` macro (which implies
#: `-text`). See the module docstring's "Binary-content guard" note (F1).
_TEXT_EXCLUDED = frozenset({b"unset"})


def _check_attr_eol_text(root: Path, paths: Tuple[bytes, ...]) -> Dict[bytes, Dict[bytes, bytes]]:
    """One batched `git check-attr eol text -z --stdin` call over the whole
    `paths` list, fetching BOTH attributes in the SAME spawn so the pinned
    three-spawn budget (AC3) does not change. Returns
    {path_bytes: {attr_name_bytes: value_bytes}}; an attribute git never
    emits a record for is simply absent from that path's sub-dict (folded
    to "unspecified" by the caller).
    """
    if not paths:
        return {}
    stdin_payload = b"\x00".join(paths) + b"\x00"
    # `input` puts the call in binary mode, so `stdout_bytes` carries git's
    # `-z` records as written -- a decoded read would substitute U+FFFD on
    # the very path bytes this census exists to compare.
    attr_read = run_git(
        ["-C", str(root), "check-attr", "eol", "text", "-z", "--stdin"],
        input=stdin_payload,
    )
    if not attr_read.ok:
        if not attr_read.timed_out and attr_read.returncode != 127:
            print(
                f"eol.census: git -C {root} check-attr eol text -z --stdin exited "
                f"{attr_read.returncode}: {attr_read.stderr.strip()}",
                file=sys.stderr,
            )
        return {}
    tokens = [t for t in attr_read.stdout_bytes.split(b"\x00") if t]
    result: Dict[bytes, Dict[bytes, bytes]] = {}
    for i in range(0, len(tokens) - len(tokens) % 3, 3):
        path_b, attr_b, value_b = tokens[i], tokens[i + 1], tokens[i + 2]
        result.setdefault(path_b, {})[attr_b] = value_b
    return result


def _require_worktree_toplevel(guarded_root: Path) -> None:
    """Refuse a `target_root` that is not itself a git worktree's top
    level. `path_guard` proves the resolved path is self-contained; it has
    no notion of git at all. A caller pointed at any subdirectory of a
    worktree silently desyncs the two path frames this module compares:
    `tracked_files_bytes` (`ls-files`, CWD-relative to whatever root it was
    given) against `_dirty_paths` (`status --porcelain`, always relative to
    the repo TOP LEVEL and covering the WHOLE repo). Under that mismatch
    `dirty_violation_count` silently reads 0 on a tree full of in-flight
    work. Never spawns: `show_toplevel` walks only.
    """
    toplevel = show_toplevel(str(guarded_root))
    resolved_toplevel = Path(toplevel).resolve() if toplevel is not None else None
    if resolved_toplevel != guarded_root:
        raise PathEscapeError(
            f"target_root must be a git worktree's top level, got "
            f"{guarded_root} (resolved worktree top level: {toplevel!r})"
        )


def _dirty_paths(root: Path) -> Set[bytes]:
    """One batched `git status --porcelain -z` call. Returns the set of
    repo-relative path bytes git reports as dirty (any status class,
    including both sides of a rename/copy record).
    """
    # `binary=True` for the same reason `_check_attr_eol_text` runs binary: a
    # `-z` record set whose paths are decoded with `errors="replace"` no
    # longer matches the path bytes the rest of this census keys on.
    status = run_git(["-C", str(root), "status", "--porcelain", "-z"], binary=True)
    if not status.ok:
        return set()
    tokens = [t for t in status.stdout_bytes.split(b"\x00") if t]
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
    # use_cache=False: this is the warm-served consumer of the process-
    # lifetime lru_cache. A cached tracked-file list would pin this root's
    # corpus for the life of a resident warm server (F4) -- census is a
    # cadence-fired rider, not a one-shot CLI, so it must not inherit the
    # cold-CLI cache assumption `ls_files_bytes`'s sibling module makes. See
    # that module's docstring.
    tracked = tracked_files_bytes(root, use_cache=False)
    attrs = _check_attr_eol_text(root, tracked)
    dirty = _dirty_paths(root)

    violations: List[dict] = []
    unspecified_count = 0

    for path_b in tracked:
        path_attrs = attrs.get(path_b, {})
        value = path_attrs.get(b"eol", b"unspecified")
        if value == b"unspecified":
            unspecified_count += 1
            continue
        if value in _NO_DIRECTION:
            continue

        text_value = path_attrs.get(b"text", b"unspecified")
        if text_value in _TEXT_EXCLUDED:
            # `-text` / `binary` macro: git never text/eol-normalizes this
            # path, so its `eol=` declaration carries no direction to check
            # bytes against (F1).
            continue

        rel = path_b.decode("utf-8", errors="surrogateescape")
        fs_path = root / rel
        try:
            content = fs_path.read_bytes()
        except OSError:
            continue

        if b"\x00" in content:
            # Belt-and-braces binary guard (F1): a NUL byte is near-certain
            # proof of binary content, catching the `text=auto` case the
            # `text` attribute check above cannot resolve per-file. See the
            # module docstring's "Binary-content guard" note.
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


# Op registration removed 2026-08-27 (K-062): the eol trio collapsed to ONE
# dispatchable op, `eol.repair`, whose `mutate: false` mode IS this census —
# it calls `census()` below directly. The handler is kept, unregistered, as
# the documented wire shape a re-registration would restore verbatim.
def _eol_census(params: dict, repo_root: Optional[Path] = None) -> dict:
    """"eol.census" handler -- bidirectional declared-vs-actual EOL drift
    census over any caller-supplied `target_root`. See module docstring for
    the full contract.

    Wire params:
        target_root (str, required) -- root of the tree to scan. Must BE a
                                        git worktree's top level (not merely
                                        resolve somewhere inside one) -- a
                                        subdirectory is refused, see F3.

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
    cannot be resolved on disk, escapes itself, or is not itself a git
    worktree's top level (see `_require_worktree_toplevel`).
    """
    target_root = params.get("target_root")
    if not target_root:
        raise ValueError("eol.census requires param: target_root")

    guarded_root = path_guard(target_root, ".")
    _require_worktree_toplevel(guarded_root)
    result = census(guarded_root)
    result["target_root"] = str(guarded_root)
    return result
