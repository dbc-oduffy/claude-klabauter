"""In-process day envelope producer for the workday-start orient step.

`day_envelope(repo_root) -> (exit_code, text)`; `text` is one JSON object with `directives[]` and
`judgment_points[]` (a silent member is omitted, never empty), the shape `driver._envelope_outcome`
parses and `rules.RULES` matches. Read-only, no network, no spawn on the common path: nine readers
(addon health, RAG staleness, agent worktrees, EM effort and model, bin sentinel, working-repo
registration, hook currency, git-perf currency, stale origin stubs). Plan orphan tiers are not
produced here: `list_orphaned` measured 266-484ms alone, so `d-plan-orphan-tiers` stays absent until
a bounded-head rebuild lands.

A reader that raises contributes nothing and is named under `reader_errors`; one failing reader
never hides the others.
"""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from coordinator_core.contract.decision_object.judgment import build_disposition, build_judgment_point
from coordinator_core.contract.decision_object.reader_result import build_directive, truncate_external_text
from coordinator_core.workday_start.local_ahead import _current_branch

_ENGINE_ROOT = Path(__file__).resolve().parents[2]
_BIN_DIR = _ENGINE_ROOT / "coordinator" / "bin"
_PROBES_CLI = "workday-start-health-probes"
_PROBES_SCRIPT = _BIN_DIR / "workday-start-health-probes.py"

REPOMAP_RELPATH = Path(".claude") / "repomap.md"
REPOMAP_MAX_AGE_S = 24 * 3600
MAX_ADDON_LINES = 20
MAX_CLASSIFIED_WORKTREES = 6
MAX_STUB_DIRECTIVES = 20
EXPECTED_EFFORT = "medium"
TRANSCRIPT_TAIL_BYTES = 64 * 1024
HEAD_BYTES = 4096

ORIGIN_STUB_KINDS = frozenset({"spinoff", "spinoff-roadmap"})
LIVE_STUB_STATES = frozenset({"ready_to_fire", "in_flight"})
SHIPPED_PLAN_STATUSES = frozenset({"implemented", "shipped"})
CLEAN_WORKTREE_STATES = frozenset({"empty-clean", "dirty-benign", "commits-clean"})

_WORKING_REPO_KEY = "engine.working_repos.claude_klabauter"
_CLAUDE_KLABAUTER_REPO_ID = "claude-klabauter"
_KLABAUTER_MARKER = "# claude-klabauter"
_MODEL_RE = re.compile(r'"model":\s*"([^"]*)"')
_AGENT_MARKER = "/.claude/worktrees/agent-"

Result = tuple[list[dict[str, Any]], list[dict[str, Any]]]


def _health_probes() -> Any:
    """The `workday-start-health-probes` script as a module (its checks are the incumbent owners)."""
    mod = sys.modules.get("_workday_start_health_probes")
    if mod is None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("_workday_start_health_probes", _PROBES_SCRIPT)
        if spec is None or spec.loader is None:
            raise ImportError("workday-start-health-probes.py has no loader")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_workday_start_health_probes"] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop("_workday_start_health_probes", None)
            raise
    return mod


def _bin_on_path() -> None:
    """`coordinator/bin/lib` importable (appended, so it never shadows an installed module)."""
    for entry in (_BIN_DIR, _BIN_DIR / "lib"):
        if str(entry) not in sys.path:
            sys.path.append(str(entry))


def _author_profile() -> bool:
    from coordinator_core.machine_profile import machine_profile

    return machine_profile() == "author"


def _addon_health(repo_root: Path) -> Result:
    from coordinator_core.plugin_health import scan

    lines, _ = scan._run("--red-and-stale")
    directives = [
        build_directive(
            f"d-addon-health-{n}", "scan-addon-health", ["--red-and-stale"], truncate_external_text(line)
        )
        for n, line in enumerate(lines[:MAX_ADDON_LINES], 1)
    ]
    return directives, []


def _rag_staleness(repo_root: Path) -> Result:
    if not _author_profile():
        return [], []
    try:
        age_s = time.time() - (repo_root / REPOMAP_RELPATH).stat().st_mtime
    except OSError:
        detail = f"{REPOMAP_RELPATH.as_posix()} is missing; example-retrieval-repo state is unknown"
    else:
        if age_s <= REPOMAP_MAX_AGE_S:
            return [], []
        detail = f"{REPOMAP_RELPATH.as_posix()} is {int(age_s // 3600)}h old (limit 24h)"
    return [build_directive("d-rag-staleness-regen", "generate-repomap", [], detail)], []


def _agent_worktrees(repo_root: Path) -> list[str]:
    """Agent worktree roots recorded under the common dir's `worktrees/*/gitdir`, sorted."""
    from coordinator_core.git.git_dir import resolve_git_common_dir

    base = resolve_git_common_dir(repo_root) / "worktrees"
    found: list[str] = []
    try:
        entries = list(os.scandir(base))
    except OSError:
        return found
    for entry in entries:
        try:
            recorded = (Path(entry.path) / "gitdir").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        root = recorded.replace("\\", "/")
        root = root[: -len("/.git")] if root.endswith("/.git") else root
        if _AGENT_MARKER in root and os.path.isdir(root):
            found.append(root)
    return sorted(found)


def _agent_worktrees_reader(repo_root: Path) -> Result:
    branch = _current_branch(repo_root)
    if not branch:
        return [], []
    roots = _agent_worktrees(repo_root)
    if not roots:
        return [], []
    from coordinator_core.ops.agent_worktree_sweep import classify_worktree

    directives: list[dict[str, Any]] = []
    points: list[dict[str, Any]] = []
    for n, root in enumerate(roots[:MAX_CLASSIFIED_WORKTREES], 1):
        verdict = classify_worktree(root, branch)
        if verdict.state in CLEAN_WORKTREE_STATES:
            directives.append(
                build_directive(
                    f"d-worktree-reap-{n}", "agent-worktree-sweep", ["--reap"],
                    f"{root}: {verdict.state}, {verdict.commits_ahead} commit(s) ahead of {branch}",
                )
            )
            continue
        points.append(
            build_judgment_point(
                None,
                id=f"j-worktree-dirty-{n}",
                question=f"Agent worktree {root} holds uncommitted changes; what happens to it?",
                dispositions=[build_disposition("pm_reviews_manually"), build_disposition("leave_for_now")],
                evidence=f"{verdict.dirty_count} dirty path(s), {verdict.commits_ahead} commit(s) ahead of {branch}",
                reason="recommendation-forbidden",
                reportable=True,
            )
        )
    return directives, points


def _claude_home() -> Path:
    return Path(os.environ.get("HOME") or str(Path.home())) / ".claude"


def _model_in_tail(transcript: Path) -> str:
    try:
        with open(transcript, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - TRANSCRIPT_TAIL_BYTES))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    found = _MODEL_RE.findall(tail)
    return found[-1] if found else ""


def _em_environment(repo_root: Path) -> Result:
    from coordinator_core.ops.check_em_environment import _resolve_effort, _resolve_transcript

    user_claude = _claude_home()
    points: list[dict[str, Any]] = []
    effort, source = _resolve_effort(repo_root, user_claude)
    if effort != EXPECTED_EFFORT:
        shown = effort if effort else "unpinned"
        points.append(
            build_judgment_point(
                None,
                id="j-em-env-effort",
                question=f"EM effort is {shown}, not {EXPECTED_EFFORT}; pin it?",
                dispositions=[build_disposition("pin"), build_disposition("leave")],
                evidence=f"effortLevel={shown}" + (f" ({source})" if source else ""),
                reason="recommendation-forbidden",
                reportable=True,
            )
        )
    transcript = _resolve_transcript("", user_claude, os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    model = _model_in_tail(Path(transcript)) if transcript else ""
    if model and "opus" not in model:
        points.append(
            build_judgment_point(
                None,
                id="j-em-env-model",
                question=f"The session transcript shows model {model}, not Opus; switch?",
                dispositions=[build_disposition("switch"), build_disposition("leave")],
                evidence=f"model={model}",
                reason="recommendation-forbidden",
                reportable=True,
            )
        )
    return [], points


def _bin_sentinel(repo_root: Path) -> Result:
    probes = _health_probes()
    sentinel = _BIN_DIR / "archive-stamp-cli.py"
    if not _BIN_DIR.is_dir():
        detail = f"CLAUDE-KLABAUTER-BIN PROBE: '{_BIN_DIR}' missing; wrong checkout or stale clone"
    elif not probes._is_readable_script(str(sentinel)):
        detail = f"CLAUDE-KLABAUTER-BIN PROBE: sentinel '{sentinel}' missing or unreadable; restore it with `git checkout -- {sentinel}`"
    else:
        return [], []
    return [build_directive("d-claude-klabauter-bin-sentinel", _PROBES_CLI, ["claude-klabauter-bin-sentinel"], detail)], []


def _is_engine_authoring_tree(repo_root: Path) -> bool:
    agents = repo_root / "AGENTS.md"
    try:
        if agents.is_file() and agents.read_text(encoding="utf-8").splitlines()[0].strip().startswith(_KLABAUTER_MARKER):
            return False
    except (OSError, IndexError):
        pass
    try:
        manifest = json.loads((repo_root / "docs" / "install" / "agent-install-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(manifest, dict) and manifest.get("repo_id") == _CLAUDE_KLABAUTER_REPO_ID


def _working_repo_registration(repo_root: Path) -> Result:
    if not _is_engine_authoring_tree(repo_root):
        return [], []
    from coordinator_core.machine_resolver import registry_get

    registered = registry_get(_WORKING_REPO_KEY)
    if registered and _health_probes()._paths_match(registered, str(repo_root)):
        return [], []
    state = f"registered as {registered!r}" if registered else "not registered"
    return [
        build_directive(
            "d-working-repo-registration", _PROBES_CLI, ["working-repo-registration", "--fix"],
            f"{_WORKING_REPO_KEY} is {state}; this checkout is {repo_root}",
        )
    ], []


def _hook_walk_report() -> str:
    """Fleet hook walk, check only; empty text means current, anything else stale or unrunnable."""
    from coordinator_core.machine_resolver import merged_flat_registry

    _bin_on_path()
    git_hook_install = importlib.import_module("git_hook_install")
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            git_hook_install.ensure_hooks_fleet(str(_BIN_DIR), check_only=True, registry=merged_flat_registry())
    except Exception as exc:  # noqa: BLE001 - an unrunnable walk reads as stale, never as current
        return f"COULD NOT RUN the fleet hook walk: {exc}"
    return buf.getvalue().strip()


def _hook_currency(repo_root: Path) -> Result:
    report = _hook_walk_report()
    if not report:
        return [], []
    detail = truncate_external_text(report.replace("\n", " | "))
    directive = build_directive("d-hook-currency", _PROBES_CLI, ["hook-currency"], detail)
    if _author_profile():
        return [directive], []
    point_id = "j-hook-currency-repair"
    directive["depends_on"] = point_id
    point = build_judgment_point(
        None,
        id=point_id,
        question="Coordinator git hooks are stale in the fleet; repair them now?",
        dispositions=[build_disposition("repair_hooks_now", ["d-hook-currency"]), build_disposition("defer")],
        evidence=detail,
        reason="recommendation-forbidden",
    )
    return [directive], [point]


def _git_perf_currency(repo_root: Path) -> Result:
    probes = _health_probes()
    _bin_on_path()
    from coordinator_core.install import git_perf_config

    walk = git_perf_config.iter_fleet_worktrees(_BIN_DIR)
    if not walk.ok:
        detail = f"could not establish fleet currency ({walk.reason})"
    else:
        drift: list[str] = []
        for item in walk.items:
            kind, key, root = item[0], item[1], item[2]
            if kind == "missing":
                drift.append(f"missing {key}")
            elif kind == "error":
                drift.append(f"unclassifiable {key}")
            elif probes._git_perf_config_key_status(Path(root)) is not True:
                drift.append(f"drift {key}")
        if not drift:
            return [], []
        detail = truncate_external_text("core.untrackedCache not fleet-current: " + "; ".join(drift))
    return [build_directive("d-git-perf-currency", _PROBES_CLI, ["git-perf-currency", "--fix"], detail)], []


def _head_fields(path: Path, keys: Iterable[str]) -> Optional[dict[str, str]]:
    wanted = frozenset(keys)
    try:
        with open(path, "rb") as fh:
            head = fh.read(HEAD_BYTES).decode("utf-8", "replace")
    except OSError:
        return None
    lines = head.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    out: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        name, sep, value = line.partition(":")
        if sep and name in wanted:
            out[name] = value.split(" #")[0].strip().strip("\"'")
    return out


_STUB_KEYS = ("kind", "deployment_state", "roadmap_id", "stub_id", "status")


def _markdown_heads(directory: Path) -> Iterable[tuple[Path, dict[str, str]]]:
    try:
        entries = sorted(os.scandir(directory), key=lambda e: e.name)
    except OSError:
        return
    for entry in entries:
        if entry.name.endswith(".md") and entry.is_file():
            fields = _head_fields(Path(entry.path), _STUB_KEYS)
            if fields is not None:
                yield Path(entry.path), fields


def _stale_origin_stubs(repo_root: Path) -> Result:
    handoffs = list(_markdown_heads(repo_root / "state" / "handoffs"))
    stubs: dict[tuple[str, str], Path] = {}
    for path, f in handoffs:
        pair = (f.get("roadmap_id", ""), f.get("stub_id", ""))
        if f.get("kind") in ORIGIN_STUB_KINDS and f.get("deployment_state") in LIVE_STUB_STATES and all(pair):
            stubs[pair] = path
    if not stubs:
        return [], []
    evidence: dict[tuple[str, str], tuple[str, Path]] = {}
    for path, f in handoffs:
        pair = (f.get("roadmap_id", ""), f.get("stub_id", ""))
        if pair in stubs and path != stubs[pair] and f.get("deployment_state") == "shipped":
            evidence.setdefault(pair, ("handoff", path))
    for path, f in _markdown_heads(repo_root / "docs" / "plans"):
        pair = (f.get("roadmap_id", ""), f.get("stub_id", ""))
        if pair in stubs and f.get("status") in SHIPPED_PLAN_STATUSES:
            evidence.setdefault(pair, ("plan", path))
    closable = sorted(evidence, key=lambda p: str(stubs[p]))
    if not closable:
        return [], []
    point_id = "j-stale-origin-stubs"
    shown = closable[:MAX_STUB_DIRECTIVES]
    directives: list[dict[str, Any]] = []
    lines: list[str] = []
    for n, pair in enumerate(shown, 1):
        kind, record = evidence[pair]
        rel = record.relative_to(repo_root).as_posix()
        directive = build_directive(
            f"d-close-origin-stub-{n}", "close-origin-stub-on-ship",
            ["--plan" if kind == "plan" else "--handoff", rel],
            f"close {stubs[pair].name}: shipped per {kind} {record.name}",
        )
        directive["depends_on"] = point_id
        directives.append(directive)
        lines.append(f"{stubs[pair].name} ({kind}: {rel})")
    more = len(closable) - len(shown)
    point = build_judgment_point(
        {"disposition": "close_stubs", "rationale": "A shipping record carries each stub's (roadmap_id, stub_id) pair."},
        id=point_id,
        question=f"{len(closable)} origin stub(s) already have a shipping record; close them?",
        dispositions=[
            build_disposition("close_stubs", [d["id"] for d in directives]),
            build_disposition("leave_as_is"),
        ],
        evidence=truncate_external_text("; ".join(lines) + (f"; +{more} more" if more else "")),
        reason="shipping-record-carries-pair",
        reportable=True,
    )
    return directives, [point]


READERS: tuple[tuple[str, Callable[[Path], Result]], ...] = (
    ("addon-health", _addon_health),
    ("rag-staleness", _rag_staleness),
    ("agent-worktrees", _agent_worktrees_reader),
    ("em-environment", _em_environment),
    ("bin-sentinel", _bin_sentinel),
    ("working-repo-registration", _working_repo_registration),
    ("hook-currency", _hook_currency),
    ("git-perf-currency", _git_perf_currency),
    ("stale-origin-stubs", _stale_origin_stubs),
)


def build_envelope(repo_root: Path) -> dict[str, Any]:
    directives: list[dict[str, Any]] = []
    points: list[dict[str, Any]] = []
    errors: list[str] = []
    for name, reader in READERS:
        try:
            d, p = reader(repo_root)
        except Exception as exc:  # noqa: BLE001 - one reader must not hide the rest
            errors.append(f"{name}: {type(exc).__name__}: {truncate_external_text(str(exc))}")
            continue
        directives.extend(d)
        points.extend(p)
    envelope: dict[str, Any] = {}
    if directives:
        envelope["directives"] = directives
    if points:
        envelope["judgment_points"] = points
    if errors:
        envelope["reader_errors"] = errors
    return envelope


def day_envelope(repo_root: Path) -> tuple[int, str]:
    """The day envelope as JSON text; exit 0 always, reader failures ride in `reader_errors`."""
    return 0, json.dumps(build_envelope(Path(repo_root)), sort_keys=True)
