"""Pins DR-290 form 2 for ``OVERRIDE_KEYS_DOC_DISPLAY``
(``coordinator_core.bash_guards._override_doc``) and its install-time
resolution leg.

DR-290 form 2 is the literal, never-expanded settings-root pointer.
``OVERRIDE_KEYS_DOC_DISPLAY`` is now its canonical carrier (2026-09-03):
this docstring used to cite ``session/guard_unlock_sentinel.py ::
_SETTINGS_ROOT_WIKI_POINTER`` as the shape that "already ships", but that
constant went unread and was removed. This module never runs the DISPLAY
string through
``expanduser``/``Path.home()`` (that would reintroduce the machine-specific
leak DR-290 exists to avoid), and never lets it be reverted to DR-290 form 1
(the repo-qualified relative string) -- that form named this repo
unconditionally, which is exactly the foreign-repo-identity leak
``docs/plans/2026-08-30-the-engine-stops-naming-its-own-repo.md`` closes.

The destination-decision pin (the four criterion markers) mechanically
forbids landing the page under ``<settings-home>/claude-klabauter/`` -- that
destination would make the pointer read
``~/.coordinator-claude-settings/claude-klabauter/docs/reference/
guard-override-keys.md``, which names the repo and self-defeats this
chunk's whole point.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from coordinator_core.bash_guards._override_doc import (
    OVERRIDE_KEYS_DOC,
    OVERRIDE_KEYS_DOC_DISPLAY,
)
from coordinator_core.install.substrate import _install_claude_klabauter_seed_wiki_page

#: The four foreign-repo-identity markers this plan's exit criterion names.
#: Mirrors the disposition probe's own criterion set — any one of these
#: appearing in a guard MESSAGE a session working in an unrelated repo would
#: see is the exact defect this plan closes.
_CRITERION_MARKERS = ("claude-klabauter", "claude-klabauter", "DoE-claude", "doe-claude-klabauter")


def test_display_form_names_no_repo():
    for marker in _CRITERION_MARKERS:
        assert marker not in OVERRIDE_KEYS_DOC_DISPLAY, (
            f"OVERRIDE_KEYS_DOC_DISPLAY names {marker!r} — DR-290 form 2 must "
            "name no repo, mechanically forbidding a claude-klabauter/ destination"
        )


def test_display_form_is_settings_root_literal():
    assert OVERRIDE_KEYS_DOC_DISPLAY.startswith("~/.coordinator-claude-settings/")
    assert OVERRIDE_KEYS_DOC_DISPLAY.endswith("guard-override-keys.md")


def test_display_form_is_never_expanded():
    # A literal `~/...` string is inert until a reader expands it themselves.
    # Simply reading the already-imported constant must never trigger a
    # home-resolution call, on either resolver a wrongly "helpful" future
    # edit might reach for.
    with mock.patch("pathlib.Path.home", side_effect=AssertionError("must not call Path.home()")):
        value = OVERRIDE_KEYS_DOC_DISPLAY
    with mock.patch("os.path.expanduser", side_effect=AssertionError("must not call expanduser")):
        value = OVERRIDE_KEYS_DOC_DISPLAY
    assert value.startswith("~")


def test_resolution_form_stays_split_from_display_form():
    # NEGATIVE SPEC: the file-resolution form a caller joins to a repo root
    # must stay a bare repo-relative path, never collapsed into the
    # settings-root display string.
    assert OVERRIDE_KEYS_DOC == "docs/reference/guard-override-keys.md"
    assert OVERRIDE_KEYS_DOC != OVERRIDE_KEYS_DOC_DISPLAY


def test_install_manifest_declares_the_pointer_target():
    manifest_path = (
        Path(__file__).resolve().parents[3] / "docs" / "install" / "agent-install-manifest.json"
    )
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = data["installed_wiki_pages"]["guard_override_keys_display_pointer"]
    assert entry["source"] == OVERRIDE_KEYS_DOC
    assert entry["destination"] == (
        "<settings_home>/coordinator-claude/docs/wiki/guard-override-keys.md"
    )
    # The manifest's declared destination must resolve to the same tail
    # OVERRIDE_KEYS_DOC_DISPLAY names, modulo the settings-home literal.
    assert OVERRIDE_KEYS_DOC_DISPLAY.endswith(
        "coordinator-claude/docs/wiki/guard-override-keys.md"
    )
    for marker in _CRITERION_MARKERS:
        assert marker not in entry["destination"]


def test_install_leg_lands_at_the_declared_destination(tmp_path):
    claude_klabauter_root = tmp_path / "claude_klabauter_root"
    settings_home = tmp_path / "settings_home"
    src = claude_klabauter_root / "docs" / "reference" / "guard-override-keys.md"
    src.parent.mkdir(parents=True)
    src.write_text("override keys content\n", encoding="utf-8")

    _install_claude_klabauter_seed_wiki_page(claude_klabauter_root, settings_home, check_only=False)

    dst = settings_home / "coordinator-claude" / "docs" / "wiki" / "guard-override-keys.md"
    assert dst.read_text(encoding="utf-8") == "override keys content\n"
