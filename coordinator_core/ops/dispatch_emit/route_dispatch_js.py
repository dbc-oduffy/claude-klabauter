"""The ``_routedRun`` JS helper: triage hop, disposition parse, one bounded redirect, refusal as BLOCKED.

Emitted once per composed script by the plan-route and engine-ask composers. It consumes a
Route-shaped JS object (``workClass``, ``triage``, ``primary``, ``targets``) and a
``dispatch(type, extra)`` callback, so it depends on no Python route module.
"""

from __future__ import annotations

from typing import Callable, Mapping

from coordinator_core.ops.workflow_scaffold import _js_string_literal

#: Prefix of the row result a refusal returns; ``BLOCKED:`` classifies the row as blocked
#: and the EM reads the rest from the run's chunk reasons.
REFUSED_PREFIX = "BLOCKED: route-refused"

ROUTED_RUN_JS = r"""async function _routedRun(route, dispatch) {
  const _refuse = (why) => 'BLOCKED: route-refused (' + route.workClass + '): ' + why;
  const _disposition = (result) => {
    const text = (typeof result === 'string' ? result : JSON.stringify(result)).replace(/\\n/g, '\n');
    const d = /^\s*disposition:\s*(proceed|redirect|refuse)\b/m.exec(text);
    if (!d) return null;
    const t = /redirect_agent_type:\s*([A-Za-z0-9_:.-]+)/.exec(text);
    const r = /reason:\s*(.+)/.exec(text);
    return { kind: d[1], target: t ? t[1] : '', reason: r ? r[1].replace(/["\s]+$/, '') : '' };
  };
  let cur = route.triage || route.primary;
  let onTriage = !!route.triage;
  let redirectsLeft = 1;
  while (true) {
    const result = await dispatch(cur, onTriage ? _triageClause(route) : '');
    const d = _disposition(result);
    if (!d) {
      if (typeof log === 'function') log('route ' + route.workClass + ': ' + cur + ' replied without a disposition line; proceeding' + (onTriage ? ' to the primary ' + route.primary : ''));
      if (!onTriage) return result;
      onTriage = false;
      cur = route.primary;
      continue;
    }
    if (d.kind === 'proceed') {
      if (!onTriage) return result;
      onTriage = false;
      cur = route.primary;
      continue;
    }
    if (d.kind === 'refuse') return _refuse(d.reason || cur + ' refused');
    if (!d.target || route.targets.indexOf(d.target) < 0) {
      return _refuse('redirect target ' + (d.target || '(none)') + ' cannot take this class');
    }
    if (onTriage) {
      onTriage = false;
    } else if (redirectsLeft > 0) {
      redirectsLeft -= 1;
    } else {
      return _refuse('second redirect to ' + d.target + ' not allowed');
    }
    cur = d.target;
  }
}
function _triageClause(route) {
  return '\n\nRouting: assess this work before proceeding. Lead your reply with a line "disposition: proceed", "disposition: redirect" or "disposition: refuse". '
    + 'On redirect add "redirect_agent_type: <type>", one of: ' + route.targets.join(', ') + '. '
    + 'On redirect or refuse add "reason: <one line>".';
}
"""


def routed_run_js() -> str:
    """The ``_routedRun`` and ``_triageClause`` declarations, emitted once per script."""
    return ROUTED_RUN_JS


def _js_list(values) -> str:
    return "[" + ", ".join(_js_string_literal(v) for v in values) + "]"


def route_object_literal(route: Mapping) -> str:
    """JS object literal for a route; ``triage`` may be absent or ``None``."""
    triage = route.get("triage")
    return (
        "{ workClass: " + _js_string_literal(route["workClass"])
        + ", triage: " + (_js_string_literal(triage) if triage else "null")
        + ", primary: " + _js_string_literal(route["primary"])
        + ", targets: " + _js_list(route["targets"]) + " }"
    )


def literal_dispatch_js(targets: Mapping[str, str], call_for: Callable[[str, str], str]) -> str:
    """``(t, extra) => t === '<a>' ? <call a> : ... : null``: one literal agent() call per target."""
    chain = "null"
    for agent_type, model in reversed(list(targets.items())):
        chain = f"t === {_js_string_literal(agent_type)} ? {call_for(agent_type, model)} : {chain}"
    return f"(t, extra) => {chain}"
