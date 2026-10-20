"""The peer-claim contract section in scoped-commit-guarantees names the module's tokens."""
from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.ops.dispatch_emit.peer_claim_contract import CONTRACT, ROUTES

_ROOT = Path(__file__).resolve().parents[4]
_HEADING = "## Peer-held path at terminal commit: path-level, no hunk carry"
_FIELDS = (
    "peer_claim_contract",
    "peer_claims_by_session",
    "peer_claim_routes",
    "peer_held_left",
    "peer_held_by",
)


def _section() -> str:
    text = (_ROOT / "docs/reference/scoped-commit-guarantees.md").read_text(encoding="utf-8")
    assert _HEADING in text
    body = text.split(_HEADING, 1)[1]
    return re.split(r"^## ", body, maxsplit=1, flags=re.M)[0]


def test_section_names_contract_and_every_route():
    section = _section()
    assert CONTRACT in section
    for route in ROUTES:
        assert f"`{route}`" in section


def test_section_names_every_reply_field():
    section = _section()
    for field in _FIELDS:
        assert field in section


def test_pointers_from_ask_route_and_dispatch_emit():
    for rel in ("docs/wiki/ask-route.md", "docs/wiki/dispatch-emit.md"):
        text = (_ROOT / rel).read_text(encoding="utf-8")
        assert "Peer-held path at terminal commit" in text
