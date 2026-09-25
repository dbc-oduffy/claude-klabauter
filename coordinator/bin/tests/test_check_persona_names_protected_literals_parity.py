"""Pin: the mirror identity gate's PROTECTED_LITERALS list agrees with the store.

The checker (`dist/mirror-native/claude-klabauter/.github/scripts/check-persona-names.py`)
cannot import `setup/percolate-hooks/percolate-store.yaml` directly -- it must run
standalone against a published mirror with no `claude-klabauter` sibling guaranteed
present -- so its `PROTECTED_LITERALS` tuple is a hand-carried copy of the store's
`base.protected_literals`. This test is the parity mechanism that stops the two from
silently drifting apart (the same role `_repo.py`'s `SKIP_DIR_NAMES` docstring names
for its own hand-agreeing list).
"""
from __future__ import annotations

import importlib.util
import pathlib

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_CHECKER_PATH = (
    _REPO_ROOT / "dist" / "mirror-native" / "claude-klabauter" / ".github" / "scripts"
    / "check-persona-names.py"
)
_STORE_PATH = _REPO_ROOT / "setup" / "percolate-hooks" / "percolate-store.yaml"


def _load_checker_module():
    spec = importlib.util.spec_from_file_location("check_persona_names", _CHECKER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_protected_literals_match_the_store():
    checker = _load_checker_module()
    store = yaml.safe_load(_STORE_PATH.read_text(encoding="utf-8"))
    store_literals = store["base"]["protected_literals"]
    assert list(checker.PROTECTED_LITERALS) == list(store_literals), (
        "check-persona-names.py's PROTECTED_LITERALS has drifted from "
        "setup/percolate-hooks/percolate-store.yaml base.protected_literals -- "
        "update the hand-carried copy in the checker to match."
    )
