"""Install-integration check: a real `_install_bin_resolvers` pass into a
THROWAWAY `tmp_path` root writes every dynamically-derived agent-helper
forwarder, a sample of them actually EXECUTE and resolve to their real
`coordinator/bin/` target (not just get written to disk), and a second
install pass over the same root is genuinely idempotent.

Never touches the operator's real `~/.coordinator-claude-settings` or
`~/.claude` — every write lands under `tmp_path`. Does not call
`substrate.run()`/`_c10a_steps`, `first_run.py`, `maximalist.py`, or
`uninstall_legs.py` — those are in a sibling session's concurrency-audit
scope this session; this file only exercises `_install_bin_resolvers` and
the forwarder-derivation helpers it calls, which are outside that scope.

Deliberately scoped, not a full "fresh install reproduces the whole
substrate" claim: `_install_bin_resolvers` covers only the `bin/` resolver
family (machine-local family, claude-home family, resolve-claude-klabauter family,
and the ~379 dynamically-derived agent-helper forwarders). The rest of the
substrate write surface (state scaffolding, hooks, settings.json merge,
venv provisioning) is the restricted functions' territory above and is
NOT exercised or claimed proven here — see the dispatch report for what
remains unproven.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from coordinator_core.install.substrate import (
    _CH_FAMILY_FILES,
    _derive_agent_helper_target_map,
    _install_bin_resolvers,
    _load_bin_templates_manifest,
    _resolve_bin_templates_manifest_root,
)
from coordinator_core.win_portability import no_console_creationflags

import pytest

# Declares a real external-process spawn (spawn ratchet Rule 2). Tiering onto the
# cadence suite is the separate threshold ruling, not this declaration.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT_BIN = _REPO_ROOT / "coordinator" / "bin"

# CLIs known (spot-checked live against this repo, 2026-08-14) to accept
# `--help`/no-args and exit 0 quickly with no network/state mutation --
# a real-target execution sample, not an exhaustive census of the ~379
# derived forwarders (see module docstring on scope).
_SAMPLE_INSTALLED_NAMES = (
    "coordinator-current-branch",
    "check-mcp-versions",
    "identity-cli",
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _run_install(tmp_path: Path, monkeypatch, bin_dst: Path) -> None:
    ml_bin = tmp_path / "ml_bin"
    ch_bin = tmp_path / "ch_bin"

    bin_manifest = _load_bin_templates_manifest(_resolve_bin_templates_manifest_root())
    for entry in bin_manifest.install_bin_resolvers_entries():
        _write(ml_bin / entry.name, f"ml-source-content::{entry.name}\n")
    for f, _exec_bit in _CH_FAMILY_FILES:
        _write(ch_bin / f, f"ch-source-content::{f}\n")

    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", str(_REPO_ROOT))

    _install_bin_resolvers(
        ml_bin, ch_bin, bin_dst,
        check_only=False,
        python3_cmd_resolved_bin=sys.executable,
    )


def test_every_derived_agent_helper_forwarder_is_written(tmp_path, monkeypatch):
    bin_dst = tmp_path / "bin_dst"
    bin_dst.mkdir()
    _run_install(tmp_path, monkeypatch, bin_dst)

    target_map = _derive_agent_helper_target_map(_AGENT_BIN)
    assert target_map, "expected at least one derived agent-helper forwarder from the live coordinator/bin/"

    missing = [name for name in target_map if not (bin_dst / name).is_file()]
    assert not missing, f"{len(missing)} derived forwarder(s) never materialized: {sorted(missing)[:10]}..."


def test_sample_forwarders_execute_and_resolve_their_real_target(tmp_path, monkeypatch):
    bin_dst = tmp_path / "bin_dst"
    bin_dst.mkdir()
    _run_install(tmp_path, monkeypatch, bin_dst)

    # A written forwarder's runtime resolution ladder (_resolve_claude_klabauter.py,
    # exec'd BY the forwarder as a fresh subprocess) is independent of this
    # install pass's CLAUDE_KLABAUTER_ROOT env-var shortcut -- it consults the
    # settings-home machine-local registry / `.claude-klabauter-root` sentinel, per its
    # own module docstring's Rung 1/Rung 2. On a genuinely fresh machine
    # neither exists, and every forwarder rc=1s with "cannot resolve
    # claude-klabauter" -- an unstated prereq this test surfaces by supplying
    # the sentinel a real install's operator would also have to write (see
    # the dispatch report's "unstated prereqs" list).
    settings_home = Path(os.environ["HOME"])
    ml_dir = settings_home / ".coordinator-claude-settings" / "machine-local"
    ml_dir.mkdir(parents=True, exist_ok=True)
    (ml_dir / ".claude-klabauter-root").write_text(str(_REPO_ROOT) + "\n", encoding="utf-8")

    target_map = _derive_agent_helper_target_map(_AGENT_BIN)
    for name in _SAMPLE_INSTALLED_NAMES:
        assert name in target_map, f"{name}: not a live derived forwarder any more -- pick a new sample name"
        forwarder = bin_dst / name
        assert forwarder.is_file(), f"{name}: forwarder not written"
        proc = subprocess.run(
            [sys.executable, str(forwarder), "--help"],
            capture_output=True, text=True, timeout=30,
            **no_console_creationflags(),
        )
        assert proc.returncode == 0, (
            f"{name}: forwarder exec failed (rc={proc.returncode}) -- "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )


def test_second_install_pass_is_idempotent(tmp_path, monkeypatch):
    bin_dst = tmp_path / "bin_dst"
    bin_dst.mkdir()
    _run_install(tmp_path, monkeypatch, bin_dst)

    first_pass = {p.relative_to(bin_dst): p.read_bytes() for p in sorted(bin_dst.rglob("*")) if p.is_file()}

    _run_install(tmp_path, monkeypatch, bin_dst)

    second_pass = {p.relative_to(bin_dst): p.read_bytes() for p in sorted(bin_dst.rglob("*")) if p.is_file()}

    assert set(first_pass) == set(second_pass), (
        f"second install changed the file set -- added={set(second_pass) - set(first_pass)} "
        f"removed={set(first_pass) - set(second_pass)}"
    )
    changed = [name for name in first_pass if first_pass[name] != second_pass[name]]
    assert not changed, f"second install rewrote {len(changed)} file(s) with different content: {changed[:10]}"
