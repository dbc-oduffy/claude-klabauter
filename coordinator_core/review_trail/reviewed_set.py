"""
coordinator_core.review_trail.reviewed_set

Purpose: the reviewed-set STORE — a flat, append-only, per-clone file of
reviewed commit SHAs, plus a companion append-only file of review-trail
record ids already folded into it. Answers "which commits carry a review
stamp?" as a resident file read (0.004ms warm, 3.40ms cold-binary-read +
frozenset, flat at 10x, zero spawns) instead of `coverage.py ::
build_reviewed_set`'s per-call `git rev-list` recomputation.

Both files live under `.git/coordinator-review-trail/` — per-clone and
gitignored by construction (nothing under `.git/` is ever tracked). NOT a
tracked artifact: the records under `state/review-trail/` are the truth: this
store is a derived union, and a tracked file appended on every write across
~50 concurrent sessions would buy a merge surface on an artifact with no
authority of its own. See the plan's `pm_resolution.storage_shape`.

Two files, two shapes:
  * `reviewed-shas`      — one 40-hex commit SHA per line (fixed-width
                            41-byte records: 40 hex chars + `\\n`).
  * `folded-record-ids`  — one opaque record-id string per line (caller-
                            supplied; this module has no opinion on how a
                            record id is derived — that is a C2 concern,
                            e.g. a trail file path or path#index).

Resolution happens at WRITE time (fold-in), never at read time — decided by
measurement, not left open (finding 3; the EM measured read-time fold-in at
93.8-343.8ms / 3-13 spawns against a 78.1ms bar, and it is REFUSED on those
numbers; write-time resolution measured 56-59ms / 3 procs per record, flat
from a 1-commit to a 200-commit range). See the chunk brief
(state/dispatch-briefs/2026-08-27-the-reviewed-set-is-a-file-not-a-computation/C1.md)
for the full numbers table. The read path here holds NO unresolved ranges
and never spawns a subprocess.

Fold-in write ordering is load-bearing (finding 1): `fold_in` appends the
newly-resolved SHAs and flushes FIRST, then records the corresponding
record ids as folded SECOND. A crash between the two steps leaves the SHAs
already durable and the record id NOT yet marked folded — the only possible
outcome is a redundant, idempotent re-fold of already-present SHAs (a set
union tolerates re-adding a member) on the next `fold_in` call, never the
reverse (a record marked folded before its SHAs land, silently losing them
forever, since fold-in only ever considers ids absent from
`folded-record-ids`).

Endpoint normalization is load-bearing (finding 2): every range endpoint is
resolved via `git rev-parse` to a full 40-hex SHA and checked against the
`git rev-list --all --parents` reach-set BEFORE its range is folded. An
endpoint that fails resolution (abbreviated SHA that does not expand, a
malformed `^N` beyond the commit's parent count) or resolves but is absent
from the reach-set (present in the object DB but unreachable from any ref,
since the reach-set is built with `--all`) leaves the WHOLE record
unresolved — it is never folded as the empty set, and is retried on the
next `fold_in` call. This directly matches `docs/wiki/coverage-gate-perf.md`
(lines 89-130): an abbreviated-SHA endpoint and a malformed/foreign endpoint
both silently collapsing to the empty set against a full-SHA-keyed map are
two prior production incidents of exactly this shape.

Concurrency and durability (finding 9): the store is a shared file on a box
running ~50 concurrent sessions, and Windows gives no POSIX below-PIPE_BUF
append atomicity. Records are fixed-width (41 bytes: 40 hex + `\\n`) and
each fold batch is a single `O_APPEND` write; the reader discards any
`\\n`-delimited fragment that is not exactly 40 lowercase-hex characters,
so a torn line from an interleaved concurrent write is silently dropped —
never trusted as a spurious member.

Negative-spec:
    - Does NOT credit anything — no verdict filter, no kind partition, no
      foreign-session narrowing, no stored-HEAD exclusion. Those five credit
      rules (`_verdict_counts`, `_record_range_has_stored_head`,
      `_credit_from_kind_partition`, `_narrow_foreign_session_scope`, the
      never-path-scoped asymmetric scope rule) are preserved BY SYMBOL in
      `coordinator_core.coverage` and applied by the CALLER before a
      (record_id, sha_range) pair ever reaches `fold_in` — this module is
      the store, not the gate.
    - Does NOT write to a tracked path. `.git/coordinator-review-trail/` is
      per-clone and gitignored by construction; nothing under `.git/` is
      ever tracked.
    - Does NOT resolve ranges at read time. `read_reviewed_set` never spawns
      a subprocess — see AC1.
    - Does NOT treat an unresolvable endpoint as the empty set. See
      "Endpoint normalization is load-bearing" above.

Spec backlink: docs/plans/2026-08-27-the-reviewed-set-is-a-file-not-a-computation.md § C1
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from coordinator_core.git.run import run_git
from coordinator_core.locked_write import held_lock
from coordinator_core.lifecycle import git_common_dir

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

_SHA_RECORD_WIDTH = 41

_STORE_SUBDIR = ("coordinator-review-trail",)
_SHAS_FILENAME = "reviewed-shas"
_FOLDED_IDS_FILENAME = "folded-record-ids"


def _run(args: List[str], cwd: str) -> Tuple[int, str, str]:
    result = run_git(args, cwd=cwd)
    return result.returncode, result.stdout, result.stderr


def store_dir(repo_root: str) -> Path:
    return git_common_dir(Path(repo_root)).joinpath(*_STORE_SUBDIR)


def _shas_path(repo_root: str) -> Path:
    return store_dir(repo_root) / _SHAS_FILENAME


def _folded_ids_path(repo_root: str) -> Path:
    return store_dir(repo_root) / _FOLDED_IDS_FILENAME


_SHAS_CACHE: Dict[str, Tuple[int, int, FrozenSet[str]]] = {}
_FOLDED_IDS_CACHE: Dict[str, Tuple[int, int, FrozenSet[str]]] = {}


def _parse_sha_lines(data: bytes) -> FrozenSet[str]:
    result: Set[str] = set()
    for line in data.split(b"\n"):
        if len(line) == 40:
            try:
                sha = line.decode("ascii")
            except UnicodeDecodeError:
                continue
            if _SHA_RE.match(sha):
                result.add(sha)
    return frozenset(result)


def _parse_id_lines(data: bytes) -> FrozenSet[str]:
    result: Set[str] = set()
    for line in data.split(b"\n"):
        if not line:
            continue
        try:
            rid = line.decode("utf-8")
        except UnicodeDecodeError:
            continue
        result.add(rid)
    return frozenset(result)


def _read_resident(
    path: Path, cache: Dict[str, Tuple[int, int, FrozenSet[str]]], parser,
) -> FrozenSet[str]:
    key = str(path)
    try:
        st = os.stat(path)
    except OSError:
        cache.pop(key, None)
        return frozenset()
    cached = cache.get(key)
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    try:
        data = path.read_bytes()
    except OSError:
        cache.pop(key, None)
        return frozenset()
    parsed = parser(data)
    cache[key] = (st.st_mtime_ns, st.st_size, parsed)
    return parsed


def read_reviewed_set(repo_root: str) -> FrozenSet[str]:
    return _read_resident(_shas_path(repo_root), _SHAS_CACHE, _parse_sha_lines)


def read_folded_record_ids(repo_root: str) -> FrozenSet[str]:
    return _read_resident(_folded_ids_path(repo_root), _FOLDED_IDS_CACHE, _parse_id_lines)


# Durable append — fixed-width records, single O_APPEND write per batch.


def _append_lines(path: Path, lines: List[str]) -> None:
    """Append `lines` (already newline-terminated by the caller) to `path`
    in ONE `O_APPEND` write, then `os.fsync`. A no-op if `lines` is empty —
    never opens/creates the file for zero content, so a fold-in batch that
    resolves nothing leaves no trace."""
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(lines).encode("utf-8")
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    flags |= getattr(os, "O_BINARY", 0)
    fd = os.open(str(path), flags, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _append_shas(repo_root: str, shas: Set[str]) -> None:
    lines = [sha + "\n" for sha in sorted(shas) if _SHA_RE.match(sha)]
    if not lines:
        return
    assert all(len(line) == _SHA_RECORD_WIDTH for line in lines), (
        "reviewed-shas record width drifted from the fixed 40-hex+\\n shape "
        "the reader's torn-line defence assumes"
    )
    # Cross-process serialization (finding 9): Windows' O_APPEND is NOT a
    # kernel-atomic append the way POSIX below-PIPE_BUF appends are — two
    with held_lock(_shas_path(repo_root), anchor_root=Path(repo_root)):
        _append_lines(_shas_path(repo_root), lines)


def _append_folded_ids(repo_root: str, record_ids: List[str]) -> None:
    lines = [rid + "\n" for rid in record_ids if rid]
    if not lines:
        return
    with held_lock(_folded_ids_path(repo_root), anchor_root=Path(repo_root)):
        _append_lines(_folded_ids_path(repo_root), lines)


def _split_range(sha_range: str) -> Optional[Tuple[str, str]]:
    if "..." in sha_range:
        left, right = sha_range.split("...", 1)
    elif ".." in sha_range:
        left, right = sha_range.split("..", 1)
    else:
        return None
    if not left or not right:
        return None
    return left, right


def _build_reach_set(repo_root: str) -> Optional[FrozenSet[str]]:
    rc, out, _err = _run(["rev-list", "--all", "--parents"], cwd=repo_root)
    if rc != 0:
        return None
    shas: Set[str] = set()
    for line in out.splitlines():
        parts = line.split()
        if parts:
            shas.add(parts[0])
    return frozenset(shas)


def _batch_check(exprs: List[str], repo_root: str) -> List[str]:
    result = run_git(
        ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
        input=("\n".join(exprs) + "\n").encode("utf-8"),
        cwd=repo_root,
    )
    return result.stdout.splitlines() if result.returncode == 0 else []


def _resolve_endpoints_batch(tokens: List[str], repo_root: str) -> Dict[str, Optional[str]]:
    result: Dict[str, Optional[str]] = {tok: None for tok in tokens}
    safe = [tok for tok in tokens if "\n" not in tok and "\r" not in tok]
    if not safe:
        return result
    lines = _batch_check([tok + "^{commit}" for tok in safe], repo_root)
    for tok, line in zip(safe, lines):
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "commit" and _SHA_RE.match(parts[0]):
            result[tok] = parts[0]
    return result


@dataclass
class FoldResult:
    """Outcome of one `fold_in` call.

    `folded_record_ids`   — record ids successfully resolved and durably
                             folded (their SHAs are on disk before this
                             list is populated — see the write-ordering
                             note on `fold_in`).
    `unresolved_record_ids` — record ids left UNFOLDED: either the range
                             did not parse, an endpoint failed resolution,
                             an endpoint is absent from the reach-set (not
                             reachable from any ref), or the range itself
                             failed to resolve via `git rev-list`. Never
                             folded as the empty set — retried on the next
                             `fold_in` call.
    `new_shas`             — the SHAs newly appended to the store by this
                             call (may be empty even when records folded,
                             if every resolved SHA was already present).
                             Best-effort under concurrency, not authoritative
                             telemetry: `existing` is read before
                             `_append_shas` takes its lock, so a concurrent
                             `fold_in` in another process can durably append
                             one of these SHAs between the read and this
                             call's own write, and it is still reported here
                             as "new". The store's own durability is
                             unaffected (a duplicate line dedupes on read
                             into a frozenset) — only this return value can
                             over-report.
    """

    folded_record_ids: List[str] = field(default_factory=list)
    unresolved_record_ids: List[str] = field(default_factory=list)
    new_shas: FrozenSet[str] = frozenset()


def fold_in(repo_root: str, records: List[Tuple[str, str]]) -> FoldResult:
    if not records:
        return FoldResult()

    reach_set = _build_reach_set(repo_root)
    if reach_set is None:
        return FoldResult(unresolved_record_ids=[rid for rid, _ in records])

    parsed: Dict[str, Tuple[str, str]] = {}
    unresolved: List[str] = []
    tokens: Set[str] = set()
    for record_id, sha_range in records:
        split = _split_range(sha_range)
        if split is None:
            unresolved.append(record_id)
            continue
        parsed[record_id] = split
        tokens.add(split[0])
        tokens.add(split[1])

    endpoint_shas = _resolve_endpoints_batch(sorted(tokens), repo_root)

    folded: List[str] = []
    collected_shas: Set[str] = set()
    eligible: List[Tuple[str, str]] = []
    for record_id, sha_range in records:
        split = parsed.get(record_id)
        if split is None:
            continue
        left, right = split
        left_sha = endpoint_shas.get(left)
        right_sha = endpoint_shas.get(right)
        if left_sha is None or right_sha is None:
            unresolved.append(record_id)
            continue
        if left_sha not in reach_set or right_sha not in reach_set:
            unresolved.append(record_id)
            continue
        eligible.append((record_id, sha_range))

    # ONE `git rev-list` SPAWN PER RANGE, DELIBERATELY — do not "batch it into a single
    # to `B D ^A ^C`, and every exclusion applies GLOBALLY, so `^A` also strips A's
    for record_id, sha_range in eligible:
        rc, out, _err = _run(["rev-list", sha_range], cwd=repo_root)
        if rc != 0:
            unresolved.append(record_id)
            continue
        collected_shas.update(s.strip() for s in out.splitlines() if s.strip())
        folded.append(record_id)

    existing = read_reviewed_set(repo_root)
    new_shas = frozenset(collected_shas - existing)

    _append_shas(repo_root, new_shas)
    _append_folded_ids(repo_root, folded)

    return FoldResult(
        folded_record_ids=folded,
        unresolved_record_ids=unresolved,
        new_shas=new_shas,
    )
