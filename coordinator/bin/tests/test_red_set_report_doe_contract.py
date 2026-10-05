"""Contract tests for the DoE-plane verbs and functions of coordinator/bin/red-set-report.py
(derive_observed_red, derive_known_red_count, resolve_entry, guard_not_nested,
assert_no_self_containment, run_pytest_collect/observe, the observed/count CLI verbs)."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
SCRIPT = BIN / "red-set-report.py"


def _load():
    spec = importlib.util.spec_from_file_location("_doe_contract_red_set_report", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rsr = _load()


@pytest.fixture
def fake_repo(tmp_path):
    (tmp_path / "coordinator" / "bin").mkdir(parents=True)
    shutil.copy(BIN / "tier-last-run.py", tmp_path / "coordinator" / "bin" / "tier-last-run.py")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "test_sample.py").write_text(
        "import pytest\n\n"
        "def test_ok():\n    assert True\n\n"
        "def test_bad():\n    assert False\n\n"
        "@pytest.mark.parametrize('v', ['a b', 'c'])\n"
        "def test_param(v):\n    assert v != 'a b'\n",
        encoding="utf-8",
    )
    (tmp_path / "coordinator.local.md").write_text(
        "---\nceremony_test_cmds:\n"
        "  - name: sample\n    collection_roots: [corpus]\n"
        "  - name: empty\n    collection_roots: [emptydir]\n"
        "  - name: norootdecl\n---\n",
        encoding="utf-8",
    )
    (tmp_path / "emptydir").mkdir()
    return tmp_path


def test_resolve_entry_returns_declared_entry(fake_repo):
    assert rsr.resolve_entry(fake_repo, "sample")["collection_roots"] == ["corpus"]


def test_resolve_entry_unknown_name_raises_value_error(fake_repo):
    with pytest.raises(ValueError, match="unknown ceremony_test_cmds entry"):
        rsr.resolve_entry(fake_repo, "nope")


def test_guard_not_nested_raises_inside_pytest():
    with pytest.raises(RuntimeError, match="PYTEST_CURRENT_TEST"):
        rsr.guard_not_nested()


def test_guard_not_nested_passes_outside_pytest(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    rsr.guard_not_nested()


def test_assert_no_self_containment_refuses_containing_root():
    repo_root = Path(rsr.__file__).resolve().parents[2]
    with pytest.raises(ValueError, match="must not live inside it"):
        rsr.assert_no_self_containment(repo_root, ["coordinator/bin"])
    rsr.assert_no_self_containment(repo_root, ["coordinator_core"])


def test_derive_observed_red_reports_failed_nodeid_set(fake_repo):
    result = rsr.derive_observed_red(fake_repo, "sample")
    assert result["entry"] == "sample"
    assert result["collection_roots"] == ["corpus"]
    assert result["collected_count"] == 4
    assert [n.split("::", 1)[1] for n in result["red_nodeids"]] == [
        "test_bad",
        "test_param[a b]",
    ]


def test_derive_observed_red_empty_corpus_raises(fake_repo):
    with pytest.raises(ValueError, match="zero collected tests"):
        rsr.derive_observed_red(fake_repo, "empty")


def test_derive_observed_red_no_roots_raises(fake_repo):
    with pytest.raises(ValueError, match="declares no collection_roots"):
        rsr.derive_observed_red(fake_repo, "norootdecl")


def test_run_pytest_collect_accepts_both_call_shapes(fake_repo):
    import inspect

    assert inspect.signature(rsr.run_pytest_collect).parameters["marker_expr"].default is None
    assert len(rsr.run_pytest_collect(str(fake_repo / "corpus"), marker_expr=None)) == 4


def test_run_pytest_observe_returns_collected_and_failed(fake_repo):
    collected, failed = rsr.run_pytest_observe(str(fake_repo / "corpus"))
    assert len(collected) == 4 and len(failed) == 2


def test_derive_known_red_count_is_len_of_entries(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "reg.json").write_text(
        json.dumps({"registry_for": "x", "seeded_from": None, "entries": {"a": {}, "b": {}}}),
        encoding="utf-8",
    )
    assert rsr.derive_known_red_count(tmp_path, "reg.json") == {
        "registry": "reg.json",
        "registry_for": "x",
        "known_red_count": 2,
        "seeded_from": None,
        "unit": "unique nodeids (never failure-report events)",
    }


def _cli(*args, cwd, env_extra=None):
    import os

    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd, env=env, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_cli_count_verb(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "reg.json").write_text(json.dumps({"entries": {"a": {}}}), encoding="utf-8")
    proc = _cli("--repo-root", str(tmp_path), "count", "--registry", "reg.json", cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["known_red_count"] == 1


def test_cli_count_missing_registry_exits_1(tmp_path):
    proc = _cli("--repo-root", str(tmp_path), "count", "--registry", "nope.json", cwd=tmp_path)
    assert proc.returncode == 1
    assert "red-set-report count:" in proc.stderr


def test_cli_observed_verb(fake_repo):
    proc = _cli("--repo-root", str(fake_repo), "observed", "--entry", "sample", cwd=fake_repo)
    assert proc.returncode == 0, proc.stderr
    assert len(json.loads(proc.stdout)["red_nodeids"]) == 2


def test_cli_observed_empty_corpus_exits_1(fake_repo):
    proc = _cli("--repo-root", str(fake_repo), "observed", "--entry", "empty", cwd=fake_repo)
    assert proc.returncode == 1
    assert "red-set-report observed:" in proc.stderr


def test_cli_doe_verb_refused_inside_pytest(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="PYTEST_CURRENT_TEST"):
        rsr.main(["count", "--registry", "x.json"])


def test_census_cli_shape_still_parses():
    ns = rsr.build_parser().parse_args(["some/dir", "--workers", "2"])
    assert (ns.target, ns.workers) == ("some/dir", 2)
    assert not rsr._is_doe_invocation(["some/dir", "--workers", "2"])
    assert rsr._is_doe_invocation(["--repo-root", "r", "observed", "--entry", "e"])
