"""test_percolate_gate_scan_register — binds C1's scan-secrets MEDIUM-split
invariants (docs/plans/2026-08-13-percolate-round-one-command-and-scan-
register.md § C1/AC1-AC5) to real assertions rather than to the module's own
docstring.

The originating memo for this plan found a docstring-stated invariant with
no test bound to it, so a green suite asserted a bug. This file exists so
that class of drift fails loudly instead.

Run: python -m pytest coordinator/bin/tests/test_percolate_gate_scan_register.py -q
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_gate_scan_register", _BIN_DIR / "percolate-gate.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _load_round_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_round_for_scan_register", _BIN_DIR / "percolate-round.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()
_round_mod = _load_round_module()


def _run_cli(args: list[str]):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _mod.main(args)
    return rc, buf.getvalue()


def _write_store(percolate_root: Path, target: str, *, guarded: bool) -> None:
    hooks_dir = percolate_root / "setup" / "percolate-hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    guards = (
        [
            {
                "kind": "no-residual-pattern",
                "params": {"pattern_source": {"registry_codenames": True}},
            }
        ]
        if guarded
        else []
    )
    store = {"targets": {target: {"guards": guards}}}
    (hooks_dir / "percolate-store.yaml").write_text(
        yaml.safe_dump(store), encoding="utf-8"
    )


def _write_peer_registry(tmp_path: Path, *names: str) -> Path:
    registry = tmp_path / "repo-registry.md"
    lines = "".join(f"- shortname: {n}\n  path: /x/{n}\n" for n in names)
    registry.write_text(lines, encoding="utf-8")
    return registry


def _write_mentions_file(tmp_path: Path, peer_name: str) -> Path:
    target_file = tmp_path / "mentions.md"
    target_file.write_text(f"cross-reference {peer_name} here\n", encoding="utf-8")
    return target_file


def _write_file_list(tmp_path: Path, *files: Path) -> Path:
    file_list = tmp_path / "files.txt"
    file_list.write_text(
        "".join(f"{f}\n" for f in files), encoding="utf-8"
    )
    return file_list


# ---------------------------------------------------------------------------
# AC1/AC3 — declared registry_codenames guard splits the render, total count
# consumed by the Step 3 gate predicate is unchanged.
# ---------------------------------------------------------------------------

def test_registry_codename_guard_splits_medium_render(tmp_path):
    percolate_root = tmp_path / "percolate-root"
    _write_store(percolate_root, "alpha", guarded=True)
    registry = _write_peer_registry(tmp_path, "alpha", "example-retrieval-repo")
    mentions = _write_mentions_file(tmp_path, "example-retrieval-repo")
    file_list = _write_file_list(tmp_path, mentions)

    rc, out = _run_cli(
        [
            "scan-secrets",
            "--files",
            str(file_list),
            "--peer-repos-file",
            str(registry),
            "--target",
            "alpha",
            "--percolate-root",
            str(percolate_root),
        ]
    )
    assert rc == 0
    assert "read pre-transform" in out
    assert "example-retrieval-repo" in out.split("read pre-transform")[1]

    # The plain (uncovered) MEDIUM group must show none — the covered hit
    # was routed to the new group, not duplicated into the old one.
    plain_medium_section = out.split(
        "MEDIUM (identity / internal paths / peer-repo names -- surfaces to gate):"
    )[1].split("LOW")[0]
    assert "(none)" in plain_medium_section
    assert "example-retrieval-repo" not in plain_medium_section


def test_registry_codename_guard_total_medium_count_unchanged(tmp_path):
    """AC3: the split changes rendering only — the total count the Step 3
    gate predicate consumes (every ``<path>:<line>:``-shaped hit between the
    HIGH and LOW headers) is identical whether or not the guard fires.

    Review: code-reviewer — the expected count is derived independently of
    ``_count_medium_hits`` (by counting the peer-repo-name occurrence
    directly in the source fixture), not by parsing both CLI outputs with
    the same function under test elsewhere — a systematic bug in the shared
    parser must still be able to fail this test."""
    percolate_root = tmp_path / "percolate-root"
    registry = _write_peer_registry(tmp_path, "alpha", "example-retrieval-repo")
    mentions = _write_mentions_file(tmp_path, "example-retrieval-repo")
    file_list = _write_file_list(tmp_path, mentions)

    # Independently-derived expected count: exactly one line in the fixture
    # mentions "example-retrieval-repo", so exactly one MEDIUM hit is expected total,
    # regardless of which group (covered or plain) it renders under.
    expected_count = sum(
        1 for line in mentions.read_text(encoding="utf-8").splitlines()
        if "example-retrieval-repo" in line
    )
    assert expected_count == 1

    _write_store(percolate_root, "alpha", guarded=True)
    rc_split, out_split = _run_cli(
        [
            "scan-secrets",
            "--files",
            str(file_list),
            "--peer-repos-file",
            str(registry),
            "--target",
            "alpha",
            "--percolate-root",
            str(percolate_root),
        ]
    )
    assert rc_split == 0

    rc_unsplit, out_unsplit = _run_cli(
        [
            "scan-secrets",
            "--files",
            str(file_list),
            "--peer-repos-file",
            str(registry),
            "--target",
            "alpha",
        ]
    )
    assert rc_unsplit == 0

    count_split = _round_mod._count_medium_hits(out_split)
    count_unsplit = _round_mod._count_medium_hits(out_unsplit)
    assert count_split == count_unsplit == expected_count


# ---------------------------------------------------------------------------
# AC4 — a target declaring no transform is byte-identical to the pre-change
# form. Reproduces C1's own manual verification (Observations item 2 of
# state/subagent-share/.../coordinatorexecutor-d27d9189.md) as a real
# assertion instead of a throwaway script.
# ---------------------------------------------------------------------------

def test_no_declared_transform_is_byte_identical_to_no_flag_run(tmp_path):
    """AC4, as amended by the review-integration pass: a genuine
    no-transform target's panel is unchanged whether or not
    --percolate-root is passed. Omitting --percolate-root now also emits a
    one-line NOTE (Review: code-reviewer — silent-fallback finding) stating
    the transform-coverage split was skipped; that NOTE is the only
    permitted difference between the two invocation shapes."""
    percolate_root = tmp_path / "percolate-root"
    _write_store(percolate_root, "alpha", guarded=False)
    registry = _write_peer_registry(tmp_path, "alpha", "example-retrieval-repo")
    mentions = _write_mentions_file(tmp_path, "example-retrieval-repo")
    file_list = _write_file_list(tmp_path, mentions)

    base_args = [
        "scan-secrets",
        "--files",
        str(file_list),
        "--peer-repos-file",
        str(registry),
        "--target",
        "alpha",
    ]

    rc_with_root, out_with_root = _run_cli(
        base_args + ["--percolate-root", str(percolate_root)]
    )
    rc_without_root, out_without_root = _run_cli(base_args)

    assert rc_with_root == rc_without_root == 0
    assert "read pre-transform" not in out_with_root
    assert "NOTE: --percolate-root not passed" not in out_with_root
    assert "NOTE: --percolate-root not passed" in out_without_root
    # Stripping the added NOTE line leaves the two outputs identical --
    # the notice is the only permitted divergence.
    without_root_lines = out_without_root.splitlines(keepends=True)
    stripped_without_root = "".join(
        line for line in without_root_lines
        if "NOTE: --percolate-root not passed" not in line
    )
    assert stripped_without_root == out_with_root


# ---------------------------------------------------------------------------
# AC5 — HIGH tier is untouched: HIGH >=1 still exits 2 regardless of a
# declared guard.
# ---------------------------------------------------------------------------

def test_high_hit_still_exits_2_with_guard_declared(tmp_path):
    percolate_root = tmp_path / "percolate-root"
    _write_store(percolate_root, "alpha", guarded=True)

    leaky = tmp_path / "leaky.md"
    leaky.write_text(
        "here is a token: sk-abcdefghijklmnopqrstuvwx\n", encoding="utf-8"  # noqa: secrets
    )
    file_list = _write_file_list(tmp_path, leaky)

    rc, out = _run_cli(
        [
            "scan-secrets",
            "--files",
            str(file_list),
            "--target",
            "alpha",
            "--percolate-root",
            str(percolate_root),
        ]
    )
    assert rc == 2
    assert "sk-a..." in out


# ---------------------------------------------------------------------------
# An undeclared target (resolve_target raises KeyError) degrades to no-split,
# not a crash.
# ---------------------------------------------------------------------------

def test_undeclared_target_degrades_to_no_split(tmp_path):
    percolate_root = tmp_path / "percolate-root"
    # Store exists but declares no "alpha" target at all.
    hooks_dir = percolate_root / "setup" / "percolate-hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "percolate-store.yaml").write_text(
        yaml.safe_dump({"targets": {"someone-else": {"guards": []}}}),
        encoding="utf-8",
    )

    registry = _write_peer_registry(tmp_path, "alpha", "example-retrieval-repo")
    mentions = _write_mentions_file(tmp_path, "example-retrieval-repo")
    file_list = _write_file_list(tmp_path, mentions)

    rc, out = _run_cli(
        [
            "scan-secrets",
            "--files",
            str(file_list),
            "--peer-repos-file",
            str(registry),
            "--target",
            "alpha",
            "--percolate-root",
            str(percolate_root),
        ]
    )
    assert rc == 0
    assert "read pre-transform" not in out
    assert "example-retrieval-repo" in out
