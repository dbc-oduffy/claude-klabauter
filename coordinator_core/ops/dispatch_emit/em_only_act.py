"""coordinator_core.ops.dispatch_emit.em_only_act -- is a plan row the EM's to perform.

INVARIANT: one predicate answers every em-performance decision. A row is em-performed when it
declares `performer: em`, or when its only act is a command dispatch forbids every subagent
(`guard_subagent_heavy_ue_launch.launches_heavy`, the single forbidden-command list; never copied).

TRAP: a row with any write act (`writes` non-empty, `writes_under`, `appends`) stays agent-performed
even when its body runs a heavy command: that is the needs_slot author lane, where the agent writes
and the EM runs the slot leg. Deriving em there would strand the authoring. A row without a
declared `writes` key is unknown and never derived.

A row whose `blocked_on.capability` names a capability only the EM session holds (the Workflow tool,
the Agent tool, SendMessage, "EM session") is em-performed too: an executor lacks that tool, so the
row cannot finish there whatever it writes. `em_only_capability` reads it.

Rows carry no commands field, so the command is read from the body's code spans (fenced blocks
line by line, then inline single-backtick spans). Prose naming a script is a mention, not a run.
A code span quoting a forbidden command in negation ("never run `build-plugin.ps1 ...`") is
indistinguishable from a run and derives em.
Pure Python: no spawn, no I/O.

INVARIANT: `executor_commit_act` reuses the Commit Gate's own matchers
(`block_subagent_commit._has_git_commit`, `_has_committing_op_invoke`, `_has_coordinator_safe_commit`),
imported lazily after a substring prefilter, so the plan bar and the guard agree on what a commit is.
The deploy list `_DEPLOY_ACT` is closed: each entry ships a committed tree.

INVARIANT: `executor_merge_act` reuses the subagent guard's `_real_git_subcommand` (lazy, after a
"merge" prefilter) and its `--ff-only` token exemption, so plan and guard agree on a denied merge.
The plan bar is stricter than the guard in one place: it ignores the guard's scratch-context merge
allowance (`_SCRATCH_CTX`), so it over-flags a scratch merge and never under-flags a denied one.

TRAP: `executor_commit_act` and `executor_merge_act` flag a backticked commit, deploy or merge quoted
in negation ("never run `git commit`"); drop the backticks to make it a mention.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterator, Optional

from coordinator_core.bash_guards.guard_subagent_heavy_ue_launch import launches_heavy
from coordinator_core.ops.dispatch_emit import stage_family

_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE = re.compile(r"`([^`\n]+)`")


def _code_spans(body: str) -> Iterator[str]:
    in_fence = False
    for line in body.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            yield line
        else:
            for match in _INLINE.finditer(line):
                yield match.group(1)


# Capabilities a dispatched executor lacks: the harness grants them to the EM session alone.
_EM_ONLY_CAPABILITY = re.compile(
    r"\bworkflow\b|\bagent tool\b|\bsendmessage\b|\bem[- ](?:session|only)\b", re.IGNORECASE
)


def em_only_capability(raw: Dict[str, Any]) -> Optional[str]:
    """`blocked_on.capability` when it names a capability only the EM session has, else None."""
    blocked = raw.get("blocked_on")
    capability = blocked.get("capability") if isinstance(blocked, dict) else None
    if isinstance(capability, str) and _EM_ONLY_CAPABILITY.search(capability):
        return capability
    return None


def forbidden_sole_act(raw: Dict[str, Any]) -> Optional[str]:
    """The first body code span dispatch forbids a subagent to run, or None.

    None unless the row declares `writes: []` and carries no `writes_under` or `appends`.
    """
    if raw.get("writes") != [] or raw.get("writes_under") or raw.get("appends"):
        return None
    body = raw.get("body")
    if not isinstance(body, str):
        return None
    for span in _code_spans(body):
        span = span.strip()
        if span and launches_heavy(span):
            return span
    return None


# Closed list of commands that publish a committed tree, so an executor (who never commits) cannot
# finish them: firebase/gcloud deploy upload the checked-in sources; npm/pnpm/yarn/bun deploy[:target]
# scripts wrap those. Tolerates a path-prefixed argv0 and .cmd/.exe spellings.
_DEPLOY_ACT = re.compile(
    r"(?:^|[\s;&|(])(?:[^\s;&|]*[\\/])?(?:"
    r"(?:firebase|gcloud)(?:\.cmd|\.exe)?\s+(?:[^;&|\n]*\s)?deploy\b"
    r"|(?:npm|pnpm|yarn|bun)(?:\.cmd|\.exe)?\s+(?:run\s+)?deploy(?::[\w-]+)?(?=\s|$)"
    r")",
    re.IGNORECASE,
)


def _span_commits_or_deploys(span: str) -> bool:
    lowered = span.lower()
    if "commit" in lowered:
        from coordinator_core.bash_guards.block_subagent_commit import (
            _has_committing_op_invoke,
            _has_coordinator_safe_commit,
            _has_git_commit,
        )

        if (
            _has_git_commit(span)
            or _has_committing_op_invoke(span)
            or _has_coordinator_safe_commit(span)
        ):
            return True
    return "deploy" in lowered and _DEPLOY_ACT.search(span) is not None


def executor_commit_act(raw: Dict[str, Any]) -> Optional[str]:
    """The first body code span an executor would run to commit or deploy, or None.

    None when row_is_em_performed(raw) (performer: em, or an agent-forbidden sole act).
    """
    if row_is_em_performed(raw):
        return None
    body = raw.get("body")
    if not isinstance(body, str):
        return None
    for span in _code_spans(body):
        span = span.strip()
        if span and _span_commits_or_deploys(span):
            return span
    return None


_SEGMENT_BREAKS = frozenset({";", "&", "&&", "|", "||", "(", ")"})
_GIT_ARGV0 = re.compile(r"(?:^|[\\/])git(?:\.exe)?$", re.IGNORECASE)
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PLAIN_WRAPPERS = frozenset({"command", "exec", "nohup", "time", "sudo"})


def _strip_command_prefix(segment: list) -> list:
    """`segment` without leading `VAR=x` assignments and `env`/`command`-style wrappers, as the guard sees git."""
    from coordinator_core.bash_guards.block_subagent_destructive_action import _strip_env_prefix

    while segment:
        if _ASSIGNMENT.match(segment[0]):
            segment = segment[1:]
        elif segment[0] == "env":
            segment = _strip_env_prefix(segment)
        elif segment[0] in _PLAIN_WRAPPERS:
            segment = segment[1:]
        else:
            break
    return segment


def _span_merges(span: str) -> bool:
    """True when a segment of `span` runs `git merge` without `--ff-only` (the guard's deny kind)."""
    if "merge" not in span.lower():
        return False
    import shlex

    from coordinator_core.bash_guards.block_subagent_destructive_action import _real_git_subcommand

    lexer = shlex.shlex(span.replace("\\", "/"), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return False
    segments: list = [[]]
    for token in tokens:
        if token in _SEGMENT_BREAKS:
            segments.append([])
        else:
            segments[-1].append(token)
    for segment in segments:
        segment = _strip_command_prefix(segment)
        if segment and _GIT_ARGV0.search(segment[0]):
            sub, ambiguous, remaining = _real_git_subcommand(segment[1:])
            if ambiguous:
                # The guard resolves an unparseable global option conservatively; so does the plan bar.
                if "merge" in segment[1:] and "--ff-only" not in segment:
                    return True
            elif sub == "merge" and "--ff-only" not in remaining:
                return True
    return False


def executor_merge_act(raw: Dict[str, Any]) -> Optional[str]:
    """The first body code span an executor would run as `git merge` (not --ff-only), or None.

    None when row_is_em_performed(raw). The subagent guard always denies the span, so the row
    changes nothing; the plan must put the merge on an EM-owned row.
    """
    if row_is_em_performed(raw):
        return None
    body = raw.get("body")
    if not isinstance(body, str):
        return None
    for span in _code_spans(body):
        span = span.strip()
        if span and _span_merges(span):
            return span
    return None


#: Agent types naming the EM itself: no agent definition exists, so the row is the EM's to discharge.
EM_OWNED_AGENT_TYPES = frozenset({"coordinator:em"})


_RULING_ASK = re.compile(r"^\s*ask\s+`?coordinator:apm\b", re.IGNORECASE)


def ruling_ask_act(raw: Dict[str, Any]) -> bool:
    """True when the row's act is asking the APM for a ruling: no write act, and the body's first
    non-blank line opens `Ask coordinator:apm`. An executor holds no agent-spawn or SendMessage tool."""
    if raw.get("writes") or raw.get("writes_under") or raw.get("appends"):
        return False
    body = raw.get("body")
    if not isinstance(body, str):
        return False
    for line in body.splitlines():
        if line.strip():
            return _RULING_ASK.match(line) is not None
    return False


_CODE_SUFFIXES = (".py", ".ts", ".js", ".cs", ".cpp", ".h", ".ps1")


def code_deliverable(raw: Dict[str, Any]) -> bool:
    """True when a `writes` or `writes_under` entry names a code file (`_CODE_SUFFIXES`)."""
    for key in ("writes", "writes_under"):
        entries = raw.get(key)
        if isinstance(entries, str):
            entries = [entries]
        if not isinstance(entries, (list, tuple)):
            continue
        for entry in entries:
            if isinstance(entry, str) and entry.strip().lower().endswith(_CODE_SUFFIXES):
                return True
    return False


def _names_family_runner(raw: Dict[str, Any]) -> bool:
    """True when the row's `family` is non-claude: the family names the runner.

    An invalid family is True too, so the declared legs yield False and
    `spine_read._row_family_dispatch` raises its refusal rather than the row going EM-owed.
    """
    try:
        return stage_family.resolve(raw.get("family")) != stage_family.FALLBACK
    except ValueError:
        return True


def row_is_em_performed(raw: Dict[str, Any]) -> bool:
    """True for a declared `performer: em` or `em_run: true` row, an `agent_type` in
    `EM_OWNED_AGENT_TYPES`, a ruling-ask row, one whose only act is agent-forbidden, or one blocked on
    a capability only the EM session has.

    INVARIANT: the declared-field legs (performer, em_run, agent_type) yield False for a
    `code_deliverable` row (code routes to an executor) and for a row naming a non-claude `family`
    (the family runner). `forbidden_sole_act` and `em_only_capability`
    still hold: a genuine EM-only capability stays EM.
    """
    declared = not code_deliverable(raw) and not _names_family_runner(raw) and (
        raw.get("performer") == "em"
        or raw.get("em_run") is True
        or raw.get("agent_type") in EM_OWNED_AGENT_TYPES
    )
    return (
        declared
        or ruling_ask_act(raw)
        or forbidden_sole_act(raw) is not None
        or em_only_capability(raw) is not None
    )
