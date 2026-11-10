"""land_run.foreign_halted_withheld: a hold that cannot be computed holds every wanted path.

Also the review_reverify peer-withheld keep, the halted-record review-edit hold, and the hunk differ's process-time bound on a land_run.py-sized file (`own_hunk_stage`, `run_hunk_claims`).
"""

import difflib
import time

from coordinator_core.ops.dispatch_emit import land_run, own_hunk_stage, run_hunk_claims
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo as product_repo  # noqa: F401  (fixture)
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import repo  # noqa: F401  (fixture)
from coordinator_core.ops.tests.test_review_reverify import BASE, PREFIX, SESSION, world  # noqa: F401  (fixture)

import pytest

pytestmark = pytest.mark.complete

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


def test_foreign_halted_withheld_corrupt_marker_with_clear_index_entry_is_not_opened(tmp_path):
    from coordinator_core.ops.dispatch_emit import halted_withheld_index

    run_dir = _recent_run_dir(tmp_path, "bbbbbb")
    (run_dir / "halted.json").write_text("{not json", encoding="utf-8")
    halted_withheld_index.add_run(tmp_path, run_dir.name, ["other.txt"])
    assert land_run.foreign_halted_withheld(tmp_path, "run-a", None, ["x.py"]) == {}
    (run_dir / "halted.json").write_text("{not json", encoding="utf-8")
    halted_withheld_index.add_run(tmp_path, run_dir.name, ["x.py"])
    assert land_run.foreign_halted_withheld(tmp_path, "run-a", None, ["x.py"]) == {"x.py": "unreadable-halt-record"}


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


def test_halted_record_keeps_a_review_edit_no_run_holds_in_the_tree(repo, monkeypatch):
    """A halt commit lands the record only; an unheld review edit stays for the refire (040758b972)."""
    from coordinator_core.ops.dispatch_emit.halted_record_commit import HALT_RUN_REASON

    out, landed = _review_edit_halt_commit(repo, monkeypatch, lambda _r, _id, _p, paths: {})
    assert not landed, out
    assert out["review_edits_unlanded"] == {"pkg/twin.py": HALT_RUN_REASON}, out


def test_a_co_declared_hold_on_an_inherited_path_is_recorded_as_provenance_and_lands(product_repo, monkeypatch):
    """A co-declared peer path is provenance whether or not it is inherited; the landing commits."""
    from coordinator_core.ops.dispatch_emit import peer_hunk_gate
    from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
    from coordinator_core.ops.dispatch_emit.tests import test_land_run_refire_inherits_withheld_scope as refire
    from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
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

    out = prod._land(root, resume_ledger=refire._resume(), inline_review=reviewed())

    assert out.get("refused") is None, out
    assert out.get("committed") is True, out
    assert [e for e in out.get("provenance") or [] if e["reason"] == peer_hunk_gate.PEER_HUNKS_CO_DECLARED], out


_PRE_SEED = "".join(f"line{i}\n" for i in range(1, 13))
_PRE_FIX = _PRE_SEED.replace("line4\n", "fixed-four\n")
_PRE_REL = "pkg/guard.py"


def _crlf_repo(tmp_path):
    import subprocess

    from coordinator_core.git import git_objects
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.win_portability import no_console_creationflags

    for cmd in (("init", "-q"), ("config", "user.email", "t@e.com"), ("config", "user.name", "t"),
                ("config", "commit.gpgsign", "false"), ("config", "core.autocrlf", "true")):
        subprocess.run(["git", *cmd], cwd=str(tmp_path), check=True, capture_output=True, **no_console_creationflags())
    (tmp_path / _PRE_REL).parent.mkdir()
    (tmp_path / _PRE_REL).write_bytes(_PRE_SEED.encode())
    for cmd in (("add", _PRE_REL), ("commit", "-q", "-m", "seed")):
        subprocess.run(["git", *cmd], cwd=str(tmp_path), check=True, capture_output=True, **no_console_creationflags())
    # The worktree carries a pre-existing CRLF hunk, as autocrlf checkouts do; the baseline is its raw hash.
    (tmp_path / _PRE_REL).write_bytes(_PRE_FIX.replace("\n", "\r\n").encode())
    common = resolve_git_common_dir(tmp_path)
    raw = (tmp_path / _PRE_REL).read_bytes()
    return {
        "baseline": {_PRE_REL: git_objects.write_object(common, b"blob", raw)},
        "measured": git_objects.write_object(common, b"blob", _PRE_FIX.encode()),
    }


def test_crlf_baseline_pre_existing_hunk_is_not_the_runs_own(tmp_path):
    seen = _crlf_repo(tmp_path)

    assert land_run._peer_dirt_only(tmp_path, _PRE_REL, seen["baseline"])


def test_verified_pre_existing_hunk_is_recognised_only_while_the_worktree_matches_the_measure(tmp_path):
    seen = _crlf_repo(tmp_path)

    assert land_run._verified_preexisting(tmp_path, _PRE_REL, seen["measured"], seen["baseline"])
    assert not land_run._verified_preexisting(tmp_path, _PRE_REL, "0" * 40, seen["baseline"])
    assert not land_run._verified_preexisting(tmp_path, _PRE_REL, None, seen["baseline"])
    (tmp_path / _PRE_REL).write_bytes(_PRE_FIX.replace("line9\n", "late-nine\n").replace("\n", "\r\n").encode())
    assert not land_run._verified_preexisting(tmp_path, _PRE_REL, seen["measured"], seen["baseline"])


def test_a_run_own_hunk_is_never_taken_for_a_verified_pre_existing_hunk(tmp_path):
    seen = _crlf_repo(tmp_path)
    own = _PRE_FIX.replace("line10\n", "own-ten\n")
    (tmp_path / _PRE_REL).write_bytes(own.replace("\n", "\r\n").encode())
    from coordinator_core.git import git_objects
    from coordinator_core.git.git_dir import resolve_git_common_dir

    measured = git_objects.write_object(resolve_git_common_dir(tmp_path), b"blob", own.encode())

    assert not land_run._verified_preexisting(tmp_path, _PRE_REL, measured, seen["baseline"])


def test_mismatch_detail_names_the_differing_line_and_the_cause_specific_remedy(tmp_path):
    seen = _crlf_repo(tmp_path)
    blobs = {_PRE_REL: seen["measured"]}

    held = land_run._mismatch_detail(tmp_path, _PRE_REL, blobs, {})
    assert "hand-commit this path whole" in held and held.startswith(_PRE_REL)

    (tmp_path / _PRE_REL).write_bytes(_PRE_FIX.replace("line9\n", "late-nine\n").replace("\n", "\r\n").encode())
    drifted = land_run._mismatch_detail(tmp_path, _PRE_REL, blobs, {})
    assert "first difference at line 9: measured 'line9', landing 'late-nine'" in drifted
    assert "re-run dispatch.measured_rev" in drifted and "hand-commit" not in drifted


def _declare_two_paths(root, first, second):
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
    from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod

    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=(first, second)),), plan_path=prod.PLAN
    )
    prod._write(root, prod.SCRIPT, "// emitted\n" + render_marker(request) + "\n")


def test_product_dirty_declared_write_left_after_landing_is_reported_with_its_path(product_repo, monkeypatch):
    """A declared product write dirty after the commit and not landed names its path in product_residue and error."""
    from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod

    prod._author(product_repo, "src/a.py", "a = 1\n")
    _declare_two_paths(product_repo, "src/a.py", "src/late.py")
    prod._record_dir(product_repo)
    monkeypatch.setattr(
        land_run, "_reap_measured_export",
        lambda root, run: prod._write(root, "src/late.py", "late = 1\n"),
    )

    out = prod._land(product_repo)

    assert out["committed"] is True, out
    assert out["product_residue"] == {"src/late.py": "product_dirty"}, out
    assert out["ok"] is False
    assert "src/late.py" in out["error"]


def test_product_dirty_landed_declared_write_reports_no_product_residue(product_repo):
    """The other side: every declared write landed, so the reply carries no product_residue and no error."""
    from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod

    prod._author(product_repo, "src/a.py", "a = 1\n")
    prod._declare(product_repo, "src/a.py")
    prod._record_dir(product_repo)

    out = prod._land(product_repo)

    assert out["committed"] is True, out
    assert "product_residue" not in out and out["error"] is None, out


from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import repo as ask_run_repo  # noqa: E402,F401


def _stage_pre_existing_measured(root, rig):
    """a.py tracked with a pre-existing dirty hunk the run never touched; its manifest baselines the
    dirty bytes and the measured-rev record carries the dirty blob."""
    import json

    from coordinator_core.git import commit
    from coordinator_core.ops.dispatch_emit import write_baseline as wb
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT, measured_rev_record_path

    rig._write(root, "a.py", _PRE_SEED)
    rig._git(root, "add", "a.py")
    rig._git(root, "commit", "-q", "-m", "seed a")
    rig._put(root, "a.py", _PRE_FIX)
    manifest_rel = f"{RUN_DIR_ROOT}/{rig.RUN}/manifest.json"
    data = json.loads((root / manifest_rel).read_text(encoding="utf-8"))
    data["write_baseline"] = wb.capture(root, ["a.py"])
    (root / manifest_rel).write_text(json.dumps(data), encoding="utf-8")
    record = measured_rev_record_path(root, rig.RUN)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        json.dumps({
            "sha": "0" * 40, "base_sha": "0" * 40, "paths": ["a.py"], "removed": [], "tree": "0" * 40,
            "blobs": {"a.py": commit.checkin_blob_sha(root, "a.py")},
        }),
        encoding="utf-8",
    )


def test_land_run_lands_a_verified_pre_existing_hunk(ask_run_repo):
    from coordinator_core.ops.dispatch_emit.tests import test_ask_run_product_commit as rig

    root = ask_run_repo
    _stage_pre_existing_measured(root, rig)

    landed = rig._call(root)

    assert landed.get("refused") is None and landed["committed"] is True, landed
    assert "a.py" in rig._names(root, landed["sha"])


def test_land_run_does_not_refuse_a_peer_drift_on_a_pre_existing_hunk_the_run_never_changed(ask_run_repo):
    from coordinator_core.ops.dispatch_emit.tests import test_ask_run_product_commit as rig

    root = ask_run_repo
    _stage_pre_existing_measured(root, rig)
    rig._put(root, "a.py", _PRE_FIX.replace("line9\n", "late-nine\n"))

    out = rig._call(root)

    assert out["refused"] != land_run.LANDING_TREE_DIFFERS, out
    assert out["committed"] is True, out


# ---- tranche chain hand-off --------------------------------------------------------------------


def _tranche_script(root, passes):
    from coordinator_core.ops.dispatch_emit import tranche_chain

    inv = root / "inv-t1.md"
    inv.write_text("record\n", encoding="utf-8")
    (root / "inv.md").write_text("base record\n", encoding="utf-8")
    spine = root / "run1.md"
    spine.write_text(
        "---\nderived_from: mise inventory record\n---\nDerived from `inv-t1.md` by `dispatch.emit --inventory`\n",
        encoding="utf-8",
    )
    spine.write_text(tranche_chain.stamp_tranche_state(spine.read_text(encoding="utf-8"), 28, passes), encoding="utf-8")
    (root / "run1.workflow.mjs").write_text("//\n", encoding="utf-8")
    return "run1.workflow.mjs"


def _landed(root, monkeypatch, script, reply=None, **params):
    from coordinator_core.ops.dispatch_emit import tranche_chain

    spawns = []
    monkeypatch.setattr(tranche_chain, "_spawn_detached", lambda *a: spawns.append(a))
    land_run._chain_landed_tranche(
        root, {"run_id": "run1", "script_path": script, "incomplete_chunks": [], **params},
        {"committed": True, "sha": "a" * 40, **(reply or {})},
    )
    return spawns


def test_landing_a_tranche_with_passes_remaining_spawns_exactly_one_chain(tmp_path, monkeypatch):
    spawns = _landed(tmp_path, monkeypatch, _tranche_script(tmp_path, 2))

    assert len(spawns) == 1
    assert spawns[0][1] == "run1" and spawns[0][2]["row_budget"] == 28
    assert spawns[0][2]["inventory_path"].endswith("inv.md")


def test_landing_the_last_tranche_spawns_nothing(tmp_path, monkeypatch):
    assert _landed(tmp_path, monkeypatch, _tranche_script(tmp_path, 0)) == []


@pytest.mark.parametrize(
    "reply, params",
    [
        ({"already_landed": True}, {}),
        ({"refused": "unreviewed"}, {}),
        ({"stranded": {"C1": ["a.py"]}}, {}),
        ({}, {"incomplete_chunks": ["C2"]}),
    ],
)
def test_a_halted_partial_or_repeat_land_never_chains(tmp_path, monkeypatch, reply, params):
    assert _landed(tmp_path, monkeypatch, _tranche_script(tmp_path, 2), reply, **params) == []


def test_chain_run_records_the_next_run_and_a_failure_in_tranche_chain_json(tmp_path, monkeypatch):
    import json

    from coordinator_core.ops.dispatch_emit import ask_contract, tranche_chain

    monkeypatch.setattr(ask_contract, "record_dir", lambda root, run_id: tmp_path / "rec" / run_id)
    fired = []
    ok = tranche_chain.run_chain(
        tmp_path, "run1", {"row_budget": 28},
        emit=lambda root, req: {"path": "x/run2.workflow.mjs", "ok": True, "run_id": "run2"},
        fire=lambda *a: fired.append(a),
    )
    assert ok == {"chained": "run2", "chain_error": None} and len(fired) == 1
    assert json.loads((tmp_path / "rec/run1/tranche-chain.json").read_text()) == {"chained": "run2", "chain_error": None}

    def boom(root, req):
        raise RuntimeError("no emit")

    bad = tranche_chain.run_chain(tmp_path, "run1", {}, emit=boom, fire=lambda *a: None)
    assert bad["chained"] is None and "no emit" in bad["chain_error"]
    assert "no emit" in json.loads((tmp_path / "rec/run1/tranche-chain.json").read_text())["chain_error"]


def _record_for_p(tmp_path, text):
    from coordinator_core.git import commit

    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "p.py").write_text(text, encoding="utf-8", newline="")
    return {"blobs": {"p.py": commit.checkin_blob_sha(tmp_path, "p.py")}, "removed": []}


def test_no_change_drops_only_the_paths_the_run_never_changed(tmp_path):
    record = _record_for_p(tmp_path, "x = 1\n")
    (tmp_path / "p.py").write_text("x = 2\n", encoding="utf-8", newline="")
    seen: list = []

    def never_changed(paths):
        seen.append(list(paths))
        return []

    assert land_run._measured_mismatch(tmp_path, record, ["p.py"], [], {}, no_change=never_changed) == ["p.py"]
    assert seen == [["p.py"]]
    assert land_run._measured_mismatch(tmp_path, record, ["p.py"], [], {}, no_change=lambda paths: ["p.py"]) == []


def test_run_made_no_change_keeps_a_path_out_when_it_cannot_prove_it(tmp_path, monkeypatch):
    base = {"p.py": "a" * 40}
    assert land_run._run_made_no_change(tmp_path, "run1", None, None)(["p.py"]) == []
    assert land_run._run_made_no_change(tmp_path, None, base, None)(["p.py"]) == []
    assert land_run._run_made_no_change(tmp_path, "run1", base, None)(["other.py"]) == []
    assert land_run._run_made_no_change(tmp_path, "run1", base, None)(["p.py"]) == []


def test_run_made_no_change_separates_an_own_hunk_from_peer_bytes_only(tmp_path, monkeypatch):
    from coordinator_core.git import git_dir, git_objects
    from coordinator_core.ops.dispatch_emit import drift_attribution

    monkeypatch.setattr(git_dir, "resolve_git_common_dir", lambda root: tmp_path)
    monkeypatch.setattr(
        drift_attribution, "run_recorded_blobs", lambda rdir, wanted: {p: ["b" * 40] for p in wanted},
    )
    monkeypatch.setattr(git_objects, "read_object", lambda common, sha: ("blob", b"bytes"))
    base = {"own.py": "a" * 40, "peer.py": "a" * 40}
    monkeypatch.setattr(land_run, "_own_hunks", lambda root, p, b, data: ["hunk"] if p == "own.py" else [])
    never = land_run._run_made_no_change(tmp_path, "run1", base, lambda paths: [])
    assert never(["own.py", "peer.py"]) == ["peer.py"]
    written = land_run._run_made_no_change(tmp_path, "run1", base, lambda paths: ["peer.py"])
    assert written(["own.py", "peer.py"]) == []


def test_prior_pass_sessions_walks_the_resumed_chain_only_when_the_run_has_a_ledger(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import landing_integrity
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    walked = []
    monkeypatch.setattr(landing_integrity, "_resumed_chain", lambda root, rid: walked.append(rid) or [])
    assert land_run._prior_pass_sessions(tmp_path, "run-no-ledger") == set()
    assert walked == []
    ledger = record_dir(tmp_path, "run-ledger")
    ledger.mkdir(parents=True)
    (ledger / "run.json").write_text("{}", encoding="utf-8")
    land_run._prior_pass_sessions(tmp_path, "run-ledger")
    assert walked == ["run-ledger"]
