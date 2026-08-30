"""Coverage for the dual-tree install cohort: on an authoring box the running
engine is the published mirror, whose `coordinator/bin/` is a strict subset of
the live source tree's. Names only the source tree serves need a forwarder AND
that tree's own resolver installed beside it -- the mirror's resolver cannot
stand in, because the rename map rewrites its registry key along with its
basename, so its live-working-tree rung resolves to the mirror itself.

See `_install_live_source_tree_forwarders` in
`coordinator_core/install/substrate.py`.
"""
from __future__ import annotations

import pytest

from coordinator_core.install import substrate
from coordinator_core.install.substrate import (
    _install_live_source_tree_forwarders,
    _live_source_tree_resolver,
)


def _make_tree(root, *, resolver_stem: str, bin_names: "list[str]"):
    lib = root / "coordinator" / "lib" / resolver_stem.replace("_resolve_", "resolve-").replace("_", "-")
    lib.mkdir(parents=True)
    (lib / f"{resolver_stem}.py").write_text("def exec_cli(target):\n    pass\n", encoding="utf-8")
    bin_dir = root / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    for n in bin_names:
        (bin_dir / f"{n}.py").write_text("#\n", encoding="utf-8")
    return root


@pytest.fixture
def dual_tree(monkeypatch, tmp_path):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    monkeypatch.setattr(
        substrate.tempfile, "gettempdir", lambda: str(tmp_path / "_unrelated-temp-root")
    )
    live = _make_tree(
        tmp_path / "live",
        resolver_stem="_resolve_claude_klabauter",
        bin_names=["shared-cli", "percolate-push"],
    )
    mirror = tmp_path / "mirror"
    _make_tree(mirror, resolver_stem="_resolve_published", bin_names=["shared-cli"])
    bin_dst = tmp_path / "settings-home-bin"
    bin_dst.mkdir()
    monkeypatch.setattr(substrate, "engine_source_root", lambda: str(live))
    return live, mirror, bin_dst


def test_resolver_is_found_by_shape_not_by_name(tmp_path):
    live = _make_tree(tmp_path / "t", resolver_stem="_resolve_anything_at_all", bin_names=[])

    found = _live_source_tree_resolver(live)

    assert found is not None and found.name == "_resolve_anything_at_all.py"


def test_resolver_lookup_returns_none_when_the_shape_is_absent(tmp_path):
    bare = tmp_path / "bare"
    (bare / "coordinator" / "lib").mkdir(parents=True)

    assert _live_source_tree_resolver(bare) is None


def test_only_live_names_get_a_forwarder_bound_to_the_live_resolver(dual_tree):
    live, mirror, bin_dst = dual_tree

    protected = _install_live_source_tree_forwarders(
        bin_dst, mirror, {"shared-cli": "shared-cli.py"}, False
    )

    body = (bin_dst / "percolate-push").read_text(encoding="utf-8")
    assert "from _resolve_claude_klabauter import exec_cli" in body, (
        "the forwarder must import the LIVE tree's resolver, not the running image's"
    )
    assert (bin_dst / "_resolve_claude_klabauter.py").exists(), "the resolver must be installed beside it"
    assert not (bin_dst / "shared-cli").exists(), (
        "a name the running engine already serves is written by the ordinary loop, not here"
    )
    assert {"percolate-push", "shared-cli", "_resolve_claude_klabauter.py"} <= protected


def test_single_tree_box_is_a_no_op(monkeypatch, tmp_path):
    monkeypatch.setattr(substrate, "engine_source_root", lambda: None)
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()

    assert _install_live_source_tree_forwarders(bin_dst, tmp_path, {}, False) == frozenset()
    assert list(bin_dst.iterdir()) == []


def test_running_engine_that_is_the_live_tree_is_a_no_op(monkeypatch, tmp_path):
    """The ordinary loop already serves every name -- installing a second
    cohort against the same tree would be pure duplication."""
    live = _make_tree(tmp_path / "live", resolver_stem="_resolve_claude_klabauter", bin_names=["a"])
    monkeypatch.setattr(substrate, "engine_source_root", lambda: str(live))
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()

    assert _install_live_source_tree_forwarders(bin_dst, live, {}, False) == frozenset()
    assert list(bin_dst.iterdir()) == []


def test_protection_survives_a_run_that_writes_nothing_new(dual_tree):
    """Protection must not depend on having just written the file: a second
    run whose forwarders are all up to date must still protect them."""
    live, mirror, bin_dst = dual_tree
    full_map = {"shared-cli": "shared-cli.py", "percolate-push": "percolate-push.py"}

    protected = _install_live_source_tree_forwarders(bin_dst, mirror, full_map, False)

    assert not (bin_dst / "percolate-push").exists(), "nothing only-live left to write"
    assert "percolate-push" in protected, "the name must still be protected from the sweep"
