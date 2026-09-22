"""coordinator_core.hooks.offer_exploration_tier_dispatch — PreToolUse hook,
matcher: Agent.

Port of: DoE-claude `coordinator/hooks/scripts/offer-exploration-tier-dispatch.py`
(docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C12). Shape per the
W4-C1 verdict: command/native-door — no coordinator/bin shim, no http
registration.

Design-as-offers hook on the dispatch path: when the EM dispatches a
doctrine-carrying agent (anything other than `Explore`/`Plan`, the two the
harness exempts from the CLAUDE.md corpus) for work whose prompt reads as
read-only-shaped, this hook offers the cheap alternative -- an UNNAMED
`Explore` dispatch -- instead of nagging about the more expensive one
already chosen. Never blocks, never mutates the call: `additionalContext`
only, matching `nudge_multiwave_workflow.py`'s own shape
(`allow_advisory("PreToolUse", ...)`), never the `rewrite_input`/
`updatedInput` shape.

WHY UNNAMED, SPECIFICALLY (unchanged from DoE source) -- naming an
`Explore` dispatch discards the harness's built-in `Explore` definition and
rebuilds the child from the main-loop prompt builder, and the `Edit`/
`Write` denial lives in the definition that gets discarded. A named
`Explore` is therefore NOT read-only, and offering one would be actively
wrong advice.

DETECTION -- conservative by design (unchanged from DoE source). The firing
rule requires BOTH:
  1. the dispatch target is doctrine-carrying (subagent_type present and
     not `Explore`/`Plan`, case-insensitive), AND
  2. the prompt is read-only-SHAPED: a find/locate/search/survey-class
     signal AND no write-shaped instruction anywhere (a write verb present
     anywhere disqualifies the offer outright -- no negation-scrubbing).
Both legs are exposed as small pure functions (`_is_doctrine_carrying`,
`_is_read_only_shaped`), directly unit-testable.

Shape changes, all forced by this row's own op contract, none a behaviour
change:
  (a) stdin/stdout JSON I/O becomes the `params`-dict-in / envelope-dict-out
      contract every op in this package shares.
  (b) `_message_envelope`'s `emit(message, CHANNEL_ADDITIONAL_CONTEXT)`
      becomes `coordinator_core._hook_envelope.allow_advisory`, returning
      the envelope dict directly.
  (c) `_git_common_dir`/`_session_hub` sibling-script imports become
      package-relative imports of `coordinator_core.hooks.support.
      git_common_dir`/`coordinator_core.hooks.support.session_hub`
      (both already ported).

Fail-open on every leg (unchanged from DoE source): unreadable/empty
input, a non-dict payload or tool_input, an unresolvable git root/marker
path, or a missing session_id (no id to dedupe on -> offering
unconditionally would spam rather than nudge, so this degrades to silent,
not to firing every time). No exception anywhere in this module may
propagate — a wedged dispatch is worse than a missed offer.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md, chunk W4-C12.
"""

from __future__ import annotations

import os
import re
from typing import Any

from coordinator_core._hook_envelope import allow_advisory, no_advisory, payload_of
from coordinator_core.hooks.support.git_common_dir import resolve_git_common_dir
from coordinator_core.hooks.support.message_envelope import compose, render
from coordinator_core.hooks.support.session_hub import ensure_session_dir, session_id_is_real
from coordinator_core.ipc import register_op

_EXEMPT_TARGETS = {"explore", "plan"}

_READ_ONLY_RE = re.compile(
    r"\b(find|locate|search|inventory|survey|list|identify)\b"
    r"|which\s+files?"
    r"|where\s+is\b"
    r"|does\b[^.\n]{0,80}\bexist\b",
    re.IGNORECASE,
)

_WRITE_VERB_RE = re.compile(
    r"\b(?:re-?)?(edit|editing|edits|"
    r"writ(?:e|es|ing|ten)|"
    r"creat(?:e|es|ing|ed)|"
    r"fix(?:es|ing|ed)?|"
    r"implement(?:s|ing|ed)?|"
    r"appl(?:y|ies|ying|ied)|"
    r"refactor(?:s|ing|ed)?|"
    r"delet(?:e|es|ing|ed)|"
    r"commit(?:s|ting|ted)?|"
    r"updat(?:e|es|ing|ed)|"
    r"add(?:s|ing|ed)?|"
    r"remov(?:e|es|ing|ed)|"
    r"renam(?:e|es|ing|ed)|"
    r"insert(?:s|ing|ed)?|"
    r"append(?:s|ing|ed)?|"
    r"modif(?:y|ies|ying|ied)|"
    r"mov(?:e|es|ing|ed)|"
    r"bump(?:s|ing|ed)?|"
    r"replac(?:e|es|ing|ed)|"
    r"generat(?:e|es|ing|ed)|"
    r"buil(?:d|ds|ding)|built|"
    r"install(?:s|ing|ed)?|"
    r"configur(?:e|es|ing|ed)|"
    r"patch(?:es|ing|ed)?|"
    r"merg(?:e|es|ing|ed)|"
    r"migrat(?:e|es|ing|ed))\b",
    re.IGNORECASE,
)

_MARKER_NAME = "exploration-tier-dispatch-offered"

#: Wiki section carrying the relocated cost/guarantee explanation this
#: message used to state in full -- see
#: state/relocations/guard-message-cap/offer-exploration-tier-dispatch.py.md.
_WIKI_ANCHOR = (
    "coordinator/docs/wiki/guard-message-concision.md"
    "#unnamed-explore-dispatch-cost-and-guarantee"
)

_OFFER_PROSE = (
    "Read-only-shaped dispatch: an unnamed Explore is cheaper and stays "
    "read-only. Naming it forfeits both -- skip if this needs Edit/Write."
)


def _compose_offer_message():
    return compose(_OFFER_PROSE)


def _is_doctrine_carrying(subagent_type: Any) -> bool:
    """True iff `subagent_type` names a real, non-exempt dispatch target.
    Pure predicate -- no I/O, directly unit-testable."""
    if not isinstance(subagent_type, str):
        return False
    normalized = subagent_type.strip().lower()
    if not normalized:
        return False
    return normalized not in _EXEMPT_TARGETS


def _is_read_only_shaped(prompt: Any) -> bool:
    """True iff `prompt` carries a read-only signal and no write-shaped
    instruction anywhere. Pure predicate -- no I/O, directly unit-testable.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        return False
    if _WRITE_VERB_RE.search(prompt):
        return False
    return bool(_READ_ONLY_RE.search(prompt))


def _git_root(start: str) -> str:
    """No-subprocess walk-up from `start` (falls back to os.getcwd()).
    Fails open to "" on any error."""
    try:
        base = start if isinstance(start, str) and start else os.getcwd()
        if not base:
            return ""
        cur = os.path.abspath(base)
        while True:
            if os.path.exists(os.path.join(cur, ".git")):
                return cur
            parent = os.path.dirname(cur)
            if parent == cur:
                return ""
            cur = parent
    except Exception:
        return ""


def _claim_offer_marker(cwd: str, session_id: str) -> bool:
    """Atomically claim the once-per-session marker via exclusive create.
    Returns True iff THIS call should emit the offer -- either it won the
    exclusive create, or the marker path could not be resolved at all
    (fails open toward offering). Returns False iff the marker already
    exists."""
    try:
        git_root = _git_root(cwd)
        if not git_root:
            return True
        common_dir = resolve_git_common_dir(git_root)
        if not common_dir:
            return True
        session_dir = os.path.join(common_dir, "coordinator-sessions", session_id)
        if not session_id_is_real(session_id):
            return True
        ensure_session_dir(session_dir, session_id)
        marker = os.path.join(session_dir, _MARKER_NAME)
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        except OSError:
            return True
        else:
            os.close(fd)
            return True
    except Exception:
        return True


def _handle(params: dict) -> dict:
    if not isinstance(params, dict):
        return no_advisory()

    if params.get("tool_name") != "Agent":
        return no_advisory()

    tool_input = params.get("tool_input")
    if not isinstance(tool_input, dict):
        return no_advisory()

    subagent_type = tool_input.get("subagent_type")
    if not _is_doctrine_carrying(subagent_type):
        return no_advisory()

    prompt = tool_input.get("prompt")
    if not _is_read_only_shaped(prompt):
        return no_advisory()

    cwd = params.get("cwd")
    if not isinstance(cwd, str):
        cwd = ""
    session_id = params.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return no_advisory()

    if not _claim_offer_marker(cwd, session_id):
        return no_advisory()

    message = render(_compose_offer_message())
    return allow_advisory("PreToolUse", message)


@register_op("hooks.offer_exploration_tier_dispatch")
def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Agent) op: offer an unnamed Explore dispatch (never
    block/deny) for a read-only-shaped dispatch to a doctrine-carrying
    agent, once per session."""
    try:
        # Normalize the two params
        # shapes both engine doors and the cold chain send (see
        # block_worktree_tool).
        return _handle(payload_of(params))
    except Exception:
        return no_advisory()
