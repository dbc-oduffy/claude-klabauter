"""Falsifiers for review reverify judging exactly the run's own post-fix content: (a) the clean mirror carries
the run's untracked non-test module beside its test, (b) a fix leg's later write reaches the mirror, (c) the
warm door classes the reverify list call a read, (d) a zero-run caused by a name defined only in an
uncommitted foreign file halts by that cause, never as test_infra_failure."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt
from coordinator_core.ops.dispatch_emit import review_edits
from coordinator_core.ops.dispatch_emit import run_write_prints
from coordinator_core.warm import client as warm_client
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "runOwnPostFix"


def _node_json(source: str):
    # Local, not imported from a sibling test module: the clean mirror may resolve that module from the
    # installed engine, where an uncommitted helper does not exist yet.
    import shutil

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    proc = subprocess.run([node, "-"], input=source, capture_output=True, text=True, timeout=30, **no_console_creationflags())
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


TEST = "pkg/tests/test_arm.py"
MODULE = "pkg/arm.py"
FIXED = "pkg/fixed.py"
TEST_FIXED = "pkg/tests/test_fixed.py"
FIXED_HEAD = "def fixed():\n    return 'broken'\n"
FIXED_REVIEW = "def fixed():\n    return 'review'\n"
FIXED_FIX_LEG = "def fixed():\n    return 'fixed'\n"
TEST_FIXED_SRC = "from pkg.fixed import fixed\n\n\ndef test_fixed():\n    assert fixed() == 'fixed'\n"
TEST_SRC = "from pkg.arm import arm\n\n\ndef test_arm():\n    assert arm() == 1\n"


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


def _put(root, rel, text):
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


def _repo(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    _put(root, "pytest.ini", "[pytest]\n")
    _put(root, "pkg/__init__.py", "")
    _put(root, "pkg/tests/__init__.py", "")
    _put(root, FIXED, FIXED_HEAD)
    _put(root, TEST_FIXED, TEST_FIXED_SRC)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _record(root, *, declared, footprint=()):
    """The run's record: a manifest naming only `declared`, plus an optional leg footprint."""
    record = cov.measured_rev_record_path(root, RUN)
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"paths": list(declared)}), encoding="utf-8")
    record.with_name("manifest.json").write_text(json.dumps({"review_declared_paths": list(declared)}), encoding="utf-8")
    if footprint:
        record.with_name("leg-footprint.json").write_text(json.dumps({"writes": list(footprint)}), encoding="utf-8")


def _isolate(monkeypatch, sibling=None):
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *_a, **_k: dict(sibling or {}))
    monkeypatch.setattr(cov, "_own_written", lambda *_a, **_k: [])
    monkeypatch.setattr(cov, "_snapshot_scope", lambda *_a, **_k: [])


def _reverify(root, target, capfd):
    code = cov.main(["--export-run", "--run-id", RUN, "--repo-root", str(root), "--",
                     sys.executable, "-m", "pytest", "-p", "no:cacheprovider", target])
    captured = capfd.readouterr()
    return code, captured.out + captured.err


def test_a_mirror_carries_an_untracked_non_test_module_the_run_wrote_and_printed(tmp_path, monkeypatch, capfd):
    root = _repo(tmp_path, monkeypatch)
    _put(root, MODULE, "def arm():\n    return 1\n")
    _put(root, TEST, TEST_SRC)
    _record(root, declared=[TEST])
    run_write_prints.record(root, RUN, [MODULE, TEST])
    _isolate(monkeypatch)

    code, out = _reverify(root, TEST, capfd)

    assert code == 0, out
    assert "1 passed" in out and "co_tenant_dependency" not in out and "ImportError" not in out


def test_a_mirror_still_refuses_an_untracked_module_no_record_names(tmp_path, monkeypatch, capfd):
    root = _repo(tmp_path, monkeypatch)
    _put(root, MODULE, "def arm():\n    return 1\n")
    _put(root, TEST, TEST_SRC)
    _record(root, declared=[TEST])
    _isolate(monkeypatch)

    code, out = _reverify(root, TEST, capfd)

    assert code == 2, out
    assert "1 passed" not in out


def test_b_mirror_carries_the_fix_legs_bytes_not_an_earlier_print_of_a_shared_path(tmp_path, monkeypatch, capfd):
    root = _repo(tmp_path, monkeypatch)
    _put(root, FIXED, FIXED_REVIEW)
    _record(root, declared=[TEST_FIXED], footprint=[FIXED])
    run_write_prints.record(root, RUN, [FIXED])
    _put(root, FIXED, FIXED_FIX_LEG)
    _isolate(monkeypatch, sibling={FIXED: "peerRun"})

    code, out = _reverify(root, TEST_FIXED, capfd)

    assert code == 0, out
    assert "1 passed" in out


def _frame(argv):
    return {"jsonrpc": "2.0", "id": 1, "method": "invoke.from_argv", "params": {"argv": argv}}


def _classed_may_mutate(**params):
    msg = _frame(["review.reverify", json.dumps({"base": "a" * 40, "slice_prefix": "p", **params})])
    return warm_client._op_may_mutate(warm_client._classified_op_name(msg))


def test_c_list_call_is_classed_a_read_by_the_warm_door():
    assert _classed_may_mutate() is False


@pytest.mark.parametrize("params", [{"failing": ["t.py::test_x"]}, {"probe": "open", "failing": ["t.py::test_x"]}])
def test_c_revert_and_probe_calls_stay_mutating(params):
    assert _classed_may_mutate(**params) is True


def _halt(reverify):
    return _node_json(
        f"let _halted = null; const _runId = 'r'; const _sizingRel = null; let _reviewReverify = {json.dumps(reverify)};\n"
        + ask_compose._review_test_infra_halt_js(None)
        + "\nconsole.log(JSON.stringify(_halted));\n"
    )


def test_d_foreign_dependency_zero_run_halts_by_cause_naming_the_source():
    halted = _halt({
        "tests_run": 0, "status_before": "indeterminate", "zero_run_cause": ltt.ZERO_RUN_FOREIGN_DEPENDENCY,
        "import_error": {"symbol": "decode_literal", "module": "pkg.lit", "source_path": "pkg/lit.py"},
    })

    assert halted["halted"] != "test_infra_failure"
    assert "pkg/lit.py" in halted["next_action"]


def test_d_an_unexplained_zero_run_still_halts_loud_as_test_infra_failure():
    halted = _halt({"tests_run": 0, "status_before": "indeterminate", "zero_run_cause": "engine-root-unset"})

    assert halted["halted"] == "test_infra_failure"
