"""The meta-repo hook installer refuses to run from a live working tree.

Pins the fix for the failure doe-claude-6b reported on 2026-08-26: three of
four pre-commit gates resolved and one did not, because the hook's gate paths
were frozen against a sibling claude-klabauter checkout that had since changed branch.
The gate could not run, and the only remedy the hook offered was
`COORDINATOR_OVERRIDE_PRECOMMIT_*` — a resolution bug that teaches an operator
to switch off a safety gate.

WHY THESE TESTS AND NOT A PATH-EQUALITY ONE: the first cut of this guard
compared the resolver's root against the module's own and was nearly inert,
because `coordinator_engine_root_with_class()` called from a live tree ANSWERS
with that tree — the two paths agree in exactly the case worth refusing. The
class beside the path is the real signal, so these tests drive the class.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops import install_meta_repo_precommit_hook as mod


@pytest.fixture
def resolver(monkeypatch):

    def _set(resolution_class: str, mirror):
        import coordinator_core.engine_root as er

        monkeypatch.setattr(
            er, "coordinator_engine_root_with_class", lambda: ("/irrelevant", resolution_class)
        )
        monkeypatch.setattr(er, "published_engine_mirror_path", lambda: mirror)

    return _set


def test_refuses_from_live_tree_when_a_mirror_is_registered(resolver, tmp_path):
    mirror = tmp_path / "claude-klabauter"
    mirror.mkdir()
    resolver("live-working-tree", str(mirror))

    msg = mod._refuse_if_not_engine_root()

    assert msg is not None
    assert "refusing" in msg
    assert str(mirror.resolve()) in msg
    assert "install-meta-repo-precommit-hook.py" in msg


def test_allows_on_a_single_tree_box(resolver):
    resolver("live-working-tree", None)

    assert mod._refuse_if_not_engine_root() is None


def test_allows_when_running_from_the_published_engine(resolver):
    resolver("resolved-engine", "/anything")

    assert mod._refuse_if_not_engine_root() is None


def test_allows_when_the_mirror_is_this_very_tree(resolver):
    self_root = Path(mod.__file__).resolve().parents[2]
    resolver("live-working-tree", str(self_root))

    assert mod._refuse_if_not_engine_root() is None


def test_resolver_failure_does_not_refuse(monkeypatch):
    import coordinator_core.engine_root as er

    def _boom():
        raise RuntimeError("no root")

    monkeypatch.setattr(er, "coordinator_engine_root_with_class", _boom)

    assert mod._refuse_if_not_engine_root() is None


def test_install_returns_nonzero_and_writes_no_hook_when_refusing(resolver, tmp_path):
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    resolver("live-working-tree", str(mirror))

    repo = tmp_path / "meta"
    (repo / ".git" / "hooks").mkdir(parents=True)

    rc = mod._install_or_append_hook(str(repo), "pre-commit", mod._GATE_REGISTRY)

    assert rc != 0
    assert not (repo / ".git" / "hooks" / "pre-commit").exists()
