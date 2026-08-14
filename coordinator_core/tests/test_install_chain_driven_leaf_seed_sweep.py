"""
The driven leaf proof — AC10(c) of docs/plans/2026-08-03-install-chain-install-md-
and-chain-walk-contract.md, chunk C11b.

Purpose: prove seed -> sweep -> pickup END TO END, not just the sweep half. A
hand-placed baton in ``$(coordinator-settings-home)/state/handoffs/`` (as
``coordinator/lib/tests/test-install-rendezvous-sweep.sh`` T1-T5 already do)
only proves the SWEEP finds an artifact that already exists. It does not prove
the SEEDING half — a downstream repo's own installer dropping its baton as
part of its own install, which is the PM's actual scenario (review note from
The Director of Engineering on C11). This test constructs a throwaway repo in a tempdir with a
~20-line ``standalone_setup_script`` that seeds its OWN ``kind: spinoff``
baton carrying ``install_chain_order:``, INVOKES that script as an installer
(a real subprocess, not a hand-written file), and only then runs the real
Step-0 sweep mechanism against the result.

The sweep half is not reimplemented: it is extracted VERBATIM (same
marker-based technique ``coordinator/lib/tests/test-install-rendezvous-sweep.sh``
uses) out of DoE-claude's own
``coordinator/templates/handoffs/continue-onboarding-and-installation.md`` —
the actual Step 0 spine-builder every coordinator-claude install session runs.
Claude-klabauter does not vendor that template (it is coordinator-claude/DoE
content, not claude-klabauter's — see claude-klabauter's own CLAUDE.md § What this repo
is), so this test resolves the DoE-claude repo root the same registry-first,
machine-portable way ``coordinator_core.doe_root_pointer`` is designed for and
SKIPS (never fails) when that root, or the template inside it, is not
resolvable on the current machine — a missing sibling checkout is an
environment fact, not a defect in the mechanism this test is proving.

Hermetic: every seed/sweep subprocess runs under an explicit, minimal ``env``
pointing ``coordinator-settings-home`` (faked on PATH) and
``${CLAUDE_HOME:-$HOME}`` at tempdir sandboxes — this test NEVER reads or
writes the real ``$(coordinator-settings-home)/state/handoffs/`` on the host
running it. ``tmp_path`` provisions and tears down the sandbox automatically;
nothing is left behind.

Spec backlink: docs/plans/2026-08-03-install-chain-install-md-and-chain-walk-
contract.md § C11 (AC10c) / chunk C11b.

Negative-spec:
    - Does NOT reimplement the Step 0 sweep grammar — extracts and executes
      the template's own ``bash`` fence verbatim, mirroring
      ``coordinator/lib/tests/test-install-rendezvous-sweep.sh``'s
      ``extract_block`` technique (ported to Python here since this test lives
      under ``coordinator_core/tests/``, a pytest-collected surface).
    - Does NOT assert anything about a REAL downstream repo (e.g. Example-game-repo) —
      that is a separate leg of C11's AC10(c) instruction ("use example-game-repo's
      leg... if conformant; otherwise construct a MINIMAL SYNTHETIC leaf").
      This test is the synthetic-leaf half; it proves the MECHANISM, not that
      any particular real repo conforms to it.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core._settings_home import settings_home as _real_settings_home
from coordinator_core.doe_root_pointer import read_doe_root_pointer

_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_TEMPLATE_REL = "coordinator/templates/handoffs/continue-onboarding-and-installation.md"
_INSTALL_LEG_MARKER = (
    "# Resolve the rendezvous through the seam, with an inline fallback for a cold tool shell"
)

_STANDALONE_SETUP_SCRIPT = """#!/usr/bin/env bash
# ~20-line synthetic downstream-repo installer — the SEED half of the proof.
# Mirrors agent-install-contract.md's real seeding guidance: resolve the
# rendezvous through the coordinator-settings-home seam, then cp/cat a
# kind:spinoff baton into it. Not the Write tool, not hand-placement.
set -euo pipefail
if command -v coordinator-settings-home >/dev/null 2>&1; then
  RENDEZVOUS="$(coordinator-settings-home)/state/handoffs"
else
  RENDEZVOUS="${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings/state/handoffs"
fi
mkdir -p "$RENDEZVOUS"
cat > "$RENDEZVOUS/install-synthleaf.md" <<'BATON'
---
title: "Install synthleaf"
created: 2026-08-03
kind: spinoff
status: active
predecessor: none
authoring_session: synthleaf-standalone-setup-script
workstream: install-chain-driven-leaf-proof
deployment_state: ready_to_fire
pickup_ready: true
scope: []
repo: synthleaf
install_chain_order: 7
---
BATON
echo "synthleaf: seeded install-synthleaf.md into $RENDEZVOUS"
"""


def _extract_bash_block(template_text: str, marker: str) -> str:
    """Python port of ``test-install-rendezvous-sweep.sh``'s ``extract_block``:
    find the fenced ```bash block containing ``marker``, return its body
    VERBATIM (dedenting the 3-space markdown list-item indent), starting at
    the marker line through (not including) the closing fence. Raises if the
    marker text has drifted out of sync with the template (same fail-loud
    contract the shell oracle uses)."""
    lines = template_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if marker in line:
            start = i
            break
    if start is None:
        raise AssertionError(f"marker not found in template: {marker!r}")
    body_lines = []
    for line in lines[start:]:
        if line.strip() == "```":
            break
        body_lines.append(line[3:] if line.startswith("   ") else line)
    body = "\n".join(body_lines).strip("\n")
    if not body:
        raise AssertionError(
            f"extraction for marker {marker!r} produced an empty block — "
            "marker text drifted out of sync with the template"
        )
    return body


def _resolve_template_path() -> Path | None:
    doe_root = read_doe_root_pointer()
    if not doe_root:
        return None
    candidate = Path(doe_root) / _TEMPLATE_REL
    return candidate if candidate.is_file() else None


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.mark.real_home
@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash on PATH — Windows without git-bash/WSL")
def test_driven_leaf_seed_sweep_pickup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Opts out of conftest.py's autouse home quarantine via ``real_home``:
    resolving the DoE-claude sibling root is a READ-ONLY oracle lookup (same
    class of use the marker exists for). That opt-out is held open ONLY long
    enough to perform the read-only DoE-root lookup and to resolve the real
    rendezvous path via the same seam (``settings_home()``) the production
    code honors — immediately after, this test re-quarantines HOME/CLAUDE_HOME/
    COORDINATOR_SETTINGS_HOME to a throwaway sandbox before the seed/sweep
    phases run, so the opt-out is not held open across them. Every subsequent
    seed/sweep subprocess additionally runs under its OWN fully-hermetic
    sandbox env (fake settings-home, fake PATH) constructed below; see the
    module docstring's hermeticity paragraph."""
    template_path = _resolve_template_path()
    if template_path is None:
        pytest.skip(
            "DoE-claude root not resolvable via coordinator_core.doe_root_pointer "
            "on this machine (or the template is missing there) — the real Step 0 "
            "sweep mechanism this test proves against is unavailable; not a defect "
            "in claude-klabauter."
        )
    template_text = template_path.read_text(encoding="utf-8")
    sweep_block = _extract_bash_block(template_text, _INSTALL_LEG_MARKER)

    # Review: coordinator:code-reviewer — resolve the REAL rendezvous through
    # the same seam (`settings_home()`) the production seed/sweep code
    # honors, while HOME/CLAUDE_HOME/COORDINATOR_SETTINGS_HOME still carry the
    # developer's real values (the `real_home` opt-out). A hardcoded
    # `Path.home()/.coordinator-claude-settings` passes vacuously on any
    # machine with COORDINATOR_SETTINGS_HOME or CLAUDE_HOME set, proving
    # nothing about hermeticity there.
    real_rendezvous = _real_settings_home() / "state" / "handoffs"

    # Narrow the `real_home` opt-out to the read-only lookups above: re-quarantine
    # HOME/CLAUDE_HOME/COORDINATOR_SETTINGS_HOME to a throwaway sandbox before the
    # seed/sweep phases below, so the opt-out is not held open across them.
    _real_home_quarantine = tmp_path / "requarantined-real-home"
    _real_home_quarantine.mkdir()
    monkeypatch.setenv("HOME", str(_real_home_quarantine))
    monkeypatch.setenv("CLAUDE_HOME", str(_real_home_quarantine))
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)

    # --- hermetic sandbox: fake settings-home, fake legacy CLAUDE_HOME, fake
    # `coordinator-settings-home` resolver on PATH. Nothing here touches the
    # real host's rendezvous folder.
    fake_settings_home = tmp_path / "settings-home"
    fake_claude_home = tmp_path / "fakehome"
    (fake_settings_home / "state" / "handoffs").mkdir(parents=True)
    (fake_claude_home / ".claude" / "state" / "handoffs").mkdir(parents=True)
    fake_bin_dir = tmp_path / "bin"
    fake_bin_dir.mkdir()
    _write_executable(
        fake_bin_dir / "coordinator-settings-home",
        f"#!/usr/bin/env bash\necho '{fake_settings_home}'\n",
    )

    sandbox_env = {
        "PATH": f"{fake_bin_dir}:/usr/bin:/bin",
        "CLAUDE_HOME": str(fake_claude_home),
        "HOME": str(fake_claude_home),
    }

    # --- SEED: write and invoke the throwaway repo's OWN installer as a real
    # subprocess (not a hand-placed file).
    leaf_repo_dir = tmp_path / "synthleaf-repo"
    leaf_repo_dir.mkdir()
    setup_script = leaf_repo_dir / "standalone_setup_script"
    _write_executable(setup_script, _STANDALONE_SETUP_SCRIPT)

    seed_result = subprocess.run(
        ["bash", str(setup_script)],
        cwd=leaf_repo_dir,
        env=sandbox_env,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=_CREATIONFLAGS,
    )
    assert seed_result.returncode == 0, (
        f"synthetic installer failed: stdout={seed_result.stdout!r} "
        f"stderr={seed_result.stderr!r}"
    )

    seeded_baton = fake_settings_home / "state" / "handoffs" / "install-synthleaf.md"
    assert seeded_baton.is_file(), "installer did not seed its baton into the fake rendezvous"
    assert not (real_rendezvous / "install-synthleaf.md").exists(), (
        "seeding leaked into the real coordinator-settings-home rendezvous — hermeticity violated"
    )

    # --- SWEEP: run the template's real Step 0 install-leg sweep, verbatim,
    # against the same sandbox the installer just seeded.
    sweep_result = subprocess.run(
        ["bash", "-c", sweep_block],
        cwd=tmp_path,
        env=sandbox_env,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=_CREATIONFLAGS,
    )
    assert sweep_result.returncode == 0, (
        f"Step 0 sweep block failed: stdout={sweep_result.stdout!r} stderr={sweep_result.stderr!r}"
    )

    # --- PICKUP-EDGE: the sweep's own stdout only ever emits a discovered
    # file's path (the sweep block's `awk` prints `$2`, the path field, never
    # the order), so the first assertion below proves only that the real Step
    # 0 sweep discovered the driven leaf's baton by path — not that the sweep
    # read or propagated install_chain_order through its own logic. The
    # second assertion re-reads the file the seed script wrote, which proves
    # the seed script wrote what it was told to, not anything about the
    # sweep. Review: coordinator:code-reviewer — comment previously overstated
    # this as proving install_chain_order was "carried" through the sweep.
    discovered = sweep_result.stdout.strip().splitlines()
    assert any("install-synthleaf.md" in line for line in discovered), (
        f"driven leaf's baton was not discovered by the real Step 0 sweep: {discovered!r}"
    )
    assert seeded_baton.read_text(encoding="utf-8").count("install_chain_order: 7") == 1
