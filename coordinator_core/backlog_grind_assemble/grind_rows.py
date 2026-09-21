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

- `check --manifest <script> --batch <id>`: reads the frozen manifest the
  composer embeds in the emitted script as the single-line sentinel constant
  `const QUEUE_GRIND_MANIFEST = <json-array>;` (one JSON array of
  `{row_id, path, digest, batch_key}` entries, § Design § Selector's manifest
  shape). Filters to `batch_key == --batch`, recomputes each entry's digest
  from disk, and prints `{"stale": [...row_id], "vanished": [...row_id]}` —
  stale for a digest mismatch, vanished for a path that no longer exists.
  This module OWNS this sentinel's name; C7's composer must emit the manifest
  under this exact constant name for `check` to find it.
- `append --profile P --row-id R --digest D --stage S --verdict V --outcome O
  --evidence-file F --run-stamp T`: appends one `json.dumps(sort_keys=True)`
  line (LEDGER_LINE_FIELDS order, per `grind_vocab.LEDGER_LINE_FIELDS`) to
  `state/queue-grind/<P>/<R>.jsonl`, opened in append mode with `newline="\n"`.
  Idempotent under a retried agent: an identical line already present (same
  serialized bytes) is not appended twice.
- `close --profile-dir D --profile P --row <path> --digest DIG --verdict V
  --evidence-file F --closed-by S --run-stamp T`: refuses (exit
  `EXIT_MANIFEST_STALE`) when the row's current on-disk digest does not match
  `--digest` or the row is missing. `V` is the CLOSING STAGE KIND (a member
  of `grind_vocab.CLOSURE_CLOSING_BRANCHES`, e.g. `fix` | `refute-close`),
  not a free-form status string — the actual status value written is
  `closed_values[V]` from the profile's closure block. Edits only the closure
  fields via `coordinator_core.frontmatter.primitives`
  (`replace_fm_field`/`insert_fm_field`), appends the evidence paragraph to
  the body block verbatim (no whole-document reflow), validates the result
  with `schema_validate.validate_frontmatter_obj` against the profile's own
  named schema, then moves the edited file with a single `os.replace` into
  `<archive_path>/<YYYY-MM from --run-stamp>/`, refusing an existing
  destination. Prints `{"old": ..., "new": ...}`.
- `settle --profile P --row-id R`: deletes
  `state/queue-grind/<P>/<R>.jsonl` if present — idempotent (already-settled
  is not an error). Only the committer calls this, immediately before the
  settling commit (§ Design § Stage library, `commit` (ledger-only)).

None of the four touches the index or spawns git (§ Design § Row verbs,
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
      graph rules) — that is `grind_profile.py`'s job (C3, not yet landed).
      `close` reads only the two blocks it needs (`closure`, `archive_path`,
      `schema`) via `schema_validate.parse_yaml`, and raises a plain usage
      error naming the missing key if the profile lacks one.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from coordinator_core.contract.grind_vocab import (
    CLOSURE_BLOCK_REQUIRED_FIELDS,
    CLOSURE_CLOSING_BRANCHES,
    LEDGER_LINE_FIELDS,
)
from coordinator_core.frontmatter import primitives as fm_primitives
from coordinator_core.frontmatter import schema_validate

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


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def _extract_manifest(script_text: str) -> list[dict[str, Any]]:
    """Extract the frozen manifest array from the emitted script's own
    `const QUEUE_GRIND_MANIFEST = [...];` sentinel line. The composer emits
    the array via `json.dumps`, which is also valid JS array-literal syntax,
    so a plain `json.loads` over the captured span works without a JS
    parser. Raises `ValueError` naming the sentinel when it is absent or
    unparseable — never a silent empty manifest."""
    marker = f"const {_MANIFEST_CONST_NAME} = "
    start = script_text.find(marker)
    if start == -1:
        raise ValueError(
            f"grind-row check: no `{marker.strip()}` sentinel found in the "
            "manifest script"
        )
    body_start = start + len(marker)
    end = script_text.find(";", body_start)
    if end == -1:
        raise ValueError(
            f"grind-row check: `{_MANIFEST_CONST_NAME}` sentinel has no "
            "terminating ';'"
        )
    raw = script_text[body_start:end]
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"grind-row check: `{_MANIFEST_CONST_NAME}` sentinel is not valid JSON: {exc}"
        ) from exc
    if not isinstance(manifest, list):
        raise ValueError(
            f"grind-row check: `{_MANIFEST_CONST_NAME}` sentinel is not a JSON array"
        )
    return manifest


def cmd_check(rest: list[str]) -> int:
    flags = _parse_flags(rest, required=("manifest", "batch"))
    if flags is None:
        return _usage("usage: grind-row check --manifest <script> --batch <id>")

    script_path = Path(flags["manifest"])
    if not script_path.is_file():
        return _usage(f"grind-row check: manifest script not found: {script_path}")

    try:
        manifest = _extract_manifest(script_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE

    stale: list[str] = []
    vanished: list[str] = []
    for entry in manifest:
        if entry.get("batch_key") != flags["batch"]:
            continue
        row_id = entry.get("row_id")
        entry_path = Path(entry.get("path", ""))
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


def _ledger_path(profile: str, row_id: str) -> Path:
    return Path("state") / "queue-grind" / profile / f"{row_id}.jsonl"


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
        ),
    )
    if flags is None:
        return _usage(
            "usage: grind-row append --profile P --row-id R --digest D --stage S "
            "--verdict V --outcome O --evidence-file F --run-stamp T"
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

    ledger_path = _ledger_path(flags["profile"], flags["row-id"])
    ledger_path.parent.mkdir(parents=True, exist_ok=True)

    if ledger_path.is_file():
        existing_lines = ledger_path.read_text(encoding="utf-8").splitlines()
        if line in existing_lines:
            # Idempotence under a retried agent (§ Design § Row verbs Tests):
            # the same append call landing twice must not duplicate the line.
            return EXIT_OK

    with open(ledger_path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")
    return EXIT_OK


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


def _load_profile_closure(profile_dir: Path, profile: str) -> dict[str, Any]:
    """Minimal profile read for `close`'s own needs: the `closure` block
    (`status_field`/`closed_values`/`stamp_fields`), `archive_path`, and
    `schema` (a repo-relative path to the row's own JSON schema). This is
    NOT `grind_profile.load_profile` (C3, not yet landed) — no graph
    validation, no vocabulary-membership checks, no unknown-key refusal.
    Raises `ValueError` naming the missing key on any gap."""
    profile_path = profile_dir / f"{profile}.yaml"
    if not profile_path.is_file():
        raise ValueError(f"profile file not found: {profile_path}")
    doc = schema_validate.parse_yaml(profile_path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"profile file did not parse to a mapping: {profile_path}")

    closure = doc.get("closure")
    if not isinstance(closure, dict):
        raise ValueError(f"profile {profile_path} carries no `closure` block")
    missing = CLOSURE_BLOCK_REQUIRED_FIELDS - set(closure)
    if missing:
        raise ValueError(
            f"profile {profile_path} closure block missing field(s): {sorted(missing)}"
        )

    archive_path = doc.get("archive_path")
    if not isinstance(archive_path, str) or not archive_path:
        raise ValueError(f"profile {profile_path} carries no `archive_path`")

    schema_rel_path = doc.get("schema")
    if not isinstance(schema_rel_path, str) or not schema_rel_path:
        raise ValueError(f"profile {profile_path} carries no `schema`")

    return {
        "closure": closure,
        "archive_path": archive_path,
        "schema": schema_rel_path,
    }


def _archive_month(run_stamp: str) -> str:
    # `run_stamp` is expected to be (or begin with) an ISO date/timestamp,
    # e.g. "2026-09-21" or "2026-09-21T10:00:00Z" -- "YYYY-MM" is its first
    # 7 characters either way.
    return run_stamp[:7]


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
        ),
    )
    if flags is None:
        return _usage(
            "usage: grind-row close --profile-dir D --profile P --row <path> "
            "--digest D --verdict V --evidence-file F --closed-by S --run-stamp T"
        )

    row_path = Path(flags["row"])
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
        profile_info = _load_profile_closure(Path(flags["profile-dir"]), flags["profile"])
    except ValueError as exc:
        print(f"grind-row close: {exc}", file=sys.stderr)
        return EXIT_USAGE

    closure = profile_info["closure"]
    closed_values = closure["closed_values"]
    if not isinstance(closed_values, dict) or verdict not in closed_values:
        return _usage(
            f"grind-row close: profile's closed_values carries no entry for {verdict!r}"
        )
    status_value = closed_values[verdict]
    status_field = closure["status_field"]
    stamp_fields = closure.get("stamp_fields") or {}

    text = row_path.read_text(encoding="utf-8")
    split = fm_primitives.split_frontmatter(text)
    if split is None:
        print(f"grind-row close: {row_path} carries no parseable frontmatter", file=sys.stderr)
        return EXIT_REFUSAL

    fm_text = split.fm_text
    fm_text = (
        fm_primitives.replace_fm_field(fm_text, status_field, status_value)
        if fm_primitives.read_fm_field(fm_text, status_field) is not None
        else fm_primitives.insert_fm_field(fm_text, status_field, status_value)
    )

    # `stamp_fields` maps a frontmatter field name to which computed value it
    # takes -- "closed_by" or "run_stamp" -- the only two `close` has to give.
    for field_name, source in stamp_fields.items():
        if source == "closed_by":
            value: Any = flags["closed-by"]
        elif source == "run_stamp":
            value = flags["run-stamp"]
        else:
            return _usage(
                f"grind-row close: profile's stamp_fields names an unknown "
                f"source {source!r} for field {field_name!r} (must be "
                "'closed_by' or 'run_stamp')"
            )
        fm_text = (
            fm_primitives.replace_fm_field(fm_text, field_name, value)
            if fm_primitives.read_fm_field(fm_text, field_name) is not None
            else fm_primitives.insert_fm_field(fm_text, field_name, value)
        )

    evidence_text = Path(flags["evidence-file"]).read_text(encoding="utf-8").strip()
    body = split.body_with_leading_newline.rstrip("\n")
    body += (
        f"\n\nCLOSED {flags['run-stamp']} ({flags['closed-by']}, {verdict}): "
        f"{evidence_text}\n"
    )

    new_split = fm_primitives.FrontmatterSplit(
        preamble=split.preamble,
        fm_text=fm_text,
        body_with_leading_newline=body,
    )
    new_text = fm_primitives.rebuild(new_split, fm_text)

    fm_dict = schema_validate.parse_yaml(fm_text)
    repo_root = Path.cwd()
    schema_obj_path = repo_root / profile_info["schema"]
    if not schema_obj_path.is_file():
        print(f"grind-row close: schema file not found: {schema_obj_path}", file=sys.stderr)
        return EXIT_REFUSAL
    schema_obj = json.loads(schema_obj_path.read_text(encoding="utf-8"))
    result = schema_validate.validate_frontmatter_obj(fm_dict, schema_obj)
    if not (isinstance(result, dict) and result.get("ok")):
        print(f"grind-row close: schema validation failed: {result}", file=sys.stderr)
        return EXIT_REFUSAL

    month = _archive_month(flags["run-stamp"])
    archive_dir = repo_root / profile_info["archive_path"] / month
    new_path = archive_dir / row_path.name
    if new_path.exists():
        print(f"grind-row close: destination already exists: {new_path}", file=sys.stderr)
        return EXIT_REFUSAL

    row_path.write_text(new_text, encoding="utf-8")
    archive_dir.mkdir(parents=True, exist_ok=True)
    os.replace(row_path, new_path)

    print(json.dumps({"old": str(row_path), "new": str(new_path)}, sort_keys=True))
    return EXIT_OK


# ---------------------------------------------------------------------------
# settle
# ---------------------------------------------------------------------------


def cmd_settle(rest: list[str]) -> int:
    flags = _parse_flags(rest, required=("profile", "row-id"))
    if flags is None:
        return _usage("usage: grind-row settle --profile P --row-id R")

    ledger_path = _ledger_path(flags["profile"], flags["row-id"])
    if ledger_path.is_file():
        ledger_path.unlink()
    return EXIT_OK


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

_VERBS = {
    "check": cmd_check,
    "append": cmd_append,
    "close": cmd_close,
    "settle": cmd_settle,
}


def main(argv: list[str]) -> int:
    if not argv:
        return _usage("usage: grind-row check|append|close|settle [...]")
    verb, rest = argv[0], argv[1:]
    handler = _VERBS.get(verb)
    if handler is None:
        return _usage(f"unrecognized grind-row verb: {verb!r}")
    return handler(rest)
