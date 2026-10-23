"""Which in-repo paths a Bash command writes, and with what text when the text is literal.

`shell_writes` applies the same filter the touch recorder applies
(`write_claim_record.record_write_claims`) so the XS lane budget and the commit
claim see the same target set. Reads only `cmd`; no subprocess.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Dict, List, NamedTuple, Optional

from coordinator_core.bash_guards.write_claim_record import (
    _is_bare_heredoc_opener,
    _is_claimable_target,
    _rel_if_inside,
)


class ShellWrite(NamedTuple):
    """`body` is the literal heredoc text (each line newline-terminated), or None
    when the text is not statically known."""

    abs_path: str
    body: Optional[str]
    append: bool


_HEREDOC_OPENER_RE = re.compile(r"(?<!<)<<(?!<)(-?)\s*(?:'([^']+)'|\"([^\"]+)\"|\\?([A-Za-z_][A-Za-z0-9_]*))")
_SEGMENT_SPLIT_RE = re.compile(r"&&|;")


def _literal_heredoc(cmd: str):
    """Return `(head_line, body)` for a command with exactly one heredoc whose body
    is literal and nothing after its terminator, else None."""
    openers = list(_HEREDOC_OPENER_RE.finditer(cmd))
    if len(openers) != 1 or cmd.count("<<") != 1:
        return None
    m = openers[0]
    strip_tabs = bool(m.group(1))
    quoted = m.group(4) is None or "\\" in m.group(0)
    delim = m.group(2) or m.group(3) or m.group(4)
    head = cmd[: m.start()]
    rest = cmd[m.end() :]
    nl = rest.find("\n")
    if nl < 0:
        return None
    if rest[:nl].strip():
        return None
    lines = rest[nl + 1 :].split("\n")
    body_lines: List[str] = []
    end = None
    for i, line in enumerate(lines):
        cand = line.lstrip("\t") if strip_tabs else line
        if cand == delim:
            end = i
            break
        body_lines.append(cand if strip_tabs else line)
    if end is None or any(ln.strip() for ln in lines[end + 1 :]):
        return None
    body = "".join(ln + "\n" for ln in body_lines)
    if not quoted and ("$" in body or "`" in body):
        return None
    return head, body


def _body_targets(head: str) -> Dict[str, bool]:
    """Raw target tokens `{raw: append}` of a single `cat`/`tee` segment (optionally
    preceded by `cd` segments); empty for any other shape."""
    if "|" in head:
        return {}
    segs = _SEGMENT_SPLIT_RE.split(head)
    if not segs or any(s.strip().split(None, 1)[:1] != ["cd"] for s in segs[:-1] if s.strip()):
        return {}
    try:
        toks = shlex.split(segs[-1])
    except ValueError:
        return {}
    if not toks or os.path.basename(toks[0]) not in ("cat", "tee"):
        return {}
    out: Dict[str, bool] = {}
    if os.path.basename(toks[0]) == "tee":
        append = any(t in ("-a", "--append") for t in toks[1:])
        for t in toks[1:]:
            if not t.startswith("-"):
                out[t] = append
        return out
    i = 1
    while i < len(toks):
        t = toks[i]
        if t in (">", ">>") and i + 1 < len(toks):
            out[toks[i + 1]] = t == ">>"
            i += 2
            continue
        if t.startswith(">") and len(t) > 1 and not t.startswith(">&"):
            out[t.lstrip(">")] = t.startswith(">>")
        i += 1
    return out


def shell_writes(cmd: str, cwd: str, root: str) -> List[ShellWrite]:
    """Every in-repo write target of `cmd`, deduplicated, in encounter order."""
    if not cmd or not cmd.strip() or not root:
        return []
    try:
        from coordinator_core.bash_guards.bump_outside_repo_write import (
            _iter_write_sink_candidates,
        )

        lit = _literal_heredoc(cmd)
        body_for = _body_targets(lit[0]) if lit else {}
        out: Dict[str, ShellWrite] = {}
        skip_next = False
        for resolved, head_base, raw in _iter_write_sink_candidates(cmd, cwd):
            if skip_next:
                skip_next = False
                continue
            if _is_bare_heredoc_opener(raw):
                skip_next = True
                continue
            if not _is_claimable_target(raw, head_base, resolved):
                continue
            if _rel_if_inside(resolved, root) is None or resolved in out:
                continue
            if lit and raw in body_for and head_base in ("cat", "tee"):
                out[resolved] = ShellWrite(resolved, lit[1], body_for[raw])
            else:
                out[resolved] = ShellWrite(resolved, None, False)
        return list(out.values())
    except Exception:  # noqa: BLE001 -- best-effort, never raise into a guard
        return []
