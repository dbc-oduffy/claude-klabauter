"""stale-registered-paths (P-24): one stale entry per source, a clean HOME, the
relocation hint, and the report-only contract."""

from __future__ import annotations

import json
import os
import plistlib
from pathlib import Path

from coordinator_core.plugin_health import sentinel as S
from coordinator_core.plugin_health.stale_registered_paths import find_stale


def _snapshot(root: Path) -> dict:
    return {
        str(p): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _build(tmp_path: Path, *, stale: bool) -> tuple[Path, Path]:
    home = tmp_path / "home"
    ml = home / ".coordinator-claude-settings" / "machine-local"
    ml.mkdir(parents=True)
    repo = home / "X" / "alive"
    repo.mkdir(parents=True)
    ghost = home / "Code_Projects"
    pick = (lambda name: str(ghost / name)) if stale else (lambda name: str(repo))

    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "a.plist").write_bytes(
        plistlib.dumps({"Label": "a", "ProgramArguments": ["/bin/sh", pick("plist-tool") if stale else str(repo)]})
    )
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "projects": {pick("pinned"): {}},
                "mcpServers": {
                    "rag": {"command": pick("rag/venv/python") if stale else str(repo), "args": ["serve"], "cwd": pick("rag-cwd")},
                    "npx-server": {"command": "npx", "args": ["-y", "pkg"]},
                },
            }
        )
    )
    (ml / "registry.local.toml").write_text(f'schema = 1\n"repos.thing" = \'{pick("reg-thing")}\'\n"coordinator.machine_slug" = \'box\'\n')
    if not stale:
        (home / ".claude.json").write_text(
            json.dumps({"projects": {str(repo): {}}, "mcpServers": {"rag": {"command": str(repo), "cwd": str(repo)}, "n": {"command": "npx"}}})
        )
        (ml / "registry.local.toml").write_text(f"schema = 1\n\"repos.thing\" = '{repo}'\n")
    return home, ml


def test_one_stale_entry_per_source_is_five_findings_and_nonzero(tmp_path):
    home, ml = _build(tmp_path, stale=True)
    # five sources: plist arg, project pin, MCP command, MCP cwd, registry key
    claude = home / ".claude.json"
    data = json.loads(claude.read_text())
    data["mcpServers"]["rag"]["command"] = str(home / "Code_Projects" / "cmd")
    claude.write_text(json.dumps(data))
    plist = home / "Library" / "LaunchAgents" / "a.plist"
    before = _snapshot(tmp_path)

    findings = find_stale(home, ml, platform="darwin")

    assert len(findings) == 5, [f.render() for f in findings]
    assert {Path(f.source).name for f in findings} == {"a.plist", ".claude.json", "registry.local.toml"}
    assert any(f.key == "ProgramArguments[1]" and f.source == str(plist) for f in findings)
    assert any(f.key.startswith("projects.") for f in findings)
    assert any(f.key == "mcpServers.rag.command" for f in findings)
    assert any(f.key == "mcpServers.rag.cwd" for f in findings)
    assert any(f.key == "repos.thing" for f in findings)
    assert _snapshot(tmp_path) == before


def test_clean_home_reports_nothing(tmp_path):
    home, ml = _build(tmp_path, stale=False)
    assert find_stale(home, ml, platform="darwin") == []


def test_launchd_skipped_off_macos(tmp_path):
    home, ml = _build(tmp_path, stale=True)
    sources = {Path(f.source).name for f in find_stale(home, ml, platform="win32")}
    assert "a.plist" not in sources


def test_relocation_hint_when_sibling_exists_under_x(tmp_path):
    home, ml = _build(tmp_path, stale=True)
    (home / "X" / "pinned").mkdir()
    findings = find_stale(home, ml, platform="darwin")
    pin = next(f for f in findings if f.key.startswith("projects."))
    assert pin.hint == str(home / "X" / "pinned")
    other = next(f for f in findings if f.key == "repos.thing")
    assert other.hint is None


def test_probe_p24_is_amber_and_names_source_key_path(tmp_path):
    home, ml = _build(tmp_path, stale=True)
    notes = S.probe_p24(home, ml)
    assert notes and all(n.id == "P-24" and n.severity == "amber" for n in notes)
    assert any(".claude.json" in n.message and "Code_Projects" in n.message for n in notes)
    assert os.path.exists(home / ".claude.json")


def test_registered_repo_mcp_json_and_shared_registry_are_attributed(tmp_path):
    home, ml = tmp_path / "home", tmp_path / "ml"
    repo = tmp_path / "repo"
    for d in (home, ml, repo):
        d.mkdir()
    (repo / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"srv": {"command": str(tmp_path / "gone" / "bin"), "args": ["-y"]}}})
    )
    (ml / "registry.toml").write_text(f"schema = 1\n\"repos.thing\" = '{repo}'\n\"x.path\" = '{tmp_path / 'nope'}'\n")
    findings = find_stale(home, ml, platform="linux")
    by_key = {f.key: Path(f.source).name for f in findings}
    assert by_key == {"mcpServers.srv.command": ".mcp.json", "x.path": "registry.toml"}
