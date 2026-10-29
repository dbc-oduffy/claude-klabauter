"""Contract pin: every params key the ask composer puts on a `workflow.revise` next_action is a
declared `workflow.revise` input field, so a halt's next_action can be sent back verbatim."""
from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.requirements_ledger import _PARAMS

_SOURCE = Path(ask_compose.__file__).read_text(encoding="utf-8")
_ACCEPTED = {f.name for f in _PARAMS}
# params keys the composer attaches to a `_nextAction` of any kind, including revise_and_refire.
_ASSIGNED = re.compile(r"_nextAction\.params\.([A-Za-z_]\w*)\s*=(?!=)")
_LITERAL = re.compile(r"op: 'workflow\.revise', params: \{(.*?)\}", re.S)
_LITERAL_KEY = re.compile(r"(?:^|,)\s*([A-Za-z_]\w*)\s*:")


def _emitted_keys() -> set[str]:
    keys = set(_ASSIGNED.findall(_SOURCE))
    for body in _LITERAL.findall(ask_compose.REVISE_NEXT_ACTION_JS + "\n" + _SOURCE):
        keys.update(_LITERAL_KEY.findall(body))
    return keys


def test_scan_finds_the_widened_assignment():
    assert "widened" in _emitted_keys()
    assert {"run_id", "note"} <= _emitted_keys()


def test_every_emitted_revise_params_key_is_an_accepted_field():
    stray = _emitted_keys() - _ACCEPTED
    assert not stray, (
        f"composer emits next_action params keys {sorted(stray)} that workflow.revise does not accept "
        f"(accepted: {sorted(_ACCEPTED)})"
    )


def test_an_unaccepted_key_would_be_caught():
    assert "not_a_revise_field" not in _ACCEPTED
