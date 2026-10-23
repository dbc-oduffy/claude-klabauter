"""Pins credit_gate on the d5ade3507c shape: C1 and C2 stranded, docs the only contributor."""

from __future__ import annotations

import ast
from pathlib import Path

from coordinator_core.ops.dispatch_emit import credit_gate as cg

D = "coordinator_core/ops/dispatch_emit/"
C1_TEST = D + "tests/test_checkpoint_lines_on_every_emitted_checkpoint.py"
C1 = [D + "emit.py", C1_TEST, D + "tests/test_other_c1.py"]
C2 = [
    D + "ask_stage.py",
    D + "spine_read.py",
    D + "tests/t1.py",
    D + "tests/t2.py",
    D + "tests/test_ask_stage_change_kind_enum.py",
]
STRANDED = {"C1": C1, "C2": C2}


def test_names_and_bookkeeping():
    assert cg.REFUSED_CREDITED_UNCOMMITTED == "credited-chunk-uncommitted"
    assert cg.REFUSED_NO_CREDITED_LANDING == "no-credited-chunk-landed"
    assert cg.BOOKKEEPING_CHUNK_IDS == frozenset({"docs", "sizing"})


def test_stranded_residue_names_review_path_of_c1():
    assert cg.stranded_residue(STRANDED, [C1_TEST, "docs/x.md"]) == {"C1": [C1_TEST]}


def test_stranded_residue_empty_when_no_overlap():
    assert cg.stranded_residue(STRANDED, ["docs/x.md"]) == {}
    assert cg.stranded_residue({}, [C1_TEST]) == {}


def test_uncommitted_credited_flags_changed_missing_path():
    out = cg.uncommitted_credited(
        {"C1": C1}, {C1[0]}, set(), set(), {C1[0], C1[1]}
    )
    assert out == {"C1": [C1[1]]}


def test_uncommitted_credited_deleted_and_sibling_count_as_landing():
    out = cg.uncommitted_credited(
        {"C1": C1}, set(), {C1[0]}, {C1[1], C1[2]}, set(C1)
    )
    assert out == {}


def test_uncommitted_credited_ignores_unchanged():
    assert cg.uncommitted_credited({"C1": C1}, set(), set(), set(), set()) == {}


def test_uncommitted_credited_structural_when_changed_unknown():
    out = cg.uncommitted_credited({"C1": C1}, {C1[0]}, set(), set(), None)
    assert out == {"C1": sorted(C1[1:])}


def test_no_credited_landing_incident_shape():
    assert cg.no_credited_landing({"C1", "C2"}, {"docs"}, set()) is True
    assert cg.no_credited_landing({"C1", "C2"}, {"docs", "sizing"}, set()) is True


def test_legal_partial_wave_does_not_refuse():
    assert cg.no_credited_landing({"C2"}, {"C1", "docs"}, set()) is False
    assert cg.no_credited_landing({"C1", "C2"}, {"docs"}, {"C0"}) is False


def test_nothing_stranded_never_refuses():
    assert cg.no_credited_landing(set(), {"docs"}, set()) is False


LEDGER_FILES = {"C1": {"a.py", "t.py"}, "C2": {"b.py"}, "C3": set()}


def test_ledger_regradable_covers_clean_candidates_in_order():
    got = cg.ledger_regradable(["C2", "C1"], set(), LEDGER_FILES, {"a.py", "t.py", "b.py"})
    assert got == ["C2", "C1"]


def test_ledger_regradable_empty_when_no_review():
    assert cg.ledger_regradable(["C1"], set(), LEDGER_FILES, None) == []


def test_ledger_regradable_excludes_blocked_empty_and_outside():
    reviewed = {"a.py", "t.py"}
    assert cg.ledger_regradable(["C1"], {"C1"}, LEDGER_FILES, reviewed) == []
    assert cg.ledger_regradable(["C3"], set(), LEDGER_FILES, reviewed) == []
    assert cg.ledger_regradable(["C2"], set(), LEDGER_FILES, reviewed) == []
    assert cg.ledger_regradable(["C9"], set(), LEDGER_FILES, reviewed) == []


def test_credit_regradable_returns_evidence_for_credited():
    assert cg.credit_regradable(["C1", "C2"], {"C1": "ok"}, set()) == {"C1": "ok"}


def test_credit_regradable_excludes_held_and_uncredited():
    assert cg.credit_regradable(["C1"], {"C1": "ok"}, {"C1"}) == {}
    assert cg.credit_regradable(["C2"], {"C1": "ok"}, set()) == {}


def test_without_withheld_drops_only_withheld_chunks():
    regraded = {"C1": "credited: ok", "C2": "credited: ok"}
    assert cg.without_withheld(regraded, ["C1"]) == {"C2": "credited: ok"}
    assert cg.without_withheld(regraded, []) == regraded
    assert regraded == {"C1": "credited: ok", "C2": "credited: ok"}


def test_withheld_path_still_excused_from_uncommitted_gate():
    assert cg.uncommitted_credited({"C1": []}, set(), set(), set(), {"a.py"}) == {}


def test_module_is_stdlib_pure():
    tree = ast.parse(Path(cg.__file__).read_text(encoding="utf-8"))
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            mods.add((n.module or "").split(".")[0])
    assert not mods & {"subprocess", "coordinator_core"}
