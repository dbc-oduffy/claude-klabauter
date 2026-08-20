"""
coordinator_core.plan_assemble.predicates.test_context — co-located pytest
for `coordinator_core.plan_assemble.predicates.PredicateContext` and
`undetermined(...)`, plus the CLI's `--plan`/`--sizing-object` usage-vs-
absent distinction (`coordinator_core.plan_assemble._dispatch_brief`).

Covers: context construction from a fixture plan + sizing object (both
present, both absent, one absent), the `undetermined` sentinel's shape, and
the CLI's usage-error-vs-silent-None behavior for the two new flags. Never
reads the live repo's `docs/plans/` or `state/sizings/` — every fixture is
built under `tmp_path`.

Run: python -m pytest coordinator_core/plan_assemble/predicates/test_context.py -q

Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C1
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.plan_assemble import _dispatch_brief, _PlanAssembleExitCode
from coordinator_core.plan_assemble import residue as residue_mod
from coordinator_core.plan_assemble.predicates import PredicateContext, undetermined


_PLAN_TEXT = """---
title: "fixture plan"
scope:
  - some/path.py
---

# fixture plan

Body text.
"""

_SIZING_TEXT = """schema: sizing-object
intent: "fixture intent"
route: dispatch
"""


def _write_plan(tmp_path: Path) -> Path:
    p = tmp_path / "fixture-plan.md"
    p.write_text(_PLAN_TEXT, encoding="utf-8")
    return p


def _write_sizing(tmp_path: Path) -> Path:
    p = tmp_path / "fixture-sizing.yaml"
    p.write_text(_SIZING_TEXT, encoding="utf-8")
    return p


def _patch_content_root(monkeypatch, tmp_path: Path) -> None:
    # Review: F3 fix — never let a CLI test resolve the live repo's content
    # root; point `residue.brief`'s one `resolve_content_root()` call at an
    # empty, unpopulated directory under `tmp_path` instead. No residue
    # segments there is fine for every test below: a bare call fail-louds
    # with a non-USAGE exit code, and a predicates-requested call reports
    # the absence in-band and still returns a well-formed envelope.
    content_root = tmp_path / "content-root"
    monkeypatch.setattr(residue_mod, "resolve_content_root", lambda: str(content_root))


# --- undetermined() sentinel shape -----------------------------------------


def test_undetermined_shape():
    result = undetermined("no --plan supplied")
    assert result == {"undetermined": True, "reason": "no --plan supplied"}


def test_undetermined_reason_is_required_positional():
    result = undetermined("some specific reason")
    assert result["reason"] == "some specific reason"
    assert result["undetermined"] is True


# --- PredicateContext.from_paths — both present -----------------------------


def test_from_paths_both_present(tmp_path):
    plan_path = _write_plan(tmp_path)
    sizing_path = _write_sizing(tmp_path)

    ctx = PredicateContext.from_paths(
        repo_root=tmp_path,
        plan_path=plan_path,
        sizing_object_path=sizing_path,
        resolved_route="spec-dispatch",
    )

    assert ctx.repo_root == tmp_path
    assert ctx.plan_path == plan_path
    assert ctx.plan_frontmatter is not None
    assert ctx.plan_frontmatter["title"] == "fixture plan"
    assert ctx.plan_body is not None
    assert "Body text." in ctx.plan_body
    assert ctx.sizing_object_path == sizing_path
    assert ctx.sizing_frontmatter is not None
    assert ctx.sizing_frontmatter["route"] == "dispatch"
    assert ctx.resolved_route == "spec-dispatch"
    assert ctx.caller_flags == {}


# --- PredicateContext.from_paths — both absent ------------------------------


def test_from_paths_both_absent(tmp_path):
    ctx = PredicateContext.from_paths(
        repo_root=tmp_path,
        plan_path=None,
        sizing_object_path=None,
        resolved_route="plan",
    )

    assert ctx.plan_path is None
    assert ctx.plan_frontmatter is None
    assert ctx.plan_body is None
    assert ctx.sizing_object_path is None
    assert ctx.sizing_frontmatter is None
    assert ctx.caller_flags == {}


# --- PredicateContext.from_paths — one absent -------------------------------


def test_from_paths_plan_only(tmp_path):
    plan_path = _write_plan(tmp_path)

    ctx = PredicateContext.from_paths(
        repo_root=tmp_path,
        plan_path=plan_path,
        sizing_object_path=None,
        resolved_route="plan",
    )

    assert ctx.plan_frontmatter is not None
    assert ctx.sizing_object_path is None
    assert ctx.sizing_frontmatter is None


def test_from_paths_sizing_only(tmp_path):
    sizing_path = _write_sizing(tmp_path)

    ctx = PredicateContext.from_paths(
        repo_root=tmp_path,
        plan_path=None,
        sizing_object_path=sizing_path,
        resolved_route="plan",
    )

    assert ctx.plan_path is None
    assert ctx.plan_frontmatter is None
    assert ctx.sizing_frontmatter is not None
    assert ctx.sizing_frontmatter["schema"] == "sizing-object"


# --- caller_flags passthrough -----------------------------------------------


def test_caller_flags_passthrough(tmp_path):
    ctx = PredicateContext.from_paths(
        repo_root=tmp_path,
        plan_path=None,
        sizing_object_path=None,
        resolved_route="plan",
        caller_flags={"arrival": "fresh_inbound"},
    )
    assert ctx.caller_flags == {"arrival": "fresh_inbound"}
    # a key never supplied is simply absent — no backfilled default
    assert "trampoline" not in ctx.caller_flags


# --- CLI: --plan / --sizing-object usage-vs-absent distinction -------------


def test_cli_plan_and_sizing_object_absent_is_not_usage_error(
    monkeypatch, capsys, tmp_path
):
    _patch_content_root(monkeypatch, tmp_path)
    # No --plan/--sizing-object at all: resolves to the existing --route-only
    # behavior, never a usage error for the two new flags being absent.
    exit_code = _dispatch_brief(["--route", "plan"])
    # residue.brief may fail BUSINESS/TRANSPORT depending on the fixture
    # content-root's (empty) residue corpus, but it must never be USAGE (2)
    # purely because --plan/--sizing-object were omitted.
    assert exit_code == _PlanAssembleExitCode.BUSINESS


def test_cli_plan_path_unresolvable_is_usage_error(tmp_path, capsys):
    missing = tmp_path / "does-not-exist.md"
    exit_code = _dispatch_brief(["--plan", str(missing)])
    assert exit_code == _PlanAssembleExitCode.USAGE
    captured = capsys.readouterr()
    assert "--plan" in captured.err


def test_cli_sizing_object_path_unresolvable_is_usage_error(tmp_path, capsys):
    missing = tmp_path / "does-not-exist.yaml"
    exit_code = _dispatch_brief(["--sizing-object", str(missing)])
    assert exit_code == _PlanAssembleExitCode.USAGE
    captured = capsys.readouterr()
    assert "--sizing-object" in captured.err


def test_cli_plan_path_resolvable_is_accepted(tmp_path, capsys, monkeypatch):
    _patch_content_root(monkeypatch, tmp_path)
    plan_path = _write_plan(tmp_path)
    exit_code = _dispatch_brief(["--plan", str(plan_path)])
    # A resolvable --plan must not itself trigger a usage error; predicates
    # were requested, so a missing residue corpus is reported in-band, not
    # fail-loud — the CLI returns SUCCESS.
    assert exit_code == _PlanAssembleExitCode.SUCCESS


def test_cli_sizing_object_path_resolvable_is_accepted(tmp_path, capsys, monkeypatch):
    _patch_content_root(monkeypatch, tmp_path)
    sizing_path = _write_sizing(tmp_path)
    exit_code = _dispatch_brief(["--sizing-object", str(sizing_path)])
    assert exit_code == _PlanAssembleExitCode.SUCCESS


def test_cli_missing_flag_value_is_usage_error():
    assert _dispatch_brief(["--plan"]) == _PlanAssembleExitCode.USAGE


# --- residue._unpack — genuinely-missing field fails loud (Review: F2 fix) --
# `_unpack` fans a Layer-0 row out to one dict entry per contract sub-field.
# A producer that forgets a documented field must not be laundered into a
# silent `None` the 60-row coverage oracle (test_residue.py) can never
# catch — see residue.py's `_unpack` docstring.


def test_unpack_raises_on_genuinely_missing_field():
    import pytest

    # A row missing a documented field entirely (producer bug) — `_unpack`
    # must fail loud (KeyError), not silently synthesize `None` for it.
    incomplete_row = {"present": True}
    with pytest.raises(KeyError):
        residue_mod._unpack(incomplete_row, "present", "path")


def test_unpack_preserves_legitimate_none_value():
    # A field the producer legitimately populated with `None` stays legal —
    # only a genuinely-ABSENT key is a failure.
    row_with_none = {"present": True, "path": None}
    result = residue_mod._unpack(row_with_none, "present", "path")
    assert result == {"present": True, "path": None}


def test_unpack_preserves_undetermined_sentinel_at_every_field():
    row = undetermined("no --plan supplied")
    result = residue_mod._unpack(row, "present", "path")
    assert result == {"present": row, "path": row}


# --- CLI: --arrival / --trampoline / --collapse-fired-this-pass ------------
# Review: caller-flags fix — wires :32a/:100/:108's previously-dead CLI seam.


def test_cli_arrival_valid_value_is_accepted(capsys, tmp_path, monkeypatch):
    _patch_content_root(monkeypatch, tmp_path)
    exit_code = _dispatch_brief(["--arrival", "fresh_inbound"])
    # No --plan/--sizing-object: bare-call shape, fails loud on the empty
    # fixture content-root.
    assert exit_code == _PlanAssembleExitCode.BUSINESS


def test_cli_arrival_invalid_value_is_usage_error(capsys):
    exit_code = _dispatch_brief(["--arrival", "bogus"])
    assert exit_code == _PlanAssembleExitCode.USAGE
    captured = capsys.readouterr()
    assert "--arrival" in captured.err


def test_cli_trampoline_true_false_are_accepted(capsys, tmp_path, monkeypatch):
    _patch_content_root(monkeypatch, tmp_path)
    assert _dispatch_brief(["--trampoline", "true"]) == _PlanAssembleExitCode.BUSINESS
    assert _dispatch_brief(["--trampoline", "false"]) == _PlanAssembleExitCode.BUSINESS


def test_cli_trampoline_invalid_value_is_usage_error(capsys):
    exit_code = _dispatch_brief(["--trampoline", "yes"])
    assert exit_code == _PlanAssembleExitCode.USAGE
    captured = capsys.readouterr()
    assert "--trampoline" in captured.err


def test_cli_collapse_fired_this_pass_true_false_are_accepted(
    capsys, tmp_path, monkeypatch
):
    _patch_content_root(monkeypatch, tmp_path)
    assert (
        _dispatch_brief(["--collapse-fired-this-pass", "true"])
        == _PlanAssembleExitCode.BUSINESS
    )
    assert (
        _dispatch_brief(["--collapse-fired-this-pass", "false"])
        == _PlanAssembleExitCode.BUSINESS
    )


def test_cli_collapse_fired_this_pass_invalid_value_is_usage_error(capsys):
    exit_code = _dispatch_brief(["--collapse-fired-this-pass", "maybe"])
    assert exit_code == _PlanAssembleExitCode.USAGE
    captured = capsys.readouterr()
    assert "--collapse-fired-this-pass" in captured.err


def test_cli_caller_flags_reach_predicate_context_undetermined_rows(
    tmp_path, capsys, monkeypatch
):
    _patch_content_root(monkeypatch, tmp_path)
    # End-to-end: supplying --arrival/--trampoline/--collapse-fired-this-pass
    # resolves :32a/:100/:108 off the shipped CLI instead of undetermined.
    plan_path = _write_plan(tmp_path)
    exit_code = _dispatch_brief(
        [
            "--plan",
            str(plan_path),
            "--arrival",
            "return_edge",
            "--trampoline",
            "true",
            "--collapse-fired-this-pass",
            "false",
        ]
    )
    assert exit_code != _PlanAssembleExitCode.USAGE
    captured = capsys.readouterr()
    import json

    envelope = json.loads(captured.out)
    gates = envelope["gates"]
    assert gates["triage"]["sizing_object"]["arrival"] == "return_edge"
    assert gates["substrate"]["trampoline"]["dec4_signal"] is True
    assert gates["substrate"]["collapse"]["fired_this_pass"] is False
    assert _dispatch_brief(["--sizing-object"]) == _PlanAssembleExitCode.USAGE
