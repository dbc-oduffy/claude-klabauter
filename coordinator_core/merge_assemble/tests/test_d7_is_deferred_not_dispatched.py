"""Pins `d7` (completion-log tag flip) as a deferred narrated no-op rather
than an apply-time dispatch, and pins its args against the CLI's real parser.

`merge-release-notes-derive flip-tags` takes four required positionals:
`release_tag_cut merge_sha merge_date entry_paths...`. Three are facts about a
merge commit that does not exist when `merge_assemble.apply()` runs — apply is
SKILL.md Step 3, before the PR is even created; the flip is Step 10. Dispatching
it at apply time spent every `/merging-to-main` run on `exited 2` (argparse
usage error), which — being a raised handler — returned
`APPLY_EXIT_PARTIAL_MUTATION` and abandoned `d8` and `d_grant_handback`.

This is the D4 argument-underfill defect one directive later, with a different
remedy: `d4`'s missing values were computable at apply time and got frozen into
its args; `d7`'s are not computable at apply time at all, so the directive
defers instead.

Negative spec: `d7` is NOT deleted. It keeps its id, its `cli`, its
`depends_on: ["d2"]` ordering edge, and a `skipped_reason` naming the post-merge
invocation — the operator sees a deferred step, never a silently dropped one.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from coordinator_core.merge_assemble import build_directives

_BIN = Path(__file__).resolve().parents[3] / "coordinator" / "bin"


def _flip_tags_cli():
    """The REAL `merge-release-notes-derive` module, loaded by path the same
    way `coordinator/bin/tests/test_merge_release_notes_derive.py` loads it —
    restating its parser here would let the two drift apart, which is exactly
    the class of defect this file pins."""
    spec = importlib.util.spec_from_file_location(
        "merge_release_notes_derive_for_d7", _BIN / "merge-release-notes-derive.py"
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _d7(tmp_path: Path) -> dict:
    directives = build_directives(tmp_path, tag_prefix="v", proposed_tag="v0.4.1")
    by_id = {d["id"]: d for d in directives}
    assert "d7" in by_id, "d7 must remain in the directive list, deferred not deleted"
    return by_id["d7"]


def test_d7_is_a_narrated_no_op(tmp_path: Path) -> None:
    d7 = _d7(tmp_path)
    assert d7["already_satisfied"] is True
    reason = d7.get("skipped_reason") or ""
    assert reason, "a deferred directive must narrate why — silence is the defect"
    # The reason has to be actionable: it names the post-merge step and the
    # facts that do not exist yet.
    assert "Step 10" in reason
    assert "merge_sha" in reason or "merge SHA" in reason


def test_d7_keeps_its_identity_and_ordering_edge(tmp_path: Path) -> None:
    d7 = _d7(tmp_path)
    assert d7["cli"] == "merge-release-notes-derive"
    assert d7["args"][0] == "flip-tags"
    assert d7["depends_on"] == ["d2"]


def test_d7_args_are_underfilled_for_the_real_parser(tmp_path: Path) -> None:
    """The reason `d7` must not dispatch, asserted against the CLI's actual
    parser shape rather than a prose claim about it. If someone later gives
    `flip-tags` defaults for the three post-merge positionals, this test goes
    red and the deferral can be revisited on evidence."""
    parser = _flip_tags_cli()._build_parser()
    d7 = _d7(tmp_path)
    with pytest.raises(SystemExit):
        parser.parse_args(d7["args"])
