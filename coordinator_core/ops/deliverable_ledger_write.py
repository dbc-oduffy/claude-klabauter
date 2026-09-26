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

Spec backlink: pln-archive-side-corpus-remediatio-3ff30d § C1
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

_LEDGER_KEY_RE = re.compile(r"^ledger:.*$")

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
    pass


def _find_ledger_key_line(lines: List[str]) -> int:
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
    ledger_line = lines[ledger_start]
    if ledger_line.rstrip("\n").strip() != "ledger:":
        return ledger_start + 1
    index = ledger_start + 1
    while index < len(lines):
        line = lines[index]
        if line.strip() and not line[0].isspace():
            break
        index += 1
    return index


def _render_scalar(value: Any) -> str:
    dumped = yaml.safe_dump(
        [value], default_flow_style=True, sort_keys=True, width=float("inf")
    ).strip()
    assert dumped.startswith("[") and dumped.endswith("]")
    return dumped[1:-1]


def _drop_none_values(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in row.items() if value is not None}


def _render_row(row: Dict[str, Any]) -> List[str]:
    """Deterministic block-list rendering for one ledger row.

    Stable key order (`_ROW_KEY_ORDER`); an optional key absent from `row`, OR whose
    value is `None`, is omitted entirely rather than rendered as an explicit `null`,
    matching the schema comment's documented "absent when not required" convention.

    The required-vs-`None` fields
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
        raise DeliverableLedgerWriteError(f"row has no renderable keys: {row!r}")
    return out


def _render_ledger_block(rows: List[Dict[str, Any]]) -> List[str]:
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
    restore_tmp_path = f"{artifact_path}.ledger-write.restore.{os.getpid()}"
    with open(restore_tmp_path, "w", encoding="utf-8", newline="") as fh:
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

    `_render_scalar`'s rendering
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
    rows = list(rows)
    artifact_path = worktree_root / _EQUIVALENCE_ARTIFACT_RELPATH
    if not artifact_path.is_file():
        raise DeliverableLedgerWriteError(f"no artifact at {artifact_path}")

    with held_lock(
        target=Path(os.path.realpath(str(artifact_path))),
        holder_label="deliverable-ledger-write",
    ):
        existing_rows = load_deliverable_ledger(worktree_root)

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
        with open(tmp_path, "w", encoding="utf-8", newline="") as fh:
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

    The happy path used to
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
