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
import warnings
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from coordinator_core.frontmatter.body_blocks import LocateStatus
from coordinator_core.ops.plan_tasks_render import load_rows
from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field

#: Repo root the `os.path.isdir` rung in `_refuse_if_directory_shaped`
#: resolves a footprint entry against -- never the process cwd (issue
#: coordinator-klabauter#47 misfiled receipts this way). Same convention,
#: same directory depth, as `pathspec.py :: _REPO_ROOT`.
_REPO_ROOT = Path(__file__).resolve().parents[3]

_CHUNK_TABLE_HEADING_RE = re.compile(r"^## Chunk table\s*$", re.MULTILINE)
_NEXT_HEADING_RE = re.compile(r"^## \S", re.MULTILINE)
_BACKTICK_RE = re.compile(r"`([^`]+)`")
#: An unescaped `|` -- a pipe preceded by a backslash never splits a Chunk
#: table row (issue coordinator-klabauter#25 class 1). Applied to the row's
#: already-outer-pipe-trimmed text; each resulting cell has `\|` unescaped
#: back to a literal `|` before it reaches any downstream parsing.
_UNESCAPED_PIPE_RE = re.compile(r"(?<!\\)\|")
#: The mise-en-place skill's own Phase 1 vocabulary for a LIVE row is
#: `pending`, `queued`, `in_progress` (issue coordinator-klabauter#45 class
#: A) -- this list must track that vocabulary, not the other way round, or
#: a scout writing the skill's documented word gets every row silently
#: classified CLOSED.
_LIVE_DISPOSITION_PREFIXES = ("in_progress", "queued", "pending")
#: A closed disposition starting with either of these texts BLOCKS every
#: row that depends on it -- see `mint_rows`'s dep-resolution docstring
#: (issue coordinator-klabauter#25 class 5). Both spellings are accepted
#: because the skill's own vocabulary is hyphenated (`routed-out`) while
#: the un-hyphenated `routed out` is what a scout naturally writes (issue
#: coordinator-klabauter#45 class A) -- a hyphenation drift here silently
#: flips a blocking edge into a SATISFIED one, which is the dangerous
#: direction #25 class 5 exists to prevent. Every OTHER closed disposition
#: (`already-fixed`, `landed`, `dropped ...`) means the dependency is
#: SATISFIED: the edge is simply dropped, never treated as blocking --
#: conflating the two was the exact trap the issue names ("routing out a
#: chunk whose only deps were already-fixed").
_BLOCKING_CLOSED_DISPOSITION_PREFIXES = ("routed-out", "routed out")
#: The closed-but-satisfied vocabulary this module recognizes by name (see
#: module docstring's disposition column and `_resolve_dep_kinds`'s own
#: docstring for the free-text examples this set is drawn from). A
#: disposition matching none of LIVE, blocking, or this set is refused by
#: name at mint time (issue coordinator-klabauter#45 class A) rather than
#: silently falling through to CLOSED-SATISFIED -- the fallthrough is what
#: let a typo'd LIVE word (`in-progress`, `In Progress`) vanish a row with
#: no error.
_KNOWN_CLOSED_SATISFIED_DISPOSITION_PREFIXES = ("already-fixed", "landed", "dropped")
#: Glob metacharacters `scoped-git-commit`'s preflight refuses in a
#: pathspec (issue coordinator-klabauter#25 class 3) -- refused HERE, at
#: mint time, rather than left to fail after the emitted script's work is
#: already done.
_GLOB_CHARS = ("*", "?", "[")

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
    """Raised when a Chunk-table row's `footprint` cell carries no
    backtick-quoted path at all -- see module docstring. Trailing prose
    AFTER a backtick-quoted path (`` `a/b.py` (verify only) ``) is tolerated
    and discarded (issue coordinator-klabauter#25 class 2); this error fires
    only when a footprint entry has no backtick-quoted span to keep."""


class GlobFootprintError(InventoryMintError):
    """Raised when a footprint entry is a glob pathspec (`*`, `?`, `[`).
    `scoped-git-commit`'s preflight refuses an unbounded pathspec by design,
    so a glob entry would emit fine and then strand the wave's work
    uncommitted at commit time -- refused here instead (issue
    coordinator-klabauter#25 class 3)."""


class DirectoryShapedFootprintError(InventoryMintError):
    """Raised when a footprint entry is directory-shaped and is NOT marked
    `writes_under:` -- in EITHER spelling: a trailing `/`/`\\`, or (no
    trailing separator) an EXISTING directory on disk at mint time. The
    smaller note inside coordinator-klabauter#45 class B: the no-trailing-
    separator spelling (`cross-repo/outbox`) used to pass this rung and only
    die later, at the workflow's claimability preflight -- both spellings
    now refuse at the same stage, mint time. `scoped-git-commit` refuses a
    directory pathspec by design, same rule
    `dispatch_emit.pathspec.DirectoryShapedWriteError` already enforces at
    emit time -- refused here instead, at mint time, naming the rule rather
    than just the entry (issue coordinator-klabauter#25 class 4). A chunk
    whose deliverable filename is minted at runtime by the tool that
    produces it (a memo send, a baton mint) has a legal escape from this
    refusal: mark the entry `writes_under:` (see `_split_footprint`,
    issue coordinator-klabauter#45 class B) rather than naming the bare
    directory."""


class WritesUnderNotDirectoryError(InventoryMintError):
    """Raised when a footprint cell's `writes_under:`-marked entry does not
    end in `/` or `\\` -- `writes_under` is a directory PREFIX by contract
    (plan-tasks.schema.json's own `writes_under` item pattern), never a
    concrete file; a concrete file belongs in an ordinary backtick-quoted
    `writes:` entry instead (issue coordinator-klabauter#45 class B)."""


class UnrecognizedDispositionError(InventoryMintError):
    """Raised when one or more chunk-table rows carry a `disposition` text
    matching none of the LIVE prefixes, the blocking-closed prefixes, or
    the known closed-satisfied prefixes (issue coordinator-klabauter#45
    class A). Refusing by name, at mint time, replaces the prior silent
    fallthrough to CLOSED-SATISFIED that made a typo'd LIVE word
    (`in-progress`, `In Progress`) or a genuinely new disposition word
    vanish a row with no error and no row named -- exactly the
    "spine derives zero waves, naming no row" failure the issue reports."""


def _is_live_disposition(raw: str) -> bool:
    """LIVE iff the disposition text starts with `pending`, `queued`, or
    `in_progress` (case-insensitive) -- the mise-en-place skill's own Phase
    1 vocabulary (issue coordinator-klabauter#45 class A). `routed out ...`/
    `routed-out ...` and any other recognized closed-satisfied word is
    CLOSED. See module docstring's column-mapping table.

    An unrecognized disposition -- one matching neither this list, the
    blocking-closed prefixes, nor the known closed-satisfied prefixes --
    is no longer silently classified CLOSED: `_raw_disposition_kind`
    raises `UnrecognizedDispositionError` naming the row and its raw text
    instead, closing the vocabulary-drift hole this function's docstring
    used to flag as deliberate."""
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


def _refuse_if_glob(row_id: str, path: str, raw_cell: str) -> None:
    if any(ch in path for ch in _GLOB_CHARS):
        raise GlobFootprintError(
            f"chunk table row {row_id!r}: footprint entry {path!r} is a "
            "glob pathspec (contains '*', '?', or '['); scoped-git-commit's "
            "commit preflight refuses an unbounded pathspec, so this would "
            f"only fail after the emitted work is done (raw cell: {raw_cell!r})"
        )


def _refuse_if_directory_shaped(row_id: str, path: str, raw_cell: str) -> None:
    """Refuse `path` if it is directory-shaped, in either spelling: a
    trailing separator, OR (coordinator-klabauter#45's smaller Class B note)
    no trailing separator but an EXISTING directory on disk at mint time --
    `cross-repo/outbox` passed this rung, then died at the workflow's
    claimability preflight instead of here. `os.path.isdir` is resolved
    against `_REPO_ROOT`, never the process cwd (issue
    coordinator-klabauter#47), and a path that does not exist yet is never
    refused by this rung -- only an EXISTING directory is a directory."""
    if path.endswith("/") or path.endswith("\\") or (_REPO_ROOT / path).is_dir():
        raise DirectoryShapedFootprintError(
            f"chunk table row {row_id!r}: footprint entry {path!r} is "
            "directory-shaped (trailing separator, or an existing "
            "directory on disk); scoped-git-commit refuses a directory "
            "pathspec by design -- name a concrete file instead, or mark "
            "the entry `writes_under:` if the row's filenames are chosen "
            f"at run time (a memo send, a baton mint) (raw cell: {raw_cell!r})"
        )


#: Marks a footprint entry as a `writes_under:` directory PREFIX rather than
#: a concrete `writes:` file (issue coordinator-klabauter#45 class B) -- a
#: chunk whose deliverable filename is minted at runtime by the tool that
#: produces it (a `cross-repo-memo` send landing under `cross-repo/outbox/`,
#: a spin-off baton mint under `state/handoffs/`) has no legal `writes:`
#: value: it cannot name a file (the tool chooses the name) and `writes:`
#: refuses a directory. `writes_under:` is the schema's own escape
#: (plan-tasks.schema.json's `writes_under` property) -- this marker is how
#: a Chunk-table footprint cell reaches it.
_WRITES_UNDER_MARKER_RE = re.compile(r"writes_under\s*:\s*`([^`]+)`", re.IGNORECASE)


def _split_footprint(row_id: str, cell: str) -> Tuple[List[str], List[str]]:
    """Comma-split a `footprint`-shaped cell into `(writes, writes_under)`.

    `—`/`-`/empty means both lists are empty (a row this module never lets
    reach the minted spine live -- see `FootprintUnreadableError` below and
    the module docstring's `surface` note).

    A `writes_under:` `` `dir/` `` -marked entry (issue
    coordinator-klabauter#45 class B) is extracted first and separately --
    its path MUST be directory-shaped (`WritesUnderNotDirectoryError`
    otherwise) and is never subject to the plain-`writes:` directory-shape
    refusal, since a directory prefix is exactly what it is FOR. Every
    remaining backtick-quoted path is extracted directly out of what is
    left of the raw cell (`_BACKTICK_RE.findall`) rather than by a strict
    comma-split-then-fullmatch -- a comma-split breaks on trailing prose
    that itself contains a comma (`` `a/b.py` (verify only, no edit) ``),
    and this module's job is to keep the path and discard the prose, not
    parse it (issue coordinator-klabauter#25 class 2). A cell with no
    backtick-quoted path and no `writes_under:` marker at all still refuses
    (`FootprintUnreadableError`), naming that the cell must hold
    backtick-quoted paths.
    """
    cell = cell.strip()
    if cell in ("", "—", "-"):
        return [], []

    writes_under: List[str] = []
    for prefix in _WRITES_UNDER_MARKER_RE.findall(cell):
        if not (prefix.endswith("/") or prefix.endswith("\\")):
            raise WritesUnderNotDirectoryError(
                f"chunk table row {row_id!r}: writes_under entry {prefix!r} "
                "is not directory-shaped (no trailing separator) -- "
                "writes_under names a directory PREFIX, never a concrete "
                f"file (raw cell: {cell!r})"
            )
        writes_under.append(prefix)
    remaining = _WRITES_UNDER_MARKER_RE.sub("", cell).strip()

    matches = _BACKTICK_RE.findall(remaining)
    if not matches:
        if writes_under:
            return [], writes_under
        raise FootprintUnreadableError(
            f"chunk table row {row_id!r}: footprint cell holds no "
            f"backtick-quoted path (raw cell: {cell!r}) -- every footprint "
            "entry must be a backtick-quoted path, or marked "
            "`writes_under:` `` `dir/` `` for a runtime-minted deliverable; "
            "trailing prose after the closing backtick is fine and is "
            "discarded"
        )
    paths: List[str] = []
    for path in matches:
        _refuse_if_glob(row_id, path, cell)
        _refuse_if_directory_shaped(row_id, path, cell)
        paths.append(path)
    return paths, writes_under


def _parse_pipe_row(line: str) -> List[str]:
    """Split one Chunk-table pipe-row into cells, unescape-aware: a
    Markdown-escaped `\\|` inside a cell is a literal pipe, never a column
    separator (issue coordinator-klabauter#25 class 1)."""
    line = line.strip()
    while line.startswith("|"):
        line = line[1:]
    while line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [
        cell.strip().replace("\\|", "|")
        for cell in _UNESCAPED_PIPE_RE.split(line)
    ]


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


#: `_resolve_dep_kind`'s three outcomes. `"live"` reaches the minted spine;
#: `"closed-satisfied"` means the edge naming it is dropped (the dependency
#: is already done); `"routed-out"` means every row depending on it,
#: transitively, is excluded from the minted spine too; `"unknown"` means
#: the id names no row in this table at all -- left as a literal edge, same
#: as before this fix, so a truly missing id still surfaces as
#: `read_spine`'s own "depends_on unresolvable chunk" refusal downstream,
#: which is a different failure than anything this issue's class 5 covers.
_DEP_KIND_UNKNOWN = "unknown"
_DEP_KIND_LIVE = "live"
_DEP_KIND_CLOSED_SATISFIED = "closed-satisfied"
_DEP_KIND_ROUTED_OUT = "routed-out"


def _raw_disposition_kind(row_id: str, raw: str) -> str:
    """`raw` (a Chunk-table row's `disposition` cell) -> one of
    `_DEP_KIND_LIVE` / `_DEP_KIND_ROUTED_OUT` / `_DEP_KIND_CLOSED_SATISFIED`.

    Raises `UnrecognizedDispositionError`, naming `row_id` and `raw`
    verbatim, when the text matches none of the LIVE prefixes, the
    blocking-closed prefixes, or the known closed-satisfied prefixes (issue
    coordinator-klabauter#45 class A) -- replacing the prior silent
    fallthrough to CLOSED-SATISFIED for anything unrecognized."""
    text = raw.strip().lower()
    if _is_live_disposition(raw):
        return _DEP_KIND_LIVE
    if text.startswith(_BLOCKING_CLOSED_DISPOSITION_PREFIXES):
        return _DEP_KIND_ROUTED_OUT
    if text.startswith(_KNOWN_CLOSED_SATISFIED_DISPOSITION_PREFIXES):
        return _DEP_KIND_CLOSED_SATISFIED
    raise UnrecognizedDispositionError(
        f"chunk table row {row_id!r}: disposition {raw!r} matches none of "
        "the LIVE vocabulary (pending/queued/in_progress), the blocking-"
        "closed vocabulary (routed-out/routed out), or the known "
        "closed-satisfied vocabulary (already-fixed/landed/dropped) -- "
        "refusing rather than silently classifying it CLOSED"
    )


def _resolve_dep_kinds(chunk_rows: List[Dict[str, str]]) -> Dict[str, str]:
    """Every row id in `chunk_rows` -> its FINAL kind, after transitively
    routing out any row whose dep chain reaches a `routed out ...` row
    (issue coordinator-klabauter#25 class 5).

    A row's own disposition decides its kind UNLESS it is live and depends,
    directly or transitively, on a `routed-out` row -- that dependency
    BLOCKS it (the premise it names moved out from under it), so it is
    routed out too, and a `UserWarning` reports which dependency did it. A
    dep on any OTHER closed row (`already-fixed`, `landed`, `pending ...`,
    `dropped ...`) is SATISFIED, never blocking -- see
    `_BLOCKING_CLOSED_DISPOSITION_PREFIX`'s own docstring for why
    conflating the two is the exact trap the issue names.
    """
    kinds: Dict[str, str] = {}
    for row in chunk_rows:
        row_id = _strip_backtick(row["id"])
        kinds[row_id] = _raw_disposition_kind(row_id, row["disposition"])

    rows_by_id = {_strip_backtick(row["id"]): row for row in chunk_rows}
    resolved: Dict[str, str] = {}

    def resolve(row_id: str, stack: set) -> str:
        if row_id in resolved:
            return resolved[row_id]
        kind = kinds.get(row_id, _DEP_KIND_UNKNOWN)
        if kind != _DEP_KIND_LIVE or row_id in stack:
            resolved[row_id] = kind
            return kind
        stack = stack | {row_id}
        for dep_id in _split_id_list(rows_by_id[row_id]["deps"]):
            if resolve(dep_id, stack) == _DEP_KIND_ROUTED_OUT:
                warnings.warn(
                    f"chunk table row {row_id!r} routed out: its dependency "
                    f"{dep_id!r} was routed out, and a row cannot outlive "
                    "the premise it depends on",
                    stacklevel=2,
                )
                resolved[row_id] = _DEP_KIND_ROUTED_OUT
                return _DEP_KIND_ROUTED_OUT
        resolved[row_id] = _DEP_KIND_LIVE
        return _DEP_KIND_LIVE

    for row_id in kinds:
        resolve(row_id, set())
    return resolved


def _bare_plan_row_id(chunk_id: str) -> str:
    """Chunk-table `id` (`P144-C6`, `P156-C1b`) -> the plan spine row id
    (`C6`, `C1b`) it names: the suffix after the first `-`, when the id
    carries a plan-prefix at all. An id with no `-` is already a bare plan
    row id (the prefix is optional -- module contract)."""
    if "-" in chunk_id:
        return chunk_id.split("-", 1)[1]
    return chunk_id


def _resolve_spec_plan_path(inventory_path: Path, spec_path: str) -> Path:
    """A Chunk-table `spec path` cell -> an absolute filesystem path.

    A relative `spec_path` resolves against the REPO ROOT the inventory
    record lives in -- `<repo>/state/mise-inventory/<file>.md`, so the repo
    root is the inventory path's own grandparent-of-grandparent
    (`.../<file>.md` -> `mise-inventory/` -> `state/` -> `<repo>/`), never
    the process cwd nor this module's own `_REPO_ROOT` (a different repo
    when the inventory being minted belongs to a sibling checkout)."""
    path = Path(spec_path)
    if path.is_absolute():
        return path
    repo_root = inventory_path.resolve().parents[2]
    return repo_root / path


def _plan_row_execution_mode(
    inventory_path: Optional[Path],
    spec_path: str,
    chunk_id: str,
    plan_cache: Dict[Path, Dict[str, dict]],
) -> Optional[str]:
    """The plan spine row `chunk_id` (or its id-prefix-stripped form, see
    `_bare_plan_row_id`) names in `spec_path`'s `` ```yaml plan-tasks ``
    block -> that row's raw `execution_mode` value, or `None`.

    Reuses `plan_tasks_render.load_rows` -- the exact tolerant fenced-block
    reader `spine_read.read_spine` itself wraps -- rather than a second YAML
    parser. `None` covers every non-plan-sourced shape alike, by design: no
    `inventory_path` (a standalone `mint_rows` call, e.g. this module's own
    unit tests), a `spec_path` that is not a readable file, a file with no
    LOCATED plan-tasks block, or a block with no row matching either id
    form -- an inventory row is not always plan-sourced, and none of these
    is a refusal.

    `plan_cache` is keyed by resolved plan path so a `spec_path` shared by
    many Chunk-table rows is read and parsed from disk once per mint, not
    once per row."""
    if inventory_path is None:
        return None
    plan_path = _resolve_spec_plan_path(inventory_path, spec_path)
    if plan_path not in plan_cache:
        try:
            text = plan_path.read_text(encoding="utf-8")
        except OSError:
            plan_cache[plan_path] = {}
        else:
            result = load_rows(text)
            if result.status is not LocateStatus.LOCATED:
                plan_cache[plan_path] = {}
            else:
                plan_cache[plan_path] = {
                    raw["id"]: raw
                    for raw in result.rows
                    if isinstance(raw.get("id"), str) and raw["id"]
                }
    rows_by_id = plan_cache[plan_path]
    row = rows_by_id.get(chunk_id)
    if row is None:
        row = rows_by_id.get(_bare_plan_row_id(chunk_id))
    return row.get("execution_mode") if row is not None else None


def mint_rows(
    chunk_rows: List[Dict[str, str]], inventory_path: Optional[Path] = None
) -> List[dict]:
    """`## Chunk table` rows (as `parse_chunk_table` returns) -> a list of
    schema-valid plan-tasks row dicts, LIVE rows only. See module docstring's
    column-mapping table for the full field-by-field rule.

    Dep resolution (issue coordinator-klabauter#25 class 5): see
    `_resolve_dep_kinds`. A dep naming a `closed-satisfied` row has its edge
    dropped (the dependency is already discharged); a dep naming a
    `routed-out` row is never reachable from a LIVE row here, because that
    row was itself routed out by `_resolve_dep_kinds` first.

    `inventory_path`, when given, is used to resolve each row's `spec path`
    against its plan and carry a `execution_mode: operator` row from that
    plan's own spine onto the minted row (see `_plan_row_execution_mode`) --
    `read_spine`'s own exclusion, one layer up, then drops that row from
    dispatch and reports it via its `exclusions` out-parameter (module
    docstring's negative-spec: this module infers no dep the inventory
    table doesn't name, but a value the SOURCE PLAN already declares for the
    same row is not an inference). Omitted (the default), this carries
    nothing -- every existing standalone `mint_rows(rows)` call keeps its
    prior behaviour unchanged.
    """
    dep_kinds = _resolve_dep_kinds(chunk_rows)
    live: List[Tuple[str, Dict[str, str], List[str]]] = []
    writes_by_id: Dict[str, set] = {}

    for row in chunk_rows:
        row_id = _strip_backtick(row["id"])
        if dep_kinds[row_id] != _DEP_KIND_LIVE:
            continue
        writes, _writes_under = _split_footprint(row_id, row["footprint"])
        if not writes:
            raise FootprintUnreadableError(
                f"chunk table row {row_id!r} is LIVE ({row['disposition']!r}) "
                "but its footprint is empty -- an EM-run/no-write row does "
                "not belong in an emitted script (see module docstring's "
                "commit-C12 precedent)"
            )
        writes_by_id[row_id] = set(writes)
        live.append((row_id, row, writes))

    plan_cache: Dict[Path, Dict[str, dict]] = {}
    minted: List[dict] = []
    for row_id, row, writes in live:
        spec_path = _strip_backtick(row["spec path"])
        summary = row["summary"].strip()
        verification = row["verification"].strip()
        complexity = row["complexity"].strip()

        depends_on = []
        for dep_id in _split_id_list(row["deps"]):
            dep_kind = dep_kinds.get(dep_id, _DEP_KIND_UNKNOWN)
            if dep_kind == _DEP_KIND_CLOSED_SATISFIED:
                continue  # satisfied -- the dependency is already discharged
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
        execution_mode = _plan_row_execution_mode(
            inventory_path, spec_path, row_id, plan_cache
        )
        if execution_mode == "operator":
            entry["execution_mode"] = execution_mode
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
    rows = mint_rows(chunk_rows, inventory_path=path)

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
