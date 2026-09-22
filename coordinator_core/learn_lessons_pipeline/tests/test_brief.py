"""Tests for coordinator_core.learn_lessons_pipeline.brief.

Spec backlink: docs/plans/2026-09-11-the-lessons-pipeline-drains-without-a-ha.md § C3

Falsifier (plan frontmatter): run A over a fixture tree carrying a COMPLETE
sentinel must emit six directive ids in pipeline order with a computed
`--before` cutoff on `d-age-sweep`; run B over an empty tree must emit five
ids (no `d-age-sweep`) plus a `gates["age_sweep"]` reason. Both cases are
exercised below.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

import pytest

import coordinator_core.learn_lessons_pipeline as pipeline
from coordinator_core.learn_lessons_pipeline import CONSUMES_MANIFEST, brief

_EXPECTED_ORDER = (
    "d-extract-lessons",
    "d-verify-extraction",
    "d-drain-outbox",
    "d-assert-outbox-empty",
    "d-age-sweep",
    "d-stamp-run-complete",
)


def _stamp(runs_dir: Path, run_date: str) -> None:
    run_dir = runs_dir / f"learn-lessons-{run_date}"
    run_dir.mkdir(parents=True)
    (run_dir / "COMPLETE").touch()


def test_stamped_tree_emits_six_directives_in_order(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    (repo_root / "state" / "lessons").mkdir(parents=True)
    claude_home = tmp_path / "fx"
    _stamp(claude_home / ".claude" / "tasks", "2026-09-01")
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

    envelope = brief(repo_root=repo_root, roots=["/some/peer/repo"])

    ids = [d["id"] for d in envelope["directives"]]
    assert ids == list(_EXPECTED_ORDER)

    by_id = {d["id"]: d for d in envelope["directives"]}
    assert by_id["d-extract-lessons"]["depends_on"] is None
    for prev, cur in zip(_EXPECTED_ORDER, _EXPECTED_ORDER[1:]):
        assert prev in by_id[cur]["depends_on"], (prev, cur)

    age_sweep_args = by_id["d-age-sweep"]["args"]
    assert "--before" in age_sweep_args
    assert age_sweep_args[age_sweep_args.index("--before") + 1] == "2026-09-01"
    assert "2026-09-01" not in envelope.get("decisions", {}).values()

    assert envelope.get("gates", {}).get("age_sweep") is None
    assert envelope["judgment_points"] == []


def test_empty_tree_withholds_age_sweep_and_records_gate_reason(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    (repo_root / "state" / "lessons").mkdir(parents=True)
    claude_home = tmp_path / "fx-empty"
    (claude_home / ".claude" / "tasks").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

    envelope = brief(repo_root=repo_root, roots=[])

    ids = [d["id"] for d in envelope["directives"]]
    assert ids == [d for d in _EXPECTED_ORDER if d != "d-age-sweep"]
    assert "d-age-sweep" not in ids

    assert envelope["gates"]["age_sweep"] == "skipped — no completed central run reachable"

    by_id = {d["id"]: d for d in envelope["directives"]}
    assert by_id["d-extract-lessons"]["depends_on"] is None
    assert "--since" not in by_id["d-extract-lessons"]["args"]
    assert "d-assert-outbox-empty" in by_id["d-stamp-run-complete"]["depends_on"]


def test_no_argv_token_is_em_supplied(tmp_path, monkeypatch):
    """No override channel exists: brief() takes no date/cutoff argument."""
    import inspect

    sig = inspect.signature(brief)
    assert "cutoff" not in sig.parameters
    assert "before" not in sig.parameters
    assert "date" not in sig.parameters


def test_cli_directives_are_consumes_manifest_members(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    (repo_root / "state" / "lessons").mkdir(parents=True)
    claude_home = tmp_path / "fx"
    _stamp(claude_home / ".claude" / "tasks", "2026-09-01")
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

    envelope = brief(repo_root=repo_root, roots=[])

    for directive in envelope["directives"]:
        if "cli" in directive:
            assert directive["cli"] in CONSUMES_MANIFEST, directive
        else:
            assert directive["op"] == "stamp-run-complete"
            assert directive["id"] == "d-stamp-run-complete"


def test_no_judgment_point_names_extract_or_verify_directives(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    (repo_root / "state" / "lessons").mkdir(parents=True)
    claude_home = tmp_path / "fx"
    _stamp(claude_home / ".claude" / "tasks", "2026-09-01")
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

    envelope = brief(repo_root=repo_root, roots=[])

    assert envelope["judgment_points"] == []


def test_no_model_call_in_package():
    """AC2 source-level oracle: parse every module in this package with
    `ast` and assert no `import subprocess`, no `subprocess.*` attribute
    access, and no import of a model/LLM client. A text-grep cannot
    distinguish the package's own "makes no model call" docstring
    sentence from an actual call — this walks the parsed AST instead."""
    package_dir = Path(pipeline.__file__).resolve().parent
    forbidden_modules = {"subprocess"}
    forbidden_name_fragments = ("anthropic", "openai", "llm", "model_client")

    for info in pkgutil.walk_packages([str(package_dir)], prefix="coordinator_core.learn_lessons_pipeline."):
        if ".tests" in info.name or info.name.endswith(".tests"):
            continue
        module = importlib.import_module(info.name)
        source_path = Path(module.__file__)
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden_modules, (source_path, alias.name)
                    assert not any(frag in alias.name.lower() for frag in forbidden_name_fragments), (
                        source_path,
                        alias.name,
                    )
            elif isinstance(node, ast.ImportFrom):
                mod_name = node.module or ""
                assert mod_name.split(".")[0] not in forbidden_modules, (source_path, mod_name)
                assert not any(frag in mod_name.lower() for frag in forbidden_name_fragments), (
                    source_path,
                    mod_name,
                )
            elif isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Name) and node.value.id == "subprocess":
                    pytest.fail(f"{source_path}: subprocess.{node.attr} attribute access found")


def test_brief_is_read_only(tmp_path, monkeypatch):
    """brief() must not create the run-stamp directory or any other new
    path on disk — it only reads the COMPLETE-sentinel tree."""
    repo_root = tmp_path / "repo"
    (repo_root / "state" / "lessons").mkdir(parents=True)
    claude_home = tmp_path / "fx"
    tasks_dir = claude_home / ".claude" / "tasks"
    _stamp(tasks_dir, "2026-09-01")
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    brief(repo_root=repo_root, roots=[])
    after = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))

    assert before == after
