"""review.reachability against the three recorded fixture shapes, plus referrer-kind pins."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git.run import run_git
from coordinator_core.ops.review_reachability import _handler
from coordinator_core.ops.tests._reachability_fixture import PLAN_REL, _git, _write, build
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _run(tmp_path, variant, **extra):
    root = build(variant, tmp_path / variant)
    base = run_git(["-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    params = {"repo_root": str(root), "base_sha": base, "worktree": True, "plan": PLAN_REL, **extra}
    return root, base, _handler(params)


def _verdicts(res):
    return {e["ref"]: e["verdict"] for e in res["entries"]}


def test_unwired_grant_permission_unreachable_and_click_path_broken(tmp_path):
    _, _, res = _run(tmp_path, "unwired")
    assert res["error"] is None
    assert _verdicts(res) == {
        "lib/permissions.ts:grantPermission": "unreachable",
        "app/api/permissions/route.ts:POST": "undecidable",
    }
    assert res["click_paths"] == [{"role": "admin", "verdict": "broken", "broken_at": "nav: no Admin entry"}]
    assert res["cost"]["spawns"] == 1


def test_wired_everything_reachable(tmp_path):
    _, _, res = _run(tmp_path, "wired")
    assert _verdicts(res) == {
        "lib/permissions.ts:grantPermission": "reachable",
        "app/api/permissions/route.ts:POST": "reachable",
        "app/admin/permissions/page.tsx": "reachable",
    }
    assert res["click_paths"] == [{"role": "admin", "verdict": "reachable", "broken_at": None}]


def test_undecidable_stack_is_never_unreachable(tmp_path):
    _, _, res = _run(tmp_path, "undecidable")
    assert _verdicts(res) == {"src/permissions.rs:grant_permission": "undecidable"}
    assert res["click_paths"] == [{"role": "admin", "verdict": "undecidable", "broken_at": None}]


def test_test_file_only_referrer_stays_unreachable(tmp_path):
    root = build("unwired", tmp_path / "t")
    base = run_git(["-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    _write(root, "lib/permissions.test.ts", "import { grantPermission } from './permissions';\ngrantPermission('a', 'b');\n")
    _write(root, "tests/perm.ts", "grantPermission();\n")
    res = _handler({"repo_root": str(root), "base_sha": base, "worktree": True, "plan": PLAN_REL})
    assert _verdicts(res)["lib/permissions.ts:grantPermission"] == "unreachable"


def test_same_file_use_does_not_count_and_whole_word_only(tmp_path):
    root = build("unwired", tmp_path / "w")
    base = run_git(["-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    _write(root, "components/Other.tsx", "export const grantPermissionLater = 1;\n")
    res = _handler({"repo_root": str(root), "base_sha": base, "worktree": True, "plan": PLAN_REL})
    assert _verdicts(res)["lib/permissions.ts:grantPermission"] == "unreachable"


def test_barrel_reexport_makes_it_undecidable(tmp_path):
    root = build("unwired", tmp_path / "b")
    base = run_git(["-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    _write(root, "lib/index.ts", "export * from './permissions';\n")
    res = _handler({"repo_root": str(root), "base_sha": base, "worktree": True, "plan": PLAN_REL})
    assert _verdicts(res)["lib/permissions.ts:grantPermission"] == "undecidable"


def test_committed_range_without_worktree(tmp_path):
    root = build("wired", tmp_path / "c")
    base = run_git(["-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "capability")
    head = run_git(["-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    res = _handler({"repo_root": str(root), "base_sha": base, "head_sha": head, "plan": PLAN_REL})
    assert set(_verdicts(res).values()) == {"reachable"}


def test_bad_base_sha_reports_error_in_result_shape(tmp_path):
    root = build("wired", tmp_path / "e")
    res = _handler({"repo_root": str(root), "base_sha": "deadbeef" * 5, "worktree": True})
    assert res["entries"] == [] and res["error"]
    assert set(res) == {"entries", "click_paths", "summary", "cost", "error"}


def test_result_has_only_schema_keys_and_writes_nothing(tmp_path):
    root, _, res = _run(tmp_path, "wired")
    before = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True, **no_console_creationflags()
    ).stdout
    _run_again = _handler({"repo_root": str(root), "base_sha": "HEAD", "worktree": True, "plan": PLAN_REL})
    after = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True, **no_console_creationflags()
    ).stdout
    assert before == after
    assert set(res) == {"entries", "click_paths", "summary", "cost", "error"}
    assert all(set(e) == {"kind", "ref", "verdict"} for e in res["entries"])
    assert all(set(c) == {"role", "verdict", "broken_at"} for c in res["click_paths"])
    assert len(res["summary"]) <= 300


def test_entry_kinds_filter(tmp_path):
    _, _, res = _run(tmp_path, "wired", entry_kinds=["page"])
    assert [e["kind"] for e in res["entries"]] == ["page"]


def test_python_class_is_not_an_entry_point(tmp_path):
    root = build("unwired", tmp_path / "k")
    base = run_git(["-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    _write(root, "lib/census.py", "class Tree:\n    pass\n\n\ndef trees():\n    return [Tree()]\n")
    res = _handler({"repo_root": str(root), "base_sha": base, "worktree": True, "paths": ["lib/census.py"]})
    assert _verdicts(res) == {"lib/census.py:trees": "unreachable"}


def test_function_called_by_a_sibling_in_its_own_file_is_undecidable(tmp_path):
    root = build("unwired", tmp_path / "s")
    base = run_git(["-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    _write(root, "lib/census.py", "def helper():\n    return 1\n\n\ndef trees():\n    return helper()\n")
    res = _handler({"repo_root": str(root), "base_sha": base, "worktree": True, "paths": ["lib/census.py"]})
    v = _verdicts(res)
    assert v["lib/census.py:helper"] == "undecidable"
    assert v["lib/census.py:trees"] == "unreachable"
    assert res["cost"]["spawns"] == 1


def test_diff_file_outside_declared_writes_contributes_no_entry(tmp_path):
    root = build("unwired", tmp_path / "o")
    base = run_git(["-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    _write(root, "lib/other_run.py", "def stranger():\n    return 1\n")
    _git(root, "add", "-A")
    res = _handler({"repo_root": str(root), "base_sha": base, "worktree": True, "plan": PLAN_REL})
    assert "lib/other_run.py:stranger" not in _verdicts(res)
    assert "lib/permissions.ts:grantPermission" in _verdicts(res)


def test_no_plan_and_no_paths_keeps_every_diff_file(tmp_path):
    root = build("unwired", tmp_path / "n")
    base = run_git(["-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    _git(root, "add", "-A")
    res = _handler({"repo_root": str(root), "base_sha": base, "worktree": True})
    assert "lib/permissions.ts:grantPermission" in _verdicts(res)


def test_tracked_declared_write_unchanged_in_range_adds_nothing(tmp_path):
    root = build("unwired", tmp_path / "u")
    _write(root, "lib/old.py", "def legacy():\n    return 1\n")
    _git(root, "add", "lib/old.py")
    _git(root, "commit", "-q", "-m", "old")
    base = run_git(["-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    res = _handler({"repo_root": str(root), "base_sha": base, "worktree": True, "paths": ["lib/old.py"]})
    assert res["entries"] == []


def test_new_unnamed_wiki_doc_is_an_unreachable_doc_entry_by_default(tmp_path):
    root = build("unwired", tmp_path / "d")
    base = run_git(["-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    x = "docs" + "/wiki/x.md"
    _write(root, x, "# x\n")
    params = {"repo_root": str(root), "base_sha": base, "worktree": True, "paths": [x]}
    want = [{"kind": "doc", "ref": x, "verdict": "unreachable"}]
    res = _handler({**params, "entry_kinds": ["doc"]})
    assert res["entries"] == want
    assert res["cost"]["spawns"] == 1
    res = _handler(params)
    assert res["entries"] == want
    assert res["cost"]["spawns"] == 1


_OP_SRC = 'from x import register_op\n\n@register_op("x.y")\ndef _h(params):\n    return {}\n'


def _by_kind(res):
    return {e["ref"].split(":")[0]: (e["kind"], e["verdict"]) for e in res["entries"] if e["kind"] != "exported-function"}


def test_default_kinds_include_an_op_entry_for_a_created_register_op_module(tmp_path):
    root = build("unwired", tmp_path / "p")
    _write(root, "pkg/ops/__init__.py", "")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "registry surface")
    base = run_git(["-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    _write(root, "pkg/ops/m.py", _OP_SRC)
    res = _handler({"repo_root": str(root), "base_sha": base, "worktree": True, "paths": ["pkg/ops/m.py"]})
    assert _by_kind(res) == {"pkg/ops/m.py": ("op", "unreachable")}


def test_created_unreferenced_bin_script_and_hook_body_are_unreachable_by_default(tmp_path):
    root = build("unwired", tmp_path / "h")
    _write(root, "pkg/hooks/__init__.py", "")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "wiring surface")
    base = run_git(["-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    paths = ["bin/tool.py", "pkg/hooks/guard.py"]
    for p in paths:
        _write(root, p, "x = 1\n")
    res = _handler({"repo_root": str(root), "base_sha": base, "worktree": True, "paths": paths})
    assert _by_kind(res) == {
        "bin/tool.py": ("cli", "unreachable"),
        "pkg/hooks/guard.py": ("hook", "unreachable"),
    }


def test_committed_range_marks_a_created_file_and_not_a_modified_one(tmp_path):
    root = build("unwired", tmp_path / "m")
    _write(root, "bin/old.py", "x = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "old script")
    base = run_git(["-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    _write(root, "bin/old.py", "x = 1\ny = 2\n")
    _write(root, "bin/fresh.py", "x = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "range")
    head = run_git(["-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    res = _handler({"repo_root": str(root), "base_sha": base, "head_sha": head, "paths": ["bin/old.py", "bin/fresh.py"]})
    assert _by_kind(res) == {"bin/old.py": ("cli", "undecidable"), "bin/fresh.py": ("cli", "unreachable")}
    assert res["cost"]["spawns"] == 1


def test_removed_dev_null_looking_content_line_does_not_mark_a_file_created(tmp_path):
    root = build("unwired", tmp_path / "r")
    _write(root, "bin/tool.py", "a\n-- /dev/null\nb\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "tool")
    base = run_git(["-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    _write(root, "bin/tool.py", "a\nb\nc\n")
    res = _handler({"repo_root": str(root), "base_sha": base, "worktree": True, "paths": ["bin/tool.py"]})
    assert _by_kind(res) == {"bin/tool.py": ("cli", "undecidable")}


@pytest.mark.parametrize("rel", ["src/proxy.ts", "middleware.ts"])
def test_a_proxy_rewrite_makes_an_unresolved_step_undecidable_not_broken(tmp_path, rel):
    root = build("unwired", tmp_path / "t")
    base = run_git(["-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    _write(root, rel, "export function proxy(req) {\n  return NextResponse.rewrite(new URL('/auth-shell', req.url));\n}\n")
    res = _handler({"repo_root": str(root), "base_sha": base, "worktree": True, "plan": PLAN_REL})
    assert res["click_paths"] == [{"role": "admin", "verdict": "undecidable", "broken_at": None}]


def test_a_proxy_without_a_rewrite_leaves_the_path_broken(tmp_path):
    root = build("unwired", tmp_path / "t")
    base = run_git(["-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    _write(root, "src/proxy.ts", "export function proxy(req) {\n  return NextResponse.next();\n}\n")
    res = _handler({"repo_root": str(root), "base_sha": base, "worktree": True, "plan": PLAN_REL})
    assert res["click_paths"] == [{"role": "admin", "verdict": "broken", "broken_at": "nav: no Admin entry"}]
