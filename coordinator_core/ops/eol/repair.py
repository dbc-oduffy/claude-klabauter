"""coordinator_core.ops.eol.repair -- "eol.repair" op: normalizes tracked
files toward their DECLARED `eol` attribute, refusing anything not provably
EOL-only.

Purpose: `eol.census` (C2) finds declared-vs-actual drift; this op fixes it,
under two hard refusals rather than warnings -- both exercised against a real
tree in the predecessor session:

  1. Skip any file git reports dirty. A dirty path may legitimately carry
     content that has not yet been committed; repairing it would rewrite
     work in flight, not drift.
  2. Refuse any file whose index blob is not byte-identical to its
     LF-normalized worktree content. That equality is the proof the change
     is EOL-only rather than a content edit -- without it the op would be
     rewriting someone's uncommitted work under the cover of "just line
     endings". The comparison is against the raw INDEX blob (`git cat-file
     --batch` over `:<path>`) with content LF-normalized for the compare,
     never HEAD and never the direction-declared DISK-write bytes: git's own
     checkin-time text normalization always stores a text-attributed blob
     LF-normalized regardless of a path's declared `eol` direction (`eol=`
     steers checkout rendering only), so comparing straight against the
     declared-direction write bytes would refuse every `eol=crlf` repair
     unconditionally -- confirmed against a real tree in the predecessor
     session. See `_normalize_for_index_check`.

Normalizes toward the DECLARED value, BOTH directions (AC5): `eol=lf` holding
CRLF is rewritten `\\r\\n` -> `\\n`; `eol=crlf` holding LF-only content is
rewritten `\\n` -> `\\r\\n`. Never normalizes toward LF unconditionally --
that breaks `.cmd`/`.ps1` launchers declared `eol=crlf` on purpose (this
plan's anti-scope).

Every skip is reported with its reason (dirty / index-blob-mismatch /
index-blob-missing / read-error) -- a silent skip is indistinguishable from a
repair that did not need to happen.

NEGATIVE SPEC (default mode): `eol.repair` defaults to DRY-RUN reporting.
Mutation (writing normalized bytes to disk) requires the caller to pass
`mutate: true` explicitly. An op that mutates a tree the caller names should
report by default, not act by default.

Known trap for the test surface (recorded here, not just in the plan, since
a future editor of this module will hit it too): after a working-tree
normalize, `git status --porcelain` keeps showing the file as `M` while
`git diff` is empty -- stat-cache staleness that `--no-optional-locks`
cannot persist a refresh for. Do not assert on porcelain rows after a repair;
assert on the file's bytes or on `git diff --stat` output instead.

Target resolution: `target_root` wire param, path-guarded via
`coordinator_core.cartography._guard.path_guard` -- the injected `repo_root`
argument is ignored entirely (this plan's anti-scope: "Do not resolve the
target root from the environment" / AC7).

Bytes end to end: no `text=True` subprocess call, no text-mode file read or
write anywhere on this path (`Path.read_bytes()` / `Path.write_bytes()`
only).

Negative-spec:
  - Does NOT normalize toward LF unconditionally -- direction is the
    DECLARED attribute, both ways (AC5).
  - Does NOT repair a file git reports dirty.
  - Does NOT repair a file whose normalized content would not byte-match its
    index blob.
  - Does NOT mutate anything unless `mutate: true` is explicitly passed
    (default is dry-run reporting).
  - Does NOT spawn a subprocess per candidate file -- the index-blob read is
    one batched `git cat-file --batch` call over every repair candidate.

Spec backlink: docs/plans/2026-08-20-every-repo-detects-its-own-eol-drift.md § C3, AC4, AC5
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.cartography._guard import path_guard
from coordinator_core.ipc import register_op
from coordinator_core.ops.eol.census import census as _census
from coordinator_core.win_portability import no_console_creationflags


def _normalize(content: bytes, declared: str) -> bytes:
    """The bytes to WRITE to disk, normalized toward `declared` ("lf" or
    "crlf"). Callers only invoke this for census-reported violations, so the
    input is known to disagree with `declared` in exactly the direction
    census checked.
    """
    if declared == "lf":
        return content.replace(b"\r\n", b"\n")
    return content.replace(b"\n", b"\r\n").replace(b"\r\r\n", b"\r\n")


def _normalize_for_index_check(content: bytes) -> bytes:
    """The form to compare against the INDEX blob, independent of declared
    direction. Git's own checkin-time text normalization always stores a
    text-attributed blob LF-normalized -- `eol=` only steers CHECKOUT
    rendering, never index storage (confirmed against a real tree: an
    `eol=crlf` path with a clean, already-committed LF body still shows
    "LF will be replaced by CRLF" from git itself, i.e. the index blob is
    LF, not CRLF). Comparing the declared-direction WRITE bytes straight
    against the index blob would therefore refuse every `eol=crlf` repair
    unconditionally -- not a hard refusal on content risk, just a wrong
    predicate. The correct proof of "EOL-only" is index-blob-equals-
    LF-normalized-content, regardless of which direction the disk write
    goes.
    """
    return content.replace(b"\r\n", b"\n")


def _index_blobs(root: Path, rels: List[str]) -> Dict[str, Optional[bytes]]:
    """One batched `git cat-file --batch` call resolving each `:<rel>`
    (the index/stage-0 blob for that path) to its raw bytes. A path missing
    from the index, or any parse/spawn failure, maps to None rather than
    raising -- the caller folds that to a skip.
    """
    if not rels:
        return {}
    git_bin = shutil.which("git")
    if git_bin is None:
        return {r: None for r in rels}
    stdin_payload = b"".join(
        f":{r}\n".encode("utf-8", errors="surrogateescape") for r in rels
    )
    try:
        proc = subprocess.run(
            [git_bin, "-C", str(root), "cat-file", "--batch"],
            input=stdin_payload,
            capture_output=True,
            timeout=30,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return {r: None for r in rels}
    if proc.returncode != 0:
        stderr = proc.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        print(
            f"eol.repair: git -C {root} cat-file --batch exited "
            f"{proc.returncode}: {(stderr or '').strip()}",
            file=sys.stderr,
        )
        return {r: None for r in rels}

    out = proc.stdout
    pos = 0
    result: Dict[str, Optional[bytes]] = {}
    try:
        for r in rels:
            nl = out.index(b"\n", pos)
            header = out[pos:nl]
            pos = nl + 1
            parts = header.split(b" ")
            if parts[-1] == b"missing":
                result[r] = None
                continue
            size = int(parts[2])
            result[r] = out[pos:pos + size]
            pos += size + 1  # skip the trailing newline after the object body
    except (ValueError, IndexError):
        # Malformed/truncated batch output -- fold every remaining path to
        # "unresolved" rather than raising out of a report-shaped op.
        for r in rels:
            result.setdefault(r, None)
    return result


def repair(root: Path, mutate: bool = False) -> dict:
    """Pure, synchronous core: census `root` for violations, then attempt to
    repair every non-dirty candidate whose normalized content is
    byte-identical to its index blob. See module docstring for the full
    contract.
    """
    census_result = _census(root)
    violations = census_result["violations"]

    candidates = [v for v in violations if not v["dirty"]]
    skipped: List[dict] = [
        {"path": v["path"], "reason": "dirty"} for v in violations if v["dirty"]
    ]

    declared_by_path: Dict[str, str] = {v["path"]: v["declared"] for v in candidates}
    write_bytes_by_path: Dict[str, bytes] = {}
    check_bytes_by_path: Dict[str, bytes] = {}
    read_error_paths: List[str] = []
    for v in candidates:
        rel = v["path"]
        fs_path = root / rel
        try:
            content = fs_path.read_bytes()
        except OSError:
            read_error_paths.append(rel)
            continue
        write_bytes_by_path[rel] = _normalize(content, v["declared"])
        check_bytes_by_path[rel] = _normalize_for_index_check(content)

    for rel in read_error_paths:
        skipped.append({"path": rel, "reason": "read-error"})

    blob_rels = list(write_bytes_by_path.keys())
    index_blobs = _index_blobs(root, blob_rels)

    repaired: List[dict] = []
    for rel in blob_rels:
        blob = index_blobs.get(rel)
        if blob is None:
            skipped.append({"path": rel, "reason": "index-blob-missing"})
            continue
        if blob != check_bytes_by_path[rel]:
            skipped.append({"path": rel, "reason": "index-blob-mismatch"})
            continue
        if mutate:
            try:
                (root / rel).write_bytes(write_bytes_by_path[rel])
            except OSError:
                skipped.append({"path": rel, "reason": "write-error"})
                continue
        repaired.append({"path": rel, "declared": declared_by_path[rel]})

    skipped.sort(key=lambda s: s["path"])
    repaired.sort(key=lambda r: r["path"])
    return {
        "dry_run": not mutate,
        "repaired": repaired,
        "repaired_count": len(repaired),
        "skipped": skipped,
        "skipped_count": len(skipped),
        "violation_count": len(violations),
    }


@register_op("eol.repair")
def _eol_repair(params: dict, repo_root: Optional[Path] = None) -> dict:
    """"eol.repair" handler -- normalizes tracked files under any
    caller-supplied `target_root` toward their declared `eol` attribute. See
    module docstring for the full contract.

    Wire params:
        target_root (str, required) -- root of the tree to repair. Must
                                        resolve inside a git worktree.
        mutate       (bool, optional, default False) -- when False (the
                                        default), reports what WOULD be
                                        repaired without writing anything.
                                        When True, writes normalized bytes to
                                        every eligible candidate.

    Returns:
        target_root     (str)  -- resolved target_root, echoed.
        dry_run          (bool) -- `not mutate`.
        repaired          (list) -- sorted [{path, declared}, ...] -- files
                                    that were (or, in dry-run, would be)
                                    normalized.
        repaired_count     (int)  -- len(repaired).
        skipped            (list) -- sorted [{path, reason}, ...].
        skipped_count       (int)  -- len(skipped).
        violation_count      (int)  -- total census violations considered
                                       (repaired_count + skipped_count).

    `repo_root` (injected by the engine) is ignored entirely -- the target is
    resolved solely from `target_root`, per this plan's anti-scope ("Do not
    resolve the target root from the environment") and AC7.

    Raises ValueError if target_root is missing; propagates
    coordinator_core.cartography._guard.PathEscapeError if target_root
    cannot be resolved inside a git worktree.
    """
    target_root = params.get("target_root")
    if not target_root:
        raise ValueError("eol.repair requires param: target_root")

    mutate = bool(params.get("mutate", False))
    guarded_root = path_guard(target_root, ".")
    result = repair(guarded_root, mutate=mutate)
    result["target_root"] = str(guarded_root)
    return result
