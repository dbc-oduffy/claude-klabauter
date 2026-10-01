"""Tests for docs/install/agent-install-manifest.json's console-entrypoint
registration -- AC5 of docs/plans/2026-08-06-claude-klabauter-ize-the-survey-census.md.

Covers manifest shape only (static JSON assertions); live fresh-install
evidence for AC5 is EM-run and cited in the AC table, not exercised here.
The registration assertions about claude-klabauter's own manifest live in
`test_console_entrypoint_manifest_claude_klabauter_corpus.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "install" / "agent-install-manifest.json"
)


def _load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_manifest_is_valid_json() -> None:
    _load_manifest()
