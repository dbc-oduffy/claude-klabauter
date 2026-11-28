"""Cold deny-class guard chain: the shim's in-process answer when no engine verdict arrived.

`evaluate(engine_event)` is a prefilter of plain token checks (no regex, no guard import) that names
which BOX_DEGRADING guards could deny the command, then lazily imports only those guards. A command
no deny class could match answers `{}` having imported nothing. Only a deny is forwarded; advisories
and every other guard belong to the warm engine, except the branch-create guard, which is evaluated and traced
so the allow record names it (its verdict is never a deny). A guard that raises is skipped (fails open).
"""
from __future__ import annotations

import contextlib
import importlib
import re
from typing import Any, Callable, Dict, Generator, List, Mapping, NamedTuple, Optional, Tuple

# The registry itself is a heavy import this module avoids; the table below is pinned against
# deny_registry.GUARD_DENY_CLASS and dispatch's GuardEntry names by the ratchet tests.
_PATCH_GUARD_PACKAGE = "coordinator_core.write_guards."
_SHELL_GUARD_PACKAGE = "coordinator_core.bash_guards."
SUBAGENT_DENY_GUARD = "block-subagent-destructive-action"
# Evaluated for the record only: the guard is advisory by ruling, so its verdict is traced and never forwarded as a deny.
BRANCH_GUARD = "block-noncanonical-branch-creation"
ADVISORY_SHELL_GUARDS = (BRANCH_GUARD,)
# Coarse on purpose: which of these commands creates a branch is the guard's own parse, never restated here.
_BRANCH_VERBS = frozenset({"branch", "checkout", "switch"})
# Must hold every deny-class write guard id (a write_guards module named by the id); advisory write guards run warm only.
PATCH_DENY_GUARDS = ("block-drive-root-new-dir-write",)

#: Irreversible-class Bash dispatch guards with no spawn-free, in-budget cold form: guard -> why. They run warm only,
#: and the ratchet reads this mapping so none is ever silently absent from the table.
NO_COLD_FORM = {
    "destructive-git-orphan": "resolves the reset target and counts lost commits with git rev-parse / rev-list, which needs the object store",
    "guard-heavy-command-admission": "denies by caller identity and measured host load, which no command text carries",
    "check-test-suite-invocation": "a 470ms process-time check on the sentinel, over the 200ms cold budget",
}
#: Table guards whose cold form errs open on a shape that needs a process: guard -> the shape. Reported, never dropped.
COLD_PARTIAL = {
    "destructive-git-revert": "a path-scoped revert needs git status to name the affected paths; only the whole-tree forms deny cold",
    "block-external-workspace-vcs-write": "a verb the guard resolves with a git spawn fails open cold",
}

_SHELL_TOOLS = frozenset({"Bash", "PowerShell", "exec_command", "shell"})
_RM_WORDS = frozenset({"rm", "remove-item", "ri", "rd", "del", "erase"})
_FILL_WORDS = frozenset({
    "cp", "rsync", "dd", "truncate", "tar", "zip", "copy-item", "copy", "cpi", "robocopy", "xcopy", "fsutil",
})
_SCAN_WORDS = frozenset({"find", "rg", "grep", "egrep", "fgrep", "fd", "fdfind", "du"})
_PYTEST_WORDS = frozenset({"pytest", "py.test"})
_REVERT_VERBS = frozenset({"checkout", "restore", "reset", "stash", "switch"})
_EDITOR_MARKS = ("unrealeditor", "ue5editor", "shadercompileworker", "livecodingconsole", "unrealtraceserver")
_SCRIPT_MARKS = (".sh", ".bash", ".ps1", ".py", ".cmd", ".bat")
_DETACH_MARKS = ("nohup", "setsid", "disown", "start-process", "saps")
# A lone `&` backgrounds; `&&`, `2>&1` and `&>` do not.
_LONE_AMP = re.compile(r"(?<![&>])&(?![&>])")
_KILL_MARKS = ("kill", "stop-process", "spps", "fuser", ".sh", ".bash", ".ps1", ".py")
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


class _ColdSpawn(BaseException):
    """A guard tried to start a process on the cold path. BaseException so a guard's own `except Exception` cannot swallow it into an allow."""


@contextlib.contextmanager
def _spawn_free(module: Any) -> Generator[None, None, None]:
    """Refuse `subprocess.Popen` and swap the module's `resolve_git_root` for the file-walk resolver; both restored on exit."""
    import subprocess

    refused = subprocess.Popen

    def no_spawn(*args: Any, **kwargs: Any) -> Any:
        raise _ColdSpawn("a guard spawned a process on the cold chain")

    spawning = getattr(module, "resolve_git_root", None)
    setattr(subprocess, "Popen", no_spawn)
    if spawning is not None:
        from coordinator_core.subagent_sandbox.engine import resolve_git_root_cheap

        setattr(module, "resolve_git_root", resolve_git_root_cheap)
    try:
        yield
    finally:
        setattr(subprocess, "Popen", refused)
        if spawning is not None:
            setattr(module, "resolve_git_root", spawning)


def _run_guard(module_name: str, call: Callable[[Any], Any]) -> Any:
    """`call(module)` under the spawn-free swap; a spawn attempt surfaces as a RuntimeError (the chain skips the guard)."""
    module = importlib.import_module(module_name)
    try:
        with _spawn_free(module):
            return call(module)
    except _ColdSpawn as exc:
        raise RuntimeError(str(exc)) from None


def _check_in(name: str) -> Callable[[Dict[str, Any], str], Any]:
    """Invoker running `<bash_guards module for `name`>.check(payload)`."""
    module_name = _SHELL_GUARD_PACKAGE + _MODULE_FOR.get(name, name.replace("-", "_"))
    return lambda payload, command: _run_guard(module_name, lambda m: m.check(payload))


def _invoke_rm(payload: Dict[str, Any], command: str) -> Any:
    # The spawn-free `.git` walk classifier the launcher-timeout backstop also uses; the full guard spawns git.
    from coordinator_core.host_hooks import backstop

    return {"decision": "deny", "reason": backstop.RM_DENY_REASON} if backstop._recursive_rm(command, str(payload.get("cwd") or "")) else None


def _invoke_git_clean(payload: Dict[str, Any], command: str) -> Any:
    """Deny on an untracked load-bearing path the clean would discard (the warm oracle's question), else on any forced clean in a work tree."""
    from coordinator_core.host_hooks import backstop

    cwd = str(payload.get("cwd") or "")
    lost = backstop._git_clean_loses_loadbearing(command, cwd)
    if lost:
        return {"decision": "deny", "reason": backstop.CLEAN_LOADBEARING_REASON % (len(lost), ", ".join(lost[:8]))}
    return {"decision": "deny", "reason": backstop.CLEAN_DENY_REASON} if backstop._git_clean_forced(command, cwd) else None


def _invoke_revert(payload: Dict[str, Any], command: str) -> Any:
    return _run_guard(
        _SHELL_GUARD_PACKAGE + "_dc_git_misc",
        lambda m: m._check_destructive_git_revert_full(command, str(payload.get("session_id") or ""), hook_payload=payload)[0],
    )


def _invoke_runaway_find(payload: Dict[str, Any], command: str) -> Any:
    return _run_guard(
        _SHELL_GUARD_PACKAGE + "_dc_rewrites",
        lambda m: m.check_runaway_find(command, str(payload.get("session_id") or ""), payload=payload),
    )


class ShellGuard(NamedTuple):
    """One cold shell candidate: `wanted(words, lowered command, subagent)` is the token prefilter, `invoke(payload, command)` the spawn-free check."""

    name: str
    wanted: Callable[[frozenset, str, bool], bool]
    invoke: Callable[[Dict[str, Any], str], Any]


def _any_in(text: str, marks: Tuple[str, ...]) -> bool:
    return any(mark in text for mark in marks)


# Guard ids whose bash_guards module is not the id with underscores.
_MODULE_FOR = {"deny-kill-by-port-or-tree": "block_kill_by_port_or_tree"}

#: Chain order. Every irreversible-class Bash dispatch guard is here or in `NO_COLD_FORM` (or `backstop.GUARD_MODULES`,
#: which carries the push/p4 guards); the ratchet fails on one in none. The advisory branch guard is traced, never denied.
SHELL_GUARDS: Tuple[ShellGuard, ...] = (
    ShellGuard(SUBAGENT_DENY_GUARD, lambda w, t, sub: sub and "git" in w, _check_in(SUBAGENT_DENY_GUARD)),
    ShellGuard("destructive-rm", lambda w, t, sub: bool(w & _RM_WORDS), _invoke_rm),
    ShellGuard("destructive-git-clean", lambda w, t, sub: "git" in w and "clean" in w and _forces(w), _invoke_git_clean),
    ShellGuard("block-stash-destruction", lambda w, t, sub: "stash" in w, _check_in("block-stash-destruction")),
    ShellGuard("guard-disk-fill", lambda w, t, sub: bool(w & _FILL_WORDS), _check_in("guard-disk-fill")),
    ShellGuard(BRANCH_GUARD, lambda w, t, sub: "git" in w and bool(w & _BRANCH_VERBS), _check_in(BRANCH_GUARD)),
    ShellGuard("block-worktree-creation", lambda w, t, sub: "worktree" in w, _check_in("block-worktree-creation")),
    ShellGuard("block-whole-filesystem-scan", lambda w, t, sub: bool(w & _SCAN_WORDS), _check_in("block-whole-filesystem-scan")),
    ShellGuard("guard-uncapped-pytest-plugin", lambda w, t, sub: bool(w & _PYTEST_WORDS), _check_in("guard-uncapped-pytest-plugin")),
    ShellGuard("block-external-workspace-vcs-write", lambda w, t, sub: "git" in w, _check_in("block-external-workspace-vcs-write")),
    ShellGuard("destructive-git-revert", lambda w, t, sub: "git" in w and bool(w & _REVERT_VERBS), _invoke_revert),
    ShellGuard("runaway-find", lambda w, t, sub: "find" in w, _invoke_runaway_find),
    ShellGuard("block-editor-kill-by-name", lambda w, t, sub: _any_in(t, _EDITOR_MARKS) or _any_in(t, _SCRIPT_MARKS), _check_in("block-editor-kill-by-name")),
    ShellGuard("deny-kill-by-port-or-tree", lambda w, t, sub: _any_in(t, _KILL_MARKS), _check_in("deny-kill-by-port-or-tree")),
    ShellGuard("refuse-unrecordable-detached-spawn", lambda w, t, sub: _any_in(t, _DETACH_MARKS) or bool(_LONE_AMP.search(t)),_check_in("refuse-unrecordable-detached-spawn")),
    ShellGuard("block-drive-root-new-dir-bash-write", lambda w, t, sub: sub and ("/" in t or "\\" in t), _check_in("block-drive-root-new-dir-bash-write")),
)
_BY_NAME = {guard.name: guard for guard in SHELL_GUARDS}
#: The deny-class shell guard ids the cold chain evaluates (the advisory branch guard is not one).
SHELL_DENY_GUARDS = tuple(g.name for g in SHELL_GUARDS if g.name not in ADVISORY_SHELL_GUARDS)


def shell_candidates(command: str, subagent: bool = False) -> List[str]:
    """The shell guards whose command shape `command` could match, in table order (the subagent guard first, so its verdict names it).

    A subagent's git command also draws the subagent destructive-action guard, whose irreversible
    git verbs (`clean`, `reset --hard`, forced push) the EM-class guards let through.
    """
    words = _words(command)
    lowered = command.lower()
    return [guard.name for guard in SHELL_GUARDS if guard.wanted(words, lowered, subagent)]


def _guard_module(name: str) -> Any:
    """The write_guards module for deny-guard id `name`."""
    return importlib.import_module(_PATCH_GUARD_PACKAGE + name.replace("-", "_"))


def _invoke_shell(name: str, payload: Dict[str, Any], command: str) -> Any:
    return _BY_NAME[name].invoke(payload, command)


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
