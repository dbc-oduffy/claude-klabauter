"""Hook body, bin script and op module verdicts of review.reachability, driven through the handler."""

from __future__ import annotations

import pytest

from coordinator_core.git.run import run_git
from coordinator_core.ops import reachability_registration as rr
from coordinator_core.ops.review_reachability import _handler
from coordinator_core.ops.tests._reachability_fixture import _git, _write

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

HOOK = "pkg/hooks/foo.py"
OP_SRC = 'from x import register_op\n\n@register_op("x.y")\ndef _h(params):\n    return {}\n'


def _repo(tmp_path, base: dict[str, str], new: dict[str, str]):
    root = tmp_path / "r"
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    for rel, text in {"README.md": "# r\n", **base}.items():
        _write(root, rel, text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    sha = run_git(["-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    for rel, text in new.items():
        _write(root, rel, text)
    return root, sha


def _run(tmp_path, base, new, paths=None, kinds=("cli", "hook", "op")):
    root, sha = _repo(tmp_path, base, new)
    res = _handler({
        "repo_root": str(root), "base_sha": sha, "worktree": True,
        "paths": list(paths or new), "entry_kinds": list(kinds),
    })
    assert res["error"] is None
    assert res["cost"]["spawns"] == 1
    return {
        e["ref"].split(":")[0] if e["kind"] in ("cli", "hook") else e["ref"]: (e["kind"], e["verdict"])
        for e in res["entries"]
    }


def test_hook_in_a_repo_with_no_wiring_surface_is_undecidable(tmp_path):
    got = _run(tmp_path, {}, {HOOK: "x = 1\n"})
    assert got == {HOOK: ("hook", "undecidable")}


def test_hook_named_by_the_wiring_surface_is_reachable(tmp_path):
    base = {"pkg/hooks/__init__.py": '_EAGER = ("pkg.hooks.bar",)\n'}
    wired = {**base, "pkg/hooks/__init__.py": '_EAGER = ("pkg.hooks.bar", "pkg.hooks.foo")\n'}
    assert _run(tmp_path, base, {HOOK: "x = 1\n", "pkg/hooks/__init__.py": wired["pkg/hooks/__init__.py"]},
                paths=[HOOK]) == {HOOK: ("hook", "reachable")}


def test_created_hook_missing_from_the_wiring_surface_is_unreachable(tmp_path):
    base = {"pkg/hooks/__init__.py": '_EAGER = ("pkg.hooks.bar",)\n'}
    got = _run(tmp_path, base, {HOOK: "x = 1\n"})
    assert got == {HOOK: ("hook", "unreachable")}


def _verdict(tmp_path, base, new, found, *, created=()):
    root, _ = _repo(tmp_path, base, new)
    index = sorted({*base, *new, "README.md"})
    return rr.registration_verdicts(root, found, {}, index, new=frozenset(created))


def test_created_unreferenced_hook_is_unreachable_but_a_modified_one_is_undecidable(tmp_path):
    base = {"pkg/hooks/__init__.py": '_EAGER = ("pkg.hooks.bar",)\n'}
    key = ("hook", "foo", HOOK)
    got = _verdict(tmp_path, base, {HOOK: "x = 1\n"}, [key], created=[HOOK])
    assert got == {key: "unreachable"}
    got = _verdict(tmp_path / "m", base, {HOOK: "x = 1\n"}, [key])
    assert got == {key: "undecidable"}
    wired = {HOOK: "x = 1\n", "pkg/hooks/__init__.py": '_EAGER = ("pkg.hooks.bar", "pkg.hooks.foo")\n'}
    assert _verdict(tmp_path / "w", base, wired, [key], created=[HOOK]) == {key: "reachable"}


def test_created_hook_with_no_wiring_surface_stays_undecidable(tmp_path):
    key = ("hook", "foo", HOOK)
    assert _verdict(tmp_path, {}, {HOOK: "x = 1\n"}, [key], created=[HOOK]) == {key: "undecidable"}


def test_created_private_hook_stem_stays_undecidable(tmp_path):
    base = {"pkg/hooks/__init__.py": ""}
    rel = "pkg/hooks/_helper.py"
    key = ("hook", "_helper", rel)
    assert _verdict(tmp_path, base, {rel: "x = 1\n"}, [key], created=[rel]) == {key: "undecidable"}


def test_created_unreferenced_bin_script_is_unreachable_but_a_modified_one_is_undecidable(tmp_path):
    rel = "bin/do-thing.mjs"
    key = ("cli", "do-thing", rel)
    assert _verdict(tmp_path, {}, {rel: "x\n"}, [key], created=[rel]) == {key: "unreachable"}
    assert _verdict(tmp_path / "m", {}, {rel: "x\n"}, [key]) == {key: "undecidable"}


def test_created_bin_script_named_in_an_ancestor_readme_is_reachable(tmp_path):
    rel = "bin/do-thing.sh"
    key = ("cli", "do-thing", rel)
    base = {"bin/README.md": "scripts: do-thing\n"}
    assert _verdict(tmp_path, base, {rel: "echo\n"}, [key], created=[rel]) == {key: "reachable"}


def test_conftest_is_never_a_hook_or_bin_entry():
    assert rr.registration_entries("pkg/hooks/conftest.py", "x = 1\n") == []
    assert rr.registration_entries("bin/conftest.py", "x = 1\n") == []


def test_hook_named_in_an_ancestor_hooks_json_is_reachable(tmp_path):
    base = {"hooks.json": "{}\n"}
    got = _run(tmp_path, base, {"hooks.json": '{"cmd": "hooks/foo.py"}\n', "hooks/foo.py": "x = 1\n"},
               paths=["hooks/foo.py"])
    assert got == {"hooks/foo.py": ("hook", "reachable")}


def test_created_bin_script_without_a_reference_is_unreachable_and_a_shim_never_wires_it(tmp_path):
    got = _run(tmp_path, {}, {"bin/do-thing.mjs": "console.log(1)\n", "bin/do-thing.cmd": "node do-thing.mjs\n"})
    assert got == {"bin/do-thing.mjs": ("cli", "unreachable")}


def test_bin_script_invoked_from_an_added_markdown_line_is_reachable(tmp_path):
    base = {"skills/s.md": "# s\n"}
    got = _run(tmp_path, base, {"bin/do-thing.mjs": "x\n", "skills/s.md": "# s\nRun `do-thing --now`.\n"},
               paths=["bin/do-thing.mjs"])
    assert got == {"bin/do-thing.mjs": ("cli", "reachable")}


def test_bin_script_named_by_an_ancestor_index_is_reachable(tmp_path):
    base = {"bin/README.md": "scripts: do-thing\n"}
    assert _run(tmp_path, base, {"bin/do-thing.sh": "echo\n"}) == {"bin/do-thing.sh": ("cli", "reachable")}


def test_test_file_is_never_a_referrer(tmp_path):
    new = {"bin/do-thing.mjs": "x\n", "tests/test_it.py": "run('do-thing')\n"}
    assert _run(tmp_path, {}, new, paths=["bin/do-thing.mjs"]) == {"bin/do-thing.mjs": ("cli", "unreachable")}
    op_new = {"pkg/ops/m.py": OP_SRC, "tests/test_m.py": "assert 'x.y'\n"}
    base = {"pkg/ops/__init__.py": "", "pkg/ops/_registry_map.py": "MAP = {}\n"}
    got = _run(tmp_path / "o", base, op_new, paths=["pkg/ops/m.py"])
    assert got["pkg/ops/m.py:x.y"] == ("op", "unreachable")


def test_op_absent_from_every_registry_surface_is_unreachable(tmp_path):
    base = {"pkg/ops/__init__.py": "", "pkg/ops/_registry_map.py": "MAP = {}\n"}
    got = _run(tmp_path, base, {"pkg/ops/m.py": OP_SRC}, kinds=("op",))
    assert got == {"pkg/ops/m.py:x.y": ("op", "unreachable")}


def test_op_listed_in_the_registry_map_is_reachable(tmp_path):
    base = {"pkg/ops/__init__.py": "", "pkg/ops/_registry_map.py": "MAP = {}\n"}
    new = {"pkg/ops/m.py": OP_SRC, "pkg/ops/_registry_map.py": 'MAP = {"x.y": "pkg.ops.m"}\n'}
    got = _run(tmp_path, base, new, paths=["pkg/ops/m.py"], kinds=("op",))
    assert got == {"pkg/ops/m.py:x.y": ("op", "reachable")}


def test_op_in_a_repo_with_no_registry_surface_is_undecidable(tmp_path):
    got = _run(tmp_path, {}, {"pkg/ops/m.py": OP_SRC}, kinds=("op",))
    assert got == {"pkg/ops/m.py:x.y": ("op", "undecidable")}


def test_op_kind_is_on_by_default(tmp_path):
    base = {"pkg/ops/__init__.py": "", "pkg/ops/_registry_map.py": "MAP = {}\n"}
    root, sha = _repo(tmp_path, base, {"pkg/ops/m.py": OP_SRC})
    res = _handler({"repo_root": str(root), "base_sha": sha, "worktree": True, "paths": ["pkg/ops/m.py"]})
    assert [(e["kind"], e["verdict"]) for e in res["entries"]] == [("op", "unreachable")]


def test_register_op_inside_a_docstring_is_not_an_op():
    assert rr.registration_entries("pkg/ops/m.py", 'doc: use @register_op("a.b") to register\n') == []
    assert rr.registration_entries("pkg/ops/m.py", '    @register_op("a.b")\n') == [("op", "a.b")]


def test_private_hook_stem_is_undecidable(tmp_path):
    base = {"pkg/hooks/__init__.py": ""}
    got = _run(tmp_path, base, {"pkg/hooks/_helper.py": "x = 1\n"})
    assert got == {"pkg/hooks/_helper.py": ("hook", "undecidable")}
