"""
Tests for coordinator_core.ops.assert_plan_sizing_citation.

Spec backlink: pln-plan-sizing-citation-gate-scaf-45eaed § C3 / AC4 / AC6
"""
from __future__ import annotations

import os

from coordinator_core.ops.assert_plan_sizing_citation import main

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _write(root: str, rel: str, content: str) -> None:
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(content)


def test_no_docs_plans_dir_returns_zero(tmp_path, capsys):
    root = str(tmp_path)
    rc = main(["--root", root])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no docs/plans" in out


def test_current_corpus_passes(capsys):
    """The real docs/plans/ corpus of this checkout has zero dangling
    frontmatter sizing_object citations (plan's own stated baseline)."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    rc = main(["--root", repo_root])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK: no dangling" in out


def test_dangling_frontmatter_citation_detected(tmp_path, capsys):
    root = str(tmp_path)
    _write(
        root,
        "docs/plans/2026-01-01-example-plan.md",
        (
            "---\n"
            "title: \"Example plan\"\n"
            "sizing_object: state/sizings/2026-01-01-does-not-exist.yaml\n"
            "---\n\n"
            "# Example plan\n"
        ),
    )
    rc = main(["--root", root])
    captured = capsys.readouterr()
    assert rc == 1
    assert "FAIL: 1 dangling" in captured.err
    assert "docs/plans/2026-01-01-example-plan.md" in captured.err
    assert "state/sizings/2026-01-01-does-not-exist.yaml" in captured.err


def test_resolving_frontmatter_citation_passes(tmp_path, capsys):
    root = str(tmp_path)
    _write(root, "state/sizings/2026-01-01-real.yaml", "schema: sizing-object\n")
    _write(
        root,
        "docs/plans/2026-01-01-example-plan.md",
        (
            "---\n"
            "title: \"Example plan\"\n"
            "sizing_object: state/sizings/2026-01-01-real.yaml\n"
            "---\n\n"
            "# Example plan\n"
        ),
    )
    rc = main(["--root", root])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK: no dangling" in out


def test_absent_frontmatter_key_skipped(tmp_path, capsys):
    root = str(tmp_path)
    _write(
        root,
        "docs/plans/2026-01-01-no-sizing.md",
        "---\ntitle: \"No sizing key\"\n---\n\n# No sizing key\n",
    )
    rc = main(["--root", root])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK: no dangling" in out


def test_post_cutoff_plan_missing_key_flagged(tmp_path, capsys):
    """AC5: a post-cutoff plan with no sizing_object key at all is a MISSING
    finding, exit 1, plan named in output."""
    root = str(tmp_path)
    _write(
        root,
        "docs/plans/2026-08-06-no-key.md",
        "---\ntitle: \"No key\"\ncreated: 2026-08-06\n---\n\n# No key\n",
    )
    rc = main(["--root", root])
    captured = capsys.readouterr()
    assert rc == 1
    assert "FAIL: 1 plan(s) missing" in captured.err
    assert "docs/plans/2026-08-06-no-key.md" in captured.err


def test_post_cutoff_plan_explicit_null_satisfies(tmp_path, capsys):
    """AC6: explicit `sizing_object: null` is a satisfied declaration."""
    root = str(tmp_path)
    _write(
        root,
        "docs/plans/2026-08-06-explicit-null.md",
        (
            "---\ntitle: \"Explicit null\"\ncreated: 2026-08-06\n"
            "sizing_object: null\n---\n\n# Explicit null\n"
        ),
    )
    rc = main(["--root", root])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK: no dangling or missing" in out


def test_pre_cutoff_plan_missing_key_not_flagged(tmp_path, capsys):
    """AC7: the 152-plan negative -- created < _CUTOFF, no key, exit 0."""
    root = str(tmp_path)
    _write(
        root,
        "docs/plans/2026-01-01-pre-cutoff.md",
        "---\ntitle: \"Pre cutoff\"\ncreated: 2026-01-01\n---\n\n# Pre cutoff\n",
    )
    rc = main(["--root", root])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK: no dangling or missing" in out


def test_prior_art_check_sidecar_with_quartet_not_flagged(tmp_path, capsys):
    """AC8, load-bearing: a `.prior-art-check.md` sidecar carrying the plan
    schema's required quartet (title/created/author/status) and no
    sizing_object key must NOT be flagged -- 68 real sidecars carry that
    quartet, which is why the quartet alone was rejected as a discriminator
    for plan-hood; the basename+kind conjunction is what excludes it."""
    root = str(tmp_path)
    _write(
        root,
        "docs/plans/2026-08-06-something.prior-art-check.md",
        (
            "---\ntitle: \"Prior art check\"\ncreated: 2026-08-06\n"
            "author: someone\nstatus: draft\n---\n\n# Prior art check\n"
        ),
    )
    rc = main(["--root", root])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK: no dangling or missing" in out


def test_kind_prior_art_check_plan_shaped_basename_not_flagged(tmp_path, capsys):
    """AC8: a `kind: prior-art-check` file with a plan-shaped basename
    (matches _PLAN_BASENAME) is excluded by the kind check, not the
    basename check."""
    root = str(tmp_path)
    _write(
        root,
        "docs/plans/2026-08-06-plan-shaped-name.md",
        (
            "---\ntitle: \"Plan shaped\"\ncreated: 2026-08-06\n"
            "kind: prior-art-check\n---\n\n# Plan shaped\n"
        ),
    )
    rc = main(["--root", root])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK: no dangling or missing" in out


def test_index_and_readme_not_flagged(tmp_path, capsys):
    """AC8: INDEX.md / README.md are excluded by the basename shape check."""
    root = str(tmp_path)
    _write(root, "docs/plans/INDEX.md", "---\ntitle: \"Index\"\ncreated: 2026-08-06\n---\n\n# Index\n")
    _write(root, "docs/plans/README.md", "---\ntitle: \"Readme\"\ncreated: 2026-08-06\n---\n\n# Readme\n")
    rc = main(["--root", root])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK: no dangling or missing" in out


def test_ac9_body_prose_dangling_with_frontmatter_null_passes(tmp_path, capsys):
    """AC9's regression test, and the reason the whole op is frontmatter-only:
    a post-cutoff plan whose BODY prose cites a nonexistent
    state/sizings/... path, while its frontmatter carries an explicit null,
    must pass. If this op ever regexed the body, it would fire on this
    plan's body text and make it unwriteable -- an explicit frontmatter null
    satisfies the check regardless of what the prose says."""
    root = str(tmp_path)
    _write(
        root,
        "docs/plans/2026-08-06-body-prose-citation.md",
        (
            "---\ntitle: \"Body prose citation\"\ncreated: 2026-08-06\n"
            "sizing_object: null\n---\n\n# Body prose citation\n\n"
            "This plan cites state/sizings/2026-08-06-does-not-exist.yaml "
            "in prose only, to document that it was never written.\n"
        ),
    )
    rc = main(["--root", root])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK: no dangling or missing" in out


def test_dangling_and_missing_peers_both_reported_separately(tmp_path, capsys):
    """One plan with a dangling citation, a peer with a missing one: exit 1,
    both reported, each in its own block."""
    root = str(tmp_path)
    _write(
        root,
        "docs/plans/2026-08-06-dangling-peer.md",
        (
            "---\ntitle: \"Dangling peer\"\ncreated: 2026-08-06\n"
            "sizing_object: state/sizings/2026-08-06-does-not-exist.yaml\n"
            "---\n\n# Dangling peer\n"
        ),
    )
    _write(
        root,
        "docs/plans/2026-08-06-missing-peer.md",
        "---\ntitle: \"Missing peer\"\ncreated: 2026-08-06\n---\n\n# Missing peer\n",
    )
    rc = main(["--root", root])
    captured = capsys.readouterr()
    assert rc == 1
    assert "FAIL: 1 plan(s) missing a sizing_object declaration" in captured.err
    assert "FAIL: 1 dangling plan sizing_object citation(s)" in captured.err
    assert "docs/plans/2026-08-06-dangling-peer.md" in captured.err
    assert "docs/plans/2026-08-06-missing-peer.md" in captured.err


def test_ac6_body_prose_citation_never_scanned(capsys):
    """AC6, the load-bearing negative: docs/plans/2026-08-06-windows-hot-path-
    less-work-per-interpreter.md cites a nonexistent state/sizings/ path in
    BODY prose (as evidence the citation was never written for a superseded
    ask), while its frontmatter carries no sizing_object key at all. This op
    must pass on the real corpus containing that plan — a text-scanning
    implementation would fire on it and make it unwriteable."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    target = os.path.join(
        repo_root,
        "docs",
        "plans",
        "2026-08-06-windows-hot-path-less-work-per-interpreter.md",
    )
    assert os.path.isfile(target), "fixture plan must exist in the real corpus"
    with open(target, "r", encoding="utf-8") as fh:
        text = fh.read()
    assert "state/sizings/2026-08-06-ten-interpreters-per-edit.yaml" in text, (
        "fixture plan must still carry the dangling body-prose citation this test protects"
    )

    rc = main(["--root", repo_root])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK: no dangling" in out
