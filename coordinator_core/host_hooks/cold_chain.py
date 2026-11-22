"""Cold deny-class guard chain: the shim's in-process answer when no engine verdict arrived.

`evaluate(engine_event)` is a prefilter of plain token checks (no regex, no guard import) that names
which BOX_DEGRADING guards could deny the command, then lazily imports only those guards. A command
no deny class could match answers `{}` having imported nothing. Only a deny is forwarded; advisories
and every other guard belong to the warm engine, except the branch-create guard, which is evaluated and traced
so the allow record names it (its verdict is never a deny). A guard that raises is skipped (fails open).
"""
from __future__ import annotations

import importlib
from typing import Any, Dict, List, Mapping, Optional

# Pinned against deny_registry.GUARD_DENY_CLASS (BOX_DEGRADING) by tests/test_cold_chain.py; the
# registry itself is a heavy import this module avoids.
_PATCH_GUARD_PACKAGE = "coordinator_core.write_guards."
SHELL_DENY_GUARDS = ("destructive-rm", "destructive-git-clean", "block-stash-destruction", "guard-disk-fill")
SUBAGENT_DENY_GUARD = "block-subagent-destructive-action"
# Evaluated for the record only: the guard is advisory by ruling, so its verdict is traced and never forwarded as a deny.
BRANCH_GUARD = "block-noncanonical-branch-creation"
ADVISORY_SHELL_GUARDS = (BRANCH_GUARD,)
# Coarse on purpose: which of these commands creates a branch is the guard's own parse, never restated here.
_BRANCH_VERBS = frozenset({"branch", "checkout", "switch"})
# Must hold every deny-class write guard id (a write_guards module named by the id); advisory write guards run warm only.
PATCH_DENY_GUARDS = ("block-drive-root-new-dir-write",)

_SHELL_TOOLS = frozenset({"Bash", "PowerShell", "exec_command", "shell"})
_RM_WORDS = frozenset({"rm", "remove-item", "ri", "rd", "del", "erase"})
_FILL_WORDS = frozenset({
    "cp", "rsync", "dd", "truncate", "tar", "zip", "copy-item", "copy", "cpi", "robocopy", "xcopy", "fsutil",
})
_PATCH_MARKERS = ("*** Add File:", "*** Move to:")

#: Per-guard outcomes of the last `evaluate`, in run order: [(guard id, "allow"|"deny"|"raised")]. Read only through
#: `take_trace`, which clears it; `evaluate` resets it first.
_trace: List[tuple] = []


def take_trace() -> List[tuple]:
    """The last `evaluate`'s ordered (guard, verdict) pairs, cleared; empty when no guard ran."""
    taken = list(_trace)
    del _trace[:]
    return taken


_SEPARATORS = str.maketrans({c: " " for c in ";&|()`${}<>\n\r\t"})
_QUOTES = str.maketrans("", "", "'\"")


def _words(command: str) -> frozenset:
    """Lowercased executable-basename words of `command`; quote-stripped so `'r''m'` reads `rm`.

    Backslash is read both as a path separator and as an escape (dropped), so a Windows `...\\git.exe`
    path and `r\\m` each yield their word.
    """
    flat = command.lower().translate(_QUOTES)
    found = set()
    for text in (flat.replace("\\", "/"), flat.replace("\\", "")):
        for token in text.translate(_SEPARATORS).split():
            word = token.rsplit("/", 1)[-1]
            found.add(word[:-4] if word.endswith(".exe") else word)
    return frozenset(found)


def _forces(words: frozenset) -> bool:
    """True when a word is `--force` or a short-flag cluster holding `f`: `git clean` refuses to remove without one."""
    return "--force" in words or any(w[:1] == "-" and w[1:2] != "-" and "f" in w for w in words)


def shell_candidates(command: str, subagent: bool = False) -> List[str]:
    """The shell deny guards whose command shape `command` could match, in chain order (the subagent guard first, so its verdict names it).

    A subagent's git command also draws the subagent destructive-action guard, whose irreversible
    git verbs (`clean`, `reset --hard`, forced push) the EM-class guards above let through.
    """
    words = _words(command)
    hits: List[str] = [SUBAGENT_DENY_GUARD] if subagent and "git" in words else []
    if words & _RM_WORDS:
        hits.append("destructive-rm")
    if "git" in words and "clean" in words and _forces(words):
        hits.append("destructive-git-clean")
    if "stash" in words:
        hits.append("block-stash-destruction")
    if words & _FILL_WORDS:
        hits.append("guard-disk-fill")
    if "git" in words and words & _BRANCH_VERBS:
        hits.append(BRANCH_GUARD)
    return hits


def _guard_module(name: str) -> Any:
    """The write_guards module for deny-guard id `name`."""
    return importlib.import_module(_PATCH_GUARD_PACKAGE + name.replace("-", "_"))


def _invoke_shell(name: str, payload: Dict[str, Any], command: str) -> Any:
    if name in ("destructive-rm", "destructive-git-clean"):
        # The spawn-free `.git` walk classifiers the launcher-timeout backstop also uses; the full guards spawn git.
        from coordinator_core.host_hooks import backstop

        cwd = str(payload.get("cwd") or "")
        if name == "destructive-rm":
            return {"decision": "deny", "reason": backstop.RM_DENY_REASON} if backstop._recursive_rm(command, cwd) else None
        return {"decision": "deny", "reason": backstop.CLEAN_DENY_REASON} if backstop._git_clean_forced(command, cwd) else None
    if name == "block-stash-destruction":
        from coordinator_core.bash_guards.block_stash_destruction import check

        return check(payload)
    if name == SUBAGENT_DENY_GUARD:
        from coordinator_core.bash_guards.block_subagent_destructive_action import check as subagent_check

        return subagent_check(payload)
    if name == BRANCH_GUARD:
        from coordinator_core.bash_guards import block_noncanonical_branch_creation as branch
        from coordinator_core.subagent_sandbox.engine import resolve_git_root_cheap

        # Trap: the guard's own root lookup spawns `git rev-parse`; the cold chain is spawn-free, so the
        # file-walk resolver stands in for the call and is restored after it.
        spawning = branch.resolve_git_root
        setattr(branch, "resolve_git_root", resolve_git_root_cheap)
        try:
            return branch.check(payload)
        finally:
            setattr(branch, "resolve_git_root", spawning)
    from coordinator_core.bash_guards.guard_disk_fill import check

    return check(payload)


def _flat_deny(result: Any, guard: str) -> Optional[Dict[str, Any]]:
    """The flat deny verdict for a guard result (record or envelope), else None; the reason names `guard`."""
    if result is None:
        return None
    if not isinstance(result, Mapping):
        from coordinator_core.guard_messages.alternative_record import Audience
        from coordinator_core.guard_messages.render import render

        result = render(result, Audience.EM)
    nested = result.get("hookSpecificOutput")
    body = nested if isinstance(nested, Mapping) else result
    if (body.get("permissionDecision") or body.get("decision")) != "deny":
        return None
    reason = body.get("permissionDecisionReason") or body.get("reason") or "denied by coordinator guard"
    if "Guard: `" not in reason:
        reason = "%s\n\nGuard: `%s`." % (str(reason).rstrip(), guard)
    return {"permissionDecision": "deny", "permissionDecisionReason": reason}


def _shell(engine_event: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    tool_input = engine_event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, Mapping) else None
    if not isinstance(command, str) or not command.strip():
        return {}
    candidates = shell_candidates(command, subagent=bool(engine_event.get("agent_id")))
    if not candidates:
        return {}
    payload = dict(engine_event)
    if payload.get("tool_name") in ("exec_command", "shell"):
        payload["tool_name"] = "Bash"
    completed = False
    for name in candidates:
        try:
            result = _invoke_shell(name, payload, command)
            advisory = name in ADVISORY_SHELL_GUARDS
            verdict = None if advisory else _flat_deny(result, name)
        except Exception:  # noqa: BLE001 -- a raising guard is skipped, never a hook failure
            _trace.append((name, "raised"))
            continue
        completed = True
        # An advisory guard's non-None result (even a deny record) is traced "advisory": it is never forwarded.
        _trace.append((name, "deny" if verdict is not None else "advisory" if advisory and result is not None else "allow"))
        if verdict is not None:
            return verdict
    return {} if completed else None


def _patch(engine_event: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    tool_input = engine_event.get("tool_input")
    patch = tool_input.get("command") if isinstance(tool_input, Mapping) else None
    if not isinstance(patch, str) or not any(marker in patch for marker in _PATCH_MARKERS):
        return {}
    try:
        from coordinator_core.host_hooks.codex_tools import patch_events

        events, _warnings = patch_events(dict(engine_event))
    except Exception:  # noqa: BLE001
        return None
    checks = []
    for name in PATCH_DENY_GUARDS:
        try:
            checks.append((name, _guard_module(name).check))
        except Exception:  # noqa: BLE001 -- an unimportable guard is skipped, never a hook failure
            continue
    if events and not checks:
        return None
    completed = False
    for event in events:
        for name, check in checks:
            try:
                verdict = _flat_deny(check(event), name)
            except Exception:  # noqa: BLE001
                _trace.append((name, "raised"))
                continue
            completed = True
            _trace.append((name, "allow" if verdict is None else "deny"))
            if verdict is not None:
                return verdict
    return {} if completed or not events else None


def evaluate(engine_event: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """A flat deny verdict, `{}` for allow, or None when the chain did not run (other tool, every guard raised)."""
    del _trace[:]
    tool = engine_event.get("tool_name")
    if tool in _SHELL_TOOLS:
        return _shell(engine_event)
    if tool == "apply_patch":
        return _patch(engine_event)
    return None
