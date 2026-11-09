"""Companion status bar; install callers use coordinator-invoke codex_status.snapshot."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import os
import sys
import uuid
from coordinator_core.group_em.atomic_record import holder_lock, LockTimeout

from coordinator_core._settings_home import settings_home
from .collect import advance, discover, load, registered_paths, save
from .model import ingest, summary, warn
from .render import render, registered_alias, NATIVE_FOOTER


def snapshot(params: dict) -> dict:
    from coordinator_core.session.core import attributable_session_id
    sid = params.get("session_id") or attributable_session_id()
    if not isinstance(sid, str) or str(uuid.UUID(sid)) != sid.lower():
        raise ValueError("a genuine session UUID is required")
    sid = sid.lower()
    cache = settings_home() / "state" / "codex-status" / f"{sid}.json"
    with holder_lock(cache, timeout=0.05, poll_interval=0.005):
        return _snapshot_locked(params, sid, cache)


def _snapshot_locked(params, sid, cache):
    state = load(cache, sid)
    paths = [Path(p) for p in params.get("rollouts", [])]
    for directory in params.get("discover_dirs", []):
        found, complete = discover(Path(directory), sid)
        paths.extend(found)
        if not complete:
            warn(state, "selected rollout-directory discovery is incomplete")
    known, registry_complete = registered_paths(settings_home() / "host-sessions" / "codex", sid, state)
    paths.extend(known)
    if not registry_complete:
        warn(state, "registered transcript discovery is incomplete")
    # Retain previously registered sources so a resumed render includes closed
    # subagents and older dates, rather than shrinking lifetime spend.
    paths.extend(Path(p) for p in state["files"])
    paths = list(dict.fromkeys(paths))
    if not paths:
        warn(state, "no registered transcript path; provide an explicit root rollout")
    # A producer may supply a durable native event journal from workflows whose
    # backend persisted the actual child UUID, parent UUID and observed usage.
    paths.extend(Path(journal) for journal in params.get("event_journals", []))
    owned_journal = settings_home() / "state" / "codex-usage" / f"{sid}.jsonl"
    if owned_journal.is_file():
        paths.append(owned_journal)
    # Presence is evidence of observed records, never proof of workflow coverage.
    advance(state, list(dict.fromkeys(paths)), byte_budget=params.get("byte_budget", 4 * 1024 * 1024))
    manifest = params.get("coverage_manifest")
    if manifest:
        data = json.loads(Path(manifest).read_text(encoding="utf-8"))
        if data.get("session_id") != sid:
            raise ValueError("coverage manifest belongs to a different session")
        state["coverage"] = {key: data.get("coverage", {}).get(key) is True for key in ("subagents", "workflows")}
    save(cache, state)
    value = summary(state, registered_alias(sid))
    value["text"] = render(value)
    value["native_footer_items"] = NATIVE_FOOTER
    value["cache_path"] = str(cache)
    value["pricing_basis"] = "last-known API-equivalent list rates; standard when service tier is absent; not an invoice"
    if value["backlog_complete"] and value.get("captured_at") and value.get("remaining_percent") is not None:
        from coordinator_core.session.context_usage_sidecar import write_usage
        try:
            write_usage(sid, {"used_percentage": 100 - value["remaining_percent"],
                              "remaining_percentage": value["remaining_percent"],
                              "context_window_size": value["context_window"]}, now=value["captured_at"])
        except OSError:
            value["warnings"] = [*value["warnings"], "context advisory sidecar could not be refreshed"]
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id")
    parser.add_argument("--rollout", action="append", default=[])
    parser.add_argument("--discover-dir", action="append", default=[])
    parser.add_argument("--event-journal", action="append", default=[])
    parser.add_argument("--coverage-manifest")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--ingest-events", action="store_true", help="Consume native app-server JSON notifications from stdin once.")
    args = parser.parse_args(argv)
    params = {"session_id": args.session_id, "rollouts": args.rollout,
              "discover_dirs": args.discover_dir, "event_journals": args.event_journal,
              "coverage_manifest": args.coverage_manifest}
    try:
        if args.ingest_events:
            sid = args.session_id or os.environ.get("CODEX_THREAD_ID")
            if not sid:
                raise ValueError("--session-id is required")
            sid = str(uuid.UUID(sid))
            cache = settings_home() / "state" / "codex-status" / f"{sid}.json"
            source = {}
            remaining = 4 * 1024 * 1024
            for _ in range(64):
                if remaining <= 0:
                    break
                line = sys.stdin.readline(min(remaining, 512 * 1024))
                if not line:
                    break
                remaining -= len(line.encode("utf-8"))
                event = json.loads(line)
                with holder_lock(cache, timeout=0.05, poll_interval=0.005):
                    state = load(cache, sid)
                    ingest(state, event, source)
                    save(cache, state)
        value = snapshot(params)
    except (ValueError, OSError, TypeError, KeyError, LockTimeout) as exc:
        parser.error(str(exc))
    print(json.dumps(value) if args.json else value["text"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
