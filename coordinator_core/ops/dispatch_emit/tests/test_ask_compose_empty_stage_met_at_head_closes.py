"""A resumed halted run whose stage staged no row takes the verify-and-land tail instead of the
`stage staged no rows` refusal, and `land_run` closes it already_landed with no new commit when every ledger
row is met by paths HEAD already holds; a row met by nothing HEAD holds still refuses."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_contract import LEG_FOOTPRINT_FILE, RUN_DIR_ROOT, UNCHANGED_SINCE_HALT_REASON
from coordinator_core.ops.dispatch_emit.grind_commit_contract import ALREADY_LANDED_KEY
from coordinator_core.ops.dispatch_emit.pre_halt_ancestry import EMPTY_STAGE_RESUME_FIELD
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import halted  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_met_refire_lands_its_own_product_changes import (
    B,
    RUN2,
    _composed,
    _git,
    _refire,
    _write,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SID = "11111111-2222-3333-4444-555555555555"
_REVIEWED = reviewed()

_BRANCH_MARK = "{ _emptyStageResume = true; _allLanded = true; _xsTail = true; }"
_HALT_PREFIX = "  else if (!_parked && (_manifest.error || (!(_manifest.rows ?? []).length && !_allLanded && !_resumeLedger))) {"
_HARNESS = """
let _halted = null, _allLanded = false, _xsTail = false, _resumeLedger = false, _parked = null, _planRel = 'p.md', _sizingRel = 's.yaml', _runId = 'r';
let _emptyStageResume = false, _incompleteChunks = [];
let _gate = {arm: 's'}, _manifest = %s;
const _refireCmd = () => 'refire';
%s
console.log(JSON.stringify({ halted: _halted, allLanded: _allLanded, xsTail: _xsTail, empty: _emptyStageResume }));
"""


def _drive(manifest: dict, **compose_kw) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    script = _compose(**compose_kw).split("\n")
    # trap: the halt line calls hoisted `_shared_exprs` arrows (e.g. `_owedHandback`); carry their declarations.
    lines = [ln for ln in script if ln.startswith("const _owedHandback = ")]
    lines += [ln for ln in script if _BRANCH_MARK in ln or ln.startswith(_HALT_PREFIX)]
    body = "\n".join(ln.replace("  else if (", "  if (", 1) for ln in lines)
    proc = subprocess.run(
        [node, "-"], input=_HARNESS % (json.dumps(manifest), body), capture_output=True, text=True,
        encoding="utf-8", timeout=60, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


_EMPTY = {"rows": [], "owed": [], "gated": [], "rulable_ids": ["R1", "R2"], "ledger_item_ids": ["R1", "R2"]}


def test_a_resumed_halted_run_with_an_empty_stage_takes_the_land_tail_not_the_refusal():
    got = _drive(_EMPTY, prior_run_ids=("run-0",))
    assert got == {"halted": None, "allLanded": True, "xsTail": True, "empty": True}


@pytest.mark.parametrize(
    "manifest, kw",
    [
        (_EMPTY, {}),
        ({**_EMPTY, "rulable_ids": []}, {"prior_run_ids": ("run-0",)}),
        ({**_EMPTY, "error": "stage failed"}, {"prior_run_ids": ("run-0",)}),
        ({**_EMPTY, "owed": [{"id": "C1"}]}, {"prior_run_ids": ("run-0",)}),
    ],
    ids=["fresh-run", "nothing-rulable", "stage-error", "rows-owed"],
)
def test_every_other_empty_stage_keeps_the_refusal(manifest, kw):
    got = _drive(manifest, **kw)
    assert got["halted"]["halted"] == "refusal" and got["allLanded"] is False and got["empty"] is False


def test_the_empty_stage_tail_hands_the_gate_a_resume_ledger_over_the_rulable_rows():
    script = _compose(prior_run_ids=("run-0",))
    assert f"{EMPTY_STAGE_RESUME_FIELD}: true" in script
    assert "blocking: _manifest.rulable_ids" in script


# ---- land_run: the credit the tail reaches ------------------------------------------------------


def _empty_stage_refire(root) -> tuple:
    """Hand-commit the declared paths after the halt, then stage the refire with no manifest row and a leg
    footprint naming those paths. Returns `(resume, token, hand_sha)`."""
    _write(root, "a.py", "a = 7\n")
    _write(root, B, "b = 7\n")
    _git(root, "add", "a.py", B)
    _git(root, "commit", "-q", "-m", "hand land the declared paths")
    hand = _git(root, "rev-parse", "HEAD").strip()
    refire = _refire(root, edit=False)
    path = root / RUN_DIR_ROOT / RUN2 / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**manifest, "rows": []}), encoding="utf-8")
    _write(root, f"{RUN_DIR_ROOT}/{RUN2}/{LEG_FOOTPRINT_FILE}", json.dumps({"writes": ["a.py", B]}))
    resume = {**_composed(root), "since_sha": hand, EMPTY_STAGE_RESUME_FIELD: True}
    return resume, refire["token"], hand


def _terminal(root, token, resume, *met_by) -> dict:
    verdict = {"items": [{"id": "R1", "status": "met", "wired": True, "met_by": list(met_by)}]}
    return land_run._handler(
        {
            "script_path": "run2.mjs", "run_id": RUN2, "run_token": token, "incomplete_chunks": [],
            "inline_review": _REVIEWED, "session_id": _SID, "ledger_verdict": verdict, "resume_ledger": resume,
        },
        repo_root=root / ".git",
    )


def test_an_empty_stage_whose_rows_are_met_by_paths_at_head_closes_already_landed_with_no_new_commit(halted):  # noqa: F811
    resume, token, hand = _empty_stage_refire(halted)

    out = _terminal(halted, token, resume, "a.py:1", f"{B}@abc1234")

    assert out.get("refused") is None, out
    assert out["committed"] is True and out[ALREADY_LANDED_KEY] is True and out["sha"] == hand, out
    assert _git(halted, "rev-parse", "HEAD").strip() == hand


def test_the_js_built_resume_ledger_carries_no_base_yet_the_chain_first_halt_resolves_it(halted):  # noqa: F811
    """The exact `{run_id, blocking, notes, remaining, empty_stage}` shape `ask_compose` emits: no
    `since_sha`, `halt_sha` or `plan`, so the base comes from the chain's first halt."""
    composed, token, hand = _empty_stage_refire(halted)
    resume = {
        "run_id": RUN2, "blocking": composed["blocking"], "notes": [], "remaining": {}, EMPTY_STAGE_RESUME_FIELD: True,
    }

    out = _terminal(halted, token, resume, "a.py:1", f"{B}@abc1234")

    assert out.get("refused") is None, out
    assert out["committed"] is True and out[ALREADY_LANDED_KEY] is True and out["sha"] == hand, out
    assert _git(halted, "rev-parse", "HEAD").strip() == hand


def test_an_empty_stage_with_a_row_met_by_no_path_at_head_still_refuses(halted):  # noqa: F811
    resume, token, hand = _empty_stage_refire(halted)

    out = _terminal(halted, token, resume, "docs/elsewhere.md", "the tests pass")

    assert out["committed"] is False and out["refused"] == "ledger-unmet", out
    assert out["ledger_failures"] == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]
    assert not out.get(ALREADY_LANDED_KEY)
    assert _git(halted, "rev-parse", "HEAD").strip() == hand


def test_without_the_empty_stage_mark_the_leg_footprint_declares_nothing(halted):  # noqa: F811
    resume, token, hand = _empty_stage_refire(halted)
    resume.pop(EMPTY_STAGE_RESUME_FIELD)

    out = _terminal(halted, token, resume, "a.py:1", f"{B}@abc1234")

    assert out["committed"] is False and out["refused"] == "ledger-unmet", out
    assert _git(halted, "rev-parse", "HEAD").strip() == hand
