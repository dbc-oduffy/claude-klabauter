"""Pins the EM engine-ask section of the rendered peer-doctrine block, and DoE's mirror of the template.

Claude-klabauter's template, beside unimatrix-install, is the only edit point. DoE keeps a byte-identical
mirror because its mirrored installer renders from the copy beside itself (G-EM ruling 2026-11-16).
"""
import importlib.util
import re
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("unimatrix_install_engine_ask", BIN / "unimatrix-install.py")
ui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ui)

HEADING = "## When the PM addresses you as EM"


@pytest.fixture
def block(tmp_path):
    return ui.render_peer_doctrine(tmp_path)


def _em_section(text):
    start = text.index(HEADING)
    nxt = re.search(r"^## ", text[start + len(HEADING):], re.M)
    end = start + len(HEADING) + nxt.start() if nxt else len(text)
    return text[start:end]


@pytest.mark.parametrize("token", ["engine_ask.submit", "ask_source: pm", "fire: false", "workflow.revise", "relayed", "non-claude-em-path.md"])
def test_block_carries_route_token(block, token):
    assert token in block


def test_send_placeholder_is_bound(block):
    assert "{SEND}" not in block


@pytest.mark.parametrize("family", ["codex", "agy", "grok"])
def test_em_section_names_no_family(block, family):
    assert family not in _em_section(block).lower()


def test_em_section_is_bounded_by_markers(block):
    section = _em_section(block)
    assert section.startswith(HEADING)
    assert ui.DOCTRINE_END not in section or section.rstrip().endswith(ui.DOCTRINE_END.rstrip())


def _doe_template():
    from coordinator_core.machine_resolver import registry_get

    root = registry_get("repos.doe_claude")
    path = Path(root) / "coordinator" / "templates" / "unimatrix" / "peer-doctrine.md" if root else None
    if path is None or not path.is_file():
        pytest.skip("no coordinator-content-repo checkout registered on this box")
    return path


def test_doe_mirror_of_the_template_is_byte_identical():
    doe = _doe_template()
    assert doe.read_bytes() == ui.PEER_DOCTRINE_TEMPLATE.read_bytes(), (
        f"{doe} differs from claude-klabauter's {ui.PEER_DOCTRINE_TEMPLATE}: port it, or memo claude-klabauter"
    )
