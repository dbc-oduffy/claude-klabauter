"""coordinator_core.bash_guards.guard_subagent_heavy_ue_launch -- PreToolUse
(Bash|PowerShell) hard-deny: a dispatched agent never launches an Unreal build or editor, an
Electron app or dev server, or (Windows) a 3+ `&` background-shell fan-out.

Port of: coordinator-content-repo coordinator/hooks/scripts/guard-subagent-heavy-ue-launch.py (169088ceb).
UBT, RunUAT, build-plugin.ps1 and UnrealEditor runs are Group EM slot legs (tripwire
AN-EXECUTOR-NEVER-FANS-OUT-HEAVY-RUNS): a compile must never overlap an editor run, and the slot
is granted to the EM. The prose rule failed (DoE bug 763a311d4442), so the launch itself is denied.

DETECTION IS COMMAND POSITION, NOT SUBSTRING. A heavy name run as the program is a launch; the
same name as a grep pattern, `cat` operand, `echo` text, `git log --` pathspec or comment is a
mention and allows. The command splits into segments on unquoted `; && || | & ( )` and newlines,
and each segment's head is peeled through launch wrappers (`&`, `.`, `call`, `start`, `env`,
`cmd /c`, `pwsh -File|-Command`, `bash -c`, `dotnet`) to the program actually run. The local
tokenizer is cold's, kept byte-compatible apart from heredoc stripping (see `launches_heavy`).

KNOWN GAPS (fail open): a program reached through a variable, `Invoke-Expression`, `eval`, an
`xargs`/`-exec` target, a concatenated name, a script that itself launches UBT, and a heredoc
body fed to an interpreter as code (`bash <<EOF`): heredoc bodies are excluded as data.

SCOPED TO SUBAGENTS: any non-empty `agent_id` string, cold's own membership test. The main loop
holds the slot and is never guarded. Fails open on a malformed payload or any exception.
"""

from __future__ import annotations

import re
import sys
from typing import Any, Callable, Dict, List, Optional

from coordinator_core.bash_guards._command_tokenizer import _strip_heredocs
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

#: Declared-heavy Python CLIs (needs_slot): dotted module names for `python -m`, and script stems
#: for `python <script>.py`. A new heavy CLI is declared by adding it here.
_HEAVY_PY_MODULES = frozenset({
    "likeness.dense_map_2d.bfm_operator",
    "likeness.dense_map_2d.overlay",
})
_HEAVY_PY_SCRIPTS = frozenset({"bfm_operator"})

#: Derived from the sets above (last dotted segment / script stem): import-time only, no hot-path cost.
_PY_PREFILTER = tuple(sorted(
    {m.rsplit(".", 1)[-1].lower() for m in _HEAVY_PY_MODULES}
    | {s.lower() for s in _HEAVY_PY_SCRIPTS}
))

_PY_LAUNCHERS = frozenset({"python", "python3", "pythonw", "py"})

_PLAIN_INTERPRETERS = frozenset({"dotnet", "bash", "sh", "zsh", "cmd", "cmd.exe"})
_PWSH_NAMES = frozenset({"pwsh", "powershell"})
_MAX_DEPTH = 4

# No `See:` citation: the tripwire page lives only in coordinator-content-repo, a family B7 bans here.
_REASON = (
    "BLOCKED: Unreal builds and editor runs are Group EM slot legs "
    "(AN-EXECUTOR-NEVER-FANS-OUT-HEAVY-RUNS)."
)

_PY_REASON = "BLOCKED: this Python CLI is a declared needs_slot leg; the Group EM runs it in the slot."


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


def _is_python_launcher(base: str) -> bool:
    if base in _PY_LAUNCHERS:
        return True
    return base.startswith(("python3.", "py-")) and base.replace(".", "").replace("-", "").isalnum()


def _python_runs_heavy(rest: List[str]) -> bool:
    """`rest` are the args after a python launcher: `-m <mod>` or the first `.py` positional."""
    skip = False
    for j, tok in enumerate(rest):
        if skip:
            skip = False
            continue
        if tok in ("-W", "-X"):
            skip = True
            continue
        if tok == "-m" and j + 1 < len(rest):
            return rest[j + 1].lower() in _HEAVY_PY_MODULES
        if tok.startswith("-m") and len(tok) > 2 and not tok.startswith("--"):
            return tok[2:].lower() in _HEAVY_PY_MODULES
        if tok.startswith("-"):
            continue
        name = tok.replace("\\", "/").rsplit("/", 1)[-1].lower()
        return name.endswith(".py") and name[:-3] in _HEAVY_PY_SCRIPTS
    return False


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
    if _is_python_launcher(base) and _python_runs_heavy(rest):
        return True
    if depth >= _MAX_DEPTH:
        return False
    if base in _PWSH_NAMES:
        for j, tok in enumerate(rest):
            low = tok.lower()
            if low in ("-file", "-f") and j + 1 < len(rest):
                return _is_heavy(rest[j + 1])
            if low in ("-command", "-c") and j + 1 < len(rest):
                return launches_heavy(" ".join(rest[j + 1:]), depth + 1)
        return False
    if base in _PLAIN_INTERPRETERS:
        for j, tok in enumerate(rest):
            if tok.lower() in ("-c", "/c", "/k") and j + 1 < len(rest):
                return launches_heavy(" ".join(rest[j + 1:]), depth + 1)
        target = _first_positional(rest)
        return target is not None and _is_heavy(target)
    return False


def launches_heavy(cmd: str, depth: int = 0) -> bool:
    """True when dispatch would deny a subagent `cmd`: the single forbidden-command list.

    Spawn-free and I/O-free; a command naming none of `_PREFILTER` returns after one scan.
    """
    if depth == 0:
        low = cmd.lower()
        if not any(s in low for s in _PREFILTER + _PY_PREFILTER):
            return False
        # The cold copy's byte-compatibility does not cover heredocs: bodies are data here.
        cmd = _strip_heredocs(cmd)
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


_PM_BASES = frozenset({"npm", "pnpm", "yarn", "bun", "npx", "pnpx", "bunx"})
_PM_VALUE_FLAGS = frozenset({"--filter", "-f", "--dir", "-c", "--prefix", "-w", "--workspace", "-p", "--package"})
_PM_WORDS = frozenset({"run", "run-script", "exec", "dlx", "x"})
_DEV_PREFILTER = ("electron", "dev")
_BG_MAX = 2

_DEV_REASON = (
    "BLOCKED: an Electron app or dev server is a Group EM slot leg; a subagent runs the scoped "
    "single-file test instead (python -m coordinator_core.testing.scoped_pytest --allow ... -- ...)."
)
_BG_REASON = (
    "BLOCKED: a background-shell fan-out (%d+ `&` launches, or one `&` inside a loop) is a Group EM slot leg; "
    "run the work sequentially or as one scoped single-file test."
) % (_BG_MAX + 1)


def _segment_launches_dev(tokens: List[str]) -> bool:
    i = 0
    while i < len(tokens) and (tokens[i].lower() in _PASS_THROUGH or _is_env_assign(tokens[i])):
        i += 1
    if i >= len(tokens):
        return False
    base, rest = _base(tokens[i]), tokens[i + 1:]
    if base == "electron":
        return True
    if base not in _PM_BASES:
        return False
    words: List[str] = []
    skip = False
    for tok in rest:
        if skip:
            skip = False
        elif tok.startswith("-"):
            skip = "=" not in tok and tok.lower() in _PM_VALUE_FLAGS
        else:
            words.append(tok.lower())
    while words and words[0] in _PM_WORDS:
        words = words[1:]
    if not words:
        return False
    head = _base(words[0])
    return head == "electron" or head == "dev" or head.startswith(("dev:", "electron:"))


def launches_dev_or_electron(cmd: str, depth: int = 0) -> bool:
    """True when `cmd` runs `electron`, `npx electron`, or a package-manager `dev`/`dev:*`/`electron:*`
    script. Other dev servers (`npx vite`, `npm start`) are not matched."""
    if depth == 0:
        low = cmd.lower()
        if not any(s in low for s in _DEV_PREFILTER):
            return False
        cmd = _strip_heredocs(cmd)
    for tokens in _segments(cmd):
        if _segment_launches_dev(tokens):
            return True
        if depth < _MAX_DEPTH:
            for j, tok in enumerate(tokens):
                if tok.lower() in ("-c", "/c", "/k", "-command") and j > 0 and _base(tokens[0]) in (
                    _PLAIN_INTERPRETERS | _PWSH_NAMES
                ) and j + 1 < len(tokens):
                    if launches_dev_or_electron(" ".join(tokens[j + 1:]), depth + 1):
                        return True
    return False


def background_launches(cmd: str) -> int:
    """Count of unquoted trailing `&` operators (a command sent to the background) in `cmd`."""
    if "&" not in cmd:
        return 0
    cmd = _strip_heredocs(cmd)
    count, quote, prev = 0, "", ""
    n = len(cmd)
    for i, ch in enumerate(cmd):
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "&":
            nxt = cmd[i + 1] if i + 1 < n else ""
            if prev not in ("&", ">", "<", "") and nxt not in ("&", ">") and prev not in (";", "|", "("):
                count += 1
        if ch not in " \t\r":
            prev = ch
        if ch == "\n":
            prev = ""
    return count


_LOOP_RE = re.compile(r"(?:^|[;&|({\n])\s*(?:for|while|until)\s.*?\bdo\b(.*)", re.S)


def _mask_quotes(cmd: str) -> str:
    out: List[str] = []
    quote = ""
    for ch in cmd:
        if quote:
            out.append(ch if ch == quote else " ")
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        else:
            out.append(ch)
    return "".join(out)


def loop_background_fanout(cmd: str) -> bool:
    """True when an unquoted `&` backgrounds work inside a `for|while|until ... do` body: one
    textual `&` that fans out once per iteration."""
    if "&" not in cmd:
        return False
    match = _LOOP_RE.search(_mask_quotes(_strip_heredocs(cmd)))
    if match is None:
        return False
    body = re.split(r"\bdone\b", match.group(1), maxsplit=1)[0]
    return background_launches(body) >= 1


def _storm_guard_applies() -> bool:
    """The background-fan-out deny is Windows-only: process creation there is the box cost."""
    return sys.platform == "win32"


def check(
    payload: Dict[str, Any],
    resolve_wiki_citation: Optional[Callable[[str], str]] = None,
) -> Any:
    """`None` (allow) or the hard-deny record. `resolve_wiki_citation` is accepted for the
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
        if launches_heavy(cmd):
            reason = _REASON if any(s in cmd.lower() for s in _PREFILTER) else _PY_REASON
        elif launches_dev_or_electron(cmd):
            reason = _DEV_REASON
        elif _storm_guard_applies() and (background_launches(cmd) > _BG_MAX or loop_background_fanout(cmd)):
            reason = _BG_REASON
        else:
            return None
    except Exception:
        return None
    from coordinator_core.bash_guards._rewrite_support import _deny_record, _em_stamped
    from coordinator_core.guard_messages.alternative_record import EscalationRoute

    return _deny_record(
        "guard-subagent-heavy-ue-launch",
        reason,
        (
            EscalationRoute(
                report_what="the run as a needs_slot leg",
                authorize_what="the Group EM launching it in the slot",
            ),
        ),
        em_text=_em_stamped(reason + " Report the run as a needs_slot leg."),
    )
