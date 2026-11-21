"""publish._main runs from a commit clone, never refusing, when publisher code differs from the published sha."""
from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.complete

_PUBLISH = Path(__file__).resolve().parents[1] / "publish.py"
_FILES = (
    "coordinator/bin/publish.py",
    "coordinator/bin/lib/helper.py",
    "coordinator/lib/percolate/targets.py",
    "coordinator_core/percolate/engine.py",
)
_STUB = (
    "import sys\n"
    "print('ran-from=' + __file__, file=sys.stderr)\n"
    "print('argv=' + '|'.join(sys.argv[1:]), file=sys.stderr)\n"
    "sys.exit(7)\n"
)


def test_the_refusal_text_is_gone_from_the_driver():
    assert "publisher code differs" not in _PUBLISH.read_text(encoding="utf-8")
