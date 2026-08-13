"""
coordinator_core.ops.deliverable_ledger_write — comment-preserving splice writer for
the close-out ledger (`ledger:`) block in `state/deliverable-equivalence.yaml`.

Purpose: `deliverable_equivalence.py` ships the ledger's READ side
(`load_deliverable_ledger`, `validate_deliverable_ledger_rows`) but nothing has ever
written a row to the real corpus. This module is the persister: a module-plus-CLI,
modelled on `backfill_deliverable_spine.py`'s shape (`main()`, `if __name__ ==
"__main__":`, atomic `os.replace`, mode-bit preservation before replace — see that
module's `_stamp_file`), NOT registered as an IPC op. `coordinator_core/ipc.py`'s
negative-spec forbids handlers writing `state/`; a module-plus-CLI outside the handler
layer is the sanctioned shape, exactly as `backfill_deliverable_spine.py` already is
(absent from `coordinator_core/ops/__init__.py`'s registration list — this module stays
absent from it too).

Comment-preserving splice, NOT a YAML round-trip: `state/deliverable-equivalence.yaml`
carries ~100 lines of load-bearing header commentary, per-entry `evidence` prose, and a
documented row schema in comments directly above the `ledger:` key. A `yaml.safe_dump`
of the parsed document would destroy all of that. This writer instead:
  - leaves every byte above the `ledger:` key untouched — that region is never parsed,
    never re-serialized, only sliced off and re-emitted byte-for-byte;
  - replaces ONLY the `ledger:` block — today the single line `ledger: []`, but this
    also handles the already-populated case (`ledger:` followed by an indented block
    list) so re-runs work;
  - renders rows deterministically (stable key order, rows sorted by `deliverable_id`)
    so re-runs diff cleanly and identical input produces a byte-identical file;
  - calls `validate_deliverable_ledger_rows` over the FULL resulting row set BEFORE
    `os.replace` — refuses to write (propagates `DeliverableLedgerValidationError`) on
    any malformed row, never partially writes;
  - calls `_reset_deliverable_ledger_cache()` after a successful write — the loader
    memo is per-process and root-insensitive, a hazard its own docstring names.
  - validates the RENDERED temp file — parsed and run through
    `validate_deliverable_ledger_rows` — BEFORE `os.replace` lands it as the
    artifact (Review: coordinatorcode-reviewer f292d223 — F2). `os.replace` is
    atomic on POSIX, so this closes the concurrent-read window for a corrupt
    render: on the 50-70-concurrent-session tree, no other process's
    `load_deliverable_ledger` call can ever observe a corrupt intermediate
    file, because a corrupt render now never lands on disk at all. The
    post-replace read-back-and-restore (`_verify_write_or_restore`) stays as a
    belt-and-braces guard for whatever this pre-replace check does not catch,
    not the primary mechanism. Residual, named honestly: this does NOT close a
    torn-read window, because none exists — `os.replace` provides no partial
    view of the file to any reader at any point, before or after this fix.

Merge semantics: UPSERT, not append. `deliverable_id` is the ledger's unique primary
key (`validate_deliverable_ledger_rows` raises on duplicates), and this artifact has
two known producers (a corpus seed, a supersession overlay) that overlap on ids by
design. `upsert_deliverable_ledger_rows` takes a row iterable and merges it against
whatever is already on disk: a supplied row REPLACES an existing row sharing its
`deliverable_id`; every row not mentioned in the supplied set is preserved untouched.
Blind append is wrong here and would raise on the very first overlapping id.

Spec backlink: docs/plans/2026-08-13-archive-side-corpus-remediation.md § C1
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, IO, Iterable, List, Optional

import yaml

from coordinator_core.locked_write import held_lock
from coordinator_core.ops.deliverable_equivalence import (
    DeliverableLedgerValidationError,
    _reset_deliverable_ledger_cache,
    load_deliverable_ledger,
    validate_deliverable_ledger_rows,
)

_EQUIVALENCE_ARTIFACT_RELPATH = Path("state") / "deliverable-equivalence.yaml"

#: The ledger key line this module splices on. Matched at column 0, exactly as it
#: appears in the artifact (`ledger: []` today; `ledger:` alone once rows exist).
_LEDGER_KEY_RE = re.compile(r"^ledger:.*$")

#: Stable, documented key order for a rendered row — mirrors the schema comment block
#: directly above `ledger:` in the artifact itself. Optional keys omitted when absent
#: from the row rather than rendered as an explicit `null`.
_ROW_KEY_ORDER = (
    "deliverable_id",
    "status",
    "closed_at",
    "governing_plan",
    "closure_evidence",
    "superseded_by",
    "adjudicator",
    "evidence_source",
)


class DeliverableLedgerWriteError(RuntimeError):
    """Raised for a splice-target shape this writer cannot safely locate or mutate."""


def _find_ledger_key_line(lines: List[str]) -> int:
    """Index of the line carrying the `ledger:` key, searched top-down.

    Raises `DeliverableLedgerWriteError` when no such line exists — this writer never
    invents the key or appends a new top-level block; the artifact is expected to
    already declare `ledger:` (today as `ledger: []`).

    Review: coordinatorcode-reviewer c8602a8b — F4: also raises when MORE than
    one column-0 `ledger:` line exists, rather than silently taking the
    first. A header authoring mistake (e.g. a mis-indented block-scalar
    continuation line landing back at column 0) can put the literal text
    `ledger:` at column 0 above the real key; silently splicing on the first
    match would absorb the second occurrence into whichever side of the
    splice it falls on. A wrong splice on this artifact is data loss, so
    refusing on ambiguity is correct.
    """
    matches = [index for index, line in enumerate(lines) if _LEDGER_KEY_RE.match(line)]
    if not matches:
        raise DeliverableLedgerWriteError(
            "no top-level 'ledger:' key found in the artifact — refusing to splice"
        )
    if len(matches) > 1:
        raise DeliverableLedgerWriteError(
            f"found {len(matches)} column-0 'ledger:' lines at indices {matches} — "
            "refusing to splice on an ambiguous target"
        )
    return matches[0]


def _find_ledger_block_end(lines: List[str], ledger_start: int) -> int:
    """Index of the first line AFTER the `ledger:` block, or `len(lines)` if the
    block runs to the end of the file.

    Review: coordinatorcode-reviewer f292d223 — F4: the splice previously assumed
    the `ledger:` block was the last content in the artifact and discarded
    anything after it. The block is either the single line `ledger: []` (its
    own line, index `ledger_start + 1` ends it) or `ledger:` followed by an
    indented block-list — every continuation line of that list is blank or
    starts with whitespace; the first line back at column 0 (or EOF) ends the
    block. This lets the caller preserve a future footer instead of deleting it.
    """
    ledger_line = lines[ledger_start]
    if ledger_line.rstrip("\n").strip() != "ledger:":
        # `ledger: []` (or any other single-line form) — the block is one line.
        return ledger_start + 1
    index = ledger_start + 1
    while index < len(lines):
        line = lines[index]
        if line.strip() and not line[0].isspace():
            break
        index += 1
    return index


def _render_scalar(value: Any) -> str:
    """Single-line YAML scalar for a row value, via `yaml.safe_dump` in flow style.

    Reused for `closure_evidence` (a mapping) too — flow style keeps the ledger block
    readable without needing block-mapping indentation bookkeeping in the splice.

    `value` is wrapped in a single-element list before dumping and the outer `[`/`]`
    stripped back off: `yaml.safe_dump` of a bare top-level scalar emits a trailing
    `...` explicit-document-end marker (YAML's own disambiguation for a
    document containing only a plain scalar), which is not valid mid-line content
    here. Dumping `[value]` keeps the stream a single flow-mapping-compatible
    document with no such marker, at the cost of this one strip step.

    `width=float("inf")` is required alongside that: `safe_dump`'s default ~80-column
    width line-wraps a long flow scalar (measured: 65 of 437 real seeded rows wrap,
    mostly the `;`-joined `evidence_source` field) at an arbitrary break point, and this
    function's caller never accounts for that continuation line breaking the block-list
    row's fixed 4-space indent — producing invalid YAML that the loader would then
    silently degrade to an empty ledger. Forcing an unbounded width keeps every rendered
    scalar on exactly one line regardless of length.
    """
    dumped = yaml.safe_dump(
        [value], default_flow_style=True, sort_keys=True, width=float("inf")
    ).strip()
    assert dumped.startswith("[") and dumped.endswith("]")
    return dumped[1:-1]


def _drop_none_values(row: Dict[str, Any]) -> Dict[str, Any]:
    """Row dict with every `None`-valued key removed.

    Companion to F5's omit-`None`-keys rendering change: the in-memory row set
    (`expected_rows`) always carries `closed_at: None`/`superseded_by: None`
    explicitly for an open row, while the rendered-and-reparsed row omits an
    absent key entirely — a direct dict comparison between the two would flag
    a false mismatch on every row with an optional `None` field. Comparisons in
    `_validate_rendered_tmp_file`/`_verify_write_or_restore` normalize both
    sides through this helper so the check reflects semantic equality, not
    presence-vs-absence of a key whose value is `None` either way.
    """
    return {key: value for key, value in row.items() if value is not None}


def _render_row(row: Dict[str, Any]) -> List[str]:
    """Deterministic block-list rendering for one ledger row.

    Stable key order (`_ROW_KEY_ORDER`); an optional key absent from `row`, OR whose
    value is `None`, is omitted entirely rather than rendered as an explicit `null`,
    matching the schema comment's documented "absent when not required" convention.

    Review: coordinatorcode-reviewer f292d223 — F5: the required-vs-`None` fields
    (`deliverable_id`, `status`, `adjudicator`, `evidence_source`) always pass
    `validate_deliverable_ledger_rows` as non-blank strings before this function
    runs, so they are never `None` here — omitting a `None` value is safe for
    every key in `_ROW_KEY_ORDER` without re-deriving per-status optionality: a
    key the validator requires non-null for this row's status (e.g.
    `closed_at`/`superseded_by`) can only be `None` here if validation already
    would have raised, so this render is unreachable with a `None` value for a
    required-for-this-status key.
    """
    out: List[str] = []
    first = True
    for key in _ROW_KEY_ORDER:
        if key not in row or row[key] is None:
            continue
        value = row[key]
        rendered = _render_scalar(value)
        prefix = "  - " if first else "    "
        out.append(f"{prefix}{key}: {rendered}\n")
        first = False
    if first:
        # A row with none of the known keys present — should be unreachable past
        # validate_deliverable_ledger_rows (required keys always present), kept as a
        # defensive guard against a caller bypassing validation.
        raise DeliverableLedgerWriteError(f"row has no renderable keys: {row!r}")
    return out


def _render_ledger_block(rows: List[Dict[str, Any]]) -> List[str]:
    """Full `ledger:` block, rows sorted by `deliverable_id` for a clean re-run diff."""
    if not rows:
        return ["ledger: []\n"]
    sorted_rows = sorted(rows, key=lambda r: r["deliverable_id"])
    out: List[str] = ["ledger:\n"]
    for row in sorted_rows:
        out.extend(_render_row(row))
    return out


def _restore_original_content(
    artifact_path: Path, original_content: str, orig_mode: Optional[int]
) -> None:
    """Atomically restore `artifact_path` to `original_content`, best-effort mode-bit
    preservation, then reset the ledger memo so a subsequent read sees the restored
    (not the failed-write) bytes."""
    restore_tmp_path = f"{artifact_path}.ledger-write.restore.{os.getpid()}"
    with open(restore_tmp_path, "w", encoding="utf-8") as fh:
        fh.write(original_content)
    if orig_mode is not None:
        try:
            os.chmod(restore_tmp_path, orig_mode)
        except OSError:
            print(
                f"skip: _restore_original_content: os.chmod(restore_tmp_path, orig_mode) failed: {sys.exc_info()[1]}",
                file=sys.stderr,
            )
    os.replace(restore_tmp_path, artifact_path)
    _reset_deliverable_ledger_cache()


def _validate_rendered_tmp_file(
    tmp_path: str, expected_rows: List[Dict[str, Any]]
) -> None:
    """Pre-`os.replace` guard (F2's fix): parse and validate the RENDERED temp file
    itself — not the pre-write in-memory rows — before it ever becomes the artifact.

    Review: coordinatorcode-reviewer f292d223 — F2: `_render_scalar`'s rendering
    runs AFTER `validate_deliverable_ledger_rows` validates the in-memory row set,
    so a corrupt render was previously only caught by `_verify_write_or_restore`
    AFTER `os.replace` had already landed it — a window in which any of this
    tree's 50-70 concurrent readers could see the corrupt file and (per
    `load_deliverable_ledger`'s documented parse-failure degradation) silently
    read back an empty ledger. `os.replace` itself is atomic on POSIX, so
    validating the rendered bytes before that call closes the window for this
    failure class entirely: a corrupt render now never lands on disk at all.
    Raises `DeliverableLedgerWriteError` on any parse/validation/mismatch,
    leaving both the temp file and the still-untouched artifact in place — the
    caller is responsible for cleaning up the temp file on this path.
    """
    try:
        with open(tmp_path, "r", encoding="utf-8") as fh:
            rendered_content = fh.read()
        parsed = yaml.safe_load(rendered_content)
        if not isinstance(parsed, dict):
            raise DeliverableLedgerWriteError(
                "rendered temp file did not parse to a mapping — refusing to "
                "replace the artifact with it"
            )
        rendered_rows = parsed.get("ledger")
        if not isinstance(rendered_rows, list):
            raise DeliverableLedgerWriteError(
                "rendered temp file's 'ledger' key is missing or not a list — "
                "refusing to replace the artifact with it"
            )
        validate_deliverable_ledger_rows(rendered_rows)
        rendered_by_id = {r["deliverable_id"]: _drop_none_values(r) for r in rendered_rows}
        expected_by_id = {r["deliverable_id"]: _drop_none_values(r) for r in expected_rows}
        if rendered_by_id != expected_by_id:
            raise DeliverableLedgerWriteError(
                "rendered temp file's ledger rows do not match the intended row "
                "set — refusing to replace the artifact with it"
            )
    except DeliverableLedgerWriteError:
        raise
    except Exception as exc:
        raise DeliverableLedgerWriteError(
            f"rendered temp file failed pre-replace validation ({exc!r}) — "
            "refusing to replace the artifact with it"
        ) from exc


def _verify_write_or_restore(
    artifact_path: Path,
    original_content: str,
    orig_mode: Optional[int],
    expected_rows: List[Dict[str, Any]],
) -> None:
    """Post-write read-back verification (defect 2's fix).

    `_render_scalar`'s rendering happens AFTER `validate_deliverable_ledger_rows` runs
    over the in-memory row set, so validating the intended rows before writing (as
    `upsert_deliverable_ledger_rows` already does) cannot catch a rendering defect that
    corrupts the bytes actually written. And `load_deliverable_ledger`'s documented
    degradation on a parse failure is to return `[]`, not raise — so a corrupt write is
    otherwise silently indistinguishable from "empty ledger", and a subsequent upsert
    would merge against zero prior rows and destroy every row already on disk.

    Re-reads the just-written file from disk (forcing a fresh read past the loader's
    per-process memo — the memo was already reset by the caller, but this function
    resets it again on both the success and failure paths so no stale state survives
    either outcome), re-validates it, and confirms the `ledger:` block's rows match
    `expected_rows` exactly (as a `deliverable_id`-keyed set, order-independent — the
    renderer sorts by `deliverable_id` but this check does not depend on that). On any
    mismatch, parse failure, or validation failure, restores the pre-write file content
    byte-for-byte and raises `DeliverableLedgerWriteError` — the file on disk must never
    be left in a state the loader would silently read as empty or partial.
    """
    try:
        _reset_deliverable_ledger_cache()
        readback_rows = load_deliverable_ledger(artifact_path.parent.parent)
        validate_deliverable_ledger_rows(readback_rows)
        readback_by_id = {r["deliverable_id"]: _drop_none_values(r) for r in readback_rows}
        expected_by_id = {r["deliverable_id"]: _drop_none_values(r) for r in expected_rows}
        if readback_by_id != expected_by_id:
            raise DeliverableLedgerWriteError(
                "post-write read-back verification failed: the re-parsed ledger does "
                "not match the intended row set — the write is presumed corrupt"
            )
    except Exception as exc:
        _restore_original_content(artifact_path, original_content, orig_mode)
        if isinstance(exc, DeliverableLedgerWriteError):
            raise
        raise DeliverableLedgerWriteError(
            f"post-write read-back verification failed ({exc!r}); the original file "
            "has been restored"
        ) from exc


def upsert_deliverable_ledger_rows(
    worktree_root: Path, rows: Iterable[Dict[str, Any]]
) -> None:
    """Merge `rows` into the artifact's `ledger:` block, keyed by `deliverable_id`.

    UPSERT, not append: a supplied row replaces any existing row sharing its
    `deliverable_id`; every on-disk row not mentioned in `rows` is preserved. The full
    resulting row set is validated via `validate_deliverable_ledger_rows` BEFORE the
    write lands — a `DeliverableLedgerValidationError` aborts with the on-disk file
    untouched. Header bytes above the `ledger:` key are copied through verbatim, never
    reparsed. Resets the ledger read-model's process memo on success.

    Concurrency (Review: coordinatorcode-reviewer c8602a8b — F1): the whole
    read-existing / merge / render / validate / replace / verify sequence runs
    under `coordinator_core.locked_write.held_lock` on `artifact_path`, so two
    overlapping invocations on the same artifact never both read the same
    `existing_rows` and silently discard one writer's rows — the second
    invocation's read cannot start until the first has fully released the
    lock (post-write verify included). `locked_rmw` was considered first but
    does not fit: its `mutate: str -> str` contract has no room for this
    function's pre-replace parse/validate guard, its own chmod-preservation,
    or its post-replace read-back-and-restore-on-mismatch step, all of which
    must stay inside the held critical section, not just the final text
    transform. `held_lock` wraps the existing body unchanged instead.
    """
    rows = list(rows)
    artifact_path = worktree_root / _EQUIVALENCE_ARTIFACT_RELPATH
    if not artifact_path.is_file():
        raise DeliverableLedgerWriteError(f"no artifact at {artifact_path}")

    with held_lock(
        target=Path(os.path.realpath(str(artifact_path))),
        holder_label="deliverable-ledger-write",
    ):
        existing_rows = load_deliverable_ledger(worktree_root)

        # Review: coordinatorcode-reviewer f292d223 — F1: a present-but-malformed
        # on-disk row must fail loud, never be silently excluded from the merge
        # (contradicts both this module's "every row not mentioned is preserved"
        # contract and validate_deliverable_ledger_rows's own fail-loud philosophy).
        merged: Dict[str, Dict[str, Any]] = {}
        for index, r in enumerate(existing_rows):
            if not isinstance(r, dict) or not isinstance(r.get("deliverable_id"), str):
                raise DeliverableLedgerWriteError(
                    f"existing on-disk ledger row at index {index} is malformed "
                    f"(not a mapping, or missing/invalid 'deliverable_id'): {r!r} — "
                    "refusing to silently drop it from the merge"
                )
            merged[r["deliverable_id"]] = r
        for row in rows:
            merged[row["deliverable_id"]] = row

        final_rows = list(merged.values())
        validate_deliverable_ledger_rows(final_rows)

        # Review: coordinatorcode-reviewer f292d223 — F8: _render_row/_render_ledger_block
        # assume LF-only line endings and a trailing newline immediately before the
        # `ledger:` key. Check raw bytes for a CRLF BEFORE opening in text mode —
        # Python's universal-newlines translation silently normalizes "\r\n" to
        # "\n" on read, which would hide the very shape this guard exists to catch.
        if b"\r\n" in artifact_path.read_bytes():
            raise DeliverableLedgerWriteError(
                "the artifact contains CRLF line endings — this writer only supports "
                "LF-terminated files; refusing to splice"
            )

        with open(artifact_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()

        ledger_start = _find_ledger_key_line(lines)
        ledger_end = _find_ledger_block_end(lines, ledger_start)

        header_lines = lines[:ledger_start]
        if header_lines and not header_lines[-1].endswith("\n"):
            raise DeliverableLedgerWriteError(
                "the artifact's header does not end with a trailing newline before "
                "'ledger:' — refusing to splice onto a non-newline-terminated line"
            )
        # Review: coordinatorcode-reviewer f292d223 — F4: preserve any content after
        # the old ledger block (a future footer) instead of silently discarding it.
        footer_lines = lines[ledger_end:]
        ledger_lines = _render_ledger_block(final_rows)
        original_content = "".join(lines)

        orig_mode: Optional[int] = None
        try:
            orig_mode = os.stat(artifact_path).st_mode
        except OSError:
            print(
                f"skip: upsert_deliverable_ledger_rows: orig_mode = os.stat(artifact_path).st_mode failed: {sys.exc_info()[1]}",
                file=sys.stderr,
            )

        tmp_path = f"{artifact_path}.ledger-write.tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.writelines(header_lines)
            fh.writelines(ledger_lines)
            fh.writelines(footer_lines)
        if orig_mode is not None:
            try:
                os.chmod(tmp_path, orig_mode)
            except OSError:
                print(
                    f"skip: upsert_deliverable_ledger_rows: os.chmod(tmp_path, orig_mode) failed: {sys.exc_info()[1]}",
                    file=sys.stderr,
                )
        try:
            _validate_rendered_tmp_file(tmp_path, final_rows)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                print(
                    f"skip: upsert_deliverable_ledger_rows: os.remove(tmp_path) failed: {sys.exc_info()[1]}",
                    file=sys.stderr,
                )
            raise

        os.replace(tmp_path, artifact_path)

        _reset_deliverable_ledger_cache()

        # Belt-and-braces (residual, not the primary guard — see F2's fix above):
        # this re-reads the just-landed file and restores-on-mismatch. What it does
        # NOT cover is a torn read of a partially-written file — `os.replace` is
        # atomic on POSIX so no reader ever observes a partial write; this guard
        # exists for a defect class this process's own render/validate logic
        # missed, which `_validate_rendered_tmp_file` above should make
        # unreachable in practice.
        _verify_write_or_restore(
            artifact_path=artifact_path,
            original_content=original_content,
            orig_mode=orig_mode,
            expected_rows=final_rows,
        )


_HELP_TEXT = """\
deliverable-ledger-write — upsert close-out ledger rows into
state/deliverable-equivalence.yaml.

This CLI has no row-authoring surface of its own (rows come from a caller importing
`upsert_deliverable_ledger_rows` in-process, e.g. C2's seeder or C3's overlay) — it
exists for parity with backfill_deliverable_spine.py's module-plus-CLI shape.
`--root` is an existence check only: it confirms the target artifact and its
`ledger:` splice point are reachable, it never calls
`upsert_deliverable_ledger_rows` and never writes (writing here risks the
same accidental no-op-rewrite of the 437-row corpus this module's own
docstring warns re-runs must stay byte-identical for — Review:
coordinatorcode-reviewer c8602a8b — F5).

Usage:
  python -m coordinator_core.ops.deliverable_ledger_write --root <worktree-root>
"""


def main(
    argv: List[str],
    out: IO[str] = sys.stdout,
    err: IO[str] = sys.stderr,
) -> int:
    """CLI entry point. No standalone row-authoring flag set — see `_HELP_TEXT`.

    Review: coordinatorcode-reviewer c8602a8b — F5: the happy path used to
    compute `artifact_path.is_file()` and then discard the result, printing
    `_HELP_TEXT` unconditionally — a validation-then-noop shape indistinguishable
    from a smoke test that never runs. This is deliberately NOT upgraded to a
    real smoke check that calls `upsert_deliverable_ledger_rows`: doing so
    would perform a real (if row-empty) write against whatever `--root` names,
    which for the real artifact risks an unwanted re-render of the 437-row
    corpus this module's docstring requires stay byte-identical across runs
    that don't change any row. The existence check is instead made load-bearing:
    on success it also locates the `ledger:` splice point via
    `_find_ledger_key_line`/`_find_ledger_block_end` and reports it, so a real
    check (not just "the file exists") backs the confirmation message.
    """
    root_override: Optional[str] = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            print(_HELP_TEXT, file=out)
            return 0
        if arg == "--root":
            if i + 1 >= len(argv):
                print("error: --root requires a value", file=err)
                return 1
            root_override = argv[i + 1]
            i += 2
            continue
        print(f"error: unrecognized argument: {arg}", file=err)
        return 1

    root = Path(root_override) if root_override else Path.cwd()
    artifact_path = root / _EQUIVALENCE_ARTIFACT_RELPATH
    if not artifact_path.is_file():
        print(f"error: no artifact at {artifact_path}", file=err)
        return 1

    try:
        with open(artifact_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        ledger_start = _find_ledger_key_line(lines)
        _find_ledger_block_end(lines, ledger_start)
    except DeliverableLedgerWriteError as exc:
        print(f"error: {artifact_path} is not a valid splice target: {exc}", file=err)
        return 1

    print(f"ok: {artifact_path} exists and has a splicable 'ledger:' key", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
