"""Feeds each product-path write of a Bash command to the XS lane budget verdict, log-only.

The Edit/Write hook is the only other producer of a lane projection; this guard asks the same
verdict (`_em_write_budget.shell_write_budget`) so a heredoc, `tee`, `sed -i`, `cp` or
interpreter-body write is counted against the lane exactly as an Edit would be. A literal
`cat`/`tee` heredoc body is projected in memory; any other shape is "not projectable". The write
is never refused and nothing reaches the context: any verdict envelope (a deny, a latch, a residue
advisory) is appended to `guard_advisory_counter` under `xs-lane-shell-write` and dropped. No
subprocess: root resolution is a walk and the lane measure reads blobs in process. Fail-open on a
crash.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards._shell_write_projection import ShellWrite

_PREFILTER_TOKENS = (">", "tee", "<<", "-c", "sed", "cp", "mv")
_NOT_PROJECTABLE = "shell write not projectable"
_GUARD_NAME = "xs-lane-shell-write"


def _log(session_id: str, cwd: str) -> None:
    """One advisory-counter record; a failed write never reaches the caller."""
    try:
        from coordinator_core.guard_advisory_counter import record_advisory_fire

        record_advisory_fire(_GUARD_NAME, session_id, cwd or None)
    except Exception:  # noqa: BLE001 -- telemetry must never turn a guard into a crash
        return


def check(cmd: str, session_id: str, cwd: str, payload: Optional[Dict[str, Any]] = None) -> None:
    """Always None: every product write target joins the lane record through the shared verdict
    -- at PreToolUse, so a later guard's deny of the command leaves the path counted -- and a
    verdict envelope is logged, never returned."""
    if not cmd or not (any(tok in cmd for tok in _PREFILTER_TOKENS) or _interpreter_body_write(cmd)):
        return None
    try:
        _check(cmd, session_id, cwd, payload or {})
    except Exception:  # noqa: BLE001 -- fail-open: the Edit/Write hook is the hard gate
        return None
    return None


_INTERPRETER_RE = re.compile(
    r"(?:^|[\s;&|(`])(?:[^\s;&|()]*/)?(?:python[0-9.]*|py|node|nodejs|ruby|perl|php|bash|sh|zsh|deno|bun)(?=\s)"
)
_BODY_WRITE_RE = re.compile(
    r"open\s*\([^)]*,\s*(?:mode\s*=\s*)?['\"][rwxabt+]*[wax+][rwxabt+]*['\"]|write|append|unlink|rename|copy|shutil|File\.|\.dump|truncate|\bsed\s+-i"
)
_BODY_FLAG_LETTERS = frozenset("ceECs")


def _interpreter_body_write(cmd: str) -> bool:
    """True when an interpreter is fed its program by stdin or heredoc (`-`, `-s`) or inline
    (`-c`, `-e`) and the text carries a write marker: such a body can write any path it names."""
    if not _BODY_WRITE_RE.search(cmd):
        return False
    for m in _INTERPRETER_RE.finditer(cmd):
        for tok in cmd[m.end():].split(None, 6)[:6]:
            if tok == "-":
                return True
            if not tok.startswith("-"):
                break
            if tok == "--eval" or (not tok.startswith("--") and _BODY_FLAG_LETTERS.intersection(tok[1:])):
                return True
    return False


_REDIRECT_TARGET_RE = re.compile(r"(?<![<>])(?:&>>?|\d*>\|?|\d*>>)\s*(?!&)([^\s;&|<>()]+)")
_PATH_TOKEN_SPLIT_RE = re.compile(r"[\s'\"()|;&=,{}]+")


def _unprojectable_extras(cmd: str, cwd: str, root: str, found: List[Any]) -> List[Any]:
    """In-repo paths the command names but `shell_writes` did not classify as targets: every
    redirect target, and -- when a heredoc or here-string feeds something other than a literal
    `cat`/`tee` write -- every path-like token, since an interpreter body can write any of them.
    Each joins as a body-less target, so the verdict counts it as unmeasurable."""
    from coordinator_core.bash_guards.write_claim_record import _rel_if_inside

    if any(t.body is not None for t in found):
        return []
    tokens = [m.group(1) for m in _REDIRECT_TARGET_RE.finditer(cmd)]
    if "<<" in cmd or _interpreter_body_write(cmd):
        tokens.extend(_PATH_TOKEN_SPLIT_RE.split(cmd))
    seen = {t.abs_path for t in found}
    out: List[Any] = []
    for raw in tokens:
        tok = raw.strip("'\"").lstrip("<>&|")
        if not tok or tok.startswith("-") or ("/" not in tok and "." not in tok):
            continue
        if "/" not in tok and f"{tok}(" in cmd:
            continue  # a dotted call such as `sys.stdout.write(`, not a file name
        absp = os.path.normpath(tok if os.path.isabs(tok) else os.path.join(cwd, tok))
        if absp in seen or _rel_if_inside(absp, root) is None:
            continue
        seen.add(absp)
        out.append(ShellWrite(absp, None, False))
    return out


def _check(cmd: str, session_id: str, cwd: str, payload: Dict[str, Any]) -> None:
    from coordinator_core.bash_guards._shell_write_projection import shell_writes
    from coordinator_core.hooks._em_write_budget import advised_deny_envelope, shell_write_budget
    from coordinator_core.hooks.nudge_unrouted_sizing import _repo_root
    from coordinator_core.session import xs_lane_budget as xlb

    if "agent_id" in payload or not session_id:
        return
    if cwd and not payload.get("cwd"):
        payload = dict(payload, cwd=cwd)
    root = _repo_root(payload)
    if not root:
        return
    base = cwd or payload.get("cwd") or ""
    targets = list(shell_writes(cmd, base, root))
    targets.extend(_unprojectable_extras(cmd, base, root, targets))

    logged = False
    for target in targets:
        envelope = advised_deny_envelope(root, session_id, (target.abs_path,))
        if envelope is None:
            projected = None
            if target.body is not None:
                projected = xlb.project_shell_write(target.abs_path, target.body, target.append)
            envelope = shell_write_budget(
                payload,
                session_id,
                target.abs_path,
                projected,
                None if projected is not None else _NOT_PROJECTABLE,
            )
        if envelope is not None and not logged:
            _log(session_id, base)
            logged = True
