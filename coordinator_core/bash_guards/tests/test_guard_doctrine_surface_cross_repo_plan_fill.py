"""Plan-fill Bash shapes judge the same whatever the payload cwd is, and the
sanctioned scaffolder launched by interpreter is read-shape for state/sizings."""
import pytest

from coordinator_core.bash_guards.guard_doctrine_surface_bash_write import (
    _sizing_write_denied,
)

S = "state/sizings/2026-10-22-x.yaml"
PLUGIN = "/mnt/repoB/coordinator"
PLAN = "docs/plans/2026-10-22-p.md"

PLAN_FILL = [
    f"coordinator-doc-new --type plan --sizing-object {S} --out {PLAN}",
    f"python3 {PLUGIN}/bin/coordinator-doc-new.py --type plan --sizing-object {S} --out {PLAN}",
    f"python {PLUGIN}/bin/coordinator-doc-new --type plan --sizing-object {S} --out {PLAN}",
    f"PYTHONPATH=. python3 {PLUGIN}/bin/coordinator-doc-new.py --type plan --sizing-object {S} --out {PLAN}",
    f"python3 {PLUGIN}/lib/plan_scaffold_markers.py {PLAN}",
    f"python3 {PLUGIN}/bin/plan-spine-check.py {PLAN}",
    f"coordinator-invoke plan.tasks.mutate '{{\"plan\":\"{PLAN}\",\"op\":\"evidence-append\"}}'",
]

DENIED = [
    f"python3 -c \"open('{S}','w')\"",
    f"python3 {PLUGIN}/bin/coordinator-doc-new.py --sizing-object {S} > {S}",
    f"python3 -m json.tool x > {S}",
    f"python3 {PLUGIN}/bin/coordinator-doc-new.py -c \"open('{S}','w')\"",
    f"python3 {PLUGIN}/bin/coordinator-doc-new.py -Ic \"open('{S}','w')\"",
    f"python3 {PLUGIN}/bin/coordinator-doc-new.py --sizing-object {S} && echo x > {S}",
    f"python3 {PLUGIN}/bin/coordinator-doc-new.py --sizing-object {S} | tee {S}",
]


@pytest.mark.parametrize("cmd", PLAN_FILL)
def test_plan_fill_shape_is_admitted(cmd):
    assert not _sizing_write_denied(cmd)


@pytest.mark.parametrize("cmd", DENIED)
def test_write_shapes_still_deny(cmd):
    assert _sizing_write_denied(cmd)
