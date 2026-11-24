"""A declared path in the run's own measured-touched.json stays in the overlay candidate when a finished run
(not live, not halted-withholding) is its named owner; a live peer's hunk stays split. The collection-failure
head names the exception class the traceback raised, not pytest's `ImportError while importing` framing."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import hunk_provenance as hp
from coordinator_core.ops.dispatch_emit.tests.test_hunk_provenance_predecessor_hunk_adopted import (  # noqa: F401
    ME, PATH, PEER, world,
)


def _split(world):
    return hp.peer_split(world.root, ME, [PATH], own_plan="plan-a")


def _own_measured_behind_a_finished_owner(world, monkeypatch, *, live: bool):
    world.setup_worktree_hunk()
    world.st["preds"] = []
    world.st["owner"] = [PEER]
    world.record(ME, "measured-touched.json", [PATH])
    world.record(PEER, "measured-touched.json", [PATH])
    monkeypatch.setattr(hp, "_non_live", lambda root, owner: not live)


def test_an_own_measured_declared_path_stays_whole_when_a_finished_run_also_touched_it(world, monkeypatch):
    _own_measured_behind_a_finished_owner(world, monkeypatch, live=False)
    assert _split(world) == ({}, {})


def test_a_live_peer_still_splits_the_path_the_run_measured(world, monkeypatch):
    _own_measured_behind_a_finished_owner(world, monkeypatch, live=True)
    split, _unsettled = _split(world)
    assert split[PATH]["owner"] == PEER


def test_a_finished_owner_of_a_path_the_run_did_not_measure_stays_split(world, monkeypatch):
    _own_measured_behind_a_finished_owner(world, monkeypatch, live=False)
    world.record(ME, "measured-touched.json", [])
    split, _unsettled = _split(world)
    assert split[PATH]["owner"] == PEER


def test_a_finished_owner_of_an_undeclared_path_stays_split(world, monkeypatch):
    _own_measured_behind_a_finished_owner(world, monkeypatch, live=False)
    world.st["declared"] = frozenset()
    split, _unsettled = _split(world)
    assert split[PATH]["owner"] == PEER


def test_a_halted_holder_withholding_the_path_stays_split(world, monkeypatch):
    _own_measured_behind_a_finished_owner(world, monkeypatch, live=False)
    monkeypatch.setattr(hp, "_halted_holder", lambda root, run, plan, path: PEER)
    split, _unsettled = _split(world)
    assert split[PATH]["owner"] == PEER


_ATTRIBUTE_ERROR = (
    "_ ERROR collecting t/test_stage_images.py __\n"
    "ImportError while importing test module '/m/t/test_stage_images.py'.\n"
    "Hint: make sure your test modules/packages have valid Python names.\n"
    "Traceback:\n"
    "t/test_stage_images.py:3: in <module>\n"
    "    import stage_images\n"
    "stage_images.py:25: in <module>\n"
    "    X = rd.STAGES_DIR\n"
    "E   AttributeError: module 'review_delivery' has no attribute 'STAGES_DIR'\n"
    "=== short test summary info ===\n"
)


def test_a_collection_failure_head_names_the_raised_attribute_error():
    head = cov.first_setup_error(_ATTRIBUTE_ERROR)
    assert head.startswith("first setup ERROR: raised AttributeError | ")
    assert "STAGES_DIR" in head


def test_a_collection_failure_that_raised_import_error_names_import_error():
    out = "_ ERROR collecting t/test_x.py __\nImportError while importing test module\nE   ImportError: cannot import name 'x'\n"
    assert cov.first_setup_error(out).startswith("first setup ERROR: raised ImportError | ")


def test_a_failure_with_no_exception_line_names_no_class():
    out = "_ ERROR at setup of test_a __\nE       fixture 'absent' not found\n"
    assert "raised" not in cov.first_setup_error(out)


def test_a_conftest_load_failure_names_the_raised_class():
    out = "ImportError while loading conftest '/m/conftest.py'.\nE   AttributeError: module 'm' has no attribute 'Y'\n"
    head = cov.first_setup_error(out)
    assert head.startswith("first setup ERROR: raised AttributeError | ")
    assert "conftest" in head
