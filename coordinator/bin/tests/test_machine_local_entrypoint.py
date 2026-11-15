"""machine-local.py entrypoint: trampoline over templates/bin/_machine_local.py."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_ENTRY = Path(__file__).resolve().parents[1] / "machine-local.py"


@pytest.fixture
def entry(tmp_path, monkeypatch):
    (tmp_path / "registry.toml").write_text('[demo]\nfoo = "bar"\n', encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("machine_local_entry", _ENTRY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_keys_lists_key(entry, capsys):
    assert entry.main(["keys"]) == 0
    assert "demo.foo" in capsys.readouterr().out.split()


def test_get_prints_value(entry, capsys):
    assert entry.main(["get", "demo.foo"]) == 0
    assert capsys.readouterr().out.strip() == "bar"


def test_has_missing_returns_1(entry):
    assert entry.main(["has", "demo.missing"]) == 1


def test_help_returns_0(entry):
    assert entry.main(["--help"]) == 0


def test_argv_restored(entry):
    before = list(sys.argv)
    entry.main(["keys"])
    entry.main(["has", "demo.missing"])
    entry.main(["--help"])
    assert sys.argv == before


def test_no_execv():
    assert "execv" not in _ENTRY.read_text(encoding="utf-8")


def test_kernel_is_loaded_once_per_process(entry):
    entry.main(["keys"])
    first = entry._impl
    entry.main(["has", "demo.foo"])
    assert first is not None and entry._impl is first


def test_read_verb_clears_the_in_process_budget(entry):
    entry.main(["get", "demo.foo"])
    impl = entry._impl
    assert impl._scan_budget_s is None and impl._deadline_at is None


def test_read_verb_arms_the_budget_while_it_runs(entry, monkeypatch):
    seen = {}
    impl = entry._load_impl()
    monkeypatch.setattr(impl, "main", lambda: seen.update(scan=impl._scan_budget_s, at=impl._deadline_at) or 0)
    assert entry.main(["get", "demo.foo"]) == 0
    assert seen["scan"] == impl._SCAN_BUDGET_S and seen["at"] is not None


def test_write_verb_leaves_the_budget_unarmed(entry, monkeypatch):
    seen = {}
    impl = entry._load_impl()
    monkeypatch.setattr(impl, "main", lambda: seen.update(scan=impl._scan_budget_s, at=impl._deadline_at) or 0)
    entry.main(["set", "demo.foo", "x"])
    assert seen == {"scan": None, "at": None}


def test_warm_stalled_scan_is_exit_4_not_unset(entry, tmp_path, monkeypatch, capsys):
    root = tmp_path / "roots"
    for i in range(4):
        c = root / f"copy{i}"
        (c / ".claude-plugin").mkdir(parents=True)
        (c / ".claude-plugin" / "marketplace.json").write_text('{"name": "qux"}')
    (tmp_path / "search-roots.toml").write_text(
        f'{sys.platform} = ["{str(root).replace(chr(92), "/")}"]\n', encoding="utf-8")
    for k in [k for k in os.environ if k.startswith("REPO_")]:
        monkeypatch.delenv(k)
    monkeypatch.setattr(entry._load_impl(), "_SCAN_BUDGET_S", 0.0)
    assert entry.main(["get", "repos.qux", "--default", "fallback"]) == 4
    cap = capsys.readouterr()
    assert cap.err.startswith("machine-local-timeout:") and "fallback" not in cap.out


def test_unset_key_stays_exit_1_warm(entry):
    assert entry.main(["get", "demo.missing"]) == 1


def test_machine_local_is_on_both_allowlist_keys():
    import json

    path = Path(__file__).resolve().parents[3] / "coordinator_core" / "ops" / "warm_entrypoint_allowlist.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "machine-local" in data["entrypoints"]
    assert "machine-local" in data["door_eligible_entrypoints"]


def test_machine_local_passes_the_serve_classifier():
    from coordinator_core.warm.serve_classifier import classify_entrypoint

    verdict = classify_entrypoint("machine-local")
    assert verdict.script_exists and verdict.has_main and verdict.main_arity_ok
    assert not verdict.findings


def test_budget_manifest_carries_the_warm_get_row():
    import json

    path = Path(__file__).resolve().parents[3] / "coordinator_core" / "benchmarks" / "budget-manifest.json"
    row = json.loads(path.read_text(encoding="utf-8"))["overrides"]["invoke.from_argv.machine-local"]
    assert row["target_ms"] == 500 and row["spawn_count_budget"] == 0


def test_entrypoint_read_verbs_match_the_kernel_main_guard(entry):
    import re

    kernel = (_ENTRY.parents[1] / "templates" / "bin" / "_machine_local.py").read_text(encoding="utf-8")
    literal = re.search(r"sys\.argv\[1\] in \{([^}]*)\}", kernel)
    assert literal is not None
    verbs = frozenset(v.strip().strip('"') for v in literal.group(1).split(","))
    assert verbs == entry._READ_VERBS
