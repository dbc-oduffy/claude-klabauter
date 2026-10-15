"""Workday-start step manifest: the ceremony's fixed step order as data, with its cadence gates.

The table is a literal tuple of directive records (`build_directive` shape; `op` or `inproc`
replaces `cli` for those kinds) beside a literal mapping of run fields. `steps_for` converts
each through `contract.step_from_directive`; nothing else builds a step. Gates read the tree
and the caller's inputs only: no spawn.
"""
from __future__ import annotations

import datetime as _dt
import functools
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from coordinator_core.contract.decision_object.reader_result import build_directive
from coordinator_core.workday_start import contract

STEP_COSTS_PATH = Path(__file__).resolve().parent / "step_costs.json"

WEEKLY_MARKER = "state/.workday-start-weekly-marker"
WEEKLY_WINDOW_DAYS = 7
DRAFT_PLAN_MIN_ENTRIES = 5
GOAL_EVENT_TEXT = ".coordinator-local/workday-start-goal-event.txt"
SNIPPETS_PREFIX = "coordinator/snippets/"
HARVEST_DEBT_NONE_WITHIN_SCAN = 99

_WS = "coordinator_core.workday_start"
_PROBES = f"{_WS}.probes"


def _cli(id_: str, cli: str, args: list[str], detail: str) -> dict[str, Any]:
    return build_directive(id_, cli, args, detail)


def _swap_key(kind: str, directive: dict[str, Any]) -> dict[str, Any]:
    out = dict(directive)
    out[kind] = out.pop("cli")
    return out


def _op(id_: str, op: str, params: str, detail: str) -> dict[str, Any]:
    return _swap_key("op", build_directive(id_, op, [params], detail))


def _inproc(id_: str, target: str, args: list[str], detail: str) -> dict[str, Any]:
    return _swap_key("inproc", build_directive(id_, target, args, detail))


def touch_weekly_marker(repo_root: Path) -> tuple[int, str]:
    """Create or touch `state/.workday-start-weekly-marker`; the weekly group's only writer."""
    path = Path(repo_root) / WEEKLY_MARKER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return 0, ""


# Table order is the ceremony's order: stages ascend through LAUNCH_STAGES then FINISH_STAGES.
STEP_TABLE: tuple[dict[str, Any], ...] = (
    _cli("hyg-reap-log", "workday-start-day-branch-resolve", ["reap-log"],
         "Step -1: reap dead sessions; the CLI logs to file."),
    _cli("hyg-ensure-hooks-fleet", "coordinator-ensure-hooks-fleet", [],
         "Step -0.45: heal both hooks in every registered repo."),
    _cli("hyg-install-meta-repo-precommit-hook", "install-meta-repo-precommit-hook", ["{claude_home}"],
         "Step -0.4: install the meta-repo precommit hook."),
    _cli("hyg-gitignore-template-drift", "check-gitignore-template-drift", [],
         "Report-only gitignore template drift."),
    _cli("hyg-check-bin-forwarders", "check-bin-forwarders", [],
         "DoE bin files the engine owns must stay thin forwarders."),
    _cli("hyg-fleet-claude-md-handoff-convention", "check-fleet-claude-md-handoff-convention", [],
         "Fleet CLAUDE.md files teaching the retired handoff location."),
    _op("hyg-git-maintenance-daily", "git.maintenance", '{"tier":"daily","repo":"{repo_root}"}',
        "Maintenance checkpoint, daily tier."),
    _inproc("hyg-watch-state-ignore", f"{_WS}.watch_state_ignore:watch_state_ignore", [],
            "Report-only watch-state gitignore and untracked check for this repo."),
    _cli("step0-branch-setup", "workday-start-step0", [],
         "Step 0: sync, precedence switch and reconcile. Its exit code gates every later stage."),
    _cli("hyg-span-assert", "workday-start-day-branch-resolve", ["span-assert"],
         "Branch span assertion."),
    _inproc("orient-day-envelope", f"{_WS}.day_envelope:day_envelope", [],
            "The day's orient envelope: directives and judgment points."),
    _cli("step0-normalize-consumed-frontmatter", "normalize-consumed-frontmatter", [],
         "Idempotent frontmatter and status sync."),
    _cli("step0-handoff-triage-trim-notes", "workday-start-handoff-triage", ["trim-notes"],
         "Rotate tasks/orphan-sweep-notes.md."),
    _cli("triage-ready", "workday-start-handoff-triage", ["ready"],
         "Step 1: actionable handoffs."),
    _cli("triage-awaiting-gate", "workday-start-handoff-triage", ["awaiting-gate"],
         "Step 1.2: gated handoffs."),
    _inproc("triage-stale-executing-plans", f"{_WS}.stale_plans:stale_executing_plans", [],
            "Executing plans untouched past the stale window."),
    _cli("triage-query-completions", "query-completions",
         ["--where", "created>={since_30d}", "--format", "json"],
         "Step 1.4: completed entries of the last 30 days."),
    _cli("triage-sweep-terminal-handoffs", "sweep-terminal-handoffs", [],
         "Step 1.47: sweep terminal handoffs."),
    _cli("triage-promote-shipped-in-flight-stubs", "promote-shipped-in-flight-stubs", [],
         "Step 1.47: promote shipped in-flight stubs."),
    _cli("triage-handoff-housekeeping", "handoff-housekeeping", [],
         "Step 1.47: archive and release handoffs."),
    _inproc("memo-outbox-stale-drafts", f"{_WS}.outbox_drafts:stale_outbox_drafts", [],
            "Outbox drafts awaiting send past the stale window."),
    _cli("memo-inbox-blitz-assemble", "workday-start-inbox-blitz-assemble", [],
         "Step 1.45a: size and shard the inbox response."),
    _cli("rollup-counters-improvement-queue", "workday-start-advisory-counters", ["improvement-queue"],
         "Step 1.6."),
    _cli("rollup-counters-push-failures", "workday-start-advisory-counters", ["push-failures"],
         "Step 1.9."),
    _inproc("rollup-local-ahead-unpublished", f"{_WS}.local_ahead:local_ahead", [],
            "Whether the work branch's tip is unpublished."),
    _inproc("rollup-recheck-due-markers", f"{_WS}.recheck_due:recheck_due", [],
            "Recheck-due markers due within the window."),
    _cli("rollup-counters-stale-stashes", "workday-start-advisory-counters", ["stale-stashes"],
         "Step 1.92."),
    _op("rollup-scratch-hygiene-dryrun", "fleet.scratch_hygiene",
        '{"repo_root":"{repo_root}","cadence":"workday_start","apply":false}',
        "Scratch purge, dry run."),
    _op("rollup-stall-report", "requirement_register.stall_report", '{"today":"{today}"}',
        "Register stall check."),
    _inproc("read-test-red-delta", f"{_PROBES}:test_red_delta", [],
            "Step 1.66: test-red delta."),
    _inproc("read-mcp-registration", f"{_PROBES}:mcp_registration", [],
            "Count of MCP servers registered in ~/.claude.json."),
    _cli("sentinel-check-plugin-drift", "check-plugin-drift", [], "Live-install drift."),
    _cli("sentinel-doctor-sentinel-full", "coordinator-doctor-sentinel", ["--full"],
         "Writes the sentinel read by the boot currency banner."),
    _cli("sentinel-engine-drift", "check-engine-drift", [], "Checkout freshness."),
    _cli("sentinel-forwarder-drift", "check-forwarder-drift", [], "Derived-vs-installed CLIs."),
    _cli("sentinel-deferral-orphan-memo", "check-deferral-orphan-memo", [], "Unowned aged ask."),
    _cli("sentinel-deferral-partial-strangle", "check-deferral-partial-strangle", [],
         "Partial strangler."),
    _cli("sentinel-global-doctrine-mirror", "check-global-doctrine-mirror", [], "Doctrine mirror byte-compare."),
    _cli("sentinel-observer-sidecar-scan", "workday-start-health-probes",
         ["observer-sidecar-scan", "--dir", "archive/daily-summaries"], "Observer sidecar gaps."),
    _cli("sentinel-corpus-currency-probe", "corpus-currency-probe", [],
         "Writes the cache read by the corpus currency banner."),
    _cli("sentinel-mcp-namespace-registration", "check-mcp-namespace-registration", [],
         "Agent-claimed namespaces no config registers."),
    _inproc("sentinel-check-fixture-sync", f"{_PROBES}:fixture_sync_presence", [],
            "Reports bin/check-fixture-sync.sh when present; it is not run (shell-out)."),
    _op("fresh-commit-delta", "freshness.commit_delta", "{}",
        "Steps 2, 3, 3.5: doc, test and bug-sweep commit deltas."),
    _cli("blast-radius", "tier-last-run", ["blast-radius"], "Step 3.7."),
    _cli("weekly-orphan-branch-sweep", "orphan-branch-sweep",
         ["--format", "text", "--severity-min", "warning"], "Weekly: orphan branches."),
    _cli("weekly-draft-plan-aging", "draft-plan-aging", ["docs/plans"], "Weekly: stale draft plans."),
    _cli("weekly-check-harvest-debt", "check-harvest-debt", [], "Harvest debt; daily when no doc commit is in scan depth."),
    _cli("weekly-central-run-due", "central-run-due", [], "Weekly: central run volume."),
    _cli("weekly-verify-snippet-sync", "verify-snippet-sync", ["example-retrieval-repo-preamble"],
         "Weekly, or early when Step 0 changed coordinator/snippets/."),
    _cli("weekly-check-provisional-expiry", "check-provisional-expiry", ["docs/plans"],
         "Weekly: expired provisionals."),
    _op("weekly-aggregate-capability-index", "fleet.aggregate_capability_index", "{}",
        "Weekly: regenerate the capability index."),
    _cli("weekly-backfill-scan", "workday-complete-backfill-scan", ["--lookback", "7"],
         "Weekly: commits-but-no-summary days."),
    _cli("priority-whats-next", "whats-next", [], "Step 4: priority suggestions."),
    _cli("groupem-nomination-who", "group-em-nomination", ["who", "--repo", "{repo_root}"],
         "Step 5.7: read-only nomination."),
    _cli("groupem-watch", "group-em-watch-cli", ["--repo-root", "{repo_root}"],
         "Step 5.7: watch verdict."),
    _inproc("read-tool-availability", f"{_PROBES}:tool_availability", [],
            "Step 5: scc and shellcheck on PATH."),
    _inproc("decide-resolve", "coordinator_core.ceremony_common.run.decide:resolve", [],
            "Resolve the orient envelope's judgment points."),
    _cli("goal-append-goal-event", "append-goal-event",
         ["--period", "day", "--period-value", "{today}", "--text-file", "{goal_event_text}"],
         "One call for the day's suggestion batch."),
    _cli("marker-write-workday-start-marker", "write-workday-start-marker", [],
         "Step 5: write state/.workday-start-marker."),
    _inproc("marker-weekly-touch", f"{__name__}:touch_weekly_marker", [],
            "Touch the weekly marker after the weekly group ran."),
    _cli("cache-orientation-cache", "regenerate-orientation-cache", ["--invoker", "workday-start"],
         "Step 5.5: regenerate state/orientation_cache.md."),
    _cli("cache-copy-currency-refresh", "copy-currency-refresh", [], "Step 5.65."),
    _cli("index-structural-index-refresh", "structural-index-refresh", [], "Step 5.66."),
    _cli("hook-ceremony-hook-output", "coordinator-ceremony-hook", ["workday-start"],
         "Step 5.6: the workday_start_post_command output."),
)

_OBS = "Observations"

# Per-step run fields: stage, heading, optional gates_next / on_exit / gate.
RUN_FIELDS: Mapping[str, Mapping[str, Any]] = {
    "hyg-reap-log": {"stage": "hygiene", "heading": _OBS},
    "hyg-ensure-hooks-fleet": {"stage": "hygiene", "heading": _OBS},
    "hyg-install-meta-repo-precommit-hook": {"stage": "hygiene", "heading": _OBS},
    "hyg-gitignore-template-drift": {"stage": "hygiene", "heading": _OBS},
    "hyg-check-bin-forwarders": {"stage": "hygiene", "heading": _OBS},
    "hyg-fleet-claude-md-handoff-convention": {"stage": "hygiene", "heading": _OBS},
    "hyg-git-maintenance-daily": {"stage": "hygiene", "heading": _OBS},
    "hyg-watch-state-ignore": {"stage": "hygiene", "heading": _OBS},
    "step0-branch-setup": {
        "stage": "step0", "heading": _OBS, "gates_next": True,
        "on_exit": {2: "step0-stale-branch", 3: "step0-reconcile-outcome"},
    },
    "hyg-span-assert": {"stage": "step0", "heading": "Branch Span Mismatch"},
    "orient-day-envelope": {"stage": "orient", "heading": _OBS},
    "step0-normalize-consumed-frontmatter": {"stage": "normalize", "heading": _OBS},
    "step0-handoff-triage-trim-notes": {"stage": "normalize", "heading": _OBS},
    "triage-ready": {"stage": "triage", "heading": "Handoffs"},
    "triage-awaiting-gate": {"stage": "triage", "heading": "Handoffs"},
    "triage-stale-executing-plans": {"stage": "triage", "heading": "Handoffs"},
    "triage-query-completions": {"stage": "triage", "heading": "Alignment Check"},
    "triage-sweep-terminal-handoffs": {"stage": "triage", "heading": "Handoffs"},
    "triage-promote-shipped-in-flight-stubs": {"stage": "triage", "heading": "Handoffs"},
    "triage-handoff-housekeeping": {"stage": "triage", "heading": "Handoffs"},
    "memo-outbox-stale-drafts": {"stage": "memos", "heading": "Inbox"},
    "memo-inbox-blitz-assemble": {"stage": "inbox-assemble", "heading": "Inbox"},
    "rollup-counters-improvement-queue": {"stage": "rollup", "heading": _OBS},
    "rollup-counters-push-failures": {"stage": "rollup", "heading": "Auto-Push Health"},
    "rollup-local-ahead-unpublished": {"stage": "rollup", "heading": "Auto-Push Health"},
    "rollup-recheck-due-markers": {"stage": "rollup", "heading": _OBS},
    "rollup-counters-stale-stashes": {"stage": "rollup", "heading": _OBS},
    "rollup-scratch-hygiene-dryrun": {"stage": "rollup", "heading": _OBS},
    "rollup-stall-report": {"stage": "rollup", "heading": _OBS},
    "read-test-red-delta": {"stage": "rollup", "heading": "Test-Red Delta"},
    "read-mcp-registration": {"stage": "rollup", "heading": _OBS},
    "sentinel-check-plugin-drift": {"stage": "sentinels", "heading": "Addon Health"},
    "sentinel-doctor-sentinel-full": {"stage": "sentinels", "heading": "Addon Health"},
    "sentinel-engine-drift": {"stage": "sentinels", "heading": "Addon Health"},
    "sentinel-forwarder-drift": {"stage": "sentinels", "heading": "Addon Health"},
    "sentinel-deferral-orphan-memo": {"stage": "sentinels", "heading": "Addon Health"},
    "sentinel-deferral-partial-strangle": {"stage": "sentinels", "heading": "Addon Health"},
    "sentinel-global-doctrine-mirror": {"stage": "sentinels", "heading": "Addon Health"},
    "sentinel-observer-sidecar-scan": {"stage": "sentinels", "heading": "Addon Health"},
    "sentinel-corpus-currency-probe": {"stage": "sentinels", "heading": "Addon Health"},
    "sentinel-mcp-namespace-registration": {"stage": "sentinels", "heading": "Addon Health"},
    "sentinel-check-fixture-sync": {"stage": "sentinels", "heading": "Addon Health"},
    "fresh-commit-delta": {"stage": "freshness", "heading": "Context Freshness"},
    "blast-radius": {"stage": "freshness", "heading": "Blast Radius"},
    "weekly-orphan-branch-sweep": {"stage": "weekly", "heading": "Orphan Sweep", "gate": "weekly"},
    "weekly-draft-plan-aging": {"stage": "weekly", "heading": "Weekly", "gate": "weekly+plans"},
    "weekly-check-harvest-debt": {"stage": "weekly", "heading": "Weekly", "gate": "harvest"},
    "weekly-central-run-due": {"stage": "weekly", "heading": "Weekly", "gate": "weekly"},
    "weekly-verify-snippet-sync": {"stage": "weekly", "heading": "Weekly", "gate": "snippets"},
    "weekly-check-provisional-expiry": {"stage": "weekly", "heading": "Weekly", "gate": "weekly"},
    "weekly-aggregate-capability-index": {"stage": "weekly", "heading": "Addon Health", "gate": "weekly"},
    "weekly-backfill-scan": {"stage": "backfill-scan", "heading": "Daily-Wrap Coverage", "gate": "weekly"},
    "priority-whats-next": {"stage": "alignment-inputs", "heading": "Priority Suggestions"},
    "groupem-nomination-who": {"stage": "alignment-inputs", "heading": _OBS},
    "groupem-watch": {"stage": "alignment-inputs", "heading": _OBS},
    "read-tool-availability": {"stage": "alignment-inputs", "heading": _OBS},
    "decide-resolve": {"stage": "decide", "heading": _OBS},
    "goal-append-goal-event": {"stage": "goal-event", "heading": _OBS, "gate": "goal-text"},
    "marker-write-workday-start-marker": {"stage": "marker", "heading": _OBS},
    "marker-weekly-touch": {"stage": "marker", "heading": _OBS, "gate": "weekly"},
    "cache-orientation-cache": {"stage": "orientation-cache", "heading": _OBS, "gate": "tasks-dir"},
    "cache-copy-currency-refresh": {"stage": "copies-cache", "heading": _OBS},
    "index-structural-index-refresh": {"stage": "structural-index", "heading": _OBS},
    "hook-ceremony-hook-output": {"stage": "ceremony-hook", "heading": _OBS},
}

CONSUMES_MANIFEST: frozenset[str] = (
    frozenset(d["cli"] for d in STEP_TABLE if "cli" in d)
    | {"coordinator-queue-append"}
)


def _kind_of(record: Mapping[str, Any]) -> str:
    for kind in ("op", "inproc"):
        if kind in record:
            return kind
    return "cli"


def _cost_class(step_id: str) -> str:
    return _cost_rows().get(step_id, {}).get("cost_class", "unmeasured")


@functools.cache
def _cost_rows() -> Mapping[str, Any]:
    try:
        return json.loads(STEP_COSTS_PATH.read_text(encoding="utf-8"))["steps"]
    except (OSError, ValueError, KeyError):
        return {}


def _expand(args: Sequence[str], values: Mapping[str, str]) -> list[str]:
    out = []
    for a in args:
        for key, val in values.items():
            a = a.replace("{" + key + "}", val)
        out.append(a)
    return out


def _to_spec(record: Mapping[str, Any], values: Mapping[str, str]) -> contract.StepSpec:
    kind = _kind_of(record)
    run = RUN_FIELDS[record["id"]]
    directive = dict(record)
    directive["cli"] = directive.pop(kind, directive.get("cli"))
    directive["args"] = _expand(record["args"], values)
    return contract.step_from_directive(
        directive,
        stage=run["stage"],
        heading=run["heading"],
        cost_class=_cost_class(record["id"]),
        kind=kind,
        gates_next=bool(run.get("gates_next")),
        on_exit=run.get("on_exit"),
    )


def _weekly_fresh(repo_root: Path, today: _dt.date) -> bool:
    try:
        mtime = (Path(repo_root) / WEEKLY_MARKER).stat().st_mtime
    except OSError:
        return False
    return (today - _dt.date.fromtimestamp(mtime)).days <= WEEKLY_WINDOW_DAYS


def _doc_commit_delta(row: Mapping[str, Any] | None) -> int | None:
    """`doc_commit_delta` from the `fresh-commit-delta` ledger row's stdout, or None."""
    if not row:
        return None
    try:
        obj = json.loads(row.get("stdout") or "")
    except ValueError:
        return None
    for layer in (obj, obj.get("result") if isinstance(obj, dict) else None):
        if isinstance(layer, dict) and isinstance(layer.get("doc_commit_delta"), int):
            return layer["doc_commit_delta"]
    return None


def _plans_at_least(repo_root: Path, n: int) -> bool:
    try:
        return len(os.listdir(Path(repo_root) / "docs" / "plans")) >= n
    except OSError:
        return False


def _gate_reason(
    gate: str | None,
    *,
    repo_root: Path,
    today: _dt.date,
    freshness: Mapping[str, Any] | None,
    step0_changed_paths: Sequence[str],
) -> str:
    """The reason a step is cadence-skipped, or an empty string when its gate is open."""
    if gate is None:
        return ""
    weekly_fresh = _weekly_fresh(repo_root, today)
    if gate == "weekly":
        return "weekly-marker-fresh" if weekly_fresh else ""
    if gate == "weekly+plans":
        if weekly_fresh:
            return "weekly-marker-fresh"
        return "" if _plans_at_least(repo_root, DRAFT_PLAN_MIN_ENTRIES) else "fewer-than-5-plans"
    if gate == "harvest":
        delta = _doc_commit_delta(freshness)
        if delta is None or delta == HARVEST_DEBT_NONE_WITHIN_SCAN:
            return ""
        return "weekly-marker-fresh" if weekly_fresh else ""
    if gate == "snippets":
        if any(str(p).replace("\\", "/").startswith(SNIPPETS_PREFIX) for p in step0_changed_paths):
            return ""
        return "weekly-marker-fresh" if weekly_fresh else ""
    if gate == "goal-text":
        return "" if (Path(repo_root) / GOAL_EVENT_TEXT).is_file() else "no-suggestion-batch"
    if gate == "tasks-dir":
        return "" if (Path(repo_root) / "tasks").is_dir() else "no-tasks-dir"
    raise ValueError(f"unknown gate {gate!r}")


def _values(repo_root: Path, today: _dt.date) -> dict[str, str]:
    from coordinator_core._settings_home import claude_config_dir

    return {
        "today": today.isoformat(),
        "repo_root": Path(repo_root).as_posix(),
        "claude_home": str(claude_config_dir()),
        "since_30d": (today - _dt.timedelta(days=30)).isoformat(),
        "goal_event_text": str(Path(repo_root) / GOAL_EVENT_TEXT),
    }


def _partition(
    stages: Sequence[str],
    *,
    repo_root: Path,
    today: _dt.date,
    freshness: Mapping[str, Any] | None,
    step0_changed_paths: Sequence[str],
) -> tuple[list[contract.StepSpec], list[tuple[contract.StepSpec, str]]]:
    wanted = set(stages)
    values = _values(Path(repo_root), today)
    owed: list[contract.StepSpec] = []
    skipped: list[tuple[contract.StepSpec, str]] = []
    for record in STEP_TABLE:
        run = RUN_FIELDS[record["id"]]
        if run["stage"] not in wanted:
            continue
        spec = _to_spec(record, values)
        reason = _gate_reason(
            run.get("gate"),
            repo_root=Path(repo_root),
            today=today,
            freshness=freshness,
            step0_changed_paths=step0_changed_paths,
        )
        if reason:
            skipped.append((spec, reason))
        else:
            owed.append(spec)
    return owed, skipped


def steps_for(
    stages: Sequence[str],
    *,
    repo_root: Path,
    today: _dt.date,
    freshness: Mapping[str, Any] | None = None,
    step0_changed_paths: Sequence[str] = (),
) -> list[contract.StepSpec]:
    """The steps of `stages` today's run owes, in table order, with open gates only.

    `freshness` is the `fresh-commit-delta` ledger row (its `doc_commit_delta` routes
    `check-harvest-debt`; absent keeps it daily). `step0_changed_paths` is Step 0's reported
    changed set. Pair with `cadence_skipped` for the rows the run still owes the ledger.
    """
    return _partition(
        stages, repo_root=repo_root, today=today, freshness=freshness,
        step0_changed_paths=step0_changed_paths,
    )[0]


def cadence_skipped(
    stages: Sequence[str],
    *,
    repo_root: Path,
    today: _dt.date,
    freshness: Mapping[str, Any] | None = None,
    step0_changed_paths: Sequence[str] = (),
) -> list[tuple[contract.StepSpec, str]]:
    """The steps of `stages` a gate closed, each with its reason; the caller ledgers `cadence-skipped`."""
    return _partition(
        stages, repo_root=repo_root, today=today, freshness=freshness,
        step0_changed_paths=step0_changed_paths,
    )[1]
