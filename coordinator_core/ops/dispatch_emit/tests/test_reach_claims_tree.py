"""judge_reach_claims: verdicts over a real tmp git tree, each unreachable case paired with a wired twin."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import reach_claims_tree as rct
from coordinator_core.ops.dispatch_emit.reach_claims import ReachClaim
from coordinator_core.ops.dispatch_emit.reach_claims_tree import REACH_CLOSURE_DEPTH, judge_reach_claims
from coordinator_core.win_portability import no_console_creationflags


pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, **no_console_creationflags()
    )


def _repo(tmp_path: Path, files: dict[str, str], commit: bool = False) -> Path:
    repo = tmp_path / "repo"
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    repo.mkdir(exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    if commit:
        _git(repo, "commit", "-q", "-m", "base")
    return repo


def _judge(repo: Path, x: str, y: str, **kw):
    (v,) = judge_reach_claims(repo, [ReachClaim(x, y, f"`{x}` reaches `{y}`")], **kw)
    return v


_R40 = {
    "pkg/__init__.py": "",
    "pkg/lib/__init__.py": "",
    "pkg/lib/a.py": "def helper_a(q):\n    return q\n",
    "pkg/lib/hooks.py": "def load_hookimpl(q):\n    return q\n",
    "pkg/tools/__init__.py": "",
    "pkg/tools/trace.py": "from pkg.lib.a import helper_a\n\n\ndef project_trace(q):\n    return helper_a(q)\n",
    "pkg/tools/referencers.py": (
        "from pkg.lib.hooks import load_hookimpl\n\n\ndef project_referencers(q):\n    return load_hookimpl(q)\n"
    ),
}


def test_r40_shape_unreachable_and_wired_twin_reachable(tmp_path):
    repo = _repo(tmp_path, _R40)
    bad = _judge(repo, "project_trace", "load_hookimpl")
    assert bad.verdict == "unreachable", bad.why
    assert _judge(repo, "project_referencers", "load_hookimpl").verdict == "reachable"


def test_wired_twin_through_a_transitive_import(tmp_path):
    files = dict(_R40)
    files["pkg/lib/a.py"] = "from pkg.lib.hooks import load_hookimpl\n\n\ndef helper_a(q):\n    return load_hookimpl(q)\n"
    repo = _repo(tmp_path, files)
    assert _judge(repo, "project_trace", "load_hookimpl").verdict == "reachable"


def _chain(n_hops: int) -> dict[str, str]:
    files = {"c/__init__.py": "", "c/m0.py": "from c import m1\n\n\ndef start_fn():\n    return m1\n"}
    for i in range(1, n_hops):
        files[f"c/m{i}.py"] = f"from c import m{i + 1}\n"
    files[f"c/m{n_hops}.py"] = "def end_fn():\n    return 1\n"
    return files


def test_depth_bound_path_at_the_depth_is_reachable(tmp_path):
    repo = _repo(tmp_path, _chain(REACH_CLOSURE_DEPTH))
    assert _judge(repo, "start_fn", "end_fn").verdict == "reachable"


def test_path_one_hop_deeper_than_the_depth_is_undecidable_never_unreachable(tmp_path):
    repo = _repo(tmp_path, _chain(REACH_CLOSURE_DEPTH + 1))
    v = _judge(repo, "start_fn", "end_fn")
    assert v.verdict == "undecidable", v.why
    assert "deeper" in v.why


def test_undefined_side_is_undecidable(tmp_path):
    repo = _repo(tmp_path, _R40)
    assert _judge(repo, "no_such_tool", "load_hookimpl").verdict == "undecidable"
    assert _judge(repo, "project_trace", "no_such_target").verdict == "undecidable"


def test_symbol_only_in_a_test_file_is_undecidable(tmp_path):
    files = dict(_R40)
    files["pkg/tests/test_x.py"] = "def only_in_tests():\n    return 1\n"
    repo = _repo(tmp_path, files)
    assert _judge(repo, "project_trace", "only_in_tests").verdict == "undecidable"


@pytest.mark.parametrize("marker", ["getattr(m, name)", "importlib.import_module(n)", "__import__(n)"])
def test_dynamic_dispatch_marker_in_the_closure_is_undecidable(tmp_path, marker):
    files = dict(_R40)
    files["pkg/lib/a.py"] = f"def helper_a(q):\n    return {marker}\n"
    repo = _repo(tmp_path, files)
    v = _judge(repo, "project_trace", "load_hookimpl")
    assert v.verdict == "undecidable", v.why
    assert "dynamic" in v.why


def test_unresolved_in_repo_import_is_undecidable(tmp_path):
    files = dict(_R40)
    files["pkg/lib/a.py"] = "from pkg.lib.gone import thing\n\n\ndef helper_a(q):\n    return thing\n"
    repo = _repo(tmp_path, files)
    v = _judge(repo, "project_trace", "load_hookimpl")
    assert v.verdict == "undecidable", v.why
    assert "unresolved" in v.why


def test_external_import_does_not_block_unreachable(tmp_path):
    files = dict(_R40)
    files["pkg/lib/a.py"] = "import os\nfrom collections import OrderedDict\n\n\ndef helper_a(q):\n    return os, OrderedDict\n"
    repo = _repo(tmp_path, files)
    assert _judge(repo, "project_trace", "load_hookimpl").verdict == "unreachable"


def test_relative_imports_resolve(tmp_path):
    files = dict(_R40)
    files["pkg/tools/trace.py"] = "from ..lib.hooks import load_hookimpl\n\n\ndef project_trace(q):\n    return load_hookimpl(q)\n"
    repo = _repo(tmp_path, files)
    assert _judge(repo, "project_trace", "load_hookimpl").verdict == "reachable"
    files["pkg/tools/trace.py"] = "from ..lib.a import helper_a\n\n\ndef project_trace(q):\n    return helper_a(q)\n"
    repo2 = _repo(tmp_path / "two", files)
    assert _judge(repo2, "project_trace", "load_hookimpl").verdict == "unreachable"


def test_string_literal_registration_defines_a_symbol(tmp_path):
    files = dict(_R40)
    files["pkg/tools/trace.py"] = (
        "from pkg.lib.a import helper_a\n\n\n@register_op(\"project.trace\")\ndef _h(q):\n    return helper_a(q)\n"
    )
    repo = _repo(tmp_path, files)
    assert _judge(repo, "project.trace", "load_hookimpl").verdict == "unreachable"


def test_file_symbol_and_file_forms(tmp_path):
    repo = _repo(tmp_path, _R40)
    assert _judge(repo, "pkg/tools/trace.py:project_trace", "load_hookimpl").verdict == "unreachable"
    assert _judge(repo, "pkg/tools/referencers.py:project_referencers", "load_hookimpl").verdict == "reachable"
    assert _judge(repo, "pkg/tools/trace.py", "pkg/lib/a.py").verdict == "reachable"
    assert _judge(repo, "pkg/tools/trace.py", "pkg/lib/hooks.py").verdict == "unreachable"


def test_typescript_relative_imports(tmp_path):
    files = {
        "web/a.ts": "import { wired } from './b';\nexport function startTs() { return wired(); }\n",
        "web/b.ts": "export function wired() { return endTs(); }\nexport function endTs() { return 1; }\n",
        "web/c.ts": "export function island() { return 2; }\n",
    }
    repo = _repo(tmp_path, files)
    assert _judge(repo, "startTs", "endTs").verdict == "reachable"
    assert _judge(repo, "island", "endTs").verdict == "unreachable"


def test_x_defined_only_in_an_unindexed_landing_path(tmp_path):
    files = dict(_R40)
    repo = _repo(tmp_path, files)
    new = repo / "pkg/tools/fresh.py"
    new.write_text("from pkg.lib.hooks import load_hookimpl\n\n\ndef fresh_tool():\n    return load_hookimpl(1)\n", encoding="utf-8")
    assert _judge(repo, "fresh_tool", "load_hookimpl").verdict == "undecidable"
    assert _judge(repo, "fresh_tool", "load_hookimpl", landing_paths=["pkg/tools/fresh.py"]).verdict == "reachable"
    new.write_text("def fresh_tool():\n    return 1\n", encoding="utf-8")
    assert _judge(repo, "fresh_tool", "load_hookimpl", landing_paths=["pkg/tools/fresh.py"]).verdict == "unreachable"


def test_reach_only_through_a_withheld_worktree_edit_is_not_reachable(tmp_path):
    repo = _repo(tmp_path, _R40, commit=True)
    (repo / "pkg/lib/a.py").write_text(
        "from pkg.lib.hooks import load_hookimpl\n\n\ndef helper_a(q):\n    return load_hookimpl(q)\n", encoding="utf-8"
    )
    withheld = _judge(repo, "project_trace", "load_hookimpl", withheld_paths=["pkg/lib/a.py"])
    assert withheld.verdict == "unreachable", withheld.why
    landing = _judge(repo, "project_trace", "load_hookimpl", landing_paths=["pkg/lib/a.py"])
    assert landing.verdict == "reachable"


def test_withheld_file_untracked_at_head_reads_absent(tmp_path):
    repo = _repo(tmp_path, _R40, commit=True)
    (repo / "pkg/tools/fresh.py").write_text("def fresh_tool():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "pkg/tools/fresh.py")
    v = _judge(repo, "fresh_tool", "load_hookimpl", withheld_paths=["pkg/tools/fresh.py"])
    assert v.verdict == "undecidable"


def test_deleted_path_is_never_the_reason_for_reachable(tmp_path):
    files = dict(_R40)
    files["pkg/lib/a.py"] = "from pkg.lib.hooks import load_hookimpl\n\n\ndef helper_a(q):\n    return load_hookimpl(q)\n"
    repo = _repo(tmp_path, files)
    assert _judge(repo, "project_trace", "load_hookimpl").verdict == "reachable"
    v = _judge(repo, "project_trace", "load_hookimpl", deleted_paths=["pkg/lib/a.py"])
    assert v.verdict != "reachable"


def test_no_claims_and_unreadable_index(tmp_path):
    assert judge_reach_claims(tmp_path, []) == ()
    plain = tmp_path / "plain"
    plain.mkdir()
    v = _judge(plain, "a", "b")
    assert v.verdict == "undecidable"


def test_scan_makes_at_most_one_process_spawn(tmp_path, monkeypatch):
    repo = _repo(tmp_path, _R40, commit=True)
    spawns = []
    real_popen = subprocess.Popen

    class _Counting(real_popen):
        def __init__(self, *a, **k):
            spawns.append(a)
            super().__init__(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", _Counting)
    assert _judge(repo, "project_trace", "load_hookimpl", withheld_paths=["pkg/lib/a.py"]).verdict == "unreachable"
    assert len(spawns) == 1


def test_judge_reads_only_landing_withheld_hit_and_closure_files(tmp_path, monkeypatch):
    files = dict(_R40)
    for i in range(30):
        files[f"decoy/d{i}.py"] = f"def decoy_{i}():\n    return {i}\n"
    files["decoy/mentions.py"] = "# load_hookimpl is only mentioned, never defined\nx = 1\n"
    repo = _repo(tmp_path, files, commit=True)
    (repo / "pkg/tools/fresh.py").write_text("def fresh_tool():\n    return 1\n", encoding="utf-8")
    reads: list[str] = []
    real = Path.read_bytes

    def _spy(self):
        reads.append(self.relative_to(repo).as_posix())
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", _spy)
    v = _judge(repo, "fresh_tool", "load_hookimpl", landing_paths=["pkg/tools/fresh.py"])
    assert v.verdict == "unreachable", v.why
    reads = [r for r in reads if not r.startswith(".git/")]
    assert set(reads) <= {"pkg/tools/fresh.py"}, reads


def test_common_name_definitions_come_from_the_grep_not_file_reads(tmp_path, monkeypatch):
    files = dict(_R40)
    for i in range(30):
        files[f"decoy/d{i}.py"] = f"def main():\n    return run({i})\n\n\ndef run(x):\n    return x\n"
    files["decoy/mention.py"] = "# run is called, main is mentioned\nrun(1)\nx = main\n"
    repo = _repo(tmp_path, files, commit=True)
    reads: list[str] = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: (reads.append(self.relative_to(repo).as_posix()), real(self))[1])
    (v,) = judge_reach_claims(repo, [ReachClaim("main", "run", "`main` reaches `run`")])
    assert v.verdict == "reachable", v.why
    decoy_reads = sorted({r for r in reads if r.startswith("decoy/")})
    assert decoy_reads == ["decoy/d0.py"], decoy_reads  # d0 sorts first among main's definers: the closure walk starts there


def test_definition_shapes_are_found_by_the_grep_and_non_definitions_are_not(tmp_path):
    files = {
        "a/one.py": "def alpha_fn():\n    return 1\n",
        "a/two.ts": "export const beta_fn = () => 1;\n",
        "a/three.py": "x = alpha_fn()\n# beta_fn mentioned\n",
        "a/four.py": "@register_op(\"gamma.op\")\ndef _h():\n    return 1\n",
    }
    repo = _repo(tmp_path, files, commit=True)
    hits = rct._index_def_hits(repo, ["alpha_fn", "beta_fn", "gamma.op"])
    assert hits == {"a/one.py": {"alpha_fn"}, "a/two.ts": {"beta_fn"}, "a/four.py": {"gamma.op"}}


def test_landing_file_with_a_token_but_no_definition_defines_nothing(tmp_path):
    repo = _repo(tmp_path, _R40, commit=True)
    (repo / "pkg/tools/fresh.py").write_text("x = fresh_tool\n", encoding="utf-8")
    v = _judge(repo, "fresh_tool", "load_hookimpl", landing_paths=["pkg/tools/fresh.py"])
    assert v.verdict == "undecidable"


def test_git_grep_failure_is_undecidable_not_a_crash(tmp_path, monkeypatch):
    repo = _repo(tmp_path, _R40, commit=True)
    monkeypatch.setattr(rct, "_index_def_hits", lambda root, syms: None)
    v = _judge(repo, "project_trace", "load_hookimpl")
    assert v.verdict == "undecidable"
    assert "git grep" in v.why
