"""
test_list_receivers_reports_unaddressable.py — klabauter#40 (second fix).

Purpose: `cross-repo-memo --list-receivers` must say when a checked-out repo
has NO addressable key, rather than rendering the registered `repos.*`/
`publish.mirrors.*` entries as if they were the whole fleet. Verifies the
PRODUCTION render path (`_render_receiver_listing`, called from the
`--list-receivers` branch of `cross-repo-memo.py`'s `main()`) actually
surfaces the third state — not just that a helper computing it returns the
right value in isolation (the batch's named failure mode: a correct helper
with no production caller).

Spec: https://github.com/dbc-oduffy/claude-klabauter/issues/40
  "Independently of what the registry contains, --list-receivers should say
  when a checked-out repo has NO addressable key, rather than printing a
  list that reads as complete."

Call chain verified here:
  cross-repo-memo.py main() --list-receivers branch
    -> _render_receiver_listing(candidates)
      -> _checked_out_unaddressable_repos(receivers, mirrors)
        -> coordinator_core.ops.discover_working_repos.discover_repo_paths()
"""

from __future__ import annotations

import importlib.util
import os
import sys
from importlib.machinery import SourceFileLoader

import pytest

pytestmark = pytest.mark.cadence


def _script_path() -> str:
    repo_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    return os.path.join(repo_root, "coordinator", "bin", "cross-repo-memo.py")


def _load_cross_repo_memo():
    path = _script_path()
    bin_dir = os.path.dirname(path)
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    loader = SourceFileLoader("cross_repo_memo_lru_test", path)
    spec = importlib.util.spec_from_loader("cross_repo_memo_lru_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_cross_repo_memo()


_RECEIVER = {
    "kind": "receiver",
    "id": "repos.claude_klabauter",
    "repo_key": "repos.claude_klabauter",
    "repo_path": "/home/user/klabauter",
    "target_inbox": "/home/user/klabauter/state/cross-repo/inbox",
    "resolved": True,
    "is_central": False,
    "aliases": [],
}


def test_unaddressable_helper_excludes_registered_and_mirror_paths(mod):
    mirror = {"kind": "publish_mirror", "path": "/home/user/oss-mirror"}

    mod.sys.modules.setdefault("_discover_stub", None)
    import coordinator_core.ops.discover_working_repos as discover_mod

    def _fake_discover():
        return [
            "/home/user/klabauter",
            "/home/user/oss-mirror",
            "/home/user/claude-klabauter",
        ]

    orig = discover_mod.discover_repo_paths
    discover_mod.discover_repo_paths = _fake_discover
    try:
        result = mod._checked_out_unaddressable_repos([_RECEIVER], [mirror])
    finally:
        discover_mod.discover_repo_paths = orig

    assert result == ["/home/user/claude-klabauter"]


def test_render_receiver_listing_surfaces_unaddressable_repo(mod, monkeypatch):
    """The PRODUCTION renderer `--list-receivers` calls prints the gap —
    not only a helper computing it correctly off to the side."""
    import coordinator_core.ops.discover_working_repos as discover_mod

    monkeypatch.setattr(
        discover_mod,
        "discover_repo_paths",
        lambda: ["/home/user/klabauter", "/home/user/claude-klabauter"],
    )

    candidates = [
        dict(_RECEIVER),
        {
            "kind": "registry_status",
            "id": "registry_status",
            "ok": True,
            "note": "machine-local registry read succeeded.",
        },
    ]

    output = mod._render_receiver_listing(candidates)

    assert "/home/user/claude-klabauter" in output
    assert "no addressable key" in output.lower()


def test_render_receiver_listing_silent_when_nothing_unaddressable(mod, monkeypatch):
    import coordinator_core.ops.discover_working_repos as discover_mod

    monkeypatch.setattr(
        discover_mod,
        "discover_repo_paths",
        lambda: ["/home/user/klabauter"],
    )

    candidates = [dict(_RECEIVER)]
    output = mod._render_receiver_listing(candidates)

    assert "no addressable key" not in output.lower()


def test_render_receiver_listing_degrades_on_discovery_failure(mod, monkeypatch):
    import coordinator_core.ops.discover_working_repos as discover_mod

    def _boom():
        raise RuntimeError("engine seam unresolvable")

    monkeypatch.setattr(discover_mod, "discover_repo_paths", _boom)

    candidates = [dict(_RECEIVER)]
    output = mod._render_receiver_listing(candidates)

    assert "no addressable key" not in output.lower()
