"""coordinator_core.probes.fork_census — JSON-RPC "probes.fork_census" operation.

Purpose: named, re-runnable successor to the one-off transcript probe that
produced the 203,819-external-fork / 8-day baseline documented in
``DoE-claude/state/plan-sidecars/2026-07-28-bash-tax-negative-space.md``
("Cost model" table). That probe was a throwaway scratchpad script
(``extract.py`` + ``analyze4.py``, session-scoped, deleted with the scratchpad).
This module ports its extraction + classification logic into a tested,
invocable op so the same measurement can be re-run before/after a bash-spawn
mitigation lands, with the output shape held stable for comparison.

Two properties the one-off probe did NOT have, both load-bearing here:

  1. **Per-machine, never pooled** (AC-1). A single pooled fork count cannot
     show whether a platform-specific mitigation (e.g. a macOS-only advisory
     nudge) is actually moving the needle on that platform versus just
     diluting into a fleet-wide average dominated by other machines. Every
     count this module emits is bucketed under ``by_machine`` first; the
     ``pooled_for_reference_only`` block exists purely as a convenience
     cross-check and is never the primary read.

  2. **macOS advisory-conversion tracked separately from Windows denies**
     (AC-14). The ``cd`` + ``git`` shape (sidecar row 3) is the concrete
     case: ``check_offer_git_c`` (claude-klabauter
     ``coordinator_core/bash_guards``) fires as a soft, fail-open *advisory*
     rewrite offer — it never blocks the command. A hard-deny guard on the
     same repo (e.g. a cost-driven Windows-only policy) is a categorically
     different intervention: "the command still ran, but the agent was
     nudged" versus "the command never ran". Conflating the two into one
     "guard activity" number would make it impossible to tell whether an
     advisory is *working* (the agent adopts ``git -C`` afterwards) or is
     merely indistinguishable from the pre-existing CLAUDE.md prose rule it
     was meant to replace. This module reports, per machine:
       - ``cd_git_shaped_commands`` — commands matching the advisory's own
         target shape (``cd X && git ...``, single-line, <3 segments).
       - ``git_dash_c_idiomatic_commands`` — commands already using the
         ``git -C`` idiom the advisory recommends.
       - ``advisory_conversion_rate`` — the latter over (the latter + the
         former), computed ONLY for non-Windows machines (where the guard's
         intervention is advisory-shaped); ``None`` on Windows, with
         ``denies_observed`` reported alongside instead so a reader is never
         tempted to read a Windows ratio as an advisory-conversion signal.
       - ``denies_observed`` — Bash tool_use calls whose paired tool_result
         begins with "BLOCKED" (any hard-deny guard, not scoped to the
         cd+git shape) — the Windows-relevant signal, reported for every
         machine so the two numbers sit side by side without being merged.

Self-registration: importing this module calls register_op("probes.fork_census",
...) as a side-effect — same pattern as coordinator_core.ops.ping /
coordinator_core.ops.cartography_churn. This op is NOT wired into
coordinator_core.ops.__init__'s eager-import list, op_scopes.py's
_OP_KEY_SCOPE, or benchmarks/budget-manifest.json — those shared-seam edits
are a separate registration chunk (same precedent as cartography_churn.py's
own docstring), out of this module's write scope.

Wire params (probes.fork_census):
    base_dir (str, optional) — root directory holding ``<project>/<session>.jsonl``
                                transcript files. Defaults to
                                ``coordinator_core._settings_home.home_dir() /
                                ".claude" / "projects"`` (the live Claude Code
                                transcript root, Windows-safe via ``Path.home()``
                                honouring ``USERPROFILE``). Tests point this at a
                                fixture directory instead of the live corpus.

Reply shape: see ``run_fork_census``'s own docstring — the same "Cost model"
fields as the original probe (external forks, builtin invocations, shell
starts, total process creations, forks-per-call percentiles, top forked
binaries), nested under ``by_machine`` plus one ``pooled_for_reference_only``
block.

Spec backlink: DoE-claude:pln-fleet-wide-bash-spawn-fan-out--2f6552 § C1.
Measurement oracle: DoE-claude state/plan-sidecars/2026-07-28-bash-tax-negative-space.md.

Negative-spec:
  - Does NOT write to the transcript corpus or to any fleet store — pure
    read-and-report, same COMPUTE_ONLY posture as coordinator_core.ops.
  - Does NOT assert or enforce the sidecar's platform-split verdict — it only
    measures. The threshold/verdict judgement stays a PM/plan-level call.
  - Does NOT attempt exact byte-for-byte fork-count parity with the original
    scratchpad script's regex-based classifier (heredoc/command-substitution
    edge cases differ) — both are "directionally solid, not exact", per the
    sidecar's own accuracy caveat. Segmentation here goes through the shared,
    guard-package tokenizer (`_command_tokenizer.tokenize_full_command`)
    rather than hand-rolled regex, per this fleet's standing "no regex for
    command-shape detection" rule.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from coordinator_core._settings_home import home_dir
from coordinator_core.bash_guards._command_tokenizer import (
    normalize_executable_basename,
    segments_from_tokens_with_pipe_flag,
    tokenize_full_command,
)
from coordinator_core.ipc import register_op

#: Shell builtins/keywords that never fork a child process. Ported from the
#: original scratchpad classifier (analyze4.py BUILTINS) with the addition of
#: "eval"/"exec" (already present there) kept, and "unset"/"in" retained —
#: this is a never-forks allowlist, not a completeness claim about bash
#: grammar; an unrecognized leading token is always treated as external
#: (fails toward OVER-counting forks, never under-counting).
BUILTIN_COMMANDS = frozenset(
    {
        "echo", "cd", "test", "[", "[[", ":", "true", "false", "printf",
        "read", "export", "local", "set", "unset", "shift", "return",
        "exit", "source", ".", "eval", "pwd", "if", "then", "else", "elif",
        "fi", "for", "while", "do", "done", "case", "esac", "function",
        "declare", "let", "trap", "wait", "break", "continue", "shopt",
        "time", "exec", "in",
    }
)

_PERCENTILES = (0.50, 0.75, 0.90, 0.99)


def classify_platform(cwd: Optional[str]) -> str:
    """Classify a transcript record's ``cwd`` into a machine bucket.

    Returns "windows", "macos", "linux", or "unknown". Transcripts carry no
    explicit platform field (verified against a live ``~/.claude/projects``
    sample) — ``cwd`` path *shape* is the only observable proxy. Checks are
    plain string indexing/prefix tests, not regex, so this cannot fall into
    the drive-letter-matches-URL-scheme trap a `[A-Za-z]:[/\\\\]` regex would
    (that bug requires an UNANCHORED substring search; indexing ``cwd[1]``
    only ever inspects the string's own second character).
    """
    if not cwd:
        return "unknown"
    if len(cwd) >= 2 and cwd[0].isalpha() and cwd[1] == ":" and (
        len(cwd) == 2 or cwd[2] in ("\\", "/")
    ):
        return "windows"
    if cwd.startswith("/"):
        # macOS convention: real user trees under /Users/, ephemeral/sandboxed
        # trees under /private/ (macOS-only — Linux uses /tmp directly, not
        # /private/tmp). Anything else POSIX-shaped is treated as linux.
        if cwd.startswith("/Users/") or cwd.startswith("/private/"):
            return "macos"
        return "linux"
    if "\\" in cwd:
        return "windows"
    return "unknown"


def strip_heredoc_bodies(cmd: str) -> str:
    """Remove heredoc BODY lines from `cmd` — the body is shell stdin data,
    never executed as commands, so leaving it in would let the tokenizer
    misread the payload's own words (e.g. a heredoc'd markdown file
    containing the word "git") as additional external-binary invocations.

    Line-based string scan, not regex: find "<<" on a line, read the
    delimiter word that follows (stripping a leading "-" for `<<-` and
    surrounding quotes), then drop every subsequent line up to and
    including the first line that is exactly that delimiter. Best-effort —
    an unterminated heredoc (delimiter never recurs) drops the remainder of
    the command, matching the original scratchpad classifier's same
    fail-direction (analyze4.py's unterminated-heredoc branch).
    """
    lines = cmd.split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        delim = _heredoc_delimiter(line)
        i += 1
        if delim is not None:
            while i < len(lines) and lines[i].strip() != delim:
                i += 1
            if i < len(lines):
                i += 1  # consume the delimiter line itself
    return "\n".join(out)


def _heredoc_delimiter(line: str) -> Optional[str]:
    """Return the heredoc delimiter word on `line`, or None if `line` does
    not open a heredoc. Plain string operations only (find/strip/split) —
    no regex.
    """
    idx = line.find("<<")
    if idx == -1:
        return None
    rest = line[idx + 2:]
    if rest.startswith("-"):
        rest = rest[1:]
    rest = rest.strip()
    if not rest:
        return None
    word = rest.split()[0]
    if len(word) >= 2 and word[0] == word[-1] and word[0] in ("'", '"'):
        word = word[1:-1]
    return word or None


def _looks_like_assignment(tok: str) -> bool:
    """True if `tok` is a leading `VAR=value` shell-assignment prefix (e.g.
    `FOO=bar`, `PATH=/x:$PATH`) rather than the command's own executable
    token. Plain string checks, no regex.
    """
    if "=" not in tok:
        return False
    name = tok.split("=", 1)[0]
    if not name:
        return False
    if not (name[0].isalpha() or name[0] == "_"):
        return False
    return all(ch.isalnum() or ch == "_" for ch in name)


@dataclass
class CommandShape:
    """Fork-classification result for a single Bash tool-call command string."""

    external_forks: int = 0
    builtin_invocations: int = 0
    binaries: Counter = field(default_factory=Counter)
    cd_seen: bool = False
    cd_then_git: bool = False
    git_dash_c: bool = False
    parse_failed: bool = False


def count_command_shape(cmd: str) -> CommandShape:
    """Classify one Bash tool-call command string into forks vs builtins.

    Segments the (heredoc-stripped) command on `;`/`&`/`|`/`&&`/`||` via the
    shared guard-package tokenizer (`tokenize_full_command` +
    `segments_from_tokens_with_pipe_flag`) rather than hand-rolled regex, per
    this fleet's standing "no regex for command-shape detection" rule. Each
    segment's leading token (after skipping `VAR=value` assignment prefixes)
    is classified as a builtin (no fork) or an external binary (one fork),
    using `normalize_executable_basename` so a `.exe`/`.cmd`-suffixed or
    path-qualified Windows spelling still resolves to the right identity.

    Command substitution (`$(...)`/backticks) is NOT segment-parsed — those
    nest an independent command inside a token the tokenizer treats as one
    word — but each occurrence still represents a real subshell fork, so it
    is counted via a plain substring `.count()` (not regex) as a fork-count
    addend, matching the original scratchpad classifier's treatment.

    On a tokenizer parse failure (unterminated quote/trailing backslash),
    `tokenize_full_command` returns None; this function fails toward
    UNDER-counting rather than raising — reports zero forks/builtins for the
    unparseable command, sets `parse_failed=True`, so a caller can track the
    (expected small) noise floor without either crashing on it or attributing
    it to a real classification.
    """
    cleaned = strip_heredoc_bodies(cmd)
    tokens = tokenize_full_command(cleaned)
    shape = CommandShape()
    if tokens is None:
        shape.parse_failed = True
        return shape

    for seg_tokens, _pipe_before in segments_from_tokens_with_pipe_flag(tokens):
        idx = 0
        while idx < len(seg_tokens) and _looks_like_assignment(seg_tokens[idx]):
            idx += 1
        if idx >= len(seg_tokens):
            continue
        head = seg_tokens[idx]
        base = normalize_executable_basename(head)
        if not base:
            continue
        if base in BUILTIN_COMMANDS:
            shape.builtin_invocations += 1
            if base == "cd":
                shape.cd_seen = True
            continue
        shape.external_forks += 1
        shape.binaries[base] += 1
        if base == "git":
            if shape.cd_seen:
                shape.cd_then_git = True
            rest = seg_tokens[idx + 1:]
            if rest and rest[0] == "-C":
                shape.git_dash_c = True

    shape.external_forks += cleaned.count("$(")
    shape.external_forks += cleaned.count("`") // 2
    return shape


@dataclass
class _MachineAccumulator:
    bash_tool_calls: int = 0
    external_forks: int = 0
    builtin_invocations: int = 0
    binaries: Counter = field(default_factory=Counter)
    forks_per_call: List[int] = field(default_factory=list)
    sessions: set = field(default_factory=set)
    cd_git_shaped_commands: int = 0
    git_dash_c_idiomatic_commands: int = 0
    denies_observed: int = 0
    parse_failed: int = 0


def _percentile(sorted_vals: List[int], p: float) -> int:
    if not sorted_vals:
        return 0
    idx = min(int(len(sorted_vals) * p), len(sorted_vals) - 1)
    return sorted_vals[idx]


def _finalize_machine(acc: _MachineAccumulator, machine: str) -> dict:
    shell_starts = acc.bash_tool_calls
    total_process_creations = acc.external_forks + acc.builtin_invocations
    per_call_sorted = sorted(acc.forks_per_call)
    advisory_conversion_rate: Optional[float]
    denom = acc.cd_git_shaped_commands + acc.git_dash_c_idiomatic_commands
    if machine == "windows":
        # The cd+git shape hits a hard-deny guard on Windows, not the
        # macOS-shaped advisory — a ratio here would silently read as an
        # advisory-conversion signal it is not (AC-14). Report None and let
        # denies_observed carry the Windows-relevant number instead.
        advisory_conversion_rate = None
    elif denom == 0:
        advisory_conversion_rate = None
    else:
        advisory_conversion_rate = acc.git_dash_c_idiomatic_commands / denom

    return {
        "bash_tool_calls": acc.bash_tool_calls,
        "sessions_with_bash": len(acc.sessions),
        "external_binary_forks": acc.external_forks,
        "builtin_invocations": acc.builtin_invocations,
        "shell_starts": shell_starts,
        "total_process_creations": total_process_creations,
        "forks_per_call": {
            "p50": _percentile(per_call_sorted, 0.50),
            "p75": _percentile(per_call_sorted, 0.75),
            "p90": _percentile(per_call_sorted, 0.90),
            "p99": _percentile(per_call_sorted, 0.99),
            "max": (per_call_sorted[-1] if per_call_sorted else 0),
        },
        "top_forked_binaries": acc.binaries.most_common(10),
        "advisory": {
            "cd_git_shaped_commands": acc.cd_git_shaped_commands,
            "git_dash_c_idiomatic_commands": acc.git_dash_c_idiomatic_commands,
            "advisory_conversion_rate": advisory_conversion_rate,
        },
        "denies_observed": acc.denies_observed,
        "parse_failed_commands": acc.parse_failed,
    }


def _iter_bash_calls(base_dir: Path) -> Iterator[Tuple[str, Optional[str], str]]:
    """Yield (command, cwd, session_key) for every Bash tool_use call under
    `base_dir` (expected shape: `<project-dir>/<session>.jsonl`), paired with
    a denial flag via the immediately-following tool_result.

    Actually yields (command, cwd, session_key, denied) — see call site.
    Streams line-by-line; a JSON-parse failure on one line is skipped (never
    aborts the scan), matching the original probe's "unparsed lines" counter
    treatment.
    """
    for project_dir in sorted(p for p in base_dir.glob("*") if p.is_dir()):
        for transcript in sorted(project_dir.glob("*.jsonl")):
            session_key = f"{project_dir.name}/{transcript.name}"
            last_cwd: Optional[str] = None
            pending: Dict[str, Tuple[str, Optional[str]]] = {}
            try:
                fh = transcript.open("r", encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(rec, dict):
                        continue
                    cwd = rec.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        last_cwd = cwd

                    message = rec.get("message")
                    content = message.get("content") if isinstance(message, dict) else None
                    if not isinstance(content, list):
                        continue

                    role = message.get("role") if isinstance(message, dict) else None
                    if role == "assistant":
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") != "tool_use" or block.get("name") != "Bash":
                                continue
                            tool_input = block.get("input") or {}
                            cmd = tool_input.get("command")
                            tool_id = block.get("id")
                            if not isinstance(cmd, str):
                                continue
                            yield_key = tool_id if isinstance(tool_id, str) else None
                            if yield_key is not None:
                                pending[yield_key] = (cmd, last_cwd)
                            else:
                                yield cmd, last_cwd, session_key, False
                    elif role == "user":
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") != "tool_result":
                                continue
                            tool_id = block.get("tool_use_id")
                            if not isinstance(tool_id, str) or tool_id not in pending:
                                continue
                            cmd, cwd_at_call = pending.pop(tool_id)
                            denied = _is_denial(block.get("content"))
                            yield cmd, cwd_at_call, session_key, denied
            # Any Bash calls never paired with a tool_result (session ended
            # mid-flight) still count toward the corpus — undenied by
            # definition (a denial requires an observed BLOCKED result).
            for cmd, cwd_at_call in pending.values():
                yield cmd, cwd_at_call, session_key, False


def _is_denial(tool_result_content) -> bool:
    """True if a Bash tool_result's content indicates a guard denial — text
    beginning with "BLOCKED" (the coordinator bash-guard denial preamble,
    e.g. "BLOCKED (approval-sentinel guard): ..."). Plain substring check,
    not regex.
    """
    texts: List[str] = []
    if isinstance(tool_result_content, str):
        texts.append(tool_result_content)
    elif isinstance(tool_result_content, list):
        for item in tool_result_content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
            elif isinstance(item, str):
                texts.append(item)
    return any(t.lstrip().startswith("BLOCKED") for t in texts)


def run_fork_census(base_dir: Optional[Path] = None) -> dict:
    """Run the fork census over transcripts under `base_dir`.

    Args:
        base_dir: root holding `<project>/<session>.jsonl` transcripts.
                  Defaults to the live Claude Code transcript root
                  (`home_dir() / ".claude" / "projects"`).

    Returns a dict:
        {
          "corpus": {"transcript_files_scanned", "bash_tool_calls_total"},
          "by_machine": {"macos": {...}, "windows": {...}, "linux": {...},
                         "unknown": {...}},
          "pooled_for_reference_only": {...same fields, summed...},
        }

    Every per-machine block carries the same "Cost model" fields as the
    original one-off probe (external forks, builtin invocations, shell
    starts, total process creations, forks-per-call percentiles, top forked
    binaries) plus the advisory/deny split (see module docstring). The
    `by_machine` mapping is the primary read; `pooled_for_reference_only` is
    a convenience cross-check only, per AC-1.
    """
    if base_dir is None:
        base_dir = home_dir() / ".claude" / "projects"
    base_dir = Path(base_dir)

    accs: Dict[str, _MachineAccumulator] = {}
    transcript_files_scanned = 0
    bash_tool_calls_total = 0
    if base_dir.is_dir():
        transcript_files_scanned = sum(
            1 for p in base_dir.glob("*") if p.is_dir() for _ in p.glob("*.jsonl")
        )
        for cmd, cwd, session_key, denied in _iter_bash_calls(base_dir):
            machine = classify_platform(cwd)
            acc = accs.setdefault(machine, _MachineAccumulator())
            acc.bash_tool_calls += 1
            acc.sessions.add(session_key)
            bash_tool_calls_total += 1
            shape = count_command_shape(cmd)
            acc.external_forks += shape.external_forks
            acc.builtin_invocations += shape.builtin_invocations
            acc.binaries.update(shape.binaries)
            acc.forks_per_call.append(shape.external_forks)
            if shape.cd_then_git:
                acc.cd_git_shaped_commands += 1
            if shape.git_dash_c:
                acc.git_dash_c_idiomatic_commands += 1
            if shape.parse_failed:
                acc.parse_failed += 1
            if denied:
                acc.denies_observed += 1

    by_machine = {m: _finalize_machine(acc, m) for m, acc in sorted(accs.items())}

    pooled_acc = _MachineAccumulator()
    for acc in accs.values():
        pooled_acc.bash_tool_calls += acc.bash_tool_calls
        pooled_acc.external_forks += acc.external_forks
        pooled_acc.builtin_invocations += acc.builtin_invocations
        pooled_acc.binaries.update(acc.binaries)
        pooled_acc.forks_per_call.extend(acc.forks_per_call)
        pooled_acc.sessions.update(acc.sessions)
        pooled_acc.cd_git_shaped_commands += acc.cd_git_shaped_commands
        pooled_acc.git_dash_c_idiomatic_commands += acc.git_dash_c_idiomatic_commands
        pooled_acc.denies_observed += acc.denies_observed
        pooled_acc.parse_failed += acc.parse_failed
    pooled = _finalize_machine(pooled_acc, "pooled")

    return {
        "corpus": {
            "transcript_files_scanned": transcript_files_scanned,
            "bash_tool_calls_total": bash_tool_calls_total,
        },
        "by_machine": by_machine,
        "pooled_for_reference_only": pooled,
    }


@register_op("probes.fork_census")
async def _fork_census(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "probes.fork_census" handler. See module docstring "Wire params"."""
    base_dir_raw = params.get("base_dir")
    base_dir = Path(base_dir_raw) if base_dir_raw else None
    return run_fork_census(base_dir=base_dir)
