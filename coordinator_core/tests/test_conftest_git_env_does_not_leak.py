"""The suite quarantine carries git settings in a gitconfig, never in GIT_CONFIG_* env."""

from __future__ import annotations

import os
from pathlib import Path


def test_environ_carries_no_injected_git_config():
    leaked = [
        k
        for k in os.environ
        if k == "GIT_CONFIG_COUNT" or k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    ]
    assert leaked == []


def test_quarantine_gitconfig_pins_signing_off_and_no_browser():
    text = (Path(os.environ["HOME"]) / ".gitconfig").read_text(encoding="utf-8")
    assert "[commit]\n\tgpgsign = false" in text
    assert "[tag]\n\tgpgsign = false" in text
    assert "browser = noop" in text
