"""The sizing path the scout is told to write and the path the run later names (refire line, stage,
plan legs) are one form: both carry the run suffix."""

from __future__ import annotations

import re

from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _RUN_ID, _compose


def test_scout_write_template_carries_the_run_suffix_the_runtime_path_uses():
    script = _compose()
    suffix = _RUN_ID.rsplit("-", 1)[-1]
    tail = f"-{suffix}.yaml"
    writes = re.findall(r"--write \S*state/sizings/\S*?<name>(\S*?)['\"]?\s", script)
    assert writes, "no --write template found for the scout"
    assert all(w.startswith(tail) for w in writes), writes
    gate_paths = re.findall(r'"sizing_path":"[^"]*<name>([^"]*)"', script)
    assert gate_paths and all(g == tail for g in gate_paths), gate_paths
    js = re.search(r"const _sizingPathFor = [^\n]*", script).group(0)
    assert f"'{tail}'" in js


def test_scout_write_template_and_runtime_path_carry_the_month_shard():
    from datetime import date

    month = date.today().isoformat()[:7]
    script = _compose()
    writes = re.findall(r"--write \S*?(state/sizings/\S*?)<name>", script)
    assert writes and all(w.startswith(f"state/sizings/{month}/") for w in writes), writes
    js = re.search(r"const _sizingPathFor = [^\n]*", script).group(0)
    assert f"'state/sizings/{month}/'" in js

