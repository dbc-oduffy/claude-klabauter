"""
coordinator_core.backlog_grind_assemble.grind_rows — the `backlog-grind-assemble
grind-row` verb family: `check`, `append`, `close`, `settle`.

Four small, index-untouched primitives the queue-grind engine's emitted
`.mjs` script shells out to (via `coordinator-invoke` in a real run, and
directly here for tests). None of the four stages the git index or spawns
git — that is exclusively the `commit` stage kind's job (§ Design § Stage
library), never this module's.

Spec backlink: docs/plans/2026-09-21-bug-blitz-emitter-engine-leg.md § Design
§ Row verbs, Tasks § C4.

- `check --manifest <script> --batch <batch-id> --repo-root D`: reads the
  emitted script's `const QUEUE_GRIND_MANIFEST = {...};` and one-line
  `const BATCHES = [...];`, takes that batch's row ids, recomputes each row's
  digest under D, and prints `{"stale": [...], "vanished": [...]}`. This
  module owns the manifest const's name.
- `append --profile P --row-id R --digest D --stage S --verdict V --outcome O
  --evidence-file F --run-stamp T`: appends one `json.dumps(sort_keys=True)`
  line (LEDGER_LINE_FIELDS order, per `grind_vocab.LEDGER_LINE_FIELDS`) to
  `state/queue-grind/<P>/<R>.jsonl`, opened in append mode with `newline="\n"`.
  Idempotent under a retried agent: an identical line already present (same
  serialized bytes) is not appended twice.
- `close --profile-dir D --profile P --row <path> --digest DIG --verdict V
  --evidence-file F --closed-by S --run-stamp T --repo-root R`: refuses with
  exit 3 when the row under R is missing or its digest differs. V is the
  closing stage kind; the status written is `closed_values[V]`. Only the
  status and stamp fields change, through `frontmatter.primitives`: a
  whole-document-YAML row (the schema's `match_mode`) is edited as a whole,
  and a fenced row gets the evidence paragraph appended to its body. A stamp
  field the schema types `format: date` takes YYYY-MM-DD from the stamp. The
  result is validated against the profile's schema (a bare name resolves to
  the engine's vendored `frontmatter/schemas/`), written to
  `<archive_path>/<YYYY-MM>/`, and only then is the source removed. Prints
  `{"old": ..., "new": ...}`.
- `settle --profile P --row-id R`: deletes
  `state/queue-grind/<P>/<R>.jsonl` if present — idempotent (already-settled
  is not an error). Only the committer calls this, immediately before the
  settling commit (§ Design § Stage library, `commit` (ledger-only)).
- `sweep --profile-dir D --profile P --queue Q [--queue Q ...] --repo-root R`:
  settles (deletes) every ledger and `_handback` mark whose row id the named
  queue dirs no longer yield — the settle for a row that left the queue by a
  hand closure or a denied committer rather than the committer's own settle.
- `run-record --profile P --run-id T --record-file F --repo-root D`: writes
  `state/queue-grind/<P>/runs/<T>.json` atomically (temp file + `os.replace`),
  contained under `--repo-root`, from the JSON at `--record-file` (or stdin
  when `F` is `-`). The committer agent has no Write tool, so the drain
  commit's run-cost record is written through this verb instead of being
  hand-written, and the write is declared like every other row mutation.

Every one of check/append/close/settle/run-record's ACTUAL writes/moves/
deletes is declared via `session.declared_writes.declare_write` (DR-276),
so the session that runs the verb holds the touch-claim the committer's
own `scoped_git_commit` scope check needs.

None of the five touches the index or spawns git (§ Design § Row verbs,
"None of the four touches the index").

Exit codes (locally scoped, matching the row's own contract):
  0 — ok
  1 — refusal (schema validation failure, an existing archive destination)
  2 — usage (malformed/missing arguments, unknown verb)
  3 — manifest-stale (digest mismatch or vanished row)

Negative-spec:
    - Does NOT shell out to git anywhere in this module — `os.replace` is a
      plain filesystem rename, not `git mv`, and is exactly what leaves the
      index untouched (an unstaged delete at the old path, an untracked add
      at the new one).
    - Does NOT reflow or re-serialize the whole YAML document on `close` —
      unlike the retired prior art (`tasks/backlog-grind-2026-09-21/
      close_row.py` at 07fd22c638, which round-tripped the row through the
      PyYAML safe-load/safe-dump pair), every untouched frontmatter field
      and the pre-existing body text are preserved byte-for-byte; only the
      closure fields and the appended evidence paragraph change.
    - Does NOT read `closed_by` from an environment variable — always
      `--closed-by` (the cmd.exe carrier rule: `VAR=value command` is not a
      line `cmd.exe` parses).
    - Does NOT own the profile file's full validation (unknown-key refusal,
      graph rules) — that is `grind_profile.py`'s job (C3); `close` loads
      the profile via `grind_profile.load_profile` and reads only the
      `closure`, `archive_path`, `schema` fields it needs.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from coordinator_core.contract.grind_vocab import (
    CLOSURE_CLOSING_BRANCHES,
    LEDGER_LINE_FIELDS,
)
from coordinator_core.frontmatter import primitives as fm_primitives
from coordinator_core.frontmatter import schema_validate
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit import grind_profile
from coordinator_core.session.declared_writes import declare_write

EXIT_OK = 0
EXIT_REFUSAL = 1
EXIT_USAGE = 2
EXIT_MANIFEST_STALE = 3

#: The sentinel constant name `check` looks for in the emitted `.mjs` script.
#: C7's composer must embed the frozen manifest under this exact name.
_MANIFEST_CONST_NAME = "QUEUE_GRIND_MANIFEST"


def _usage(msg: str) -> int:
    print(f"grind-row: {msg}", file=sys.stderr)
    return EXIT_USAGE


def _parse_flags(rest: list[str], required: tuple[str, ...], optional: tuple[str, ...] = ()) -> dict[str, str] | None:
    """Parse `--flag value` pairs. Returns `None` (never raises) on a usage
    error — a missing value, an unrecognized flag, or a repeated flag — so
    every verb's own caller can turn that into `_usage(...)` with its own
    message. Every flag in `required` must be present exactly once; a flag
    in `optional` may be absent."""
    known = set(required) | set(optional)
    seen: dict[str, str] = {}
    i = 0
    while i < len(rest):
        token = rest[i]
        if not token.startswith("--"):
            return None
        name = token[2:]
        if name not in known:
            return None
        if name in seen:
            return None
        if i + 1 >= len(rest):
            return None
        seen[name] = rest[i + 1]
        i += 2
    if not all(flag in seen for flag in required):
        return None
    return seen


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _declare_before_unlink(path: Path, repo_root: Path) -> None:
    """Record `path`'s touch-claim NOW, immediately, while it still exists
    on disk -- then the caller may safely delete it.

    `declare_write` alone is not enough for a path about to be deleted: it
    only appends to the open collection, and recording (the check that
    `ipc._resolve_declared_touch_root_and_path` gates on "is this currently
    an existing regular file") happens on the OUTER `recording_declared_
    writes()` context's `__exit__` -- which, for every `grind-row` verb, is
    after this whole `main()` call returns, i.e. strictly after the delete
    already happened. A nested `recording_declared_writes()` scope records
    on ITS OWN `__exit__`, immediately, before this function returns --
    nesting is a documented, supported shape of `session.declared_writes.
    collecting` (see that module's own docstring), not a second recorder."""
    from coordinator_core.cli_entry import recording_declared_writes

    with recording_declared_writes(cwd=str(repo_root)):
        declare_write(path)


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def _extract_manifest(script_text: str) -> list[dict[str, Any]]:
    """Extract the frozen manifest entries from the emitted script's own
    `const QUEUE_GRIND_MANIFEST = {"entries": [...], "digest": ...};`
    sentinel line (the exact shape `grind_compose._manifest_const` emits —
    an object, not a bare array, so `check` reads the SAME const shape the
    composer writes). The composer emits it via `json.dumps`, which is also
    valid JS object-literal syntax, so a plain `json.loads` over the
    captured span works without a JS parser. The span is found via
    `json.JSONDecoder().raw_decode` (stdlib incremental parser: reads
    exactly one JSON value starting at `body_start`, honouring string
    escapes itself — never a hand-written brace/string-escape balancer),
    since a row's `path`/`row_id` may itself contain a `;`. Raises
    `ValueError` naming the sentinel when it is absent or unparseable —
    never a silent empty manifest."""
    marker = f"const {_MANIFEST_CONST_NAME} = "
    start = script_text.find(marker)
    if start == -1:
        raise ValueError(
            f"grind-row check: no `{marker.strip()}` sentinel found in the "
            "manifest script"
        )
    body_start = start + len(marker)
    if body_start >= len(script_text) or script_text[body_start] != "{":
        raise ValueError(
            f"grind-row check: `{_MANIFEST_CONST_NAME}` sentinel is not a "
            "JSON object"
        )
    try:
        parsed, _end = json.JSONDecoder().raw_decode(script_text, body_start)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"grind-row check: `{_MANIFEST_CONST_NAME}` sentinel is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("entries"), list):
        raise ValueError(
            f"grind-row check: `{_MANIFEST_CONST_NAME}` sentinel carries no "
            "`entries` array"
        )
    return parsed["entries"]


def _extract_batch_rows(script_text: str, batch_id: str) -> Optional[list[str]]:
    """Row ids of ``batch_id`` from the script's one-line
    `const BATCHES = [...];` (the composer writes it with `json.dumps`).
    None when the batch id is absent."""
    marker = "const BATCHES = "
    for line in script_text.splitlines():
        if line.startswith(marker) and line.endswith(";"):
            try:
                batches = json.loads(line[len(marker):-1])
            except json.JSONDecodeError as exc:
                raise ValueError(f"grind-row check: `BATCHES` is not valid JSON: {exc}") from exc
            for batch in batches:
                if batch.get("id") == batch_id:
                    return list(batch.get("rows", []))
            return None
    raise ValueError("grind-row check: no `const BATCHES = ` line found in the manifest script")


def cmd_check(rest: list[str]) -> int:
    flags = _parse_flags(rest, required=("manifest", "batch", "repo-root"))
    if flags is None:
        return _usage("usage: grind-row check --manifest <script> --batch <batch-id> --repo-root <dir>")
    repo_root = Path(flags["repo-root"])

    script_path = Path(flags["manifest"])
    if not script_path.is_file():
        return _usage(f"grind-row check: manifest script not found: {script_path}")

    try:
        script_text = script_path.read_text(encoding="utf-8")
        manifest = _extract_manifest(script_text)
        batch_rows = _extract_batch_rows(script_text, flags["batch"])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    if batch_rows is None:
        return _usage(f"grind-row check: batch {flags['batch']!r} is not in the manifest script")

    stale: list[str] = []
    vanished: list[str] = []
    wanted = set(batch_rows)
    for entry in manifest:
        if entry.get("row_id") not in wanted:
            continue
        row_id = entry.get("row_id")
        entry_path = repo_root / entry.get("path", "")
        current_digest = _sha256_file(entry_path)
        if current_digest is None:
            vanished.append(row_id)
        elif current_digest != entry.get("digest"):
            stale.append(row_id)

    print(json.dumps({"stale": stale, "vanished": vanished}, sort_keys=True))
    return EXIT_OK


# ---------------------------------------------------------------------------
# append
# ---------------------------------------------------------------------------


class RowIdEscapeError(ValueError):
    """Raised when a `--row-id` value carries a path separator or `..` —
    refused before it is ever joined onto the ledger directory (S3): an
    unvalidated row id is a path-traversal surface into an arbitrary
    `*.jsonl` under (or outside) `state/queue-grind/<profile>/`."""


def _check_row_id(row_id: str) -> None:
    if "/" in row_id or "\\" in row_id or row_id in (".", "..") or ".." in Path(row_id).parts:
        raise RowIdEscapeError(f"grind-row: --row-id escapes the ledger directory: {row_id!r}")


def _ledger_path(repo_root: Path, profile: str, row_id: str) -> Path:
    """`<repo_root>/state/queue-grind/<profile>/<row_id>.jsonl`.

    Resolved under an EXPLICIT `repo_root` (S3) — never `Path("state")` /
    `Path.cwd()`, which diverges from `queue_select._read_ledger_lines`'s
    own `repo_root`-anchored read the moment a caller's cwd is not the repo
    root, and reads/writes two different files for the SAME row."""
    _check_row_id(row_id)
    return Path(repo_root) / "state" / "queue-grind" / profile / f"{row_id}.jsonl"


def cmd_append(rest: list[str]) -> int:
    flags = _parse_flags(
        rest,
        required=(
            "profile",
            "row-id",
            "digest",
            "stage",
            "verdict",
            "outcome",
            "evidence-file",
            "run-stamp",
            "repo-root",
        ),
    )
    if flags is None:
        return _usage(
            "usage: grind-row append --profile P --row-id R --digest D --stage S "
            "--verdict V --outcome O --evidence-file F --run-stamp T --repo-root D"
        )

    record = {
        "profile": flags["profile"],
        "row_id": flags["row-id"],
        "digest": flags["digest"],
        "stage": flags["stage"],
        "verdict": flags["verdict"],
        "outcome": flags["outcome"],
        "evidence_file": flags["evidence-file"],
        "run_stamp": flags["run-stamp"],
    }
    assert set(record) == set(LEDGER_LINE_FIELDS), (
        f"grind_rows.cmd_append: record keys {sorted(record)} drifted from "
        f"grind_vocab.LEDGER_LINE_FIELDS {sorted(LEDGER_LINE_FIELDS)}"
    )
    line = json.dumps(record, sort_keys=True)

    repo_root = Path(flags["repo-root"])
    try:
        ledger_path = _ledger_path(repo_root, flags["profile"], flags["row-id"])
    except RowIdEscapeError as exc:
        return _usage(str(exc))
    ledger_path.parent.mkdir(parents=True, exist_ok=True)

    if ledger_path.is_file():
        existing_lines = ledger_path.read_text(encoding="utf-8").splitlines()
        if line in existing_lines:
            # Idempotence under a retried agent (§ Design § Row verbs Tests):
            # the same append call landing twice must not duplicate the line.
            return EXIT_OK

    with open(ledger_path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")
    declare_write(ledger_path)
    return EXIT_OK


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


def _archive_month(run_stamp: str) -> str:
    return _stamp_date(run_stamp)[:7]


def _stamp_date(run_stamp: str) -> str:
    """YYYY-MM-DD from a run stamp (`20260922T110458Z` or ISO-8601), for a
    stamp field the queue schema types `format: date`.

    Assumes a fixed-width `YYYYMMDD`/`YYYY-MM-DD...` prefix (zero-padded
    month/day); a non-zero-padded stamp fails the length-8 check below and
    raises loudly rather than silently mis-dating."""
    digits = run_stamp.replace("-", "")[:8]
    if len(digits) != 8 or not digits.isdigit():
        raise ValueError(f"grind-row close: run stamp {run_stamp!r} carries no YYYYMMDD date")
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"


def _resolve_schema_path(schema: str, repo_root: Path) -> Path:
    """A bare schema file name resolves against the engine's vendored
    schemas (how DoE's profiles name them); a path resolves under repo_root."""
    if "/" not in schema and "\\" not in schema:
        return Path(schema_validate.__file__).resolve().parent / "schemas" / schema
    return repo_root / schema


def cmd_close(rest: list[str]) -> int:
    flags = _parse_flags(
        rest,
        required=(
            "profile-dir",
            "profile",
            "row",
            "digest",
            "verdict",
            "evidence-file",
            "closed-by",
            "run-stamp",
            "repo-root",
        ),
    )
    if flags is None:
        return _usage(
            "usage: grind-row close --profile-dir D --profile P --row <path> "
            "--digest D --verdict V --evidence-file F --closed-by S --run-stamp T "
            "--repo-root D"
        )

    repo_root = Path(flags["repo-root"])
    row_path = contained_path(repo_root / flags["row"], [repo_root])
    if row_path is None:
        return _usage(
            f"grind-row close: --row escapes --repo-root: {flags['row']!r} not "
            f"under {repo_root!r}"
        )
    current_digest = _sha256_file(row_path)
    if current_digest is None:
        print(f"grind-row close: row not found: {row_path}", file=sys.stderr)
        return EXIT_MANIFEST_STALE
    if current_digest != flags["digest"]:
        print(
            f"grind-row close: digest mismatch for {row_path} "
            f"(expected {flags['digest']}, found {current_digest})",
            file=sys.stderr,
        )
        return EXIT_MANIFEST_STALE

    verdict = flags["verdict"]
    if verdict not in CLOSURE_CLOSING_BRANCHES:
        return _usage(
            f"grind-row close: --verdict must be one of "
            f"{sorted(CLOSURE_CLOSING_BRANCHES)}, got {verdict!r}"
        )

    try:
        profile = grind_profile.load_profile(flags["profile"], Path(flags["profile-dir"]))
    except ValueError as exc:
        print(f"grind-row close: {exc}", file=sys.stderr)
        return EXIT_USAGE

    closure = profile.closure
    closed_values = closure.closed_values
    if verdict not in closed_values:
        return _usage(
            f"grind-row close: profile's closed_values carries no entry for {verdict!r}"
        )
    status_value = closed_values[verdict]
    status_field = closure.status_field
    stamp_fields = closure.stamp_fields

    schema_obj_path = _resolve_schema_path(profile.schema, repo_root)
    if not schema_obj_path.is_file():
        print(f"grind-row close: schema file not found: {schema_obj_path}", file=sys.stderr)
        return EXIT_REFUSAL
    schema_obj = json.loads(schema_obj_path.read_text(encoding="utf-8"))
    properties = schema_obj.get("properties") or {}

    def _stamp_value(field_name: str) -> Any:
        if field_name == "closed_by":
            return flags["closed-by"]
        if (properties.get(field_name) or {}).get("format") == "date":
            return _stamp_date(flags["run-stamp"])
        return flags["run-stamp"]

    text = row_path.read_text(encoding="utf-8")
    whole_document = schema_obj.get("match_mode") == "whole-document-yaml"
    if whole_document:
        fm_text = text
    else:
        split = fm_primitives.split_frontmatter(text)
        if split is None:
            print(f"grind-row close: {row_path} carries no parseable frontmatter", file=sys.stderr)
            return EXIT_REFUSAL
        fm_text = split.fm_text

    for field_name, value in [(status_field, status_value)] + [(f, _stamp_value(f)) for f in stamp_fields]:
        fm_text = (
            fm_primitives.replace_fm_field(fm_text, field_name, value)
            if fm_primitives.read_fm_field(fm_text, field_name) is not None
            else fm_primitives.insert_fm_field(fm_text, field_name, value)
        )

    if whole_document:
        new_text = fm_text if fm_text.endswith("\n") else fm_text + "\n"
    else:
        evidence_text = Path(flags["evidence-file"]).read_text(encoding="utf-8").strip()
        body = split.body_with_leading_newline.rstrip("\n")
        body += (
            f"\n\nCLOSED {flags['run-stamp']} ({flags['closed-by']}, {verdict}): "
            f"{evidence_text}\n"
        )
        new_text = fm_primitives.rebuild(
            fm_primitives.FrontmatterSplit(
                preamble=split.preamble, fm_text=fm_text, body_with_leading_newline=body
            ),
            fm_text,
        )

    fm_dict = schema_validate.parse_yaml(fm_text)
    result = schema_validate.validate_frontmatter_obj(fm_dict, schema_obj)
    if not (isinstance(result, dict) and result.get("ok")):
        print(f"grind-row close: schema validation failed: {result}", file=sys.stderr)
        return EXIT_REFUSAL

    month = _archive_month(flags["run-stamp"])
    archive_dir = repo_root / profile.archive_path / month
    new_path = archive_dir / row_path.name
    if new_path.exists():
        # Reconcile a crash between the prior run's os.replace and unlink
        # (S4): if the archive copy already matches what THIS run would
        # write and the source row is still present, finish the interrupted
        # removal instead of refusing forever on a duplicate a human would
        # otherwise have to clean up by hand.
        if new_path.read_bytes() == new_text.encode("utf-8"):
            _declare_before_unlink(row_path, repo_root)
            row_path.unlink()
            declare_write(new_path)
            print(json.dumps({"old": str(row_path), "new": str(new_path)}, sort_keys=True))
            return EXIT_OK
        print(f"grind-row close: destination already exists: {new_path}", file=sys.stderr)
        return EXIT_REFUSAL

    # Atomic (S4): write the edited text to the DESTINATION via temp file +
    # os.replace, only THEN remove the source -- never mutate the live row
    # in place first. A failed write/replace between the two steps leaves
    # the source untouched and re-closable; the old shape wrote the edited
    # text over the live row before the move, so a failed replace left a
    # mutated row with a digest that no longer matched the manifest.
    archive_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = archive_dir / f".{row_path.name}.tmp-{os.getpid()}"
    tmp_path.write_text(new_text, encoding="utf-8")
    os.replace(tmp_path, new_path)
    _declare_before_unlink(row_path, repo_root)
    row_path.unlink()
    declare_write(new_path)

    print(json.dumps({"old": str(row_path), "new": str(new_path)}, sort_keys=True))
    return EXIT_OK


# ---------------------------------------------------------------------------
# settle
# ---------------------------------------------------------------------------


def cmd_settle(rest: list[str]) -> int:
    flags = _parse_flags(rest, required=("profile", "row-id", "repo-root"))
    if flags is None:
        return _usage("usage: grind-row settle --profile P --row-id R --repo-root D")

    repo_root = Path(flags["repo-root"])
    try:
        ledger_path = _ledger_path(repo_root, flags["profile"], flags["row-id"])
    except RowIdEscapeError as exc:
        return _usage(str(exc))
    if ledger_path.is_file():
        _declare_before_unlink(ledger_path, repo_root)
        ledger_path.unlink()
    return EXIT_OK


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------


def cmd_sweep(rest: list[str]) -> int:
    """`sweep --profile-dir D --profile P --queue Q [--queue Q ...] --repo-root R`:
    settles every ledger and hand-back mark under `state/queue-grind/<P>/`
    whose row id no named queue dir (nor the profile's source op) yields.

    Trap: a row that leaves the queue by any route but the committer's settle
    (a hand closure after a hand-back, a denied committer) orphans its ledger,
    and a later row restored at the same id inherits it — including a
    `route-to-*` mark that declines the row regardless of digest. The caller
    names the FULL queue set, as for emit; a queue dir left out reads as
    closed and its rows' ledgers are settled. Prints `{"settled": [...]}`."""
    from coordinator_core.ops.dispatch_emit.queue_select import live_row_ids

    queue: list[str] = []
    others: list[str] = []
    i = 0
    while i < len(rest):
        if rest[i] == "--queue" and i + 1 < len(rest):
            queue.append(rest[i + 1])
            i += 2
            continue
        others.append(rest[i])
        i += 1
    flags = _parse_flags(others, required=("profile-dir", "profile", "repo-root"))
    if flags is None or not queue:
        return _usage(
            "usage: grind-row sweep --profile-dir D --profile P --queue Q [--queue Q ...] --repo-root R"
        )

    repo_root = Path(flags["repo-root"])
    queue_dirs: list[Path] = []
    for q in queue:
        guarded = contained_path(Path(q), [repo_root])
        if guarded is None or not guarded.is_dir():
            return _usage(f"grind-row sweep: --queue is not a directory under --repo-root: {q!r}")
        queue_dirs.append(guarded)
    try:
        profile = grind_profile.load_profile(flags["profile"], Path(flags["profile-dir"]))
    except Exception as exc:  # noqa: BLE001 -- surfaced as a refusal, never swallowed
        print(f"grind-row sweep: profile refused: {exc}", file=sys.stderr)
        return EXIT_REFUSAL
    live = live_row_ids(
        queue_dirs, row_id_key=profile.row_id_key, repo_root=repo_root, source=profile.source
    )

    ledger_dir = repo_root / "state" / "queue-grind" / profile.name
    candidates = [(p.stem, p) for p in sorted(ledger_dir.glob("*.jsonl"))]
    # Keyed by the record's `row` field, as `queue_select._read_handback_marks`
    # reads it — the file name is not the key.
    for p in sorted((ledger_dir / "_handback").glob("*.json")):
        try:
            row = json.loads(p.read_text(encoding="utf-8")).get("row")
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            continue
        if isinstance(row, str):
            candidates.append((row, p))
    settled: list[str] = []
    for row_id, path in candidates:
        if row_id in live or not path.is_file():
            continue
        _declare_before_unlink(path, repo_root)
        path.unlink()
        settled.append(path.relative_to(repo_root).as_posix())
    print(json.dumps({"settled": settled}, sort_keys=True))
    return EXIT_OK


# ---------------------------------------------------------------------------
# run-record
# ---------------------------------------------------------------------------


def cmd_run_record(rest: list[str]) -> int:
    """`run-record --profile P --run-id T --record-file F --repo-root D`:
    atomically writes `<repo_root>/state/queue-grind/<P>/runs/<T>.json` from
    the JSON at `--record-file` (or stdin when `--record-file -`), contained
    under `--repo-root`. The sole route the committer agent has for the
    drain run record -- it carries no Write tool, so it invokes this verb
    instead of hand-writing the file, and the write is declared like every
    other row-verb mutation (§ Design § Row verbs, `run-record`)."""
    flags = _parse_flags(
        rest, required=("profile", "run-id", "record-file", "repo-root")
    )
    if flags is None:
        return _usage(
            "usage: grind-row run-record --profile P --run-id T --record-file F "
            "--repo-root D"
        )

    repo_root = Path(flags["repo-root"])
    record_file = flags["record-file"]
    if record_file == "-":
        raw_text = sys.stdin.read()
    else:
        record_path = Path(record_file)
        if not record_path.is_file():
            return _usage(f"grind-row run-record: --record-file not found: {record_path}")
        raw_text = record_path.read_text(encoding="utf-8")

    try:
        record = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        print(f"grind-row run-record: --record-file is not valid JSON: {exc}", file=sys.stderr)
        return EXIT_USAGE

    run_id = flags["run-id"]
    if "/" in run_id or "\\" in run_id or run_id in (".", "..") or ".." in Path(run_id).parts:
        return _usage(f"grind-row run-record: --run-id escapes the runs directory: {run_id!r}")

    target = repo_root / "state" / "queue-grind" / flags["profile"] / "runs" / f"{run_id}.json"
    contained = contained_path(target, [repo_root])
    if contained is None:
        return _usage(
            f"grind-row run-record: resolved path escapes --repo-root: {target!r}"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.parent / f".{target.name}.tmp-{os.getpid()}"
    tmp_path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, target)
    declare_write(target)

    print(json.dumps({"path": str(target)}, sort_keys=True))
    return EXIT_OK


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

_VERBS = {
    "check": cmd_check,
    "append": cmd_append,
    "close": cmd_close,
    "settle": cmd_settle,
    "sweep": cmd_sweep,
    "run-record": cmd_run_record,
}


def main(argv: list[str]) -> int:
    if not argv:
        return _usage("usage: grind-row check|append|close|settle|sweep|run-record [...]")
    verb, rest = argv[0], argv[1:]
    handler = _VERBS.get(verb)
    if handler is None:
        return _usage(f"unrecognized grind-row verb: {verb!r}")
    return handler(rest)
