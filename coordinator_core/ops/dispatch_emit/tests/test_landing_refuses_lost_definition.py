"""A landed caller of a module attribute defined only by an uncommitted file the landing leaves out is lost-definition."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import land_run, landing_integrity as li
from coordinator_core.ops.dispatch_emit.tests.test_landing_integrity import _w
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process  # repo setup only; the calls under test are spawn-guarded

# (module file, attribute the worktree adds, caller source) -- the three incidents.
INCIDENTS = [
    ("pkg/run_token.py", "redeem_claim", "from pkg import run_token\nrun_token.redeem_claim()\n"),
    ("pkg/claims.py", "holds_claim", "from pkg.claims import holds_claim\n"),
    ("pkg/box_probe.py", "stored_capability", "from pkg import box_probe\nbox_probe.stored_capability()\n"),
]


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=root, check=True, capture_output=True, **no_console_creationflags(),
    )


def _repo(tmp_path, module, attr):
    _git(tmp_path, "init", "-q")
    _w(tmp_path, "pkg/__init__.py", "")
    _w(tmp_path, module, "def other():\n    return 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _w(tmp_path, module, f"def other():\n    return 1\n\n\ndef {attr}():\n    return 2\n")
    return tmp_path


@pytest.fixture
def no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("git/process spawn in lost_definition_findings")

    def arm():
        monkeypatch.setattr(subprocess, "Popen", boom)
        monkeypatch.setattr(subprocess, "run", boom)

    return arm


@pytest.mark.parametrize("module,attr,caller", INCIDENTS)
def test_caller_of_an_attribute_only_the_dirty_definition_has_is_a_lost_definition(
    tmp_path, module, attr, caller, no_spawn,
):
    root = _repo(tmp_path, module, attr)
    _w(root, "caller.py", caller)
    no_spawn()
    out = li.lost_definition_findings(root, ["caller.py"], [])
    assert [(f.path, f.reason) for f in out] == [("caller.py", li.LOST_DEFINITION)], out
    assert module in out[0].detail


@pytest.mark.parametrize("module,attr,caller", INCIDENTS)
def test_a_landing_that_carries_the_definition_is_clean(tmp_path, module, attr, caller, no_spawn):
    root = _repo(tmp_path, module, attr)
    _w(root, "caller.py", caller)
    no_spawn()
    assert li.lost_definition_findings(root, ["caller.py", module], []) == []
    assert li.lost_definition_findings(root, ["caller.py"], [], carried=[module]) == []


def test_a_caller_of_an_attribute_head_already_has_is_clean(tmp_path, no_spawn):
    root = _repo(tmp_path, "pkg/m.py", "added")
    _w(root, "caller.py", "from pkg.m import other\n")
    no_spawn()
    assert li.lost_definition_findings(root, ["caller.py"], []) == []


def test_lost_definition_is_not_a_claim_provenance_reason():
    assert li.LOST_DEFINITION not in li.CLAIM_PROVENANCE_REASONS


@pytest.mark.parametrize("module,attr,caller", INCIDENTS)
def test_carry_drops_a_non_test_caller_whose_target_stays_out(tmp_path, module, attr, caller, monkeypatch):
    root = _repo(tmp_path, module, attr)
    _w(root, "caller.py", caller)
    prov = li.import_closure_findings(root, ["caller.py"], [])
    assert prov, "fixture must raise the import-closure finding"
    carry, dropped = land_run._carry_withheld_symbol_targets(
        root, prov, ["caller.py"], [], barred={module},
        session_id=None, rid=None, script_path=None, plan_rel=None, run_base=None,
        minted_before=None, own_run_ids=(),
    )
    assert carry == []
    assert dropped == {"caller.py": f"{li.LOST_DEFINITION}: {module}"}


def test_carry_keeps_a_free_target_and_lands_both(tmp_path, monkeypatch):
    module, attr, caller = INCIDENTS[0]
    root = _repo(tmp_path, module, attr)
    _w(root, "caller.py", caller)
    prov = li.import_closure_findings(root, ["caller.py"], [])
    monkeypatch.setattr(land_run, "_peer_held", lambda *a, **k: {})
    carry, dropped = land_run._carry_withheld_symbol_targets(
        root, prov, ["caller.py"], [], barred=set(),
        session_id=None, rid=None, script_path=None, plan_rel=None, run_base=None,
        minted_before=None, own_run_ids=(),
    )
    assert carry == [module]
    assert dropped == {}


def test_carry_still_names_a_test_caller_test_without_source(tmp_path):
    module, attr, caller = INCIDENTS[0]
    root = _repo(tmp_path, module, attr)
    _w(root, "tests/test_caller.py", caller)
    prov = li.import_closure_findings(root, ["tests/test_caller.py"], [])
    _, dropped = land_run._carry_withheld_symbol_targets(
        root, prov, ["tests/test_caller.py"], [], barred={module},
        session_id=None, rid=None, script_path=None, plan_rel=None, run_base=None,
        minted_before=None, own_run_ids=(),
    )
    assert dropped == {"tests/test_caller.py": f"{land_run.TEST_WITHOUT_SOURCE}: {module}"}
