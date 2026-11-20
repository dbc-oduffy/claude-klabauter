"""coordinator_core.bash_guards.block_kill_by_port_or_tree -- PreToolUse(Bash,
PowerShell) deny: no kill chosen by port, name or process tree.

A kill selected by listening port, by process name, or by `taskkill /T` (which
walks ParentProcessId; stale or reused parent pids reach foreign trees) can end
a job the session never spawned. Denied, per statement (`;`, `&&`, `||`, `|`,
`&`, newline, `$(...)`, and `-c`/`-Command`/`eval`/`cmd /c` bodies are parsed
too):
  - `taskkill` with /T.
  - `taskkill /IM|/FI`, `pkill`, `killall`, `fuser -k`, `Stop-Process -Name`.
  - `kill`, `taskkill /PID` or `Stop-Process -Id` fed by a port or name lookup
    (netstat, Get-NetTCPConnection, lsof, pgrep, pidof, tasklist, Get-Process
    other than `-Id`, Win32_Process, psutil.process_iter): the lookup sits on
    the kill's line, or the kill's pid variable derives from one. In a Python
    script a kill beside any lookup still denies.
Allowed: `kill $!`, `Stop-Process -Id $proc.Id` from Start-Process,
`taskkill /PID <pid>` without /T.

A statement that runs a .sh/.ps1/.py file written within `_SCRIPT_WINDOW_S` has
that one file read and held to the same patterns. Heredoc bodies are scanned
only when fed to a shell, interpreter or script file.

In-process, no spawn: `_PREFILTER` returns at once on text naming no kill verb
and no script, before any file read.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Dict

from coordinator_core.bash_guards._rewrite_support import _override
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 43

GUARD_NAME = "deny-kill-by-port-or-tree"
ANCHOR = "DENY-KILL-BY-PORT-OR-TREE"
OVERRIDE_ENV = "COORDINATOR_ALLOW_KILL_BY_PORT_OR_TREE"

#: Lowercase substrings a command must contain for this guard to possibly fire;
#: the crash-path trigger table reads them.
COMMAND_WORDS = ("kill", "stop-process", "spps", "fuser", ".sh", ".bash", ".ps1", ".py")

_SCRIPT_WINDOW_S = 12 * 3600
_SCRIPT_MAX_BYTES = 262144

_PREFILTER = re.compile(r"kill|stop-process|spps|fuser|\.(?:sh|bash|ps1|py)\b", re.I)
_HEREDOC = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")
_BODY_RUNS = re.compile(
    # The interpreter must stand as a word, not a file extension: "-- guard.py <<EOF" feeds git, not python.
    r">>?\s*\S*\.(?:sh|bash|ps1|py)\b|(?<![\w./\\-])(?:bash|sh|zsh|pwsh|powershell|python3?|py|tee)\b", re.I)
# Trap: `-c` opens an inline body only after an interpreter; `rg -c "a|Stop-Process"` is a count
# flag, and eating its quote would split the pattern into kill statements.
_EXEC_BODY = re.compile(
    r"(?:(?<![\w.-])(?:bash|sh|zsh|dash|pwsh|powershell|python3?|py)(?:\.exe)?\b[^;|&\n]*?\s-(?:c|command)"
    r"|\beval|\bcmd(?:\.exe)?\s+/[ck])\s+[\"']?", re.I)
_PS_BLOCK_COMMENT = re.compile(r"<#.*?#>", re.S)
_COMMENT_LINE = re.compile(r"^[ \t]*#.*$", re.M)
_SPLIT = re.compile(r"\r?\n|;|&&|\|\||\||&|\$\(|[(){}`]")
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAP = {"sudo", "env", "nohup", "time", "command", "exec", "xargs", "timeout", "nice", "call",
         "start", "then", "do", "else", "if", "while", "until", "!", "setsid"}
_XARGS_VAL = {"-n", "-I", "-P", "-L", "-d", "-E", "-s", "-a"}
_TREE_ARGS = {"/t", "//t", "-t"}
_NAME_ARGS = {"/im", "//im", "-im", "/fi", "//fi", "-fi"}
_PS_NAME = re.compile(r"^-n(?:a(?:m(?:e)?)?)?$", re.I)
_SCRIPT_EXT = {"bash": (".sh", ".bash"), "sh": (".sh", ".bash"), "zsh": (".sh", ".bash"),
               "dash": (".sh", ".bash"), "source": (".sh", ".bash"), ".": (".sh", ".bash", ".ps1"),
               "pwsh": (".ps1",), "powershell": (".ps1",), "python": (".py",), "python3": (".py",),
               "py": (".py",)}
_SOURCE = re.compile(
    r"\bnetstat\b|\bGet-Net(?:TCP|UDP)Connection\b|\bOwningProcess\b|\blsof\b|\bpgrep\b|\bpidof\b"
    r"|\btasklist\b|\bGet-Process\b(?!\s+-(?:Id|InputObject)\b)|\bgps\b|\bWin32_Process\b|\bwmic\b"
    r"|\bprocess_iter\b|\bnet_connections\b|\.connections\s*\(", re.I)
_PY_SINK = re.compile(r"\bos\.kill(?:pg)?\s*\(|\.(?:kill|terminate)\s*\(\s*\)|\btaskkill\b|Stop-Process", re.I)
_PY_NAMED = re.compile(r"\b(?:pkill|killall)\b|Stop-Process\s+(?:\S+\s+)*?-n(?:ame)?\b", re.I)
_PY_ARG_BOUND = r"(?:^|[\s\"',\[(])"
_PY_TREE = re.compile(_PY_ARG_BOUND + r"[/-]{1,2}t(?=$|[\s\"',\])])", re.I)
_PY_NAME = re.compile(_PY_ARG_BOUND + r"[/-]{1,2}(?:im|fi)(?=$|[\s\"',\])])", re.I)

_QUOTED = re.compile(r"'[^'\n]*'|\"[^\"\n]*\"")
_QUOTED_SEP = re.compile(r";|&&|\|\||\||&|[(){}`]")
_TAINT_SPLIT = re.compile(r"\r?\n|;|&&|\|\|")
# bash NAME=..., PowerShell $name = / += ... ; group 3 is the right-hand side.
_ASSIGN = re.compile(
    r"(?:(?:^|[\s{(])(?:export\s+|local\s+|readonly\s+)?([A-Za-z_]\w*)=(?!=)"
    r"|\$([A-Za-z_]\w*)\s*[+-]?=(?!=))(.*)", re.I)
# A PowerShell foreach-in loop header and a bash for-in loop header: group 1/3 the loop var, 2/4 its source.
_LOOP = re.compile(r"\bforeach\s*\(\s*\$(\w+)\s+in\s+(.*)|\bfor\s+(\w+)\s+in\s+(.*)", re.I)
_VAR_REF = re.compile(r"\$\{?([A-Za-z_]\w*)")

_TREE_LABEL = "taskkill /T (kills a process tree through ParentProcessId)"
_NAME_LABEL = "a kill that selects processes by name"


def _strip_heredocs(text: str) -> str:
    out: list[str] = []
    end: str | None = None
    keep = False
    for line in text.split("\n"):
        if end is not None:
            if line.strip() == end:
                end = None
            elif keep:
                out.append(line)
            continue
        out.append(line)
        m = _HEREDOC.search(line)
        if m and "<<<" not in line:
            end, keep = m.group(1), bool(_BODY_RUNS.search(line))
    return "\n".join(out)


def _head(tokens: list[str]) -> tuple[str, str, list[str]]:
    """(command basename, raw command token, argv) after env assignments and wrapper words."""
    i, n = 0, len(tokens)
    while i < n:
        t = tokens[i]
        if _ENV.match(t):
            i += 1
        elif t in _WRAP:
            i += 1
            if t == "xargs":
                while i < n and tokens[i].startswith("-"):
                    i += 2 if tokens[i] in _XARGS_VAL else 1
            else:
                while i < n and tokens[i].startswith("-") and len(tokens[i]) > 1:
                    i += 1
                if t == "timeout" and i < n:
                    i += 1
        else:
            break
    if i >= n:
        return "", "", []
    name = re.split(r"[\\/]", tokens[i])[-1].lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name, tokens[i], tokens[i + 1:]


def _classify(name: str, args: list[str]) -> str | None:
    """A deny label, "plain" for a kill that needs a pid-provenance check, or None."""
    low = [a.lower() for a in args]
    if name == "taskkill":
        if any(a in _TREE_ARGS for a in low):
            return _TREE_LABEL
        if any(a in _NAME_ARGS for a in low):
            return "taskkill selecting by image name or filter"
        return "plain"
    if name in ("pkill", "killall"):
        return f"{name}, which selects processes by name"
    if name == "fuser" and any(re.match(r"^-[a-z]*k", a) for a in low):
        return "fuser -k, which kills whatever holds a port or file"
    if name in ("stop-process", "spps"):
        if any(_PS_NAME.match(a) for a in args):
            return "Stop-Process -Name, which kills every process of that name"
        return "plain"
    if name == "kill":
        return "plain"
    return None


def _script_arg(name: str, raw: str, args: list[str]) -> str | None:
    exts = (".sh", ".bash", ".ps1", ".py")
    if name not in _SCRIPT_EXT:
        return raw if raw.lower().endswith(exts) and not raw.startswith("-") else None
    if name in ("python", "python3", "py") and any(a in ("-m", "-c") for a in args):
        return None
    for a in args:
        if not a.startswith("-") and a.lower().endswith(_SCRIPT_EXT[name]):
            return a
    return None


def _mask_quoted_separators(text: str) -> str:
    """Blank statement separators inside quoted spans: `rg "a|Stop-Process"` is one statement.

    An assignment's quoted value (`F="a.py b.py"`) is also glued into one token, so its words are
    never read as the command the statement runs.
    """
    def one(m: re.Match) -> str:
        span = _QUOTED_SEP.sub(" ", m.group(0))
        if m.start() and text[m.start() - 1] == "=":
            span = re.sub(r"\s", "\x00", span)  # NUL: str.split() treats \x1c-\x1f as whitespace
        return span
    return _QUOTED.sub(one, text)


def _tainted_vars(text: str) -> set[str]:
    """Variables whose value derives from a port or name lookup, to a fixpoint.

    Trap: taint follows assignment and loop binding, never a function call. A pid list returned by a
    function whose body walks the process table (leg-drive's birth-gated Get-SpawnTreeIds) is not
    tainted; that walk is judged at its own kill sites, not by file-wide co-occurrence.
    """
    pairs: list[tuple[str, str]] = []
    for chunk in _TAINT_SPLIT.split(text):
        for m in _ASSIGN.finditer(chunk):
            pairs.append(((m.group(1) or m.group(2)).lower(), m.group(3)))
        for m in _LOOP.finditer(chunk):
            pairs.append(((m.group(1) or m.group(3)).lower(), m.group(2) or m.group(4)))
    tainted: set[str] = set()
    changed = True
    while changed:
        changed = False
        for var, rhs in pairs:
            if var in tainted:
                continue
            if _SOURCE.search(rhs) or {v.lower() for v in _VAR_REF.findall(rhs)} & tainted:
                tainted.add(var)
                changed = True
    return tainted


def _scan_shell(text: str) -> tuple[str | None, list[str]]:
    """(deny label, script paths the statements run) for shell or PowerShell text.

    A plain kill (`kill`, `taskkill /PID`, `Stop-Process -Id`) is denied only when a lookup feeds
    it: a port or name lookup on the kill's own line, or a pid variable derived from one. A lookup
    elsewhere in the same script, read for evidence or a birth-gated walk, does not deny it.
    """
    # Comments go before the split: it breaks on ( ) and ;, so a "# ... (taskkill /T)" line would
    # otherwise yield a bare "taskkill /T" statement.
    text = _COMMENT_LINE.sub("", _PS_BLOCK_COMMENT.sub("", _strip_heredocs(text)))
    text = _mask_quoted_separators(_EXEC_BODY.sub(" ; ", text))
    tainted: set[str] | None = None
    scripts: list[str] = []
    for line in text.split("\n"):
        for stmt in _SPLIT.split(line):
            tokens = [t.strip("\"'") for t in stmt.split()]
            tokens = [t for t in tokens if t]
            if not tokens or tokens[0].startswith("#"):
                continue
            name, raw, args = _head(tokens)
            if not name:
                continue
            verdict = _classify(name, args)
            if verdict == "plain":
                m = _SOURCE.search(line)
                if m:
                    return f"a kill whose pid comes from a port or name lookup ({m.group(0).strip()})", scripts
                if tainted is None:
                    tainted = _tainted_vars(text)
                fed = sorted({v.lower() for v in _VAR_REF.findall(" ".join(args))} & tainted)
                if fed:
                    return f"a kill whose pid comes from a port or name lookup (via ${fed[0]})", scripts
            elif verdict:
                return verdict, scripts
            script = _script_arg(name, raw, args)
            if script:
                scripts.append(script)
    return None, scripts


def _scan_python(text: str) -> str | None:
    for m in re.finditer(r"taskkill", text, re.I):
        rest = text[m.end():m.end() + 300].split("\n", 1)[0]
        if _PY_TREE.search(rest):
            return _TREE_LABEL
        if _PY_NAME.search(rest):
            return "taskkill selecting by image name or filter"
    m = _PY_NAMED.search(text)
    if m:
        return _NAME_LABEL
    if _PY_SINK.search(text):
        src = _SOURCE.search(text)
        if src:
            return f"a kill whose pid comes from a port or name lookup ({src.group(0).strip()})"
    return None


def _norm(path: str) -> str:
    p = path.replace("\\", "/")
    if os.name == "nt":
        m = re.match(r"^/([A-Za-z])(/.*)?$", p)
        if m:
            p = f"{m.group(1).upper()}:{m.group(2) or '/'}"
    return p


def _read_recent(path: str, cwd: str) -> str | None:
    """File text when `path` is a small regular file written within the session window."""
    if "$" in path or "`" in path or any(c in path for c in "*?["):
        return None
    try:
        p = _norm(path)
        if p == "~" or p.startswith("~/"):
            p = os.path.expanduser(p)
        if not os.path.isabs(p):
            p = os.path.join(_norm(cwd), p)
        st = os.stat(p)
        if st.st_size > _SCRIPT_MAX_BYTES or time.time() - st.st_mtime > _SCRIPT_WINDOW_S:
            return None
        with open(p, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except (OSError, ValueError):
        return None


def _decide(command: str, cwd: str) -> tuple[str, str] | None:
    """(label, where) for a denied command, or None. `where` names the script when the hit is in one."""
    label, scripts = _scan_shell(command)
    if label:
        return label, ""
    for path in scripts[:4]:
        body = _read_recent(path, cwd)
        if body is None:
            continue
        hit = _scan_python(body) if path.lower().endswith(".py") else _scan_shell(body)[0]
        if hit:
            return hit, re.split(r"[\\/]", path)[-1]
    return None


def _deny_record(label: str, where: str) -> Any:
    from coordinator_core.bash_guards._rewrite_support import _deny_record as _build
    from coordinator_core.guard_messages.alternative_record import AdaptableOffer

    place = f" in script {where}" if where else ""
    text = (
        f"BLOCKED: {label}{place}. A kill chosen by port, name or process tree reaches jobs this "
        f"session never spawned. {ANCHOR}."
    )
    return _build(
        GUARD_NAME,
        text,
        em_text=text,
        members=(
            AdaptableOffer(
                template="kill <pid> (taskkill /PID <pid>, Stop-Process -Id <pid>) with no /T",
                what_to_adapt="<pid>, a pid recorded at spawn (`$!`, `Start-Process -PassThru`, Popen `.pid`)",
            ),
        ),
        override_env=OVERRIDE_ENV,
    )


def check(payload: Dict[str, Any]) -> Any:
    """The chain verdict for one PreToolUse payload: a deny record, or None to allow."""
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not _PREFILTER.search(command):
        return None
    if _override(OVERRIDE_ENV, payload=payload):
        return None
    cwd = payload.get("cwd")
    hit = _decide(command, cwd if isinstance(cwd, str) and cwd else os.getcwd())
    if not hit:
        return None
    return _deny_record(*hit)
