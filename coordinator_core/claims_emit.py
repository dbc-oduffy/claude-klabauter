"""
coordinator_core.claims_emit

Writer for the atomic `<stem>.claims.json` + `<stem>.claims.meta.json` pair —
the shape-owning half of the claims-emit verb (the other half, producer
run-selection, lives in example-market-data-repo and calls this module).

Spec backlink:
  docs/plans/2026-08-06-claims-emit-writer-atomic-pair.md — chunk C2.
  source_memo: 2026-08-06-example-market-data-repo-em-claims-emit-verb-implementation.md

Public surface:
  emit_claims(claims, *, producer, ran_at, pipeline, out_stem, schema_path=None) -> int
    Validates flags and per-record claim shape, then writes the pair
    atomically. Returns an exit code — never raises for a producer- or
    invocation-level failure; only a genuinely unexpected error not covered
    by the documented taxonomy propagates.

Exit-code taxonomy (AC7):
  0 — both files written.
  1 — producer-side failure: a claim record fails schema validation, a
      claims-list element is not a dict, or the atomic pair write itself
      fails partway (disk/permission/OS-level).
  2 — invalid invocation: producer/out_stem missing or blank, `ran_at`
      missing/malformed/not timezone-aware, or `pipeline` missing/blank.

Negative-spec:
  - `ran_at` is validated with `datetime.fromisoformat` (a bare trailing `Z`
    is translated to `+00:00` first — PEP 616 `Z` support lands in 3.11 but
    this repo's floor is 3.11+, so the translation is defensive, not a
    version workaround). A parse that yields a naive datetime — no offset in
    the source string — is REJECTED (exit 2), not defaulted to UTC. That is
    the opposite of `archive_stamp._parse_iso_timestamp`'s convention
    elsewhere in this repo; the divergence is deliberate — `ran_at` is a
    producer-asserted fact the writer must never guess at.
  - `pipeline` is validated only by `.strip()` non-emptiness. It is NEVER
    derived from `producer` under any code path in this module, including
    error/fallback paths — they are different facts, and conflating them
    would silently mis-tag data at the landing end.
  - The claims array is validated per-record via
    `frontmatter.schema_validate.validate_frontmatter`, which takes ONE
    record dict — never the whole array. `research-claim.schema.json`
    describes a single claim.
  - `sidecar["ran_at"]` persists `ran_at.strip()` — the producer-supplied
    string verbatim (trailing bare `Z` and all) — never `parsed_ran_at
    .isoformat()`. This is deliberate, not dead code: `parsed_ran_at` exists
    only to validate the claim, not to normalize it; per the producer-
    asserted-fact convention above, the writer persists what the producer
    said, not a reinterpretation of it.
  - The atomic pair guarantee (AC8) is two-temp-then-two-replace: both temp
    files are written and both are known-good before either destination is
    touched via `os.replace`. A failed emission is a no-op on disk in BOTH
    the fresh-stem case (neither destination exists afterwards, no temps
    left) and the occupied-stem case (a pre-existing pair, or just one of
    the two files, is restored byte-for-byte) — see `_write_atomic_pair`'s
    docstring for the backup-and-restore shape. This module does not create
    the output stem's parent directory; a missing directory is a write
    failure like any other; the module does not invent a mkdir side effect
    that AC9's tree-diff isolation check would then have to route around.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from coordinator_core.frontmatter.schema_validate import SchemaVersionError, validate_frontmatter

DEFAULT_SCHEMA_PATH = (
    Path(__file__).resolve().parent / "frontmatter" / "schemas" / "research-claim.schema.json"
)

EXIT_OK = 0
EXIT_PRODUCER_FAILURE = 1
EXIT_INVALID_INVOCATION = 2

#: Generator-provenance declaration: emit_claims() writes to `<out_stem>.
#: claims.json` / `<out_stem>.claims.meta.json`, where out_stem is a
#: producer-supplied path — never a fixed repo location. No tracked artifact
#: is emitted by this module itself.
GENERATES: list = []


def _parse_rfc3339_tz_aware(ran_at: str) -> datetime | None:
    """Parses `ran_at` as an RFC3339 timezone-aware datetime, or returns None
    for anything that fails to parse, parses as a date-only value, or parses
    naive. A naive parse (no offset in the source string) is the trap this
    function exists to close — `datetime.fromisoformat` accepts a naive
    string without complaint."""
    if not ran_at or not ran_at.strip():
        return None
    text = ran_at.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt


def _validate_pipeline(pipeline: str) -> str | None:
    """Returns the stripped pipeline string when non-empty, else None.
    Never derives a value from any other input — the caller must reject
    None outright, never substitute the producer slug."""
    if pipeline is None:
        return None
    stripped = pipeline.strip()
    return stripped if stripped else None


def _write_atomic_pair(
    claims_bytes: bytes, meta_bytes: bytes, claims_path: Path, meta_path: Path
) -> None:
    """Writes both files to temp siblings, validates nothing further (the
    caller already validated content), then `os.replace()`s both only after
    both temps are complete. Before either destination is touched, any
    pre-existing file at that destination is moved aside to its own backup
    temp sibling (never copied — `os.replace` on the same filesystem is a
    rename, so the backup step cannot itself corrupt the original bytes).

    On any exception — including one raised by the second `os.replace`,
    after the first has already landed — every backed-up original is moved
    back to its destination (restoring pre-existing content byte-for-byte),
    any newly-created destination that has no backup (a fresh stem) is
    unlinked, and every write-temp file is removed. Restoring a caller's
    pre-existing data is NOT best-effort: a failure to move a backup back is
    a louder, different exception than the one that triggered the unwind —
    swallowing it would silently leave a mangled stem, the exact defect this
    function exists to close. Cleanup of this module's own now-unneeded
    backup/temp files, by contrast, stays best-effort.

    Success path: both destinations replaced, both backups (if any) are
    unlinked, and both write-temps are consumed by `os.replace`. Raises the
    triggering exception after cleanup; the caller maps that to exit code 1.
    """
    claims_dir = claims_path.parent
    meta_dir = meta_path.parent
    tmp_claims: str | None = None
    tmp_meta: str | None = None
    backup_claims: str | None = None
    backup_meta: str | None = None
    claims_replaced = False
    meta_replaced = False
    try:
        fd_claims, tmp_claims = tempfile.mkstemp(prefix=".claims-emit.", suffix=".tmp", dir=str(claims_dir))
        with os.fdopen(fd_claims, "wb") as f:
            f.write(claims_bytes)

        fd_meta, tmp_meta = tempfile.mkstemp(prefix=".claims-emit.", suffix=".tmp", dir=str(meta_dir))
        with os.fdopen(fd_meta, "wb") as f:
            f.write(meta_bytes)

        if claims_path.exists():
            # mkstemp here is used purely for atomic, collision-free name
            # reservation — the empty file it creates is discarded by the
            # os.replace below, which overwrites it with the real backup
            # content via rename.
            fd_bk, backup_claims = tempfile.mkstemp(prefix=".claims-emit.bak.", suffix=".tmp", dir=str(claims_dir))
            os.close(fd_bk)
            os.replace(claims_path, backup_claims)

        if meta_path.exists():
            fd_bk, backup_meta = tempfile.mkstemp(prefix=".claims-emit.bak.", suffix=".tmp", dir=str(meta_dir))
            os.close(fd_bk)
            os.replace(meta_path, backup_meta)

        os.replace(tmp_claims, claims_path)
        claims_replaced = True
        tmp_claims = None

        os.replace(tmp_meta, meta_path)
        meta_replaced = True
        tmp_meta = None

        # Review: coordinator:code-reviewer (A-writer-module P1) — backup
        # discard moved out of a shared `finally` and gated on the outcome:
        # this branch only runs once BOTH destinations have replaced
        # successfully, so the backups are genuinely superseded and safe to
        # drop. Our own temp file, so best-effort unlink is fine here.
        for backup in (backup_claims, backup_meta):
            if backup is not None:
                try:
                    os.unlink(backup)
                except OSError:
                    pass
        backup_claims = None
        backup_meta = None
    except Exception:
        if backup_claims is not None:
            # Original pre-existed and was moved aside — restore it
            # unconditionally, whether or not this run's replace of
            # claims_path itself succeeded before the failure occurred.
            # If THIS os.replace itself raises, backup_claims is deliberately
            # left non-None and untouched: it is the only surviving copy of
            # the caller's original data, so it must never be deleted (see
            # docstring — restore failure surfaces loudly, not silently).
            os.replace(backup_claims, claims_path)
            backup_claims = None
        elif claims_replaced:
            try:
                os.unlink(claims_path)
            except OSError:
                pass
        if backup_meta is not None:
            os.replace(backup_meta, meta_path)
            backup_meta = None
        elif meta_replaced:
            try:
                os.unlink(meta_path)
            except OSError:
                pass
        for tmp in (tmp_claims, tmp_meta):
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        raise


def emit_claims(
    claims: list[Any],
    *,
    producer: str,
    ran_at: str,
    pipeline: str,
    out_stem: str | os.PathLike[str],
    schema_path: str | os.PathLike[str] | None = None,
) -> int:
    """Validates invocation flags and per-record claim shape, then writes
    `<out_stem>.claims.json` (bare top-level array) and
    `<out_stem>.claims.meta.json` (object carrying at minimum `ran_at` and
    `pipeline`) as an atomic pair. Returns an exit code per this module's
    documented taxonomy — see module docstring — rather than raising, for
    every failure this module anticipates.
    """
    if not producer or not producer.strip():
        print("claims-emit: --producer is required and must be non-empty", file=sys.stderr)
        return EXIT_INVALID_INVOCATION

    if not out_stem or not str(out_stem).strip():
        print("claims-emit: --out is required and must be non-empty", file=sys.stderr)
        return EXIT_INVALID_INVOCATION

    parsed_ran_at = _parse_rfc3339_tz_aware(ran_at)
    if parsed_ran_at is None:
        print(
            f"claims-emit: ran_at {ran_at!r} does not parse as an RFC3339 timezone-aware "
            "datetime (naive, date-only, and empty are all rejected)",
            file=sys.stderr,
        )
        return EXIT_INVALID_INVOCATION

    validated_pipeline = _validate_pipeline(pipeline)
    if validated_pipeline is None:
        print(
            "claims-emit: --pipeline is required, must be non-empty after stripping, "
            "and is never derived from --producer",
            file=sys.stderr,
        )
        return EXIT_INVALID_INVOCATION

    if not isinstance(claims, list):
        print("claims-emit: claims must be a list — the emitted file is a bare top-level array", file=sys.stderr)
        return EXIT_PRODUCER_FAILURE

    resolved_schema_path = Path(schema_path) if schema_path is not None else DEFAULT_SCHEMA_PATH

    for index, record in enumerate(claims):
        if not isinstance(record, dict):
            print(f"claims-emit: claim record [{index}] is not an object", file=sys.stderr)
            return EXIT_PRODUCER_FAILURE
        try:
            errors = validate_frontmatter(record, resolved_schema_path)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            # Review: coordinator:code-reviewer (A-writer-module P2) — a
            # missing/malformed schema file is an environment/deployment
            # defect, not a per-record content failure; misreporting it as
            # "record [i] failed validation" hides the real fault.
            print(f"claims-emit: schema at {resolved_schema_path} is unusable ({type(exc).__name__}): {exc}", file=sys.stderr)
            return EXIT_PRODUCER_FAILURE
        except SchemaVersionError as exc:
            print(f"claims-emit: claim record [{index}] schema version mismatch: {exc}", file=sys.stderr)
            return EXIT_PRODUCER_FAILURE
        if errors:
            print(f"claims-emit: claim record [{index}] failed schema validation: {errors}", file=sys.stderr)
            return EXIT_PRODUCER_FAILURE

    stem_path = Path(out_stem)
    claims_path = stem_path.with_name(stem_path.name + ".claims.json")
    meta_path = stem_path.with_name(stem_path.name + ".claims.meta.json")

    claims_bytes = (json.dumps(claims, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    sidecar = {
        "ran_at": ran_at.strip(),
        "pipeline": validated_pipeline,
        "producer": producer.strip(),
    }
    meta_bytes = (json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    try:
        _write_atomic_pair(claims_bytes, meta_bytes, claims_path, meta_path)
    except Exception as exc:
        print(f"claims-emit: atomic pair write failed: {exc}", file=sys.stderr)
        return EXIT_PRODUCER_FAILURE

    return EXIT_OK
