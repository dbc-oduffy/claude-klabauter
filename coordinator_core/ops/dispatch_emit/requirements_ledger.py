"""workflow.revise: a warp `--ask` run's requirements ledger -- the PM's words, seeded at emit and
grown by the EM's revisions while the run is in flight.

`RECORD_DIR_ROOT/<run_id>/requirements.jsonl` holds one JSON object per line and only ever grows; file
order is ledger order, so item ids (`R1`, `R2`, ...) are stable. A line is a `requirement`
(`origin`: `pm_verbatims`, `ask`, `relayed` or `revision`; `relayed` is an EM's relay, a requirement
that never supersedes a `pm_verbatims` item) or a `flag` (`all_of_pm_ask`; the last one wins).
`requirements.md` beside it is the view every stage prompt reads, rewritten from the JSONL on each
write. File I/O only: no git, no subprocess, so the op holds the 500ms bar.

`judge` is the one pass rule. The run's final ledger checker applies it in its prompt and
`dispatch.terminal_commit` re-applies it against the live ledger, so a revision that lands after the
check, or a verdict that skips an item, cannot reach the product commit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Sequence

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, record_dir
from coordinator_core.ops.dispatch_emit.request_validation import Field, validate_params

GENERATES = []

LEDGER_FILE = "requirements.jsonl"
LEDGER_VIEW_FILE = "requirements.md"
ORIGIN_PM_VERBATIMS, ORIGIN_ASK, ORIGIN_REVISION = "pm_verbatims", "ask", "revision"
ORIGIN_RELAYED = "relayed"
STATUSES = ("met", "not_met", "drifted", "superseded")
VERBATIM_BYTE_CAP = 16 * 1024

_PARAMS = (
    Field("run_id", "nonempty_str", required=True),
    Field("verbatim", "str"),
    Field("all_of_pm_ask", "bool"),
)


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
    items: tuple[Item, ...]
    all_of_pm_ask: bool

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


def _read_lines(path: Path) -> list[dict]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # a torn tail line from a crashed writer; the rest stands
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _fold(entries: Sequence[Mapping]) -> Ledger:
    items: list[Item] = []
    all_of = False
    for e in entries:
        if e.get("kind") == "flag" and isinstance(e.get("all_of_pm_ask"), bool):
            all_of = e["all_of_pm_ask"]
        elif e.get("kind") == "requirement" and isinstance(e.get("text"), str) and e["text"].strip():
            items.append(Item(f"R{len(items) + 1}", e["text"], str(e.get("origin") or ""), str(e.get("at") or "")))
    return Ledger(tuple(items), all_of)


def read_ledger(dir_: Path) -> Optional[Ledger]:
    """The ledger in run dir `dir_`, or None when the run was emitted without one."""
    path = Path(dir_) / LEDGER_FILE
    if not path.is_file():
        return None
    return _fold(_read_lines(path))


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


def render(ledger: Ledger, run_id: str) -> str:
    lines = [
        f"# Requirements ledger -- run {run_id}",
        "",
        "Every item is the PM's (an origin), a relayed ask (origin `relayed`: an EM's relay, not the "
        "PM's words; a `pm_verbatims` item outranks it) or a revision the EM sent while the run was "
        "in flight, in the order they arrived. A revision governs over an earlier item it changes.",
        "",
        "All of the PM's ask is mandatory (all_of_pm_ask): " + ("yes" if ledger.all_of_pm_ask else "no"),
        "",
    ]
    for item in ledger.items:
        lines.append(f"## {item.id} ({item.origin}, {item.at})")
        lines.append("")
        lines.extend("> " + ln if ln else ">" for ln in item.text.strip().splitlines())
        lines.append("")
    return "\n".join(lines)


def _append(dir_: Path, run_id: str, entries: Sequence[dict]) -> Ledger:
    from coordinator_core.atomic_append import append_line
    from coordinator_core.atomic_replace import atomic_write_bytes

    dir_.mkdir(parents=True, exist_ok=True)
    sink = dir_ / LEDGER_FILE
    for entry in entries:
        append_line(sink, (json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
    ledger = _fold(_read_lines(sink))
    atomic_write_bytes(dir_ / LEDGER_VIEW_FILE, render(ledger, run_id).encode("utf-8"), preserve_mode=False)
    return ledger


def seed(
    dir_: Path, run_id: str, texts: Sequence[str], *, origin: str, all_of_pm_ask: Optional[bool] = None
) -> Ledger:
    """Append each of `texts` not already in the ledger as an origin item; idempotent, so emit and
    stage may both seed. `all_of_pm_ask` (None leaves it) records the run flag."""
    have = {i.text.strip() for i in (read_ledger(dir_) or Ledger((), False)).items}
    at = _now()
    entries: list[dict] = []
    for text in texts:
        t = text.strip() if isinstance(text, str) else ""
        if t and t not in have:
            have.add(t)
            entries.append({"kind": "requirement", "origin": origin, "text": t, "at": at})
    if all_of_pm_ask is not None:
        entries.append({"kind": "flag", "all_of_pm_ask": bool(all_of_pm_ask), "at": at})
    if not entries and (Path(dir_) / LEDGER_FILE).is_file():
        return read_ledger(dir_) or Ledger((), False)
    return _append(Path(dir_), run_id, entries)


def revise(
    repo_root: Path, run_id: str, *, verbatim: Optional[str] = None, all_of_pm_ask: Optional[bool] = None
) -> tuple[Ledger, Path]:
    """Append the EM's revision (and/or the flag) to an emitted run's ledger."""
    text = verbatim.strip() if isinstance(verbatim, str) else ""
    if not text and all_of_pm_ask is None:
        raise LedgerRefused("workflow.revise needs a non-empty verbatim, all_of_pm_ask, or both")
    if len(text.encode("utf-8")) > VERBATIM_BYTE_CAP:
        raise LedgerRefused(f"verbatim exceeds {VERBATIM_BYTE_CAP} bytes")
    dir_ = run_dir(repo_root, run_id)
    if not dir_.is_dir():
        raise LedgerRefused(f"no run {run_id!r} under {RECORD_DIR_ROOT}: emit it first")
    at = _now()
    entries: list[dict] = []
    if text:
        entries.append({"kind": "requirement", "origin": ORIGIN_REVISION, "text": text, "at": at})
    if all_of_pm_ask is not None:
        entries.append({"kind": "flag", "all_of_pm_ask": bool(all_of_pm_ask), "at": at})
    return _append(dir_, run_id, entries), dir_


def judge(ledger: Ledger, verdict: object) -> dict:
    """`{"pass": bool, "failures": [{"id", "reason"}]}` for a checker `verdict`
    (`{"items": [{"id", "status", "essential"?, "superseded_by"?}]}`) against the live ledger.

    Every item needs exactly one judgment. `drifted` (lost or reinterpreted) always fails;
    `superseded` passes only when it names a later revision item; `not_met` fails when
    `all_of_pm_ask` is set or the item is essential (the default)."""
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
                "reason": "not met" + (" (all_of_pm_ask: every item is mandatory)" if ledger.all_of_pm_ask else ""),
            })
    unknown = sorted(set(by_id) - set(order))
    failures.extend({"id": rid, "reason": "names no ledger item"} for rid in unknown)
    return {"pass": not failures, "failures": failures}


@register_op("workflow.revise")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "workflow.revise": params `run_id` (str, required), `verbatim` (str), `all_of_pm_ask` (bool).

    Appends a stamped, ordered revision to the run's requirements ledger, which every later stage of
    the run reads at its start. Replies `{ok, run_id, item, items, all_of_pm_ask, ledger, run_live}`;
    `run_live` is false once the run's commit token is consumed or absent (nothing will read it).
    A refused request replies `{"error": str}`.
    """
    if repo_root is None:
        return {"error": "workflow.revise requires repo_root"}
    refusal = validate_params("workflow.revise", params, _PARAMS)
    if refusal is not None:
        return refusal
    root = main_worktree_root(Path(repo_root))
    try:
        ledger, dir_ = revise(
            root, params["run_id"], verbatim=params.get("verbatim"), all_of_pm_ask=params.get("all_of_pm_ask")
        )
    except LedgerRefused as exc:
        return {"error": str(exc)}
    from coordinator_core.ops.dispatch_emit import run_token

    record = run_token._record_path(root, params["run_id"])
    added = ledger.items[-1].id if params.get("verbatim") and ledger.items else None
    return {
        "ok": True,
        "run_id": params["run_id"],
        "item": added,
        "items": len(ledger.items),
        "all_of_pm_ask": ledger.all_of_pm_ask,
        "ledger": (dir_ / LEDGER_VIEW_FILE).relative_to(root.resolve()).as_posix(),
        "run_live": bool(record is not None and record.is_file()),
    }
