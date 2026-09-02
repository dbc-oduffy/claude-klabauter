"""
coordinator_core.hooks.plan_persistence_check — warm-door counterpart of
DoE-claude's `coordinator/hooks/scripts/plan-persistence-check.py`
(PostToolUse(ExitPlanMode) hook).

Purpose: reads the approved plan from `tool_response.plan` and persists it to
`docs/plans/<YYYY-MM-DD>-<slug>.md` in the calling repo, NEVER committing —
committing from a hook fire would bypass every PreToolUse commit-safety
matcher. Returns a `post_advisory` envelope pre-filling the exact scoped
commit command plus the subagent-review-artifact reminder — the same shape
the source script printed to stdout.

Built against `docs/reference/warm-hook-migration.md` (candidate-selection
input) and this plan's own C5 dispatch brief
(`state/dispatch-briefs/2026-08-31-six-hook-scripts-become-engine-ops/C5.md`).
Spec backlink (source script): DoE-claude's own
`docs/plans/2026-06-18-plan-persistence-hook-automation.md`.

CORRECTION (coordinatorcode-reviewer.a986dd968d6771f99, Finding 1): the two
spawns named below are the ones THIS ROW owns and eliminates — they are NOT
the whole spawn picture for a firing payload. `persist_captured_plan`'s own
routed success path (the dominant case) still shells out to
`coordinator-doc-new.py --type plan` via `plan_capture_persist.
invoke_coordinator_doc_new`'s `subprocess.run(..., timeout=60)` — a third,
larger spawn that survives one level down in code this chunk did not touch.
Porting that scaffolder in-process is its own plan (`coordinator-doc-new.py`
is 7226 lines); not attempted here. See
`coordinator_core/ops/plan_capture_persist.py::invoke_coordinator_doc_new`
for the residual, and `test_op_returns_post_advisory_shape_for_a_firing_payload`
in this module's test file for the pinned assertion.

THE TWO SPAWNS THIS ROW ELIMINATES, per the dispatch brief (both already
reachable in-process, no port needed for either):
  - `git rev-parse --show-toplevel` -> `coordinator_core.git.repo_root.
    show_toplevel` — a non-spawning, cwd-keyed-memoized parent walk that
    mirrors the CLI form byte-for-byte (see that function's own docstring
    for the four-way probe establishing this). This is the direct drop-in
    replacement for the source script's `_git_toplevel()`; the source
    script never reads a HEAD sha or the index, so `coordinator_core.git.
    git_state` (whose surface is `head_sha`/`read_index`/tree-spine reads)
    has no call site here, and `coordinator_core.git.git_dir.
    resolve_git_dir` likewise has none — this script never inspects
    whether `.git` is a directory or a worktree pointer file. Both are
    real precedent for "pure-Python git reads exist in this codebase," and
    both were checked against this script's actual body before being
    ruled inapplicable to it specifically.
  - The `plan-capture-persist.py` trampoline -> `coordinator_core.ops.
    plan_capture_persist.persist_captured_plan`, called DIRECTLY as a
    Python function. The source script shelled out to that trampoline as a
    subprocess and parsed its one-line JSON stdout; there is no subprocess
    boundary left to cross once caller and callee share one process, so
    this op calls the pure orchestration function instead of the
    JSON-RPC-wrapped `_handler` (params-shape marshalling has no purpose
    when both callers are Python in the same interpreter).

Every input comes from `params["payload"]` — the shape `warm/hook_http.py ::
payload_from_event` builds from the fired event — NEVER from `os.environ` or
this process's own `cwd`. The resident engine serves ~50 concurrent
sessions; its own process environment and cwd belong to none of them. In
particular `CLAUDE_HOME`/`HOME`/`USERPROFILE`/`CLAUDE_PROJECT_DIR` (the four
vars `docs/reference/warm-hook-migration.md` names as this script's own
env-var finding) are read here from `payload["env"]`, never
`os.environ.get(...)`. CLOSED 2026-09-02: all four are named in
`hook_http.FORWARDED_ENV_NAMES` and thread end-to-end over an http
registration built per `docs/reference/warm-hook-migration.md` § Step 3.
Until then `FORWARDED_ENV_PREFIXES` dropped every one of them after the
header arrived, so a correctly-written registration still left this op
resolving home against the ENGINE host — silently. Adding a fifth env read
here means adding its name to that list too; a name outside it is now
refused loudly rather than dropped, so the failure is visible, but it is
still a failure.

Three behavior changes from the source script, named rather than silently
inherited or silently fixed:

  1. DROPPED: the source script's third repo-root rung, `_git_toplevel(None)`
     — "git rev-parse --show-toplevel" run with no `-C`, defaulting to the
     CALLING PROCESS's own cwd. On a resident engine that cwd is the
     engine's own, not any session's, so this rung would resolve the wrong
     repo (or the engine's own repo) for every caller. This is the same
     "never an ambient walk from this process's own cwd" contract
     `postuse_advisory_dispatch.py::_check_runtime_tripwire_sync`'s repo-root
     docstring already states for a sibling op — required by the payload-in
     contract, not a new policy choice.
  2. RESOLVED HOME LADDER READS `payload["env"]`, not `os.environ` — the
     source script's `_claude_home_dir()` fail-loud CLAUDE_HOME contract
     ("a set-but-empty or non-absolute CLAUDE_HOME... fails OPEN THE WHOLE
     HOOK") is preserved verbatim, just against the payload's env mapping.
     The source script's FIRST home-resolution rung — importing this very
     engine's own `claude_home_shim.resolve_home_base()` via a
     `_resolve_claude_klabauter_root()` sys.path dance — is dropped entirely: that
     dance exists so a script running OUTSIDE this engine can find it. This
     op already runs INSIDE the engine, and `resolve_home_base()`/
     `home_dir()` read `os.environ` directly (the ENGINE's own ambient env,
     not any session's override) — using it here would silently defeat the
     per-session CLAUDE_HOME test-isolation guarantee the fail-loud contract
     exists for. `Path.home()` (final rung, on every miss) is a fixed
     ENGINE-HOST machine fact, not per-session state — same precedent
     `nudge_autonomous_askuserquestion.py::_resolve_posture` already
     establishes for `os.path.expanduser("~")`.
  3. WIKI ANCHOR CITED, NOT RESOLVED — the source script's `_WIKI_ANCHOR`
     ("coordinator/docs/wiki/guard-message-concision.md#plan-persistence-check")
     is rewritten by DoE-claude's own `_message_envelope.resolve_wiki_citation`
     into an absolute path anchored at THAT repo's own `__file__`-derived
     `coordinator/` directory before being printed. This op has no reliable,
     already-pinned way to locate DoE-claude's checkout on an arbitrary
     machine (the reverse of `coordinator_core.engine_root`'s own
     DoE-side-finds-claude-klabauter direction; no such find-DoE-from-claude-klabauter seam
     exists today), and that resolution is itself DoE-plane wiki-citation
     policy this engine does not own (`CLAUDE.md` § What this repo is:
     coordinator-claude owns "every discovery-resolved surface"). The
     citation is emitted as the same repo-relative literal the source
     script authored — a known, bounded degradation (a reader outside
     DoE-claude gets a path that does not resolve from their own cwd,
     exactly the pre-C2-fix behavior that fix corrected) rather than an
     invented cross-repo resolver. Every OTHER guard behavior (the fail-open
     direction on every I/O error, the collision/idempotent/persisted
     three-way branch, the no-index-lock negative spec) is unchanged.

Two write paths, in preference order, unchanged from the source script:
  1. `coordinator_core.ops.plan_capture_persist.persist_captured_plan` —
     scaffolds a schema-compliant artifact. Any outcome OTHER than
     "ok"/"idempotent"/"collision" (including a caught exception; the
     trampoline CLI's own subprocess/timeout/JSON-parse failure mode is gone
     — this call is in-process, not shelled — but `persist_captured_plan`
     ITSELF still shells out one level down to `coordinator-doc-new.py`
     with a 60s timeout on its routed success path, per the correction
     above; a genuine bug or a hang in that function must not crash an
     advisory hook) falls through to path 2 below — never plan loss.
  2. Fallback: the verbatim raw write. Reached whenever path 1 is
     unavailable for ANY reason. Its worst case is exactly the pre-routing
     status quo (a captured-but-gate-invisible plan), never plan loss.

Negative spec (verbatim from the source script) — this hook takes NO git
index lock, by PM ruling (2026-08-07). Do not reintroduce a `git add` here —
not with a retry, not with `--no-optional-locks`. Persist to disk, print the
command, take no lock.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.hooks._envelope import no_advisory, post_advisory
from coordinator_core.ipc import register_op
from coordinator_core.ops.plan_capture_persist import persist_captured_plan

#: This op's own repo root, computed structurally from `__file__` — mirrors
#: `coordinator_core.ops.invoke_from_argv._ENGINE_ROOT` and its siblings.
#: Used only for the meta-repo-routing branch below: unlike the source
#: script (which had to resolve ITS OWN host engine via `_resolve_claude_klabauter_root()`
#: from a DIFFERENT repo), this op already runs inside that engine, so its
#: own root is a fixed fact of this file's location, never something to
#: resolve at call time.
_ENGINE_ROOT = Path(__file__).resolve().parents[2]

#: Degraded (unresolved) verbatim from the source script's `_WIKI_ANCHOR` —
#: see module docstring point 3 for why this op does not attempt to
#: absolutize it the way DoE-claude's `_message_envelope.py` does.
_WIKI_ANCHOR = "coordinator/docs/wiki/guard-message-concision.md#plan-persistence-check"

#: Generator-provenance declaration (generator_provenance.py). This op fires
#: for whichever repo the calling session's `ExitPlanMode` happened in
#: (`repo_root_path`, resolved via `show_toplevel()` off the caller's own
#: cwd -- meta-repo routing can redirect it to `_ENGINE_ROOT` instead, see
#: the meta-repo branch above), and the filename it writes is `<today>-
#: <slug>.md` where `slug` is derived from the plan text (`_derive_slug`).
#: Neither the target repo nor the target filename is fixed ahead of time,
#: same shape as DoE-claude's `coordinator-doc-new.py` (acknowledged in
#: `state/generator-provenance/unresolved-writers.json` for the identical
#: reason: "mints an operator/title-derived doc path ... a data-dependent
#: target set; GENERATES cannot express it"). `docs/README.md`'s append is
#: likewise conditional on which repo's README got selected.
GENERATES = []


# ---------------------------------------------------------------------------
# claude-home / meta-repo detection (payload-env ladder, see module
# docstring point 2)
# ---------------------------------------------------------------------------


def _claude_home_dir(env: Mapping) -> Path:
    """Resolved ~/.claude directory for the FIRING SESSION, read from
    `payload["env"]` — never `os.environ`. Verbatim port of the source
    script's fail-loud CLAUDE_HOME contract; see module docstring point 2
    for why the source script's first (engine-shim) rung is dropped rather
    than ported."""
    claude_home = env.get("CLAUDE_HOME")
    if claude_home is not None:
        if not claude_home:
            raise ValueError("CLAUDE_HOME is set but empty")
        p = Path(claude_home)
        if not p.is_absolute():
            raise ValueError(f"CLAUDE_HOME must be an absolute path; got {claude_home!r}")
        return p / ".claude"
    home = env.get("HOME")
    if home and Path(home).is_absolute():
        return Path(home) / ".claude"
    userprofile = env.get("USERPROFILE")
    if userprofile and Path(userprofile).is_absolute():
        return Path(userprofile) / ".claude"
    # Fixed ENGINE-HOST machine fact, not per-session state — see module
    # docstring point 2.
    return Path.home() / ".claude"


def _canon(p) -> str:
    try:
        return os.path.normcase(os.path.realpath(str(p)))
    except Exception:
        return os.path.normcase(str(p))


def _is_meta_repo(repo_root, env: Mapping) -> bool:
    return _canon(repo_root) == _canon(_claude_home_dir(env))


# ---------------------------------------------------------------------------
# slug derivation (pure, verbatim from the source script)
# ---------------------------------------------------------------------------


def _derive_slug(plan_content: str) -> str:
    import re

    h1_line = None
    for line in plan_content.splitlines():
        if line.startswith("# "):
            h1_line = line
            break

    if h1_line is not None:
        h1_text = h1_line[2:]
        h1_lower = h1_text.lower()
        h1_slug = re.sub(r"[^a-z0-9]+", "-", h1_lower)
        slug = h1_slug.strip("-")
        slug = slug[:60]
        slug = slug.rstrip("-")
        return slug

    return "plan-" + datetime.now(timezone.utc).strftime("%H%M%S")


def _local_day() -> str:
    """Local calendar day, YYYY-MM-DD. This op's clock is the ENGINE HOST's
    clock; the harness/engine share one machine (DR-215: spawn-per-call
    invocations, no cross-host daemon), so this matches the source script's
    own `date -I` (local TZ) exactly."""
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# message composition — a minimal local flattener, NOT DoE-claude's
# `_message_envelope.compose`/`render`/280-char-ceiling machinery. That
# module also validates alternative-block SHAPE and enforces a
# DoE-plane-authored prose cap on hand-authored text; both are authoring-time
# concerns over already-fixed, already-shipped literals below, not
# per-request branching logic this op needs to reproduce. See module
# docstring point 3 for the wiki-anchor-specific piece of this same
# closure-vs-decision-logic boundary.
# ---------------------------------------------------------------------------


def _flatten(prose: str, *, alternative: Optional[str] = None, anchor: Optional[str] = None) -> str:
    parts = [prose]
    if alternative:
        parts.append("")
        parts.append("```\n" + alternative.rstrip("\n") + "\n```")
    if anchor:
        parts.append("")
        parts.append(f"See {anchor}.")
    return "\n".join(parts)


def _idempotent_text(commit_cmd: str) -> str:
    return _flatten(
        "PLAN ALREADY PERSISTED (byte-identical) -- commit if not "
        "yet done, route the body through coordinator:sizing to close "
        "it out, and write review artifacts to disk.",
        alternative=commit_cmd,
        anchor=_WIKI_ANCHOR,
    )


def _collision_text(target_display: str) -> str:
    return _flatten(
        "Plan-slug collision: a DIFFERENT plan already exists at this "
        "path. Not overwritten -- resolve manually before committing.",
        alternative=target_display,
        anchor=_WIKI_ANCHOR,
    )


def _persisted_text(commit_cmd: str) -> str:
    return _flatten(
        "PLAN PERSISTED, not staged -- commit it now; a peer sweep can "
        "delete it until it lands. Route the body through coordinator:sizing "
        "to close it out. Write review artifacts to disk.",
        alternative=commit_cmd,
        anchor=_WIKI_ANCHOR,
    )


def _commit_cmd(plans_git_root: Path, repo_root: Path, rel_paths: list, slug: str) -> str:
    """Prefilled scoped-commit command for the persisted plan (and README
    row). Cross-repo (meta-repo routing sent the write into the engine
    checkout) gets an explicit `git -C "<root>"` prefix; same-repo keeps the
    bare form. Verbatim port."""
    paths = " ".join(rel_paths)
    if str(plans_git_root) != str(repo_root):
        return (
            f'git -C "{plans_git_root}" add -- {paths} && '
            f'git -C "{plans_git_root}" commit -m "plan: {slug}" -- {paths}'
        )
    return f'git add -- {paths} && git commit -m "plan: {slug}" -- {paths}'


def _append_readme_row(docs_readme: Path, readme_row: str) -> bool:
    """Idempotently append the engine-supplied Plans-section row. Verbatim
    port of the source script's own append."""
    if not readme_row or not docs_readme.is_file():
        return False
    try:
        existing = docs_readme.read_text(encoding="utf-8")
    except Exception:
        return False
    if readme_row.strip() in existing:
        return False
    try:
        with docs_readme.open("a", encoding="utf-8", newline="") as fh:
            fh.write(f"\n{readme_row.rstrip()}\n")
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------


@register_op("hooks.plan_persistence_check")
def _handler(params: dict, repo_root=None) -> dict:
    """PostToolUse(ExitPlanMode): persist an approved plan to
    `docs/plans/<date>-<slug>.md` in the firing repo, or the meta-repo
    reroute target when the firing repo IS the operator's own `~/.claude`.

    `params["payload"]` is the dict `warm/hook_http.py :: payload_from_event`
    builds from the fired event. Every input this handler reads —
    `tool_name`, `tool_response`, `cwd`, `env` — comes from that payload,
    never from `os.environ` or this process's own `cwd`.

    Activation predicate (identical to the source script):
      - tool_name must be ExitPlanMode
      - tool_response.isAgent must NOT be true (a subagent's internal
        ExitPlanMode is not a PM-approved plan)
      - tool_response.plan must be non-empty
      - a repo root must be discoverable from payload["env"]["CLAUDE_PROJECT_DIR"]
        or payload["cwd"] (see module docstring point 1 for the dropped
        third rung)
      - EITHER docs/plans/ OR docs/README.md must exist at the effective
        target (never auto-creates docs/plans/ in an arbitrary repo)

    Returns `no_advisory()` (empty dict) on any suppression/guard miss;
    otherwise `post_advisory(<text>)` — the same hookSpecificOutput shape
    the source script printed to stdout.
    """
    payload = params.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}

    tool_name = payload.get("tool_name") or ""
    if not isinstance(tool_name, str):
        tool_name = str(tool_name)
    if tool_name != "ExitPlanMode":
        return no_advisory()

    tool_response = payload.get("tool_response")
    if not isinstance(tool_response, Mapping):
        tool_response = {}

    is_agent_raw = tool_response.get("isAgent", False)
    if str(is_agent_raw).lower() == "true":
        return no_advisory()

    plan_content = tool_response.get("plan", "") or ""
    if not isinstance(plan_content, str):
        plan_content = str(plan_content)
    # Bash-oracle parity: unconditionally strip ALL trailing newlines (see
    # source script for the byte-visible rationale).
    plan_content = plan_content.rstrip("\n")
    if not plan_content:
        return no_advisory()

    env = payload.get("env")
    if not isinstance(env, Mapping):
        env = {}

    cwd = payload.get("cwd") or ""
    if not isinstance(cwd, str):
        cwd = ""

    claude_project_dir = env.get("CLAUDE_PROJECT_DIR") or ""
    if not isinstance(claude_project_dir, str):
        claude_project_dir = ""

    resolved_root = None
    if claude_project_dir and Path(claude_project_dir).is_dir():
        resolved_root = show_toplevel(claude_project_dir)
    if not resolved_root and cwd and Path(cwd).is_dir():
        resolved_root = show_toplevel(cwd)
    if not resolved_root:
        return no_advisory()

    repo_root_path = Path(resolved_root)

    try:
        is_meta = _is_meta_repo(repo_root_path, env)
    except ValueError:
        return no_advisory()

    docs_plans_dir = repo_root_path / "docs" / "plans"
    docs_readme = repo_root_path / "docs" / "README.md"
    plans_git_root = repo_root_path

    if is_meta:
        docs_plans_dir = _ENGINE_ROOT / "docs" / "plans"
        docs_readme = _ENGINE_ROOT / "docs" / "README.md"
        plans_git_root = _ENGINE_ROOT

    if not docs_plans_dir.is_dir() and not docs_readme.is_file():
        return no_advisory()

    slug = _derive_slug(plan_content)
    today = _local_day()
    target_name = f"{today}-{slug}.md"
    target_path = docs_plans_dir / target_name

    # --- Routed persist (preferred): direct in-process call, no subprocess ---
    try:
        routed = persist_captured_plan(plan_content, repo_root=plans_git_root)
    except Exception:
        routed = None

    if isinstance(routed, dict):
        status = routed.get("status")
        if status == "collision":
            return post_advisory(_collision_text(str(target_path)))
        if status in ("ok", "idempotent") and routed.get("path"):
            rel_paths = [routed["path"]]
            if _append_readme_row(docs_readme, routed.get("readme_row") or ""):
                rel_paths.append("docs/README.md")
            cmd = _commit_cmd(plans_git_root, repo_root_path, rel_paths, slug)
            text = _idempotent_text(cmd) if status == "idempotent" else _persisted_text(cmd)
            return post_advisory(text)

    # --- Fallback: raw verbatim write ---
    if not docs_plans_dir.is_dir():
        try:
            docs_plans_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return no_advisory()

    if target_path.is_file():
        try:
            existing = target_path.read_text(encoding="utf-8")
        except Exception:
            existing = ""
        if existing == plan_content:
            cmd = _commit_cmd(
                plans_git_root, repo_root_path, [f"docs/plans/{target_name}"], slug
            )
            return post_advisory(_idempotent_text(cmd))
        return post_advisory(_collision_text(str(target_path)))

    try:
        with target_path.open("w", encoding="utf-8", newline="") as fh:
            fh.write(plan_content)
    except Exception:
        return no_advisory()

    # Deliberately NOT staged — see the no-index-lock negative spec above.

    readme_modified = False
    if docs_readme.is_file():
        # Review: coordinatorcode-reviewer.a986dd968d6771f99, Finding 5 —
        # link target is relative to docs_readme's own directory (docs/), the
        # same shape plan_capture_persist.readme_row() uses for the routed
        # path; the prior "docs/plans/..." form double-prefixed docs/ for a
        # README that already lives in docs/.
        readme_line = f"- [`{target_name}`](plans/{target_name})"
        try:
            existing_readme = docs_readme.read_text(encoding="utf-8")
        except Exception:
            existing_readme = ""
        if target_name not in existing_readme:
            try:
                with docs_readme.open("a", encoding="utf-8", newline="") as fh:
                    fh.write(f"\n{readme_line}\n")
                readme_modified = True
            except Exception:
                pass

    rel_paths = [f"docs/plans/{target_name}"]
    if readme_modified:
        rel_paths.append("docs/README.md")
    cmd = _commit_cmd(plans_git_root, repo_root_path, rel_paths, slug)
    return post_advisory(_persisted_text(cmd))
