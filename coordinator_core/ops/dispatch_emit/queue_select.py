"""
coordinator_core.ops.dispatch_emit.queue_select — queue rows -> one frozen manifest.

Purpose: the queue-grind engine's row-selection seam (docs/plans/2026-09-21-
bug-blitz-emitter-engine-leg.md § Design § Selector, Tasks § C2). `select_rows`
turns one or more queue directories into a `Manifest` — an ordered, digested,
batch-keyed row set — with zero process spawns and one read per row. It is
the substitute for a plan spine when the emitter composes a queue grind
instead of a wave-derived script.

Reuse, not re-parse: row bytes go through
`coordinator_core.frontmatter.schema_validate.parse_yaml`, the same lenient
parser `records_query`/`queue_family` use — never `yaml.safe_load`. The
`where`/reserved-key/source-op vocabularies come from
`coordinator_core.contract.grind_vocab` — this module owns no closed-set
literal of its own for any of them.

Negative-spec:
  - Does NOT glob or walk — one `os.listdir` per named queue directory,
    sorted, no recursion.
  - Does NOT parse a CLI mini-language for `where` — the single JSON DNF
    grammar is the only one (overengineering-reviewer #3).
  - Does NOT silently skip an unparseable row — every row a directory lists
    is accounted for; a parse failure is a named `ValueError`, never a
    silent drop the way `records_query`'s `_load_record` skips a bad file.
  - Does NOT re-read a live row after freezing the manifest — staleness
    detection is `grind-row check`'s job (C4), not this module's.
  - Does NOT spawn a subprocess anywhere in its read/where/batch/ledger path.

Spec backlink: docs/plans/2026-09-21-bug-blitz-emitter-engine-leg.md § Design
§ Selector, Tasks § C2.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from coordinator_core.contract import grind_vocab
from coordinator_core.frontmatter import schema_validate

__all__ = [
    "ManifestEntry",
    "DeclinedEntry",
    "SourceOpResult",
    "Manifest",
    "select_rows",
    "RouteToRefusedError",
    "DuplicateStemError",
    "UnparseableRowError",
    "WhereTermError",
    "UnknownSourceOpError",
    "MissingRowIdError",
    "DuplicateRowIdError",
]


class UnparseableRowError(ValueError):
    """Raised for a queue row whose bytes do not parse as YAML — never a silent skip."""


class WhereTermError(ValueError):
    """Raised for a `where` term outside the closed DNF grammar, naming the term."""


class DuplicateStemError(ValueError):
    """Raised when `row_id_key == '@stem'` and two queue dirs yield the same stem."""


class RouteToRefusedError(ValueError):
    """Raised for a row whose ledger carries a `route-to-*` hand-back or mark."""


class UnknownSourceOpError(ValueError):
    """Raised when `source["op"]` names an op outside `grind_vocab.SOURCE_OPS`."""


class MissingRowIdError(ValueError):
    """Raised when `row_id_key` (not `'@stem'`) names a field a row does not
    carry — never silently coerced to the literal string `"None"`, which
    would make every row missing the field collide onto one shared id (and
    one shared ledger file)."""


class DuplicateRowIdError(ValueError):
    """Raised when two rows resolve to the same `row_id` under a
    profile-declared `row_id_key` (not `'@stem'`, which has its own
    `DuplicateStemError`) — sharing a ledger file cross-contaminates fold-in,
    skip and settle between two otherwise-unrelated rows."""


_SENTINEL_ABSENT = object()


@dataclass(frozen=True)
class ManifestEntry:
    """One frozen row: `{row_id, path, digest, batch_key}` plus fold-in state.

    `skip_stages` is the ledger fold-in's answer to "what has this row
    already passed" — non-empty only when the ledger's last digest for
    `row_id` equals `digest` (§ Design § Selector, "Ledger fold-in").
    """

    row_id: str
    path: str
    digest: str
    batch_key: str
    skip_stages: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeclinedEntry:
    """One row EXCLUDED from the manifest rather than raising — a
    `route-to-*` ledger mark or hand-back mark (DR-404: never re-entered
    into another selector, but a re-emit/resume must not fail because of
    it)."""

    row_id: str
    path: str
    reason: str


@dataclass(frozen=True)
class SourceOpResult:
    """`{source op, args, output sha256}` — recorded only when `source` is set."""

    op: str
    args: Mapping[str, Any]
    output_sha256: str


@dataclass(frozen=True)
class Manifest:
    """The frozen manifest `select_rows` returns.

    `digest` is a sha256 over the canonical (sorted-keys) JSON of
    `entries`/`batch_sizes`/`source`/`declined` — the value the receipt
    records instead of the manifest itself (§ Design § Selector, "Frozen
    means frozen"). `declined` is every row EXCLUDED for carrying a
    `route-to-*` mark, sorted by `row_id` for determinism — never raised
    (DR-404).
    """

    entries: tuple[ManifestEntry, ...]
    batch_sizes: Mapping[str, int]
    source: Optional[SourceOpResult]
    digest: str
    declined: tuple[DeclinedEntry, ...] = ()


def _repo_relative(path: Path, repo_root: Path) -> str:
    """POSIX path relative to ``repo_root``, so the frozen manifest names no
    host path.

    # A queue dir outside repo_root used
    # to fall back to the raw (possibly absolute, host-specific) path, which
    # would leak into the manifest digest and make it non-reproducible
    # across machines/CI. Refused instead; the only current caller
    # (queue_emit.py) already enforces containment before calling in.
    """
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(
            f"queue_select: row {path!s} does not resolve under repo_root "
            f"{repo_root!s} — a manifest path must never carry a host-specific "
            "absolute path"
        ) from exc


def _read_ledger_lines(profile: str, repo_root: Path) -> dict[str, list[dict]]:
    """Read every `state/queue-grind/<profile>/*.jsonl` line, grouped by `row_id`.

    Each JSONL line is one `grind-row append` record — see
    `grind_vocab.LEDGER_LINE_FIELDS`. Lines are kept in on-disk file-sorted,
    then in-file, order, which is the order they were appended in (append-only
    ledger, one writer per row per file — § Design § Row verbs).
    """
    ledger_dir = (repo_root / "state" / "queue-grind" / profile).resolve()
    by_row: dict[str, list[dict]] = {}
    if not ledger_dir.is_dir():
        return by_row
    for name in sorted(os.listdir(ledger_dir)):
        if not name.endswith(".jsonl"):
            continue
        ledger_path = ledger_dir / name
        if not ledger_path.is_file():
            continue
        with ledger_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                row_id = record.get("row_id")
                by_row.setdefault(row_id, []).append(record)
    return by_row


def _read_handback_marks(profile: str, repo_root: Path) -> dict[str, str]:
    """Read every hand-back record the drain writes under
    `state/queue-grind/<profile>/_handback/*.json` (S2) -- one file per
    row, `{"row": row_id, "type": ..., "reason": ...}`, the same on-disk
    shape/location family as the run-cost/ledger records this profile's
    stage kinds write beside it (`state/queue-grind/<profile>/`). A row
    marked `route-to-*` here is refused the same way a `route-to-*` ledger
    line already is (`_fold_in_ledger`) -- fold-in must not re-enter a row
    the LAST run already handed off elsewhere."""
    handback_dir = (repo_root / "state" / "queue-grind" / profile / "_handback").resolve()
    marks: dict[str, str] = {}
    if not handback_dir.is_dir():
        return marks
    for name in sorted(os.listdir(handback_dir)):
        if not name.endswith(".json"):
            continue
        record_path = handback_dir / name
        if not record_path.is_file():
            continue
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        row_id = record.get("row")
        htype = record.get("type")
        if isinstance(row_id, str) and isinstance(htype, str):
            marks[row_id] = htype
    return marks


def _fold_in_ledger(
    row_id: str, digest: str, ledger_by_row: Mapping[str, list[dict]]
) -> tuple[tuple[str, ...], Optional[str]]:
    """Return `(skip_stages, declined_reason)` for `row_id`.

    `declined_reason` is `None` unless the row's ledger carries a
    `route-to-*` mark, in which case it names the mark and `skip_stages` is
    empty — the row is EXCLUDED from the manifest and recorded in
    `Manifest.declined` rather than raising (DR-404: a `route-to-*` row must
    never re-enter another selector, but a re-emit/resume must not fail
    because of it either).

    Matching last digest -> skip the stages recorded against it. Mismatched
    last digest -> re-run (empty skip set; the row content changed since the
    ledger was written).
    """
    lines = ledger_by_row.get(row_id)
    if not lines:
        return (), None
    for line in lines:
        verdict = line.get("verdict")
        outcome = line.get("outcome")
        for marker in (verdict, outcome):
            if isinstance(marker, str) and marker.startswith("route-to-"):
                return (), (
                    f"row {row_id!r} carries a route-to-* ledger mark ({marker!r})"
                )
    last_digest = lines[-1].get("digest")
    if last_digest != digest:
        return (), None
    passed: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if line.get("digest") != digest:
            continue
        stage = line.get("stage")
        if stage not in seen:
            seen.add(stage)
            passed.append(stage)
    return tuple(passed), None


def _normalise_sentinels(
    row: Mapping[str, Any], absent_sentinels: Mapping[str, Sequence[Any]]
) -> dict[str, Any]:
    """Return a copy of `row` with every declared sentinel value dropped.

    Applied once per row, before `where` evaluation and batch-key coalescing
    (§ Design § Selector, "Absent sentinels"). The raw row (as parsed) is
    untouched — only this normalised copy feeds `where`/batching.
    """
    if not absent_sentinels:
        return dict(row)
    normalised = dict(row)
    for field_name, sentinel_values in absent_sentinels.items():
        if field_name in normalised and normalised[field_name] in sentinel_values:
            del normalised[field_name]
    return normalised


def _check_where_term(
    term: Sequence[Any], absent_sentinels: Mapping[str, Sequence[Any]]
) -> tuple[str, str, Any]:
    """Validate one DNF term's shape/operator, returning `(field, op, value)`.

    `value` is `_SENTINEL_ABSENT` for `present`/`absent`, which carry no
    literal. Refuses (naming the term) any operator outside
    `grind_vocab.WHERE_OPERATORS`, any wrong-arity term, and an `==`/`in`
    term whose literal is a declared sentinel for that field
    (overengineering-reviewer #3).
    """
    if not isinstance(term, (list, tuple)) or not term:
        raise WhereTermError(f"queue_select: malformed where term {term!r}")
    field_name = term[0]
    op = term[1] if len(term) > 1 else None
    if op not in grind_vocab.WHERE_OPERATORS:
        raise WhereTermError(
            f"queue_select: where term {term!r} uses unsupported operator {op!r} — "
            f"valid operators are {sorted(grind_vocab.WHERE_OPERATORS)}"
        )
    if op in ("present", "absent"):
        if len(term) != 2:
            raise WhereTermError(
                f"queue_select: where term {term!r} — {op!r} takes no literal value"
            )
        return field_name, op, _SENTINEL_ABSENT
    if len(term) != 3:
        raise WhereTermError(
            f"queue_select: where term {term!r} — {op!r} requires a literal value"
        )
    value = term[2]
    if op == "in" and not isinstance(value, (list, tuple)):
        raise WhereTermError(
            f"queue_select: where term {term!r} — 'in' requires a list literal, "
            f"got {type(value).__name__}"
        )
    if op in ("==", "in"):
        sentinel_values = absent_sentinels.get(field_name, ())
        literals = value if op == "in" else (value,)
        for literal in literals:
            if literal in sentinel_values:
                raise WhereTermError(
                    f"queue_select: where term {term!r} names sentinel value "
                    f"{literal!r} for field {field_name!r} as a literal — a declared "
                    "absent sentinel can never be matched by == / in"
                )
    return field_name, op, value


def _eval_where_term(
    row: Mapping[str, Any], field_name: str, op: str, value: Any
) -> bool:
    present = field_name in row and row[field_name] is not None
    if op == "present":
        return present
    if op == "absent":
        return not present
    if not present:
        return False
    row_value = row[field_name]
    if op == "==":
        return row_value == value
    if op == "in":
        return row_value in value
    if op == "contains":
        if not isinstance(row_value, list):
            raise WhereTermError(
                f"queue_select: where term ({field_name!r}, 'contains', {value!r}) "
                f"applied to non-list field value {row_value!r} — contains is "
                "list fields only"
            )
        return value in row_value
    raise WhereTermError(f"queue_select: unreachable operator {op!r}")  # pragma: no cover


def _check_where(
    where: Optional[Sequence[Sequence[Sequence[Any]]]],
    absent_sentinels: Mapping[str, Sequence[Any]],
) -> Optional[list[list[tuple[str, str, Any]]]]:
    """Validate the whole DNF `where` up front — refused at emit, not lazily.

    Every term is checked once here, regardless of whether any row survives
    to be evaluated against it, so a malformed `where` is refused even over
    an empty queue.
    """
    if not where:
        return None
    return [[_check_where_term(term, absent_sentinels) for term in clause] for clause in where]


def _matches_where(
    row: Mapping[str, Any],
    checked_where: Optional[Sequence[Sequence[tuple[str, str, Any]]]],
) -> bool:
    """Evaluate the pre-validated DNF `checked_where` over one (sentinel-
    normalised) row. `None` -> every row matches. Otherwise a list of OR
    clauses, each a list of AND terms; the row matches if any clause's every
    term is true.
    """
    if checked_where is None:
        return True
    for clause in checked_where:
        if all(_eval_where_term(row, field_name, op, value) for field_name, op, value in clause):
            return True
    return False


def _coalesce_batch_key(
    row: Mapping[str, Any], batch_key: Sequence[str]
) -> str:
    """First present field in the ordered `batch_key` list, else `@unkeyed`."""
    for field_name in batch_key:
        if field_name in row and row[field_name] is not None:
            return str(row[field_name])
    return "@unkeyed"


def _call_source_op(
    op_name: str, args: Mapping[str, Any], repo_root: Path
) -> Any:
    """Resolve and call `op_name` in-process through the op registry.

    Refuses any op outside `grind_vocab.SOURCE_OPS`. Runs the handler
    directly — no subprocess, ever — and awaits it via `asyncio.run` if it
    returns a coroutine (handlers may be sync or async per
    `coordinator_core.ipc.register_op`'s contract).
    """
    if op_name not in grind_vocab.SOURCE_OPS:
        raise UnknownSourceOpError(
            f"queue_select: source op {op_name!r} is not a member of "
            f"SOURCE_OPS {sorted(grind_vocab.SOURCE_OPS)}"
        )
    from coordinator_core.ipc import get_op_handler

    handler = get_op_handler(op_name)
    if handler is None:
        raise UnknownSourceOpError(
            f"queue_select: source op {op_name!r} is not registered"
        )
    result = handler(dict(args), repo_root=repo_root)
    import inspect

    if inspect.isawaitable(result):
        result = _run_awaitable_sync(result)
    return result


def _run_awaitable_sync(awaitable: Any) -> Any:
    """Await `awaitable` to completion from SYNCHRONOUS code, whether or not
    a loop is already running in this thread.

    `asyncio.run` raises `RuntimeError: asyncio.run() cannot be called from
    a running event loop` the moment `select_rows` is reached from inside
    the IPC daemon's own loop (S1) — a bare `asyncio.run` call here is a
    dead path over the daemon's real call shape, not a hypothetical. When no
    loop is running in this thread, `asyncio.run` is used directly (the
    common, loop-free test/CLI path); when one IS running, the coroutine is
    driven to completion on a SEPARATE thread with its own fresh loop, so
    this thread's running loop is never re-entered.
    """
    import asyncio
    import threading

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    box: dict[str, Any] = {}

    def _runner() -> None:
        try:
            box["result"] = asyncio.run(awaitable)
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the caller's thread below
            box["error"] = exc

    thread = threading.Thread(target=_runner)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["result"]


def _file_row_id(row_path: Path, parsed: Mapping[str, Any], row_id_key: str) -> str:
    """A queue file's row id: its stem under `'@stem'`, else its `row_id_key`
    field. The one derivation `select_rows` and `live_row_ids` share, so a
    ledger's key and the sweep's liveness test cannot drift apart."""
    if row_id_key == "@stem":
        return row_path.stem
    raw_row_id = parsed.get(row_id_key)
    if raw_row_id is None:
        raise MissingRowIdError(
            f"queue_select: row {row_path!s} carries no {row_id_key!r} "
            "field — refused, never coerced to the literal string 'None'"
        )
    return str(raw_row_id)


def _source_row_id(
    op_name: str, record: Mapping[str, Any], record_digest: str, row_id_key: str, index: int
) -> str:
    """A source-op record's row id — the `_file_row_id` twin for `source` rows."""
    if row_id_key == "@stem":
        return f"@source-{op_name}-{record_digest[:16]}"
    raw_row_id = record.get(row_id_key)
    if raw_row_id is None:
        raise MissingRowIdError(
            f"queue_select: source row {index} from op {op_name!r} carries "
            f"no {row_id_key!r} field — refused, never coerced to a "
            "positional id"
        )
    return str(raw_row_id)


def _record_digest(record: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def live_row_ids(
    queue: Sequence[Path],
    *,
    row_id_key: str,
    repo_root: Path,
    source: Optional[Mapping[str, Any]] = None,
) -> set[str]:
    """Every row id the named queue dirs (and `source` op) yield today, before
    `where`/`limit` — the liveness set `grind-row sweep` settles ledgers
    against. A row filtered out by `where` is still live."""
    ids: set[str] = set()
    for queue_dir in queue:
        queue_dir = Path(queue_dir)
        for name in sorted(os.listdir(queue_dir)):
            if name.startswith(".") or not (name.endswith(".yaml") or name.endswith(".yml")):
                continue
            row_path = queue_dir / name
            if not row_path.is_file():
                continue
            if row_id_key == "@stem":
                ids.add(row_path.stem)
                continue
            parsed = schema_validate.parse_yaml(row_path.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise UnparseableRowError(f"queue_select: row {row_path!s} did not parse to a mapping")
            ids.add(_file_row_id(row_path, parsed, row_id_key))
    if source is not None:
        output = _call_source_op(source["op"], source.get("args") or {}, Path(repo_root))
        records = output.get("records") if isinstance(output, Mapping) else None
        for i, record in enumerate(records or []):
            if isinstance(record, Mapping):
                ids.add(_source_row_id(source["op"], record, _record_digest(record), row_id_key, i))
    return ids


def select_rows(
    queue: Sequence[Path],
    *,
    where: Optional[Sequence[Sequence[Sequence[Any]]]],
    order: Optional[tuple[str, Sequence[Any]]],
    limit: Optional[int],
    batch_key: Sequence[str],
    batch_sizes: Mapping[str, int],
    row_id_key: str,
    profile: str,
    repo_root: Path,
    source: Optional[Mapping[str, Any]] = None,
    absent_sentinels: Optional[Mapping[str, Sequence[Any]]] = None,
) -> Manifest:
    """Turn one or more queue directories into a frozen `Manifest`.

    A pure function of the directories it names: sorted `os.listdir` per
    queue dir, one `read_bytes` + sha256 + `schema_validate.parse_yaml` per
    file, no glob, no walk, no spawn (§ Design § Selector). `row_id_key` may
    be a profile field name or the reserved `'@stem'` (the file stem); two
    queue dirs yielding the same stem under `'@stem'` is refused
    (`DuplicateStemError`). `where` is the closed JSON-DNF grammar
    (`grind_vocab.WHERE_OPERATORS`); a malformed or unsupported term is
    refused naming the term (`WhereTermError`). `absent_sentinels` values are
    normalised out of each row once, before `where` and batch-key coalescing.
    `order` is `(field, ordered_values)`: rows sort by their field value's
    index in `ordered_values` (unlisted values sort last, stably); `None`
    keeps read order. `batch_key` is an ordered coalesce list; a row missing
    every key batches under the reserved `'@unkeyed'`. `profile` names the
    ledger directory (`state/queue-grind/<profile>/*.jsonl`) the fold-in
    reads; a row whose ledger carries a `route-to-*` mark is EXCLUDED from
    the manifest and recorded in `Manifest.declined` — never raised
    (DR-404). `source`, when given, is `{"op": ..., "args":
    ...}`; `op` must be a member of `grind_vocab.SOURCE_OPS` and is resolved
    and called in-process through the op registry (`UnknownSourceOpError`
    otherwise).
    """
    absent_sentinels = absent_sentinels or {}
    repo_root = Path(repo_root)

    seen_stems: dict[str, Path] = {}
    rows: list[tuple[str, Path, str, dict[str, Any]]] = []  # (row_id, path, digest, normalised)

    for queue_dir in queue:
        queue_dir = Path(queue_dir)
        for name in sorted(os.listdir(queue_dir)):
            if name.startswith(".") or not (name.endswith(".yaml") or name.endswith(".yml")):
                continue
            row_path = queue_dir / name
            if not row_path.is_file():
                continue
            raw_bytes = row_path.read_bytes()
            digest = hashlib.sha256(raw_bytes).hexdigest()
            try:
                parsed = schema_validate.parse_yaml(raw_bytes.decode("utf-8"))
            except Exception as exc:  # noqa: BLE001 -- re-raised, named, never swallowed
                raise UnparseableRowError(
                    f"queue_select: row {row_path!s} could not be parsed as YAML: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            if not isinstance(parsed, dict):
                raise UnparseableRowError(
                    f"queue_select: row {row_path!s} did not parse to a mapping "
                    f"(got {type(parsed).__name__})"
                )

            row_id = _file_row_id(row_path, parsed, row_id_key)
            if row_id in seen_stems and seen_stems[row_id] != row_path:
                error = DuplicateStemError if row_id_key == "@stem" else DuplicateRowIdError
                label = "stem" if row_id_key == "@stem" else "row_id"
                raise error(
                    f"queue_select: {label} {row_id!r} is yielded by both "
                    f"{seen_stems[row_id]!s} and {row_path!s}"
                )
            seen_stems[row_id] = row_path

            normalised = _normalise_sentinels(parsed, absent_sentinels)
            rows.append((row_id, row_path, digest, normalised))

    checked_where = _check_where(where, absent_sentinels)
    filtered = [r for r in rows if _matches_where(r[3], checked_where)]

    if order is not None:
        order_field, order_spec = order
        if isinstance(order_spec, str) and order_spec in ("asc", "desc"):
            # The `{field, order: asc|desc}` shape a profile's `priority`
            # block declares (B4) -- no enumerated value list to index into,
            # so a present field value sorts by its own natural ordering; a
            # row missing the field sorts last regardless of direction.
            reverse = order_spec == "desc"
            present = [item for item in filtered if item[3].get(order_field) is not None]
            missing = [item for item in filtered if item[3].get(order_field) is None]
            present = sorted(present, key=lambda item: item[3].get(order_field), reverse=reverse)
            filtered = present + missing
        else:
            ordered_values = order_spec
            order_index = {v: i for i, v in enumerate(ordered_values)}

            # `rows.index(item)` was an
            # O(n) linear scan with full-tuple equality per comparison inside
            # sorted()'s O(n log n) comparisons; sorted() is already stable
            # over `filtered`'s read order so the tie-break was redundant as
            # well as costly. Dropped; stability alone preserves read order
            # among equal keys.
            filtered = sorted(
                filtered,
                key=lambda item: order_index.get(item[3].get(order_field), len(ordered_values)),
            )

    if limit is not None:
        filtered = filtered[:limit]

    ledger_by_row = _read_ledger_lines(profile, repo_root)
    handback_marks = _read_handback_marks(profile, repo_root)

    entries: list[ManifestEntry] = []
    declined: list[DeclinedEntry] = []
    for row_id, row_path, digest, normalised in filtered:
        rel_path = _repo_relative(row_path, repo_root)
        mark = handback_marks.get(row_id, "")
        if mark.startswith("route-to-"):
            declined.append(
                DeclinedEntry(
                    row_id=row_id,
                    path=rel_path,
                    reason=f"row carries a route-to-* hand-back mark ({mark!r})",
                )
            )
            continue
        skip_stages, declined_reason = _fold_in_ledger(row_id, digest, ledger_by_row)
        if declined_reason is not None:
            declined.append(DeclinedEntry(row_id=row_id, path=rel_path, reason=declined_reason))
            continue
        entries.append(
            ManifestEntry(
                row_id=row_id,
                path=rel_path,
                digest=digest,
                batch_key=_coalesce_batch_key(normalised, batch_key),
                skip_stages=skip_stages,
            )
        )

    source_result: Optional[SourceOpResult] = None
    if source is not None:
        op_name = source["op"]
        args = source.get("args") or {}
        output = _call_source_op(op_name, args, repo_root)
        output_sha256 = hashlib.sha256(
            json.dumps(output, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        source_result = SourceOpResult(op=op_name, args=args, output_sha256=output_sha256)

        # The source op's own output rows enter the SAME manifest as a
        # queue source (S1) -- `records` is the shape `lessons.extract`
        # (the one registered SOURCE_OPS member) returns; a source op with
        # no `records` list contributes no rows, only the recorded
        # `SourceOpResult` provenance.
        source_records = output.get("records") if isinstance(output, Mapping) else None
        if source_records:
            seen_source_ids: dict[str, int] = {}
            for i, record in enumerate(source_records):
                if not isinstance(record, Mapping):
                    continue
                normalised = _normalise_sentinels(record, absent_sentinels)
                if not _matches_where(normalised, checked_where):
                    continue
                record_digest = _record_digest(record)
                row_id = _source_row_id(op_name, record, record_digest, row_id_key, i)
                if row_id_key == "@stem":
                    if row_id in seen_source_ids:
                        raise DuplicateStemError(
                            f"queue_select: source rows {seen_source_ids[row_id]} and {i} "
                            f"from op {op_name!r} resolve to the same content-derived id "
                            f"{row_id!r}"
                        )
                    seen_source_ids[row_id] = i
                source_path = f"@source:{op_name}:{i}"
                mark = handback_marks.get(row_id, "")
                if mark.startswith("route-to-"):
                    declined.append(
                        DeclinedEntry(
                            row_id=row_id,
                            path=source_path,
                            reason=f"row carries a route-to-* hand-back mark ({mark!r})",
                        )
                    )
                    continue
                skip_stages, declined_reason = _fold_in_ledger(row_id, record_digest, ledger_by_row)
                if declined_reason is not None:
                    declined.append(
                        DeclinedEntry(row_id=row_id, path=source_path, reason=declined_reason)
                    )
                    continue
                entries.append(
                    ManifestEntry(
                        row_id=row_id,
                        path=source_path,
                        digest=record_digest,
                        batch_key=_coalesce_batch_key(normalised, batch_key),
                        skip_stages=skip_stages,
                    )
                )

    declined_sorted = tuple(sorted(declined, key=lambda d: d.row_id))

    canonical = {
        "entries": [
            {
                "row_id": e.row_id,
                "path": e.path,
                "digest": e.digest,
                "batch_key": e.batch_key,
                "skip_stages": list(e.skip_stages),
            }
            for e in entries
        ],
        "batch_sizes": dict(batch_sizes),
        "source": (
            {
                "op": source_result.op,
                "args": dict(source_result.args),
                "output_sha256": source_result.output_sha256,
            }
            if source_result is not None
            else None
        ),
        "declined": [
            {"row_id": d.row_id, "path": d.path, "reason": d.reason} for d in declined_sorted
        ],
    }
    manifest_digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True).encode("utf-8")
    ).hexdigest()

    return Manifest(
        entries=tuple(entries),
        batch_sizes=dict(batch_sizes),
        source=source_result,
        digest=manifest_digest,
        declined=declined_sorted,
    )
