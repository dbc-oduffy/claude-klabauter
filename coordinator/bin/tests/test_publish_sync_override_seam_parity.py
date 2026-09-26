
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parents[1]
_COORDINATOR_LIB = _BIN_DIR.parent / "lib"
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate.publish_sync_contract import (  # noqa: E402
    accepted_keywords as _accepted_keywords,
    would_refuse as _would_refuse,
)


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_override_seam_parity_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

_OVERRIDE_RELATIVE_PATHS = (
    Path("setup") / "publish_sync.py",
    Path("coordinator") / "templates" / "setup" / "publish_sync.py",
)


def _declared_roots() -> "list[Path]":
    roots: "list[Path]" = []
    pointer = publish._read_doe_root_pointer()
    if pointer:
        roots.append(Path(pointer))
    return roots


def _resolvable_overrides() -> "list[Path]":
    return [
        root / relative
        for root in _declared_roots()
        for relative in _OVERRIDE_RELATIVE_PATHS
        if (root / relative).is_file()
    ]


def test_no_resolvable_override_would_refuse_a_round():
    overrides = _resolvable_overrides()
    if not overrides:
        pytest.skip(
            "no percolate root resolves on this box (`.doe-root` unset or absent) — "
            "nothing to compare against; this is a machine fact, not a pass"
        )
    refusing = {
        str(path): reasons
        for path in overrides
        if (reasons := _would_refuse(path))
    }
    assert not refusing, (
        "percolate root(s) carry a publish_sync.py override that claude-klabauter's own "
        f"dispatch contract WOULD REFUSE: {refusing}. The seam changed here and "
        "these copies did not follow. Port the BODIES, not just the parameters — "
        "a signature-only patch satisfies this check while silently not doing "
        f"the work. Engine module: {publish._ENGINE_PUBLISH_SYNC_PATH}."
    )


def test_the_engine_module_satisfies_its_own_contract():
    assert _would_refuse(publish._ENGINE_PUBLISH_SYNC_PATH) == []


def test_a_stale_override_is_caught(tmp_path):
    stale = tmp_path / "publish_sync.py"
    stale.write_text(
        "def sync_mirror(src, dest, *, copy_file=None, renamed_dir_names=None):\n"
        "    pass\n"
        "def sync_flat_mirror(src, dest, *, copy_file=None):\n"
        "    pass\n"
        "def sync_repo_cut(src, dest, *, dry_run=False):\n"
        "    pass\n"
        "def load_ignore(path):\n"
        "    pass\n",
        encoding="utf-8",
    )
    reasons = _would_refuse(stale)
    assert len(reasons) == 1
    assert "renamed_file_names" in reasons[0]
    assert "sweep_top_level_orphans" in reasons[0]


def test_a_bare_wrapper_does_not_pass_as_acceptance(tmp_path):
    wrapper = tmp_path / "publish_sync.py"
    wrapper.write_text(
        "def sync_mirror(*args, **kwargs):\n    pass\n"
        "def sync_flat_mirror(*args, **kwargs):\n    pass\n"
        "def sync_repo_cut(*args, **kwargs):\n    pass\n"
        "def load_ignore(*args, **kwargs):\n    pass\n",
        encoding="utf-8",
    )
    reasons = _would_refuse(wrapper)
    assert len(reasons) == 4
    assert all("bare" in reason for reason in reasons)
