"""land_run.foreign_halted_withheld: a hold that cannot be computed holds every wanted path.

Also the review_reverify peer-withheld keep, the halted-record review-edit hold, and the hunk differ's process-time bound on a land_run.py-sized file (`own_hunk_stage`, `run_hunk_claims`).
"""

import difflib
import time

from coordinator_core.ops.dispatch_emit import land_run, own_hunk_stage, run_hunk_claims
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo as product_repo  # noqa: F401  (fixture)
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import repo  # noqa: F401  (fixture)
from coordinator_core.ops.tests.test_review_reverify import BASE, PREFIX, SESSION, world  # noqa: F401  (fixture)

_LINES = 4000
_BAR_SECONDS = 0.15  # DR-344 bar is 500ms; process time, never wall clock


def _big_pair():
    """Repetitive lines (the quadratic case: untrimmed this costs ~280ms) with two nearby edits."""
    old = [f"    v{i % 3}\n".encode() for i in range(_LINES)]
    new = list(old)
    new[_LINES // 2] = b"edited\n"
    new[_LINES // 2 + 200:_LINES // 2 + 200] = [b"inserted\n"]
    return old, new


def test_hunks_of_a_large_file_stays_well_under_the_bar_in_process_time():
    old, new = _big_pair()
    start = time.process_time()
    hunks = own_hunk_stage._hunks_of("big.py", old, new)
    spent = time.process_time() - start
    assert len(hunks) == 2, hunks
    assert spent < _BAR_SECONDS, spent


def test_delta_spans_of_a_large_file_stays_well_under_the_bar_in_process_time():
    old, new = _big_pair()
    start = time.process_time()
    spans = run_hunk_claims._delta_spans(old, new)
    spent = time.process_time() - start
    assert len(spans) == 2, spans
    assert spent < _BAR_SECONDS, spent


def _edited_world(root, monkeypatch):
    from coordinator_core.ops.tests.test_review_reverify import _ledger, _row

    (root / "tests/test_plain.py").write_text("def test_p():\n    assert nope\n", encoding="utf-8")
    return _ledger(monkeypatch, _row("F1", "tests/test_plain.py", "    assert True\n", "    assert nope\n"))


def test_probe_open_with_an_empty_run_id_still_reads_the_halted_holds(world, monkeypatch):
    from coordinator_core.ops import review_reverify as rr

    root, _orig, _ = world
    sidecars = _edited_world(root, monkeypatch)
    seen = {}

    def held(_root, run_id, _plan, paths):
        seen["run_id"] = run_id
        return {p: "other-halted" for p in paths}

    monkeypatch.setattr(land_run, "foreign_halted_withheld", held)
    out = rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=["tests/test_plain.py::test_p"],
        sidecars=sidecars, probe="open", run_id="",
    )
    assert seen == {"run_id": None}
    assert "tests/test_plain.py" in out["error"] and out["probed"] == []


def test_revert_keeps_a_path_a_halted_peer_withholds(world, monkeypatch):
    from coordinator_core.ops import review_reverify as rr

    root, _orig, _ = world
    sidecars = _edited_world(root, monkeypatch)
    edited = (root / "tests/test_plain.py").read_text(encoding="utf-8")
    monkeypatch.setattr(land_run, "foreign_halted_withheld", lambda _r, _id, _p, paths: {p: "peer" for p in paths})
    out = rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=["tests/test_plain.py::test_p"],
        sidecars=sidecars, run_id="me",
    )
    assert out["reverted"] == []
    assert [k["findings"][0]["reason"] for k in out["kept"]] == ["peer-withheld"]
    assert (root / "tests/test_plain.py").read_text(encoding="utf-8") == edited


def test_changed_opcodes_match_the_untrimmed_differ_on_a_small_file():
    old = [b"a\n", b"b\n", b"c\n", b"d\n", b"e\n"]
    new = [b"a\n", b"B\n", b"c\n", b"d\n", b"e\n", b"f\n"]
    full = [
        op for op in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes() if op[0] != "equal"
    ]
    assert run_hunk_claims.changed_opcodes(old, new) == full
    assert run_hunk_claims.changed_opcodes(old, old) == []


def test_foreign_halted_withheld_unreadable_record_dir_holds_every_wanted_path(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import sibling_claims

    def _boom(*_a, **_k):
        raise OSError("unreadable")

    monkeypatch.setattr(sibling_claims, "_unlanded_run_ids", _boom)
    held = land_run.foreign_halted_withheld(tmp_path, "run-a", None, ["x.py", "", "y.py"])
    assert held == {"x.py": "unreadable-halt-record", "y.py": "unreadable-halt-record"}


def test_foreign_halted_withheld_malformed_record_holds_every_wanted_path(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import sibling_claims

    def _bad(*_a, **_k):
        raise ValueError("malformed")

    monkeypatch.setattr(sibling_claims, "_unlanded_run_ids", _bad)
    assert land_run.foreign_halted_withheld(tmp_path, "run-a", None, ["x.py"]) == {"x.py": "unreadable-halt-record"}


def _recent_run_dir(root, suffix):
    from datetime import datetime, timedelta, timezone

    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    stamp = (datetime.now(timezone.utc) - timedelta(seconds=60)).strftime("%Y%m%dT%H%M%S")
    path = root / RECORD_DIR_ROOT / f"ask-{stamp}-{suffix}"
    path.mkdir(parents=True)
    return path


def test_foreign_halted_withheld_corrupt_halt_marker_holds_every_wanted_path(tmp_path):
    (_recent_run_dir(tmp_path, "bbbbbb") / "halted.json").write_text("{not json", encoding="utf-8")
    held = land_run.foreign_halted_withheld(tmp_path, "run-a", None, ["x.py", "y.py"])
    assert held == {"x.py": "unreadable-halt-record", "y.py": "unreadable-halt-record"}


def test_foreign_halted_withheld_non_object_halt_marker_holds(tmp_path):
    (_recent_run_dir(tmp_path, "bbbbbb") / "halted.json").write_text("[1]", encoding="utf-8")
    assert land_run.foreign_halted_withheld(tmp_path, "run-a", None, ["x.py"]) == {"x.py": "unreadable-halt-record"}


def test_foreign_halted_withheld_absent_halt_marker_holds_nothing(tmp_path):
    _recent_run_dir(tmp_path, "bbbbbb")
    assert land_run.foreign_halted_withheld(tmp_path, "run-a", None, ["x.py"]) == {}


def test_foreign_halted_withheld_no_wanted_paths_holds_nothing_even_on_error(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import sibling_claims

    monkeypatch.setattr(sibling_claims, "_unlanded_run_ids", lambda *_a, **_k: (_ for _ in ()).throw(OSError("x")))
    assert land_run.foreign_halted_withheld(tmp_path, "run-a", None, []) == {}


def _review_edit_halt_commit(repo, monkeypatch, holder):
    from coordinator_core.ops.dispatch_emit import review_edits
    from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (
        RUN, _call, _landed, _write_record,
    )

    _write_record(repo, RUN)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "twin.py").write_text("# reviewed\n", encoding="utf-8")
    monkeypatch.setattr(review_edits, "review_writes", lambda *a, **k: {"pkg/twin.py": "agent-1"})
    monkeypatch.setattr(land_run, "foreign_halted_withheld", holder)
    out = _call(repo)
    return out, "pkg/twin.py" in _landed(repo, out, "--name-only")


def test_halted_record_does_not_stage_a_review_edit_another_run_holds(repo, monkeypatch):
    out, landed = _review_edit_halt_commit(
        repo, monkeypatch, lambda _r, _id, _p, paths: {p: "peer-run" for p in paths if p == "pkg/twin.py"}
    )
    assert not landed, out
    assert "pkg/twin.py" in out["review_edits_unlanded"], out


def test_halted_record_stages_a_review_edit_no_run_holds(repo, monkeypatch):
    out, landed = _review_edit_halt_commit(repo, monkeypatch, lambda _r, _id, _p, paths: {})
    assert landed, out


def test_a_co_declared_hold_on_an_inherited_path_still_refuses(product_repo, monkeypatch):
    """terminal_commit holds a co-declared peer path whether or not it is inherited; land_run agrees."""
    from coordinator_core.ops.dispatch_emit import peer_hunk_gate
    from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
    from coordinator_core.ops.dispatch_emit.tests import test_land_run_refire_inherits_withheld_scope as refire
    from coordinator_core.session import touch_record

    monkeypatch.setattr(land_run, "_ledger_gate", lambda *a, **k: (None, None, []))
    monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: True)
    root = product_repo
    refire._stage(root)
    refire._predecessor_halt(root, refire.PRED, {refire.INHERITED: refire.HALT_REASON})
    refire._predecessor_halt(root, refire.FOREIGN, {refire.INHERITED: refire.HALT_REASON})
    foreign_dir = f"{refire.RECORD_DIR_ROOT}/{refire.FOREIGN}"
    prod._write(root, f"{foreign_dir}/manifest.json", '{"rows": [{"id": "F1", "writes": ["%s"]}]}' % refire.INHERITED)
    prod._write(root, f"{foreign_dir}/run.json", '{"sizing_rel": "state/sizings/other.yaml"}')

    out = prod._land(root, resume_ledger=refire._resume())

    assert out.get("refused") == peer_hunk_gate.PEER_HUNKS_CO_DECLARED, out
    assert refire.INHERITED not in prod._tree(root)
