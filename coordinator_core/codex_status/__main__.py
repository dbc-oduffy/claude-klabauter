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
from .collect import advance, discover, load, registered_paths, rollout_for, save
from .model import descendants, ingest, summary, warn
from .render import render, registered_alias, NATIVE_FOOTER
from coordinator_core.tui_status.grok import before_compact


def _auto_compact_limit(row: dict | None = None) -> tuple[int | None, str | None]:
    """Resolve an observed session limit, selected profile, or version-matched model default."""
    import tomllib
    row = row or {}
    def valid(value):
        return isinstance(value, int) and not isinstance(value, bool) and value > 0
    limit = row.get("auto_compact_limit")
    basis = "observed session model_auto_compact_token_limit" if valid(limit) else None
    raw_window = row.get("raw_context_window")
    home = os.environ.get("CODEX_HOME")
    base = Path(home) if home else Path.home() / ".codex"
    try:
        with (base / "config.toml").open("rb") as handle:
            config = tomllib.loads(handle.read(256 * 1024).decode("utf-8"))
        profile = row.get("profile", config.get("profile"))
        selected = config.get("profiles", {}).get(profile, {}) if isinstance(profile, str) else {}
        if not valid(limit):
            limit = selected.get("model_auto_compact_token_limit", config.get("model_auto_compact_token_limit"))
            if valid(limit):
                basis = f"configured model_auto_compact_token_limit={limit} (profile={profile})"
        raw_window = row.get("raw_context_window", selected.get("model_context_window", config.get("model_context_window")))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    try:
        with (base / "models_cache.json").open("rb") as handle:
            catalog = json.loads(handle.read(1024 * 1024))
        if row.get("cli_version") and catalog.get("client_version") == row["cli_version"]:
            for model in catalog.get("models", []):
                if model.get("slug") != row.get("model"):
                    continue
                raw = raw_window if valid(raw_window) else model.get("context_window")
                default = model.get("auto_compact_token_limit")
                if row["cli_version"] == "0.171.0" and valid(raw):
                    default = min(default, raw * 9 // 10) if valid(default) else raw * 9 // 10
                if valid(default):
                    resolved = min(limit, default) if valid(limit) else default
                    return resolved, f"runtime model default ({row['cli_version']}, {model['slug']}); {basis or 'resolved raw context window'}"
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    observed_raw = row.get("raw_context_window")
    observed_model = row.get("model")
    if (row.get("cli_version") == "0.171.0" and valid(observed_raw)
            and isinstance(observed_model, str) and observed_model.strip()
            and observed_model != "unknown"):
        default = observed_raw * 9 // 10
        resolved = min(limit, default) if valid(limit) else default
        return resolved, f"runtime model default (0.171.0, {observed_model}); observed raw context window; {basis or 'native default'}"
    return (limit, basis) if valid(limit) else (None, None)


def snapshot(params: dict) -> dict:
    from coordinator_core.session.core import attributable_session_id
    sid = params.get("session_id") or attributable_session_id()
    if not isinstance(sid, str) or str(uuid.UUID(sid)) != sid.lower():
        raise ValueError("a genuine session UUID is required")
    sid = sid.lower()
    cache = settings_home() / "state" / "codex-status" / f"{sid}.json"
    with holder_lock(cache, timeout=0.05, poll_interval=0.005):
        return _snapshot_locked(params, sid, cache)


def _advance_descendant_journals(state: dict, sid: str, budget: int) -> None:
    """Read the owned journal of every thread already attributed to this root's tree.

    A workflow worker that fires its own workflow journals that usage under its own id; the tree
    reaches it only through the parent link recorded in an ancestor's journal.
    """
    journals = settings_home() / "state" / "codex-usage"
    read = {sid}
    complete = state.get("backlog_complete", True)
    while True:
        pending = [t for t in descendants(state) - read if (journals / f"{t}.jsonl").is_file()]
        read |= descendants(state)
        if not pending:
            state["backlog_complete"] = complete
            return
        paths = [journals / f"{t}.jsonl" for t in pending]
        # A journal first registered by this refresh was unread when the refresh began.
        fresh = any(str(p.resolve()) not in state["files"] for p in paths)
        advance(state, paths, byte_budget=budget)
        complete = complete and state["backlog_complete"] and not fresh


def _snapshot_locked(params, sid, cache) -> dict:
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
    if not any(p.name.endswith(f"-{sid}.jsonl") for p in paths):
        own = rollout_for(sid)
        if own is not None:
            paths.insert(0, own)
        else:
            warn(state, "thread rollout not found by session id in today's or yesterday's sessions; pass an explicit rollout")
    paths = list(dict.fromkeys(paths))
    if not paths:
        warn(state, "no registered transcript path; provide an explicit root rollout")
    # A producer may supply a durable native event journal from workflows whose
    # backend persisted the actual child UUID, parent UUID and observed usage.
    paths.extend(Path(journal) for journal in params.get("event_journals", []))
    for owned_sid in descendants(state):
        try:
            owned_sid = str(uuid.UUID(owned_sid))
        except (ValueError, TypeError, AttributeError):
            continue
        owned_journal = settings_home() / "state" / "codex-usage" / f"{owned_sid}.jsonl"
        if owned_journal.is_file():
            paths.append(owned_journal)
    # Presence is evidence of observed records, never proof of workflow coverage.
    budget = params.get("byte_budget", 4 * 1024 * 1024)
    advance(state, list(dict.fromkeys(paths)), byte_budget=budget)
    _advance_descendant_journals(state, sid, budget)
    manifest = params.get("coverage_manifest")
    if manifest:
        data = json.loads(Path(manifest).read_text(encoding="utf-8"))
        if data.get("session_id") != sid:
            raise ValueError("coverage manifest belongs to a different session")
        state["coverage"] = {key: data.get("coverage", {}).get(key) is True for key in ("subagents", "workflows")}
    save(cache, state)
    value = summary(state, registered_alias(sid))
    limit, basis = _auto_compact_limit(state["threads"].get(sid))
    window = value.get("context_window")
    if limit and isinstance(window, (int, float)) and not isinstance(window, bool) and window > 0:
        before = before_compact(value.get("remaining_percent"), 100 * limit / window)
        if before is not None:
            value["before_compact_percent"] = before
            value["context_basis"] = basis
            value["auto_compact_token_limit"] = limit
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
