"""
coordinator_core.ops.dispatch_shape_classify — post-hoc dispatch-shape observer (Flag 9).

Purpose: given a plan slug (or an explicit plan file), count the plan's declared
parallel-permitted chunk count — the `## Tasks` fenced ```yaml plan-tasks``` spine's
non-deferred row count — then count distinct EXECUTOR-CLASS agentIds recorded in the
EM session's `dispatched-agents.txt`. If N > 1 declared chunks were observed but only
1 distinct executor ran, emit a question-framed offer (never a verdict) to stderr.

REPOINT (2026-07-13, docs/plans/2026-07-13-retire-plan-body-dispatch-ledger.md
DEC-3 / C8): the plan-body `## Dispatch Ledger` markdown table this classifier used
to parse is RETIRED — the Workflow script (or the fan-out TSV, on the rare
hand-orchestrated carve-out) is now the sole wave-map. This classifier survives as
the hand-orchestrated carve-out path's serial-grind post-hoc backstop: the
structural-prevention argument for retiring it holds only on the Workflow path,
where each `agent()` call IS a distinct dispatch; it does NOT hold on the carve-out
path, where "N chunks declared but only 1 executor ran" remains reachable.

BINDING CONSTRAINTS (from the bash oracle's own the Staff Engineer-reviewed header, preserved):
  - F2: Offer text MUST be question-framed (ask, never accuse). Acknowledge
        pilot-then-expand as a valid shape that also presents as 1 agent.
  - F3: Filter agentId count to EXECUTOR-CLASS subagent_type only (exclude
        reviewers, scouts, personas). Executor-class: `general-purpose`,
        `coordinator:executor`, `feature-dev:*`. Non-executor: any `coordinator:*`
        except executor, `coordinator:staff-eng`, `coordinator:review-integrator`,
        persona names.
  - F4: Use a BOUNDED per-gate-group window derived from the em_sid session dir.
        Whole-session is the bound — finer granularity is not available without
        chunk-ids in the records (FORBIDDEN, see below).

FORBIDDEN MECHANISMS (unchanged from the oracle):
  - No temporal-overlap computation (dispatched-at is Agent RETURN-time, not
    dispatch-time; foreground dispatches are always recorded strictly serial).
  - No chunk-id-substring correlation (no chunk-id field exists in the records).

FIDELITY LIMIT (stated in offer text): the records do not carry a plan slug.
Attribution is scoped to the em_sid session directory. A multi-plan session will mix
agents from other plans — this is stated in the offer text so the EM can evaluate
accordingly.

OFFER SHAPE: exit 0 always (best-effort / never-block observer). Finding to stderr
only; stdout is unused. Silent on pass. A transport/import failure at the trampoline
layer degrades to exit 0 (loud on stderr) for the same reason — this is a
best-effort advisory tool, never a caller-facing blocking gate.

Port of: classify-dispatch-shape.sh (example-doctrine-repo b5a4192c, 2026-07-20, 339 lines)
Spec backlink: docs/plans/2026-06-22-invariant-verification-observers.md § C3
Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Negative-spec (do NOT "fix" while porting):
    - The bash oracle's `BASH_VERSINFO[0] < 4` guard (protecting `declare -A` usage)
      has no Python analogue — associative arrays are just `dict`/`set` here. Dropped,
      consistent with other bash-to-Python ports in this migration wave that
      carried the same bash-version guard with no Python equivalent to port it to.
    - Does NOT read a `## Dispatch Ledger` markdown table — that spine was retired
      2026-07-13; the declared-chunk-count input is the `## Tasks` fenced
      ```yaml plan-tasks``` spine's non-deferred row count, exactly as the bash
      oracle was repointed to do.
    - `_resolve_git_dir` deliberately does NOT chdir into the target directory before
      treating `git rev-parse --git-dir`'s (possibly relative) output as a path —
      this reproduces the bash oracle's own latent relative-path fragility (the
      oracle never `cd`s either; it string-concatenates `${GIT_DIR}/coordinator-sessions`
      against whatever `git -C "$dir" rev-parse --git-dir` printed, which git may emit
      relative to `$dir` rather than to the caller's cwd). Faithful-bug-repro, not a
      regression — do not "fix" this to an absolute resolution.
    - `em_sid_display` resolution independently re-checks `CLAUDE_CODE_SESSION_ID` /
      `em_sid` env vars rather than reusing whichever one `dispatched-agents.txt` was
      actually located under — this mirrors the oracle's own two-independent-lookups
      structure (a session-dir-resolution env lookup, then a SEPARATE display-text env
      lookup) verbatim, including the resulting oracle-bug where a stale
      `CLAUDE_CODE_SESSION_ID` (pointing at a since-removed session dir, so the agents
      file actually resolves via the mtime-fallback branch) still displays the stale
      env var rather than the fallback dir's basename.
    - Prefix-match candidate ordering (`--plan-file` NOT given; `<slug>.md` not found
      directly) uses `sorted(os.listdir(...))` rather than the oracle's
      `find | head -1` (filesystem/inode enumeration order, not lexical and not
      stable across filesystems). Both are "first hit under a weak, unspecified
      order" — this port picks a *deterministic* first hit rather than reproducing
      an already-nondeterministic oracle order.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_GIT_TIMEOUT_SECS = 10

_PROG = "classify-dispatch-shape.sh"

# ---------------------------------------------------------------------------
# F3: executor-class subagent_type filter
# ---------------------------------------------------------------------------
_EXECUTOR_EXACT = frozenset({"general-purpose", "coordinator:executor"})
_EXECUTOR_PREFIX = "feature-dev:"


def _is_executor_class(subagent_type: str) -> bool:
    """Executor-class iff general-purpose OR coordinator:executor OR feature-dev:* (F3)."""
    if subagent_type in _EXECUTOR_EXACT:
        return True
    return subagent_type.startswith(_EXECUTOR_PREFIX)


# ---------------------------------------------------------------------------
# Unit 1 — arg parsing + declared parallel-permitted chunk count
# ---------------------------------------------------------------------------

def _resolve_plan_file(plan_slug: str, script_dir: Optional[str]) -> Optional[str]:
    """Resolve a plan slug to a docs/plans/ file.

    Search order mirrors the bash oracle: $(pwd)/docs/plans, then (when script_dir
    is known — the example-doctrine-repo-side trampoline's own directory, NOT this claude-klabauter module's
    directory) REPO_ROOT/../../docs/plans and REPO_ROOT/../docs/plans, where
    REPO_ROOT = dirname(script_dir). First hit wins: exact `<slug>.md`, else a
    `*<slug>*.md` prefix match (see negative-spec re: ordering).
    """
    search_dirs = [os.path.join(os.getcwd(), "docs", "plans")]
    if script_dir:
        repo_root = os.path.dirname(script_dir)
        search_dirs.append(os.path.join(repo_root, "..", "..", "docs", "plans"))
        search_dirs.append(os.path.join(repo_root, "..", "docs", "plans"))

    for search_dir in search_dirs:
        if not os.path.isdir(search_dir):
            continue
        candidate = os.path.join(search_dir, f"{plan_slug}.md")
        if os.path.isfile(candidate):
            return candidate
        try:
            names = sorted(os.listdir(search_dir))
        except OSError:
            print(f"skip: _resolve_plan_file: names = sorted(os.listdir(search_dir)) failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue
        for name in names:
            if name.endswith(".md") and plan_slug in name:
                full = os.path.join(search_dir, name)
                if os.path.isfile(full):
                    return full
    return None


_TASKS_HEADING_RE = re.compile(r"^##[ \t]+Tasks[ \t]*$")
_HEADING_RE = re.compile(r"^##[ \t]")
_FENCE_OPEN_RE = re.compile(r"^```yaml[ \t]+plan-tasks[ \t]*$")
_FENCE_CLOSE_RE = re.compile(r"^```[ \t]*$")
_ROW_START_RE = re.compile(r"^-[ \t]+id:")
_DEFERRED_TRUE_RE = re.compile(r"^[ \t]+deferred:[ \t]*true[ \t]*(#.*)?$")


def _count_all_fence_opens(plan_text: str) -> int:
    """Count every ```yaml plan-tasks``` fence-open line anywhere in the document
    (not scoped to ## Tasks) — mirrors the oracle's unconstrained fence_hits pass,
    which is how a malformed doc with a second fenced block elsewhere in the file
    (e.g. under an unrelated heading) is detected as malformed."""
    return sum(1 for line in plan_text.splitlines() if _FENCE_OPEN_RE.match(line))


def _extract_tasks_spine_lines(plan_text: str) -> List[str]:
    """Extract the body lines of the single fenced ```yaml plan-tasks``` block that
    appears directly under a `## Tasks` heading. Mirrors the oracle's awk state
    machine (in_tasks / in_fence tracking with heading-boundary reset) line-for-line."""
    in_tasks = False
    in_fence = False
    lines: List[str] = []
    for raw_line in plan_text.splitlines():
        if _TASKS_HEADING_RE.match(raw_line):
            in_tasks = True
            continue
        if _HEADING_RE.match(raw_line) and not _TASKS_HEADING_RE.match(raw_line):
            if in_tasks and not in_fence:
                in_tasks = False
            continue
        if in_tasks and not in_fence and _FENCE_OPEN_RE.match(raw_line):
            in_fence = True
            continue
        if in_fence and _FENCE_CLOSE_RE.match(raw_line):
            in_fence = False
            continue
        if in_fence:
            lines.append(raw_line)
    return lines


def _count_spine_nondeferred_rows(plan_text: str) -> int:
    """Non-deferred `- id:` row count in the single ## Tasks fenced spine block.

    Fail-open (returns 0) exactly as the oracle does: zero or more-than-one fenced
    ```yaml plan-tasks``` blocks anywhere in the document, or an empty located
    block, is malformed/absent — same as "no ledger table" did pre-repoint.
    """
    fence_hits = _count_all_fence_opens(plan_text)
    spine_lines = _extract_tasks_spine_lines(plan_text)
    if fence_hits != 1 or not spine_lines:
        return 0

    total = 0
    started = False
    is_deferred_true = False

    def _flush() -> None:
        nonlocal total
        if started and not is_deferred_true:
            total += 1

    for line in spine_lines:
        if _ROW_START_RE.match(line):
            _flush()
            started = True
            is_deferred_true = False
            continue
        if started and _DEFERRED_TRUE_RE.match(line):
            is_deferred_true = True
    _flush()
    return total


# ---------------------------------------------------------------------------
# Unit 2 — session/agent-count resolution + signal evaluation
# ---------------------------------------------------------------------------

def _resolve_git_dir(near_path: str) -> Optional[str]:
    """Resolve the git-dir near `near_path`, falling back to resolving from the
    process cwd. Mirrors the oracle's two-rung
    `git -C "$(dirname "$PLAN_FILE")" rev-parse --git-dir || git rev-parse --git-dir`
    fallback — see module negative-spec re: relative-path fragility (deliberately
    preserved, not fixed)."""
    for cwd in (os.path.dirname(near_path) or ".", None):
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_SECS,
                stdin=subprocess.DEVNULL,
                creationflags=_CREATIONFLAGS,
            )
        except (OSError, subprocess.TimeoutExpired):
            print(f"skip: _resolve_git_dir: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue
        if result.returncode == 0:
            git_dir = result.stdout.strip()
            if git_dir:
                return git_dir
    return None


def _find_agents_file(
    sessions_dir: str,
    session_id_env: Optional[str],
    em_sid_env: Optional[str],
) -> Optional[str]:
    """Locate dispatched-agents.txt: CLAUDE_CODE_SESSION_ID dir, else em_sid dir,
    else the most-recently-modified session dir's file (maxdepth-2 equivalent —
    one level under sessions_dir)."""
    if session_id_env and os.path.isdir(os.path.join(sessions_dir, session_id_env)):
        return os.path.join(sessions_dir, session_id_env, "dispatched-agents.txt")
    if em_sid_env and os.path.isdir(os.path.join(sessions_dir, em_sid_env)):
        return os.path.join(sessions_dir, em_sid_env, "dispatched-agents.txt")

    if not os.path.isdir(sessions_dir):
        return None
    candidates: List[Tuple[float, str]] = []
    try:
        for entry in os.scandir(sessions_dir):
            if not entry.is_dir():
                continue
            candidate = os.path.join(entry.path, "dispatched-agents.txt")
            try:
                mtime = os.stat(candidate).st_mtime
            except OSError:
                print(f"skip: _find_agents_file: mtime = os.stat(candidate).st_mtime failed: {sys.exc_info()[1]}", file=sys.stderr)
                continue
            candidates.append((mtime, candidate))
    except OSError:
        print(f"skip: _find_agents_file: for entry in os.scandir(sessions_dir): failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


def _count_executor_agents(agents_file: str) -> int:
    """Distinct EXECUTOR-CLASS agentId count (F3), tab-delimited 4-column records.

    Columns: agentId | model | subagent_type | dispatched-at. Blank / `#`-comment
    lines skipped. Missing trailing columns treated as empty string (mirrors bash
    `read -r` under-assignment on short lines)."""
    seen = set()
    count = 0
    try:
        with open(agents_file, "r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line = raw_line.rstrip("\n").rstrip("\r")
                if not line or line.startswith("#"):
                    continue
                fields = line.split("\t")
                agent_id = fields[0] if len(fields) > 0 else ""
                subagent_type = fields[2] if len(fields) > 2 else ""
                if not agent_id or agent_id.startswith("#"):
                    continue
                if not _is_executor_class(subagent_type):
                    continue
                if agent_id not in seen:
                    seen.add(agent_id)
                    count += 1
    except OSError:
        print(f"skip: _count_executor_agents: with open(agents_file, \"r\", encoding=\"utf-8\", errors=\"replace\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return 0
    return count


_OFFER_TEMPLATE = """[classify-dispatch-shape] DISPATCH SHAPE QUESTION

The plan's ## Tasks spine declares {chunk_count} non-deferred chunks,
but only 1 distinct executor agent is attributable to session {em_sid}.

Was this a serial grind (one agent handling chunks sequentially), or an intentional
pilot-then-expand shape, or did the EM author some chunks inline?

If serial grind: consider re-dispatching with true fan-out parallelism — e.g.
  bash ~/.claude/plugins/coordinator/bin/fan-out-dispatch.sh <tsv>

If intentional (pilot-then-expand / inline EM / other valid shape): no action needed.

Fidelity note: records are scoped to session {em_sid} from
  {agents_file}
Multi-plan sessions may include agents from other plans. This classifier detects
the gross serial-grind antipattern; fine-grained interleaving within a session is
not distinguishable from the available records.

"""


def main(argv: List[str], *, script_dir: Optional[str] = None) -> int:
    """Entry point. Always returns 0 (offer-shaped observer — never blocks the
    caller); see module docstring § OFFER SHAPE. `script_dir` is the example-doctrine-repo-side
    trampoline's own directory (`os.path.dirname(os.path.abspath(__file__))` at
    the `.sh` trampoline, NOT this module's directory) — required to reproduce the
    oracle's REPO_ROOT-relative docs/plans/ search when a bare slug is given."""
    if not argv:
        sys.stderr.write("Usage: classify-dispatch-shape.sh <plan-slug>\n")
        sys.stderr.write("       classify-dispatch-shape.sh --plan-file <path>\n")
        return 0

    plan_file: Optional[str] = None
    plan_slug: Optional[str] = None

    if argv[0] == "--plan-file":
        if len(argv) < 2:
            sys.stderr.write(f"{_PROG}: --plan-file requires a path argument\n")
            return 0
        plan_file = argv[1]
    else:
        plan_slug = argv[0]
        plan_file = _resolve_plan_file(plan_slug, script_dir)

    if not plan_file or not os.path.isfile(plan_file):
        label = plan_slug if plan_slug else plan_file
        sys.stderr.write(
            f"{_PROG}: plan file not found for '{label}' — skipping check\n"
        )
        return 0

    try:
        plan_text = Path(plan_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        # Fail-open, silently — mirrors the oracle's awk reading an unreadable
        # file: no diagnostic, parallel_chunk_count resolves to 0 either way.
        return 0

    parallel_chunk_count = _count_spine_nondeferred_rows(plan_text)
    if parallel_chunk_count <= 1:
        return 0

    git_dir = _resolve_git_dir(plan_file)
    if not git_dir:
        return 0
    sessions_dir = os.path.join(git_dir, "coordinator-sessions")

    session_id_env = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    em_sid_env = os.environ.get("em_sid", "")

    agents_file = _find_agents_file(
        sessions_dir, session_id_env or None, em_sid_env or None
    )
    if not agents_file or not os.path.isfile(agents_file):
        return 0

    if session_id_env:
        em_sid_display = session_id_env
    elif em_sid_env:
        em_sid_display = em_sid_env
    else:
        em_sid_display = os.path.basename(os.path.dirname(agents_file))

    distinct_executor_count = _count_executor_agents(agents_file)

    if parallel_chunk_count > 1 and distinct_executor_count == 1:
        sys.stderr.write(
            _OFFER_TEMPLATE.format(
                chunk_count=parallel_chunk_count,
                em_sid=em_sid_display,
                agents_file=agents_file,
            )
        )

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
