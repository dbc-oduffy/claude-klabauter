"""coordinator_core.roadmap.census -- the one read-only screen a `census[].command`
must clear, and the comparison a recorded `count` earns.

ONE SCREEN, TWO CALLERS. The prep gate (`prep_gate._census`) refuses at stamp time
what the fire-time revalidator (`coordinator/bin/mise-census-revalidate.py`) would
refuse at run time. With a screen per side they disagreed: 20 of 60 premises in one
14-plan run were stamped by prep and then REFUSED by the revalidator (`python -c`,
`$( )`, `for` loops), so a third of the recorded premises could never be re-checked.
A stamped premise is runnable only if both sides ask the same function.

Screening is pure string work, except a `python` target: with a `repo_root` it must be a
git-tracked file of that repo, resolved by ONE `git ls-files` call per batch (`tracked_targets`),
never one per entry.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Optional

#: Read-only command heads. A CLOSED list, and deliberately short: the point is not to anticipate
#: every question an author might ask but to make the set of things a census can execute small
#: enough to read in one sitting. Extending it is a deliberate act -- add a verb only after
#: checking that no flag of it writes, and note that several entries below are here ONLY because
#: their mutating subcommands are screened separately (`git`).
READ_ONLY_HEADS = frozenset({
    "grep", "rg", "egrep", "fgrep", "ls", "find", "cat", "head", "tail", "wc", "sed", "awk",
    "sort", "uniq", "cut", "tr", "basename", "dirname", "stat", "file", "test", "echo", "true",
    "python", "python3", "git", "jq", "yq", "diff", "comm", "realpath", "readlink", "du", "date",
    # `cd` mutates only the child shell the revalidator spawns and dies with it. Present because
    # the `cd <repo> && grep ...` idiom is how an author writes a question about a sibling tree.
    "cd",
})

#: `git` subcommands that only read. Everything else under `git` is refused, including the ones
#: that look harmless: `git stash list` is a read but `git stash` is not, and a screen that has
#: to reason about which is which per invocation is a screen that will eventually be wrong.
GIT_READ_ONLY = frozenset({
    "log", "show", "diff", "status", "ls-files", "ls-tree", "rev-parse", "rev-list", "cat-file",
    "grep", "blame", "describe", "shortlog", "branch", "tag", "remote", "config", "count-objects",
    "merge-base", "name-rev", "for-each-ref", "symbolic-ref", "check-ignore", "var",
})

#: The form a refused author rewrites to. Printed by the prep gate beside every refusal, so the
#: fix is stated where the refusal is read.
ALLOWED_FORM = (
    "one pipeline of read-only commands (grep, rg, find, ls, wc, sed, awk, jq, git <read>, ...) "
    "joined by | && || ; -- `python <script>` / `python -m <module>` only when the target is a "
    "literal git-tracked file of this repo (no $VAR, no glob, no untracked or site-packages "
    "target); no `python -c`, no `$( )` or backticks, no `for`/`while` loops, no "
    "redirect except to /dev/null; a loop over N things is N census entries, or one "
    "`grep -c`/`find | wc -l` over all of them"
)

#: Redirect targets that write nothing anybody can read back. `2>/dev/null` is the dominant
#: idiom in a census command and refusing it rejects a read for being tidy about stderr.
_NULL_SINKS = frozenset({"/dev/null"})

#: Pipeline separators, as shlex hands them back once the string is parsed.
_SEPARATORS = frozenset({"|", "||", "&&", ";"})

#: Operators that are not a pipeline of reads: backgrounding, subshells and process
#: substitution (`(`), case terminators, `|&`, which pipes stderr where `|` does not, and a
#: heredoc, whose body is a program fed to the head rather than a stage this screen can read.
_CONTROL_REFUSED = frozenset({"&", "(", ")", ";;", "|&", ";&", "<(", ">(", "<<", "<<-"})


def _newlines_as_separators(command: str) -> str:
    """`command` with each unquoted newline replaced by ` ; ` and each `\\<newline>` continuation
    removed, so every line is screened as the stage the shell will run it as. A newline inside
    quotes is part of an argument and is kept."""
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == "\\" and quote != "'" and i + 1 < len(command):
            if command[i + 1] == "\n":
                i += 2
                continue
            out.append(command[i:i + 2])
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "\n":
            ch = " ; "
        out.append(ch)
        i += 1
    return "".join(out)


_PY_HEADS = ("python", "python3")
_DYNAMIC = re.compile(r"[$*?\[`]")
_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def _split_stages(command: str) -> Optional[list[list[str]]]:
    """Parsed pipeline stages, or None when `command` does not tokenise."""
    try:
        lex = shlex.shlex(_newlines_as_separators(command), posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return None
    stages: list[list[str]] = [[]]
    for tok in tokens:
        if tok in _SEPARATORS:
            stages.append([])
        else:
            stages[-1].append(tok)
    return [[p for p in st if p] for st in stages]


def _python_target(parts: list[str]) -> tuple[str, str]:
    """(kind, target) of a python stage: kind is "module", "script" or "none"."""
    rest = parts[1:]
    for i, tok in enumerate(rest):
        if tok == "-m":
            return ("module", rest[i + 1]) if i + 1 < len(rest) else ("none", "")
        if tok.startswith("-m") and len(tok) > 2:
            return "module", tok[2:]
        if tok.startswith("-") and tok != "-":
            continue
        return "script", tok
    return "none", ""


def _candidates(kind: str, target: str, root: Path) -> Optional[list[str]]:
    """Repo-relative tracked-path candidates for a target, or None when it cannot be one."""
    if kind == "module":
        if not _MODULE.match(target):
            return None
        base = target.replace(".", "/")
        return [f"{base}.py", f"{base}/__main__.py", f"{base}/__init__.py"]
    if kind != "script" or _DYNAMIC.search(target):
        return None
    path = Path(target.replace("\\", "/"))
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            return None
    rel = path.as_posix()
    if rel.startswith("../") or "/../" in rel or rel in ("", ".", ".."):
        return None
    return [rel[2:] if rel.startswith("./") else rel]


def python_candidates(command: str, repo_root: Path) -> list[str]:
    """Every tracked-path candidate the `python` stages of `command` could resolve to."""
    out: list[str] = []
    for parts in _split_stages(command) or []:
        if parts and Path(parts[0]).name in _PY_HEADS and "-c" not in parts:
            cands = _candidates(*_python_target(parts), Path(repo_root))
            out.extend(cands or [])
    return out


def tracked_targets(repo_root: Path, candidates: list[str]) -> frozenset:
    """The subset of `candidates` git tracks in `repo_root`: ONE `git ls-files` spawn for the
    whole batch, none when there is nothing to ask. A failed spawn tracks nothing, so every
    python target is refused rather than waved through."""
    unique = sorted(set(candidates))
    if not unique:
        return frozenset()
    from coordinator_core.git.run import run_git

    result = run_git(
        ["-C", str(repo_root), "--literal-pathspecs", "ls-files", "-z", "--", *unique],
        timeout=30,
    )
    if not result.ok:
        return frozenset()
    return frozenset(p for p in result.stdout.split("\0") if p)


def _python_refusal(
    parts: list[str], repo_root: Path, tracked: Optional[frozenset]
) -> Optional[str]:
    kind, target = _python_target(parts)
    if kind == "none":
        return "python with no script or -m module target -- name a tracked file"
    if _DYNAMIC.search(target):
        return f"python target {target!r} is built from a variable or glob -- name the file"
    cands = _candidates(kind, target, repo_root)
    if cands is None:
        return f"python {kind} {target!r} is not a path inside the repo"
    if tracked is None:
        tracked = tracked_targets(repo_root, cands)
    if not tracked.intersection(cands):
        return f"python {kind} {target!r} is not a git-tracked file of this repo"
    return None


def screen(
    command: str, repo_root: Optional[Path] = None, tracked: Optional[frozenset] = None
) -> Optional[str]:
    """`None` when the command is read-only; the refusal reason otherwise.

    With `repo_root`, a `python` target must also be a tracked file of that repo. `tracked` is the
    batch answer from `tracked_targets`; omitted, it is resolved for this command alone.

    PARSE FIRST, THEN SPLIT. A metacharacter inside a quoted argument is not a metacharacter:
    screening the raw string refused every `grep -rn 'a\\|b' path | wc -l` as unparseable.

    Screens every stage of a pipeline, not just the first: `grep -c x | tee out.txt` is a write
    hiding behind a read, and a screen that looks only at the head reads it as safe.

    TOKENISE AS THE SHELL WILL. `shlex.split` folds a newline into whitespace and keeps `x;rm`
    or `x&` inside one word, so `grep x\\nrm y`, `grep x;rm y` and `grep x & rm y` each screened
    as a lone `grep` and ran the `rm`. Operators are split out as their own tokens
    (`punctuation_chars`), and an unquoted newline is the separator the shell reads it as.
    """
    try:
        lex = shlex.shlex(_newlines_as_separators(command), posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError as exc:
        return f"unparseable as a command ({exc})"
    for tok in tokens:
        if tok in _CONTROL_REFUSED:
            return f"shell control operator {tok!r} -- not a pipeline of reads"

    # On the PARSED tokens, so `'$('` quoted as a grep pattern is a literal, not a substitution.
    for tok in tokens:
        for marker in ("$(", "`"):
            if marker in tok:
                return f"command substitution ({marker!r}) cannot be screened unevaluated"

    stages: list[list[str]] = [[]]
    for tok in tokens:
        if tok in _SEPARATORS:
            stages.append([])
            continue
        stages[-1].append(tok)

    for parts in stages:
        parts = [p for p in parts if p]
        if not parts:
            continue
        for i, tok in enumerate(parts):
            target = parts[i + 1] if i + 1 < len(parts) else ""
            if tok in (">", ">>", "&>", "&>>") and target not in _NULL_SINKS:
                return f"redirects output to {target or '<nothing>'!r} -- not a read"
            # `2>&1` duplicates a descriptor; any other `>&` target is a file.
            if tok == ">&" and not target.isdigit() and target not in _NULL_SINKS:
                return f"redirects output to {target or '<nothing>'!r} -- not a read"
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", parts[0]):
            return f"variable assignment {parts[0].split('=', 1)[0]}= -- write the value inline"
        head = Path(parts[0]).name
        if head not in READ_ONLY_HEADS:
            return f"{head!r} is not on the read-only allowlist"
        if head == "git":
            # `git -C <path> <sub>` and `--git-dir=<path> <sub>` put a value before the
            # subcommand; skip a flag and, where it takes a separate value, its value too.
            sub = None
            skip = False
            for tok in parts[1:]:
                if skip:
                    skip = False
                    continue
                if tok in ("-C", "--git-dir", "--work-tree", "-c"):
                    skip = True
                    continue
                if tok.startswith("-"):
                    continue
                sub = tok
                break
            if sub not in GIT_READ_ONLY:
                return f"git subcommand {sub!r} is not a declared read"
        if head in _PY_HEADS:
            if "-c" in parts:
                return "python -c cannot be screened without evaluating it"
            if repo_root is not None:
                reason = _python_refusal(parts, Path(repo_root), tracked)
                if reason:
                    return reason
    return None


def observed_counts(stdout: str) -> tuple[Optional[int], int]:
    """(the output's leading integer or None, its non-empty line count).

    Two idioms carry a quantity: `wc -l`/`grep -c`/`rev-list --count` print it as the output's
    leading integer, `grep -n`/`ls`/`find` as the number of lines. A recorded count is held to
    either reading -- `142 file.py` from `wc -l` is one line whose number is 142.
    """
    out = stdout.strip()
    lead = re.match(r"^\s*(-?\d+)\b", out)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return (int(lead.group(1)) if lead else None, len(lines))


def compare_count(count: int, stdout: str) -> dict:
    """MATCH or DRIFT against a structured `count`; never UNDECIDABLE.

    The author's `count` is a declared quantity, not a leading token whose meaning a reader has
    to guess, so a disagreement under both readings is counter-evidence.
    """
    lead, nlines = observed_counts(stdout)
    if lead == count:
        return {"state": "MATCH", "basis": f"count={count} (leading integer)"}
    if nlines == count:
        return {"state": "MATCH", "basis": f"count={count} (line count)"}
    observed = str(lead) if lead is not None and nlines == 1 else f"{nlines} line(s)"
    return {"state": "DRIFT", "basis": f"recorded count {count}, observed {observed}"}


#: The one canonical shape for a count carried inside a value: `count N; v`. The revalidator
#: DISPLAYS a count-bearing entry this way, so a recorded result may arrive in it. Writers must
#: not emit it on the observed side; if one does, `split_count` strips it symmetrically.
_COUNT_PREFIX = re.compile(r"^\s*count\s+(-?\d+)\s*;\s*(.*)\Z", re.DOTALL)


def split_count(text: str) -> tuple[Optional[int], str]:
    """(N, v) for `count N; v`, else (None, text stripped). The value is whitespace-stripped."""
    m = _COUNT_PREFIX.match(text or "")
    if m:
        return int(m.group(1)), m.group(2).strip()
    return None, (text or "").strip()


def same_value(count: Optional[int], recorded: str, observed: str) -> Optional[dict]:
    """MATCH/DRIFT when recorded and observed agree or contradict on the NORMALISED shape.

    Both sides are reduced to (count, value) first. Two counts that are both present and differ
    are a DRIFT; an identical non-empty value is a MATCH whatever the declared `count` says,
    because identical output means nothing moved (a count measuring something other than the
    output's lines or leading integer is an authoring slip, not drift). None when undecided.
    """
    rec_n, rec_v = split_count(recorded)
    obs_n, obs_v = split_count(observed)
    rec_n = rec_n if rec_n is not None else count
    if rec_n is not None and obs_n is not None and rec_n != obs_n:
        return {"state": "DRIFT", "basis": f"recorded count {rec_n}, observed count {obs_n}"}
    if rec_v and rec_v == obs_v:
        return {"state": "MATCH", "basis": "exact (count prefix normalised)"}
    return None


def count_defect(entry: dict) -> Optional[str]:
    """Why an entry's `count` is unusable, or None when absent or a non-negative int."""
    if "count" not in entry:
        return None
    value = entry["count"]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return f"count {value!r} is not a non-negative integer"
    return None
