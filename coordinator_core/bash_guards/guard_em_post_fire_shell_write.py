"""Denies a Bash write to a tracked product path once the EM session has fired a Workflow run.

The Bash twin of the Edit/Write post-fire deny: both decide through `_em_post_fire`, whose
`GUARD_NAME` is a floor guard, so this entry denies at every guard level. One tmp read answers
"has this session fired" before any command parsing; an unfired session, a subagent payload
and an empty session id fall through to the pre-fire budget guards. No subprocess.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

_PREFILTER_TOKENS = (">", "tee", "<<", "-c", "sed", "cp", "mv", "git", "rm", "perl", "patch", "truncate", "dd", "rsync", "install")
_REVISE_ROUTE = "workflow.revise"


def _record(text: str) -> Any:
    from coordinator_core.bash_guards._rewrite_support import _alt_record
    from coordinator_core.guard_messages.alternative_record import AdaptableOffer
    from coordinator_core.hooks._em_post_fire import GUARD_NAME

    return _alt_record(
        "deny",
        GUARD_NAME,
        text,
        (AdaptableOffer(template=_REVISE_ROUTE, what_to_adapt="the gap"),),
        em_text=text,
    )


def check(cmd: str, session_id: str, cwd: str, payload: Optional[Dict[str, Any]] = None) -> Any:
    """An `AlternativeRecord` deny for the first product write target of `cmd` in a fired EM
    session, else None. Fail-open on a crash; the Edit/Write hook is the other gate."""
    payload = payload or {}
    if not cmd or "agent_id" in payload or not session_id:
        return None
    if not any(tok in cmd for tok in _PREFILTER_TOKENS):
        return None
    try:
        return _check(cmd, session_id, cwd, payload)
    except Exception:  # noqa: BLE001 -- fail-open
        return None


_COMPOUND_MARKERS = ("&", ";", "|", "\n", "`", "$(", ">", "<")


def _is_plain_rm(cmd: str) -> bool:
    """True for one simple `rm` segment: no chaining, redirection or substitution, no recursive or
    directory flag."""
    import shlex

    if any(m in cmd for m in _COMPOUND_MARKERS):
        return False
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return False
    if len(tokens) < 2 or tokens[0] != "rm":
        return False
    for tok in tokens[1:]:
        if tok == "--":
            break
        if tok in ("--recursive", "--dir"):
            return False
        if tok.startswith("-") and not tok.startswith("--") and any(c in tok for c in "rRd"):
            return False
    return True


def _check(cmd: str, session_id: str, cwd: str, payload: Dict[str, Any]) -> Any:
    from coordinator_core.bash_guards._shell_write_projection import shell_writes
    from coordinator_core.hooks import _em_post_fire
    from coordinator_core.hooks.nudge_unrouted_sizing import _repo_root

    if not _em_post_fire.fired(session_id):
        return None
    if cwd and not payload.get("cwd"):
        payload = dict(payload, cwd=cwd)
    root = _repo_root(payload)
    if not root:
        return None
    targets = shell_writes(cmd, cwd or payload.get("cwd") or "", root, with_git=True)
    if targets and _is_plain_rm(cmd) and all(
        _em_post_fire.is_empty_untracked(t.abs_path, root) for t in targets
    ):
        return None
    for target in targets:
        if _em_post_fire.is_product_path(target.abs_path, root, session_id):
            envelope = _em_post_fire.deny_envelope() or {}
            out = envelope.get("hookSpecificOutput") or {}
            text = out.get("permissionDecisionReason") or out.get("additionalContext") or ""
            return _record(text)
    return None
