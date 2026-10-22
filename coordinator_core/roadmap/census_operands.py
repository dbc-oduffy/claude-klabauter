"""The path operands a census command's reader stages name, read from argv alone.

Shared by `mise-census-revalidate` (which keys an entry by what these operands hold) and
`prep_gate` (which warns on an operand git does not track). Spawns nothing, reads no file.
"""

from __future__ import annotations

import re
from typing import Optional

from coordinator_core.roadmap import census

#: Programs whose reads are exactly what their argv names, or the previous stage's stdout.
READERS = frozenset("grep wc find ls cat jq head tail sort uniq cut tr".split())

#: Untracked live-state roots: an operand at or under one cannot be revalidated from the index.
LIVE_STATE_ROOTS = (".coordinator-local", "scratch", "state/mise-workflows")

_FIND_PREDICATES = frozenset(
    "-name -iname -type -maxdepth -mindepth -path -ipath -not -o -a -print -print0 -empty".split()
)
_GREP_VALUE_SHORT = frozenset("mABCefdDgtTjM")
_GREP_VALUE_LONG = frozenset(
    "--include --exclude --exclude-dir --max-count --file --regexp --after-context "
    "--before-context --context --glob --type --type-not --max-depth --threads "
    "--replace --color --colors --binary-files --devices --directories --label".split()
)
_LS_FLAGS = frozenset("1aAR")
_REDIRECTS = frozenset({">", ">>", "&>", "&>>", ">&"})
_PUNCT_ONLY = re.compile(r"^[<>&|;()]+$")


def scrub_redirects(parts: list[str]) -> Optional[list[str]]:
    """`parts` without `[N]>/dev/null`; None for any other redirect or stray operator."""
    out: list[str] = []
    i = 0
    while i < len(parts):
        tok = parts[i]
        if tok in _REDIRECTS:
            if i + 1 < len(parts) and parts[i + 1] == "/dev/null":
                if out and len(out[-1]) == 1 and out[-1].isdigit():
                    out.pop()
                i += 2
                continue
            return None
        if _PUNCT_ONLY.match(tok):
            return None
        out.append(tok)
        i += 1
    return out


def grep_operands(prog: str, args: list[str], first: bool) -> Optional[list[str]]:
    shorts = [a[1:] for a in args if a.startswith("-") and not a.startswith("--") and a != "-"]
    longs = [a for a in args if a.startswith("--")]
    non = [a for a in args if not a.startswith("-")]
    if "--" in longs or "--files" in longs:
        return None
    if any(a in ("--dereference-recursive", "--follow") for a in longs):
        return None
    if any(("R" in c or "L" in c) for c in shorts):
        return None
    if any(lo.startswith(("--file", "--exclude-from")) for lo in longs):
        return None
    value_short = _GREP_VALUE_SHORT
    for cluster in shorts:
        for idx, ch in enumerate(cluster):
            if ch in value_short:
                if idx == len(cluster) - 1:
                    return None
                break
    if any("=" not in lo and lo in _GREP_VALUE_LONG for lo in longs):
        return None
    recursive = any("r" in c for c in shorts) or "--recursive" in longs
    pattern_opt = (
        any(("e" in c or "f" in c) for c in shorts)
        or any(lo.startswith("--regexp=") for lo in longs)
    )
    operands = non if pattern_opt else non[1:]
    if not operands and (recursive or first):
        return None
    return operands


def stage_operands(prog: str, args: list[str], first: bool) -> Optional[list[str]]:
    """The path operands a reader stage names, or None when its read set is not bounded by argv.
    A stage that reads the tree by default (`grep -r`, `rg`, `find`, `ls` with no operand) reads
    "." and so is None; a first stage with no operand reads inherited stdin and is None."""
    if prog in ("grep", "rg"):
        return grep_operands(prog, args, first)
    opts = [a for a in args if a.startswith("-") and a != "-"]
    if "--" in args:
        return None
    if prog == "find":
        lead: list[str] = []
        for a in args:
            if a.startswith("-"):
                break
            lead.append(a)
        preds = [a for a in args[len(lead):] if a.startswith("-")]
        if any(p not in _FIND_PREDICATES for p in preds) or not lead:
            return None
        return lead
    non = [a for a in args if not a.startswith("-") or a == "-"]
    non = [a for a in non if a != "-"]
    if prog == "ls":
        if any(o.startswith("--") or not set(o[1:]) <= _LS_FLAGS for o in opts) or not non:
            return None
        return non
    if not non and first:
        return None
    return non


def operand_rel(token: str) -> Optional[str]:
    """`token` as a clean repo-relative path, or None for any shape the key will not bound."""
    if token.startswith(("/", "~")):
        return None
    rel = token
    while rel.startswith("./"):
        rel = rel[2:]
    segs = [s for s in rel.split("/") if s]
    if not segs or segs == ["."] or ".." in segs or ".git" in segs or "." in segs:
        return None
    return "/".join(segs)


def command_operands(command: str) -> Optional[list[str]]:
    """Every bounded reader stage's operand rels, in order; None when the command does not
    tokenise. A stage outside `READERS` or whose operands argv does not bound contributes none."""
    stages = census._split_stages(command)
    if not stages:
        return None
    rels: list[str] = []
    for index, raw in enumerate(stages):
        parts = scrub_redirects(raw) if raw else None
        if not parts or parts[0] not in READERS:
            continue
        operands = stage_operands(parts[0], parts[1:], index == 0)
        for token in operands or ():
            rel = operand_rel(token)
            if rel is not None and rel not in rels:
                rels.append(rel)
    return rels
