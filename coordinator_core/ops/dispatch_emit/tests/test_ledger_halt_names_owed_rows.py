"""A ledger-check halt whose every blocking item is owed by EM/operator rows names them and advises no refire."""

from __future__ import annotations

import json
import shutil
import subprocess

import jsonschema
import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import _LEDGER_CHECK_SCHEMA, compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_OWED = [
    {"id": "C9", "reason": "operator", "detail": "run the migration"},
    {"id": "C10", "reason": "operator", "detail": "rotate the key"},
    {"id": "C11", "reason": "em-performed", "detail": "send the memo"},
]
_NODE = shutil.which("node")


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        **REVIEW_KW,
    )


def _next_action(monkeypatch, items, *, blocked=(), incomplete=("C9", "C10", "C11")) -> str:
    script = _script(monkeypatch)
    start = script.index("const _owedById")
    end = script.index("}; }", script.index("halted: 'ledger-check'", start)) + len("}; }")
    program = (
        "const _manifest = {owed: %s}; const _ledger = {items: %s}; const _ledgerBlocking = (l) => l.items;\n"
        "const _emptyVerdict = false; const _blockedChunks = %s; const _incompleteChunks = %s;\n"
        "let _halted = null; const _runId = 'run-1'; const _planRel = 'p.md'; const _sizingRel = 's.yaml';\n"
        "const _blkIds = _ledger.items.map((i) => i.id); const _blkRulable = []; const _blkForeign = [];\n"
        "%s\nprocess.stdout.write(_halted.next_action);"
        % (json.dumps(_OWED), json.dumps(items), json.dumps(list(blocked)), json.dumps(list(incomplete)), script[start:end])
    )
    return subprocess.run(
        [_NODE, "-e", program], capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _item(id_, owed_by=None):
    item = {"id": id_, "status": "not_met", "remaining": ["x"]}
    if owed_by is not None:
        item["owed_by"] = owed_by
    return item


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_every_blocking_item_owed_names_the_rows_and_advises_no_refire(monkeypatch):
    text = _next_action(monkeypatch, [_item("R1", ["C9", "C10", "C11"])])
    assert text.startswith("Everything left is EM or operator work:")
    for fragment in ("C9 (operator): run the migration", "C10 (operator): rotate the key", "C11 (em-performed): send the memo"):
        assert fragment in text
    assert "Re-fire the same sizing" not in text
    assert "The re-fire resumes at the ledger check" not in text
    assert "then refire to re-judge R1." in text


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
@pytest.mark.parametrize(
    "items, kwargs",
    [
        ([_item("R1", ["C9"]), _item("R2")], {}),
        ([_item("R1", ["C99"])], {}),
        ([_item("R1", ["C9"])], {"incomplete": ("C9", "C4")}),
        ([_item("R1", ["C9"])], {"blocked": ("C4",)}),
    ],
)
def test_any_other_shape_keeps_the_refire_advice(monkeypatch, items, kwargs):
    text = _next_action(monkeypatch, items, **kwargs)
    assert text.startswith("Ledger rows ")
    assert "Re-fire the same sizing: it executes the remaining work of the rows it keeps" in text
    assert "Everything left is EM or operator work" not in text


def test_the_schema_admits_owed_by_and_still_validates_an_item_without_it():
    jsonschema.validate({"items": [{"id": "R1", "status": "not_met", "owed_by": ["C9"]}]}, _LEDGER_CHECK_SCHEMA)
    jsonschema.validate({"items": [{"id": "R1", "status": "not_met"}]}, _LEDGER_CHECK_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"items": [{"id": "R1", "status": "not_met", "owed_by": "C9"}]}, _LEDGER_CHECK_SCHEMA)
