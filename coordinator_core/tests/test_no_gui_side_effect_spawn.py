"""
coordinator_core.tests.test_no_gui_side_effect_spawn -- a child launch that raises a
visible window, a focus-stealing window, or the AppStarting (arrow + hourglass) cursor is a
defect for background work, and this guard stops a new one from landing.

The defect classes (one tag each, all scanned in live Python CODE under
`coordinator_core/`, `coordinator/`, `scripts/`, `bin/`):

- `create_new_console`  the `CREATE_NEW_CONSOLE` name (always a visible console).
- `pythonw`             a non-docstring string literal naming `pythonw` (GUI-subsystem
                        interpreter: raises AppStarting until it pumps messages). Spawn
                        `win_portability.console_python()` under `no_console_creationflags()`.
- `startfile`           `os.startfile` (ShellExecute under the hood, opens a window).
- `shellexecute`        any `ShellExecute*` name or literal.
- `cmd_start`           `cmd /c start ...` as a string or as an argv list.
- `start_process`       a `Start-Process` literal without `-WindowStyle Hidden`.
- `explorer`            `explorer` / `explorer.exe` launched.
- `difftool`            `difftool` / `mergetool` (git launches a GUI tool).
- `editor`              the `EDITOR` / `VISUAL` env key (an editor window).
- `raw_spawn`           `subprocess.run/Popen/call/check_call/check_output` with neither
                        explicit `creationflags=` nor a `**` splat of
                        `no_console_creationflags()`, `no_console_passthrough_kwargs()` or
                        `leaf_spawn_creationflags()` (or a name bound to one of them).
- `dup_startupinfo`     a `startupinfo=` kwarg beside a `**no_console_*()` splat: the helper
                        already carries a `startupinfo`, so the call raises a duplicate keyword.

Pre-existing hits are a FROZEN per-file count baseline (`_BASELINE`). Ratchet: a count may
fall, never rise; no new file may appear; an entry whose file or hits are gone must be deleted.

negative-spec -- this guard scans CODE, never prose. Docstrings and comments naming `pythonw`
or `CREATE_NEW_CONSOLE` are how the surrounding negative-specs explain the rule, and flagging
them would force the explanation to be deleted in order to satisfy the guard. Widen this to
new spawn shapes; never narrow a category, and never add a baseline entry for a new file --
route the launch through `win_portability.no_console_creationflags()` instead.

Scope excludes test trees (a regression test's job is to name the defect) and
`spawn_policy/windowless.py`-style sites are baseline entries, not exemptions.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: negative-spec -- never narrow back to `coordinator_core/`; add trees, never remove one.
_SCANNED_TREES = ("coordinator_core", "coordinator", "scripts", "bin")
_EXCLUDED_DIR_NAMES = frozenset({"tests", "__pycache__", "_vendor", "node_modules"})

_SPAWN_FUNCS = frozenset({"run", "Popen", "call", "check_call", "check_output"})
_HELPERS = frozenset(
    {
        "no_console_creationflags",
        "no_console_passthrough_kwargs",
        "leaf_spawn_creationflags",
        "no_console_kwargs",
    }
)
#: Helpers whose mapping already carries `startupinfo`.
_STARTUPINFO_HELPERS = frozenset(
    {"no_console_creationflags", "no_console_passthrough_kwargs", "no_console_kwargs"}
)

_CATEGORIES = (
    "create_new_console",
    "pythonw",
    "startfile",
    "shellexecute",
    "cmd_start",
    "start_process",
    "explorer",
    "difftool",
    "editor",
    "raw_spawn",
    "dup_startupinfo",
)

_CMD_START_RE = re.compile(r"\bcmd(?:\.exe)?\"?\s+/c\s+(?:\"\"\s+)?start\b", re.IGNORECASE)
_EXPLORER_RE = re.compile(r"^\s*\"?explorer(?:\.exe)?\"?(?:\s|$)", re.IGNORECASE)


def _call_name(node: object) -> str:
    if not isinstance(node, ast.Call):
        return ""
    f = node.func
    return f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """ids of Constant nodes that are bare-expression prose (docstrings, string statements)."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            out.add(id(node.value))
    return out


def _helper_bound_names(tree: ast.AST) -> set[str]:
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and _call_name(node.value) in _HELPERS:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            bound |= {t.id for t in targets if isinstance(t, ast.Name)}
    return bound


def _subprocess_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """(module aliases for subprocess, bare names imported from subprocess that spawn)."""
    modules: set[str] = set()
    funcs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "subprocess":
                    modules.add(a.asname or "subprocess")
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for a in node.names:
                if a.name in _SPAWN_FUNCS:
                    funcs.add(a.asname or a.name)
    return modules, funcs


def _splat_helper(kw: ast.keyword, bound: set[str]) -> str:
    """The helper a `**value` keyword splats ('' when it is not one of the helpers)."""
    if kw.arg is not None:
        return ""
    value = kw.value
    name = _call_name(value)
    if name in _HELPERS:
        return name
    if isinstance(value, ast.Name) and value.id in bound:
        return value.id
    return ""


def _scan_tree(tree: ast.AST) -> list[tuple[str, int]]:
    prose = _docstring_nodes(tree)
    bound = _helper_bound_names(tree)
    mods, funcs = _subprocess_names(tree)
    hits: list[tuple[str, int]] = []

    def add(cat: str, node: ast.AST) -> None:
        hits.append((cat, node.lineno))

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr == "CREATE_NEW_CONSOLE":
                add("create_new_console", node)
            elif node.attr == "startfile":
                add("startfile", node)
            elif node.attr.startswith("ShellExecute"):
                add("shellexecute", node)
        elif isinstance(node, ast.Name):
            if node.id.lstrip("_") == "CREATE_NEW_CONSOLE":
                add("create_new_console", node)
            elif node.id.startswith("ShellExecute"):
                add("shellexecute", node)
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if node.module == "os" and a.name == "startfile":
                    add("startfile", node)
        elif isinstance(node, (ast.List, ast.Tuple)):
            lowered = [
                e.value.lower() if isinstance(e, ast.Constant) and isinstance(e.value, str) else None
                for e in node.elts
            ]
            for i in range(len(lowered) - 2):
                if (
                    lowered[i] in ("cmd", "cmd.exe")
                    and lowered[i + 1] == "/c"
                    and lowered[i + 2] is not None
                    and lowered[i + 2].startswith("start")
                ):
                    add("cmd_start", node)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in prose:
            text = node.value
            low = text.lower()
            if "pythonw" in low:
                add("pythonw", node)
            if "create_new_console" in low:
                add("create_new_console", node)
            if "shellexecute" in low:
                add("shellexecute", node)
            if _CMD_START_RE.search(text):
                add("cmd_start", node)
            if "start-process" in low and not re.search(r"-windowstyle\s+hidden", low):
                add("start_process", node)
            if _EXPLORER_RE.match(text):
                add("explorer", node)
            if low in ("difftool", "mergetool") or re.search(r"\bgit\s+(?:difftool|mergetool)\b", low):
                add("difftool", node)
            if text in ("EDITOR", "VISUAL"):
                add("editor", node)
        if isinstance(node, ast.Call):
            func = node.func
            is_spawn = (
                isinstance(func, ast.Attribute)
                and func.attr in _SPAWN_FUNCS
                and isinstance(func.value, ast.Name)
                and func.value.id in mods
            ) or (isinstance(func, ast.Name) and func.id in funcs)
            splats = [_splat_helper(k, bound) for k in node.keywords]
            if any(k.arg == "startupinfo" for k in node.keywords) and any(
                s in _STARTUPINFO_HELPERS or (s in bound) for s in splats if s
            ):
                add("dup_startupinfo", node)
            if is_spawn and not any(k.arg == "creationflags" for k in node.keywords) and not any(splats):
                add("raw_spawn", node)
    return sorted(set(hits))


def scan_source(text: str, filename: str = "<synthetic>") -> list[tuple[str, int]]:
    """(category, lineno) hits in one module's source; empty on a syntax error."""
    try:
        return _scan_tree(ast.parse(text, filename=filename))
    except (SyntaxError, ValueError):
        return []


def _scan_path(path: Path) -> list[tuple[str, int]]:
    try:
        return scan_source(path.read_text(encoding="utf-8"), str(path))
    except (OSError, UnicodeDecodeError):
        return []


def _live_modules() -> list[Path]:
    out: list[Path] = []
    for tree in _SCANNED_TREES:
        root = _REPO_ROOT / tree
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            parts = set(path.relative_to(root).parts)
            if parts & _EXCLUDED_DIR_NAMES or path.name.startswith("test_"):
                continue
            out.append(path)
    return sorted(out)


@lru_cache(maxsize=1)
def _census() -> dict[str, dict[str, int]]:
    """{relpath: {category: count}} for every live module with at least one hit."""
    census: dict[str, dict[str, int]] = {}
    for path in _live_modules():
        counts = Counter(cat for cat, _ in _scan_path(path))
        if counts:
            census[path.relative_to(_REPO_ROOT).as_posix()] = dict(counts)
    return census


# ---------------------------------------------------------------------------
# Frozen baseline: pre-existing hits at authoring. Ratchet -- shrink, never grow.
# ---------------------------------------------------------------------------
_BASELINE: dict[str, dict[str, int]] = {
    'coordinator/bin/_queue_append_locator.py': {'raw_spawn': 2},
    'coordinator/bin/age-sweep-lessons.py': {'raw_spawn': 2},
    'coordinator/bin/bug-sweep-probes.py': {'raw_spawn': 1},
    'coordinator/bin/check-anchor-freshness.py': {'raw_spawn': 1},
    'coordinator/bin/check-bin-sh-polyglot.py': {'raw_spawn': 3},
    'coordinator/bin/check-machine-path-leak.py': {'raw_spawn': 1},
    'coordinator/bin/check-mcp-versions.py': {'raw_spawn': 1},
    'coordinator/bin/check-no-illegal-paths.py': {'raw_spawn': 1},
    'coordinator/bin/check-schema-version-bump.py': {'raw_spawn': 1},
    'coordinator/bin/check-sh-suffix-polyglot.py': {'raw_spawn': 1},
    'coordinator/bin/classify-engine-root-residue.py': {'raw_spawn': 1},
    'coordinator/bin/claude-author.py': {'pythonw': 1, 'raw_spawn': 4},
    'coordinator/bin/coordinator-doc-new.py': {'raw_spawn': 6},
    'coordinator/bin/coordinator-harvest-deferrals.py': {'raw_spawn': 2},
    'coordinator/bin/coordinator-prepare-commit-msg.py': {'raw_spawn': 1},
    'coordinator/bin/coordinator-safe-commit.py': {'raw_spawn': 7},
    'coordinator/bin/coordinator-subsession.py': {'raw_spawn': 1},
    'coordinator/bin/cross-repo-memo.py': {'editor': 1, 'raw_spawn': 4},
    'coordinator/bin/emit-goal-from-artifact.py': {'raw_spawn': 2},
    'coordinator/bin/fan-out-dispatch.py': {'raw_spawn': 3},
    'coordinator/bin/frontmatter-parse-check.py': {'raw_spawn': 1},
    'coordinator/bin/generate-claudemeta-manifest.py': {'raw_spawn': 1},
    'coordinator/bin/group-em-memory-governor.py': {'pythonw': 2},
    'coordinator/bin/handoff-loe-summary.py': {'raw_spawn': 1},
    'coordinator/bin/headless-em-launch.py': {'raw_spawn': 1},
    'coordinator/bin/host-gpu-probe.py': {'raw_spawn': 2},
    'coordinator/bin/install-sentinel-write.py': {'raw_spawn': 1},
    'coordinator/bin/lib/cc_invoke.py': {'raw_spawn': 3},
    'coordinator/bin/lib/claude_author_control/relay_posix.py': {'raw_spawn': 1},
    'coordinator/bin/lib/emit-lesson-summaries.py': {'raw_spawn': 2},
    'coordinator/bin/lib/python_interp.py': {'pythonw': 1},
    'coordinator/bin/lib/session_detach.py': {'pythonw': 1},
    'coordinator/bin/merge-gate-and-pr.py': {'raw_spawn': 4},
    'coordinator/bin/merge-recovery-and-tag-cut.py': {'raw_spawn': 4},
    'coordinator/bin/percolate-round.py': {'raw_spawn': 1},
    'coordinator/bin/plan-reversibility-eligibility.py': {'raw_spawn': 1},
    'coordinator/bin/pre_commit_corpus_artifact_guard.py': {'raw_spawn': 1},
    'coordinator/bin/refresh-plugin-live-install.py': {'raw_spawn': 15},
    'coordinator/bin/repomap/generate-repomap.py': {'raw_spawn': 3},
    'coordinator/bin/scip-rebuild-at-ceremony.py': {'raw_spawn': 2},
    'coordinator/bin/spinoff-deliverable-and-commit.py': {'raw_spawn': 2},
    'coordinator/bin/standup.py': {'raw_spawn': 2},
    'coordinator/bin/structural-index-refresh.py': {'raw_spawn': 1},
    'coordinator/bin/survey-consume-gate.py': {'raw_spawn': 1},
    'coordinator/bin/unimatrix-agy-hook-launch.py': {'raw_spawn': 2},
    'coordinator/bin/unimatrix-agy-hook-pretool.py': {'raw_spawn': 2},
    'coordinator/bin/unimatrix-autolink.py': {'raw_spawn': 1},
    'coordinator/bin/unimatrix-codex-launch.py': {'raw_spawn': 1},
    'coordinator/bin/unimatrix-install.py': {'pythonw': 1, 'raw_spawn': 2},
    'coordinator/bin/unimatrix-peer-grok.py': {'raw_spawn': 1},
    'coordinator/bin/unimatrix-relay.py': {'raw_spawn': 4},
    'coordinator/bin/unimatrix-roster.py': {'raw_spawn': 1},
    'coordinator/bin/validate-fast-and-packageability.py': {'raw_spawn': 1},
    'coordinator/bin/workday-complete-reconcile.py': {'raw_spawn': 2},
    'coordinator/bin/workday-complete-step1-validate.py': {'raw_spawn': 1},
    'coordinator/bin/workday-complete-step9-append-changelog.py': {'raw_spawn': 3},
    'coordinator/bin/workday-start-day-branch-resolve.py': {'raw_spawn': 2},
    'coordinator/bin/workweek-complete-close.py': {'raw_spawn': 1},
    'coordinator/bin/workweek-start-goal-and-priorities.py': {'raw_spawn': 1},
    'coordinator/lib/coordinator_session.py': {'raw_spawn': 1},
    'coordinator/lib/disk_budget.py': {'raw_spawn': 1},
    'coordinator/lib/generate-doctrine-surface-split.py': {'raw_spawn': 1},
    'coordinator/lib/install/coordinator_install.py': {'explorer': 1},
    'coordinator/lib/percolate/doc_path_refs.py': {'raw_spawn': 1},
    'coordinator/lib/percolate/ensure_required_targets.py': {'raw_spawn': 2},
    'coordinator/lib/percolate/publish_sync.py': {'raw_spawn': 2},
    'coordinator/lib/percolate/resolve_target.py': {'raw_spawn': 1},
    'coordinator/lib/resolve-coordinator-clone.py': {'raw_spawn': 2},
    'coordinator/lib/settings_hook_identity.py': {'raw_spawn': 1},
    'coordinator/scripts/setup-github-auth-1password.py': {'raw_spawn': 1},
    'coordinator/scripts/setup.py': {'raw_spawn': 1},
    'coordinator/templates/bin/_machine_local.py': {'raw_spawn': 1},
    'coordinator/templates/bin/claude_machine_local.py': {'raw_spawn': 1},
    'coordinator/templates/bin/platform-localize.py': {'raw_spawn': 1},
    'coordinator_core/bash_guards/_alternative_liveness.py': {'pythonw': 1, 'raw_spawn': 6},
    'coordinator_core/bash_guards/_dialect.py': {'raw_spawn': 1, 'start_process': 1},
    'coordinator_core/bash_guards/block_drive_root_new_dir_bash_write.py': {'pythonw': 1},
    'coordinator_core/bash_guards/check_test_suite_invocation.py': {'pythonw': 1},
    'coordinator_core/bash_guards/guard_subagent_heavy_ue_launch.py': {'pythonw': 1, 'start_process': 3},
    'coordinator_core/benchmarks/hook_op_resolve_cost.py': {'raw_spawn': 1},
    'coordinator_core/benchmarks/leaf_spawn_migration_verify.py': {'raw_spawn': 1},
    'coordinator_core/benchmarks/process_time.py': {'raw_spawn': 1},
    'coordinator_core/benchmarks/publish_under_live_engine.py': {'raw_spawn': 6},
    'coordinator_core/benchmarks/swap_continuous_caller.py': {'raw_spawn': 1},
    'coordinator_core/git/run.py': {'raw_spawn': 2},
    'coordinator_core/hooks/project_orientation.py': {'raw_spawn': 1},
    'coordinator_core/hooks/runtime_tripwire_stop_watcher.py': {'raw_spawn': 1},
    'coordinator_core/hooks/sessionstart_ensure_http_forwarder.py': {'raw_spawn': 1},
    'coordinator_core/machine_local_spawn.py': {'raw_spawn': 1},
    'coordinator_core/ops/agent_pipe.py': {'raw_spawn': 1},
    'coordinator_core/ops/app_session.py': {'raw_spawn': 1},
    'coordinator_core/ops/ceremony/close_background.py': {'raw_spawn': 1},
    'coordinator_core/ops/ceremony/detached_spawn.py': {'raw_spawn': 1},
    'coordinator_core/ops/check_native_door_interpreter_handoff.py': {'pythonw': 1},
    'coordinator_core/ops/codex_runner.py': {'raw_spawn': 1},
    'coordinator_core/ops/detect_guardless_sessions.py': {'explorer': 1},
    'coordinator_core/ops/dispatch_emit/terminal_commit.py': {'raw_spawn': 1},
    'coordinator_core/ops/dispatch_emit/tranche_chain.py': {'raw_spawn': 1},
    'coordinator_core/ops/rag_freshness.py': {'raw_spawn': 1},
    'coordinator_core/ops/session_ceremonies.py': {'raw_spawn': 1},
    'coordinator_core/ops/verify_no_powershell_flash.py': {'raw_spawn': 1},
    'coordinator_core/ops/workflow_fire/fire.py': {'raw_spawn': 1},
    'coordinator_core/ops/workflow_fire/headless.py': {'raw_spawn': 2},
    'coordinator_core/percolate/publish_lease.py': {'raw_spawn': 1},
    'coordinator_core/prior_art_recall/benchmark.py': {'raw_spawn': 1},
    'coordinator_core/pyresolve.py': {'pythonw': 4},
    'coordinator_core/win_portability.py': {'create_new_console': 2, 'pythonw': 1, 'raw_spawn': 1},
    'coordinator_core/write_guards/nudge_windows_subprocess_popup.py': {'create_new_console': 2, 'pythonw': 1},
    'scripts/cloud_setup.py': {'raw_spawn': 5},
    'scripts/rag_fleet_register.py': {'raw_spawn': 2},
    'scripts/rag_work_target_index.py': {'raw_spawn': 3},
    'scripts/setup.py': {'raw_spawn': 18},
    'scripts/windows_install_acceptance.py': {'raw_spawn': 1},
    'scripts/windows_install_probe.py': {'raw_spawn': 1},
}


def test_scan_actually_covers_the_trees_it_claims() -> None:
    """The sweep must not pass by scanning nothing."""
    modules = _live_modules()
    assert len(modules) > 200, f"sweep collected only {len(modules)} modules -- scan is broken"
    for tree in _SCANNED_TREES:
        if (_REPO_ROOT / tree).is_dir():
            assert any(p.is_relative_to(_REPO_ROOT / tree) for p in modules), (
                f"declared tree {tree!r} contributed no modules to the sweep"
            )


def test_no_new_gui_side_effect_launch_beyond_the_frozen_baseline() -> None:
    census = _census()
    offenders: list[str] = []
    for rel, counts in sorted(census.items()):
        frozen = _BASELINE.get(rel)
        if frozen is None:
            offenders.append(f"{rel}: new file with {dict(sorted(counts.items()))}")
            continue
        for cat, n in sorted(counts.items()):
            if n > frozen.get(cat, 0):
                offenders.append(f"{rel}: {cat} {n} > baseline {frozen.get(cat, 0)}")
    assert not offenders, (
        "A launch that raises a visible window, focus steal or AppStarting cursor landed. Route the "
        "spawn through coordinator_core.win_portability.no_console_creationflags() (splat it; never "
        "pythonw -- use console_python(); never CREATE_NEW_CONSOLE; no startupinfo= beside the splat). "
        "Do not add a baseline entry.\n  " + "\n  ".join(offenders)
    )


def test_baseline_has_no_stale_entry() -> None:
    """Ratchets only shrink: an entry whose file or hits are all gone must be deleted."""
    census = _census()
    stale: list[str] = []
    for rel, counts in sorted(_BASELINE.items()):
        if not (_REPO_ROOT / rel).is_file():
            stale.append(f"{rel}: file gone")
            continue
        live = census.get(rel, {})
        for cat, n in sorted(counts.items()):
            if live.get(cat, 0) == 0:
                stale.append(f"{rel}: {cat} baseline {n}, live 0 -- delete it")
    assert not stale, "Baseline above the live count; shrink it:\n  " + "\n  ".join(stale)


def test_documented_inline_creationflags_carve_out_passes_the_guard() -> None:
    """`resolve_coordinator_clone.py`'s inline getattr(subprocess, "CREATE_NO_WINDOW", 0) is
    explicit creationflags, which satisfies the guard without a baseline entry."""
    live = [p for p in _live_modules() if p.name == "resolve_coordinator_clone.py"]
    assert live, "resolve_coordinator_clone.py moved; re-point this pin, do not delete it"
    for path in live:
        assert not [h for h in _scan_path(path) if h[0] == "raw_spawn"], f"{path} trips raw_spawn"


# ---------------------------------------------------------------------------
# The guard fails on each defect class (synthetic modules; both sides pinned).
# ---------------------------------------------------------------------------
_DEFECTS = {
    "pythonw": ("pythonw", "import subprocess\nsubprocess.run(['pythonw.exe', 'x.py'], creationflags=0)\n"),
    "create_new_console": (
        "create_new_console",
        "import subprocess\nsubprocess.run(['x'], creationflags=subprocess.CREATE_NEW_CONSOLE)\n",
    ),
    "startfile": ("startfile", "import os\nos.startfile('x.txt')\n"),
    "shellexecute": ("shellexecute", "import ctypes\nctypes.windll.shell32.ShellExecuteW(0, 'open', 'x', None, None, 1)\n"),
    "cmd_start_string": ("cmd_start", "import os\nos.system('cmd /c start notepad')\n"),
    "cmd_start_argv": (
        "cmd_start",
        "import subprocess\nsubprocess.run(['cmd', '/c', 'start', 'x'], creationflags=0)\n",
    ),
    "start_process": ("start_process", "CMD = 'powershell -Command Start-Process notepad'\n"),
    "explorer": ("explorer", "import subprocess\nsubprocess.run(['explorer.exe', 'C:/x'], creationflags=0)\n"),
    "difftool": ("difftool", "ARGV = ['git', 'difftool', 'HEAD']\n"),
    "mergetool": ("difftool", "ARGV = ['git', 'mergetool']\n"),
    "editor": ("editor", "import os\ne = os.environ.get('EDITOR')\n"),
    "raw_run": ("raw_spawn", "import subprocess\nsubprocess.run(['git', 'status'])\n"),
    "raw_popen": ("raw_spawn", "import subprocess as sp\nsp.Popen(['git', 'status'])\n"),
    "raw_from_import": ("raw_spawn", "from subprocess import check_output\ncheck_output(['git', 'status'])\n"),
    "unrelated_splat": ("raw_spawn", "import subprocess\nsubprocess.run(['x'], **extra)\n"),
    "dup_startupinfo": (
        "dup_startupinfo",
        "import subprocess\nfrom coordinator_core.win_portability import no_console_creationflags\n"
        "subprocess.run(['x'], startupinfo=si, **no_console_creationflags())\n",
    ),
}


@pytest.mark.parametrize("name", sorted(_DEFECTS))
def test_guard_flags_each_defect_class(name: str, tmp_path: Path) -> None:
    category, source = _DEFECTS[name]
    module = tmp_path / "synthetic.py"
    module.write_text(source, encoding="utf-8")
    assert category in {cat for cat, _ in _scan_path(module)}, f"{name} not flagged as {category}"


_CLEAN = {
    "helper_splat": (
        "import subprocess\nfrom coordinator_core.win_portability import no_console_creationflags\n"
        "subprocess.run(['x'], **no_console_creationflags())\n"
    ),
    "passthrough_splat": "import subprocess\nsubprocess.run(['x'], **no_console_passthrough_kwargs())\n",
    "leaf_splat": "import subprocess\nsubprocess.Popen(['x'], **leaf_spawn_creationflags())\n",
    "bound_splat": "import subprocess\n_NC = no_console_creationflags()\nsubprocess.run(['x'], **_NC)\n",
    "explicit_creationflags": "import subprocess\nsubprocess.run(['x'], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))\n",
    "hidden_start_process": "CMD = 'powershell Start-Process x -WindowStyle Hidden'\n",
    "prose_only": (
        '"""Never pythonw, never CREATE_NEW_CONSOLE, never os.startfile, never cmd /c start."""\n'
        "# pythonw CREATE_NEW_CONSOLE explorer difftool\n"
        "def f():\n    '''pythonw.exe is forbidden here.'''\n"
    ),
    "non_spawn_run": "import other\nother.run(['x'])\n",
}


@pytest.mark.parametrize("name", sorted(_CLEAN))
def test_guard_passes_the_sanctioned_shapes(name: str, tmp_path: Path) -> None:
    module = tmp_path / "synthetic.py"
    module.write_text(_CLEAN[name], encoding="utf-8")
    assert _scan_path(module) == []


def test_root_conftest_sets_the_prompt_env_and_installs_the_seam() -> None:
    """The root conftest sets both git-prompt vars at import and calls the install; the install
    is gated off an xdist controller (execnet launch stays as xdist issues it)."""
    src = (_REPO_ROOT / "conftest.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    defaults = {
        n.args[0].value
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "attr", "") == "setdefault"
        and n.args
        and isinstance(n.args[0], ast.Constant)
    }
    assert {"GIT_TERMINAL_PROMPT", "GCM_INTERACTIVE"} <= defaults
    configure = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "pytest_configure")
    installs = [n for n in ast.walk(configure) if _call_name(n) == "install_console_less_spawn_default"]
    assert installs, "pytest_configure no longer installs the console-less spawn default"
    assert "dist" in ast.unparse(configure), "install lost its xdist-controller gate"


def test_root_conftest_environment_is_live_when_the_root_conftest_loaded() -> None:
    import os
    import sys

    root_conftest = str(_REPO_ROOT / "conftest.py").lower()
    if not any(str(getattr(m, "__file__", "")).lower() == root_conftest for m in list(sys.modules.values())):
        pytest.skip("root conftest not loaded (coordinator_core rootdir regime)")
    assert "GIT_TERMINAL_PROMPT" in os.environ and "GCM_INTERACTIVE" in os.environ
    if sys.platform == "win32":
        import _winapi

        assert getattr(_winapi.CreateProcess, "_console_less_default", False)


def test_baseline_categories_are_known() -> None:
    for rel, counts in _BASELINE.items():
        assert set(counts) <= set(_CATEGORIES), f"{rel}: unknown category in {sorted(counts)}"
