"""
coordinator_core.ops.dispatch_emit.inventory_mint — mise-inventory record
-> plan-tasks spine.

Purpose: the `--inventory` mint leg coordinator-claude#47 names. A PURE
module deriving a schema-valid `` ```yaml plan-tasks `` spine (the same
fenced-block shape `spine_read.read_spine` consumes, DEFAULT_HEADING
"Tasks" / DEFAULT_INFO_STRING "yaml plan-tasks" —
`coordinator_core.frontmatter.body_blocks.locate_fenced_block`) from a
mise-inventory record's `## Chunk table`, and returning the spine's full
markdown TEXT plus the `<run-id>.spine.md` path it belongs beside — the
exact shape `state/mise-inventory/*.spine.md` prior art already
hand-authors (e.g. `state/mise-inventory/20260911T111541-8087eee2.spine.md`).

This module touches no disk of its own — `dispatch_emit.op` is the ONE
place in the dispatch-emit pipeline that writes (its own module docstring,
§ "this module composes their output ... and is the ONE place in this
pipeline that touches disk for a write"). "The mint leg writes spine
`body` text only" (S1-C4 row body): this module's own output is a plan-
tasks row's `body` field prose and the row dict around it: it does not
compose agent prompts -- `emit.py` still leads every emitted prompt with
`_BRIEF_PRECEDENCE_CLAUSE` because `emit_script` composes the prompt from
whatever spine (hand-authored or minted) `op.py` hands it, unchanged by
this module's existence.

Column mapping, applied per live Chunk-table row:
    id            -> row `id` (backtick-stripped).
    footprint     -> row `writes` (comma-split, each cell backtick-quoted;
                     a malformed footprint cell -- one whose entries are
                     not cleanly backtick-quoted -- raises
                     `FootprintUnreadableError` naming the row and the raw
                     cell rather than silently emitting a broken `writes`
                     list).
    deps          -> row `depends_on`, one `{chunk, gate_kind:
                     "output-consumption-runtime"}` edge per comma-split
                     id, MINUS any edge whose target row's own `writes`
                     set intersects this row's `writes` set -- a
                     write-overlap edge is already implied structurally
                     by `pathspec`'s collision-based sequencing (the same
                     shape `wave_map.py`'s predecessor derivation already
                     keys on), so carrying it a second time as an explicit
                     `depends_on` edge is redundant, not informative.
    summary       -> row `title` verbatim, and the body's second line.
    spec path     -> the body's `Spec: <path> (<id>)` first line.
    verification  -> the body's `Verification (this row is DONE only when
                     this holds): <text>` line.
    complexity    -> the body's `Complexity: <text>` line.
    disposition   -> LIVE/CLOSED classification (see `_is_live_disposition`)
                     -- only LIVE rows reach the minted spine at all; a
                     CLOSED row (`pending ...`, `routed out ...`, `dropped
                     ...`) is not yet, or no longer, dispatchable and is
                     silently excluded, matching `spine_read.read_spine`'s
                     own exclusion of non-dispatchable rows one layer up.
    change_kind   -> inferred from `writes`: every entry ending `.md`
                     yields `doc-edit` (matches the hand-authored prior
                     art -- e.g. `docs/research/*.md`-only rows), anything
                     else (including an empty `writes`, which never
                     reaches this point live -- see below) yields
                     `code-edit`, the wider default the plan-tasks
                     `change_kind` enum documents as the catch-all surface
                     token.
    (none)        -> `surface`, `writes[0]` when `writes` is non-empty
                     (schema requires `surface`; a LIVE row with an empty
                     `writes` -- footprint cell `—` -- is refused via
                     `FootprintUnreadableError` rather than left surfaceless,
                     since an EM-run/no-write row belongs in the inventory
                     record's own prose, not the emitted script -- see
                     `state/mise-inventory/20260918T140906-29c9094a.md`'s
                     `commit-C12` row, whose disposition text says exactly
                     this: "not in the emitted script, EM-executed at tail").

`deliverable_id` (row: "inherited from the Source baton, never minted"):
    the spine's OWN frontmatter, never a per-row field (plan-tasks.schema.json
    carries no row-level `deliverable_id` -- confirmed against its
    `properties` set). Read via the frontmatter's `source_baton` field (a
    path, resolved against the inventory record's own directory) naming
    the baton/handoff record this inventory run was claimed against; that
    baton's own `deliverable_id` frontmatter field is copied VERBATIM.
    `coordinator_core.ops.read_frontmatter_field.read_frontmatter_field`
    is reused for both reads -- no fresh frontmatter parser here. Absent
    `source_baton`, or an unreadable/deliverable_id-less baton, the spine
    simply carries NO `deliverable_id` key -- this module never calls
    `mint_deliverable_id.mint` or otherwise fabricates one; a phantom join
    key is worse than an absent one (same rationale `op.py ::
    _receipt_session_id` already documents for session identity).

Negative-spec:
  - Does NOT enumerate the tree, glob, or shell out -- the ONE file this
    module reads is the caller-supplied `inventory_path`, plus (at most)
    one more: the `source_baton` path its own frontmatter names.
  - Does NOT write `<run-id>.spine.md` to disk -- returns `(text, path)`;
    `op.py` performs the guarded write.
  - Does NOT re-validate the minted spine against `schema_validate.py` --
    the row shape this module builds is schema-valid by construction
    (every required field populated, `change_kind`/`gate_kind` drawn from
    closed enums), and `dispatch.emit`'s existing `run_checks` pass over
    the composed script is the fleet's one verification surface for a
    spine, minted or hand-authored alike.
  - Does NOT infer `depends_on` edges the Chunk table's `deps` column
    doesn't name -- no transitive closure, no same-spec-path grouping.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § S1-C4
(coordinator-claude#47).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field

_CHUNK_TABLE_HEADING_RE = re.compile(r"^## Chunk table\s*$", re.MULTILINE)
_NEXT_HEADING_RE = re.compile(r"^## \S", re.MULTILINE)
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_LIVE_DISPOSITION_PREFIXES = ("in_progress", "queued")

_REQUIRED_COLUMNS = (
    "id",
    "spec path",
    "summary",
    "footprint",
    "deps",
    "verification",
    "complexity",
    "disposition",
)


class InventoryMintError(ValueError):
    """Base class for every refusal this module raises."""


class ChunkTableAbsentError(InventoryMintError):
    """Raised when the inventory record carries no `## Chunk table` section,
    or the table has no parseable header/rows."""


class ChunkTableMalformedError(InventoryMintError):
    """Raised when a Chunk-table row's cell count disagrees with its header,
    or a required column is absent from the header."""


class FootprintUnreadableError(InventoryMintError):
    """Raised when a Chunk-table row's `footprint` cell cannot be parsed into
    a clean, backtick-quoted path list -- see module docstring."""


def _is_live_disposition(raw: str) -> bool:
    """LIVE iff the disposition text starts with `in_progress` or `queued`
    (case-insensitive) -- `pending ...`, `routed out ...`, and any other free
    text is CLOSED. See module docstring's column-mapping table.

    Review: code-reviewer -- this is a silent-drop classifier, not a
    closed-set validator: a typo'd spelling (`in-progress`, `In Progress`)
    or a future disposition word this module doesn't know about reads as
    CLOSED with no error and no warning, dropping the row from the minted
    spine. Deliberate given `disposition` is a controlled vocabulary from
    an upstream tool, but a vocabulary drift fails silently rather than
    loudly, unlike `FootprintUnreadableError`'s refusal on a malformed
    footprint cell."""
    text = raw.strip().lower()
    return any(text.startswith(prefix) for prefix in _LIVE_DISPOSITION_PREFIXES)


def _strip_backtick(cell: str) -> str:
    cell = cell.strip()
    match = _BACKTICK_RE.fullmatch(cell)
    return match.group(1) if match else cell


def _split_id_list(cell: str) -> List[str]:
    """Comma-split a `deps`-shaped cell; `—`/`-`/empty means no entries."""
    cell = cell.strip()
    if cell in ("", "—", "-"):
        return []
    return [piece.strip() for piece in cell.split(",") if piece.strip()]


def _split_footprint(row_id: str, cell: str) -> List[str]:
    """Comma-split a `footprint`-shaped cell into backtick-quoted paths.

    `—`/`-`/empty means an empty `writes` list (a row this module never
    lets reach the minted spine live -- see `FootprintUnreadableError`
    below and the module docstring's `surface` note).
    """
    cell = cell.strip()
    if cell in ("", "—", "-"):
        return []
    paths: List[str] = []
    for piece in cell.split(","):
        piece = piece.strip()
        match = _BACKTICK_RE.fullmatch(piece)
        if not match:
            raise FootprintUnreadableError(
                f"chunk table row {row_id!r}: footprint entry not "
                f"backtick-quoted: {piece!r} (raw cell: {cell!r})"
            )
        paths.append(match.group(1))
    return paths


def _parse_pipe_row(line: str) -> List[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_chunk_table(text: str) -> List[Dict[str, str]]:
    """Parse the `## Chunk table` markdown pipe-table into one dict per data
    row, keyed by the table's own header cells (verbatim, lower-cased by the
    source table already -- see module docstring's `_REQUIRED_COLUMNS`).

    Raises `ChunkTableAbsentError`/`ChunkTableMalformedError` -- see their
    docstrings.
    """
    heading_match = _CHUNK_TABLE_HEADING_RE.search(text)
    if heading_match is None:
        raise ChunkTableAbsentError(
            "inventory record carries no '## Chunk table' section"
        )
    rest = text[heading_match.end():]
    next_heading = _NEXT_HEADING_RE.search(rest)
    block = rest[: next_heading.start()] if next_heading else rest

    lines = [ln for ln in block.splitlines() if ln.strip().startswith("|")]
    if len(lines) < 2:
        raise ChunkTableAbsentError(
            "'## Chunk table' section carries no pipe-table header/rows"
        )

    header = [cell.lower() for cell in _parse_pipe_row(lines[0])]
    missing = [col for col in _REQUIRED_COLUMNS if col not in header]
    if missing:
        raise ChunkTableMalformedError(
            f"Chunk table header missing required column(s): {missing!r} "
            f"(header: {header!r})"
        )

    rows: List[Dict[str, str]] = []
    for line in lines[2:]:  # lines[1] is the '|---|---|...' separator
        cells = _parse_pipe_row(line)
        if len(cells) != len(header):
            raise ChunkTableMalformedError(
                f"Chunk table row column count ({len(cells)}) disagrees "
                f"with header ({len(header)}): {line!r}"
            )
        rows.append(dict(zip(header, cells)))
    return rows


def _infer_change_kind(writes: List[str]) -> str:
    if writes and all(path.endswith(".md") for path in writes):
        return "doc-edit"
    return "code-edit"


def _row_body(row_id: str, spec_path: str, summary: str, verification: str, complexity: str) -> str:
    return (
        f"Spec: {spec_path} ({row_id})\n"
        f"{summary}\n"
        f"Verification (this row is DONE only when this holds): {verification}\n"
        f"Complexity: {complexity}\n"
    )


def mint_rows(chunk_rows: List[Dict[str, str]]) -> List[dict]:
    """`## Chunk table` rows (as `parse_chunk_table` returns) -> a list of
    schema-valid plan-tasks row dicts, LIVE rows only. See module docstring's
    column-mapping table for the full field-by-field rule."""
    live: List[Tuple[str, Dict[str, str], List[str]]] = []
    writes_by_id: Dict[str, set] = {}

    for row in chunk_rows:
        row_id = _strip_backtick(row["id"])
        if not _is_live_disposition(row["disposition"]):
            continue
        writes = _split_footprint(row_id, row["footprint"])
        if not writes:
            raise FootprintUnreadableError(
                f"chunk table row {row_id!r} is LIVE ({row['disposition']!r}) "
                "but its footprint is empty -- an EM-run/no-write row does "
                "not belong in an emitted script (see module docstring's "
                "commit-C12 precedent)"
            )
        writes_by_id[row_id] = set(writes)
        live.append((row_id, row, writes))

    minted: List[dict] = []
    for row_id, row, writes in live:
        spec_path = _strip_backtick(row["spec path"])
        summary = row["summary"].strip()
        verification = row["verification"].strip()
        complexity = row["complexity"].strip()

        depends_on = []
        for dep_id in _split_id_list(row["deps"]):
            dep_writes = writes_by_id.get(dep_id)
            if dep_writes and writes_by_id[row_id] & dep_writes:
                continue  # write-overlap edge dropped -- see module docstring
            depends_on.append({"chunk": dep_id, "gate_kind": "output-consumption-runtime"})

        entry: dict = {
            "id": row_id,
            "title": summary,
            "change_kind": _infer_change_kind(writes),
            "surface": writes[0],
            "body": _row_body(row_id, spec_path, summary, verification, complexity),
            "writes": writes,
        }
        if depends_on:
            entry["depends_on"] = depends_on
        minted.append(entry)

    return minted


class _LiteralBlockDumper(yaml.SafeDumper):
    """`yaml.SafeDumper` that renders any multi-line string in literal
    block style (`|`) -- matches the hand-authored `body:` fields in prior
    art (`state/mise-inventory/*.spine.md`), and is what
    `body_blocks.locate_fenced_block` / `yaml.safe_load` round-trip
    identically to a single-line-style string, so this is presentation
    only, never a semantic choice."""


def _represent_str(dumper: yaml.Dumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_LiteralBlockDumper.add_representer(str, _represent_str)


def _dump_rows(rows: List[dict]) -> str:
    return yaml.dump(
        rows,
        Dumper=_LiteralBlockDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )


def _inherited_deliverable_id(inventory_path: Path, record_text: str) -> Optional[str]:
    """`deliverable_id` inherited from the Source baton -- never minted. See
    module docstring's `deliverable_id` section."""
    baton_ref = read_frontmatter_field(str(inventory_path), "source_baton")
    if not baton_ref:
        return None
    baton_path = Path(baton_ref)
    if not baton_path.is_absolute():
        baton_path = inventory_path.parent / baton_path
    deliverable_id = read_frontmatter_field(str(baton_path), "deliverable_id")
    return deliverable_id or None


def spine_path_for(inventory_path: Path, run_id: str) -> Path:
    """`<run-id>.spine.md`, beside the inventory record -- the naming prior
    art (`state/mise-inventory/*.spine.md`) already establishes."""
    return inventory_path.parent / f"{run_id}.spine.md"


def mint_spine(inventory_path: str) -> Tuple[str, Path]:
    """The `--inventory` mint leg's one entry point.

    Reads `inventory_path` (a mise-inventory record), derives a
    schema-valid plan-tasks spine from its `## Chunk table`, and returns
    `(spine_markdown_text, spine_output_path)` -- text only, no write (see
    module docstring's negative-spec).

    Raises `ChunkTableAbsentError` / `ChunkTableMalformedError` /
    `FootprintUnreadableError` -- see their docstrings. An unreadable
    `inventory_path` itself raises `OSError`, uncaught -- the same
    fail-loud contract every other read in this pipeline (`spine_read.py`,
    `emit.py`) already keeps.
    """
    path = Path(inventory_path)
    text = path.read_text(encoding="utf-8")

    run_id = read_frontmatter_field(str(path), "run_id") or path.stem
    deliverable_id = _inherited_deliverable_id(path, text)

    chunk_rows = parse_chunk_table(text)
    rows = mint_rows(chunk_rows)

    frontmatter_lines = [f"run_id: {run_id}", "derived_from: mise inventory record"]
    if deliverable_id:
        frontmatter_lines.append(f"deliverable_id: {deliverable_id}")

    spine_text = (
        "---\n"
        + "\n".join(frontmatter_lines)
        + "\n---\n\n"
        + f"# Minted dispatch spine — {run_id}\n\n"
        + f"Derived from `{path}` by `dispatch.emit --inventory`.\n"
        + "**Do not hand-edit** — the next mint overwrites this file in place.\n"
        + "Coordination facts belong in the inventory record.\n\n"
        + "## Tasks\n\n"
        + "```yaml plan-tasks\n"
        + _dump_rows(rows)
        + "```\n"
    )

    return spine_text, spine_path_for(path, run_id)
