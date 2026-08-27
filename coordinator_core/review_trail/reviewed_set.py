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
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.locked_write import held_lock

#: 40 lowercase-hex-char commit SHA — the only line shape the reader trusts.
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: Fixed record width for `reviewed-shas`: 40 hex chars + one `\n`.
_SHA_RECORD_WIDTH = 41

_NO_CONSOLE = no_console_creationflags()

_STORE_SUBDIR = ("coordinator-review-trail",)
_SHAS_FILENAME = "reviewed-shas"
_FOLDED_IDS_FILENAME = "folded-record-ids"


def _run(cmd: List[str], cwd: str) -> Tuple[int, str, str]:
    """Run `cmd`; return (returncode, stdout, stderr). Never raises — a
    spawn failure (missing git, timeout, etc.) degrades to (1, "", msg),
    the same fail-closed shape every caller here already treats an
    unresolved endpoint/range as."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            **_NO_CONSOLE,
        )
        return result.returncode, result.stdout, result.stderr
    except Exception as exc:  # pragma: no cover - defensive, mirrors coverage.py's _run
        return 1, "", str(exc)


def store_dir(repo_root: str) -> Path:
    """The per-clone store directory — under `.git/`, never tracked."""
    return Path(repo_root).joinpath(".git", *_STORE_SUBDIR)


def _shas_path(repo_root: str) -> Path:
    return store_dir(repo_root) / _SHAS_FILENAME


def _folded_ids_path(repo_root: str) -> Path:
    return store_dir(repo_root) / _FOLDED_IDS_FILENAME


# ---------------------------------------------------------------------------
# Resident read — AC1: zero added spawns, os.stat revalidation.
# ---------------------------------------------------------------------------

#: (path -> (mtime_ns, size, frozenset(...))) — module-level so the resident
#: cache survives across calls within one process, revalidated on every read
#: via a single `os.stat` (mtime_ns + size), never trusted stale.
_SHAS_CACHE: Dict[str, Tuple[int, int, FrozenSet[str]]] = {}
_FOLDED_IDS_CACHE: Dict[str, Tuple[int, int, FrozenSet[str]]] = {}


def _parse_sha_lines(data: bytes) -> FrozenSet[str]:
    """Split on `\\n` and keep only fragments that are exactly 40 lowercase
    hex characters. This is deliberately NOT fixed-offset (byte[i:i+41])
    slicing — a torn concurrent write need not land on a 41-byte boundary,
    and splitting on the delimiter itself degrades a torn line to "does not
    match the shape", which is dropped, regardless of where the tear fell.
    """
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
    """Split on `\\n`; keep every non-empty fragment as an opaque record id.
    Record ids are caller-supplied strings (not a fixed shape like a SHA),
    so the only defence available against a torn line is emptiness/decode
    failure — both are dropped rather than trusted."""
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
    """Return the resident, revalidated set of reviewed commit SHAs.

    Zero spawns (AC1). Revalidates against the on-disk file with a single
    `os.stat` (mtime_ns + size); re-parses only when either has changed
    since the last read in this process. Returns the empty set if the
    store file does not yet exist — never an error, since "no reviews
    folded yet" is a legitimate initial state.
    """
    return _read_resident(_shas_path(repo_root), _SHAS_CACHE, _parse_sha_lines)


def read_folded_record_ids(repo_root: str) -> FrozenSet[str]:
    """Return the resident, revalidated set of record ids already folded
    into the reviewed-set store. Zero spawns; same resident-read shape as
    `read_reviewed_set`."""
    return _read_resident(_folded_ids_path(repo_root), _FOLDED_IDS_CACHE, _parse_id_lines)


# ---------------------------------------------------------------------------
# Durable append — fixed-width records, single O_APPEND write per batch.
# ---------------------------------------------------------------------------


def _append_lines(path: Path, lines: List[str]) -> None:
    """Append `lines` (already newline-terminated by the caller) to `path`
    in ONE `O_APPEND` write, then `os.fsync`. A no-op if `lines` is empty —
    never opens/creates the file for zero content, so a fold-in batch that
    resolves nothing leaves no trace."""
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(lines).encode("ascii", errors="strict")
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
    # Cross-process serialization (finding 9): Windows' O_APPEND is NOT a
    # kernel-atomic append the way POSIX below-PIPE_BUF appends are — two
    # concurrent writers can compute the same end-of-file offset and one
    # clobbers the other's fully-written record, a genuine LOST write, not
    # merely a torn line the reader's line-shape filter can catch. The
    # reader's discard-malformed-line defence protects against a torn
    # write; it cannot recover a write that never reached disk. `held_lock`
    # (coordinator_core.locked_write) is the repo's existing cross-process
    # exclusive-advisory-lock primitive (flock/msvcrt.locking, kernel-
    # enforced, auto-released on process death) — anchored at `repo_root`
    # itself, same pattern as the `touched.txt` writers in
    # `session/claims.py`/`session/scope.py`, since this store's target
    # lives inside the CALLER's own repo, not a foreign one.
    with held_lock(_shas_path(repo_root), anchor_root=Path(repo_root)):
        _append_lines(_shas_path(repo_root), lines)


def _append_folded_ids(repo_root: str, record_ids: List[str]) -> None:
    lines = [rid + "\n" for rid in record_ids if rid]
    if not lines:
        return
    with held_lock(_folded_ids_path(repo_root), anchor_root=Path(repo_root)):
        _append_lines(_folded_ids_path(repo_root), lines)


# ---------------------------------------------------------------------------
# Endpoint normalization + range resolution (write-time only).
# ---------------------------------------------------------------------------


def _split_range(sha_range: str) -> Optional[Tuple[str, str]]:
    """Split a `LEFT..RIGHT` or `LEFT...RIGHT` range into its two endpoint
    tokens. Returns None for anything that does not carry a `..` — this
    module never folds a bare single ref (no left endpoint to bound it)."""
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
    """The `git rev-list --all --parents` reach-set map (AC2): every commit
    SHA reachable from any ref. Built ONCE per `fold_in` batch and shared
    across every record's endpoint check in that batch — this is the flat-
    cost lever the write-time measurement table cites (56-59ms, 3 procs
    per record, flat from a 1-commit range to a 200-commit one).

    Returns None on a git failure (never a partial/empty set mistaken for
    "nothing is reachable") — callers treat None as "cannot resolve
    anything this batch", leaving every record unresolved rather than
    folding a wrong empty-set answer."""
    rc, out, _err = _run(["git", "rev-list", "--all", "--parents"], cwd=repo_root)
    if rc != 0:
        return None
    shas: Set[str] = set()
    for line in out.splitlines():
        parts = line.split()
        if parts:
            shas.add(parts[0])
    return frozenset(shas)


def _batch_check(exprs: List[str], repo_root: str) -> List[str]:
    """One `git cat-file --batch-check` spawn over `exprs`, fed via stdin, returning
    its stdout lines. Deliberately NOT built on `_run`: `--batch-check` is the one
    git subcommand here that must WRITE to stdin, which `_run`'s
    `stdin=subprocess.DEVNULL` (the pinned Windows hang fix) cannot support. Mirrors
    `_run`'s portability flag without inheriting its stdin behaviour.

    Not imported from `coordinator_core.coverage._batch_check_hex_tokens`, which is
    the same mechanism: that module is heavy and this one sits on the review-trail
    path that `test_hot_path_hook_import_budget` polices, so the ~10 lines are paid
    here rather than its import weight. The output-shape guarantee below is that
    function's, empirically verified there (code-reviewer item 3 + EM follow-up,
    2026-07-28) and unchanged since."""
    try:
        result = subprocess.run(
            ["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
            input="\n".join(exprs) + "\n",
            cwd=repo_root,
            capture_output=True,
            text=True,
            **_NO_CONSOLE,
        )
        return result.stdout.splitlines() if result.returncode == 0 else []
    except Exception:
        return []


def _resolve_endpoints_batch(tokens: List[str], repo_root: str) -> Dict[str, Optional[str]]:
    """Resolve each token in `tokens` to its full 40-hex SHA in ONE `git cat-file
    --batch-check` spawn (handles abbreviated SHAs, `^N`/`~N` ancestry suffixes, and
    symbolic refs uniformly, since `--batch-check` accepts any rev expression).

    WAS one `git rev-parse --verify --quiet` spawn per distinct token, on a measured
    and correct objection that no longer applies to the primitive now used: `git
    rev-parse` does NOT reliably emit one stdout line per argument on a mixed batch
    — an unresolvable arg (e.g. `^N` beyond a root commit's parent count) truncates
    the whole batch's stdout after the failing arg, silently misaligning any
    positional zip against the remaining tokens (measured directly against this
    repo's git). That is a `rev-parse` property, not a batching property.
    `--batch-check` emits EXACTLY ONE stdout line per stdin expression, in input
    order, with failures reported in-band ("<expr> missing", "<expr> ambiguous") and
    diagnostics confined to stderr — so the alignment the old docstring rightly
    refused to trust is guaranteed here rather than assumed. `zip` still truncates
    to the shorter side, so a short read degrades to unresolved, never to a
    misattributed SHA.

    A token that fails to resolve maps to None — unresolved, never guessed. This
    preserves the fix for the abbreviated-SHA and malformed-`^N` incident classes
    named in this module's docstring: resolution is gated on the reported object
    type being `commit` AND the objectname being a full 40-hex SHA, so an ambiguous
    prefix comes back None rather than as a resolved SHA.

    Token count per record is fixed at 2 regardless of the range's commit span, and
    the spawn count is now fixed at 1 regardless of token count — strictly stronger
    than the flat-cost property the write-time measurement depends on."""
    result: Dict[str, Optional[str]] = {tok: None for tok in tokens}
    # A token carrying an embedded newline would be TWO stdin lines and draw TWO output
    # lines, shifting every later token by one and handing it another token's SHA. Verified
    # exploitable (review, 2026-08-27): a valid 40-hex commit SHA was attributed to the wrong
    # token this way. Positional framing is what makes the one-spawn form safe, so anything
    # that can break the framing is refused BEFORE it reaches stdin rather than validated
    # after — a misattributed SHA is written to a durable store and never noticed, while an
    # unresolved one only marks its record unresolved. `\r` is refused with `\n` because
    # `text=True` newline translation makes a lone `\r` a line terminator on this platform.
    # Nothing legitimate reaches here with either: these are `L..R` endpoints off a record.
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
    """

    folded_record_ids: List[str] = field(default_factory=list)
    unresolved_record_ids: List[str] = field(default_factory=list)
    new_shas: FrozenSet[str] = frozenset()


def fold_in(repo_root: str, records: List[Tuple[str, str]]) -> FoldResult:
    """Resolve and fold a batch of (record_id, sha_range) pairs into the
    store.

    Callers (C2) are responsible for applying the five preserved credit
    rules and for excluding record ids already in `read_folded_record_ids`
    — this function folds whatever it is given, unconditionally.

    Write ordering is load-bearing (finding 1): the newly-resolved SHAs
    are appended and flushed FIRST; only THEN are the corresponding
    record ids appended to `folded-record-ids`. A crash between the two
    steps leaves the SHAs durable and the record id absent from
    `folded-record-ids` — the next `fold_in` call re-resolves and
    re-folds that record, which is an idempotent no-op on the SHA side
    (set union tolerates re-adding a member) and simply completes the
    interrupted fold. The reverse ordering would let a record be marked
    folded before its SHAs landed, silently losing them forever.

    A record whose sha_range does not parse, whose endpoint(s) fail
    resolution, or whose endpoint(s) resolve but are absent from the
    `git rev-list --all --parents` reach-set, is placed in
    `unresolved_record_ids` and contributes NOTHING to the store — it is
    never folded as the empty set (AC2).
    """
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
    # Ranges whose endpoints both resolved AND are both reachable — the only ones a
    # rev-list is worth spawning for. Everything else is already in `unresolved`.
    eligible: List[Tuple[str, str]] = []
    for record_id, sha_range in records:
        split = parsed.get(record_id)
        if split is None:
            continue  # already in `unresolved` above
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
    # call", which is what the amplification gate's generic advice says here and what a
    # first attempt at this actually did (2026-08-27) before a live-corpus check caught
    # it. `git rev-list A..B C..D` does NOT emit the union of two ranges: ranges desugar
    # to `B D ^A ^C`, and every exclusion applies GLOBALLY, so `^A` also strips A's
    # ancestors out of C..D. Measured on this repo: two ranges yielded 3 SHAs batched
    # against 5 per-range, rc=0 both ways. Batching here silently DROPS reviewed SHAs —
    # a wrong verdict in the review-attribution path, produced with no error to notice.
    #
    # The flat-cost property this module depends on is per-RECORD, not per-repo: token
    # count per record is fixed at 2 regardless of a range's commit span, and endpoint
    # resolution for every record is already collapsed to ONE `git cat-file
    # --batch-check` spawn in `_resolve_endpoints_batch` above. What remains is one
    # spawn per record that actually has a resolvable, reachable range — a correctness
    # floor for this primitive, in the same class as `explicit_stage`'s retained
    # `git check-ignore`.
    for record_id, sha_range in eligible:
        rc, out, _err = _run(["git", "rev-list", sha_range], cwd=repo_root)
        if rc != 0:
            unresolved.append(record_id)
            continue
        collected_shas.update(s.strip() for s in out.splitlines() if s.strip())
        folded.append(record_id)

    existing = read_reviewed_set(repo_root)
    new_shas = frozenset(collected_shas - existing)

    # Write ordering (finding 1): SHAs first and flushed, THEN folded ids.
    _append_shas(repo_root, new_shas)
    _append_folded_ids(repo_root, folded)

    return FoldResult(
        folded_record_ids=folded,
        unresolved_record_ids=unresolved,
        new_shas=new_shas,
    )
