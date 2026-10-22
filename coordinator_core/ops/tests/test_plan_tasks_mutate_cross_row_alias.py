"""A spine whose row aliases an anchor in another row patches without crashing."""
import yaml

from coordinator_core.ops import plan_tasks_mutate as m

BODY = """- id: C1
  body: &kbody shared text
- id: C2
  body: *kbody
"""


def test_cross_row_alias_takes_whole_body_fallback():
    rows = yaml.safe_load(BODY)
    changed = [dict(rows[0]), dict(rows[1], depends_on=["C1"])]
    out = m._patch_body(BODY, rows, changed)
    assert yaml.safe_load(out) == changed
