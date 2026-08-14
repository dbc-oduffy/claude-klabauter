"""
Fixture-drift guard: every ``ml_bin / "<name>"`` source literal in
``coordinator_core.install.substrate`` names a file the DoE-claude repo
actually ships under ``coordinator/templates/bin/``. The 2026-07-22
platform-localize.sh -> {.py,.cmd} rename regression shipped BECAUSE the
``test_install_substrate_uninstall_legs.py`` fixture stubbed a filename
substrate.py no longer installs, and nothing cross-checked the fixture
against DoE's real listing — this test is that cross-check, so a future
rename trips a red test here instead of a silent green-tests-dead-path.

Resolves the real DoE-claude checkout the same way the install hubs do
(``coordinator_core.install._shared.resolve_coordinator_root``); skips
cleanly when unresolvable on this machine (e.g. a CI box without a sibling
DoE-claude clone).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from coordinator_core.install import _shared
from coordinator_core.install.substrate import (
    _load_bin_templates_manifest,
    _resolve_bin_templates_manifest_root,
)

_SUBSTRATE_SRC = Path(__file__).with_name("substrate.py")
_ML_BIN_LITERAL_RE = re.compile(r'ml_bin\s*/\s*"([^"]+)"')


def _ml_bin_names_from_source() -> list:
    """Names substrate.py sources from ``ml_bin`` (DoE-claude's
    ``templates/bin/``) — the union of any remaining bare ``ml_bin /
    "<name>"`` source literals (regex scan, e.g. ``_render_python3_cmd``'s
    template read) and `coordinator/lib/bin-templates-manifest.py`'s
    ``ML_FAMILY_FILES``/``ML_EXPLICIT_FILES``/``PLATFORM_LOCALIZE_FILES``
    groups (C12 — the single source of truth
    ``_install_bin_resolvers``'s write loops read instead of spelling each
    name out as a literal; ``_static_bin_family_names()`` reuses the same
    groups for ``_sweep_orphaned_agent_helpers``'s completeness check — see
    that function's docstring). ``_CH_FAMILY_FILES`` (sourced from
    ``ch_bin``, not ``ml_bin``), ``_RM_FAMILY_FILES`` (sourced from
    ``resolve_claude_klabauter_lib``), and the manifest's own ``LAUNCHER_TEMPLATE_
    FILES`` group (rendered by ``gen_claude_doe_launcher.py``, never copied
    via ``_install_one`` / never referenced as an ``ml_bin / "<name>"``
    literal in substrate.py) are deliberately excluded — this guard is
    scoped to names substrate.py itself sources from ``ml_bin``, matching
    its DoE ``templates/bin/`` cross-check below."""
    text = _SUBSTRATE_SRC.read_text(encoding="utf-8")
    seen: list = []
    for name in _ML_BIN_LITERAL_RE.findall(text):
        if name not in seen:
            seen.append(name)
    bin_manifest = _load_bin_templates_manifest(_resolve_bin_templates_manifest_root())
    for entry in bin_manifest.install_bin_resolvers_entries():
        if entry.name not in seen:
            seen.append(entry.name)
    return seen


def test_ml_bin_literals_present_in_source():
    # Sanity: this test is only meaningful if the extraction actually finds
    # the sites substrate.py is known to carry.
    names = _ml_bin_names_from_source()
    assert "platform-localize.py" in names
    assert "platform-localize.cmd" in names


@pytest.mark.real_home  # live-tree oracle: resolves the real DoE-claude coordinator root via
# `_shared.resolve_coordinator_root` (registry_get / .doe-root pointer), which the suite-root
# `_quarantine_real_home` autouse fixture would otherwise hide, turning this into an
# unconditional skip. Read-only (Path.is_file/is_dir checks only, no writes).
def test_ml_bin_literals_exist_in_real_doe_templates_bin():
    try:
        coordinator_root = _shared.resolve_coordinator_root()
    except RuntimeError as exc:
        pytest.skip(f"DoE-claude coordinator root unresolvable on this machine: {exc}")

    templates_bin = Path(coordinator_root) / "templates" / "bin"
    if not templates_bin.is_dir():
        pytest.skip(f"no templates/bin under resolved coordinator root: {templates_bin}")

    names = _ml_bin_names_from_source()
    missing = [name for name in names if not (templates_bin / name).is_file()]
    assert missing == [], (
        f"substrate.py names {missing} under ml_bin, but they do not exist "
        f"at {templates_bin} — source-of-truth drift between substrate.py "
        "and DoE-claude's real templates/bin/ listing"
    )
