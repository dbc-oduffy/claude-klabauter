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
from typing import Iterable, Mapping, Optional, Sequence

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.locked_write import MutateAbort
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, record_dir
from coordinator_core.ops.dispatch_emit.request_validation import Field, validate_params
from coordinator_core.session.declared_writes import declare_write

GENERATES = []

RUN_FILE = "run.json"
INBOX_FILE = "revisions.jsonl"
LEDGER_VIEW_FILE = "requirements.md"
ORIGIN_PM_VERBATIMS, ORIGIN_ASK, ORIGIN_REVISION = "pm_verbatims", "ask", "revision"
ORIGIN_RELAYED = "relayed"
#: Prefix of the `ruling.ref` a `workflow.revise` out_of_scope stamps: `<REVISE_REF>:<run_id>:<revision row id>`.
REVISE_REF = "workflow.revise"
STATUSES = ("met", "not_met", "drifted", "superseded", "post_commit")
#: Acts that can only exist after the terminal commit; a `post_commit` item names which remain.
POST_COMMIT_ACTS = ("commit", "sha_reply", "sha_memo")
VERBATIM_BYTE_CAP = 16 * 1024
#: Every seeded row's surface: a PM sentence names no entry kind, so the checker judges `wired`
#: as reachable from the product at all.
ROW_SURFACE = "internal"

_PARAMS = (
    Field("run_id", "nonempty_str", required=True),
    Field("verbatim", "str"),
    Field("all_of_pm_ask", "bool"),
    Field("out_of_scope", "str_list"),
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
    """The rows a run is judged against: every register row not already ruled deferred/waived.
    `withheld` (`{id, reason}`) are the rows a `workflow.revise` ruled out of scope: never judged,
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


def _withheld_revision(row: Mapping, run_id: str) -> Optional[str]:
    """The revision row id that ruled `row` out of scope in run `run_id`, else None."""
    ruling = row.get(rr.ROW_RULING)
    ref = ruling.get(rr.RULING_REF) if isinstance(ruling, Mapping) else None
    prefix = f"{REVISE_REF}:{run_id}:"
    if row.get(rr.ROW_STATUS) == rr.STATUS_DEFERRED and isinstance(ref, str) and ref.startswith(prefix):
        return ref[len(prefix):] or None
    return None


def _ledger_of(rows: Sequence[Mapping], all_of: bool, sizing_rel: Optional[str], run_id: str = "") -> Ledger:
    items = []
    withheld = []
    for row in rows:
        if row.get(rr.ROW_STATUS) in (rr.STATUS_DEFERRED, rr.STATUS_WAIVED):
            revision = _withheld_revision(row, run_id)
            if revision:
                withheld.append({"id": str(row.get(rr.ROW_ID)), "reason": f"withheld by revision {revision}"})
            continue
        origin, at = _origin(row.get(rr.ROW_ANCHOR))
        items.append(Item(str(row.get(rr.ROW_ID)), str(row.get(rr.ROW_TEXT) or ""), origin, at))
    return Ledger(tuple(items), all_of, sizing_rel, tuple(withheld))


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
        if new_rows is None or new_rows == rows:
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
    root: Path, sizing: Optional[Mapping], ask_text: Optional[str], relayed_text: Optional[str]
) -> dict[int, str]:
    """`{turn: normalised text}` of the sizing's embedded PM turns that provably belong to another
    run. One-turn sizing: the lone turn, when the ask does not quote it and quotes a different
    recorded session turn (the sizing embedded the newest turn by recency). Multi-turn sizing: an
    embedded turn the ask does not quote that matches a recorded session turn preceding every
    session turn the ask quotes; a later turn (a follow-up) is never foreign. Empty for a relayed
    run, a run without an ask, or an ask no recorded session turn quotes."""
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
        if n < earliest and min(len(mine), len(theirs)) >= _MIN_FOREIGN_MATCH and (mine in theirs or theirs in mine):
            out[n] = mine
    return out


FOREIGN_TURN_REASON = "another run's PM turn: the run's ask does not quote it"


def _append_rows(
    root: Path, sizing_rel: str, entries: Sequence[tuple[str, str]], *, drop_turns: Optional[Mapping[int, str]] = None,
    withhold: Optional[Mapping] = None,
) -> list[dict]:
    """Append one open row per `(text, anchor)`; a PM-word or ask row already on the register
    (same verbatim text) is not added twice, a revision always is. `drop_turns` (`{turn: text}`)
    removes the open, unclaimed rows derived from those PM turns and lists them in
    `unrelated_turns`. `withhold` (`{"ids": [...], "run_id", "verbatim"}`) defers the named rows
    with a ruling that cites the row this call appends, in the same register write; an unknown
    id or a met/waived row refuses the whole write."""

    drop_turns = drop_turns or {}

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
        if named:
            revision = str(rows[-1].get(rr.ROW_ID))
            ruling = {
                rr.RULING_SOURCE: "pm", rr.RULING_QUOTE: str(withhold["verbatim"]),
                rr.RULING_REF: f"{REVISE_REF}:{withhold['run_id']}:{revision}", rr.RULING_ON: _now()[:10],
            }
            for rid in dict.fromkeys(named):
                row = by_id[rid]
                if row.get(rr.ROW_STATUS) != rr.STATUS_DEFERRED:
                    row[rr.ROW_STATUS] = rr.STATUS_DEFERRED
                    row[rr.ROW_RULING] = dict(ruling)
        return rows

    unrelated = [{"turn": n, "reason": FOREIGN_TURN_REASON} for n in sorted(drop_turns)]
    return _mutate_register(root, sizing_rel, edit, unrelated=unrelated)


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
    if ledger.withheld:
        lines.append("## Out of scope (ruled by a revision; never judged, carried open)")
        lines.append("")
        lines.extend(f"- {w['id']}: {w['reason']}" for w in ledger.withheld)
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
    return _ledger_of(register.rows if register is not None else [], all_of, sizing_rel, Path(dir_).name)


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
    sizing = sizing if isinstance(sizing, dict) else {}
    foreign = foreign_turns(root, sizing, ask_text, relayed_text)
    entries = (
        []
        if relayed_text and relayed_text.strip()
        else [(t, ORIGIN_PM_VERBATIMS) for t in _seedable_words(sizing, foreign)]
    )
    if ask_text and ask_text.strip():
        entries.append((ask_text, ORIGIN_ASK))
    if relayed_text and relayed_text.strip():
        entries.append((relayed_text, ORIGIN_RELAYED))
    queued = _inbox(dir_)
    entries.extend((e["text"], f"{ORIGIN_REVISION}@{e.get('at') or _now()}") for e in queued)
    if entries or foreign:
        _append_rows(root, sizing_rel, entries, drop_turns=foreign)
    if not bound:
        _write_run(dir_, {**run, "sizing_rel": sizing_rel})
    if queued:
        (dir_ / INBOX_FILE).unlink(missing_ok=True)
    return _write_view(root, dir_, run_id)


def revise(
    repo_root: Path, run_id: str, *, verbatim: Optional[str] = None, all_of_pm_ask: Optional[bool] = None,
    out_of_scope: Sequence[str] = (),
) -> tuple[Ledger, Path, Optional[str]]:
    """Append the EM's revision (and/or set the flag) on an emitted run: a register row when the
    run's sizing is bound, else a queued revision `bind` folds in. `out_of_scope` ids are deferred
    in the same register write as the revision row, the verbatim being their ruling; it needs that
    verbatim and a bound sizing. Returns (ledger, run dir, the new row id or None)."""
    text = verbatim.strip() if isinstance(verbatim, str) else ""
    named = [str(i) for i in out_of_scope]
    if not text and all_of_pm_ask is None:
        raise LedgerRefused("workflow.revise needs a non-empty verbatim, all_of_pm_ask, or both")
    if named and not text:
        raise LedgerRefused("out_of_scope needs a non-empty verbatim: the ruling carries its words")
    if len(text.encode("utf-8")) > VERBATIM_BYTE_CAP:
        raise LedgerRefused(f"verbatim exceeds {VERBATIM_BYTE_CAP} bytes")
    root = Path(repo_root)
    dir_ = run_dir(root, run_id)
    run = read_run(dir_)
    if run is None:
        raise LedgerRefused(f"no ledger-bearing run {run_id!r} under {RECORD_DIR_ROOT}: emit it first")
    if named and not run.get("sizing_rel"):
        raise LedgerRefused("out_of_scope needs a bound sizing: the run has no register yet")
    withhold = {"ids": named, "run_id": run_id, "verbatim": text} if named else None
    if all_of_pm_ask is not None:
        run = {**run, "all_of_pm_ask": bool(all_of_pm_ask)}
        _write_run(dir_, run)
        declare_write(dir_ / RUN_FILE)
    added: Optional[str] = None
    if text:
        at = _now()
        if run.get("sizing_rel"):
            rows = _append_rows(root, run["sizing_rel"], [(text, f"{ORIGIN_REVISION}@{at}")], withhold=withhold)
            added = str(rows[-1].get(rr.ROW_ID)) if rows else None
        else:
            from coordinator_core.atomic_append import append_line

            append_line(dir_ / INBOX_FILE, (json.dumps({"text": text, "at": at}, ensure_ascii=False) + "\n").encode("utf-8"))
            declare_write(dir_ / INBOX_FILE)
            added = f"Q{len(_inbox(dir_))}"
    ledger = _write_view(root, dir_, run_id)
    if ledger is not None:
        declare_write(dir_ / LEDGER_VIEW_FILE)
    return ledger or Ledger((), bool(run.get("all_of_pm_ask"))), dir_, added


def normalize_verdict(verdict: object) -> object:
    """A copy of `verdict` in which every `not_met` item whose non-empty `remaining` list holds only
    `POST_COMMIT_ACTS` reads `post_commit` with those acts; every other item and a shapeless
    verdict come back unchanged."""
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


def _roots_read(row: Mapping) -> list[str]:
    value = row.get("root_read")
    if isinstance(value, str):
        value = [value]
    return [v for v in value or [] if isinstance(v, str) and v.strip()]


def _misrouted(row: Mapping, owed: str) -> Optional[str]:
    """The failure reason when `row` (met/not_met/post_commit) did not name the engine root `owed`."""
    from coordinator_core.win_portability import same_path

    read = _roots_read(row)
    if owed and any(same_path(r, owed) for r in read):
        return None
    return f"judged at {', '.join(read) or 'no root named'}, owed the engine root {owed or '(unresolved)'}"


def judge(
    ledger: Ledger, verdict: object, routed: Optional[Mapping[str, str]] = None,
    gated: Optional[Iterable[str]] = None,
) -> dict:
    """`{"pass": bool, "failures": [{"id", "reason"}], "open": [{"id", "reason"}]}` for a checker
    `verdict` (`{"items": [{"id", "status", "essential"?, "withheld_by"?, "superseded_by"?,
    "post_commit_acts"?, "remaining"?, "root_read"?}]}`, read through `normalize_verdict`) against
    the live register. `open` lists every `not_met` row let through (never passed as met).

    `gated` is the stage manifest's gated chunk ids (`manifest.gated[].id`); None or empty withholds
    nothing. A `not_met` row whose `withheld_by` is in `gated` passes and is listed open
    (`withheld_by <id>`), whatever `all_of_pm_ask` or `essential` say; a `withheld_by` outside
    `gated` fails.

    `routed` maps an engine-routed item id to the engine root it was owed: a met/not_met/post_commit
    item whose `root_read` (string or list) does not include that root adds a failure. It never
    removes one; None or empty is the cwd-only judgment.

    A row in `ledger.withheld` (ruled out of scope by a revise) is listed in `open` as
    `withheld by revision <id>` whether the verdict omits or judges it, and never fails.

    Every row needs exactly one judgment. `drifted` (lost or reinterpreted) always fails;
    `superseded` passes only when it names a later revision row; `not_met` fails when
    `all_of_pm_ask` is set or the row is essential (the default), else it passes listed open as
    `non-essential`; `post_commit` passes only when
    `post_commit_acts` is a non-empty list drawn from `POST_COMMIT_ACTS`."""
    verdict = normalize_verdict(verdict)
    rows = verdict.get("items") if isinstance(verdict, Mapping) else None
    if not isinstance(rows, list):
        return {"pass": False, "failures": [{"id": None, "reason": "verdict carries no items list"}], "open": []}
    gated_ids = set(gated or ())
    by_id: dict[str, Mapping] = {}
    failures: list[dict] = []
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
        if row is None:
            failures.append({"id": item.id, "reason": "unjudged (arrived after the check, or skipped)"})
            continue
        status = row.get("status")
        if routed and item.id in routed and status in ("met", "not_met", "post_commit"):
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
            if not isinstance(acts, list) or not acts:
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
                else:
                    failures.append({
                        "id": item.id,
                        "reason": f"withheld_by {withheld} names no gated chunk of the run's manifest",
                    })
            elif ledger.all_of_pm_ask or row.get("essential", True) is not False:
                failures.append({
                    "id": item.id,
                    "reason": "not met" + (" (all_of_pm_ask: every row is mandatory)" if ledger.all_of_pm_ask else ""),
                })
            else:
                open_rows.append({"id": item.id, "reason": "non-essential"})
    withheld_ids = {w["id"] for w in ledger.withheld}
    unknown = sorted(set(by_id) - set(order) - withheld_ids)
    failures.extend({"id": rid, "reason": "names no requirement row"} for rid in unknown)
    open_rows.extend(dict(w) for w in ledger.withheld)
    return {"pass": not failures, "failures": failures, "open": open_rows}


def apply_verdict(repo_root: Path, sizing_rel: str, verdict: Mapping, *, run_id: str, sha: str) -> list[dict]:
    """Write a passed verdict onto the register once the commit `sha` landed: met and wired ->
    `met` with `<run_id>@<sha>` in met_by; met alone -> `partial`; superseded -> `waived` with the
    PM's superseding words as the ruling. A `post_commit` item whose acts are within
    `{commit, sha_reply}` is written as `met` (the landed commit is the act); one naming
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
                if wired and not memo_owed:
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


def post_commit_owed(verdict: Mapping, sha: str) -> list[dict]:
    """What the run still owes once `sha` exists: `{id, acts, sha}` per `post_commit` item that
    names `sha_memo`, which the engine never sends."""
    verdict = normalize_verdict(verdict)
    owed = []
    for r in verdict.get("items") or []:
        if not isinstance(r, Mapping) or r.get("status") != "post_commit":
            continue
        acts = [a for a in r.get("post_commit_acts") or [] if isinstance(a, str)]
        if "sha_memo" in acts:
            owed.append({"id": str(r.get("id")), "acts": acts, "sha": sha})
    return owed


@register_op("workflow.revise")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "workflow.revise": params `run_id` (str, required), `verbatim` (str), `all_of_pm_ask` (bool),
    `out_of_scope` (list of register row ids, needs `verbatim`: each is deferred with the verbatim as
    its ruling, carried open on the commit, and replied as `withheld`).

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
            root, params["run_id"], verbatim=params.get("verbatim"), all_of_pm_ask=params.get("all_of_pm_ask"),
            out_of_scope=params.get("out_of_scope") or (),
        )
    except LedgerRefused as exc:
        return {"error": str(exc)}
    from coordinator_core.ops.dispatch_emit import run_token

    live = run_token.is_live(root, params["run_id"])
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
    named = set(params.get("out_of_scope") or ())
    if named:
        out["withheld"] = [w["id"] for w in ledger.withheld if w["id"] in named]
    if not live:
        out.update(_refire(ledger.sizing_rel, root))
    return out


def _unclaimed_rows(root: Path, sizing_rel: Optional[str]) -> tuple[Optional[str], list[str]]:
    """`(plan_rel, register row ids the plan's register_claims omit)` for the sizing's `plan:` edge;
    `(None, [])` when the sizing has no readable plan claiming it. File reads only."""
    if not sizing_rel:
        return None, []
    import yaml

    try:
        sizing_text = _sizing_path(root, sizing_rel).read_text(encoding="utf-8")
        sizing = yaml.safe_load(sizing_text)
    except (LedgerRefused, OSError, yaml.YAMLError):
        return None, []
    edge = sizing.get("plan") if isinstance(sizing, dict) else None
    if not (isinstance(edge, str) and edge.strip()):
        return None, []
    plan = contained_path(Path(root) / edge, [Path(root) / "docs" / "plans"])
    if plan is None or not plan.is_file():
        return None, []
    from coordinator_core.roadmap.prep_gate import plan_frontmatter

    claims = plan_frontmatter(plan.read_text(encoding="utf-8", errors="replace")).get("register_claims")
    if not isinstance(claims, dict) or claims.get("sizing") != sizing_rel or not isinstance(claims.get("rows"), list):
        return None, []
    register = rr.read_register(sizing_text)
    claimed = {str(r) for r in claims["rows"]}
    missing = [str(r[rr.ROW_ID]) for r in register.rows if str(r[rr.ROW_ID]) not in claimed] if register else []
    return (edge, missing) if missing else (None, [])


def _refire(sizing_rel: Optional[str], root: Optional[Path] = None) -> dict:
    """`{next, note}` for a revision no stage will read: the exact submit call that starts a run which
    will. When the sizing's plan omits register rows, the refire would fire that stale plan: the
    reply names the plan-revision step first and holds the fire call as `then`."""
    from coordinator_core.warp_fire import fire_command

    plan_rel, missing = _unclaimed_rows(root, sizing_rel) if root is not None else (None, [])
    if plan_rel:
        return {
            "next": {"kind": "revise-plan", "plan": plan_rel, "rows": missing, "then": fire_command(sizing_rel)},
            "note": (
                f"run finished: {plan_rel} omits register row(s) {', '.join(missing)} in register_claims.rows; "
                "a refire would fire the stale plan. Revise the plan to claim them, then refire."
            ),
        }
    return {
        "next": {"kind": "refire", "call": fire_command(sizing_rel)},
        "note": "run finished: nothing reads this revision until a refire.",
    }
