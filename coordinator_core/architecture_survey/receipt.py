"""Architecture survey receipt renderer: outcome, decisions, findings, not-run, run cost.

Pure; no I/O. Headings come from SECTIONS only. Ledger rows are dicts with `step`, `status`
(`ok`, `not-composed`, `failed`), `reason`, and optional `process_ms`, `spawns`, `cost_class`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from coordinator_core.architecture_survey.contract import DECISION_IDS, owed_decisions

SECTIONS: tuple[str, ...] = ("Outcome", "Decisions", "Findings", "Not run", "Run cost")

STALE_DAYS = 90
BAR_MS = 500


def _heading(name: str) -> str:
    if name not in SECTIONS:
        raise ValueError(f"heading outside SECTIONS: {name}")
    return f"### {name}"


def _by_id(decisions: list[Any]) -> dict[str, dict[str, str]]:
    rows = [d.to_row() if hasattr(d, "to_row") else dict(d) for d in decisions]
    return {r["id"]: r for r in rows}


def _all_decisions(decisions: list[Any], agent_results: dict | None) -> dict[str, dict[str, str]]:
    merged = _by_id(decisions)
    if agent_results:
        merged.update(_by_id(agent_results.get("decisions", [])))
    return merged


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _failed_step(ledger_rows: list[dict]) -> str | None:
    for row in ledger_rows:
        if row.get("status") == "failed":
            return str(row.get("step"))
    return None


def _clean(state, by_id, ledger_rows, structure, orphans) -> bool:
    if state == "failed":
        return False
    if any(d["chosen"] == "not-run" for d in by_id.values()):
        return False
    if any(r.get("status") in ("not-composed", "failed") for r in ledger_rows):
        return False
    cp = by_id.get("citation-paths")
    if cp and "fail" in cp["chosen"].lower():
        return False
    if orphans:
        return False
    if structure:
        if structure["index"]["state"] == "behind":
            return False
        if any(not n["covered"] for n in structure["needs"].values()):
            return False
    return True


def _outcome(digest, by_id, ledger_rows, agent_results, structure, orphans, state, reason) -> str:
    mode = digest["mode"]
    if state == "failed":
        step = _failed_step(ledger_rows)
        where = f" at step {step}" if step else ""
        return f"{_heading('Outcome')}\n\nfailed{where}: {reason}\nMode {mode}.\n"
    mapped = len(agent_results["pages_written"]) if agent_results else 0
    commit = by_id.get("commit")
    commit_txt = f"commit {commit['chosen']} ({commit['evidence']})" if commit else "commit none"
    verdict = "clean" if _clean(state, by_id, ledger_rows, structure, orphans) else "not-clean"
    return f"{_heading('Outcome')}\n\nMode {mode}; {mapped} systems mapped; {commit_txt}; {verdict}.\n"


def _decisions(by_id) -> str:
    lines = [_heading("Decisions"), "", "| id | chosen | rule | evidence |", "|---|---|---|---|"]
    for did in DECISION_IDS:
        d = by_id.get(did)
        if d:
            lines.append(f"| {did} | {_cell(d['chosen'])} | {_cell(d['rule'])} | {_cell(d['evidence'])} |")
    return "\n".join(lines) + "\n"


def _days_old(last_mapped: str | None, now: datetime) -> int | None:
    if not last_mapped:
        return None
    try:
        when = datetime.fromisoformat(last_mapped)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (now - when).days


def _run_now(run_id: str) -> datetime:
    return datetime.strptime(run_id[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def _orphans(digest, structure) -> list[str]:
    if not structure:
        return []
    known = {s["name"] for s in structure["systems"]}
    return [s["page"] for s in digest["existing_atlas"]["systems"] if s["name"] not in known]


def _dead_path_entries(payload: Any) -> list[tuple[str, str, str | None]]:
    out: list[tuple[str, str, str | None]] = []
    if isinstance(payload, dict):
        for page, items in payload.items():
            for it in items or []:
                if isinstance(it, dict):
                    out.append((page, str(it.get("path", "")), it.get("last_touch")))
                else:
                    out.append((page, str(it), None))
    return out


def _findings(digest, structure, agent_results, by_id, orphans, now) -> str:
    parts: list[str] = []
    cp = by_id.get("citation-paths")
    if cp:
        parts.append(f"- Citation path check: {cp['chosen']}; {cp['evidence']}")
    ca = by_id.get("citation-atlas")
    if ca:
        parts.append(f"- Atlas citations: {ca['chosen']}; {ca['evidence']}")
    if agent_results:
        for c in agent_results["clarity"]:
            if c["verdict"] != "pass":
                parts.append(f"- Clarity {c['page']}: {c['verdict']}; {'; '.join(c['findings'])}")
    stale = []
    for s in digest["existing_atlas"]["systems"]:
        age = _days_old(s["last_mapped"], now)
        if age is not None and age > STALE_DAYS:
            stale.append(f"{s['page']} ({age} days)")
    if stale:
        parts.append(f"- Pages over {STALE_DAYS} days: {', '.join(stale)}")
    if orphans:
        parts.append(f"- Orphan pages: {', '.join(orphans)}")
    if structure:
        idx = structure["index"]
        parts.append(f"- Structure: {len(structure['systems'])} systems; "
                     f"{sum(s['file_count'] for s in structure['systems'])} files")
        parts.append(f"- Index commit {idx['commit']}: {idx['state']}, {idx['commits_behind']} behind HEAD")
        if idx["dropped_paths"] or idx["unindexed"]:
            parts.append(f"- Dropped paths {idx['dropped_paths']}; unindexed {len(idx['unindexed'])}")
        for name, need in structure["needs"].items():
            where = f"door {need['door']}" if need["covered"] else f"uncovered: {need['reason']}"
            parts.append(f"- Need {name}: {where}")
        repaired = {(r["page"], r["path"]): r["action"]
                    for r in (agent_results or {}).get("dead_paths_repaired", [])}
        for page, path, touch in _dead_path_entries(structure["needs"]["dead-paths"]["payload"]):
            if (page, path) in repaired:
                mark = f"repaired ({repaired[(page, path)]})"
            else:
                mark = "left" + (f", last touched {touch}" if touch else "")
            parts.append(f"- Dead path {page}: {path}: {mark}")
    elif digest.get("consume_gate"):
        gate = digest["consume_gate"]
        for key in ("in_scope_files", "in_scope_file_count", "oversized"):
            if key in gate:
                parts.append(f"- Gate {key}: {gate[key]}")
    if not parts:
        return ""
    return _heading("Findings") + "\n\n" + "\n".join(parts) + "\n"


def _not_run(ledger_rows) -> str:
    rows = [r for r in ledger_rows if r.get("status") in ("not-composed", "failed")]
    if not rows:
        return ""
    lines = [f"- {r['step']}: {r['status']}; {r.get('reason')}" for r in rows]
    return _heading("Not run") + "\n\n" + "\n".join(lines) + "\n"


def _door_timings(structure) -> list[str]:
    out = []
    for name, need in (structure or {}).get("needs", {}).items():
        if not need["door"]:
            continue
        payload = need["payload"] if isinstance(need["payload"], dict) else {}
        prov = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
        ms = prov.get("timing_ms", payload.get("timing_ms"))
        out.append(f"{need['door']} ({name}): {need['calls']} calls, timing_ms {ms}")
    return out


def _run_cost(ledger_rows, structure, agent_results) -> str:
    lines: list[str] = []
    ran = [r for r in ledger_rows if r.get("status") == "ok"]
    lines.append(f"- Steps run: {len(ran)}")
    lines.append(f"- Spawns: {sum(int(r.get('spawns') or 0) for r in ledger_rows)}")
    timed = [r for r in ledger_rows if r.get("process_ms") is not None]
    if timed:
        slow = max(timed, key=lambda r: r["process_ms"])
        lines.append(f"- Slowest step: {slow['step']}, {slow['process_ms']}ms process time")
    over = [r for r in timed if r.get("cost_class") == "engine" and r["process_ms"] > BAR_MS]
    for r in over:
        lines.append(f"- Over {BAR_MS}ms engine step: {r['step']}, {r['process_ms']}ms")
    for d in _door_timings(structure):
        lines.append(f"- Door {d}")
    if agent_results and agent_results.get("agents"):
        a = agent_results["agents"]
        lines.append(f"- Agents: {a['count']}; tokens {a['tokens']}")
    return _heading("Run cost") + "\n\n" + "\n".join(lines) + "\n"


def render(ledger_rows, digest, structure, agent_results, decisions, *, state, reason) -> str:
    """Render the receipt; raise ValueError when a non-failed run lacks an owed decision."""
    by_id = _all_decisions(decisions, agent_results)
    if state != "failed":
        missing = [d for d in owed_decisions(digest["mode"], digest["source"]) if d not in by_id]
        if missing:
            raise ValueError("receipt missing owed decisions: " + ", ".join(missing))
    now = _run_now(digest["run_id"])
    orphans = _orphans(digest, structure)
    sections = [
        _outcome(digest, by_id, ledger_rows, agent_results, structure, orphans, state, reason),
        _decisions(by_id),
        _findings(digest, structure, agent_results, by_id, orphans, now),
        _not_run(ledger_rows),
        _run_cost(ledger_rows, structure, agent_results),
    ]
    return "\n".join(s for s in sections if s)
