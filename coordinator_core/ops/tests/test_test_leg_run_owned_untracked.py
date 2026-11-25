"""test_leg_run at default flags carries a run's own modified tracked source and its untracked test into the mirror, names foreign uncommitted code a test imports, and puts a collection error's pytest text in the receipt."""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from coordinator_core.ops import test_leg_run as tlr
from coordinator_core.ops.dispatch_emit import abandoned_runs, halted_withheld_index, stranded_sibling
from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "runOwnedUntracked"
PEER_RUN = "runPeerDeclares"
SEED = "pkg/tests/test_seed.py"
OWNED = "pkg/tests/test_owned_untracked.py"
CONFTEST = "pkg/tests/conftest.py"
HELPER = "pkg/helper.py"
MOD = "pkg/mod.py"
SENTINEL = "sentinel-xyz"
NO_DETAIL = "pytest printed no exception or assertion detail"


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


def _put(root, rel, text):
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def run_repo(tmp_path, monkeypatch):
    """(root, base_sha): a committed package whose `pkg/mod.py` holds only VALUE; OWNED, CONFTEST and HELPER
    stay untracked."""
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    _put(root, "pytest.ini", "[pytest]\n")
    _put(root, "pkg/__init__.py", "")
    _put(root, MOD, "VALUE = 1\n")
    _put(root, "pkg/tests/__init__.py", "")
    _put(root, SEED, "def test_seed():\n    assert True\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    monkeypatch.chdir(root)
    return root, _git(root, "rev-parse", "HEAD")


def _declare(root, *paths):
    _put(root, f"state/warp/{RUN}/manifest.json", json.dumps({"doc_writes": list(paths)}))


def _leg(root, base, out, target=OWNED):
    """The receipt of `test_leg_run.main` at default flags (only --run-id and --base-sha) over `target`."""
    tlr.main(["--repo-root", str(root), "--run-id", RUN, "--base-sha", base, "--out", str(out), "--", target])
    return json.loads(out.read_text(encoding="utf-8"))


def _own_source_beside_a_foreign_hunk(root, how):
    """The run's own edit to tracked `pkg/mod.py` (NEW_SYMBOL, recorded the way `how` names) sitting in a worktree
    file that also carries a foreign hunk, with the run's untracked test importing NEW_SYMBOL. The run's manifest
    declares only the test, as the cited run's did."""
    _put(root, MOD, "VALUE = 1\nNEW_SYMBOL = 5\nFOREIGN = 9\n")
    _put(root, OWNED, "from pkg.mod import NEW_SYMBOL\n\n\ndef test_owned():\n    assert NEW_SYMBOL == 5\n")
    _declare(root, OWNED)
    if how == "touched":
        stranded_sibling.write(root / "state" / "warp" / RUN, {MOD: {"base": "a" * 40, "run": "b" * 40}})
    else:
        halted_withheld_index.add_run(root, RUN, [MOD])


def _a_live_peer_declares(root, monkeypatch, path):
    _put(root, f"state/warp/{PEER_RUN}/manifest.json", json.dumps({"review_declared_paths": [path]}))
    monkeypatch.setattr(abandoned_runs, "_token_records", lambda _root: [{"run_id": PEER_RUN}])


@pytest.mark.parametrize("how", ["touched", "withheld"])
def test_a_run_owned_tracked_source_a_live_peer_also_declared_is_overlaid_so_its_untracked_test_collects(
        run_repo, tmp_path, monkeypatch, how):
    root, base = run_repo
    _own_source_beside_a_foreign_hunk(root, how)
    _a_live_peer_declares(root, monkeypatch, MOD)

    receipt = _leg(root, base, tmp_path / "o.json")

    assert MOD in receipt["own_tree"]["overlaid"], receipt
    assert receipt["tests_run"] > 0, receipt
    assert "zero_run_cause" not in receipt, receipt


@pytest.mark.parametrize("how", ["touched", "withheld"])
def test_the_same_source_with_no_peer_declaring_it_is_overlaid_and_its_test_runs(run_repo, tmp_path, how):
    root, base = run_repo
    _own_source_beside_a_foreign_hunk(root, how)

    receipt = _leg(root, base, tmp_path / "o.json")

    assert MOD in receipt["own_tree"]["overlaid"], receipt
    assert receipt["tests_run"] > 0 and "zero_run_cause" not in receipt, receipt


def test_a_symbol_defined_only_in_foreign_uncommitted_code_is_named_not_reported_as_the_runs_failing_test(
        run_repo, tmp_path):
    root, base = run_repo
    _put(root, MOD, "VALUE = 1\nPEER = 2\n")
    _put(root, OWNED, "from pkg.mod import PEER\n\n\ndef test_owned():\n    assert PEER == 2\n")
    _declare(root, OWNED)

    receipt = _leg(root, base, tmp_path / "o.json")

    assert receipt["zero_run_cause"] == ltt.ZERO_RUN_FOREIGN_DEPENDENCY, receipt
    assert receipt["import_error"]["symbol"] == "PEER" and receipt["import_error"]["source_path"] == MOD, receipt
    assert OWNED not in receipt["failing"], receipt


@pytest.mark.parametrize("target", [
    OWNED,
    pytest.param(OWNED.replace("/", "\\"), marks=pytest.mark.skipif(os.sep != "\\", reason="backslash path is Windows-only")),
])
def test_a_collection_error_only_the_errors_section_prints_is_in_the_failing_detail(run_repo, tmp_path, target):
    root, base = run_repo
    _put(root, OWNED, "import pkg.mod as m\n\nX = m.MISSING_SYMBOL\n\n\ndef test_owned():\n    assert X\n")
    _declare(root, OWNED)

    receipt = _leg(root, base, tmp_path / "o.json", target)

    messages = " ".join(d["message"] for d in receipt["failing_detail"])
    assert receipt["zero_run_cause"] == ltt.ZERO_RUN_COLLECTION, receipt
    assert "has no attribute 'MISSING_SYMBOL'" in messages, receipt["failing_detail"]
    assert NO_DETAIL not in messages, receipt["failing_detail"]


def test_an_untracked_owned_test_beside_an_untracked_conftest_and_helper_collects_and_runs(run_repo, tmp_path):
    root, base = run_repo
    _put(root, CONFTEST, "import pytest\n\n\n@pytest.fixture\ndef thing():\n    return 1\n")
    _put(root, HELPER, "VALUE = 1\n")
    _put(root, OWNED, "from pkg.helper import VALUE\n\n\ndef test_owned(thing):\n    assert thing == VALUE\n")
    _declare(root, OWNED, CONFTEST, HELPER)

    receipt = _leg(root, base, tmp_path / "o.json")

    assert receipt["tests_run"] > 0, receipt
    assert "zero_run_cause" not in receipt, receipt
    assert receipt["status"] == "pass", receipt


def test_an_untracked_owned_test_that_raises_at_import_puts_the_exception_text_in_the_receipt(run_repo, tmp_path):
    root, base = run_repo
    _put(root, OWNED, f"raise ImportError({SENTINEL!r})\n\n\ndef test_owned():\n    assert True\n")
    _declare(root, OWNED)

    receipt = _leg(root, base, tmp_path / "o.json")

    assert SENTINEL in json.dumps(receipt), receipt


def test_an_untracked_owned_conftest_that_raises_at_import_puts_the_exception_text_in_the_receipt(run_repo, tmp_path):
    root, base = run_repo
    _put(root, CONFTEST, f"raise ImportError({SENTINEL!r})\n")
    _put(root, OWNED, "def test_owned():\n    assert True\n")
    _declare(root, OWNED, CONFTEST)

    receipt = _leg(root, base, tmp_path / "o.json")

    assert SENTINEL in json.dumps(receipt), receipt


def test_a_summary_line_cut_with_dots_takes_the_traceback_line_instead():
    node = "pkg/tests/test_x.py::test_a"
    text = "\n".join([
        "=== FAILURES ===",
        "___ test_a ___",
        "E   AssertionError: the full assertion line",
        "=== short test summary info ===",
        f"FAILED {node} - AssertionError: the full assert...",
    ])

    assert tlr._detail(text, [node])[node] == "AssertionError: the full assertion line"
