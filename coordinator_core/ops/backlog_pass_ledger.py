"""Backlog-pass ledger: cumulative tokens per pass over a ceremony's backlog.

One JSONL file per ceremony under the `backlog-pass` state home. A pass stays
open across runs until a run reports `deferred == 0`, which closes it; the next
append starts a new pass. One file append, zero git spawns.
"""

from __future__ import annotations

import json
import os

from coordinator_core.contract import run_budget
from coordinator_core import atomic_append
from coordinator_core.session import record_homes

HOME_KEY = "backlog-pass"


class LedgerHomeMissing(KeyError):
    """The `backlog-pass` key is not declared in record_homes.HOMES."""


def _path(repo_root: str, ceremony: str) -> str:
    try:
        home = record_homes.home_dir(repo_root, HOME_KEY)
    except KeyError as exc:
        raise LedgerHomeMissing(f"record_homes.HOMES has no {HOME_KEY!r} key") from exc
    return os.path.join(home, f"{ceremony}.jsonl")


def _rows(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and all(k in row for k in ("pass_id", "run_id", "deferred", "tokens_projected")):
            rows.append(row)
    return rows


def _current_pass(rows: list[dict]) -> tuple[int, list[dict]]:
    """Pass number and its rows; a closed last pass means the next is new."""
    if not rows:
        return 1, []
    last = rows[-1]["pass_id"]
    in_pass = [r for r in rows if r["pass_id"] == last]
    if in_pass[-1]["deferred"] == 0:
        return last + 1, []
    return last, in_pass


def append(
    repo_root: str,
    ceremony: str,
    run_id: str,
    tokens_projected: int,
    admitted: int,
    deferred: int,
) -> dict:
    """Record one run; returns the written row. A repeated `run_id` within the
    open pass is not recorded twice. Not safe against two concurrent appenders
    (read, then append): one writer per ceremony is assumed."""
    path = _path(repo_root, ceremony)
    rows = _rows(path)
    pass_id, in_pass = _current_pass(rows)
    for r in in_pass:
        if r["run_id"] == run_id:
            return r
    row = {
        "pass_id": pass_id,
        "ceremony": ceremony,
        "run_id": run_id,
        "tokens_projected": tokens_projected,
        "admitted": admitted,
        "deferred": deferred,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_append.append_line(path, (json.dumps(row, sort_keys=True) + "\n").encode("utf-8"))
    return row


def summary(repo_root: str, ceremony: str) -> dict:
    """`{pass_id, runs, cumulative_tokens, open, whole_backlog_bar}` for the
    latest pass. `cumulative_tokens` sums each run's projected tokens (the
    cost-model estimate at emit), not measured spend; `pass_id` is None and `open` False when nothing is recorded."""
    rows = _rows(_path(repo_root, ceremony))
    bar = run_budget.whole_backlog_bar()
    if not rows:
        return {
            "pass_id": None,
            "runs": 0,
            "cumulative_tokens": 0,
            "open": False,
            "whole_backlog_bar": bar,
        }
    last = rows[-1]["pass_id"]
    in_pass = [r for r in rows if r["pass_id"] == last]
    return {
        "pass_id": last,
        "runs": len(in_pass),
        "cumulative_tokens": sum(r["tokens_projected"] for r in in_pass),
        "open": in_pass[-1]["deferred"] != 0,
        "whole_backlog_bar": bar,
    }
