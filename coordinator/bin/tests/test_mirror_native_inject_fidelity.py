"""Inject-fidelity test for the mirror-native `.github` CI-harness payload.

Spec backlink: docs/plans/2026-08-07-publish-identity-scrub-and-two-repo-gates.md
chunk C6r (replaces the excised, unbuildable chunk C6 -- see that plan's C6r body
for why a two-path drift comparison was refused: `dist/mirror-native/claude-
klabauter/.github/` is the SOLE in-repo copy of this payload; there is no second
in-repo copy to diff it against).

What this test actually covers: `coordinator_core.percolate.engine.
run_inject_for_section`'s real `inject` dispatch, driven in-process against a
`tmp_path` destination, for the ONE store row that injects this payload
(`claude-klabauter-publish-repo-toplevel`, `setup/percolate-hooks/percolate-
store.yaml`, the `inject` entry whose `src` is
`<claude-klabauter-content-root>/dist/mirror-native/claude-klabauter/.github`, `dst:
".github"`). This closes the genuinely uncovered risk C6r identifies: that the
inject payload does not arrive intact at the destination -- not a drift-against-
a-second-copy question, which does not exist for this payload.

Negative-spec: does NOT invoke `coordinator/bin/publish.py`'s `main()` or any
git/rsync/publish-clone machinery, and does NOT depend on a machine-local
publish clone existing anywhere on disk. Every assertion here runs against a
throwaway `tmp_path` destination this test itself constructs.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

from coordinator_core.percolate import engine, store

_REPO_ROOT = Path(__file__).resolve().parents[3]
_STORE_PATH = _REPO_ROOT / "setup" / "percolate-hooks" / "percolate-store.yaml"
_TARGET_NAME = "claude-klabauter-publish-repo-toplevel"
_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    """Load `coordinator/bin/publish.py` by path (no package `__init__.py` in
    `coordinator/bin`), mirroring `test_unscanned_published_gate.py`'s own
    loader so this file does not invent a second loading convention."""
    spec = importlib.util.spec_from_file_location(
        "publish_mirror_native_inject_fidelity_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _resolved_inject_section() -> dict:
    """Load the real store, resolve the real target, and resolve the real
    `<claude-klabauter-content-root>` placeholder via `publish.py`'s own resolver
    (`_resolve_inject_src_placeholders`) -- not a hand-rolled substitute, so
    this test cannot silently diverge from what a real publish run resolves
    `src` to."""
    raw_store = store.load_store(_STORE_PATH)
    section = store.resolve_target(raw_store, _TARGET_NAME)
    # `percolate_root` only matters for the `<coordinator-content-root>`
    # token; this entry's `src` carries only `<claude-klabauter-content-root>`, which
    # resolves to `_REPO_ROOT` regardless of `percolate_root`'s value -- any
    # non-None placeholder satisfies the resolver's opt-in gate.
    return publish._resolve_inject_src_placeholders(section, percolate_root=_REPO_ROOT)


def _inject_entry(section: dict) -> dict:
    entries = section.get("inject") or []
    matches = [e for e in entries if e.get("dst") == ".github"]
    assert len(matches) == 1, (
        f"expected exactly one inject entry with dst='.github' on target "
        f"{_TARGET_NAME!r}, found {len(matches)}"
    )
    return matches[0]


class TestMirrorNativeInjectFidelity:
    def test_inject_entry_resolves_to_the_real_on_disk_payload(self):
        """Sanity precondition: the resolved `src` is the real, on-disk
        `dist/mirror-native/claude-klabauter/.github` directory this test's
        every other assertion depends on existing and being non-empty."""
        section = _resolved_inject_section()
        entry = _inject_entry(section)
        src = Path(entry["src"])
        assert src.is_dir(), f"resolved inject src does not exist as a directory: {src}"
        src_files = [p for p in src.rglob("*") if p.is_file()]
        assert src_files, f"resolved inject src {src} contains zero files -- nothing to compare"

    def test_every_source_file_arrives_at_dest_byte_identical_or_declared_transformed(
        self, tmp_path
    ):
        """AC1/AC2/AC4 (§ chunk C6r): every file under the inject `src` arrives
        under `dest/.github`; the compared set is asserted non-empty (a
        fidelity test over zero files is the vacuous-verifier class this plan
        exists to close); `check-persona-names.py` arrives byte-identical to
        its source (the ratified `exclude_basenames` carve-out, § below); and
        every OTHER file is asserted content-transformed and NOT silently
        skipped from the comparison.

        `check-persona-names.py` carve-out: `setup/percolate-hooks/percolate-
        store.yaml:139` (`base.file_surface.exclude_basenames`), ratified by
        docs/plans/2026-08-03-mirror-native-content-homed-and-injected.md AC4
        and AC5 -- it carries its own BANNED vocabulary as literal codenames
        and must not scrub itself, else the checker's own source would have
        its detection tokens fragmented by the scrub it is meant to run. Not
        this plan's carve-out to re-decide; asserted here, not re-litigated.

        Every OTHER file in this payload today (CI workflow YAML, issue/PR
        templates, allowlist dotfiles, the other checker scripts, `.gitignore`,
        a stray `__pycache__` artifact) carries none of this store's scrub
        vocabulary (no persona names, no `example-doctrine-repo`/`example_doctrine_repo` tokens, no
        `claude-klabauter` stem) -- so the content-transform pipeline (§
        `engine._apply_content_transforms`) is a no-op on every one of them
        and byte-identical is the CORRECT expected outcome for the whole
        payload today, not a narrowing of what gets compared. Verified below
        by walking `src` and asserting EVERY file's dest bytes, not a
        hand-picked subset.
        """
        section = _resolved_inject_section()
        entry = _inject_entry(section)
        src = Path(entry["src"])

        dest_root = tmp_path / "dest"
        dest_root.mkdir()

        engine.run_inject_for_section(dest_root, section, stdin=io.StringIO())

        github_dst = dest_root / ".github"
        assert github_dst.is_dir(), "inject did not create dest/.github"

        src_files = sorted(p for p in src.rglob("*") if p.is_file())
        assert src_files, "resolved inject src contains zero files -- nothing to compare"

        compared = 0
        for src_file in src_files:
            rel = src_file.relative_to(src)
            dst_file = github_dst / rel
            assert dst_file.is_file(), f"{rel} present under src but missing under dest/.github"

            src_bytes = src_file.read_bytes()
            dst_bytes = dst_file.read_bytes()

            if src_file.name == "check-persona-names.py":
                assert dst_bytes == src_bytes, (
                    "check-persona-names.py must arrive byte-identical to its "
                    "source (ratified exclude_basenames carve-out, "
                    "percolate-store.yaml:139) -- got divergent bytes"
                )
            else:
                # No store scrub vocabulary appears in this payload's other
                # files today (verified: none carry persona names, example-doctrine-repo
                # tokens, or a `claude-klabauter` stem), so the content-transform
                # pipeline is a no-op and byte-identical is the expected,
                # asserted outcome -- not an unexamined skip.
                assert dst_bytes == src_bytes, (
                    f"{rel} diverged from its source under inject scrub; "
                    f"this file was expected to be a content-transform no-op "
                    f"-- if it is now legitimately meant to be rewritten on "
                    f"inject, this test's docstring and this branch must be "
                    f"updated to assert the new expected bytes explicitly, "
                    f"not silently pass"
                )
            compared += 1

        assert compared == len(src_files), "not every source file was compared"
        assert compared > 0, "fidelity check compared zero files -- vacuous pass"

    def test_required_children_from_the_store_row_all_arrive(self, tmp_path):
        """AC3 (§ chunk C6r): every `required_children` entry the store row
        itself declares arrives under `dest/.github`. Reads the list from the
        resolved store row rather than hand-copying it into this test, so a
        store edit adding/removing a required child cannot silently go
        uncovered here."""
        section = _resolved_inject_section()
        entry = _inject_entry(section)
        required_children = entry.get("required_children") or []
        assert required_children, (
            "resolved inject entry declares no required_children -- nothing "
            "for this test to assert (update the store row or this test if "
            "that is intentional)"
        )

        dest_root = tmp_path / "dest"
        dest_root.mkdir()

        engine.run_inject_for_section(dest_root, section, stdin=io.StringIO())

        github_dst = dest_root / ".github"
        for child in required_children:
            assert (github_dst / child).exists(), (
                f"required_children entry {child!r} missing under dest/.github "
                f"after inject"
            )

    def test_inject_itself_enforces_required_children_and_did_not_raise(self, tmp_path):
        """`inject.run_inject` raises `RequiredChildMissingError` if a
        required child is absent post-copy (§ `coordinator_core.percolate.
        inject.run_inject` docstring) -- this test asserts the real dispatch
        path completes without that error for the real on-disk payload,
        rather than only re-deriving the same check independently."""
        from coordinator_core.percolate.inject import RequiredChildMissingError

        section = _resolved_inject_section()
        dest_root = tmp_path / "dest"
        dest_root.mkdir()

        try:
            engine.run_inject_for_section(dest_root, section, stdin=io.StringIO())
        except RequiredChildMissingError as exc:
            pytest.fail(f"real inject dispatch raised RequiredChildMissingError: {exc}")
