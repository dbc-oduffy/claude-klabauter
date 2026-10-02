"""planBlitz launch args resolved caller-side: sidecar, spine-check and arming-check CLIs, agent roster.

One resolver shared by every emitter that binds a `planBlitz({...})` call, so they cannot drift.
In-process only: file stats and `shutil.which`, never a subprocess.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path


def _settings_home_bin(name: str) -> str | None:
    """A settings-home launcher path, or None. Rung 2 of the resolution ladder."""
    settings_home = os.environ.get("COORDINATOR_SETTINGS_HOME") or str(
        Path(os.environ.get("CLAUDE_HOME") or Path.home()) / ".coordinator-claude-settings"
    )
    candidate = Path(settings_home) / "bin" / name
    if candidate.is_file():
        return str(candidate)
    return shutil.which(name)


def _engine_env_prefix(engine_root: Path) -> str:
    """`COORDINATOR_ENGINE_ROOT=<engine> `, or empty. Part of the injected literal.

    `review-findings-ledger` resolves CLAUDE_KLABAUTER_ROOT before it does anything, and on a
    box with no machine-local registry that resolution fails outright. The integrator
    then reports the op refused and declines to hand-author around it, so the wave runs
    every review and records no disposition. Silent by construction: the refusal loses
    only the RECORD. The dispatching side already resolved an engine root; the agent
    running the CLI cannot.
    """
    if os.environ.get("COORDINATOR_ENGINE_ROOT"):
        return ""
    if os.name != "posix":
        return ""
    return f"COORDINATOR_ENGINE_ROOT={shlex.quote(str(engine_root))} "


#: The two layouts a published/mirrored plugin ships the registry manifest in. The private
#: DoE tree keeps it under `coordinator/`; the OSS publish row ships it flat at plugin root.
_MANIFEST_RELPATHS = (
    Path("schemas") / "coordinator-registry.manifest.json",
    Path("coordinator") / "schemas" / "coordinator-registry.manifest.json",
)


def _registry_manifest_prefix(engine_root: Path, plugin_root: Path) -> str:
    """`CONTENT_ROOT=<a root the manifest actually resolves under> `, or empty.

    VALIDATE THE VALUE YOU EXPORT: `CONTENT_ROOT` is taken as-is ahead of every other rung and
    is the state-write root, so a root the consumer cannot resolve the manifest from is
    worse than none. Both the plugin root and its parent are probed against the layouts the
    consumer probes. POSIX-shaped, deliberately not emitted elsewhere: `VAR=x cmd` is not a
    command on a PowerShell host, where the CLI's own diagnostic names the variable.
    """
    if any((engine_root / rel).is_file() for rel in _MANIFEST_RELPATHS):
        return ""
    if os.name != "posix":
        return ""
    for candidate in (plugin_root.parent, plugin_root):
        if any((candidate / rel).is_file() for rel in _MANIFEST_RELPATHS):
            return f"CONTENT_ROOT={shlex.quote(str(candidate))} "
    return ""


def _engine_bin(
    engine_root: Path | None, name: str, plugin_root: Path | None = None
) -> str | None:
    """The engine checkout's own copy of a CLI, as a runnable invocation, or None.

    Those files carry no shebang and no executable bit, so the path alone is not an
    invocation: the interpreter is part of the literal. Building it here is what removes a
    hand-typed interpreter prefix per run.
    """
    if engine_root is None:
        return None
    candidate = engine_root / "coordinator" / "bin" / f"{name}.py"
    if not candidate.is_file():
        return None
    prefix = _engine_env_prefix(engine_root)
    if plugin_root is not None:
        prefix += _registry_manifest_prefix(engine_root, plugin_root)
    return f"{prefix}{shlex.quote(sys.executable)} {shlex.quote(str(candidate))}"


#: Agent definitions every wave dispatches under. The roster the write guards consult is walked
#: from the plugin's own `agents/*.md`, so their presence IS the question being answered.
_WAVE_AGENT_DEFINITIONS = ("plan-author.md", "blitz-em.md")


def _plugin_agents_available(plugin_root: Path | None, explicit: str) -> tuple[bool, str]:
    """Resolve whether `coordinator:*` agent types resolve here. Returns (value, why).

    `false` is not a safe default and must never be reached by omission. An `agent()` call
    carrying no `agentType` is stamped `workflow-subagent`, a non-empty type on no roster,
    and the write guards confine it on that roster absence: the planner is refused its own
    plan body and denied `plan-spine-check.py`, burns its token budget, and lands no plan.

    Negative-spec: this NEVER probes the harness for whether a type would resolve at dispatch
    time. It answers "does this plugin root define the agents the wave dispatches" and says
    which question it answered.
    """
    if explicit in ("true", "false"):
        return explicit == "true", f"passed --plugin-agents-available {explicit}"
    if plugin_root is None:
        return False, "no plugin root resolved, so no agents/ directory to read"
    agents_dir = plugin_root / "agents"
    missing = [n for n in _WAVE_AGENT_DEFINITIONS if not (agents_dir / n).is_file()]
    if missing:
        return False, f"{agents_dir} is missing {', '.join(missing)}"
    return True, f"{agents_dir} defines every agent this wave dispatches"


def _plugin_bin_cli(plugin_root: Path | None, script: str) -> str | None:
    if plugin_root is None:
        return None
    candidate = plugin_root / "bin" / script
    if not candidate.is_file():
        return None
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(candidate))}"


def _default_spine_check_cli(plugin_root: Path | None) -> str | None:
    """`plan-spine-check`, resolved off the PLUGIN root, never the repo being planned.

    It ships in the plugin's own `bin/`; a repo-relative citation resolves only inside the
    plugin's source tree, and the planner then returns a plan whose spine was never checked,
    silently, because a missing file reads as a tooling hiccup.
    """
    return _plugin_bin_cli(plugin_root, "plan-spine-check.py")


def _default_arming_check_cli(plugin_root: Path | None) -> str | None:
    """`instrument-can-report-red`, resolved off the PLUGIN root, never a bare relative path.

    A relative citation resolves against the repo being planned, and the arming line then
    reads `N/A`, indistinguishable from a plan that declared no falsifier.
    """
    return _plugin_bin_cli(plugin_root, "instrument-can-report-red.py")


def _default_sidecar_cli(
    engine_root: Path | None, plugin_root: Path | None = None
) -> str | None:
    """`provision-sidecar`, resolved caller-side.

    A reviewer cannot resolve `<machinery_root>` or `<your session id>` from inside its
    brief; an agent handed placeholders invents them, and the invention is silent: findings
    land somewhere the repo does not track.
    """
    return _settings_home_bin("provision-sidecar") or _engine_bin(
        engine_root, "provision-sidecar", plugin_root
    )


def resolve(
    *,
    plugin_root: Path | None,
    engine_root: Path | None,
    sizing_abs: str,
) -> dict:
    """planBlitz args for a single-mode ask. A key with no resolvable value is omitted,
    except `pluginAgentsAvailable`, which is always present."""
    available, _why = _plugin_agents_available(plugin_root, "")
    out: dict = {}
    sidecar = _default_sidecar_cli(engine_root, plugin_root)
    if sidecar:
        out["provisionSidecarCli"] = sidecar
    out["pluginAgentsAvailable"] = available
    out["gateReportPath"] = sizing_abs
    spine = _default_spine_check_cli(plugin_root)
    if spine:
        out["spineCheckCli"] = spine
    arming = _default_arming_check_cli(plugin_root)
    if arming:
        out["armingCheckCli"] = arming
    return out
