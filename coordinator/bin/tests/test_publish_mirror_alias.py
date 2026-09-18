"""coordinator/bin/tests/test_publish_mirror_alias.py — pins
`publish.py::_mirror_sigil_for_alias`, which lets the word an operator types
for a mirror (`klabauter`) resolve to that mirror's `publish-mirror:` sigil.

`percolate-gate branch0-gate klabauter` routes to `coordinator-publish
klabauter`; before this helper `publish.py` refused that same word as an
unknown target, so the gate's own instruction failed when run verbatim.

Negative spec: an alias never chooses between destinations. A match spanning
two mirrors, reaching a non-mirror row, or matching nothing returns None and
falls through to the unknown-target FATAL.

Run: python -m pytest coordinator/bin/tests/test_publish_mirror_alias.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def publish():
    spec = importlib.util.spec_from_file_location("_publish_mirror_alias_under_test", _BIN_DIR / "publish.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_SIGILS = {
    "claude-klabauter": "publish-mirror:claude_klabauter",
    "claude-klabauter-bin": "publish-mirror:claude_klabauter",
    "claude-klabauter-scripts": "publish-mirror:claude_klabauter",
    "claude-other-bin": "publish-mirror:claude_other",
    "plain-row": "../plain-dest",
}


@pytest.mark.parametrize("alias", ["klabauter", "claude_klabauter", "klabauter-scripts"])
def test_alias_resolves_to_its_one_mirror(publish, alias):
    assert publish._mirror_sigil_for_alias(alias, _SIGILS) == "publish-mirror:claude_klabauter"


@pytest.mark.parametrize("alias", ["claude", "bin", "plain", "claude-klabautr", "", "-"])
def test_alias_spanning_destinations_or_matching_nothing_is_refused(publish, alias):
    assert publish._mirror_sigil_for_alias(alias, _SIGILS) is None
