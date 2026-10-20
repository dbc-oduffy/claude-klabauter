"""
coordinator_core.backlog_grind_assemble.grind_rows — the `backlog-grind-assemble
grind-row` verb family: `check`, `append`, `close`, `settle`.

Small primitives the queue-grind engine's emitted `.mjs` script shells out to
(via `coordinator-invoke` in a real run, and directly here for tests). None
stages the git index or spawns git, except `run-record`, which lands the run's
own bookkeeping through the in-process `ceremony.commit_v2` handler; the
per-row `commit` stage kind stays the only other committer and never names a
`state/queue-grind/` path.

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
  `{"old": ..., "new": ...}`. After a successful close (fresh or
  crash-reconcile) the closer's own row claim (`claims.ROW_CLAIM_CLASS`) is
  released; the release is holder-checked, so a peer's claim is untouched,
  and filesystem-only. A refused or failed close releases nothing.
- `settle --profile P --row-id R`: deletes
  `state/queue-grind/<P>/<R>.jsonl` if present — idempotent (already-settled
  is not an error). Only the committer calls this, immediately before the
  settling commit (§ Design § Stage library, `commit` (ledger-only)).
- `sweep --profile-dir D --profile P --queue Q [--queue Q ...] --repo-root R`:
  settles (deletes) every ledger and `_handback` mark whose row id the named
  queue dirs no longer yield — the settle for a row that left the queue by a
  hand closure or a denied committer rather than the committer's own settle.
- `run-record --profile P --run-id T [--record-file F|-] --repo-root D`: writes
  `state/queue-grind/<P>/runs/<T>.json` atomically (temp file + `os.replace`),
  contained under `--repo-root`, from the JSON at `--record-file` (or stdin
  when `F` is `-` or the flag is absent). The committer agent has no Write tool, so the drain
  commit's run-cost record is written through this verb instead of being
  hand-written, and the write is declared like every other row mutation.
  `--sweep --profile-dir D --queue Q [...]` first runs the `sweep` body in-process; a
  sweep refusal or exception never stops the record, which names it as one `stage-dead`
  hand-back and prints `swept: false` (exit 0 covers the record only).

- `settle-run --profile-dir D --profile P --run-id T --queue Q [--queue Q ...]
  --repo-root R [--run-dir RD] [--rows-file F] [--dry-run]`: implemented in
  `grind_settle_run.py`; lands an already-ended run's finished rows through the
  grind terminal commit, re-running no other stage.

- `size-chain --profile P --row-id R --digest D --sizing S --run-stamp T --repo-root D
  -- <sizing-assemble flags>`: implemented in `grind_size_chain.py`; skips when a
  superseded predecessor already carries route-to-plan, else runs sizing-assemble,
  coordinator-doc-new and the ledger mark; any failing step writes
  `<item>.size.md` and appends a `size`/`fail` ledger line.

- `size-fail --profile P --row-id R --digest D --run-stamp T --repo-root D --reason TEXT`:
  the same fail record for a size stage that ran no chain.

- `undo --paths-urlenc E --repo-root R`: implemented in `grind_undo.py` (the
  one verb here that spawns git): puts each named path back to HEAD when this
  session declared it and no live peer holds it.

Every one of check/append/close/settle/run-record's ACTUAL writes/moves/
deletes is declared via `session.declared_writes.declare_write` (DR-276),
so the session that runs the verb holds the touch-claim the committer's
own `scoped_git_commit` scope check needs.

Only `run-record` commits (see its docstring); the other verbs leave the
index untouched.

Exit codes (locally scoped, matching the row's own contract):
  0 — ok
  1 — refusal (schema validation failure, an existing archive destination)
  2 — usage (malformed/missing arguments, unknown verb)
  3 — manifest-stale (digest mismatch or vanished row)

Negative-spec:
    - Does NOT shell out to git anywhere in this module (the claim release is
      spawn-free) — `os.replace` is a plain filesystem rename, not `git mv`, and is exactly what leaves the
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

import yaml

from coordinator_core.backlog_grind_assemble.grind_undo import cmd_undo
from coordinator_core.contract.grind_vocab import (
    CLOSURE_CLOSING_BRANCHES,
    LEDGER_LINE_FIELDS,
)
from coordinator_core.frontmatter import primitives as fm_primitives
from coordinator_core.frontmatter import schema_validate
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit import grind_profile
from coordinator_core.session import claims
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


def _parse_flags(
    rest: list[str], required: tuple[str, ...], optional: tuple[str, ...] = (), bare: tuple[str, ...] = ()
) -> dict[str, str] | None:
    """Parse `--flag value` pairs. Returns `None` (never raises) on a usage
    error — a missing value, an unrecognized flag, or a repeated flag — so
    every verb's own caller can turn that into `_usage(...)` with its own
    message. Every flag in `required` must be present exactly once; a flag
    in `optional` may be absent; a flag in `bare` takes no value and reads `""`."""
    known = set(required) | set(optional) | set(bare)
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
        if name in bare:
            seen[name] = ""
            i += 1
            continue
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


def _declare_under_repo_root(path: Path, repo_root: Path) -> None:
    """Record `path`'s touch-claim now, anchored on the verb's `--repo-root`.

    The outer `recording_declared_writes()` scope opened by the CLI entry
    anchors on `os.getcwd()`; a dispatched agent's cwd is rarely the repo it
    operates on, and `ipc._resolve_declared_touch_root_and_path` silently
    skips a path outside that anchor, leaving it unclaimed so a later
    same-session committer denies it as an orphan. A nested scope records on
    its own `__exit__`, before this returns -- which is also why a path about
    to be unlinked must be declared through here first: the outer scope
    records after `main()` returns, when the file is already gone.

    Negative spec: never call bare `declare_write` from a grind-row verb."""
    from coordinator_core.cli_entry import recording_declared_writes

    with recording_declared_writes(cwd=str(repo_root)):
        declare_write(path)


def _declare_before_unlink(path: Path, repo_root: Path) -> None:
    """Record `path`'s touch-claim NOW, immediately, while it still exists
    on disk -- then the caller may safely delete it. See
    `_declare_under_repo_root`'s docstring for why this must anchor on the
    verb's own `--repo-root` rather than the outer scope's ambient cwd, and
    for the timing half of the same fix."""
    _declare_under_repo_root(path, repo_root)


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


def evidence_rel(profile: str, run_id: str, item_id: str, kind: str) -> str:
    """POSIX repo-relative `state/queue-grind/<profile>/evidence/<run_id>/<item_id>.<kind>.md`.

    `kind` is `done`, `verify`, `judgment` or `size`. Segments are joined unchecked: callers pass
    literals or dispatch-time placeholders; an agent-supplied path is checked
    in `cmd_append`. Pure string work."""
    if kind not in ("done", "verify", "judgment", "size"):
        raise ValueError(f"grind-row: evidence kind must be 'done', 'verify', 'judgment' or 'size': {kind!r}")
    return f"state/queue-grind/{profile}/evidence/{run_id}/{item_id}.{kind}.md"


def _evidence_file_refusal(evidence_file: str, repo_root: Path) -> str | None:
    """Refusal text for an `--evidence-file` outside `repo_root` or under a `scratch`
    segment, else None. Pure path arithmetic; the file need not exist."""
    home = "state/queue-grind/<profile>/evidence/<run-id>/<item-id>.done.md"
    normalised = evidence_file.replace("\\", "/")
    root = os.path.normpath(os.path.abspath(repo_root)).replace("\\", "/")
    full = os.path.normpath(os.path.join(root, normalised)).replace("\\", "/")
    # A Windows drive path is absolute on every host; join() does not see it as such on POSIX.
    if len(normalised) > 1 and normalised[1] == ":" and normalised[0].isalpha():
        full = os.path.normpath(normalised).replace("\\", "/")
    rel = os.path.relpath(full, root).replace("\\", "/")
    if rel == ".." or rel.startswith("../") or os.path.isabs(rel):
        return f"--evidence-file is outside the repo; write it under {home}"
    if "scratch" in rel.split("/"):
        return f"--evidence-file is under a scratch directory; write it under {home}"
    return None


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

    refusal = _evidence_file_refusal(flags["evidence-file"], Path(flags["repo-root"]))
    if refusal is not None:
        return _usage(refusal)

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
    _declare_under_repo_root(ledger_path, repo_root)
    return EXIT_OK


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


def _archive_month(run_stamp: str) -> str:
    return _stamp_date(run_stamp)[:7]


def archive_destination(archive_path: str, run_stamp: str, row_name: str) -> str:
    """Repo-relative POSIX path a closed row lands at: `<archive_path>/<YYYY-MM>/<row_name>`.
    The one place the month bucket is decided; `close` writes here and the
    emitted workflow's commit stage names the same path."""
    return f"{archive_path.rstrip('/')}/{_archive_month(run_stamp)}/{row_name}"


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

    new_path = repo_root / archive_destination(profile.archive_path, flags["run-stamp"], row_path.name)
    archive_dir = new_path.parent
    if new_path.exists():
        # Reconcile a crash between the prior run's os.replace and unlink
        # (S4): if the archive copy already matches what THIS run would
        # write and the source row is still present, finish the interrupted
        # removal instead of refusing forever on a duplicate a human would
        # otherwise have to clean up by hand.
        if new_path.read_bytes() == new_text.encode("utf-8"):
            _declare_before_unlink(row_path, repo_root)
            row_path.unlink()
            _declare_under_repo_root(new_path, repo_root)
            _release_row_claim(row_path, repo_root)
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
    tmp_path.write_text(new_text, encoding="utf-8", newline="\n")
    os.replace(tmp_path, new_path)
    _declare_before_unlink(row_path, repo_root)
    row_path.unlink()
    _declare_under_repo_root(new_path, repo_root)
    _release_row_claim(row_path, repo_root)

    print(json.dumps({"old": str(row_path), "new": str(new_path)}, sort_keys=True))
    return EXIT_OK


def _release_row_claim(row_path: Path, repo_root: Path) -> None:
    """Give up the closer's own row claim; holder-checked, a no-op for a peer's or absent claim."""
    try:
        claims.release_artifact(claims.ROW_CLAIM_CLASS, str(row_path), cwd=str(repo_root))
    except ValueError:
        pass  # not a <queue-dir>/<row>.yaml path: no row claim can exist for it


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

    try:
        settled = _sweep_ledgers(
            flags["profile"], Path(flags["profile-dir"]), queue, Path(flags["repo-root"])
        )
    except _SweepError as exc:
        if exc.usage:
            return _usage(f"grind-row sweep: {exc}")
        print(f"grind-row sweep: {exc}", file=sys.stderr)
        return EXIT_REFUSAL
    print(json.dumps({"settled": settled}, sort_keys=True))
    return EXIT_OK


class _SweepError(Exception):
    """A sweep that did not run: `usage` marks a bad argument, otherwise a refusal."""

    def __init__(self, message: str, *, usage: bool = False) -> None:
        super().__init__(message)
        self.usage = usage


def _sweep_ledgers(profile_name: str, profile_dir: Path, queue: list[str], repo_root: Path) -> list[str]:
    """The body of `sweep`: unlinks (declared) every orphaned ledger and hand-back mark
    and returns their repo-relative paths. Raises `_SweepError` before any unlink."""
    from coordinator_core.ops.dispatch_emit.queue_select import live_row_ids

    queue_dirs: list[Path] = []
    for q in queue:
        guarded = contained_path(Path(q), [repo_root])
        if guarded is None or not guarded.is_dir():
            raise _SweepError(f"--queue is not a directory under --repo-root: {q!r}", usage=True)
        queue_dirs.append(guarded)
    try:
        profile = grind_profile.load_profile(profile_name, profile_dir)
    except Exception as exc:  # noqa: BLE001 -- surfaced as a refusal, never swallowed
        raise _SweepError(f"profile refused: {exc}") from exc
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
    return settled


# ---------------------------------------------------------------------------
# run-record
# ---------------------------------------------------------------------------


def _git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 -- git object id, not security


class _LandSet:
    """What `run-record` commits: `paths` present on disk and differing from HEAD,
    `deleted` gone from disk and present in HEAD, `withheld` held by a live peer.
    `unresolved` (filled by `_split_deletions`) are deletions left out of the commit."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.deleted: list[str] = []
        self.withheld: list[str] = []
        self.unresolved: list[str] = []


def _split_deletions(
    repo_root: Path, profile: "grind_profile.Profile | None", profile_name: str, run_id: str, land: _LandSet
) -> None:
    """Was each removal a closed row or a settled ledger, or a stray? Keeps settled
    ledger removals under `state/queue-grind/<P>/` and rows whose archive copy exists on
    disk (adding that copy to `land.paths` when it differs from HEAD); every other
    deletion moves to `land.unresolved`, uncommitted and still deleted on disk. Zero
    git spawns."""
    from coordinator_core.git.git_state import read_tree_spine

    ledger_prefix = f"state/queue-grind/{profile_name}/"
    kept: list[str] = []
    archives: list[str] = []
    for rel in land.deleted:
        if rel.startswith(ledger_prefix):
            kept.append(rel)
            continue
        if profile is not None:
            archive_rel = archive_destination(profile.archive_path, run_id, rel.rpartition("/")[2])
            if (repo_root / archive_rel).is_file():
                kept.append(rel)
                archives.append(archive_rel)
                continue
        land.unresolved.append(rel)
    land.deleted = kept
    if not archives:
        return
    spine = read_tree_spine(repo_root, archives) or {}
    for archive_rel in archives:
        head_dir, _, head_name = archive_rel.rpartition("/")
        entry = spine.get(head_dir, {}).get(head_name)
        if archive_rel not in land.paths and (
            entry is None or entry[1] != _git_blob_sha((repo_root / archive_rel).read_bytes())
        ):
            land.paths.append(archive_rel)


def _land_set(
    repo_root: Path,
    profile: str,
    roots: tuple[str, ...] | None = None,
    declared: tuple[str, ...] = (),
    baseline_dirty: frozenset[str] = frozenset(),
) -> "_LandSet | str":
    """Which of the run's writes still differ from HEAD. Candidates are the paths THIS
    session claims under `roots` minus `baseline_dirty` (claims that predate the run), the
    `declared` paths under `roots`. `roots=None` is the profile's ledger dir alone. Returns a
    refusal string when HEAD cannot be read.
    Never a directory pathspec; a path a live peer holds is withheld. Zero git spawns."""
    from coordinator_core.git.git_state import head_sha, read_tree_spine
    from coordinator_core.ops.dispatch_emit.grind_commit_contract import under_roots
    from coordinator_core.session import core, scope

    result = _LandSet()
    root_str = str(repo_root)
    sid = core.resolve_session_id(root_str)
    if not sid:
        return result
    roots = roots or (f"state/queue-grind/{profile}/",)
    held = {
        p for p in scope.own_path_claims(sid, cwd=root_str)
        if under_roots(p, roots) and p not in baseline_dirty
    }
    held.update(p for p in declared if under_roots(p, roots))
    candidates = sorted(p for p in held if not (repo_root / p).is_dir())
    if not candidates:
        return result
    if head_sha(repo_root) is None:
        return result  # unborn branch: nothing to land against
    spine = read_tree_spine(repo_root, candidates)
    if spine is None:
        return "HEAD is unreadable; the run's bookkeeping was not committed"

    dirty: list[str] = []
    gone: list[str] = []
    for rel in candidates:
        head_dir, _, head_name = rel.rpartition("/")
        entry = spine.get(head_dir, {}).get(head_name)
        target = repo_root / rel
        if target.is_file():
            if entry is None or entry[1] != _git_blob_sha(target.read_bytes()):
                dirty.append(rel)
        elif entry is not None:
            gone.append(rel)
    contested = scope.contested_by_live_peers(dirty + gone, sid, cwd=root_str) or {}
    result.withheld = sorted(contested)
    result.paths = [p for p in dirty if p not in contested]
    result.deleted = [p for p in gone if p not in contested]
    return result


def _commit_land_set(repo_root: Path, profile: str, run_id: str, land: _LandSet) -> "tuple[str | None, str | None]":
    """`(sha, None)` on success, `(None, refusal)` otherwise, through the in-process
    `ceremony.commit_v2` handler."""
    from coordinator_core.ops.ceremony.commit_v2 import _handler

    ledger_suffix = ".jsonl"
    removed = sum(1 for p in land.deleted if p.endswith(ledger_suffix))
    written = sum(1 for p in land.paths if p.endswith(ledger_suffix))
    subject = f"grind({profile}): run {run_id} bookkeeping"
    if removed:
        subject += f", removed {removed} settled ledgers"
    message = (
        f"{subject}\n\n"
        f"Ledgers removed: {removed}. Ledgers written: {written}. "
        f"Paths committed: {len(land.paths)}, deleted: {len(land.deleted)}.\n"
    )
    try:
        reply = _handler(
            {
                "paths": land.paths,
                "deleted_paths": land.deleted,
                # `_split_deletions` already kept only settled ledgers and archived rows.
                "declared_reverts": land.deleted,
                "message": message,
                # Every path is this session's own claimed bookkeeping, so the
                # handler's check-ignore spawn is skipped by declaring none ignored.
                "ignored_untracked": [],
            },
            repo_root=repo_root,
        )
    except Exception as exc:  # noqa: BLE001 -- surfaced verbatim; the commit outcome is unknown
        return None, f"{type(exc).__name__}: {exc}"
    if reply.get("committed"):
        return reply.get("sha"), None
    if reply.get("already_landed_sha"):
        return reply["already_landed_sha"], None
    return None, str(reply.get("error") or reply)


GRIND_CLOSE_AGENT_DESCRIPTIONS = ("ledger-sweep",)


def _run_usage(repo_root: Path, run_id: str) -> "dict[str, Any] | None":
    """The `usage` block for the run record: scan_run's shape plus `as_of` and `unsettled`
    (agents still live at record time). `{"reason": ...}` when the Workflow run dir is not
    located (the caller records `usage: null` plus `usage_reason`). None when no session or
    session dir resolves (no Workflow run to locate; the record stays unchanged).

    Never raises: usage is evidence beside spend, not a gate on the record."""
    try:
        from coordinator_core.session import core
        from coordinator_core.usage import run_locate
        from coordinator_core.usage.agent_usage import scan_run

        sid = core.resolve_session_id(str(repo_root))
        if not sid:
            return None
        session_dir = run_locate.session_dir_for(sid)
        if session_dir is None:
            return None
        run_dir = run_locate.locate_run_dir(
            session_dir, run_id, agent_descriptions=GRIND_CLOSE_AGENT_DESCRIPTIONS
        )
        if run_dir is None:
            return {"reason": "no unique unsettled close-leg agent names this run id; run dir not located"}
        usage = scan_run(str(run_dir))
        unsettled = run_locate._unsettled_agent_ids(run_dir / "journal.jsonl")
        usage["as_of"] = "run-record"
        usage["unsettled"] = sorted(unsettled)
        return usage
    except Exception as exc:  # noqa: BLE001 -- the record write must still happen
        return {"reason": f"usage scan failed: {type(exc).__name__}: {exc}"}


def cmd_run_record(rest: list[str]) -> int:
    """`run-record --profile P --run-id T [--record-file F|- | --record-urlenc E] --repo-root D`:
    atomically writes `<repo_root>/state/queue-grind/<P>/runs/<T>.json` from
    the JSON at `--record-file`, from stdin when it is `-` or absent, or from
    `--record-urlenc` (the JSON percent-encoded into one shell-safe token -- the
    drain's op-runner passes it that way because an agent asked to pipe a blob
    to stdin "byte for byte" ran the verb with empty stdin), contained
    under `--repo-root`. The sole route the committer agent has for the
    drain run record -- it carries no Write tool, so it invokes this verb
    instead of hand-writing the file, and the write is declared like every
    other row-verb mutation (§ Design § Row verbs, `run-record`).

    Then lands the run's own bookkeeping in one commit through the in-process
    `ceremony.commit_v2` handler (no git spawn on the common shape): the paths this
    session claims under `state/queue-grind/<P>/` (swept and settled ledgers,
    appended ledgers, evidence, the record), each only when it differs from HEAD.
    A path a live peer also claims is withheld and named. Deletions are declared as
    reverts only when settled (a ledger under `state/queue-grind/<P>/`) or resolved (a
    row whose archive copy exists, which then lands too, `--profile-dir` required); any
    other removal stays out of the commit, deleted on disk, and is named in
    `unresolved_deletions`. Prints
    `{"path", "commit", "landed", "deleted", "unresolved_deletions", "withheld"}` (plus `swept`, `sweep_reason`
    under `--sweep`, which needs `--profile-dir` and a `--queue`); `commit` is null when
    nothing was left to land, and also when no session id resolves or the branch
    is unborn (the record is then written but left uncommitted). A commit refusal exits 1 with the verbatim
    refusal on stderr, the record already written."""
    from coordinator_core.ops.dispatch_emit import grind_commit_contract as contract

    queue: list[str] = []
    others: list[str] = []
    i = 0
    while i < len(rest):
        if rest[i] == f"--{contract.QUEUE_FLAG}" and i + 1 < len(rest):
            queue.append(rest[i + 1])
            i += 2
            continue
        others.append(rest[i])
        i += 1
    flags = _parse_flags(
        others, required=("profile", "run-id", "repo-root"),
        optional=("record-file", "record-urlenc", contract.DECLARED_FLAG, contract.DECISIONS_FLAG, "profile-dir"),
        bare=(contract.SWEEP_FLAG,),
    )
    if flags is None:
        return _usage(
            "usage: grind-row run-record --profile P --run-id T "
            "[--record-file F|- | --record-urlenc E] [--queue Q ...] "
            "[--declared-urlenc E] [--decisions-file F] [--sweep --profile-dir D] --repo-root D"
        )
    sweeping = contract.SWEEP_FLAG in flags
    if sweeping and ("profile-dir" not in flags or not queue):
        return _usage("grind-row run-record: --sweep needs --profile-dir and at least one --queue")

    repo_root = Path(flags["repo-root"])
    record_file = flags.get("record-file", "-")
    if "record-urlenc" in flags:
        if "record-file" in flags:
            return _usage("grind-row run-record: pass --record-file or --record-urlenc, not both")
        from urllib.parse import unquote

        raw_text = unquote(flags["record-urlenc"])
    elif record_file == "-":
        raw_text = sys.stdin.read()
    else:
        record_path = Path(record_file)
        if not record_path.is_file():
            return _usage(f"grind-row run-record: --record-file not found: {record_path}")
        raw_text = record_path.read_text(encoding="utf-8")

    if not raw_text.strip():
        print(
            "grind-row run-record: no record on stdin -- feed it with a heredoc: "
            "`... --record-file - --repo-root D <<'RUN_RECORD_JSON'` + JSON line + `RUN_RECORD_JSON`",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        record = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        print(f"grind-row run-record: --record-file is not valid JSON: {exc}", file=sys.stderr)
        return EXIT_USAGE

    missing = [k for k in contract.RUN_RECORD_REQUIRED if not isinstance(record, dict) or k not in record]
    if missing:
        return _usage(f"grind-row run-record: record is missing required keys: {', '.join(missing)}")

    run_id = flags["run-id"]
    if "/" in run_id or "\\" in run_id or run_id in (".", "..") or ".." in Path(run_id).parts:
        return _usage(f"grind-row run-record: --run-id escapes the runs directory: {run_id!r}")

    declared: list[str] = []
    baseline_dirty: set[str] = set()
    if contract.DECLARED_FLAG in flags:
        try:
            declared.extend(contract.decode_declared_paths(flags[contract.DECLARED_FLAG]))
        except ValueError as exc:
            return _usage(f"grind-row run-record: --{contract.DECLARED_FLAG}: {exc}")
    if contract.DECISIONS_FLAG in flags:
        decisions_path = Path(flags[contract.DECISIONS_FLAG])
        try:
            decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
            minted = decisions.get(contract.MINTED_KEY) or []
            dirty_at_fire = decisions.get(contract.BASELINE_DIRTY_KEY) or []
            if not all(isinstance(p, str) for p in [*minted, *dirty_at_fire]):
                raise ValueError("entries must be strings")
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            return _usage(f"grind-row run-record: --{contract.DECISIONS_FLAG} unreadable: {exc}")
        declared.extend(minted)
        baseline_dirty.update(dirty_at_fire)
    declared = [d for d in declared if d and not d.startswith("/") and ".." not in Path(d).parts]
    widened = bool(queue or declared or baseline_dirty or contract.DECISIONS_FLAG in flags)
    roots = None
    if widened:
        queue_rel: list[str] = []
        for q in queue:
            guarded = contained_path(repo_root / q if not Path(q).is_absolute() else Path(q), [repo_root])
            if guarded is None:
                return _usage(f"grind-row run-record: --queue escapes --repo-root: {q!r}")
            queue_rel.append(Path(os.path.relpath(guarded, repo_root.resolve())).as_posix())
        roots = contract.run_write_roots(flags["profile"], queue_rel)

    target = repo_root / "state" / "queue-grind" / flags["profile"] / "runs" / f"{run_id}.json"
    contained = contained_path(target, [repo_root])
    if contained is None:
        return _usage(
            f"grind-row run-record: resolved path escapes --repo-root: {target!r}"
        )

    swept, sweep_reason = True, ""
    if sweeping:
        try:
            _sweep_ledgers(
                flags["profile"], Path(flags["profile-dir"]),
                [str(repo_root / q) if not Path(q).is_absolute() else q for q in queue], repo_root,
            )
        except Exception as exc:  # noqa: BLE001 -- recorded verbatim; the record write must still happen
            swept, sweep_reason = False, str(exc) or type(exc).__name__
    if not swept:
        handed_back = record["handed_back"] if isinstance(record["handed_back"], list) else []
        reason = f"{contract.SWEEP_FAILED_HEAD}: state/queue-grind/{flags['profile']}/runs/{run_id}.json"
        record["handed_back"] = [
            *handed_back, {"row": run_id, "type": "stage-dead", "reason": reason + (f": {sweep_reason}" if sweep_reason else "")}
        ]
        counts = record["counts"] if isinstance(record["counts"], dict) else {}
        by_type = counts.get("by_type") if isinstance(counts.get("by_type"), dict) else {}
        by_type["stage-dead"] = by_type.get("stage-dead", 0) + 1
        counts["by_type"] = by_type
        record["counts"] = counts

    if isinstance(record, dict) and "usage" not in record:
        block = _run_usage(repo_root, run_id)
        if block is not None:
            record["usage"] = block if "agents" in block else None
            if "agents" not in block:
                record["usage_reason"] = block["reason"]

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.parent / f".{target.name}.tmp-{os.getpid()}"
    tmp_path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8", newline="\n")
    os.replace(tmp_path, target)
    _declare_under_repo_root(target, repo_root)

    for entry in record["handed_back"] if isinstance(record["handed_back"], list) else []:
        tradeoff = entry.get("tradeoff") if isinstance(entry, dict) else None
        row_id = entry.get("row") if isinstance(entry, dict) else None
        if not (isinstance(tradeoff, str) and tradeoff.strip() and isinstance(row_id, str)):
            continue
        try:
            _check_row_id(row_id)
        except RowIdEscapeError:
            continue
        judgment = repo_root / evidence_rel(flags["profile"], run_id, row_id, "judgment")
        judgment.parent.mkdir(parents=True, exist_ok=True)
        judgment.write_text(tradeoff, encoding="utf-8", newline="\n")
        _declare_under_repo_root(judgment, repo_root)

    land = _land_set(repo_root, flags["profile"], roots, tuple(declared), frozenset(baseline_dirty))
    if isinstance(land, str):
        print(f"grind-row run-record: {land}", file=sys.stderr)
        return EXIT_REFUSAL
    if land.deleted:
        profile = None
        if "profile-dir" in flags:
            try:
                profile = grind_profile.load_profile(flags["profile"], Path(flags["profile-dir"]))
            except (ValueError, OSError, yaml.YAMLError) as exc:
                print(
                    f"grind-row run-record: profile {flags['profile']} did not load "
                    f"({type(exc).__name__}: {exc}); its queue-row deletions stay unresolved",
                    file=sys.stderr,
                )
        _split_deletions(repo_root, profile, flags["profile"], run_id, land)
    sha: str | None = None
    if land.paths or land.deleted:
        sha, refusal = _commit_land_set(repo_root, flags["profile"], run_id, land)
        if sha is None:
            print(f"grind-row run-record: commit refused: {refusal}", file=sys.stderr)
            return EXIT_REFUSAL
    print(
        json.dumps(
            {
                "path": str(target),
                "commit": sha,
                "landed": land.paths,
                "deleted": land.deleted,
                "unresolved_deletions": land.unresolved,
                "withheld": land.withheld,
                **({"swept": swept, "sweep_reason": sweep_reason} if sweeping else {}),
            },
            sort_keys=True,
        )
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _cmd_settle_run(rest: list[str]) -> int:
    from coordinator_core.backlog_grind_assemble.grind_settle_run import cmd_settle_run

    return cmd_settle_run(rest)


def _cmd_size_fail(rest: list[str]) -> int:
    from coordinator_core.backlog_grind_assemble.grind_size_chain import cmd_size_fail

    return cmd_size_fail(rest)


def _cmd_size_chain(rest: list[str]) -> int:
    from coordinator_core.backlog_grind_assemble.grind_size_chain import cmd_size_chain

    return cmd_size_chain(rest)


_VERBS = {
    "check": cmd_check,
    "append": cmd_append,
    "close": cmd_close,
    "settle": cmd_settle,
    "sweep": cmd_sweep,
    "run-record": cmd_run_record,
    "undo": cmd_undo,
    "settle-run": _cmd_settle_run,
    "size-chain": _cmd_size_chain,
    "size-fail": _cmd_size_fail,
}


def main(argv: list[str]) -> int:
    if not argv:
        return _usage("usage: grind-row check|append|close|settle|sweep|run-record|undo|settle-run|size-chain|size-fail [...]")
    verb, rest = argv[0], argv[1:]
    handler = _VERBS.get(verb)
    if handler is None:
        return _usage(f"unrecognized grind-row verb: {verb!r}")
    return handler(rest)
