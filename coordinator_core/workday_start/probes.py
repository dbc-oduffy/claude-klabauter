"""In-process read probes for the workday-start steps that have no CLI.

Each probe is `(repo_root[, ...]) -> (exit_code, text)`; the runner calls it as an `inproc`
step. Read only: no spawn, no write. Empty text means the probe is silent.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

import yaml

from coordinator_core._settings_home import home_dir
from coordinator_core.machine_resolver import compute_machine
from coordinator_core.ops.test_red_record import _test_red_path
from coordinator_core.plugin_health.sentinel import read_claude_json_mcp_servers

_TOOLS: tuple[str, ...] = ("scc", "shellcheck")
_FIXTURE_SYNC = "bin/check-fixture-sync.sh"


def _load_record(repo_root: Path, machine: Optional[str]) -> Optional[dict]:
    """The machine's test-red record, or None when absent, unreadable or not a mapping."""
    path = _test_red_path(repo_root, machine or compute_machine())
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _str_list(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def _tier_line(name: str, tier: dict) -> Optional[str]:
    outcome = tier.get("outcome")
    failing = tier.get("failing")
    if outcome == "green" and failing in ([], None):
        return None
    if outcome in (None, "not-run"):
        return None
    if failing is None:
        return f"- {name}: {outcome}, failing set undeterminable"
    current = _str_list(failing)
    if not current:
        return None
    previous = tier.get("previous")
    prior = _str_list(previous.get("failing")) if isinstance(previous, dict) else []
    baseline: list[str] = []
    ack = tier.get("acknowledged")
    if isinstance(ack, dict):
        baseline = _str_list(ack.get("baseline"))
    known = set(prior) | set(baseline)
    new = [n for n in current if n not in known]
    return f"- {name}: {len(current)} failing, {len(new)} new since the last authoritative run"


def test_red_delta(repo_root: Path, machine: Optional[str] = None) -> tuple[int, str]:
    """One line per red tier in `state/test-red/<machine>.yaml`; silent when absent, malformed or green.

    `failing: null` is red-but-undeterminable, never green.
    """
    record = _load_record(Path(repo_root), machine)
    tiers = record.get("tiers") if record else None
    if not isinstance(tiers, dict):
        return 0, ""
    lines = [
        line
        for name, tier in sorted(tiers.items())
        if isinstance(tier, dict) and (line := _tier_line(str(name), tier)) is not None
    ]
    return 0, "\n".join(lines)


def mcp_registration(repo_root: Path) -> tuple[int, str]:
    """One line counting the `mcpServers` entries in `~/.claude.json`; exit 0 on every branch."""
    cfg, servers, failed = read_claude_json_mcp_servers(home_dir() / ".claude")
    if not cfg.exists():
        return 0, f"- MCP registration: {cfg.name} not found"
    if failed:
        return 0, f"- MCP registration: {cfg.name} unreadable or not valid JSON"
    if not isinstance(servers, dict):
        return 0, f"- MCP registration: {cfg.name} mcpServers is not an object"
    return 0, f"- MCP servers registered: {len(servers)}"


def fixture_sync_presence(repo_root: Path) -> tuple[int, str]:
    """Names `bin/check-fixture-sync.sh` when present, as not run; silent when absent. Never spawns."""
    if not (Path(repo_root) / _FIXTURE_SYNC).is_file():
        return 0, ""
    return 0, (
        f"- {_FIXTURE_SYNC}: present, not run "
        "(a shell-out not on the closed list in docs/reference/shell-out-carve-outs.md)"
    )


def tool_availability(repo_root: Path) -> tuple[int, str]:
    """Names each of `scc` and `shellcheck` that does not resolve on PATH; silent when both do."""
    missing = [t for t in _TOOLS if shutil.which(t) is None]
    return 0, "\n".join(f"- {t}: not on PATH" for t in missing)
