"""Workday-start's own contract names: its stages and rule ids.

Everything ceremony-neutral lives in `ceremony_common.run.contract` and is re-exported here, so
the package's modules and the DoE workflow template keep one import path.
"""
from __future__ import annotations

from coordinator_core.ceremony_common.run.contract import (  # noqa: F401
    COST_CLASSES,
    DEADLINE_S,
    DECISIONS_FILE,
    DIGEST_STATES,
    EXIT_GATE_FAILED,
    EXIT_IN_FLIGHT,
    EXIT_OK,
    EXIT_TEMPLATE_MISSING,
    EXIT_USAGE,
    HANDBACK_DISPOSITIONS,
    LEDGER_ROW_KEYS,
    NARRATION_DENY_LIST,
    NEVER_OFFERED_DISPOSITIONS,
    RUN_DIR_SUFFIX,
    RUN_ID_RE,
    SCHEMA_PATH,
    STEP_KINDS,
    STEP_STATUSES,
    Decision,
    StepResult,
    StepSpec,
    build_digest,
    step_from_directive,
    validate_agent_results,
    validate_digest,
)

LAUNCH_STAGES: tuple[str, ...] = (
    "hygiene",
    "step0",
    "orient",
    "normalize",
    "triage",
    "memos",
    "inbox-assemble",
    "rollup",
    "sentinels",
    "freshness",
    "weekly",
    "backfill-scan",
    "alignment-inputs",
    "decide",
)

FINISH_STAGES: tuple[str, ...] = (
    "decisions",
    "failure-backlog",
    "briefing",
    "goal-event",
    "marker",
    "orientation-cache",
    "copies-cache",
    "structural-index",
    "ceremony-hook",
)

RULE_IDS: tuple[str, ...] = (
    "step0-stale-branch",
    "step0-reconcile-outcome",
    "recommendation",
    "memo-inbox",
    "no-live-disposition",
)
