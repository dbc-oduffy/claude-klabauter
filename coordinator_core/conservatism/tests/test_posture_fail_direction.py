"""
Behavioural pin for the fail direction of
`DoE-claude coordinator/hooks/scripts/_posture.py`.

This file exists because that module's docstring declares its direction
load-bearing -- "an unreadable identity file degrades to 'change nothing',
never to 'start blocking'" -- and NOTHING asserted it. The declaration was the
gap; this is the assertion.

Read the vocabulary note in `coordinator_core.conservatism`'s docstring first.
These tests assert the BEHAVIOUR and never the name of the anchor constant
`_posture.py` carries -- a rename on their side (e.g. DoE-claude 4026b4250,
`_FAIL_OPEN_POSTURE` -> `_MOST_CAUTIOUS_POSTURE`) does not need an edit here.

Why it lives in claude-klabauter's tree and not DoE's: this baton's anti-scope forbids
authoring in `DoE-claude`, and the assertion does not need to. It resolves the
sibling root through the existing `coordinator_doe_root` ladder (~39ms, no
spawn on a registered box) and loads the module by path -- the same
import-by-path shape claude-klabauter already uses for `frontmatter/schema_validate.py`.
It SKIPS rather than fails when the sibling is unresolvable, because an
unresolvable peer checkout is an environment fact, not a defect in their code.
`_posture.py` is not ours to edit, so if this pin ever goes red the disposition
is a peer-team ask, not a fix here.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root

_ANCHOR = "precision"


def _load_posture():
    root = coordinator_doe_root()
    if not root:
        pytest.skip("coordinator-claude sibling root unresolvable on this box")
    path = os.path.join(root, "coordinator", "hooks", "scripts", "_posture.py")
    if not os.path.isfile(path):
        pytest.skip(f"_posture.py absent at {path}")
    spec = importlib.util.spec_from_file_location("_doe_posture_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module._cached_posture = None
    module._cached_posture_by_root = {}
    return module


@pytest.fixture()
def posture(monkeypatch, tmp_path):
    module = _load_posture()
    try:
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_HOME", str(home))
        yield module
    finally:
        # Review: code-reviewer Finding 3 -- never popped, a stale module
        # loaded from a sibling repo's file otherwise outlives this test.
        sys.modules.pop("_doe_posture_under_test", None)


def _resolve(module, repo_root):
    module._cached_posture = None
    module._cached_posture_by_root = {}
    return module.resolve_posture(repo_root=str(repo_root))


def test_anchor_constant_is_a_valid_posture(posture):
    """An anchor outside the enum would make every failure path return an
    invalid value -- the pins below would still be green and every caller
    would be wrong. Asserted behaviourally, name-free (Review:
    overengineering-reviewer -- the constant-name axis was a workaround for
    a name coupling this file otherwise disclaims)."""
    assert _ANCHOR in posture._VALID_POSTURES


def test_control_a_valid_declaration_resolves_to_something_else(posture, tmp_path):
    """The control run. Without this, a `resolve_posture` hardcoded to return
    "precision" would pass every assertion in this file."""
    root = tmp_path / "control"
    root.mkdir()
    (root / "coordinator.local.md").write_text(
        "---\nengagement_posture: substrate-free\n---\n", encoding="utf-8"
    )
    assert _resolve(posture, root) == "substrate-free"
    assert _resolve(posture, root) != _ANCHOR


def test_absent_files_return_the_anchor(posture, tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    assert _resolve(posture, root) == _ANCHOR


def test_present_file_without_the_key_returns_the_anchor(posture, tmp_path):
    root = tmp_path / "nokey"
    root.mkdir()
    (root / "coordinator.local.md").write_text("---\ntitle: something else\n---\n", encoding="utf-8")
    assert _resolve(posture, root) == _ANCHOR


def test_value_outside_the_enum_returns_the_anchor(posture, tmp_path):
    root = tmp_path / "bogus"
    root.mkdir()
    (root / "coordinator.local.md").write_text(
        "---\nengagement_posture: maximum-overdrive\n---\n", encoding="utf-8"
    )
    assert _resolve(posture, root) == _ANCHOR


def test_unreadable_file_returns_the_anchor(posture, monkeypatch, tmp_path):
    root = tmp_path / "unreadable"
    root.mkdir()
    (root / "coordinator.local.md").write_text(
        "---\nengagement_posture: substrate-free\n---\n", encoding="utf-8"
    )

    def _boom(path, key):
        raise OSError("permission denied")

    monkeypatch.setattr(posture, "_read_key_from_file", _boom)
    assert _resolve(posture, root) == _ANCHOR


class _ExplodingPathOs:
    """A stand-in for the `os` module that raises from `path.join` and
    delegates everything else. Review: code-reviewer Finding 2 -- the
    previous version monkeypatched `posture.os.path.join` directly, which is
    the process-global `os.path` module every importer shares, not a copy
    scoped to `_posture.py`. Replacing `_posture.py`'s own `os` binding
    (`monkeypatch.setattr(posture, "os", ...)`) confines the breakage to the
    module under test while keeping the test's real intent: an arbitrary
    exception ANYWHERE in the resolution body still returns the anchor. This
    stays distinct from `test_unreadable_file_returns_the_anchor` above,
    which patches `_read_key_from_file` -- the two are different failure
    paths and must not collapse into duplicates."""

    class _ExplodingPath:
        def join(self, *a, **k):
            raise RuntimeError("boom")

        def __getattr__(self, name):
            return getattr(os.path, name)

    def __init__(self):
        self.path = self._ExplodingPath()

    def __getattr__(self, name):
        return getattr(os, name)


def test_arbitrary_exception_in_the_body_returns_the_anchor(posture, monkeypatch, tmp_path):
    """The docstring promises the contract holds for ANY exception in the
    resolution body, not just the two guarded I/O paths."""
    root = tmp_path / "explode"
    root.mkdir()
    monkeypatch.setattr(posture, "os", _ExplodingPathOs())
    assert _resolve(posture, root) == _ANCHOR
