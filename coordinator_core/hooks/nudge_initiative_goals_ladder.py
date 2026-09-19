"""coordinator_core.hooks.nudge_initiative_goals_ladder — PostToolUse
(Write|Edit) advisory op: when a file is Written or Edited under
state/initiatives/*.yaml AND the written initiative has an empty/absent
`goals` field AND the repo has >=1 goal under state/goals/*.yaml, advise
with matching candidate goal-ids. Never blocks the write.

Port of: DoE-claude `coordinator/hooks/scripts/nudge-initiative-goals-ladder.py`
(docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C12). Shape per the
W4-C1 verdict: command/native-door — no coordinator/bin shim, no http
registration.

Shape changes, all forced by this row's own op contract, none a behaviour
change (mirrors `nudge_plan_test_surface_tier.py`'s own W4-C7 arrival note):

  (a) stdin/stdout JSON I/O becomes the `params`-dict-in / envelope-dict-out
      contract every op in this package shares.
  (b) The source's own cross-plane `_engine_root.resolve_claude_klabauter_root()` +
      `place_engine_root_on_path` seam (reached FROM OUTSIDE this repo)
      collapses to a plain same-repo import of `coordinator_core.ops.
      goals_match` — already this tree, no plane boundary to cross, no
      sys.path surgery, no "engine root unresolvable" failure mode.
  (c) `_git_toplevel` (a `git rev-parse --show-toplevel` subprocess spawn,
      used twice per fire in the DoE source) becomes
      `coordinator_core.git.repo_root.show_toplevel` — a zero-spawn,
      walk-only equivalent already load-bearing elsewhere in this tree
      (`context_pressure_precompact.py`'s own `_resolve_state_root` uses
      the same seam). Two fewer process spawns per fire, same semantics
      (returns None on the same failure cases the bash oracle's `git`
      invocation would exit 128 on).
  (d) `_message_envelope`'s `emit(message, CHANNEL_STOP)` (a
      stderr-writing side effect shaped for the old stdin/stdout Stop-family
      entrypoint) becomes `compose()` + `render()` +
      `coordinator_core._hook_envelope.post_advisory`, returning the
      envelope dict directly — this hook fires on PostToolUse, and
      `post_advisory` IS this package's PostToolUse advisory shape (see
      `postuse_advisory_dispatch.py` for the sibling convention).
  (e) `goal.match_candidates` is called via `coordinator_core.ipc.
      get_op_handler` directly (already imported/registered in-process by
      this engine) rather than a fresh `import coordinator_core.ops.
      goals_match` + `asyncio.run(asyncio.wait_for(...))` dance reached
      from a foreign interpreter — the op is already warm here; no event
      loop needs spinning up for a single await. `_handler` below is
      `async def` and awaits the coroutine directly, matching this
      package's other async op handlers (e.g. `session_heartbeat.py`).

Escape hatch (unchanged): `COORDINATOR_INITIATIVE_GOALS_NUDGE_OFF=1`
(autonomous runs) suppresses this hook entirely.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md, chunk W4-C12.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional

from coordinator_core.hooks._envelope import no_advisory, post_advisory
from coordinator_core.hooks.support.message_envelope import compose, render
from coordinator_core.ipc import get_op_handler, register_op

#: See state/relocations/guard-message-cap/nudge-initiative-goals-ladder.py.md
#: for the full explanation this hook's message used to spell out inline
#: (docs/plans/2026-08-02-guard-message-character-cap.md § C6).
_WIKI_ANCHOR = (
    "coordinator/docs/wiki/guard-message-concision.md"
    "#initiative-goals-nudge-remedies"
)


async def _resolve_goal_candidates(repo_root: str, text: str) -> list:
    """In-process call into `goal.match_candidates`. Fail-open at every
    step (unresolvable op / handler exception / non-list result) -> [] — a
    nudge with no suggestions is still a valid, safe nudge."""
    if not text:
        return []

    try:
        import coordinator_core.ops.goals_match  # noqa: F401 -- registers the op
        from coordinator_core.lifecycle import git_common_dir

        handler = get_op_handler("goal.match_candidates")
        if handler is None:
            return []

        common_dir = git_common_dir(Path(repo_root))
        result = handler({"text": text}, repo_root=common_dir)
        if hasattr(result, "__await__"):
            result = await result
    except Exception:
        return []

    if not isinstance(result, dict):
        return []
    candidates = result.get("candidates")
    if not isinstance(candidates, list):
        return []
    return candidates


def _compose_nudge_message(initiative_id: str, candidate_ids: List[str], candidate_ids_str: str):
    """Pure message composer, routed through `message_envelope.compose`. See
    `_WIKI_ANCHOR` for the relocated explanation of the escape-hatch env var
    and remedy shapes."""
    if candidate_ids_str:
        prose = (
            "Initiative {} has no goals field; candidate goal(s): {}. Attach "
            "one, or ignore -- nothing is blocked.".format(initiative_id, candidate_ids_str)
        )
        first_id = candidate_ids[0] if candidate_ids else ""
        alternative = "coordinator-initiative attach --goals {} state/initiatives/{}.yaml".format(
            first_id, initiative_id
        )
    else:
        prose = (
            "Initiative {} has no goals field, and this repo has goal(s) "
            "under state/goals/. Tag one, or ignore -- nothing is "
            "blocked.".format(initiative_id)
        )
        alternative = "coordinator-initiative attach --goals <goal-id> state/initiatives/{}.yaml".format(
            initiative_id
        )
    return compose(prose, alternative=alternative, anchor=_WIKI_ANCHOR)


def _extract_write_fields(params: dict) -> tuple:
    tool_input = params.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    tool_name = params.get("tool_name") or ""
    file_path = tool_input.get("file_path") or ""
    content_raw = tool_input.get("content")
    if not content_raw:
        content_raw = tool_input.get("new_string")
    content = ""
    if isinstance(content_raw, str) and content_raw:
        # Mirrors `head -60` (first 60 lines of the extracted content).
        content = "\n".join(content_raw.splitlines()[:60])
    return tool_name, file_path, content


async def _handle(params: dict) -> dict:
    if os.environ.get("COORDINATOR_INITIATIVE_GOALS_NUDGE_OFF", "0") == "1":
        return no_advisory()

    tool_name, file_path, content = _extract_write_fields(params)

    if tool_name not in ("Write", "Edit"):
        return no_advisory()
    if not file_path:
        return no_advisory()

    file_path_norm = file_path.replace("\\", "/")

    base = os.path.basename(file_path_norm)
    dirpart = file_path_norm[: -len(base)] if base else file_path_norm
    is_initiative_yaml = base.endswith(".yaml") and (
        dirpart.endswith("/state/initiatives/") or dirpart == "state/initiatives/"
    )
    if not is_initiative_yaml:
        return no_advisory()

    lines: List[str] = []
    if content:
        lines = content.split("\n")

        inline_re = re.compile(r"^goals:[ \t]*[^ \t#\[]")
        goals_line_re = re.compile(r"^goals:")
        for line in lines:
            if inline_re.match(line):
                goals_val = goals_line_re.sub("", line, count=1)
                goals_val = goals_val.lstrip(" \t")
                if goals_val not in ("null", "", "[]", "~"):
                    return no_advisory()  # non-empty value -- suppress
                break

        list_item_re = re.compile(r"^[ \t]+-")
        for i, line in enumerate(lines):
            if goals_line_re.match(line):
                if i + 1 < len(lines) and list_item_re.match(lines[i + 1]):
                    return no_advisory()
                break

    from coordinator_core.git.repo_root import show_toplevel

    repo_root: Optional[str] = None
    dir_of_file = os.path.dirname(file_path_norm)
    if dir_of_file and os.path.isdir(dir_of_file):
        repo_root = show_toplevel(dir_of_file)
    if not repo_root:
        cwd = params.get("cwd") if isinstance(params.get("cwd"), str) else None
        repo_root = show_toplevel(cwd or os.getcwd())
    if not repo_root:
        return no_advisory()

    goals_dir = Path(repo_root) / "state" / "goals"
    if not goals_dir.is_dir():
        return no_advisory()
    goal_count = sum(1 for f in goals_dir.glob("*.yaml") if f.is_file())
    if goal_count == 0:
        return no_advisory()

    match_text = ""
    if content:
        label_re = re.compile(r"^label:[ \t]+(.*)$")
        desc_re = re.compile(r"^description:[ \t]+(.*)$")
        for line in lines:
            m = label_re.match(line)
            if m:
                match_text = m.group(1).replace('"', "")
                break
        if not match_text:
            for line in lines:
                m = desc_re.match(line)
                if m:
                    match_text = m.group(1).replace('"', "")[:120]
                    break
    if not match_text:
        stem = base
        if stem.endswith(".yaml"):
            stem = stem[: -len(".yaml")]
        match_text = stem

    candidates = await _resolve_goal_candidates(repo_root, match_text)
    candidate_ids = []
    for c in candidates[:3]:
        if isinstance(c, dict):
            gid = c.get("goal_id")
            if gid:
                candidate_ids.append(gid)
    candidate_ids_str = " or ".join(candidate_ids)

    initiative_id = base[: -len(".yaml")] if base.endswith(".yaml") else base

    message = _compose_nudge_message(initiative_id, candidate_ids, candidate_ids_str)
    return post_advisory(render(message, env=params.get("env")))


@register_op("hooks.nudge_initiative_goals_ladder")
async def _handler(params: dict, repo_root=None) -> dict:
    """PostToolUse(Write|Edit) op: advise (never deny/block) when a written
    initiative has no `goals` field and the repo carries goal(s) to attach.
    """
    try:
        return await _handle(params if isinstance(params, dict) else {})
    except Exception:
        return no_advisory()
