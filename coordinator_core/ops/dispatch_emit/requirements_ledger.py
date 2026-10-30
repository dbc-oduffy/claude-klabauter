"""workflow.revise: a warp `--ask` run's PM requirements, held as rows of its sizing's
`requirement_register` -- the one list of what the PM asked, which `sizing-assemble --register`,
plan `register_claims` and the terminal judge already read.

A run is ledger-bearing once the emit writes `RECORD_DIR_ROOT/<run_id>/run.json` (`start`):
`{sizing_rel, all_of_pm_ask}`, the run's binding and flag, never a requirement. `bind` seeds the
bound sizing's register with the PM's words (`pm_verbatims`, else a PM-verbatim intent; none for a
relayed run, whose session turns the sizing lists as `unrelated_turns`) and the raw ask (`ask`, or `relayed` when an EM relayed it: never the PM's words), one row each, and drains `revisions.jsonl` -- the transport for a revision sent before a
raw ask's sizing exists -- into rows. Once bound, `workflow.revise` appends its row straight onto
the register. A row's `anchor` carries its origin (`pm_verbatims`, `ask`, `revision@<UTC>`); row
order is arrival order. `requirements.md` beside run.json is the view every stage prompt reads,
rewritten on each write. Register writes go through `locked_rmw` and the sizing schema. File I/O
only: no git, no subprocess, so the op holds the 500ms bar.

`judge` is the one pass rule. The run's final checker applies it in its prompt and
`dispatch.land_run` re-applies it against the live register, so a revision that lands after
the check, or a verdict that skips a row, cannot reach the product commit. It is a gate beside the
delivery verifier, the terminal judge and the falsifier, never a stand-in for any of them.
`apply_verdict` writes the passed verdict back onto the rows once the commit has landed.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Collection, Iterable, Mapping, Optional, Sequence

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.locked_write import MutateAbort
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_SOURCE_RELAYED,
    CLAIM_PARAM,
    EMPTY_REGISTER_REASON,
    LATE_ROWS_FIELD,
    OP_LATE_ROWS,
    PLAN_ROW_ANCHOR_PREFIX,
    RECORD_DIR_ROOT,
    STOP_AFTER_FILE,
    STOP_AFTER_PLAN,
    empty_register_refusal,
    record_dir,
    stamp_run_stage,
)
from coordinator_core.ops.dispatch_emit import bug_close, review_floor
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import slice_ask_source
from coordinator_core.ops.dispatch_emit.request_validation import Field, validate_params
from coordinator_core.ops.sizing_intake_contract import EM_CONTEXT_MARKER, read_em_brief, read_em_context_paths
from coordinator_core.session.declared_writes import declare_write

GENERATES = []

RUN_FILE = "run.json"
INBOX_FILE = "revisions.jsonl"
NOTES_FILE = "notes.jsonl"
#: Per-run `{nodeid, owner_run_id, reason, at}` lines: failing tests another run owns; recorded only, no landing gate reads it yet.
OWNED_RED_FILE = "owned_red.jsonl"
#: run.json key naming the halted run a fresh fire resumed.
RESUMED_FROM_FIELD = "resumed_from"
#: Most `resumed_from` hops `carried_notes` follows.
CARRIED_HOP_CAP = 16
LEDGER_VIEW_FILE = "requirements.md"
ORIGIN_PM_VERBATIMS, ORIGIN_ASK, ORIGIN_REVISION = "pm_verbatims", "ask", "revision"
ORIGIN_RELAYED = "relayed"
#: Prefix of the `ruling.ref` a `workflow.revise` out_of_scope stamps: `<REVISE_REF>:<run_id>:<OUT_OF_SCOPE_REF>`.
#: Rulings written before the ruling stopped minting a row end in a revision row id instead.
REVISE_REF = "workflow.revise"
OUT_OF_SCOPE_REF = "out_of_scope"
STATUSES = ("met", "not_met", "drifted", "superseded", "post_commit")
#: Acts the terminal commit's landing performs itself: the run's consumed baton archived and the
#: plan stamped implemented, and each bug row the spine says to close closed with the landing sha.
LIFECYCLE_ACTS: tuple[str, ...] = ("baton_archive", "plan_implemented", bug_close.ACT)
#: Acts that can only exist after the terminal commit; a `post_commit` item names which remain.
POST_COMMIT_ACTS = ("commit", "sha_reply", "sha_memo", *LIFECYCLE_ACTS)
#: The post-commit acts the landing performs with no further sender.
CLOSE_DISCHARGED_ACTS: tuple[str, ...] = ("commit", *LIFECYCLE_ACTS)
VERBATIM_BYTE_CAP = 16 * 1024
#: Every seeded row's surface: a PM sentence names no entry kind, so the checker judges `wired`
#: as reachable from the product at all.
ROW_SURFACE = "internal"
#: Item origin of a register row minted from a plan row (anchor `plan-row:<id>`).
ORIGIN_PLAN_ROW = "plan-row"
NOT_A_ROW_REASON = "names no requirement row"

_PARAMS = (
    Field("run_id", "nonempty_str", required=True),
    Field("verbatim", "str"),
    Field("note", "str"),
    Field("all_of_pm_ask", "bool"),
    Field("out_of_scope", "str_list"),
    Field("withhold_paths", "list"),
    Field("accept_partial", "list"),
    Field("add_paths", "list"),
    Field("widened", "list"),
    Field("credit_chunks", "list"),
    Field("owned_red", "list"),
    Field("retract_note", "dict"),
    Field("retract_row", "dict"),
    Field("build_only", "bool"),
    Field("stop_after", "str"),
    Field("from", "str"),
    Field(CLAIM_PARAM, "bool"),
    Field("halted", "dict"),
    Field("inline_review", "dict"),
    Field("ledger_verdict", "dict"),
    Field("objective_loop", "dict"),
)
#: Separator between a revision anchor's UTC stamp and the peer session that sent it.
SENDER_SEP = " from "
_SIZING_SCHEMA = Path(__file__).resolve().parents[2] / "frontmatter" / "schemas" / "sizing-object.schema.json"


class LedgerRefused(ValueError):
    """The run or the revision cannot be admitted; the message names the cause."""


@dataclass(frozen=True)
class Item:
    id: str
    text: str
    origin: str
    at: str
    anchor: str = ""
    sender: str = ""

    def to_json(self) -> dict:
        out = {"id": self.id, "text": self.text, "origin": self.origin, "at": self.at}
        if self.sender:
            out["from"] = self.sender
        return out


@dataclass(frozen=True)
class Ledger:
    """The rows a run is judged against: every register row not already ruled deferred/waived.
    `withheld` (`{id, reason}`) are the rows a `workflow.revise` ruled out of scope: not judged,
    always carried open."""

    items: tuple[Item, ...]
    all_of_pm_ask: bool
    sizing_rel: Optional[str] = None
    withheld: tuple[dict, ...] = ()

    def to_json(self) -> dict:
        return {"items": [i.to_json() for i in self.items], "all_of_pm_ask": self.all_of_pm_ask}


def run_dir(repo_root: Path, run_id: str) -> Path:
    """The run's record directory (`ask_contract.record_dir`); LedgerRefused on an unsafe id or an escape."""
    try:
        return record_dir(Path(repo_root), run_id)
    except ValueError as exc:
        raise LedgerRefused(str(exc)) from exc


def _parent_lists_run(root: Path, run_id: str) -> bool:
    """True when some write-once parent record under scratch/warp lists `run_id` among its minted runs."""
    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import ParentRecord, parent_record_rel

    for path in root.glob(parent_record_rel("*")):
        try:
            record = ParentRecord.from_json(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if any(r.run_id == run_id for r in record.runs):
            return True
    return False


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_design(anchor: object) -> bool:
    """Is this a design-conformance row (anchor carries `design_conformance.ANCHOR_PREFIX`)?"""
    from coordinator_core.ops.dispatch_emit.design_conformance import ANCHOR_PREFIX

    return isinstance(anchor, str) and anchor.startswith(ANCHOR_PREFIX)


def _design_failure(
    item: Item, row: Mapping, repo_root: Optional[Path], plan_text: Optional[str],
    wave_reviews: Sequence[Mapping] = (), noted_paths: Sequence[str] = (),
) -> Optional[str]:
    """Why a `met` design row fails: evidence_refusal's sentence, else None."""
    from coordinator_core.ops.dispatch_emit.design_conformance import evidence_refusal

    if repo_root is None:
        return f"design row {item.id}: no repo root to check its evidence against"
    return evidence_refusal(
        Path(repo_root),
        {rr.ROW_ID: item.id, rr.ROW_ANCHOR: item.anchor, rr.ROW_TEXT: item.text},
        row, plan_text, wave_reviews=wave_reviews, noted_paths=noted_paths,
    )


def _origin(anchor: object) -> tuple[str, str]:
    """(origin, at) from a row anchor; a row from a source document has origin ''."""
    text = anchor if isinstance(anchor, str) else ""
    if text.startswith(ORIGIN_REVISION + "@"):
        return ORIGIN_REVISION, text[len(ORIGIN_REVISION) + 1:].partition(SENDER_SEP)[0]
    if text in (ORIGIN_PM_VERBATIMS, ORIGIN_ASK, ORIGIN_RELAYED):
        return text, ""
    if text.startswith(PLAN_ROW_ANCHOR_PREFIX):
        return ORIGIN_PLAN_ROW, text[len(PLAN_ROW_ANCHOR_PREFIX):]
    return "", ""


def _sender(anchor: object) -> str:
    """The peer session a revision anchor names, else '' (an EM revision carries none)."""
    text = anchor if isinstance(anchor, str) else ""
    return text.partition(SENDER_SEP)[2].strip() if text.startswith(ORIGIN_REVISION + "@") else ""


def _clean_sender(sender: object) -> str:
    return " ".join(sender.split()) if isinstance(sender, str) else ""


def _revision_anchor(at: str, sender: object) -> str:
    who = _clean_sender(sender)
    return f"{ORIGIN_REVISION}@{at}" + (SENDER_SEP + who if who else "")


RULING_ID_RE = re.compile(r"\bJ\d+\b")


def pending_ruling_ids(rows: Sequence[Mapping]) -> tuple[str, ...]:
    """J<n> ids named by still-open (open/partial) revision rows, in row then text order, deduped."""
    seen: dict[str, None] = {}
    for row in rows:
        if _origin(row.get(rr.ROW_ANCHOR))[0] != ORIGIN_REVISION:
            continue
        if row.get(rr.ROW_STATUS) not in (rr.STATUS_OPEN, rr.STATUS_PARTIAL):
            continue
        text = row.get(rr.ROW_TEXT)
        for found in RULING_ID_RE.findall(text if isinstance(text, str) else ""):
            seen.setdefault(found)
    return tuple(seen)


def read_run(dir_: Path) -> Optional[dict]:
    """run.json, or None when the run was emitted without a ledger."""
    try:
        data = json.loads((Path(dir_) / RUN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_run(dir_: Path, data: dict) -> None:
    from coordinator_core.atomic_replace import atomic_write_bytes

    Path(dir_).mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(
        Path(dir_) / RUN_FILE, (json.dumps(data, sort_keys=True) + "\n").encode("utf-8"), preserve_mode=False
    )


def _inbox(dir_: Path) -> list[dict]:
    try:
        raw = (Path(dir_) / INBOX_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # a torn tail line from a crashed writer; the rest stands
        if isinstance(entry, dict) and isinstance(entry.get("text"), str) and entry["text"].strip():
            out.append(entry)
    return out


def carry_inbox(repo_root: Path, from_run_ids: Iterable[str], to_run_id: str) -> int:
    """Append the queued revisions filed under `from_run_ids` that `to_run_id`'s inbox lacks (matched on
    stripped text); the count appended. Unsafe ids and missing dirs carry nothing."""
    from coordinator_core.atomic_append import append_line

    try:
        dest = run_dir(repo_root, to_run_id)
    except LedgerRefused:
        return 0
    if not dest.is_dir():
        return 0
    have = {e["text"].strip() for e in _inbox(dest)}
    carried = 0
    for source_id in from_run_ids:
        if source_id == to_run_id:
            continue
        try:
            source = run_dir(repo_root, source_id)
        except LedgerRefused:
            continue
        for entry in _inbox(source):
            key = entry["text"].strip()
            if key in have:
                continue
            append_line(dest / INBOX_FILE, (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))
            have.add(key)
            carried += 1
    if carried:
        declare_write(dest / INBOX_FILE)
    return carried


def read_notes(dir_: Path) -> list[str]:
    """The `note` strings of the run's `NOTES_FILE` not retracted, oldest first; malformed lines and
    non-string notes are skipped, a missing file yields []. A retracted note names no path to
    `noted_paths`."""
    from coordinator_core.ops.dispatch_emit import run_notes

    return [e["note"] for e in run_notes.live(run_notes.entries(Path(dir_) / NOTES_FILE, ""))]


def noted_paths(repo_root: Path, dir_: Path) -> list[str]:
    """Repo-relative, forward-slash paths of the files the run's stored notes name, first-seen order."""
    return _paths_named(repo_root, read_notes(dir_))


def path_tokens(text: str) -> list[str]:
    """The whitespace-split tokens of `text` with wrapping quotes, brackets and trailing
    punctuation stripped; candidates only, none checked against disk."""
    out = []
    for token in text.split():
        out.append(token.strip("`'\"[]<>(").rstrip(".,;:)").strip("`'\"[]<>("))
    return out


def _paths_named(repo_root: Path, notes: Iterable[str]) -> list[str]:
    from coordinator_core.ops.dispatch_emit.design_conformance import _contained_file

    root = Path(repo_root).resolve()
    out: list[str] = []
    for note in notes:
        for token in path_tokens(note):
            found = _contained_file(root, token)
            if found is None:
                continue
            rel = found.relative_to(root).as_posix()
            if rel not in out:
                out.append(rel)
    return out


def run_footprint(repo_root: Path, dir_: Path, sizing_rel: Optional[str]) -> Optional[tuple[set[str], list[str]]]:
    """`(files, dirs)` a staged run may write: its manifest rows' `writes` and `writes_under`, its
    `doc_writes`, and the bound sizing's `writes`. None while the run has no manifest (nothing is
    frozen yet, so no declared write can fall outside it)."""
    try:
        manifest = json.loads((Path(dir_) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    files: set[str] = {w for w in manifest.get("doc_writes") or [] if isinstance(w, str)}
    dirs: list[str] = []
    for row in manifest.get("rows") or []:
        if isinstance(row, dict):
            files.update(w for w in row.get("writes") or [] if isinstance(w, str))
            dirs.extend(d.rstrip("/") for d in row.get("writes_under") or [] if isinstance(d, str))
    files.update(_sizing_writes(repo_root, sizing_rel))
    return files, dirs


def _sizing_writes(repo_root: Path, sizing_rel: Optional[str]) -> set[str]:
    """The bound sizing's `writes` entries; empty when unbound or unreadable."""
    if not sizing_rel:
        return set()
    import yaml

    try:
        sizing = yaml.safe_load(_sizing_path(Path(repo_root), sizing_rel).read_text(encoding="utf-8"))
    except (LedgerRefused, OSError, yaml.YAMLError):
        return set()
    return {w for w in sizing.get("writes") or [] if isinstance(w, str)} if isinstance(sizing, dict) else set()


def _addition_route(run_id: str, paths: Sequence[str]) -> str:
    return (
        f"run {run_id}'s write set does not cover {', '.join(paths)}; workflow.revise cannot add scope or "
        "write paths. Fire a new ask naming them, or resize the sizing (add them to its `writes`) and refire."
    )


def _refuse_additions(
    root: Path, dir_: Path, run_id: str, run: Optional[Mapping], add_paths: Sequence,
) -> None:
    """LedgerRefused naming every `add_paths` entry outside the run's write set. An unstaged run's
    write set is its sizing's `writes` alone. A note is prose: a path it names (a tool to run, a
    source) is never a declared write, so it is never checked. Writes nothing."""
    sizing_rel = run.get("sizing_rel") if run is not None and isinstance(run.get("sizing_rel"), str) else None
    declared: list[str] = []
    for entry in add_paths:
        raw = entry.get("path") if isinstance(entry, Mapping) else entry
        if not isinstance(raw, str) or not raw.strip():
            raise LedgerRefused("add_paths must be a list of repo-relative path strings")
        declared.append(raw.strip().replace("\\", "/").removeprefix("./"))
    outside: list[str] = []
    if declared:
        footprint = run_footprint(root, dir_, sizing_rel)
        files, dirs = footprint if footprint is not None else (_sizing_writes(root, sizing_rel), [])
        outside = [p for p in dict.fromkeys(declared) if p not in files and not any(p.startswith(d + "/") for d in dirs)]
    if outside:
        raise LedgerRefused(_addition_route(run_id, outside))


def paths_outside_footprint(
    repo_root: Path, dir_: Path, sizing_rel: Optional[str], declared: Iterable[str]
) -> list[str]:
    """Declared write targets (repo-relative paths, never prose) that a staged run's frozen footprint
    does not cover; [] when it has no manifest."""
    footprint = run_footprint(repo_root, dir_, sizing_rel)
    if footprint is None:
        return []
    files, dirs = footprint
    return [p for p in dict.fromkeys(declared) if p not in files and not any(p.startswith(d + "/") for d in dirs)]


def _footprint_route(run_id: str, paths: Sequence[str]) -> str:
    return (
        f"run {run_id}'s write footprint is frozen at its manifest and does not cover {', '.join(paths)}; "
        "a refire of the same sizing cannot reach them. Send the widened scope as a follow-up ask "
        "(a new ask naming those files), or add them to the sizing's `writes` and refire."
    )


def pm_words(sizing: Optional[Mapping]) -> list[str]:
    """The PM's own words a sizing records: its `pm_verbatims` texts, else a PM-verbatim `intent`.
    Never the EM's paraphrase: an `em-elaborated` intent is left out."""
    sizing = sizing or {}
    quotes: list[str] = []
    for item in sizing.get("pm_verbatims") or []:
        text = item.get("text") if isinstance(item, Mapping) else None
        if isinstance(text, str) and text.strip():
            quotes.append(text.strip())
    intent = sizing.get("intent")
    if not quotes and sizing.get("intent_source") == "pm-verbatim" and isinstance(intent, str) and intent.strip():
        quotes.append(intent.strip())
    return quotes


def _sizing_path(root: Path, sizing_rel: str) -> Path:
    from coordinator_core.session.record_homes import home_dir

    found = contained_path(Path(root) / sizing_rel, [Path(home_dir(str(root), "sizings"))])
    if found is not None and found.is_file():
        return found
    # Trap: the archive probe is a basename rglob, so it runs only on a live miss. Writers
    # (_mutate_register, locked_rmw, apply_verdict) edit the archived record in place; that is
    # intended -- the archive is the sizing's home once the review stamp moves it.
    from coordinator_core.ops._sizing_citation import resolve_sizing_citation

    archived = resolve_sizing_citation(root, sizing_rel)
    if archived is not None and archived.is_file():
        contained = contained_path(archived, [Path(root) / "archive" / "sizings"])
        if contained is not None:
            return contained
    raise LedgerRefused(f"sizing {sizing_rel!r} is not a file under the sizings home or the sizings archive")


def _withheld_revision(row: Mapping) -> Optional[str]:
    """The tail (`out_of_scope`, or a legacy revision row id) of a deferred row's
    `workflow.revise:<run id>:<tail>` ruling ref, else None. The run id is provenance: the ruling
    withholds the row for every run bound to the sizing."""
    ruling = row.get(rr.ROW_RULING)
    ref = ruling.get(rr.RULING_REF) if isinstance(ruling, Mapping) else None
    if row.get(rr.ROW_STATUS) != rr.STATUS_DEFERRED or not isinstance(ref, str):
        return None
    head, _, rest = ref.partition(":")
    run_id, _, tail = rest.partition(":")
    return tail if head == REVISE_REF and run_id and tail else None


def ruled_out_ids(rows: Sequence[Mapping]) -> tuple[str, ...]:
    """Ids of the rows a recorded `workflow.revise` ruling withholds, in register order."""
    return tuple(str(r.get(rr.ROW_ID)) for r in rows if _withheld_revision(r))


def _ledger_of(rows: Sequence[Mapping], all_of: bool, sizing_rel: Optional[str]) -> Ledger:
    items = []
    withheld = []
    for row in rows:
        if row.get(rr.ROW_STATUS) in (rr.STATUS_DEFERRED, rr.STATUS_WAIVED):
            revision = _withheld_revision(row)
            if revision:
                reason = "withheld by out-of-scope ruling" if revision == OUT_OF_SCOPE_REF else f"withheld by revision {revision}"
                withheld.append({"id": str(row.get(rr.ROW_ID)), "reason": reason})
            continue
        origin, at = _origin(row.get(rr.ROW_ANCHOR))
        anchor = row.get(rr.ROW_ANCHOR)
        items.append(Item(
            str(row.get(rr.ROW_ID)), str(row.get(rr.ROW_TEXT) or ""), origin, at,
            anchor if isinstance(anchor, str) else "", _sender(anchor),
        ))
    return Ledger(tuple(items), all_of, sizing_rel, tuple(withheld))


def ledger_ids(rows: Sequence[Mapping]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """`(item_ids, withheld_ids)` of the ledger `_ledger_of` builds from `rows`, in register order:
    the rows the verify leg must judge, and the rows a recorded out-of-scope ruling withholds."""
    ledger = _ledger_of(rows, False, None)
    return tuple(i.id for i in ledger.items), tuple(w["id"] for w in ledger.withheld)


def _next_id(rows: Sequence[Mapping]) -> str:
    used = {str(r.get(rr.ROW_ID)) for r in rows}
    n = len(rows) + 1
    while f"R{n}" in used:
        n += 1
    return f"R{n}"


def _validated(text: str) -> str:
    import yaml

    from coordinator_core.frontmatter.schema_validate import format_validation_errors, validate_frontmatter
    from coordinator_core.locked_write import MutateAbort

    try:
        errors = validate_frontmatter(yaml.safe_load(text) or {}, _SIZING_SCHEMA)
    except Exception as exc:  # noqa: BLE001 -- surfaced as a refusal
        raise MutateAbort(f"register write refused: {exc}")
    if errors:
        raise MutateAbort(f"register write refused: {format_validation_errors(errors)}")
    return text


_SHIPPED_STATUS_LINE = re.compile(r"^status:[ \t]*[\"']?shipped[\"']?[ \t]*(?=#|$)", re.MULTILINE)


def _reopened(text: str, rows: Sequence[Mapping]) -> str:
    """`text` with a top-level `status: shipped` moved back to `routed` when `rows` holds a row the
    schema's shipped rule refuses (not met-and-wired, not deferred/waived with a ruling): a
    requirement landing on a shipped sizing reopens it."""
    if not _SHIPPED_STATUS_LINE.search(text):
        return text
    satisfied = all(
        (r.get(rr.ROW_STATUS) == rr.STATUS_MET and r.get(rr.ROW_WIRED) is True)
        or (r.get(rr.ROW_STATUS) in (rr.STATUS_DEFERRED, rr.STATUS_WAIVED) and r.get(rr.ROW_RULING))
        for r in rows
    )
    return text if satisfied else _SHIPPED_STATUS_LINE.sub("status: routed", text, count=1)


def _mutate_register(root: Path, sizing_rel: str, edit, *, unrelated: Sequence[Mapping] = ()) -> list[dict]:
    """Apply `edit(rows) -> rows | None` to the sizing's register under the file lock; None or an
    unchanged list writes nothing. `unrelated` entries (`{turn, reason}`) join the register's
    `unrelated_turns` when rows were written. Returns the rows on disk afterwards."""
    from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw

    path = _sizing_path(root, sizing_rel)
    out: dict = {"rows": []}

    def mutate(old: str) -> str:
        register = rr.read_register(old)
        rows = [dict(r) for r in register.rows] if register is not None else []
        raw = dict(register.raw) if register is not None else {}
        new_rows = edit([dict(r) for r in rows])
        out["rows"] = rows
        listed = {e.get("turn") for e in raw.get(rr.UNRELATED_TURNS_KEY) or [] if isinstance(e, dict)}
        fresh = [e for e in unrelated if e["turn"] not in listed]
        if new_rows is None or (new_rows == rows and not fresh):
            return old
        sources = [s for s in raw.get(rr.SOURCES_KEY) or [] if isinstance(s, dict)]
        if not any(s.get("doc") == sizing_rel for s in sources):
            sources.append({"doc": sizing_rel, "ref": "pm_verbatims, ask, workflow.revise"})
        block = {**raw, rr.SOURCES_KEY: sources, rr.ROWS_KEY: new_rows}
        if unrelated:
            waived = [e for e in raw.get(rr.UNRELATED_TURNS_KEY) or [] if isinstance(e, dict)]
            have_turns = {e.get("turn") for e in waived}
            waived += [dict(e) for e in unrelated if e["turn"] not in have_turns]
            block[rr.UNRELATED_TURNS_KEY] = waived
        crlf = "\r\n" in old
        new = _validated(_reopened(
            rr.write_register_text(old.replace("\r\n", "\n"), block, datetime.now(timezone.utc)), new_rows
        ))
        out["rows"] = new_rows
        return new.replace("\n", "\r\n") if crlf else new

    try:
        locked_rmw(path, mutate, repo_root=Path(root))
    except MutateAbort as exc:
        raise LedgerRefused(str(exc.args[0]) if exc.args else "register write refused") from exc
    except LockTimeout as exc:
        raise LedgerRefused(f"timed out waiting for the lock on {sizing_rel}: {exc}") from exc
    return out["rows"]


def _quotes(ask: str, turn_text: str) -> bool:
    """True when the run's ask is, contains, or quotes the PM turn."""
    from coordinator_core.ops.baton_pm_turns import quotes_turn

    a, t = " ".join(ask.lower().split()), " ".join(turn_text.lower().split())
    return bool(a and t and (a in t or t in a)) or quotes_turn(ask, turn_text)


def _session_turns(root: Path) -> list[tuple[int, str]]:
    """The session's recorded PM turns as `(turn, prompt)`; empty when no session resolves."""
    from coordinator_core.ops.baton_pm_turns import read_turns

    try:
        found = read_turns(cwd=str(root), all_turns=True)["turns"]
    except Exception:  # noqa: BLE001 -- no session log: nothing proves a turn foreign
        return []
    return [(t["turn"], str(t.get("prompt") or "")) for t in found if isinstance(t.get("turn"), int)]


def _norm(text: object) -> str:
    return " ".join(str(text).split())


_MIN_FOREIGN_MATCH = 12  # a shorter turn ("ok", "do it") sits inside too many texts to prove identity


def _int_turn(value: object) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def foreign_turns(
    root: Path, sizing: Optional[Mapping], ask_text: Optional[str], relayed_text: Optional[str],
    *, followups: bool = False,
) -> dict[int, str]:
    """`{turn: normalised text}` of the sizing's embedded PM turns that provably belong to another
    run. One-turn sizing: the lone turn, when the ask does not quote it and quotes a different
    recorded session turn (the sizing embedded the newest turn by recency). Multi-turn sizing: an
    embedded turn the ask does not quote that matches a recorded session turn preceding every
    session turn the ask quotes; a later turn (a follow-up) is never foreign. Empty for a relayed
    run, a run without an ask, or an ask no recorded session turn quotes. `followups=True` also
    returns the later turns the lenient rule keeps: a session turn neither quoted by the ask nor
    relayed or revised into the run is not this run's requirement."""
    if (relayed_text and relayed_text.strip()) or not (ask_text and ask_text.strip()):
        return {}
    embedded = [
        (n, str(v.get("text") or ""))
        for v in (sizing or {}).get("pm_verbatims") or []
        if isinstance(v, Mapping) and (n := _int_turn(v.get("turn"))) is not None
    ]
    turns = [(n, t) for n, t in embedded if not _quotes(ask_text, t)]
    if not turns:
        return {}
    from coordinator_core.ops.baton_pm_turns import quotes_turn

    # Proof of "another run's turn" uses the word-run rule only: a short turn ("ok", "do it")
    # that happens to sit inside the ask must not prove a turn foreign.
    session = _session_turns(root)
    quoted = [n for n, text in session if quotes_turn(ask_text, text)]
    if not quoted:
        return {}
    if len(embedded) == 1:
        lone, text = turns[0]
        return {lone: _norm(text)} if any(n != lone for n in quoted) else {}
    earliest = min(quoted)
    recorded = {n: _norm(text) for n, text in session}
    out: dict[int, str] = {}
    for n, text in turns:
        mine, theirs = _norm(text), recorded.get(n, "")
        if min(len(mine), len(theirs)) < _MIN_FOREIGN_MATCH or not (mine in theirs or theirs in mine):
            continue
        if (followups and not _CONTINUATION_OPENER.match(mine)) or n < earliest or not _shares_content_word(ask_text, mine):
            out[n] = mine
    return out


# A later turn that opens as a continuation ("also ...", "and ...") is the PM extending the ask:
# it stays coverable even in `followups` mode, where a bare shared word does not.
_CONTINUATION_OPENER = re.compile(r"\s*(also|and|plus|additionally|furthermore|then)\b")


_CONTENT_WORD = re.compile(r"[a-z0-9]{5,}")


def _shares_content_word(ask: str, turn_text: str) -> bool:
    """True when the turn and the ask share a word of five or more characters: the mark of a
    follow-up, as opposed to an unrelated session turn (a question about token usage).
    Lexical and lenient: one shared generic word keeps a turn coverable."""
    return bool(set(_CONTENT_WORD.findall(ask.lower())) & set(_CONTENT_WORD.findall(turn_text.lower())))


FOREIGN_TURN_REASON = "another run's PM turn: the run's ask neither quotes nor addresses it"


def _append_rows(
    root: Path, sizing_rel: str, entries: Sequence[tuple[str, str]], *, drop_turns: Optional[Mapping[int, str]] = None,
    withhold: Optional[Mapping] = None, list_turns: Optional[Mapping[int, str]] = None,
    plan_rows: Sequence[tuple[str, str]] = (), reuse: Optional[dict] = None, retract_row: Optional[str] = None,
) -> list[dict]:
    """Append one open row per `(text, anchor)`; a PM-word or ask row already on the register
    (same verbatim text) is not added twice, a revision always is, unless `reuse` is a dict: then a
    revision whose text equals a live (open, partial or met) revision row adds nothing and
    `reuse["id"]` receives that row's id. `retract_row` (a row id) deletes that row in the same
    write, refusing unless it is an open, unclaimed revision row. `drop_turns` (`{turn: text}`)
    removes the open, unclaimed rows derived from those PM turns and lists them in
    `unrelated_turns`. `withhold` (`{"ids": [...], "run_id", "verbatim"}`) defers the named rows
    with a ruling quoting the verbatim, in the same register write; it appends no row itself, and
    when every named row is already deferred and no entry is added the write is skipped. An
    unknown id or a met/waived row refuses the whole write."""

    drop_turns = drop_turns or {}
    list_turns = list_turns or {}

    def edit(rows: list[dict]) -> list[dict]:
        named = list((withhold or {}).get("ids") or [])
        by_id = {str(r.get(rr.ROW_ID)): r for r in rows}
        unknown = [i for i in dict.fromkeys(named) if i not in by_id]
        if unknown:
            raise MutateAbort(f"out_of_scope names no register row: {', '.join(unknown)}")
        closed = [i for i in dict.fromkeys(named) if by_id[i].get(rr.ROW_STATUS) in (rr.STATUS_MET, rr.STATUS_WAIVED)]
        if closed:
            raise MutateAbort(f"out_of_scope names a row already {by_id[closed[0]].get(rr.ROW_STATUS)}: {', '.join(closed)}")
        if drop_turns:
            anchors = {f"{ORIGIN_PM_VERBATIMS} turn {n}" for n in drop_turns}
            texts = set(drop_turns.values())
            rows = [
                r for r in rows
                if not (
                    (
                        str(r.get(rr.ROW_ANCHOR) or "") in anchors
                        or (r.get(rr.ROW_ANCHOR) == ORIGIN_PM_VERBATIMS and _norm(r.get(rr.ROW_TEXT) or "") in texts)
                    )
                    and r.get(rr.ROW_STATUS) == rr.STATUS_OPEN and not r.get(rr.ROW_CLAIMED_BY)
                )
            ]
        if retract_row is not None:
            target = by_id.get(retract_row)
            if target is None:
                raise MutateAbort(f"retract_row names no register row: {retract_row}")
            if _origin(target.get(rr.ROW_ANCHOR))[0] != ORIGIN_REVISION:
                raise MutateAbort(f"retract_row {retract_row} is not a revision row: only a revision can be withdrawn")
            if target.get(rr.ROW_STATUS) != rr.STATUS_OPEN:
                raise MutateAbort(f"retract_row {retract_row} is already {target.get(rr.ROW_STATUS)}: only an open row can be withdrawn")
            if rr.claimants(target):
                raise MutateAbort(f"retract_row {retract_row} is claimed by {', '.join(rr.claimants(target))}: release the claim first")
            rows = [r for r in rows if str(r.get(rr.ROW_ID)) != retract_row]
        have = {str(r.get(rr.ROW_TEXT) or "").strip() for r in rows}
        live_revisions = {
            str(r.get(rr.ROW_TEXT) or "").strip(): str(r.get(rr.ROW_ID))
            for r in reversed(rows)
            if _origin(r.get(rr.ROW_ANCHOR))[0] == ORIGIN_REVISION
            and r.get(rr.ROW_STATUS) in (rr.STATUS_OPEN, rr.STATUS_PARTIAL, rr.STATUS_MET)
        }
        for text, anchor in entries:
            t = text.strip()
            if reuse is not None and t in live_revisions and anchor.startswith(ORIGIN_REVISION + "@"):
                reuse["id"] = live_revisions[t]
                continue
            if not t or (anchor in (ORIGIN_PM_VERBATIMS, ORIGIN_ASK, ORIGIN_RELAYED) and t in have):
                continue
            have.add(t)
            rows.append({
                rr.ROW_ID: _next_id(rows), rr.ROW_TEXT: t, rr.ROW_ANCHOR: anchor,
                rr.ROW_SURFACE: ROW_SURFACE, rr.ROW_STATUS: rr.STATUS_OPEN, rr.ROW_CLAIMED_BY: [],
            })
        if plan_rows and ledger_ids(rows) == ((), ()):
            for pid, title in plan_rows:
                rows.append({
                    rr.ROW_ID: _next_id(rows), rr.ROW_TEXT: f"Plan row {pid}: {title}".strip(),
                    rr.ROW_ANCHOR: f"{PLAN_ROW_ANCHOR_PREFIX}{pid}",
                    rr.ROW_SURFACE: ROW_SURFACE, rr.ROW_STATUS: rr.STATUS_OPEN, rr.ROW_CLAIMED_BY: [],
                })
        if named:
            ruling = {
                rr.RULING_SOURCE: "pm", rr.RULING_QUOTE: str(withhold["verbatim"]),
                rr.RULING_REF: f"{REVISE_REF}:{withhold['run_id']}:{OUT_OF_SCOPE_REF}", rr.RULING_ON: _now()[:10],
            }
            for rid in dict.fromkeys(named):
                row = by_id[rid]
                if row.get(rr.ROW_STATUS) != rr.STATUS_DEFERRED:
                    row[rr.ROW_STATUS] = rr.STATUS_DEFERRED
                    row[rr.ROW_RULING] = dict(ruling)
        return rows

    unrelated = [{"turn": n, "reason": FOREIGN_TURN_REASON} for n in sorted({*drop_turns, *list_turns})]
    return _mutate_register(root, sizing_rel, edit, unrelated=unrelated)


def render(ledger: Ledger, run_id: str) -> str:
    where = f"the requirement_register of {ledger.sizing_rel}" if ledger.sizing_rel else "this run (sizing not yet bound)"
    lines = [
        f"# Requirements -- run {run_id}",
        "",
        f"Every row of {where} still to deliver: the PM's words, the ask (origin `relayed`: an EM's relay, "
        "not the PM's words; a `pm_verbatims` row outranks it), and each revision the EM "
        "(or a peer session, named in `from`) sent while the run was in flight, in the order they "
        "arrived. A revision governs over an earlier row it changes.",
        "",
        "All of the PM's ask is mandatory (all_of_pm_ask): " + ("yes" if ledger.all_of_pm_ask else "no"),
        "",
    ]
    for item in ledger.items:
        default_origin = "design registry" if _is_design(item.anchor) else "source document"
        tag = ", ".join(p for p in (item.origin or default_origin, item.at, f"from {item.sender}" if item.sender else "") if p)
        lines.append(f"## {item.id} ({tag})")
        lines.append("")
        lines.extend("> " + ln if ln else ">" for ln in item.text.strip().splitlines())
        lines.append("")
    if ledger.withheld:
        lines.append("## Out of scope (ruled by a revision; not judged, carried open)")
        lines.append("")
        lines.extend(f"- {w['id']}: {w['reason']}" for w in ledger.withheld)
        lines.append("")
    return "\n".join(lines)


def _queued_ledger(dir_: Path, all_of: bool) -> Ledger:
    """The revisions waiting in the run's inbox as `Q<n>` items (no sizing bound, or no emit yet)."""
    return Ledger(
        tuple(
            Item(
                f"Q{n + 1}", e["text"].strip(), ORIGIN_REVISION, str(e.get("at") or ""),
                sender=" ".join(str(e.get("from") or "").split()),
            )
            for n, e in enumerate(_inbox(dir_))
        ),
        all_of,
    )


def read_ledger(repo_root: Path, dir_: Path) -> Optional[Ledger]:
    """The run's requirements, or None when the run was emitted without a ledger. Before the
    sizing is bound, the queued revisions are the only items. LedgerRefused when the bound
    sizing is unreadable."""
    run = read_run(dir_)
    if run is None:
        return None
    all_of = bool(run.get("all_of_pm_ask"))
    sizing_rel = run.get("sizing_rel") if isinstance(run.get("sizing_rel"), str) else None
    if not sizing_rel:
        return _queued_ledger(dir_, all_of)
    try:
        text = _sizing_path(repo_root, sizing_rel).read_text(encoding="utf-8")
    except OSError as exc:
        raise LedgerRefused(f"cannot read sizing {sizing_rel}: {exc}") from exc
    register = rr.read_register(text)
    return _ledger_of(register.rows if register is not None else [], all_of, sizing_rel)


def _write_view(repo_root: Path, dir_: Path, run_id: str) -> Optional[Ledger]:
    from coordinator_core.atomic_replace import atomic_write_bytes

    try:
        ledger = read_ledger(repo_root, dir_)
    except LedgerRefused:
        # The view is advisory; terminal_commit._ledger_gate refuses the unreadable sizing.
        return None
    if ledger is not None:
        from coordinator_core.ops.dispatch_emit import run_notes

        body = render(ledger, run_id)
        section = run_notes.render_section(carried_notes(repo_root, run_id))
        if section:
            body = body.rstrip("\n") + "\n\n" + "\n".join(section)
        atomic_write_bytes(dir_ / LEDGER_VIEW_FILE, body.encode("utf-8"), preserve_mode=False)
    return ledger


def start(
    repo_root: Path, run_id: str, *, all_of_pm_ask: bool = False, resumed_from: Optional[str] = None,
) -> Path:
    """Make the run ledger-bearing (emit time). Returns the run dir. `resumed_from` names the halted
    run this one resumes; omitted, a rebound run keeps the link its run.json already carries."""
    dir_ = run_dir(repo_root, run_id)
    prior = read_run(dir_) or {}
    data = {
        "sizing_rel": prior.get("sizing_rel"),
        "all_of_pm_ask": bool(all_of_pm_ask) or bool(prior.get("all_of_pm_ask")),
    }
    if resumed_from is not None:
        if not safe_id(resumed_from) or resumed_from == run_id:
            raise LedgerRefused(f"resumed_from {resumed_from!r} is not a safe run id distinct from {run_id!r}")
        data[RESUMED_FROM_FIELD] = resumed_from
    elif isinstance(prior.get(RESUMED_FROM_FIELD), str):
        data[RESUMED_FROM_FIELD] = prior[RESUMED_FROM_FIELD]
    _write_run(dir_, data)
    _write_view(repo_root, dir_, run_id)
    stamp_run_stage(repo_root, run_id, "minted")
    return dir_


def _note_entries(dir_: Path, run_id: str) -> list[dict]:
    from coordinator_core.ops.dispatch_emit import run_notes

    return run_notes.entries(Path(dir_) / NOTES_FILE, run_id)


def carried_notes(repo_root: Path, run_id: str) -> list[dict]:
    """`{id, note, at, run, retracted}` entries of this run's `NOTES_FILE` and of its `resumed_from`
    chain (each predecessor's `resumed_from`, cycle-safe, at most `CARRIED_HOP_CAP` hops),
    retracted ones included, oldest first, de-duplicated on (note, at)."""
    own_dir = run_dir(repo_root, run_id)
    entries = _note_entries(own_dir, run_id)
    visited = {run_id}
    cur_dir = own_dir
    for _ in range(CARRIED_HOP_CAP):
        prev = (read_run(cur_dir) or {}).get(RESUMED_FROM_FIELD)
        if not (isinstance(prev, str) and safe_id(prev)) or prev in visited:
            break
        visited.add(prev)
        cur_dir = run_dir(repo_root, prev)
        entries = _note_entries(cur_dir, prev) + entries
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for e in sorted(entries, key=lambda e: e["at"]):
        key = (e["note"], e["at"])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def _seedable_words(sizing: Mapping, foreign: Mapping[int, str]) -> list[str]:
    """`pm_words` minus the turns bind must not seed: a turn the sizing's register lists in
    `unrelated_turns`, one proven foreign this bind, and one an existing register row covers
    (the `requirement_register.coverage_gaps` rule)."""
    register = (sizing.get(rr.REGISTER_KEY) if isinstance(sizing.get(rr.REGISTER_KEY), Mapping) else {}) or {}
    skip = {n for e in register.get(rr.UNRELATED_TURNS_KEY) or [] if isinstance(e, Mapping) and (n := _int_turn(e.get("turn"))) is not None}
    skip.update(foreign)
    needles = [n for r in register.get(rr.ROWS_KEY) or [] if isinstance(r, Mapping) and (n := _norm(r.get(rr.ROW_TEXT) or ""))]
    verbatims = [v for v in sizing.get("pm_verbatims") or [] if isinstance(v, Mapping)]
    if not any(isinstance(v.get("text"), str) and v["text"].strip() for v in verbatims):
        return [t for t in pm_words(sizing) if not any(n in _norm(t) for n in needles)]
    out = []
    for v in verbatims:
        text = v.get("text")
        if not (isinstance(text, str) and text.strip()) or _int_turn(v.get("turn")) in skip:
            continue
        if not any(n in _norm(text) for n in needles):
            out.append(text.strip())
    return out


def _unbound_followups(
    root: Path, sizing: Mapping, ask_text: Optional[str], relayed_text: Optional[str], foreign: Mapping[int, str],
) -> dict[int, str]:
    """Recorded session turns after the ask that no register row covers and that bind has not
    already proven foreign: they were neither relayed into the run nor revise it, so bind lists
    them under `unrelated_turns` instead of seeding a row."""
    recorded = foreign_turns(root, sizing, ask_text, relayed_text, followups=True)
    register = (sizing.get(rr.REGISTER_KEY) if isinstance(sizing.get(rr.REGISTER_KEY), Mapping) else {}) or {}
    needles = [n for r in register.get(rr.ROWS_KEY) or [] if isinstance(r, Mapping) and (n := _norm(r.get(rr.ROW_TEXT) or ""))]
    return {n: t for n, t in recorded.items() if n not in foreign and not any(x in t for x in needles)}


def validate_owned_red(raw: object) -> list[dict]:
    """`[{nodeid, owner_run_id, reason?}]` as clean entries; a malformed entry raises ValueError naming its index."""
    if not isinstance(raw, (list, tuple)):
        raise ValueError("owned_red must be a list of {nodeid, owner_run_id}")
    out: list[dict] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise ValueError(f"owned_red[{i}] is not an object with nodeid and owner_run_id")
        nodeid, owner = entry.get("nodeid"), entry.get("owner_run_id")
        if not isinstance(nodeid, str) or "::" not in nodeid.strip():
            raise ValueError(f"owned_red[{i}].nodeid {nodeid!r} is not a pytest node id (path::test)")
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError(f"owned_red[{i}].owner_run_id is empty")
        reason = entry.get("reason")
        out.append({"nodeid": nodeid.strip(), "owner_run_id": owner.strip(), "reason": reason.strip() if isinstance(reason, str) else ""})
    return out


def _lettered_entries(text: object) -> list[tuple[str, str]]:
    """One `relayed` entry `(label) part` per lettered part of `text`; `_append_rows` skips one the
    register already holds."""
    from coordinator_core.ops.dispatch_emit import stated_parts

    return [(f"({p.label}) {p.text}", ORIGIN_RELAYED) for p in stated_parts.lettered_parts(str(text or ""))]


def _slice_entries(sizing: Mapping) -> list[tuple[str, str]]:
    """A sibling slice shares its `pm_verbatims` with every other slice, so it seeds none of them:
    its register already holds the rows the split assigned it, else its `intent` is its one row,
    anchored `relayed` when the slice's `em_analysis.ask_source` is relayed and `ask` otherwise.
    Context stamped on the slice (`em_brief`, `em_context_paths`) is the relayer's words: it adds a
    second row anchored `relayed` whatever the slice's own source."""
    register = sizing.get(rr.REGISTER_KEY)
    if isinstance(register, Mapping) and register.get(rr.ROWS_KEY):
        return []
    intent = sizing.get("intent")
    origin = ORIGIN_RELAYED if slice_ask_source(sizing) == ASK_SOURCE_RELAYED else ORIGIN_ASK
    entries = [(intent, origin)] if isinstance(intent, str) and intent.strip() else []
    brief = read_em_brief(sizing)
    paths = read_em_context_paths(sizing)
    if brief or paths:
        lines = ([brief] if brief else []) + [f"Context files: {p}" for p in paths]
        entries.append(("\n".join(lines), ORIGIN_RELAYED))
    return entries


def bind(
    repo_root: Path, run_id: str, sizing_rel: str, *, ask_text: Optional[str] = None,
    relayed_text: Optional[str] = None, plan_rows: Sequence[tuple[str, str]] = (),
) -> Optional[Ledger]:
    """Bind a ledger-bearing run to its sizing and seed that sizing's register: the PM's words,
    the raw ask, then every queued revision. `plan_rows` (`(row id, title)`, spine order) seed one
    `plan-row:<id>` row each, only when the register would otherwise hold no judged or withheld
    row. Idempotent; a run without run.json is left alone
    (None). Raises LedgerRefused when the run names another sizing or the write is refused."""
    root = Path(repo_root)
    dir_ = run_dir(root, run_id)
    run = read_run(dir_)
    if run is None:
        return None
    bound = run.get("sizing_rel")
    if bound and bound != sizing_rel:
        raise LedgerRefused(f"run {run_id} is bound to {bound}, not {sizing_rel}")
    import yaml

    try:
        sizing = yaml.safe_load(_sizing_path(root, sizing_rel).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LedgerRefused(f"cannot read sizing {sizing_rel}: {exc}") from exc
    sizing = sizing if isinstance(sizing, dict) else {}
    foreign = foreign_turns(root, sizing, ask_text, relayed_text)
    unbound = _unbound_followups(root, sizing, ask_text, relayed_text, foreign)
    if rr.is_sibling_slice(sizing):
        entries = _slice_entries(sizing)
    elif relayed_text and relayed_text.strip():
        entries = []
    else:
        entries = [(t, ORIGIN_PM_VERBATIMS) for t in _seedable_words(sizing, {**foreign, **unbound})]
    entries.extend(_lettered_entries(read_em_brief(sizing)))
    register = (sizing.get(rr.REGISTER_KEY) if isinstance(sizing.get(rr.REGISTER_KEY), Mapping) else {}) or {}
    rows_now = [r for r in register.get(rr.ROWS_KEY) or [] if isinstance(r, Mapping)]
    asked = any(r.get(rr.ROW_ANCHOR) in (ORIGIN_ASK, ORIGIN_RELAYED) for r in rows_now)
    held = {str(r.get(rr.ROW_TEXT) or "").strip() for r in rows_now}
    # A reused sizing already holds its first ask: a later, different ask is a revision of it.
    for text, origin in ((ask_text, ORIGIN_ASK), (relayed_text, ORIGIN_RELAYED)):
        if text and text.strip():
            fresh = asked and text.strip() not in held
            entries.append((text, f"{ORIGIN_REVISION}@{_now()}" if fresh else origin))
    queued = _inbox(dir_)
    entries.extend((e["text"], _revision_anchor(str(e.get("at") or _now()), e.get("from"))) for e in queued)
    if entries or foreign or unbound or plan_rows:
        _append_rows(root, sizing_rel, entries, drop_turns=foreign, list_turns=unbound, plan_rows=plan_rows)
    if not bound:
        _write_run(dir_, {**run, "sizing_rel": sizing_rel})
    if queued:
        (dir_ / INBOX_FILE).unlink(missing_ok=True)
    return _write_view(root, dir_, run_id)


def revise(
    repo_root: Path, run_id: str, *, verbatim: Optional[str] = None, all_of_pm_ask: Optional[bool] = None,
    out_of_scope: Sequence[str] = (), note: Optional[str] = None, sender: Optional[str] = None,
    withhold_paths: Sequence = (), credit_chunks: Sequence = (), build_only: bool = False,
    outcome: Optional[dict] = None, stop_after: Optional[str] = None, retract_note: Optional[dict] = None,
    claim: bool = False, owned_red: Sequence = (), add_paths: Sequence = (), retract_row: Optional[dict] = None,
    widened: Sequence = (), accept_partial: Sequence = (),
) -> tuple[Ledger, Path, Optional[str]]:
    """Append the EM's revision (and/or set the flag) on an emitted run: a register row when the
    run's sizing is bound, else a queued revision `bind` folds in. `note` is free text that is
    not a requirement: it is persisted to `NOTES_FILE` and adds no row or queued revision. Any non-empty `verbatim`
    is a requirement and always adds a row: free text belongs in `note`. `out_of_scope` ids are deferred
    in the same register write as the revision row, the verbatim being their ruling; it needs that
    verbatim and a bound sizing; that verbatim is only the ruling's quote and adds no row, and a
    repeated call changes nothing. `sender` names the peer session that sent the revision; it is
    stamped on the row anchor (`revision@<UTC> from <sender>`) and, for a queued revision, on the
    inbox entry. A run with no run.json yet (held, not emitted) queues a `verbatim` in its inbox,
    which `bind` folds in, only when an engine-minted parent record lists the run id (any other
    unknown id refuses and creates no directory); the flag, `out_of_scope` and `withhold_paths` need an emitted run and refuse.
    `withhold_paths` (`[{path, reason}]`, `run_withhold.validate` before any write) rules product paths
    out of the run's terminal commit: it is recorded to the run's `withhold.jsonl`, needs no bound
    sizing, adds no register row and no inbox entry, and alone satisfies the non-empty precondition.
    `add_paths` (repo-relative paths) names write paths the run was not sized for: any outside the run's
    write set refuses, as does a `note` citing an existing repo file outside a staged run's footprint;
    the refusal names the routes (a new ask, or resize the sizing) and nothing is written.
    `accept_partial` (`[{chunk, reason}]`, `run_accept_partial.validate` before any write) rules PARTIAL
    chunks good enough to release their dependents on the refire: recorded to the run's
    `accept_partial.jsonl`, same preconditions and effects as `withhold_paths`; a BLOCKED chunk stays held.
    `credit_chunks` (`[{chunk, evidence}]`, `run_credit.validate` before any write) marks stranded
    chunks complete: recorded to the run's `credit.jsonl`, same preconditions and effects as
    `withhold_paths`; the re-fired terminal commit re-grades the named chunks.
    `build_only=True` rules the research half done or abandoned: it removes the bound sizing's `research`
    block (`_research_sizing.drop_research_block`) so the ask gate stops reading it as a research sizing.
    It needs an emitted run bound to a sizing (else it refuses and writes nothing), adds no row, and
    alone satisfies the non-empty precondition; `outcome`, when given, receives `research_dropped`.
    `stop_after` (only `STOP_AFTER_PLAN`; any other value refuses) parks the run after planning: it
    needs an emitted run whose record dir holds no `manifest.json` (a staged run cannot be parked),
    writes `STOP_AFTER_FILE` atomically, adds no row, and alone satisfies the non-empty precondition;
    a repeat rewrites the marker.
    `retract_note` (`{note_id, reason}`, `run_notes.validate_retract` before any write) withdraws one of
    this run's notes as applied or superseded: it needs an emitted run (a halted one qualifies), refuses
    an id the run's `NOTES_FILE` lacks or already retracted, appends a retraction line to `NOTES_FILE`
    (nothing is deleted), adds no row, and alone satisfies the non-empty precondition.
    A `verbatim` equal (after strip) to a live revision row on the register, or to a queued inbox
    entry, mints nothing and returns that row's id. `retract_row` (`{row_id, reason}`) deletes one
    open, unclaimed revision row of the bound sizing's register (not claimed by a plan either),
    adds no row, refuses otherwise with nothing written, and alone satisfies the non-empty
    precondition; the same verbatim sent afterwards mints a fresh row.
    `claim=True` records a ruling as work: it needs a non-empty `verbatim` (the row it mints), an
    emitted run bound to a sizing, and a sizing whose `plan:` edge names a plan whose
    `register_claims.sizing` is that sizing, else it refuses with nothing written. After the row is
    minted it appends the id to the plan's `register_claims.rows` with its pin (an id already
    claimed is left as is) and adds the id to `ruled` on the plan's ledger-halt pointer when one
    exists. `outcome["claimed"]` receives `[id]`. It cannot combine with `out_of_scope`, which adds no row.
    Returns (ledger, run dir, the new row id or None)."""
    text = verbatim.strip() if isinstance(verbatim, str) else ""
    named = [str(i) for i in out_of_scope]
    has_note = isinstance(note, str) and bool(note.strip())
    withheld_entries: list[dict] = []
    if withhold_paths:
        from coordinator_core.ops.dispatch_emit import run_withhold

        try:
            withheld_entries = run_withhold.validate(withhold_paths)
        except ValueError as exc:
            raise LedgerRefused(str(exc)) from exc
    accept_entries: list[dict] = []
    if accept_partial:
        from coordinator_core.ops.dispatch_emit import run_accept_partial

        try:
            accept_entries = run_accept_partial.validate(accept_partial)
        except ValueError as exc:
            raise LedgerRefused(str(exc)) from exc
    credit_entries: list[dict] = []
    if credit_chunks:
        from coordinator_core.ops.dispatch_emit import run_credit

        try:
            credit_entries = run_credit.validate(credit_chunks)
        except ValueError as exc:
            raise LedgerRefused(str(exc)) from exc
    red_entries: list[dict] = []
    if owned_red:
        try:
            red_entries = validate_owned_red(owned_red)
        except ValueError as exc:
            raise LedgerRefused(str(exc)) from exc
    retraction: Optional[dict] = None
    if retract_note is not None:
        from coordinator_core.ops.dispatch_emit import run_notes

        try:
            retraction = run_notes.validate_retract(retract_note)
        except ValueError as exc:
            raise LedgerRefused(str(exc)) from exc
    row_retraction: Optional[str] = None
    if retract_row is not None:
        rid = retract_row.get("row_id") if isinstance(retract_row, dict) else None
        why = retract_row.get("reason") if isinstance(retract_row, dict) else None
        if not isinstance(retract_row, dict) or not isinstance(rid, str) or not rid.strip():
            raise LedgerRefused("retract_row must be an object with row_id and reason")
        if not isinstance(why, str) or not why.strip():
            raise LedgerRefused("retract_row.reason is empty")
        if text or named:
            raise LedgerRefused("retract_row cannot combine with verbatim or out_of_scope")
        row_retraction = rid.strip()
    if stop_after is not None and stop_after != STOP_AFTER_PLAN:
        raise LedgerRefused(f"stop_after takes only {STOP_AFTER_PLAN!r}, got {stop_after!r}")
    if (
        not text and all_of_pm_ask is None and not has_note and not withheld_entries and not credit_entries
        and not accept_entries and not red_entries and not build_only and stop_after is None and retraction is None and not add_paths
        and not widened and row_retraction is None
    ):
        raise LedgerRefused(
            "workflow.revise needs a non-empty verbatim, note, all_of_pm_ask, withhold_paths, "
            "accept_partial, credit_chunks, retract_note, retract_row, build_only, stop_after, or a combination"
        )
    if named and not text:
        raise LedgerRefused("out_of_scope needs a non-empty verbatim: the ruling carries its words")
    if len(text.encode("utf-8")) > VERBATIM_BYTE_CAP:
        raise LedgerRefused(f"verbatim exceeds {VERBATIM_BYTE_CAP} bytes")
    root = Path(repo_root)
    dir_ = run_dir(root, run_id)
    run = read_run(dir_)
    _refuse_additions(root, dir_, run_id, run, add_paths)
    if run is None:
        from coordinator_core.ops.dispatch_emit import pipeline_run
        from coordinator_core.ops.dispatch_emit.pipeline_contract import RUN_ID_PREFIX

        script = pipeline_run.emitted_pipeline_run(root, run_id)
        if script is None and str(run_id).startswith(RUN_ID_PREFIX):
            raise LedgerRefused(
                f"research pipeline run {run_id!r} has no emitted receipt: revision not delivered"
            )
        if script is not None:
            if pipeline_run.pipeline_run_state(root, run_id) == "finished":
                raise LedgerRefused(f"research pipeline run {run_id!r} is finished: revision not delivered")
            if (
                named or all_of_pm_ask is not None or withheld_entries or credit_entries or red_entries or build_only
                or stop_after or retraction is not None or claim or accept_entries
            ):
                raise LedgerRefused(
                    f"{run_id!r} is a research pipeline run: it takes only a verbatim or note revision, "
                    "not out_of_scope, all_of_pm_ask, withhold_paths, credit_chunks, build_only, stop_after, "
                    "retract_note or claim"
                )
            try:
                emitted_text = script.read_text(encoding="utf-8")
            except OSError as exc:
                raise LedgerRefused(f"cannot read the emitted script of research pipeline run {run_id!r}: {exc}") from exc
            if not pipeline_run.reads_revisions(emitted_text):
                raise LedgerRefused(
                    f"research pipeline run {run_id!r} was emitted by a script that predates revision delivery: "
                    "its stages read no revisions file; re-fire it to take a revision"
                )
            if has_note and len(str(note).encode("utf-8")) > VERBATIM_BYTE_CAP:
                raise LedgerRefused(f"note exceeds {VERBATIM_BYTE_CAP} bytes")
            who = _clean_sender(sender) or "em"
            if text:
                pipeline_run.append_revision(root, run_id, text, who, "verbatim")
            if has_note:
                pipeline_run.append_revision(root, run_id, str(note), who, "note")
            if outcome is not None:
                outcome["pipeline_revisions"] = pipeline_run.revisions_rel(run_id)
            return Ledger((), False), dir_, None
        if (
            named or all_of_pm_ask is not None or withheld_entries or credit_entries or red_entries or build_only
            or stop_after or retraction is not None or row_retraction is not None or accept_entries
        ):
            raise LedgerRefused(
                f"no ledger-bearing run {run_id!r} under {RECORD_DIR_ROOT}: emit it first "
                "(a held run takes only a verbatim revision)"
            )
        if not text:
            return Ledger((), False), dir_, None
        if not _parent_lists_run(root, run_id):
            raise LedgerRefused(
                f"unknown run {run_id!r}: no engine-minted parent record under scratch/warp lists it "
                "(check the run id); nothing was held"
            )
        from coordinator_core.atomic_append import append_line

        for n, queued_entry in enumerate(_inbox(dir_)):
            if queued_entry["text"].strip() == text:
                return _queued_ledger(dir_, False), dir_, f"Q{n + 1}"
        dir_.mkdir(parents=True, exist_ok=True)
        entry = {"text": text, "at": _now()}
        if _clean_sender(sender):
            entry["from"] = _clean_sender(sender)
        append_line(dir_ / INBOX_FILE, (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))
        declare_write(dir_ / INBOX_FILE)
        queued = _queued_ledger(dir_, False)
        return queued, dir_, f"Q{len(queued.items)}"
    if retraction is not None:
        from coordinator_core.ops.dispatch_emit import run_notes

        held = {e["id"]: e for e in _note_entries(dir_, run_id)}.get(retraction["note_id"])
        if held is None:
            raise LedgerRefused(f"run {run_id!r} holds no note {retraction['note_id']}")
        if held["retracted"] is not None:
            raise LedgerRefused(
                f"note {retraction['note_id']} of run {run_id!r} was already retracted at {held['retracted']['at']}"
            )
    if stop_after and (dir_ / "manifest.json").exists():
        raise LedgerRefused(f"the run has staged; stop_after={STOP_AFTER_PLAN} can no longer park it")
    if withheld_entries:
        outside = paths_outside_footprint(root, dir_, run.get("sizing_rel"), [e["path"] for e in withheld_entries])
        if outside:
            raise LedgerRefused(_footprint_route(run_id, outside))
    if named and not run.get("sizing_rel"):
        raise LedgerRefused("out_of_scope needs a bound sizing: the run has no register yet")
    claim_plan: Optional[tuple[str, Path]] = None
    if claim:
        if not text:
            raise LedgerRefused(f"{CLAIM_PARAM} needs a non-empty verbatim: the ruling is the row it mints")
        if named:
            raise LedgerRefused(f"{CLAIM_PARAM} cannot combine with out_of_scope: a ruling out adds no row")
        if not run.get("sizing_rel"):
            raise LedgerRefused(f"{CLAIM_PARAM} needs a run bound to a sizing")
        claim_plan = _claiming_plan(root, run["sizing_rel"])
    if row_retraction is not None:
        if not run.get("sizing_rel"):
            raise LedgerRefused("retract_row needs a run bound to a sizing: a queued revision holds no row yet")
        _, plan_path, _ = _sizing_plan(root, run["sizing_rel"])
        if plan_path is not None:
            from coordinator_core.ops.dispatch_emit import revise_absorb

            plan_text = plan_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
            _, planned = rr.plan_claims(revise_absorb._plan_fm(plan_text))
            if row_retraction in planned:
                raise LedgerRefused(f"retract_row {row_retraction} is claimed by the plan's register_claims: release the claim first")
    if build_only and not run.get("sizing_rel"):
        raise LedgerRefused(
            "build_only needs a run bound to a sizing: close the research with research.close and `sizing`"
        )
    if build_only:
        from coordinator_core.ops._research_sizing import ResearchSizingRefused, drop_research_block

        try:
            dropped = drop_research_block(root, run["sizing_rel"]) is not None
        except ResearchSizingRefused as exc:
            raise LedgerRefused(str(exc)) from exc
        if outcome is not None:
            outcome["research_dropped"] = dropped
    withhold ={"ids": named, "run_id": run_id, "verbatim": text} if named else None
    if all_of_pm_ask is not None:
        run = {**run, "all_of_pm_ask": bool(all_of_pm_ask)}
        _write_run(dir_, run)
        declare_write(dir_ / RUN_FILE)
    added: Optional[str] = None
    if stop_after:
        from coordinator_core.atomic_replace import atomic_write_bytes

        marker = {"stop_after": stop_after, "at": _now(), "from": _clean_sender(sender) or None}
        atomic_write_bytes(
            dir_ / STOP_AFTER_FILE, (json.dumps(marker, sort_keys=True) + "\n").encode("utf-8"), preserve_mode=False
        )
        declare_write(dir_ / STOP_AFTER_FILE)
    if has_note:
        from coordinator_core.atomic_append import append_line

        append_line(
            dir_ / NOTES_FILE,
            (json.dumps({"note": note.strip(), "at": _now()}, ensure_ascii=False) + "\n").encode("utf-8"),
        )
        declare_write(dir_ / NOTES_FILE)
    if retraction is not None:
        from coordinator_core.atomic_append import append_line

        append_line(
            dir_ / NOTES_FILE,
            run_notes.retraction_line(retraction["note_id"], retraction["reason"], _now(), _clean_sender(sender)),
        )
        declare_write(dir_ / NOTES_FILE)
    if withheld_entries:
        run_withhold.record(dir_, withheld_entries, _now())
    if accept_entries:
        run_accept_partial.record(dir_, accept_entries, _now())
    if credit_entries:
        run_credit.record(dir_, credit_entries, _now())
    if red_entries:
        from coordinator_core.atomic_append import append_line

        for e in red_entries:
            append_line(dir_ / OWNED_RED_FILE, (json.dumps({**e, "at": _now()}, ensure_ascii=False) + "\n").encode("utf-8"))
        declare_write(dir_ / OWNED_RED_FILE)
    if row_retraction is not None:
        _append_rows(root, run["sizing_rel"], [], retract_row=row_retraction)
    if named:
        _append_rows(root, run["sizing_rel"], [], withhold=withhold)
    elif text:
        at = _now()
        if run.get("sizing_rel"):
            reused: dict = {}
            rows = _append_rows(
                root, run["sizing_rel"], [(text, _revision_anchor(at, sender))], withhold=withhold, reuse=reused,
            )
            added = reused.get("id") or (str(rows[-1].get(rr.ROW_ID)) if rows else None)
            if claim_plan is not None and added:
                _claim_ruling(root, run["sizing_rel"], claim_plan, run_id, added)
                if outcome is not None:
                    outcome["claimed"] = [added]
        else:
            from coordinator_core.atomic_append import append_line

            who = _clean_sender(sender)
            queued_at = next((n for n, e in enumerate(_inbox(dir_)) if e["text"].strip() == text), None)
            if queued_at is None:
                entry = {"text": text, "at": at, **({"from": who} if who else {})}
                append_line(dir_ / INBOX_FILE, (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))
                declare_write(dir_ / INBOX_FILE)
                queued_at = len(_inbox(dir_)) - 1
            added = f"Q{queued_at + 1}"
    ledger = _write_view(root, dir_, run_id)
    if ledger is not None:
        declare_write(dir_ / LEDGER_VIEW_FILE)
    return ledger or Ledger((), bool(run.get("all_of_pm_ask"))), dir_, added


def _claiming_plan(root: Path, sizing_rel: str) -> tuple[str, Path]:
    """`(plan_rel, plan path)` of the plan the sizing's `plan:` edge names, when its
    `register_claims.sizing` is that sizing; LedgerRefused otherwise. File reads only."""
    from coordinator_core.roadmap.prep_gate import plan_frontmatter

    edge, plan, _ = _sizing_plan(root, sizing_rel)
    if plan is None or edge is None:
        raise LedgerRefused(f"{CLAIM_PARAM} needs {sizing_rel} to name a readable plan under docs/plans")
    claims = plan_frontmatter(plan.read_text(encoding="utf-8", errors="replace")).get("register_claims")
    if not isinstance(claims, dict) or claims.get("sizing") != sizing_rel or not isinstance(claims.get("rows"), list):
        raise LedgerRefused(f"{CLAIM_PARAM} needs {edge} register_claims to name {sizing_rel}")
    return edge, plan


def _claim_ruling(root: Path, sizing_rel: str, plan: tuple[str, Path], run_id: str, row_id: str) -> None:
    """Claim the freshly minted `row_id` on `plan` (rows + pin, one `locked_rmw`, CRLF kept) and add
    it to `ruled` on the plan's ledger-halt pointer. A failure names the minted row so
    `plan.claim_register_rows` can finish the claim."""
    import dataclasses

    from coordinator_core.locked_write import LockTimeout, locked_rmw
    from coordinator_core.ops.dispatch_emit import revise_absorb
    from coordinator_core.ops.dispatch_emit.ask_contract import (
        LEDGER_HALT_KIND, ledger_halt_path, read_ledger_halt, write_ledger_halt,
    )

    plan_rel, plan_path = plan
    stranded = f"row {row_id} was minted but not claimed on {plan_rel}: claim it with plan.claim_register_rows"
    try:
        register = rr.read_register(_sizing_path(root, sizing_rel).read_text(encoding="utf-8"))
        if register is None:
            raise LedgerRefused(f"{sizing_rel} carries no requirement_register; {stranded}")
        crlf = b"\r\n" in plan_path.read_bytes()

        def mutate(old_text: str) -> str:
            _, now_claimed = rr.plan_claims(revise_absorb._plan_fm(old_text.replace("\r\n", "\n")))
            if row_id in now_claimed:
                return old_text
            try:
                new_text = revise_absorb._claim_rows_text(old_text, [row_id], register)
            except ValueError as exc:
                raise MutateAbort(str(exc)) from exc
            return new_text.replace("\n", "\r\n") if crlf else new_text

        locked_rmw(plan_path, mutate, repo_root=root)
    except (OSError, LockTimeout, MutateAbort) as exc:
        raise LedgerRefused(f"{exc}; {stranded}") from exc
    declare_write(plan_path)
    pointer = ledger_halt_path(root, plan_rel)
    halt = read_ledger_halt(pointer)
    if halt is not None and halt.halted == LEDGER_HALT_KIND and halt.sizing_rel == sizing_rel and row_id not in halt.ruled:
        write_ledger_halt(pointer, dataclasses.replace(halt, ruled=(*halt.ruled, row_id)))
        declare_write(pointer)


def normalize_verdict(verdict: object) -> object:
    """A copy of `verdict` in which every `not_met` item whose non-empty `remaining` list holds only
    `POST_COMMIT_ACTS` (commit, sha_reply, sha_memo, baton_archive, plan_implemented, bug_close) reads
    `post_commit` with those acts; every other item and a shapeless verdict come back unchanged."""
    items = verdict.get("items") if isinstance(verdict, Mapping) else None
    if not isinstance(items, list):
        return verdict
    out = []
    for item in items:
        remaining = item.get("remaining") if isinstance(item, Mapping) else None
        if (
            isinstance(item, Mapping) and item.get("status") == "not_met"
            and isinstance(remaining, list) and remaining
            and all(isinstance(a, str) and a in POST_COMMIT_ACTS for a in remaining)
        ):
            item = {**item, "status": "post_commit", "post_commit_acts": list(remaining)}
        out.append(item)
    return {**verdict, "items": out}


HAND_BUILT_REASON = (
    "acceptance evidence hand-built its fixture; add a test through the production entry point "
    "the requirement names"
)


def _hand_built(row: Mapping) -> bool:
    return row.get(rr.JUDGE_EVIDENCE_ENTRY_POINT) == rr.EVIDENCE_HAND_BUILT


def _roots_read(row: Mapping) -> list[str]:
    value = row.get("root_read")
    if isinstance(value, str):
        value = [value]
    return [v for v in value or [] if isinstance(v, str) and v.strip()]


def _names_root(entry: str, owed: str) -> bool:
    """True when `entry` is the root `owed`, or opens with it and continues only past a
    non-path character (`<root> (read-only)`); `<root>-old` and `<root>/sub` are other paths."""
    from coordinator_core.win_portability import same_path

    if same_path(entry, owed):
        return True

    def fold(text: str) -> str:
        return os.path.normcase(text.strip()).replace("\\", "/")

    head, want = fold(entry), fold(owed).rstrip("/")
    if not want or not head.startswith(want):
        return False
    rest = head[len(want):]
    return not rest or not (rest[0].isalnum() or rest[0] in "_-./")


def _misrouted(row: Mapping, owed: str) -> Optional[str]:
    """The failure reason when `row` (met/not_met/post_commit) did not name the engine root `owed`."""
    read = _roots_read(row)
    if owed and any(_names_root(r, owed) for r in read):
        return None
    return f"judged at {', '.join(read) or 'no root named'}, owed the engine root {owed or '(unresolved)'}"


def judge(
    ledger: Ledger, verdict: object, routed: Optional[Mapping[str, str]] = None,
    gated: Optional[Iterable[str]] = None, operator: Optional[Mapping[str, str]] = None,
    repo_root: Optional[Path] = None, plan_text: Optional[str] = None,
    wave_reviews: Sequence[Mapping] = (), noted_paths: Sequence[str] = (),
    code_paths: Sequence[str] = (), review_verdicts: Optional[int] = None,
) -> dict:
    """`{"pass": bool, "failures": [{"id", "reason"}], "open": [{"id", "reason"}]}` for a checker
    `verdict` (`{"items": [{"id", "status", "essential"?, "withheld_by"?, "superseded_by"?,
    "post_commit_acts"?, "remaining"?, "root_read"?}]}`, read through `normalize_verdict`) against
    the live register. `open` lists every `not_met` row let through (never passed as met).

    `operator` maps the manifest's `execution_mode: operator` plan-row ids to the command a person
    runs. A `not_met` row whose `withheld_by` is one of them classifies `awaiting_operator`: it
    passes, is listed open as `awaiting_operator <id>` with `row` and `command` keys.
    A verdict id naming an operator plan row is ignored, like a gated one.

    `gated` is the stage manifest's gated chunk ids (`manifest.gated[].id`); None or empty withholds
    nothing. A verdict id naming a gated chunk is a plan row, not a requirement: it is ignored,
    not judged and never a failure. A `not_met` row whose `withheld_by` is in `gated` passes and is listed open
    (`withheld_by <id>`), whatever `all_of_pm_ask` or `essential` say; a `withheld_by` outside
    `gated` fails.

    `routed` maps an engine-routed item id to the engine root it was owed: a met/not_met/post_commit
    item whose `root_read` (string or list) does not name that root adds a failure; an entry may carry a
    trailing annotation, and a `not_met` row withheld by a gated chunk is exempt. It never
    removes one; None or empty is the cwd-only judgment.

    A row in `ledger.withheld` (ruled out of scope by a revise) is listed in `open` as
    `withheld by revision <id>` whether the verdict omits or judges it, and never fails.

    Every row needs exactly one judgment. `drifted` (lost or reinterpreted) always fails;
    `superseded` passes only when it names a later revision row; `not_met` fails when
    `all_of_pm_ask` is set or the row is essential (the default), else it passes listed open as
    `non-essential`; `post_commit` passes only when
    `post_commit_acts` is a non-empty list drawn from `POST_COMMIT_ACTS` (commit, sha_reply,
    sha_memo, baton_archive, plan_implemented, bug_close).

    A design-conformance row (anchor `design-registry:<id>`) is always essential: `essential: false`
    is ignored, `post_commit` fails, and `met` passes only when `design_conformance.evidence_refusal`
    (given `repo_root` and the run's `plan_text`) returns None; a `met` that fails it is judged
    `not_met`, so an empty `met_by` never stands as met. `wave_reviews` (the review wave's
    `{agent_type, sidecar_path, verdict}` returns) and `noted_paths` (files the revise notes name)
    are default evidence beside the item's own `met_by`.

    `code_paths` (the run's declared code paths) with `review_verdicts` 0 or None (unsupplied counts
    as 0) sets `review_floor_note` on the result and fails nothing. A non-None
    `review_verdicts` adds `receipt` ("review: N verdicts") to the result."""
    floor = review_floor.floor_reason(list(code_paths), review_verdicts or 0)
    verdict = normalize_verdict(verdict)
    rows = verdict.get("items") if isinstance(verdict, Mapping) else None
    if not isinstance(rows, list):
        return {
            "pass": False, "failures": [{"id": None, "reason": "verdict carries no items list"}],
            "open": [],
        }
    gated_ids = set(gated or ())
    operator = dict(operator or {})
    by_id: dict[str, Mapping] = {}
    failures: list[dict] = []
    if not ledger.items and not ledger.withheld:
        failures.append({
            "id": None,
            "reason": empty_register_refusal(ledger.sizing_rel) if ledger.sizing_rel else EMPTY_REGISTER_REASON,
        })
    open_rows: list[dict] = []
    for row in rows:
        rid = row.get("id") if isinstance(row, Mapping) else None
        if not isinstance(rid, str):
            continue
        if rid in by_id:
            failures.append({"id": rid, "reason": "judged twice"})
        by_id[rid] = row
    order = {item.id: n for n, item in enumerate(ledger.items)}
    for item in ledger.items:
        row = by_id.get(item.id)
        if item.text.lstrip().startswith(EM_CONTEXT_MARKER) and not _is_design(item.anchor):
            if row is None or row.get("status") != "met":
                open_rows.append({"id": item.id, "reason": "em-context"})
            continue
        if row is None:
            failures.append({"id": item.id, "reason": "unjudged (arrived after the check, or skipped)"})
            continue
        status = row.get("status")
        design = _is_design(item.anchor)
        if design and status == "met":
            why = _design_failure(item, row, repo_root, plan_text, wave_reviews, noted_paths)
            if why is not None:
                failures.append({"id": item.id, "reason": why})
                status = "not_met"
        if status == "met" and _hand_built(row):
            failures.append({"id": item.id, "reason": HAND_BUILT_REASON})
            status = "not_met"
        gated_open = status == "not_met" and isinstance(row.get("withheld_by"), str) and row["withheld_by"] in gated_ids
        if routed and item.id in routed and status in ("met", "not_met", "post_commit") and not gated_open:
            reason = _misrouted(row, routed[item.id])
            if reason is not None:
                failures.append({"id": item.id, "reason": reason})
        if status not in STATUSES:
            failures.append({"id": item.id, "reason": f"status {status!r} is not one of {STATUSES}"})
        elif status == "drifted":
            failures.append({"id": item.id, "reason": "drifted: lost or reinterpreted in transit"})
        elif status == "superseded":
            later = row.get("superseded_by")
            target = next((i for i in ledger.items if i.id == later), None)
            if target is None or target.origin != ORIGIN_REVISION or order[target.id] <= order[item.id]:
                failures.append({"id": item.id, "reason": f"superseded_by {later!r} names no later revision"})
        elif status == "post_commit":
            acts = row.get("post_commit_acts")
            if design:
                failures.append({"id": item.id, "reason": "a design row is met only on its evidence, never post_commit"})
            elif not isinstance(acts, list) or not acts:
                failures.append({"id": item.id, "reason": "post_commit names no post-commit act"})
            else:
                bad = next((a for a in acts if a not in POST_COMMIT_ACTS), None)
                if bad is not None:
                    failures.append({"id": item.id, "reason": f"post_commit act {bad!r} is not one of {POST_COMMIT_ACTS}"})
        elif status == "not_met":
            withheld = row.get("withheld_by")
            if isinstance(withheld, str) and withheld:
                if withheld in gated_ids:
                    open_rows.append({"id": item.id, "reason": f"withheld_by {withheld}"})
                elif withheld in operator:
                    open_rows.append({
                        "id": item.id, "reason": f"awaiting_operator {withheld}",
                        "row": withheld, "command": operator[withheld],
                    })
                else:
                    failures.append({
                        "id": item.id,
                        "reason": f"withheld_by {withheld} names no gated chunk of the run's manifest",
                    })
            elif design or ledger.all_of_pm_ask or row.get("essential", True) is not False:
                failures.append({
                    "id": item.id,
                    "reason": "not met" + (" (all_of_pm_ask: every row is mandatory)" if ledger.all_of_pm_ask else ""),
                })
            else:
                open_rows.append({"id": item.id, "reason": "non-essential"})
    withheld_ids = {w["id"] for w in ledger.withheld}
    unknown = sorted(set(by_id) - set(order) - withheld_ids - gated_ids - set(operator))
    failures.extend({"id": rid, "reason": NOT_A_ROW_REASON} for rid in unknown)
    open_rows.extend(dict(w) for w in ledger.withheld)
    result = {"pass": not failures, "failures": failures, "open": open_rows}
    if review_verdicts is not None:
        result["receipt"] = review_floor.receipt(review_verdicts)
    if floor is not None:
        result["review_floor_note"] = floor
    return result


def apply_verdict(repo_root: Path, sizing_rel: str, verdict: Mapping, *, run_id: str, sha: str) -> list[dict]:
    """Write a passed verdict onto the register once the commit `sha` landed: met and wired ->
    `met` with `<run_id>@<sha>` in met_by; met alone, or met whose `evidence_entry_point` is
    `hand-built-fixture` (no met_by entry) -> `partial`; superseded -> `waived` with the
    PM's superseding words as the ruling. A `post_commit` item whose acts are within
    `{commit, sha_reply, baton_archive, plan_implemented, bug_close}` is written as `met` (the landed commit
    is the act, and the landing performs the lifecycle acts); one naming
    `sha_memo` is `partial`, the engine sending no memo. A `not_met` item whose `remaining` is only
    post-commit acts reads as `post_commit` (`normalize_verdict`). Every judged row gets
    `last_progress_at`."""
    verdict = normalize_verdict(verdict)
    judged = {str(r.get("id")): r for r in verdict.get("items") or [] if isinstance(r, Mapping)}
    stamp = _now()

    def edit(rows: list[dict]) -> list[dict]:
        text_of = {str(r.get(rr.ROW_ID)): str(r.get(rr.ROW_TEXT) or "") for r in rows}
        for row in rows:
            got = judged.get(str(row.get(rr.ROW_ID)))
            if got is None or row.get(rr.ROW_STATUS) in (rr.STATUS_DEFERRED, rr.STATUS_WAIVED):
                continue
            status, wired = got.get("status"), got.get("wired") is True
            memo_owed = status == "post_commit" and "sha_memo" in (got.get("post_commit_acts") or [])
            if status == "post_commit" and not memo_owed:
                status = "met"
            if status == "met" or memo_owed:
                row[rr.ROW_WIRED] = wired
                if wired and not memo_owed and not _hand_built(got):
                    row[rr.ROW_STATUS] = rr.STATUS_MET
                    ref = f"{run_id}@{sha}"
                    met_by = [m for m in row.get(rr.ROW_MET_BY) or [] if isinstance(m, str)]
                    row[rr.ROW_MET_BY] = met_by + ([ref] if ref not in met_by else [])
                else:
                    row[rr.ROW_STATUS] = rr.STATUS_PARTIAL
            elif status == "superseded" and text_of.get(str(got.get("superseded_by"))):
                row[rr.ROW_STATUS] = rr.STATUS_WAIVED
                row[rr.ROW_RULING] = {
                    rr.RULING_SOURCE: "pm", rr.RULING_QUOTE: text_of[str(got["superseded_by"])],
                    rr.RULING_REF: str(got["superseded_by"]), rr.RULING_ON: stamp[:10],
                }
            else:
                continue
            row[rr.ROW_LAST_PROGRESS_AT] = stamp
        return rows

    return _mutate_register(Path(repo_root), sizing_rel, edit)


def close_dischargeable(item: Mapping) -> bool:
    """True when `item`, after `normalize_verdict`'s per-item rule, is `post_commit` with a
    non-empty act list drawn only from `CLOSE_DISCHARGED_ACTS`."""
    if not isinstance(item, Mapping):
        return False
    got = normalize_verdict({"items": [item]})["items"][0]
    acts = got.get("post_commit_acts")
    return (
        got.get("status") == "post_commit" and isinstance(acts, list) and bool(acts)
        and all(isinstance(a, str) and a in CLOSE_DISCHARGED_ACTS for a in acts)
    )


def post_commit_owed(verdict: Mapping, sha: str, discharged: Collection[str] = ()) -> list[dict]:
    """What the run still owes once `sha` exists: `{id, acts, sha}` per `post_commit` item that
    names `sha_memo`, which the engine never sends, and `{id, acts, sha}` (the undischarged
    lifecycle acts) per `post_commit` item naming a `LIFECYCLE_ACTS` member absent from `discharged`."""
    verdict = normalize_verdict(verdict)
    owed = []
    for r in verdict.get("items") or []:
        if not isinstance(r, Mapping) or r.get("status") != "post_commit":
            continue
        acts = [a for a in r.get("post_commit_acts") or [] if isinstance(a, str)]
        if "sha_memo" in acts:
            owed.append({"id": str(r.get("id")), "acts": acts, "sha": sha})
            continue
        undone = [a for a in acts if a in LIFECYCLE_ACTS and a not in discharged]
        if undone:
            owed.append({"id": str(r.get("id")), "acts": undone, "sha": sha})
    return owed


@register_op("workflow.revise")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "workflow.revise": params `run_id` (str, required), `verbatim` (str, a requirement), `note` (str, free
    text that adds no row; `notes` is its alias, a str or a list of str joined by newline, and
    sending both is refused), `from` (str, the peer session sending the revision; stamped on the
    row), `all_of_pm_ask` (bool), `out_of_scope` (list of register row ids, needs
    `verbatim`: each is deferred with the verbatim as
    its ruling, carried open on the commit, and replied as `withheld`), `withhold_paths` (list of
    `{path, reason}`: product paths the run's terminal commit leaves out, recorded to the run's
    `withhold.jsonl`; needs an emitted run, no sizing, adds no row; replied as `withhold_paths`),
    `accept_partial` (list of `{chunk, reason}`: PARTIAL chunks whose dependents the refire starts,
    recorded to the run's `accept_partial.jsonl`; needs an emitted run, no sizing, adds no row; replied
    as `accept_partial`),
    `credit_chunks` (list of `{chunk, evidence}`: stranded chunks the EM rules complete, recorded to
    the run's `credit.jsonl` for the re-fired terminal commit to re-grade; needs an emitted run, no
    sizing, adds no row; replied as `credit_chunks`), `retract_note` (`{note_id, reason}`: withdraws
    one of the run's notes as applied by hand, appended to the run's `notes.jsonl` and never deleted;
    needs an emitted run, no sizing, adds no row; replied as `retracted_note`), `retract_row`
    (`{row_id, reason}`: deletes one open, unclaimed revision row of the bound sizing's register;
    replied as `retracted_row`), `build_only` (bool: removes the bound sizing's
    `research` block; needs an emitted run bound to a sizing; adds no row; replied as `research_dropped`),
    `stop_after` (str, only "plan": records the park marker so `dispatch.ask_stage` parks the run after
    planning; needs an emitted run that has not staged; adds no row; replied as `stop_after`),
    `owned_red` (list of `{nodeid, owner_run_id, reason?}`: failing tests another run owns, recorded to
    the run's `owned_red.jsonl`; needs an emitted run, adds no row; replied as `owned_red`),
    `claim` (bool, needs `verbatim` and a run bound to a sizing whose plan claims it: the minted row is
    claimed on the plan with its pin and added to `ruled` on the plan's ledger-halt pointer in the
    same call; replied as `claimed: [id]`).

    Appends a stamped revision row to the run's requirements (its sizing's requirement_register),
    which every later stage of the run reads at its start. Replies `{ok, run_id, item, items,
    all_of_pm_ask, sizing, ledger, run_live}` plus `warning` and `pin_drift` (the drifted row ids)
    when the sizing's plan pinned rows the register no longer matches, and `warning` plus
    `unclaimed_rows` when its register_claims omit register rows; `item` is the new row id
    (`Q<n>` while a raw ask's sizing is not yet minted), `run_live` is false once the run's commit
    token is redeemed or absent, or the run holds a `halted` marker (nothing will read it: the
    marker is stamped `revised_at` and the reply names the fresh refire). On an emitted research pipeline
    run (`pipeline-` id) only `verbatim` and `note` are taken: they append to the run's revisions file and
    reply `{ok, run_id, delivered, revisions, run_live: None, item: None, note}`; a finished run, a
    pre-revision script, or any other parameter replies an error. A refused request replies `{"error": str}`.
    """
    if repo_root is None:
        return {"error": "workflow.revise requires repo_root"}
    if "notes" in params:
        if "note" in params:
            return {"error": "workflow.revise takes `note`; `notes` is its alias, send one of them"}
        notes = params["notes"]
        params = {k: v for k, v in params.items() if k != "notes"}
        if isinstance(notes, (list, tuple)) and all(isinstance(n, str) for n in notes):
            notes = "\n".join(notes)
        params["note"] = notes
    refusal = validate_params("workflow.revise", params, _PARAMS, strict=True)
    if refusal is not None:
        return refusal
    root = main_worktree_root(Path(repo_root))
    outcome: dict = {}
    try:
        ledger, dir_, added = revise(
            root, params["run_id"], build_only=bool(params.get("build_only")), outcome=outcome, verbatim=params.get("verbatim"), all_of_pm_ask=params.get("all_of_pm_ask"),
            out_of_scope=params.get("out_of_scope") or (), note=params.get("note"),
            sender=params.get("from"), withhold_paths=params.get("withhold_paths") or (),
            credit_chunks=params.get("credit_chunks") or (), stop_after=params.get("stop_after"),
            retract_note=params.get("retract_note"), claim=bool(params.get(CLAIM_PARAM)),
            owned_red=params.get("owned_red") or (), add_paths=params.get("add_paths") or (),
            retract_row=params.get("retract_row"), widened=params.get("widened") or (),
            accept_partial=params.get("accept_partial") or (),
        )
    except LedgerRefused as exc:
        return {"error": str(exc)}
    if "pipeline_revisions" in outcome:
        return _mutation_reply(
            (f"revision appended to {outcome['pipeline_revisions']}",), (), params["run_id"],
            run_id=params["run_id"], delivered=True, revisions=outcome["pipeline_revisions"],
            run_live=None, item=None,
            note="appended to the run's revisions file: if the run is still running, each stage reads it "
                 "at its next stage start; a stage already running does not see it.",
        )
    if read_run(dir_) is None:
        held_refused = (
            ({"input": "widened", "reason": "run not emitted yet: no run directory to record the widen in"},)
            if params.get("widened") else ()
        )
        return _mutation_reply(
            (f"row {added} held for emit",) if added else (), held_refused, params["run_id"],
            run_id=params["run_id"], item=added, items=len(ledger.items),
            all_of_pm_ask=False, sizing=None, ledger=None, run_live=False, held=True,
            note="run not emitted yet: the revision is held and folds into the ledger at emit.",
        )
    from coordinator_core.ops.dispatch_emit import halted_run, run_token

    widen_refused: list[dict] = []
    halted_run.mark_revised(dir_, _now())
    halted = halted_run.revised_halt(dir_) is not None
    live = run_token.is_live(root, params["run_id"]) and not halted
    out = {
        "ok": True,
        "run_id": params["run_id"],
        "item": added,
        "items": len(ledger.items),
        "all_of_pm_ask": ledger.all_of_pm_ask,
        "sizing": ledger.sizing_rel,
        "ledger": (dir_ / LEDGER_VIEW_FILE).relative_to(root.resolve()).as_posix(),
        "run_live": live,
    }
    if "research_dropped" in outcome:
        out["research_dropped"] = outcome["research_dropped"]
    if "claimed" in outcome:
        out["claimed"] = outcome["claimed"]
    if params.get("stop_after"):
        out["stop_after"] = params["stop_after"]
    if params.get("widened"):
        from coordinator_core.ops.dispatch_emit import in_run_widen

        recorded = in_run_widen.record_widens(dir_, params["widened"])
        if recorded:
            out["widened"] = recorded
        if recorded < len(params["widened"]):
            widen_refused.append({
                "input": "widened",
                "reason": f"{len(params['widened']) - recorded} item(s) not recorded: each must be {{row, path}} "
                          "with a repo-relative path outside the denied prefixes",
            })
    if params.get("withhold_paths"):
        from coordinator_core.ops.dispatch_emit import run_withhold

        out["withhold_paths"] = run_withhold.validate(params["withhold_paths"])
    if params.get("accept_partial"):
        from coordinator_core.ops.dispatch_emit import run_accept_partial

        out["accept_partial"] = run_accept_partial.validate(params["accept_partial"])
    if params.get("credit_chunks"):
        from coordinator_core.ops.dispatch_emit import run_credit

        out["credit_chunks"] = run_credit.validate(params["credit_chunks"])
    if params.get("owned_red"):
        out["owned_red"] = validate_owned_red(params["owned_red"])
    if params.get("retract_note"):
        out["retracted_note"] = {"run_id": params["run_id"], "note_id": params["retract_note"]["note_id"].strip()}
    if params.get("retract_row"):
        out["retracted_row"] = {"run_id": params["run_id"], "row_id": params["retract_row"]["row_id"].strip()}
    named = set(params.get("out_of_scope") or ())
    if named:
        out["withheld"] = [w["id"] for w in ledger.withheld if w["id"] in named]
    plan_rel, drifted = pinned_plan_drift(root, ledger.sizing_rel)
    warnings = []
    if drifted:
        out["pin_drift"] = drifted
        warnings.append(
            f"{plan_rel} pinned register row(s) {', '.join(drifted)} that this register no longer matches: "
            "the plan will be pulled on register-pin-drift."
        )
    unclaimed_result = _unclaimed_rows(root, ledger.sizing_rel)
    unclaimed_plan, unclaimed = unclaimed_result
    if unclaimed:
        out["unclaimed_rows"] = unclaimed
        warnings.append(
            f"{unclaimed_plan} omits register row(s) {', '.join(unclaimed)} in register_claims.rows"
            + (": the refire's ledger re-plan leg adds rows serving them." if not live else ".")
        )
    refused: list[dict] = list(widen_refused)
    if params.get("build_only") and not outcome.get("research_dropped"):
        refused.append({"input": "build_only", "reason": "the bound sizing holds no research block to remove"})
    if warnings:
        out["warning"] = " ".join(warnings)
    if not live:
        out.update(_refire(ledger.sizing_rel, root, unclaimed_result, halted=halted))
    elif verify_staged(dir_):
        out["lands_on_refire"] = True
        out.update(_refire(ledger.sizing_rel, root, unclaimed_result))
        out["note"] = (
            "run live, verify already staged: the run's late-row read has passed, so no leg of this run "
            "executes or reads this revision; " + out["note"].replace("run finished: ", "", 1) + " Fire next.call to land it."
        )
    else:
        if _rows_briefed(dir_):
            out["briefed"] = True
        out["note"] = (
            ("run live, rows already briefed: no row brief reads this revision; " if out.get("briefed") else "run live: ")
            + "the pre-verify late-row leg re-plans it once and runs one bounded executor leg briefed with only "
            "that row, then verify and the ledger-fix leg judge it in this run, with no halt and no second fire."
        )
    applied = [f"row {added}"] if added else []
    applied += [k for k in _APPLIED_KEYS if out.get(k)]
    applied += [k for k in ("note", "all_of_pm_ask") if params.get(k) is not None and params.get(k) != ""]
    return _mutation_reply(tuple(applied), tuple(refused), params["run_id"], **out)


_APPLIED_KEYS = ("widened", "research_dropped", "claimed", "stop_after", "withhold_paths", "accept_partial", "credit_chunks", "owned_red", "retracted_note", "withheld")


def _mutation_reply(applied: tuple, refused: tuple, target: str, **fields) -> dict:
    """The reply of a mutating call: `ok` is true only when `applied` is non-empty; a call that
    applied nothing replies `ok:false` with its `refused` rows and an `error`."""
    from coordinator_core.ops.dispatch_emit.run_truth_contract import MutationReply

    reply = MutationReply(applied=applied, refused=refused, target=target)
    out = {**fields, "ok": reply.ok, "applied": list(reply.applied), "refused": list(reply.refused), "target": reply.target}
    if not reply.ok:
        out["error"] = "nothing applied: " + ("; ".join(str(r) for r in refused) or "the revision carried no consumable input")
    return out


def late_rows(repo_root: Path, run_id: str) -> list[str]:
    """Ids of the run's live ledger items its staged manifest holds no work for (not in
    `manifest.json` `ledger_item_ids`, not withheld), in arrival order. [] with no readable manifest.
    Two file reads, no spawn; LedgerRefused on an unsafe run_id or unreadable sizing."""
    dir_ = run_dir(repo_root, run_id)
    try:
        staged = json.loads((dir_ / "manifest.json").read_text(encoding="utf-8")).get("ledger_item_ids")
    except (OSError, ValueError, AttributeError):
        return []
    ledger = read_ledger(repo_root, dir_)
    if ledger is None:
        return []
    held = {str(i) for i in staged or ()} | {w["id"] for w in ledger.withheld}
    return [i.id for i in ledger.items if i.id not in held]


def verify_staged(dir_: Path) -> bool:
    """True once the run's verify prompt (`prompts/verify.md`, stage_as="verify") is on disk: a row
    revised after that is judged by no verify leg of this run."""
    return (dir_ / "prompts" / "verify.md").is_file()


@register_op(OP_LATE_ROWS)
def _late_rows_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "workflow.late_rows" (COMPUTE_ONLY): params `run_id`. Replies `{late_rows: [id]}`;
    a refused request replies `{"error": str}`."""
    if repo_root is None:
        return {"error": f"{OP_LATE_ROWS} requires repo_root"}
    refusal = validate_params(OP_LATE_ROWS, params, (Field("run_id", "nonempty_str", required=True),), strict=True)
    if refusal is not None:
        return refusal
    try:
        return {LATE_ROWS_FIELD: late_rows(main_worktree_root(Path(repo_root)), params["run_id"])}
    except LedgerRefused as exc:
        return {"error": str(exc)}


def _rows_briefed(dir_: Path) -> bool:
    """True once the run's stage manifest lists rows with composed briefs: later revisions reach no brief."""
    try:
        rows = json.loads((dir_ / "manifest.json").read_text(encoding="utf-8")).get("rows")
    except (OSError, ValueError, AttributeError):
        return False
    return any(isinstance(r, dict) and r.get("brief_path") for r in rows or ())


def _sizing_plan(root: Path, sizing_rel: Optional[str]) -> tuple[Optional[str], Optional[Path], str]:
    """`(plan_rel, plan path, sizing text)` for the sizing's `plan:` edge; `(None, None, "")` when
    the sizing or its plan is unreadable. File reads only."""
    if not sizing_rel:
        return None, None, ""
    import yaml

    try:
        sizing_text = _sizing_path(root, sizing_rel).read_text(encoding="utf-8")
        sizing = yaml.safe_load(sizing_text)
    except (LedgerRefused, OSError, yaml.YAMLError):
        return None, None, ""
    edge = sizing.get("plan") if isinstance(sizing, dict) else None
    if not (isinstance(edge, str) and edge.strip()):
        return None, None, ""
    plan = contained_path(Path(root) / edge, [Path(root) / "docs" / "plans"])
    if plan is None or not plan.is_file():
        return None, None, ""
    return edge, plan, sizing_text


def pinned_plan_drift(root: Path, sizing_rel: Optional[str]) -> tuple[Optional[str], list[str]]:
    """`(plan_rel, ids)` of the register rows the sizing's plan pinned (`register_claims.pins`)
    whose text no longer matches; `(None, [])` when there is no readable plan or nothing drifted."""
    edge, plan, _ = _sizing_plan(root, sizing_rel)
    if plan is None:
        return None, []
    from coordinator_core.frontmatter.primitives import split_frontmatter

    split = split_frontmatter(plan.read_text(encoding="utf-8", errors="replace"))
    drift = rr.plan_pin_drift(split.fm_text, root) if split is not None else []
    return (edge, [d["id"] for d in drift]) if drift else (None, [])


def _unclaimed_rows(root: Path, sizing_rel: Optional[str]) -> tuple[Optional[str], list[str]]:
    """`(plan_rel, register row ids the plan's register_claims omit)` for the sizing's `plan:` edge;
    `(None, [])` when the sizing has no readable plan claiming it. File reads only."""
    edge, plan, sizing_text = _sizing_plan(root, sizing_rel)
    if plan is None:
        return None, []
    from coordinator_core.ops.dispatch_emit.ask_stage import scaffold_markers
    from coordinator_core.roadmap.prep_gate import plan_frontmatter

    plan_text = plan.read_text(encoding="utf-8", errors="replace")
    if scaffold_markers(plan_text):
        return None, []
    claims = plan_frontmatter(plan_text).get("register_claims")
    if not isinstance(claims, dict) or claims.get("sizing") != sizing_rel or not isinstance(claims.get("rows"), list):
        return None, []
    register = rr.read_register(sizing_text)
    claimed = {str(r) for r in claims["rows"]}
    missing = rr.unclaimed_ids(register.rows, claimed, exclude=ruled_out_ids(register.rows)) if register else []
    return (edge, missing) if missing else (None, [])


def _refire(
    sizing_rel: Optional[str], root: Optional[Path] = None,
    unclaimed: Optional[tuple[Optional[str], list[str]]] = None, halted: bool = False,
) -> dict:
    """`{next, note}` for a revision no stage will read: the exact submit call that starts a run which
    will. `halted` words the note for a halted run, whose resume would replay its stages. When the sizing's plan omits
    register rows, `next.replans` names them: the refire's ledger re-plan leg claims them (M+ arm only)."""
    from coordinator_core.warp_fire import fire_command

    plan_rel, missing = unclaimed or (_unclaimed_rows(root, sizing_rel) if root is not None else (None, []))
    if plan_rel:
        state = "run halted" if halted else "run finished"
        rows = ", ".join(missing)
        return {
            "next": {"kind": "refire", "call": fire_command(sizing_rel), "replans": {"plan": plan_rel, "rows": missing}},
            "note": (
                f"{state}: {plan_rel} omits register row(s) {rows}; the refire's ledger re-plan leg adds rows "
                "serving them and claims them before staging, on any arm. Fire next.call."
            ),
        }
    return {
        "next": {"kind": "refire", "call": fire_command(sizing_rel)},
        "note": (
            "run halted: resumeFromRunId replays its stages and never reads this revision; "
            "a fresh refire with next.call reads it."
            if halted else "run finished: nothing reads this revision until a refire."
        ),
    }
