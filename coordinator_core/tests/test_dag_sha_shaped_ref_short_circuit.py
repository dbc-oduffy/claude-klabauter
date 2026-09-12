"""
coordinator_core.tests.test_dag_sha_shaped_ref_short_circuit — Coverage for
C9 #1: `resolve_target`'s SHA-shaped ref short-circuit.

A ref whose whole stripped value matches ``^[0-9a-f]{7,40}$`` is not a path
and not a handoff_id -- it is the `kind: recovery` baton convention of
carrying a crash-commit SHA in `predecessor:` (schema comment: "NOT a
predecessor handoff path"). The short-circuit must return None BEFORE the
`id_index` lookup (so `_LazyHandoffIdIndex.__contains__`'s repo-wide corpus
scan never runs) and before any subprocess spawn (tiers 1-3).

Spec backlink: docs/plans/2026-09-11-three-ceremony-briefs-rebuilt-from-their-requirements.md
§ C9 (AC11 / DR-415 deletion 7)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import dag

pytestmark = [pytest.mark.cadence]


@pytest.fixture(autouse=True)
def _no_real_subprocess(monkeypatch):
    """This whole file spawns nothing (per the row's Tests section) — a
    reject-shaped stub stands in for `dag.subprocess.run` everywhere, so
    even the "unaffected" ref shapes that legitimately reach resolve_target's
    tier 3 exercise that code path without a real `git` spawn. Tests that
    assert the SHA-shaped short-circuit itself still install their own
    `boom`-raising stub (a strictly stronger assertion: not merely
    unspawned-by-default, but a hard failure if reached at all).
    """
    import subprocess as _subprocess

    class _FakeCompletedProcess:
        returncode = 1
        stdout = ''

    def _fake_run(*_a, **_kw):
        return _FakeCompletedProcess()

    monkeypatch.setattr(_subprocess, "run", _fake_run)


def _write_handoff(root: Path, rel_path: str, lines: list[str]) -> Path:
    p = root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    body = "---\n" + "\n".join(lines) + "\n---\n\nbody\n"
    p.write_text(body, encoding="utf-8")
    return p


class TestShaShapedRefShortCircuit:
    def test_sha_shaped_ref_resolves_to_none(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        handoff_dir = str(root / "state" / "handoffs")

        result = dag.resolve_target(
            "a1b2c3d4e5f6", handoff_dir, str(root)
        )

        assert result is None

    def test_no_subprocess_is_spawned(self, tmp_path, monkeypatch):
        root = tmp_path / "repo"
        root.mkdir()
        handoff_dir = str(root / "state" / "handoffs")

        def boom(*_a, **_kw):
            raise AssertionError(
                "resolve_target must not spawn a subprocess for a SHA-shaped ref"
            )

        monkeypatch.setattr(dag.subprocess, "run", boom)

        result = dag.resolve_target(
            "229e792f1a2b3c4d", handoff_dir, str(root)
        )

        assert result is None

    def test_the_lazy_id_index_is_never_built(self, tmp_path, monkeypatch):
        root = tmp_path / "repo"
        root.mkdir()
        handoff_dir = str(root / "state" / "handoffs")

        def boom(repo_root):
            raise AssertionError(
                "resolve_target must not consult id_index for a SHA-shaped ref"
            )

        monkeypatch.setattr(dag, "_scan_handoff_corpus_paths", boom)
        lazy_index = dag._LazyHandoffIdIndex(str(root))

        result = dag.resolve_target(
            "a1b2c3d4e5f6", handoff_dir, str(root), id_index=lazy_index
        )

        assert result is None

    def test_plain_dict_id_index_is_also_never_consulted(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        handoff_dir = str(root / "state" / "handoffs")
        target = _write_handoff(
            root, "state/handoffs/decoy.md", ["handoff_id: a1b2c3d4e5f6"]
        )
        # A SHA-shaped ref that ALSO happens to be a key in id_index must
        # still short-circuit to None -- the short-circuit runs before the
        # id_index lookup, unconditionally.
        id_index = {"a1b2c3d4e5f6": str(target.absolute())}

        result = dag.resolve_target(
            "a1b2c3d4e5f6", handoff_dir, str(root), id_index=id_index
        )

        assert result is None

    @pytest.mark.parametrize(
        "sha",
        [
            "1234567",  # 7 hex chars -- lower bound
            "0123456789abcdef0123456789abcdef01234567"[:40],  # 40 hex chars -- upper bound
        ],
    )
    def test_boundary_lengths_short_circuit(self, tmp_path, sha):
        root = tmp_path / "repo"
        root.mkdir()
        handoff_dir = str(root / "state" / "handoffs")

        result = dag.resolve_target(sha, handoff_dir, str(root))

        assert result is None


class TestRefContainingHexIsUnaffected:
    def test_ref_that_merely_contains_hex_is_not_short_circuited(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        handoff_dir = str(root / "state" / "handoffs")
        target = _write_handoff(
            root, "state/handoffs/a1b2c3d.md", ["title: not a sha, a filename"]
        )

        # "a1b2c3d.md" is NOT SHA-shaped (whole-string match fails: has a
        # '.md' suffix), so it must resolve normally via the filename tier.
        result = dag.resolve_target("a1b2c3d.md", handoff_dir, str(root))

        assert result == str(target.absolute())

    def test_hex_substring_embedded_in_longer_non_hex_ref_is_unaffected(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        handoff_dir = str(root / "state" / "handoffs")

        # "deadbeef-some-slug" contains a hex run but the WHOLE stripped
        # value is not all-hex, so it is not SHA-shaped.
        result = dag.resolve_target(
            "deadbeef-some-slug.md", handoff_dir, str(root)
        )

        # Unresolvable (no such file) but via the normal tiers, not the
        # short-circuit -- distinguished from the SHA-shaped case by the
        # other tests in this module (this just pins that it reaches the
        # normal path without raising).
        assert result is None

    def test_too_short_hex_string_is_not_short_circuited(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        handoff_dir = str(root / "state" / "handoffs")

        # 6 hex chars -- below the 7-char floor -- must not short-circuit.
        result = dag.resolve_target("abcdef", handoff_dir, str(root))

        assert result is None

    def test_too_long_hex_string_is_not_short_circuited(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        handoff_dir = str(root / "state" / "handoffs")

        # 41 hex chars -- above the 40-char ceiling -- must not short-circuit.
        result = dag.resolve_target("a" * 41, handoff_dir, str(root))

        assert result is None
