"""review_floor: the code subset, verdict count, receipt text and floor reason."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from coordinator_core import coverage
from coordinator_core.hooks import guard_terminal_review as hook
from coordinator_core.ops.dispatch_emit import review_floor as rf


def test_code_paths_keeps_only_code_in_input_order():
    assert rf.code_paths(["coordinator_core/x.py", "state/a.yaml", "docs/wiki/a.md"]) == ["coordinator_core/x.py"]


def test_code_paths_dedupes_and_strips_trailing_slash():
    assert rf.code_paths(["pkg/", "pkg", "b.py", "pkg/"]) == ["pkg", "b.py"]


def test_bookkeeping_directory_prefixes_are_not_code():
    assert rf.code_paths(["state/", "docs/plans/", "state/roadmap/"]) == ["state/roadmap"]


def test_docs_and_state_only_list_has_no_code():
    assert rf.code_paths(["docs/plans/p.md", "state/q/x.jsonl", ".coordinator-local/a.py", "README.md"]) == []


@pytest.mark.parametrize("path,expected", [
    ("state/roadmap/x.md", False),
    ("pyproject.toml", True),
    ("docs/plans/p.py", False),
    ("coordinator_core/a.py", True),
    ("state/x.py", False),
])
def test_lifted_rule_answers_are_unchanged(path, expected):
    assert rf.is_code_path(path) is expected
    assert coverage._is_code_path(path) is expected


def test_hook_shares_the_classifier_and_keeps_no_copy():
    src = Path(hook.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_is_code_path"]
    assert hook._is_code_path is coverage._is_code_path
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("coordinator_core.ops")


@pytest.mark.parametrize("path", [
    ".github/workflows/ci.yml", "plugins/x/hooks/hooks.yaml", "commands/foo.md", "skills/bar/SKILL.md",
    "docs/reference/hooks.yaml",
])
def test_carve_in_counts_executable_config_as_code(path):
    assert rf.is_code_path(path) is True
    assert rf.code_paths([path]) == [path]


@pytest.mark.parametrize("path", ["docs/wiki/x.md", "state/x.yaml", "docs/plans/skills/x.md", "docs/wiki/skills/x.md", "docs/reference/commands/x.md", "README.md"])
def test_carve_in_leaves_docs_and_bookkeeping_non_code(path):
    assert rf.is_code_path(path) is False


@pytest.mark.parametrize("path", [
    ".github/workflows/ci.yml", "plugins/x/hooks/hooks.yaml", "commands/foo.md", "skills/bar/SKILL.md",
])
def test_base_classifier_still_calls_carve_in_paths_doc_only(path):
    assert coverage._is_code_path(path) is False


def _sidecar(tmp_path, name="v.md"):
    p = tmp_path / name
    p.write_text("verdict", encoding="utf-8")
    return name


def test_verdict_count_requires_a_sidecar_file(tmp_path):
    ok = _sidecar(tmp_path)
    review = {"wave_reviews": [
        {"verdict": "APPROVE", "sidecar_path": ok},
        {"verdict": "APPROVE"},
        {"verdict": "APPROVE", "sidecar_path": "missing.md"},
        {"verdict": "APPROVE", "sidecar_path": ""},
        {"verdict": "", "sidecar_path": ok},
        {"verdict": None, "sidecar_path": ok},
        {"verdict": "  ", "sidecar_path": ok},
        {"sidecar_path": ok},
        "x",
    ], "delivery": {"verdict": "ok"}}
    assert rf.verdict_count(review, tmp_path) == 1


def test_verdict_count_bare_verdict_is_zero(tmp_path):
    assert rf.verdict_count({"wave_reviews": [{"verdict": "x"}]}, tmp_path) == 0


def test_verdict_count_accepts_absolute_sidecar(tmp_path):
    _sidecar(tmp_path)
    review = {"wave_reviews": [{"verdict": "APPROVE", "sidecar_path": str(tmp_path / "v.md")}]}
    assert rf.verdict_count(review, tmp_path / "elsewhere") == 1


@pytest.mark.parametrize("bad", [None, {}, {"wave_reviews": None}, {"wave_reviews": "APPROVE"}, []])
def test_verdict_count_is_zero_for_a_shapeless_review(bad, tmp_path):
    assert rf.verdict_count(bad, tmp_path) == 0


class _Chunk:
    def __init__(self, paths):
        self.paths, self.prefixes = paths, ()


class _Req:
    chunks = [_Chunk(["coordinator_core/x.py"])]


@pytest.mark.parametrize("review,from_disk,expected", [
    ({"prep": {"product_files": 0}}, True, []),
    ({"prep": {"product_files": 0}}, False, ["coordinator_core/x.py"]),
    ({"prep": {"product_files": None}}, True, ["coordinator_core/x.py"]),
    ({"prep": {"product_files": 2}}, True, ["coordinator_core/x.py"]),
    ({"prep": {"product_files": False}}, True, ["coordinator_core/x.py"]),
    ({}, True, ["coordinator_core/x.py"]),
    (None, True, ["coordinator_core/x.py"]),
])
def test_floor_code_paths_exempts_only_disk_confirmed_zero_product_files(review, from_disk, expected):
    assert rf.floor_code_paths(_Req(), review, from_disk) == expected


def test_receipt_text_is_literal():
    assert rf.receipt(0) == "review: 0 verdicts"
    assert rf.receipt(1) == "review: 1 verdicts"


def test_floor_reason_fires_only_for_code_with_no_verdict():
    code = [f"p{i}.py" for i in range(7)]
    reason = rf.floor_reason(code, 0)
    assert reason.startswith("review: 0 verdicts on a code-editing run (")
    assert "p4.py" in reason and "p5.py" not in reason
    assert rf.floor_reason(code, 1) is None
    assert rf.floor_reason([], 0) is None


def test_constants():
    assert (rf.REFUSED_REVIEW_FLOOR, rf.HALT_REVIEW_FLOOR, rf.MANIFEST_KEY) == (
        "review-floor", "review_floor", "review_code_paths")
