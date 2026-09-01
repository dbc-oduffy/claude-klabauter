"""
coordinator_core.ops.group_em_idle_report — JSON-RPC "groupem.idle_report".

Purpose: expose `coordinator_core.group_em.idle_report.build_report` through
the op registry, mirroring `coordinator_core.ops.session_peer_roster`'s own
thin registration convention exactly. The idle-report oracle is already
correct in-process Python, reachable today only via its own CLI arm; during
an hour-long box-wide Bash denial on 2026-09-01 a crown could not run the
oracle at all through any door — the capability was never missing, only the
door was.

Self-registration: importing this module calls
register_op("groupem.idle_report", _groupem_idle_report) as a side-effect.
Registered in `coordinator_core/ops/__init__.py`'s registration list,
`coordinator_core/ops/_registry_map.py`'s `OP_MODULE_MAP`,
`coordinator_core/op_scopes.py` (scope "none" — same resolution story as
`groupem.enter`), and `coordinator_core/authz/classification.py` (read-only —
`idle_report.build_report` only reads peer transcripts, the harness
registry, and the Group-EM's own offer log; it writes nothing).

Spec backlink: `state/dispatch-briefs/2026-09-01-the-crowns-standing-surfaces-report-themselves/C7.md`.

Negative-spec:
    - Never renders — `build_report`'s own `render`/`summary_line` helpers
      are CLI/human-facing formatting, not this op's job; the op returns
      the report dict verbatim for a caller to interpret.
    - Never supplies its own `names`/`registry_read` override — those are
      `build_report`'s own test-injection seam (see that function's
      docstring); a caller reaching this op always gets the live registry
      read.
    - Never accepts `observed_exits` as anything but a caller-supplied
      list — `build_report` documents this as evidence the caller actually
      saw (no queryable exit event exists in the engine), and this veneer
      does not invent or infer one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.group_em import idle_report as group_em_idle_report


@register_op("groupem.idle_report")
def _groupem_idle_report(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.idle_report" handler.

    Params:
        repo_root (str, optional) -- the repo whose peer transcripts are
            polled. Defaults to the CALLING process's own `os.getcwd()`
            when omitted, matching `groupem.enter`'s own convention.
        group_em_session_id (str, optional) -- the Group EM's session id;
            excluded from the roster along with `caller_session_id`.
        caller_session_id (str, optional) -- the process running the poll;
            excluded from the roster.
        peer (str, optional) -- filter to session ids starting with this
            prefix.
        now (number, optional).
        projects_dir (str, optional).
        observed_exits (list, optional) -- exit transitions the CALLER
            actually saw (session ids or names); see
            `build_report`'s own docstring for why this has no engine-side
            lookup and must be caller-supplied.

    Returns: the `build_report` dict verbatim — see that function's own
    docstring for the full shape (`repo-root`, `verdict`, `group-em-moved`,
    `peers`, `counts`, ...). No field is renamed, dropped, or added here.

    Scope "none" (op_scopes.py): the engine-injected `repo_root` kwarg is
    never resolved/injected for a "none"-scoped op — the `repo_root` WIRE
    PARAM above (read from `params`) is the only way a caller narrows the
    target, matching `groupem.enter`'s own convention exactly.
    """
    p = params if isinstance(params, dict) else {}

    override = p.get("repo_root")
    target_root = override if isinstance(override, str) and override else str(Path.cwd())

    observed_exits = p.get("observed_exits")
    observed_exits = observed_exits if isinstance(observed_exits, (list, tuple, frozenset, set)) else None

    return group_em_idle_report.build_report(
        target_root,
        group_em_session_id=p.get("group_em_session_id"),
        caller_session_id=p.get("caller_session_id"),
        peer=p.get("peer"),
        now=p.get("now"),
        projects_dir=p.get("projects_dir"),
        observed_exits=observed_exits,
    )
