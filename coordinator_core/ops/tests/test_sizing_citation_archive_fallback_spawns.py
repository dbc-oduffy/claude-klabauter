from __future__ import annotations


import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_CITED = "state/sizings/2026-08-01-example.yaml"
_SIZING_BODY = "schema: sizing-object\nestimate:\n  tshirt: M\n"


def test_import_defers_archive_sizings_until_the_archive_probe() -> None:
    import subprocess
    import sys

    code = (
        "import sys, tempfile, pathlib\n"
        "import coordinator_core.ops._sizing_citation as m\n"
        "assert 'coordinator_core.ops.fleet.archive_sizings' not in sys.modules\n"
        "root = pathlib.Path(tempfile.mkdtemp())\n"
        "(root / 'archive' / 'sizings').mkdir(parents=True)\n"
        "assert m.resolve_sizing_citation(root, 'state/sizings/2026-08-01-x.yaml') is None\n"
        "assert 'coordinator_core.ops.fleet.archive_sizings' in sys.modules\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
