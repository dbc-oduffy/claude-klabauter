
from __future__ import annotations

from coordinator_core.bash_guards.dispatch_checks import check_destructive_git_orphan
import pytest

# The spawn is statically reachable from the code under test; tiered so a future change cannot spawn on the fast tier.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_Q = chr(39)
_PLAIN = "git push --force origin main"
_SPLIT = _Q + "g" + _Q + _Q + "it" + _Q + " push --force origin main"


def test_plain_spelling_denies():
    assert check_destructive_git_orphan(_PLAIN) is not None


def test_quote_split_spelling_denies_too():
    assert check_destructive_git_orphan(_SPLIT) is not None, (
        "quote-splitting the verb walked past the orphan guard's per-segment "
        "gate -- the gate is scanning raw text again"
    )
