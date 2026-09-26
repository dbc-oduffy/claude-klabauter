"""coordinator_core.hooks.observe_config_change — ConfigChange hook, OBSERVE ONLY.

Port of: DoE-claude `coordinator/hooks/scripts/observe-config-change.py`
(docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C12). Shape per the
W4-C1 verdict (state/audits/doe-script-arrivals/W4-C1-hook-reach-spike.md):
command/native-door — no coordinator/bin shim (W4-C16 lands the single
`hook-run` entrypoint), no http registration.

Records a JSONL line per fire; blocks nothing, mutates nothing outside its
own log file. `ConfigChange` CAN block a config change on this harness, but
a first registration that both observes and enforces gives no way to tell a
false positive from a correct block on a file every session boots through —
so this lands the signal only (see DoE source docstring, reused verbatim
below for the reachability/payload-shape rationale, not re-derived here).

Payload shape (`session_id`, `transcript_path`, `cwd`, `hook_event_name`,
`prompt_id`, `source`, `file_path`) is EMPIRICALLY MEASURED on harness
2.1.220, not inferred — see DoE's
`state/reference/anthropic-docs/_hook-frontmatter-reachability.md`. Confirmed
coverage is narrower than "watches config": tool-caused writes to
`.claude/settings.local.json` only. Do not widen this handler's assumed
coverage without a new measurement.

Contract: always returns `no_advisory()` — this hook injects no context; its
only product is the log line. Never raises. Malformed input, an unwritable
log directory, or a missing key must never propagate — fail-open is the
whole contract for an observer hook.

Where the record goes:
`<git-common-dir>/coordinator-sessions/hook-observations/ConfigChange.jsonl`
(one JSON object per line). DERIVED bookkeeping, not authored state — lives
outside `state/subagent-share/` (DR-091 — that namespace is authored-only).
`<git-common-dir>` resolution delegates to
`coordinator_core.hooks.support.git_common_dir.resolve_git_common_dir`
(ported W4-C4, zero-spawn) instead of DoE's sibling-script `_git_common_dir`
import — same algorithm, package-relative import per this plan's landing
convention.

Negative-spec: do NOT add enforcement (blocking a config change) here — that
is a successor's job per the DoE source's own NAMED EXIT CONDITION (after 50
observed payloads or 2026-09-10, whichever comes first, the EM reads the
accumulated records and either files the enforcement successor or retires
this hook). This port carries that condition forward unchanged.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md, chunk W4-C12.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.hooks._envelope import no_advisory, payload_of
from coordinator_core.hooks.support.git_common_dir import resolve_git_common_dir
from coordinator_core.ipc import register_op

_EVENT_NAME = "ConfigChange"

_KNOWN_FIELDS = (
    "session_id",
    "transcript_path",
    "cwd",
    "hook_event_name",
    "prompt_id",
    "source",
    "file_path",
)


def _find_git_common_dir(start: Path) -> Optional[Path]:
    try:
        probe = start.resolve()
    except Exception:
        return None

    git_root = None
    for candidate in (probe, *probe.parents):
        try:
            if (candidate / ".git").exists():
                git_root = candidate
                break
        except Exception:
            return None
    if git_root is None:
        return None

    common = resolve_git_common_dir(str(git_root))
    return Path(common) if common else None


def _append_record(git_common_dir: Path, record: dict) -> None:
    try:
        log_dir = git_common_dir / "coordinator-sessions" / "hook-observations"
        log_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with (log_dir / f"{_EVENT_NAME}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        return


def run(payload: Optional[dict]) -> None:
    observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if not isinstance(payload, dict):
        record = {
            "observed_at": observed_at,
            "hook_event_name": _EVENT_NAME,
            "parse_error": True,
        }
        cwd_hint = Path.cwd()
    else:
        record: dict = {
            "observed_at": observed_at,
            "hook_event_name": _EVENT_NAME,
        }
        for key in _KNOWN_FIELDS:
            if key in payload:
                record[key] = payload.get(key)
        cwd_value = payload.get("cwd")
        cwd_hint = Path(cwd_value) if isinstance(cwd_value, str) and cwd_value else Path.cwd()

    git_common_dir = _find_git_common_dir(cwd_hint)
    if git_common_dir is None:
        return

    _append_record(git_common_dir, record)


@register_op("hooks.observe_config_change")
def _handler(params: dict, repo_root=None) -> dict:
    params = payload_of(params)
    try:
        run(params)
    except Exception:
        pass
    return no_advisory()
