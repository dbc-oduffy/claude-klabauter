"""eviction_callers: removed definitions and the callers an eviction would strand."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.git import run as git_run
from coordinator_core.git.checkpoint_hunks import uses_new_definition
from coordinator_core.ops.dispatch_emit import eviction_callers
from coordinator_core.ops.dispatch_emit.eviction_callers import (
    UNJUDGEABLE, removed_definitions, stranded_callers,
)

BASE_HEAD = b"def shading_exempt():\n    return 1\n\n\ndef keep():\n    return 2\n"
BASE_RESET = b"def keep():\n    return 2\n"
MAIN_REL = b"from . import base\n\nprint(base.shading_exempt())\n"


REMOVED = {"pkg/base.py": {"shading_exempt"}}
AFTER = {"pkg/base.py": BASE_RESET}


def test_removed_definitions_names_dropped_top_level_defs() -> None:
    assert removed_definitions(BASE_HEAD, BASE_RESET) == {"shading_exempt"}
    assert removed_definitions(BASE_HEAD, BASE_HEAD) == set()
    assert removed_definitions(BASE_HEAD, None) == {"shading_exempt", "keep"}
    assert removed_definitions(b"X = 1\nclass C: pass\n", b"X = 2\n") == {"C"}


def test_empty_removed_spawns_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(git_run, "run_git", boom)
    assert stranded_callers(tmp_path, {"pkg/base.py": set()}, AFTER) == {}
    assert stranded_callers(tmp_path, {"native/x.cpp": {"f"}}, {}) == {}


def test_not_a_repo_is_unjudgeable_never_clean(tmp_path: Path) -> None:
    bare = tmp_path / "nogit"
    bare.mkdir()
    got = stranded_callers(bare, REMOVED, AFTER)
    assert got["pkg/base.py"][0][0] == UNJUDGEABLE


def test_module_aliases_cover_repo_root_and_sys_path_subtree() -> None:
    assert eviction_callers._module_aliases("a/b/c.py", "x/y.py") == ["a.b.c", "b.c"]
    assert eviction_callers._module_aliases("a/b/__init__.py", "x/y.py") == ["a.b"]
    assert eviction_callers._module_aliases("a/b/c.py", "a/b/d.py") == ["a.b.c", "b.c", "c"]


def test_relative_imports_resolve_only_with_a_package() -> None:
    src = "from . import base\nbase.gone()\n"
    assert uses_new_definition(src, "pkg.base", {"gone"}, {}) is None
    assert uses_new_definition(src, "pkg.base", {"gone"}, {}, "pkg") == "uses pkg.base.gone"
    deep = "from ..sub import mod\nmod.gone()\n"
    assert uses_new_definition(deep, "pkg.sub.mod", {"gone"}, {}, "pkg.x") == "uses pkg.sub.mod.gone"
    assert uses_new_definition(deep, "pkg.sub.mod", {"gone"}, {}, "pkg") is None  # climbs past the root
    assert uses_new_definition("from . import base\nbase.gone()\n", "pkg.base", {"other"}, {}, "pkg") is None
