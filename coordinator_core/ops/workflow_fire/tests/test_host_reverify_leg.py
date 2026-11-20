"""The host runs reverify outside the worker's shell deadline."""

import json
import time

from coordinator_core.ops.workflow_fire import headless


def test_run_diff_uses_host_test_receipt_after_worker_timeout(tmp_path, monkeypatch):
    repo = tmp_path / "product"
    repo.mkdir()
    engine = tmp_path / "engine"
    module = engine / "coordinator_core" / "ops" / "test_leg_run.py"
    module.parent.mkdir(parents=True)
    for package in (module.parent, module.parent.parent):
        (package / "__init__.py").touch()
    module.write_text(
        "import json, pathlib, sys, pytest\n"
        "rc = pytest.main(sys.argv[sys.argv.index('--')+1:])\n"
        "assert rc == 0\n"
        "pathlib.Path(sys.argv[sys.argv.index('--out')+1]).write_text("
        "json.dumps(dict(status='pass', status_after='pass', tests_run=1)))\n"
        "pathlib.Path(sys.argv[sys.argv.index('--base-export-out')+1]).write_text("
        "'FAILED test_reviewed.py::test_other\\n{}\\n')\n",
        encoding="utf-8",
    )
    (repo / "test_reviewed.py").write_text(
        "import time\ndef test_long_leg():\n    time.sleep(10.2)\n", encoding="utf-8",
    )
    (repo / "delivery.md").touch()
    monkeypatch.setattr(headless, "_executor_root", lambda: engine)
    monkeypatch.setattr(headless, "_invoke_source", lambda *_args: {
        "edited": ["module.py", "test_reviewed.py"], "created": [],
        "owned": ["test_reviewed.py", "delivery.md"], "tripwires": [],
        "tripwires_not_selected": [], "error": None, "derived_from": "run_diff",
    })
    start = time.monotonic()
    result = headless._reverify_test_leg(
        {"base": "a" * 40, "slice_prefixes": ["prep"], "run_key": "run"},
        str(repo), tmp_path / "operations", 30,
    )
    assert time.monotonic() - start > 10
    assert result["edited"] == ["module.py", "test_reviewed.py"]
    assert result["status_before"] == "pass"
    assert result["tests_run"] == 1
    assert result["not_run"] == ["delivery.md"]
    assert json.loads((repo / result["sidecar_path"]).read_text())["tests_run"] == 1

    assert (repo / "scratch/reverify-base-export-run.txt").read_text().startswith("FAILED ")
