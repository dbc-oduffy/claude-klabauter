"""Every agent() call the ask composer owns ends its prompt with exactly one sidecar clause."""

from __future__ import annotations

import re

from coordinator_core.ops._workflow_contract import _AGENT_CALL_SITE, _find_matching_paren, scrub
from coordinator_core.ops.dispatch_emit import agent_sidecar as sc
from coordinator_core.ops.dispatch_emit.tests.test_leg_register_ratchet import _ask, _ask_scripts

#: Legs composed by emit.py or review_mint, not by ask_compose; their clause is C2's and C6's.
_FOREIGN_LABEL_PREFIXES = ("commit:wave-", "verify:", "review:", "seam:wave-", "continue:", "falsifier:")
_LABEL_RE = re.compile(r"label:\s*(['\"])(.*?)\1")
_RUN_ID = "run-1"


def _owned_calls(script: str) -> list[tuple[str, str]]:
    scrubbed = scrub(script)
    calls = []
    for m in _AGENT_CALL_SITE.finditer(scrubbed):
        open_paren = m.end() - 1
        raw = script[open_paren : _find_matching_paren(scrubbed, open_paren)]
        label = _LABEL_RE.search(raw)
        name = label.group(2) if label else raw.split("label:", 1)[-1][:40]
        if name.startswith(_FOREIGN_LABEL_PREFIXES):
            continue
        calls.append((name, raw))
    return calls


def test_every_ask_owned_call_carries_one_marker(tmp_path):
    seen = 0
    for script in _ask_scripts(tmp_path):
        for label, raw in _owned_calls(script):
            seen += 1
            n = raw.count(sc.SIDECAR_DIR_MARKER_PREFIX)
            assert n == 1, f"ask agent() {label!r}: {n} sidecar_dir markers"
            assert "/ask/unkeyed/" not in raw, f"ask agent() {label!r} lost its run_id"
    assert seen > 30


def test_static_call_names_its_run_and_label_dir():
    script = _ask()
    assert "/unkeyed/" not in script
    for label in ("size", "plan"):
        assert f"{sc.SIDECAR_DIR_MARKER_PREFIX}{sc.sidecar_dir('ask', _RUN_ID, label)}" in script, label


def test_work_leg_uses_fire_time_label():
    script = _ask()
    prefix = sc.sidecar_dir("ask", _RUN_ID, "x")[: -len("x/")]
    work = [raw for label, raw in _owned_calls(script) if label.startswith("work:")]
    assert work, "no work: leg composed"
    for raw in work:
        assert prefix in raw
        assert ".replace(/[^A-Za-z0-9._-]/g, '-')" in raw
