"""coordinator_core.bash_guards.guard_subagent_heavy_ue_launch -- PreToolUse
(Bash|PowerShell) hard-deny: a dispatched agent never launches an Unreal build or editor.

Port of: coordinator-content-repo coordinator/hooks/scripts/guard-subagent-heavy-ue-launch.py (169088ceb).
UBT, RunUAT, build-plugin.ps1 and UnrealEditor runs are Group EM slot legs (tripwire
AN-EXECUTOR-NEVER-FANS-OUT-HEAVY-RUNS): a compile must never overlap an editor run, and the slot
is granted to the EM. The prose rule failed (DoE bug 763a311d4442), so the launch itself is denied.

DETECTION IS COMMAND POSITION, NOT SUBSTRING. A heavy name run as the program is a launch; the
same name as a grep pattern, `cat` operand, `echo` text, `git log --` pathspec or comment is a
mention and allows. The command splits into segments on unquoted `; && || | & ( )` and newlines,
and each segment's head is peeled through launch wrappers (`&`, `.`, `call`, `start`, `env`,
`cmd /c`, `pwsh -File|-Command`, `bash -c`, `dotnet`) to the program actually run. The local
tokenizer is cold's, kept byte-compatible so both paths judge a command alike.

KNOWN GAPS (fail open): a program reached through a variable, `Invoke-Expression`, `eval`, an
`xargs`/`-exec` target, a concatenated name, and a script that itself launches UBT.

SCOPED TO SUBAGENTS: any non-empty `agent_id` string, cold's own membership test. The main loop
holds the slot and is never guarded. Fails open on a malformed payload or any exception.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from coordinator_core._hook_envelope import deny as _deny
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
GENERATES: List[str] = []

#: Lowercased basenames, `.exe` stripped, that are a heavy UE launch when run.
_HEAVY_NAMES = frozenset({
    "unrealbuildtool", "unrealbuildtool.dll",
    "build.bat",
    "runuat", "runuat.bat", "runuat.sh", "runuat.command",
    "build-plugin.ps1",
    "unrealeditor", "unrealeditor-cmd",
})

#: Words that precede the real program without being it.
_PASS_THROUGH = frozenset({
    "&", ".", "call", "start", "start-process", "env", "sudo", "time", "nohup", "exec", "command",
    "builtin", "nice", "stdbuf", "{", "}", "!", "if", "then", "else", "elif", "do", "while",
    "until", "-and", "-or", "invoke-command",
})

#: Lowercased substrings one of which every heavy launch contains: a command carrying none
#: skips the tokenizer, so the common case costs one scan.
_PREFILTER = ("unrealbuildtool", "build.bat", "runuat", "build-plugin", "unrealeditor")

_PLAIN_INTERPRETERS = frozenset({"dotnet", "bash", "sh", "zsh", "cmd", "cmd.exe"})
_PWSH_NAMES = frozenset({"pwsh", "powershell"})
_MAX_DEPTH = 4

# No `See:` citation: the tripwire page lives only in coordinator-content-repo, a family B7 bans here.
_REASON = (
    "BLOCKED: Unreal builds and editor runs are Group EM slot legs "
    "(AN-EXECUTOR-NEVER-FANS-OUT-HEAVY-RUNS). Report the run as a needs_slot leg."
)


def _segments(cmd: str) -> List[List[str]]:
    """Split `cmd` into segments of unquoted-token lists; quotes removed, comments dropped."""
    segments: List[List[str]] = []
    tokens: List[str] = []
    cur: List[str] = []
    have_tok = False
    quote = ""
    i, n = 0, len(cmd)

    def end_tok() -> None:
        nonlocal cur, have_tok
        if have_tok:
            tokens.append("".join(cur))
        cur, have_tok = [], False

    def end_seg() -> None:
        nonlocal tokens
        end_tok()
        if tokens:
            segments.append(tokens)
        tokens = []

    while i < n:
        ch = cmd[i]
        if quote:
            if ch == quote:
                quote = ""
            else:
                cur.append(ch)
            i += 1
            continue
        if ch in "\"'":
            quote, have_tok = ch, True
            i += 1
            continue
        if ch == "#" and not have_tok:
            while i < n and cmd[i] != "\n":
                i += 1
            continue
        if ch in " \t\r":
            end_tok()
        elif ch in ";\n|()":
            end_seg()
        elif ch == "&":
            if cmd[i:i + 2] == "&&":
                end_seg()
                i += 1
            elif not have_tok and not tokens:
                tokens.append("&")  # PowerShell call operator in command position
            else:
                end_seg()
        else:
            cur.append(ch)
            have_tok = True
        i += 1
    end_seg()
    return segments


def _base(token: str) -> str:
    name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _is_heavy(token: str) -> bool:
    return _base(token) in _HEAVY_NAMES


def _is_env_assign(token: str) -> bool:
    head, eq, _ = token.partition("=")
    return bool(eq) and head.replace("_", "a").isalnum() and not head[:1].isdigit()


def _first_positional(tokens: List[str]) -> Optional[str]:
    for tok in tokens:
        if tok and not tok.startswith("-") and tok.lower() not in ("/c", "/k", "/s", "/q", "exec"):
            return tok
    return None


def _segment_launches_heavy(tokens: List[str], depth: int) -> bool:
    i = 0
    while i < len(tokens) and (tokens[i].lower() in _PASS_THROUGH or _is_env_assign(tokens[i])):
        i += 1
    if i >= len(tokens):
        return False
    head, rest = tokens[i], tokens[i + 1:]
    if _is_heavy(head):
        return True
    base = _base(head)
    if depth >= _MAX_DEPTH:
        return False
    if base in _PWSH_NAMES:
        for j, tok in enumerate(rest):
            low = tok.lower()
            if low in ("-file", "-f") and j + 1 < len(rest):
                return _is_heavy(rest[j + 1])
            if low in ("-command", "-c") and j + 1 < len(rest):
                return _launches_heavy(" ".join(rest[j + 1:]), depth + 1)
        return False
    if base in _PLAIN_INTERPRETERS:
        for j, tok in enumerate(rest):
            if tok.lower() in ("-c", "/c", "/k") and j + 1 < len(rest):
                return _launches_heavy(" ".join(rest[j + 1:]), depth + 1)
        target = _first_positional(rest)
        return target is not None and _is_heavy(target)
    return False


def _launches_heavy(cmd: str, depth: int = 0) -> bool:
    for tokens in _segments(cmd):
        if _segment_launches_heavy(tokens, depth):
            return True
        # Start-Process/start take the program as -FilePath or first positional.
        lowered = [t.lower() for t in tokens]
        if lowered and lowered[0] in ("start-process", "start") or (
            len(lowered) > 1 and lowered[0] == "&" and lowered[1] in ("start-process", "start")
        ):
            for j, low in enumerate(lowered):
                if low == "-filepath" and j + 1 < len(tokens) and _is_heavy(tokens[j + 1]):
                    return True
            positional = _first_positional(tokens[2 if lowered[0] == "&" else 1:])
            if positional is not None and _is_heavy(positional):
                return True
    return False


def check(
    payload: Dict[str, Any],
    resolve_wiki_citation: Optional[Callable[[str], str]] = None,
) -> Optional[Dict[str, Any]]:
    """`None` (allow) or the hard-deny envelope. `resolve_wiki_citation` is accepted for the
    chain's call signature and unused: the deny carries no citation (see `_REASON`)."""
    del resolve_wiki_citation
    try:
        if not isinstance(payload, dict) or payload.get("tool_name") not in MATCHERS:
            return None
        agent_id = payload.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id.strip():
            return None
        tool_input = payload.get("tool_input")
        cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
        if not isinstance(cmd, str) or not cmd.strip():
            return None
        low = cmd.lower()
        if not any(s in low for s in _PREFILTER) or not _launches_heavy(cmd):
            return None
    except Exception:
        return None
    return _deny("PreToolUse", _REASON)
