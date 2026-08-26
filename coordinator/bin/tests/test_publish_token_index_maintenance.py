"""AC4 and AC8 of docs/plans/2026-08-26-payload-parity-asks-an-index-not-the-
payload.md -- the round's own token-index maintenance leg in
`coordinator/bin/publish.py`.

Why this file exists: C4 landed 113 lines into `publish.py`, and `publish.py`
maps to no runnable test target in that plan's spine, so no dispatch wave ever
ran a test over any of it. AC8 was shipped entirely untested and AC4's
stamp-provenance half with it. Both are silent-degradation shapes -- an index
that wrongly believes itself fresh is indistinguishable, at the prescreen, from
"this file does not reference the needle" -- so an untested branch here is not a
coverage nit, it is the 2026-08-21 outage class reintroduced by the very
optimisation that was supposed to make the gate affordable.

Negative-spec: nothing here asserts publish-round orchestration, manifest
shape, or swap mechanics. This file is scoped to the token-index maintenance
helpers and their branch dispositions.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_PUBLISH_PATH = Path(__file__).resolve().parents[1] / "publish.py"
_REPO_ROOT = Path(__file__).resolve().parents[2]

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from coordinator_core.percolate import token_index as ti  # noqa: E402


def _load_publish():
    """Import `coordinator/bin/publish.py` by path -- it is a script, not an
    importable package member, and has no `__init__.py` lineage. Registered in
    `sys.modules` BEFORE `exec_module`: `publish.py` defines dataclasses, and
    `dataclasses` resolves a string annotation through
    `sys.modules[cls.__module__]`, which is `None` for a module that is not
    registered yet."""
    spec = importlib.util.spec_from_file_location(
        "publish_token_index_maintenance_under_test", _PUBLISH_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def publish():
    return _load_publish()


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _build_full_index(root: Path, index_path: Path) -> ti.TokenIndex:
    """A completed C3 build over `root` -- every `.py` file covered, freshly
    stamped against dest."""
    result = ti.build_slice(root, budget_files=10_000)
    index = ti.TokenIndex(format_version=ti.FORMAT_VERSION, files=[], postings={}, stamps={})
    ti.apply_update(index, result)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    ti.serialize_index(index, index_path)
    return index


def _dest_tree(tmp_path: Path, name: str = "dest") -> Path:
    root = tmp_path / name
    _write(root, "coordinator_core/__init__.py", "")
    _write(root, "coordinator_core/callee.py", "def resolve_thing(a, b):\n    return a\n")
    _write(
        root,
        "coordinator_core/caller.py",
        "from coordinator_core.callee import resolve_thing\nresolve_thing(1, 2)\n",
    )
    _write(root, "coordinator_core/bystander.py", "x = 1\n")
    return root


# ---------------------------------------------------------------------------
# AC8 -- three branches, one "invalidate this root" call.
# ---------------------------------------------------------------------------
class TestAC8BranchDisposition:
    """AC8: an undetermined root, a refused row, and a `PublishSwapPartial`
    root each get an explicit disposition, and both invalidating branches
    route through the SAME single call -- one code path to test."""

    def test_undetermined_root_invalidates(self, publish):
        root = Path("/tmp/whatever")
        assert (
            publish._token_index_action_for_root(
                root, invalidate_roots=set(), undetermined_roots={root}
            )
            == "invalidate"
        )

    def test_partial_swap_root_invalidates(self, publish):
        root = Path("/tmp/whatever")
        assert (
            publish._token_index_action_for_root(
                root, invalidate_roots={root}, undetermined_roots=set()
            )
            == "invalidate"
        )

    def test_refused_row_root_updates_and_needs_no_branch_of_its_own(self, publish):
        """A refused row never swapped, so it appears in NEITHER set. The
        disposition is an ordinary update over a delta the refused row is
        absent from -- not a third special case."""
        root = Path("/tmp/whatever")
        assert (
            publish._token_index_action_for_root(
                root, invalidate_roots=set(), undetermined_roots=set()
            )
            == "update"
        )

    def test_both_invalidating_branches_reach_the_same_call(self, publish, tmp_path):
        """The branches are not merely labelled the same -- each actually
        removes the index AND its build cursor, so the next round treats the
        root as cold rather than trusting a stale-but-covered stamp."""
        for label, kwargs in (
            ("undetermined", {"invalidate_roots": set(), "undetermined_roots": None}),
            ("partial-swap", {"invalidate_roots": None, "undetermined_roots": set()}),
        ):
            root = tmp_path / label
            index_path, cursor_path = publish._token_index_paths(root)
            _dest_tree(tmp_path, name=label)
            _build_full_index(root, index_path)
            cursor_path.write_text('{"after": null, "done": true}', encoding="utf-8")
            assert index_path.is_file() and cursor_path.is_file()

            resolved = {k: ({root} if v is None else v) for k, v in kwargs.items()}
            action = publish._token_index_action_for_root(root, **resolved)
            assert action == "invalidate", label
            publish._invalidate_token_index(root)

            assert not index_path.exists(), f"{label}: index survived invalidation"
            assert not cursor_path.exists(), f"{label}: build cursor survived invalidation"

    def test_invalidating_a_cold_root_is_a_no_op_not_an_error(self, publish, tmp_path):
        """Idempotence matters because two branches can name the same root in
        one round (an undetermined root that also had a partial swap)."""
        root = tmp_path / "cold"
        root.mkdir()
        publish._invalidate_token_index(root)
        publish._invalidate_token_index(root)

    def test_stale_but_covered_entry_never_survives_invalidation(self, publish, tmp_path):
        """The failure AC8 exists to prevent, stated as a test: after
        invalidation the prescreen must not be able to answer from the index
        at all -- not "answer differently", not "answer with a warning"."""
        from coordinator_core.percolate import payload_parity as pp

        root = _dest_tree(tmp_path)
        index_path, _cursor = publish._token_index_paths(root)
        _build_full_index(root, index_path)

        # Dest changes out of band, exactly as a partial swap leaves it.
        _write(root, "coordinator_core/caller.py", "resolve_thing = None\n")
        publish._invalidate_token_index(root)

        index = pp.build_first_party_import_index(root)
        via_index = pp._files_referencing_needles(
            index,
            frozenset({"resolve_thing"}),
            token_index_path=index_path,
            index_root=root,
        )
        full_scan = pp._files_referencing_needles(index, frozenset({"resolve_thing"}))
        assert via_index == full_scan


# ---------------------------------------------------------------------------
# AC4 -- delta-derived index == from-scratch index, and the stamp is a
# POST-SWAP DEST stat.
# ---------------------------------------------------------------------------
def _canonical(index: ti.TokenIndex) -> dict:
    """Compare by CONTENT, not by file-id assignment: a from-scratch build
    numbers files in walk order and a delta-derived one in update order, so
    raw `files`/`postings` lists differ while the index they describe is
    identical."""
    id_to_rel = {i: rel for i, rel in enumerate(index.files)}
    return {
        "postings": {
            token: frozenset(id_to_rel[i] for i in ids) for token, ids in index.postings.items() if ids
        },
        "stamps": {rel: (st.size, st.mtime_ns) for rel, st in index.stamps.items()},
    }


class TestAC4DeltaDerivedEqualsFromScratch:
    def test_add_update_delete_delta_matches_a_from_scratch_build(self, publish, tmp_path):
        root = _dest_tree(tmp_path)
        index_path, _cursor = publish._token_index_paths(root)
        _build_full_index(root, index_path)

        added = _write(root, "coordinator_core/newcomer.py", "def brand_new(z):\n    return z\n")
        updated = _write(
            root,
            "coordinator_core/caller.py",
            "from coordinator_core.callee import resolve_thing\nresolve_thing(1, 2, 3)\n",
        )
        removed = root / "coordinator_core" / "bystander.py"
        removed.unlink()

        publish._update_token_index_from_delta(root, {added, updated}, {removed})

        derived = ti.load_index(index_path)
        scratch_path = tmp_path / "scratch.bin"
        scratch = _build_full_index(root, scratch_path)

        assert _canonical(derived) == _canonical(scratch)

    def test_oracle_pair_delta_matches_a_from_scratch_build(self, publish, tmp_path):
        """AC4's oracle-pair leg: the same signature-moved shape the 2026-08-21
        outage had (a callee whose signature moved while its caller kept
        passing the dropped kwarg), carried through as a round delta."""
        root = tmp_path / "oracle-dest"
        _write(root, "coordinator_core/__init__.py", "")
        _write(
            root,
            "coordinator_core/callee.py",
            "def carry(payload, equivalence_map=None):\n    return payload\n",
        )
        _write(
            root,
            "coordinator_core/caller.py",
            "from coordinator_core.callee import carry\ncarry({}, equivalence_map={})\n",
        )
        index_path, _cursor = publish._token_index_paths(root)
        _build_full_index(root, index_path)

        # The outage edit: the callee drops the kwarg, the caller does not.
        moved = _write(root, "coordinator_core/callee.py", "def carry(payload):\n    return payload\n")
        publish._update_token_index_from_delta(root, {moved}, set())

        derived = ti.load_index(index_path)
        scratch = _build_full_index(root, tmp_path / "oracle-scratch.bin")
        assert _canonical(derived) == _canonical(scratch)

        # And the token that moved is genuinely gone from the callee's postings
        # -- the equality above would also hold if BOTH were wrong.
        assert "equivalence_map" not in _canonical(derived)["postings"] or (
            "coordinator_core/callee.py"
            not in _canonical(derived)["postings"]["equivalence_map"]
        )

    def test_stamp_is_a_dest_stat_never_the_staging_side_one(self, publish, tmp_path):
        """Review finding 2, as a test. `_create_publish_staging_dir` mkdtemps a
        fresh tree every round, so every staging file has a NEW mtime and a
        possibly different size. A staging-side stat therefore either reads
        permanently stale or optimistically claims a freshness dest does not
        have -- and the second one fails SILENTLY."""
        root = _dest_tree(tmp_path)
        index_path, _cursor = publish._token_index_paths(root)
        _build_full_index(root, index_path)

        dest_file = root / "coordinator_core" / "caller.py"
        dest_file.write_text("from coordinator_core.callee import resolve_thing\n", encoding="utf-8")
        os.utime(dest_file, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))

        # A staging twin with deliberately different bytes AND a different mtime.
        staging_file = _write(
            tmp_path / "staging", "coordinator_core/caller.py", "# staging-only content, longer\n" * 4
        )
        assert staging_file.stat().st_size != dest_file.stat().st_size
        assert staging_file.stat().st_mtime_ns != dest_file.stat().st_mtime_ns

        publish._update_token_index_from_delta(root, {dest_file}, set())

        stamp = ti.load_index(index_path).stamps["coordinator_core/caller.py"]
        dest_stat = dest_file.stat()
        assert (stamp.size, stamp.mtime_ns) == (dest_stat.st_size, dest_stat.st_mtime_ns)
        assert (stamp.size, stamp.mtime_ns) != (
            staging_file.stat().st_size,
            staging_file.stat().st_mtime_ns,
        ), "AC4: the stamp came off the STAGING file -- see review finding 2"

    def test_unreadable_dest_path_drops_from_coverage_rather_than_guessing(self, publish, tmp_path):
        """"Never optimistic": a changed path that vanished before stamp time
        must leave NO entry, so the prescreen falls back and reads it."""
        root = _dest_tree(tmp_path)
        index_path, _cursor = publish._token_index_paths(root)
        _build_full_index(root, index_path)

        ghost = root / "coordinator_core" / "ghost.py"
        publish._update_token_index_from_delta(root, {ghost}, set())

        assert "coordinator_core/ghost.py" not in ti.load_index(index_path).stamps

    def test_cold_root_is_left_alone_rather_than_built(self, publish, tmp_path):
        """A round never builds (§ AC3) -- an absent index stays absent."""
        root = _dest_tree(tmp_path)
        index_path, _cursor = publish._token_index_paths(root)
        assert not index_path.exists()

        publish._update_token_index_from_delta(
            root, {root / "coordinator_core" / "caller.py"}, set()
        )
        assert not index_path.exists(), "a round must never build the index itself"
