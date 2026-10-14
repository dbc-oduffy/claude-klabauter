"""workflow.revise: a warp `--ask` run's PM requirements, held as rows of its sizing's
`requirement_register` -- the one list of what the PM asked, which `sizing-assemble --register`,
plan `register_claims` and the terminal judge already read.

A run is ledger-bearing once the emit writes `RECORD_DIR_ROOT/<run_id>/run.json` (`start`):
`{sizing_rel, all_of_pm_ask}`, the run's binding and flag, never a requirement. `bind` seeds the
bound sizing's register with the PM's words (`pm_verbatims`, else a PM-verbatim intent) and the
raw ask (`ask`, or `relayed` when an EM relayed it: never the PM's words), one row each, and drains `revisions.jsonl` -- the transport for a revision sent before a
raw ask's sizing exists -- into rows. Once bound, `workflow.revise` appends its row straight onto
the register. A row's `anchor` carries its origin (`pm_verbatims`, `ask`, `revision@<UTC>`); row
order is arrival order. `requirements.md` beside run.json is the view every stage prompt reads,
rewritten on each write. Register writes go through `locked_rmw` and the sizing schema. File I/O
only: no git, no subprocess, so the op holds the 500ms bar.

`judge` is the one pass rule. The run's final checker applies it in its prompt and
`dispatch.terminal_commit` re-applies it against the live register, so a revision that lands after
the check, or a verdict that skips a row, cannot reach the product commit. It is a gate beside the
delivery verifier, the terminal judge and the falsifier, never a stand-in for any of them.
`apply_verdict` writes the passed verdict back onto the rows once the commit has landed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Sequence

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, record_dir
from coordinator_core.ops.dispatch_emit.request_validation import Field, validate_params

GENERATES = []

RUN_FILE = "run.json"
INBOX_FILE = "revisions.jsonl"
LEDGER_VIEW_FILE = "requirements.md"
ORIGIN_PM_VERBATIMS, ORIGIN_ASK, ORIGIN_REVISION = "pm_verbatims", "ask", "revision"
ORIGIN_RELAYED = "relayed"
STATUSES = ("met", "not_met", "drifted", "superseded")
VERBATIM_BYTE_CAP = 16 * 1024
#: Every seeded row's surface: a PM sentence names no entry kind, so the checker judges `wired`
#: as reachable from the product at all.
ROW_SURFACE = "internal"

_PARAMS = (
    Field("run_id", "nonempty_str", required=True),
    Field("verbatim", "str"),
    Field("all_of_pm_ask", "bool"),
)
_SIZING_SCHEMA = Path(__file__).resolve().parents[2] / "frontmatter" / "schemas" / "sizing-object.schema.json"


class LedgerRefused(ValueError):
    """The run or the revision cannot be admitted; the message names the cause."""


@dataclass(frozen=True)
class Item:
    id: str
    text: str
    origin: str
    at: str

    def to_json(self) -> dict:
        return {"id": self.id, "text": self.text, "origin": self.origin, "at": self.at}


@dataclass(frozen=True)
class Ledger:
    """The rows a run is judged against: every register row not already ruled deferred/waived."""

    items: tuple[Item, ...]
    all_of_pm_ask: bool
    sizing_rel: Optional[str] = None

    def to_json(self) -> dict:
        return {"items": [i.to_json() for i in self.items], "all_of_pm_ask": self.all_of_pm_ask}


def run_dir(repo_root: Path, run_id: str) -> Path:
    """The run's record directory (`ask_contract.record_dir`); LedgerRefused on an unsafe id or an escape."""
    try:
        return record_dir(Path(repo_root), run_id)
    except ValueError as exc:
        raise LedgerRefused(str(exc)) from exc


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _origin(anchor: object) -> tuple[str, str]:
    """(origin, at) from a row anchor; a row from a source document has origin ''."""
    text = anchor if isinstance(anchor, str) else ""
    if text.startswith(ORIGIN_REVISION + "@"):
        return ORIGIN_REVISION, text[len(ORIGIN_REVISION) + 1:]
    if text in (ORIGIN_PM_VERBATIMS, ORIGIN_ASK, ORIGIN_RELAYED):
        return text, ""
    return "", ""


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
    if found is None or not found.is_file():
        raise LedgerRefused(f"sizing {sizing_rel!r} is not a file under the sizings home")
    return found


def _ledger_of(rows: Sequence[Mapping], all_of: bool, sizing_rel: Optional[str]) -> Ledger:
    items = []
    for row in rows:
        if row.get(rr.ROW_STATUS) in (rr.STATUS_DEFERRED, rr.STATUS_WAIVED):
            continue
        origin, at = _origin(row.get(rr.ROW_ANCHOR))
        items.append(Item(str(row.get(rr.ROW_ID)), str(row.get(rr.ROW_TEXT) or ""), origin, at))
    return Ledger(tuple(items), all_of, sizing_rel)


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


def _mutate_register(root: Path, sizing_rel: str, edit) -> list[dict]:
    """Apply `edit(rows) -> rows | None` to the sizing's register under the file lock; None or an
    unchanged list writes nothing. Returns the rows on disk afterwards."""
    from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw

    path = _sizing_path(root, sizing_rel)
    out: dict = {"rows": []}

    def mutate(old: str) -> str:
        register = rr.read_register(old)
        rows = [dict(r) for r in register.rows] if register is not None else []
        raw = dict(register.raw) if register is not None else {}
        new_rows = edit([dict(r) for r in rows])
        out["rows"] = rows
        if new_rows is None or new_rows == rows:
            return old
        sources = [s for s in raw.get(rr.SOURCES_KEY) or [] if isinstance(s, dict)]
        if not any(s.get("doc") == sizing_rel for s in sources):
            sources.append({"doc": sizing_rel, "ref": "pm_verbatims, ask, workflow.revise"})
        block = {**raw, rr.SOURCES_KEY: sources, rr.ROWS_KEY: new_rows}
        crlf = "\r\n" in old
        new = _validated(rr.write_register_text(old.replace("\r\n", "\n"), block, datetime.now(timezone.utc)))
        out["rows"] = new_rows
        return new.replace("\n", "\r\n") if crlf else new

    try:
        locked_rmw(path, mutate, repo_root=Path(root))
    except MutateAbort as exc:
        raise LedgerRefused(str(exc.args[0]) if exc.args else "register write refused") from exc
    except LockTimeout as exc:
        raise LedgerRefused(f"timed out waiting for the lock on {sizing_rel}: {exc}") from exc
    return out["rows"]


def _append_rows(root: Path, sizing_rel: str, entries: Sequence[tuple[str, str]]) -> list[dict]:
    """Append one open row per `(text, anchor)`; a PM-word or ask row already on the register
    (same verbatim text) is not added twice, a revision always is."""

    def edit(rows: list[dict]) -> list[dict]:
        have = {str(r.get(rr.ROW_TEXT) or "").strip() for r in rows}
        for text, anchor in entries:
            t = text.strip()
            if not t or (anchor in (ORIGIN_PM_VERBATIMS, ORIGIN_ASK, ORIGIN_RELAYED) and t in have):
                continue
            have.add(t)
            rows.append({
                rr.ROW_ID: _next_id(rows), rr.ROW_TEXT: t, rr.ROW_ANCHOR: anchor,
                rr.ROW_SURFACE: ROW_SURFACE, rr.ROW_STATUS: rr.STATUS_OPEN, rr.ROW_CLAIMED_BY: [],
            })
        return rows

    return _mutate_register(root, sizing_rel, edit)


def render(ledger: Ledger, run_id: str) -> str:
    where = f"the requirement_register of {ledger.sizing_rel}" if ledger.sizing_rel else "this run (sizing not yet bound)"
    lines = [
        f"# Requirements -- run {run_id}",
        "",
        f"Every row of {where} still to deliver: the PM's words, the ask (origin `relayed`: an EM's relay, "
        "not the PM's words; a `pm_verbatims` row outranks it), and each revision the EM "
        "sent while the run was in flight, in the order they arrived. A revision governs over an "
        "earlier row it changes.",
        "",
        "All of the PM's ask is mandatory (all_of_pm_ask): " + ("yes" if ledger.all_of_pm_ask else "no"),
        "",
    ]
    for item in ledger.items:
        tag = ", ".join(p for p in (item.origin or "source document", item.at) if p)
        lines.append(f"## {item.id} ({tag})")
        lines.append("")
        lines.extend("> " + ln if ln else ">" for ln in item.text.strip().splitlines())
        lines.append("")
    return "\n".join(lines)


def read_ledger(repo_root: Path, dir_: Path) -> Optional[Ledger]:
    """The run's requirements, or None when the run was emitted without a ledger. Before the
    sizing is bound, the queued revisions are the only items."""
    run = read_run(dir_)
    if run is None:
        return None
    all_of = bool(run.get("all_of_pm_ask"))
    sizing_rel = run.get("sizing_rel") if isinstance(run.get("sizing_rel"), str) else None
    if not sizing_rel:
        queued = _inbox(dir_)
        return Ledger(
            tuple(Item(f"Q{n + 1}", e["text"].strip(), ORIGIN_REVISION, str(e.get("at") or "")) for n, e in enumerate(queued)),
            all_of,
        )
    try:
        text = _sizing_path(repo_root, sizing_rel).read_text(encoding="utf-8")
    except (LedgerRefused, OSError):
        return Ledger((), all_of, sizing_rel)
    register = rr.read_register(text)
    return _ledger_of(register.rows if register is not None else [], all_of, sizing_rel)


def _write_view(repo_root: Path, dir_: Path, run_id: str) -> Optional[Ledger]:
    from coordinator_core.atomic_replace import atomic_write_bytes

    ledger = read_ledger(repo_root, dir_)
    if ledger is not None:
        atomic_write_bytes(dir_ / LEDGER_VIEW_FILE, render(ledger, run_id).encode("utf-8"), preserve_mode=False)
    return ledger


def start(repo_root: Path, run_id: str, *, all_of_pm_ask: bool = False) -> Path:
    """Make the run ledger-bearing (emit time). Returns the run dir."""
    dir_ = run_dir(repo_root, run_id)
    _write_run(dir_, {"sizing_rel": None, "all_of_pm_ask": bool(all_of_pm_ask)})
    _write_view(repo_root, dir_, run_id)
    return dir_


def bind(
    repo_root: Path, run_id: str, sizing_rel: str, *, ask_text: Optional[str] = None,
    relayed_text: Optional[str] = None,
) -> Optional[Ledger]:
    """Bind a ledger-bearing run to its sizing and seed that sizing's register: the PM's words,
    the raw ask, then every queued revision. Idempotent; a run without run.json is left alone
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
    entries = [(t, ORIGIN_PM_VERBATIMS) for t in pm_words(sizing if isinstance(sizing, dict) else {})]
    if ask_text and ask_text.strip():
        entries.append((ask_text, ORIGIN_ASK))
    if relayed_text and relayed_text.strip():
        entries.append((relayed_text, ORIGIN_RELAYED))
    queued = _inbox(dir_)
    entries.extend((e["text"], f"{ORIGIN_REVISION}@{e.get('at') or _now()}") for e in queued)
    if entries:
        _append_rows(root, sizing_rel, entries)
    if not bound:
        _write_run(dir_, {**run, "sizing_rel": sizing_rel})
    if queued:
        (dir_ / INBOX_FILE).unlink(missing_ok=True)
    return _write_view(root, dir_, run_id)


def revise(
    repo_root: Path, run_id: str, *, verbatim: Optional[str] = None, all_of_pm_ask: Optional[bool] = None
) -> tuple[Ledger, Path, Optional[str]]:
    """Append the EM's revision (and/or set the flag) on an emitted run: a register row when the
    run's sizing is bound, else a queued revision `bind` folds in. Returns (ledger, run dir, the
    new row id or None)."""
    text = verbatim.strip() if isinstance(verbatim, str) else ""
    if not text and all_of_pm_ask is None:
        raise LedgerRefused("workflow.revise needs a non-empty verbatim, all_of_pm_ask, or both")
    if len(text.encode("utf-8")) > VERBATIM_BYTE_CAP:
        raise LedgerRefused(f"verbatim exceeds {VERBATIM_BYTE_CAP} bytes")
    root = Path(repo_root)
    dir_ = run_dir(root, run_id)
    run = read_run(dir_)
    if run is None:
        raise LedgerRefused(f"no ledger-bearing run {run_id!r} under {RECORD_DIR_ROOT}: emit it first")
    if all_of_pm_ask is not None:
        run = {**run, "all_of_pm_ask": bool(all_of_pm_ask)}
        _write_run(dir_, run)
    added: Optional[str] = None
    if text:
        at = _now()
        if run.get("sizing_rel"):
            rows = _append_rows(root, run["sizing_rel"], [(text, f"{ORIGIN_REVISION}@{at}")])
            added = str(rows[-1].get(rr.ROW_ID)) if rows else None
        else:
            from coordinator_core.atomic_append import append_line

            append_line(dir_ / INBOX_FILE, (json.dumps({"text": text, "at": at}, ensure_ascii=False) + "\n").encode("utf-8"))
            added = f"Q{len(_inbox(dir_))}"
    ledger = _write_view(root, dir_, run_id)
    return ledger or Ledger((), bool(run.get("all_of_pm_ask"))), dir_, added


def judge(ledger: Ledger, verdict: object) -> dict:
    """`{"pass": bool, "failures": [{"id", "reason"}]}` for a checker `verdict`
    (`{"items": [{"id", "status", "essential"?, "superseded_by"?}]}`) against the live register.

    Every row needs exactly one judgment. `drifted` (lost or reinterpreted) always fails;
    `superseded` passes only when it names a later revision row; `not_met` fails when
    `all_of_pm_ask` is set or the row is essential (the default)."""
    rows = verdict.get("items") if isinstance(verdict, Mapping) else None
    if not isinstance(rows, list):
        return {"pass": False, "failures": [{"id": None, "reason": "verdict carries no items list"}]}
    by_id: dict[str, Mapping] = {}
    failures: list[dict] = []
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
        if row is None:
            failures.append({"id": item.id, "reason": "unjudged (arrived after the check, or skipped)"})
            continue
        status = row.get("status")
        if status not in STATUSES:
            failures.append({"id": item.id, "reason": f"status {status!r} is not one of {STATUSES}"})
        elif status == "drifted":
            failures.append({"id": item.id, "reason": "drifted: lost or reinterpreted in transit"})
        elif status == "superseded":
            later = row.get("superseded_by")
            target = next((i for i in ledger.items if i.id == later), None)
            if target is None or target.origin != ORIGIN_REVISION or order[target.id] <= order[item.id]:
                failures.append({"id": item.id, "reason": f"superseded_by {later!r} names no later revision"})
        elif status == "not_met" and (ledger.all_of_pm_ask or row.get("essential", True) is not False):
            failures.append({
                "id": item.id,
                "reason": "not met" + (" (all_of_pm_ask: every row is mandatory)" if ledger.all_of_pm_ask else ""),
            })
    unknown = sorted(set(by_id) - set(order))
    failures.extend({"id": rid, "reason": "names no requirement row"} for rid in unknown)
    return {"pass": not failures, "failures": failures}


def apply_verdict(repo_root: Path, sizing_rel: str, verdict: Mapping, *, run_id: str, sha: str) -> list[dict]:
    """Write a passed verdict onto the register once the commit `sha` landed: met and wired ->
    `met` with `<run_id>@<sha>` in met_by; met alone -> `partial`; superseded -> `waived` with the
    PM's superseding words as the ruling. Every judged row gets `last_progress_at`."""
    judged = {str(r.get("id")): r for r in verdict.get("items") or [] if isinstance(r, Mapping)}
    stamp = _now()

    def edit(rows: list[dict]) -> list[dict]:
        text_of = {str(r.get(rr.ROW_ID)): str(r.get(rr.ROW_TEXT) or "") for r in rows}
        for row in rows:
            got = judged.get(str(row.get(rr.ROW_ID)))
            if got is None or row.get(rr.ROW_STATUS) in (rr.STATUS_DEFERRED, rr.STATUS_WAIVED):
                continue
            status, wired = got.get("status"), got.get("wired") is True
            if status == "met":
                row[rr.ROW_WIRED] = wired
                if wired:
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


@register_op("workflow.revise")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "workflow.revise": params `run_id` (str, required), `verbatim` (str), `all_of_pm_ask` (bool).

    Appends a stamped revision row to the run's requirements (its sizing's requirement_register),
    which every later stage of the run reads at its start. Replies `{ok, run_id, item, items,
    all_of_pm_ask, sizing, ledger, run_live}`; `item` is the new row id (`Q<n>` while a raw ask's
    sizing is not yet minted), `run_live` is false once the run's commit token is redeemed or absent
    (nothing will read it). A refused request replies `{"error": str}`.
    """
    if repo_root is None:
        return {"error": "workflow.revise requires repo_root"}
    refusal = validate_params("workflow.revise", params, _PARAMS)
    if refusal is not None:
        return refusal
    root = main_worktree_root(Path(repo_root))
    try:
        ledger, dir_, added = revise(
            root, params["run_id"], verbatim=params.get("verbatim"), all_of_pm_ask=params.get("all_of_pm_ask")
        )
    except LedgerRefused as exc:
        return {"error": str(exc)}
    from coordinator_core.ops.dispatch_emit import run_token

    return {
        "ok": True,
        "run_id": params["run_id"],
        "item": added,
        "items": len(ledger.items),
        "all_of_pm_ask": ledger.all_of_pm_ask,
        "sizing": ledger.sizing_rel,
        "ledger": (dir_ / LEDGER_VIEW_FILE).relative_to(root.resolve()).as_posix(),
        "run_live": run_token.is_live(root, params["run_id"]),
    }
