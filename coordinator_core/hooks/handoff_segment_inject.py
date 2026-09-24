"""coordinator_core.hooks.handoff_segment_inject — UserPromptExpansion
auto-fire hook that serves `/handoff`'s residue segments into the SAME turn
the EM's `/handoff` prompt is being expanded into.

Port of: DoE-claude `coordinator/hooks/scripts/handoff-segment-inject.py`
(docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C12). Shape per the
W4-C1 verdict: command/native-door — no coordinator/bin shim, no http
registration.

`coordinator/skills/handoff/SKILL.md` is a resident core plus a `residue/`
directory of case-gated segments (frozen contract, one file per segment,
YAML frontmatter with exactly four keys: `segment_id`, `case`
(`shared|predecessor|dirty-tree|carried-items`), `class`
(`protected|droppable`), `order`). This hook resolves which cases are
ACTIVE for the current session (`shared` always; `dirty-tree` from a
read-only `git status --porcelain`; `predecessor`/`carried-items` from the
read-only `baton-assemble brief handoff` CLI), selects every segment whose
`case` is active, sorts them ascending by `order`, and injects the
concatenated bodies as one `additionalContext` envelope.

Segment source: `baton-assemble brief handoff`'s decision object carries a
`segments[]` key -- already case-filtered and selected by that CLI's own
engine. `segments_from_engine_brief` prefers it when present and
well-formed; falls back to this hook's own local `load_all_segments`
derivation otherwise. Both paths converge on the SAME
`render_additional_context` budget ladder.

Shape changes, all forced by this row's own op contract, none a behaviour
change:
  (a) stdin/stdout JSON I/O becomes the `params`-dict-in / envelope-dict-out
      contract; `compute_context` takes the already-parsed payload dict
      directly rather than raw stdin text.
  (b) `REPO_ROOT`/`_RESIDUE_DIR`'s DoE-layout-specific `parents[3]` walk
      (`coordinator/hooks/scripts/handoff-segment-inject.py` -> content
      root) becomes `coordinator_core.subagent_sandbox.provision_report.
      resolve_plugin_root()` -- the claude-klabauter-resident, already-load-bearing
      resolver for "where does the coordinator-claude plugin's content
      live" (`cater_subagent_start.py`'s own sibling consumer), per this
      row's own instruction to resolve doctrine assets through the plugin
      root rather than a DoE path.
  (c) `_resolve_repo_root`'s local zero-spawn `.git` walk becomes
      `coordinator_core.git.repo_root.show_toplevel` — the session's repo
      is resolved through the payload `cwd` (per this row's own
      instruction), falling back to this process's own cwd only when the
      payload carries none, matching the DoE source's own fallback shape.
  (d) `_skill_invocation`/`_forwarder_resolve` sibling-script imports
      become package-relative imports of the already-ported
      `coordinator_core.hooks.support` equivalents.
  (e) `_context_envelope("UserPromptExpansion", ...)` becomes
      `coordinator_core.hooks._envelope.context_only`, returning the
      envelope dict directly instead of printing to stdout.

`baton-assemble brief handoff` and `git status --porcelain` remain real
subprocess calls (unchanged from the DoE source) -- neither has an
in-process op equivalent in this tree today (unlike `group-em-enter`,
folded into an in-process op ahead of this chunk); both are read-only, and
this row's measure-first rule targets `pickup_autofire.py`'s 1353 lines
specifically, not this narrower two-subprocess surface.

Safety envelope (unchanged from DoE source, each clause load-bearing):
  (a) READ-ONLY, end to end. `baton-assemble` is called with no
      artifact-path argument; `apply` is NEVER invoked. `git status
      --porcelain` is the only other subprocess, inherently read-only.
  (b) EVERY FAILURE MODE DEGRADES TO SILENT PASS.
  (c) A malformed segment is silently DROPPED from the render, never
      raised -- this is a prompt-surface hook, not an EM-invoked assembler.
  (d) The injected context must land in the SAME turn -- no async fire.
  (e) BUDGET DEGRADE IS VISIBLE, never silent or mid-body. See
      `render_additional_context`.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md, chunk W4-C12.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import List, Optional, Set

try:
    import yaml
except Exception:  # pragma: no cover -- defensive: an isolated test harness
    # or partial deploy must still fail open rather than crash on import.
    yaml = None

from coordinator_core.hooks._envelope import context_only, no_advisory, payload_of
from coordinator_core.hooks.support.forwarder_resolve import forwarder_argv, resolve_forwarder
from coordinator_core.hooks.support.skill_invocation import read_invocation
from coordinator_core.ipc import register_op
from coordinator_core.subagent_sandbox.provision_report import resolve_plugin_root

# --- Constants ---------------------------------------------------------------

_HANDOFF_COMMAND_NAMES = frozenset({"handoff"})

# Deliberately NOT the 10_000 chars `mise_autofire.py` carries -- this hook
# renders peer-authored doctrine lifted verbatim out of the skill body,
# where truncation costs a RULE. See DoE source's own derivation (measured
# 19,451 chars for the all-cases-active segment set).
_CONTEXT_BUDGET_CHARS = 24_000

_OMISSION_MARKER = (
    "_(omitted for context budget: {segment_id} — "
    "coordinator/skills/handoff/residue/{source})_"
)

_VALID_CASES = frozenset({"shared", "predecessor", "dirty-tree", "carried-items"})
_VALID_CLASSES = frozenset({"protected", "droppable"})
_REQUIRED_SEGMENT_KEYS = frozenset({"segment_id", "case", "class", "order"})

_GIT_STATUS_TIMEOUT_SECONDS = 5
_BRIEF_TIMEOUT_SECONDS = 12

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def resolve_settings_home() -> Path:
    """Resolve the coordinator-claude settings-home root — bit-for-bit the
    same precedence as `mise_autofire.py`'s own copy."""
    override = os.environ.get("COORDINATOR_SETTINGS_HOME")
    if override:
        return Path(override)
    base = os.environ.get("CLAUDE_HOME") or str(Path.home())
    return Path(base) / ".coordinator-claude-settings"


def _residue_dir() -> Optional[Path]:
    """`<plugin content root>/skills/handoff/residue`, or None when the
    plugin root cannot be resolved — the caller degrades to an empty
    segment set, never raises."""
    plugin_root = resolve_plugin_root()
    if not plugin_root:
        return None
    return Path(plugin_root) / "skills" / "handoff" / "residue"


def resolve_baton_assemble_bin(settings_home: Path) -> Optional[Path]:
    """Resolve the installed `baton-assemble` forwarder under
    `settings_home`. Returns None -- the caller treats that as a transport
    failure and fails open."""
    return resolve_forwarder(settings_home / "bin", "baton-assemble")


# --- Repo-root resolution (spawn-free) ---------------------------------------


def _resolve_repo_root(payload_cwd: Optional[str]) -> Optional[Path]:
    """Zero-spawn repo-root resolution via `coordinator_core.git.repo_root.
    show_toplevel`, resolved through the payload's own `cwd` first (per
    this row's own instruction), falling back to this process's own cwd --
    matching the DoE source's own fallback shape (dir-of-file, then PWD),
    adapted since this hook has no dir-of-file to prefer. Returns None when
    undeterminable."""
    from coordinator_core.git.repo_root import show_toplevel

    start = payload_cwd if isinstance(payload_cwd, str) and payload_cwd else None
    toplevel = show_toplevel(start) if start else None
    if not toplevel:
        try:
            toplevel = show_toplevel(os.getcwd())
        except OSError:
            toplevel = None
    return Path(toplevel) if toplevel else None


# --- git status (read-only) -------------------------------------------------


def _git_status_porcelain(repo_root: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_STATUS_TIMEOUT_SECONDS,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _is_dirty_tree(repo_root: Optional[Path]) -> bool:
    if repo_root is None:
        return False
    out = _git_status_porcelain(repo_root)
    return bool(out and out.strip())


# --- baton-assemble brief (read-only) ---------------------------------------


def _run_baton_assemble_brief(script_path: Path) -> Optional[subprocess.CompletedProcess]:
    try:
        argv = forwarder_argv(script_path, ["brief", "handoff"])
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_BRIEF_TIMEOUT_SECONDS,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _decode_brief_payload(stdout: str) -> Optional[dict]:
    try:
        obj = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def predecessor_path_from_brief(brief: dict) -> Optional[str]:
    artifact = brief.get("artifact")
    if not isinstance(artifact, dict):
        return None
    lineage = artifact.get("lineage")
    if not isinstance(lineage, dict):
        return None
    predecessor = lineage.get("predecessor")
    if isinstance(predecessor, str) and predecessor:
        return predecessor
    if isinstance(predecessor, dict):
        path = predecessor.get("path")
        if isinstance(path, str) and path:
            return path
    return None


# --- Frontmatter parsing ------------------------------------------------------


def _split_frontmatter(text: str) -> tuple:
    if not text.startswith("---"):
        return None, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, text
    fm_text, body = parts[1], parts[2]
    if body.startswith("\n"):
        body = body[1:]
    if yaml is None:
        return None, text
    try:
        fm = yaml.safe_load(fm_text)
    except Exception:
        return None, text
    if not isinstance(fm, dict):
        return None, text
    return fm, body


def _carried_items_active(repo_root: Optional[Path], predecessor_rel_path: Optional[str]) -> bool:
    if not predecessor_rel_path:
        return False
    candidate = (repo_root / predecessor_rel_path) if repo_root is not None else Path(predecessor_rel_path)
    try:
        text = candidate.read_text(encoding="utf-8")
    except OSError:
        return False
    fm, _body = _split_frontmatter(text)
    if not isinstance(fm, dict):
        return False
    items = fm.get("carried_items")
    return isinstance(items, list) and len(items) > 0


# --- Active-case computation --------------------------------------------------


def _compute_active_cases_and_brief(repo_root: Optional[Path], settings_home: Path) -> tuple:
    active: Set[str] = {"shared"}

    if _is_dirty_tree(repo_root):
        active.add("dirty-tree")

    script_path = resolve_baton_assemble_bin(settings_home)
    if script_path is None:
        return active, None

    result = _run_baton_assemble_brief(script_path)
    if result is None or result.returncode != 0:
        return active, None

    brief = _decode_brief_payload(result.stdout)
    if brief is None:
        return active, None

    predecessor = predecessor_path_from_brief(brief)
    if not predecessor:
        return active, brief
    active.add("predecessor")

    if _carried_items_active(repo_root, predecessor):
        active.add("carried-items")

    return active, brief


# --- Residue-segment loading --------------------------------------------------


def _normalize_segment_fields(
    segment_id: object, case: object, klass: object, order: object, body: object, source: str
) -> Optional[dict]:
    if not (isinstance(segment_id, str) and segment_id):
        return None
    if case not in _VALID_CASES:
        return None
    if klass not in _VALID_CLASSES:
        return None
    if not isinstance(order, int) or isinstance(order, bool):
        return None
    if not isinstance(body, str):
        return None
    body = body.strip("\n")
    if not body:
        return None
    return {
        "segment_id": segment_id,
        "case": case,
        "class": klass,
        "order": order,
        "body": body,
        "source": source,
    }


def _load_segment(path: Path) -> Optional[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm, body = _split_frontmatter(text)
    if not isinstance(fm, dict):
        return None
    if not _REQUIRED_SEGMENT_KEYS.issubset(fm.keys()):
        return None
    return _normalize_segment_fields(
        fm.get("segment_id"), fm.get("case"), fm.get("class"), fm.get("order"), body, path.name
    )


def _load_engine_segment(entry: object) -> Optional[dict]:
    if not isinstance(entry, dict):
        return None
    if not _REQUIRED_SEGMENT_KEYS.issubset(entry.keys()) or "content" not in entry:
        return None
    source_path = entry.get("source_path")
    source = Path(source_path).name if isinstance(source_path, str) and source_path else "?"
    return _normalize_segment_fields(
        entry.get("segment_id"),
        entry.get("case"),
        entry.get("class"),
        entry.get("order"),
        entry.get("content"),
        source,
    )


def segments_from_engine_brief(brief: object) -> Optional[List[dict]]:
    if not isinstance(brief, dict):
        return None
    raw = brief.get("segments")
    if not isinstance(raw, list):
        return None
    if not raw:
        return []
    validated = [s for s in (_load_engine_segment(entry) for entry in raw) if s is not None]
    if not validated:
        return None
    return validated


def load_all_segments(residue_dir: Optional[Path]) -> List[dict]:
    if residue_dir is None:
        return []
    try:
        candidates = sorted(residue_dir.glob("*.md"))
    except OSError:
        return []
    segments: List[dict] = []
    for path in candidates:
        segment = _load_segment(path)
        if segment is not None:
            segments.append(segment)
    return segments


# --- Selection + budget-degraded rendering ----------------------------------


def select_segments(segments: List[dict], active_cases: Set[str]) -> List[dict]:
    selected = [s for s in segments if s["case"] in active_cases]
    selected.sort(key=lambda s: s["order"])
    return selected


def render_additional_context(segments: List[dict], active_cases: Set[str]) -> str:
    selected = select_segments(segments, active_cases)
    if not selected:
        return ""

    def _render(kept: List[dict], dropped: List[dict]) -> str:
        parts = [s["body"] for s in kept]
        parts.extend(
            _OMISSION_MARKER.format(
                segment_id=s.get("segment_id", "?"),
                source=s.get("source", "?"),
            )
            for s in sorted(dropped, key=lambda s: s["order"])
        )
        return "\n\n".join(parts)

    kept = list(selected)
    dropped: List[dict] = []
    rendered = _render(kept, dropped)
    if len(rendered) <= _CONTEXT_BUDGET_CHARS:
        return rendered

    for klass in ("droppable", "protected"):
        while len(rendered) > _CONTEXT_BUDGET_CHARS:
            victim = next(
                (i for i in range(len(kept) - 1, -1, -1) if kept[i]["class"] == klass),
                None,
            )
            if victim is None or len(kept) == 1:
                break
            dropped.append(kept.pop(victim))
            rendered = _render(kept, dropped)
        if len(rendered) <= _CONTEXT_BUDGET_CHARS:
            break

    return rendered


# --- Entry point --------------------------------------------------------------


def compute_context(payload: dict) -> Optional[str]:
    """The full compute-and-render path for a `/handoff` invocation,
    returning the rendered `additionalContext` string, or `None` when there
    is nothing to inject. Never raises."""
    invocation = read_invocation(payload if isinstance(payload, dict) else {})
    if invocation is None or invocation.command_name not in _HANDOFF_COMMAND_NAMES:
        return None  # not a /handoff invocation -- silent pass

    try:
        payload_cwd = payload.get("cwd") if isinstance(payload, dict) else None
        repo_root = _resolve_repo_root(payload_cwd)
        settings_home = resolve_settings_home()
        active_cases, brief = _compute_active_cases_and_brief(repo_root, settings_home)
        engine_segments = segments_from_engine_brief(brief)
        segments = engine_segments if engine_segments is not None else load_all_segments(_residue_dir())
        additional_context = render_additional_context(segments, active_cases)
    except Exception:
        return None

    return additional_context or None


@register_op("hooks.handoff_segment_inject")
def _handler(params: dict, repo_root=None) -> dict:
    """UserPromptExpansion / PreToolUse(Skill) op: serve `/handoff`'s
    residue segments into the SAME turn's `additionalContext`."""
    params = payload_of(params)
    try:
        additional_context = compute_context(params)
    except Exception:
        additional_context = None

    if not additional_context:
        return no_advisory()
    return context_only("UserPromptExpansion", additional_context)
