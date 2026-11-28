"""route_heal.classify maps break reasons to a heal or a runnable, tracked fix."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

from coordinator_core.group_em import route_heal

ROOT = Path(__file__).resolve().parents[3]


def _reasons():
    spec = importlib.util.spec_from_file_location(
        "delivery_reason_under_test", ROOT / "coordinator" / "bin" / "_unimatrix" / "delivery_reason.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.REASONS


@pytest.mark.parametrize(
    "reason",
    ["no_binding", "no leg endpoint", "grok session is not listening for agent mail", "box_unreachable", "no_receipt"],
)
def test_every_named_break_has_a_fix_command(reason):
    fix = route_heal.classify(reason)["fix"]
    assert fix.startswith("python coordinator/bin/") and "/" + "help" not in fix
    assert not fix.startswith("/")


def test_no_leg_listening_aliases_share_the_agy_heal():
    for reason in ("no_leg_listening", "no leg endpoint", "delivery failed: grok session is not listening"):
        assert route_heal.classify(reason)["heal"] is route_heal.heal_agy_leg


def test_box_unreachable_is_far_unreachable():
    assert route_heal.classify("box_unreachable") == route_heal.classify("far_unreachable")
    assert route_heal.classify("box_unreachable")["heal"] is None


def test_reason_with_detail_suffix_resolves_by_prefix():
    assert route_heal.classify("no_binding:whatever")["fix"] == route_heal.classify("no_binding")["fix"]


def test_unknown_reason_still_names_a_fix_and_no_heal():
    out = route_heal.classify("something_new")
    assert out["heal"] is None and out["fix"].startswith("python coordinator/bin/")


def test_every_table_reason_is_registered_in_delivery_reason():
    assert route_heal.table_reasons() <= _reasons()


def test_every_fix_script_is_tracked_at_head():
    tracked = set(
        subprocess.run(
            ["git", "ls-files", "coordinator/bin"], cwd=ROOT, capture_output=True, text=True, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.split()
    )
    paths = route_heal.fix_paths()
    assert paths
    for path in paths:
        assert path in tracked, path


def test_heal_agy_leg_reports_spawn_failure_as_false(monkeypatch):
    def boom(*a, **k):
        raise OSError("no spawn")

    monkeypatch.setattr(route_heal.subprocess, "run", boom)
    assert route_heal.heal_agy_leg("c") is False


def test_heal_agy_leg_true_only_on_exit_zero(monkeypatch):
    class Done:
        def __init__(self, rc):
            self.returncode = rc

    monkeypatch.setattr(route_heal.subprocess, "run", lambda *a, **k: Done(0))
    assert route_heal.heal_agy_leg("c") is True
    monkeypatch.setattr(route_heal.subprocess, "run", lambda *a, **k: Done(2))
    assert route_heal.heal_agy_leg("c") is False
