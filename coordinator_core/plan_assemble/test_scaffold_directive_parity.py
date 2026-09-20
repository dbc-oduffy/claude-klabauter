"""coordinator_core.plan_assemble.test_scaffold_directive_parity -- C4's
parity pin for the `plan_assemble` host.

Purpose: `plan_assemble` is an in-scope host for C0's checked-in table
(`coordinator_core/ops/doctype_hosts.py`), emitted row (type="plan",
ceremony="plan-assemble", module="coordinator_core.plan_assemble"). This
pin follows the plan's § Test surface "Parity/pin per emitted type" shape
(`coordinator_core/frontmatter/tests/test_plan_scaffold_census_parity.py`'s
precedent, same idiom `roadmap_planning_assemble`'s C3 pin already uses):
it calls `plan_assemble.brief()` -- never hand-assembles a directive -- and
checks the resulting `args` against `coordinator-doc-new`'s OWN real
argument parser, so a future required-flag addition on that CLI fails this
test rather than silently authoring an invalid scaffold.

Loaded by file path (`importlib.machinery.SourceFileLoader`), same idiom as
`roadmap_planning_assemble`'s pin: `coordinator-doc-new.py` is imported for
its `_build_parser` alone -- `main()` is never invoked, so this test writes
nothing to disk and spawns no subprocess.

Residue-corpus fixtures (`_make_residue_dir`/`_patch_content_root`) are
IMPORTED from `test_residue.py`, never copied (same import discipline
`test_residue_admission.py` already establishes for this package) -- two
drifting definitions of the same residue corpus is a worse failure than the
import coupling. `residue_mod.show_toplevel` is pinned in every test that
supplies `sizing_object_path` (predicates requested), so this module spawns
no `git` subprocess either, mirroring `test_residue_admission.py`'s own
"pins show_toplevel" discipline -- no `spawns_process` marker needed.

Covers (AC2/AC3/AC4, this module's slice):
  - `brief()`, called with a sizing-object path whose `intent:`/`route:`
    resolve the route to `"plan"`, emits a `d-scaffold-plan` directive
    whose `cli == "coordinator-doc-new"` and whose `args` parse clean
    under the real parser, with `--title` computed from the sizing
    object's own `intent:` field (never a fresh free-text argument) and
    exactly one of `--sizing-object`/`--no-sizing-object` present.
  - `brief()`, called with `explicit_route="spec-dispatch"`, emits NO
    `d-scaffold-plan` directive -- `directives == []`, byte-identical to
    every pre-C4 caller on that route.
  - `brief()`, called with no sizing object at all (route defaults to
    `"plan"`), emits `--no-sizing-object` and omits `--title` entirely
    (never a placeholder minted here).
  - Every emitted directive's `--out` resolves under the repo root
    (AC4's containment, exercised end-to-end through this host).

Negative-spec: does NOT re-assert the shared constructor's own unit-level
behaviour (omit-when-None, `--out` escape rejection) -- that is C2's
`roadmap_planning_assemble/tests/test_scaffold_directive.py`, not
duplicated here. Does NOT assert on `already_satisfied` truthiness for an
on-disk fixture -- the constructor's own existence-stat behaviour is C2's
unit surface; this pin only asserts the key is present and boolean.

Spec backlink: docs/plans/2026-09-11-document-scaffolding-is-emitted-not-
remembered.md, chunk C4.

Run:
    pytest coordinator_core/plan_assemble/test_scaffold_directive_parity.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
from pathlib import Path

import pytest

import coordinator_core.plan_assemble as plan_assemble
from coordinator_core.plan_assemble import residue as residue_mod
from coordinator_core.plan_assemble.test_residue import (
    _make_residue_dir,
    _patch_content_root,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLI_PATH = _REPO_ROOT / "coordinator" / "bin" / "coordinator-doc-new.py"


def _load_doc_new_parser():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_plan_assemble_scaffold_parity_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_plan_assemble_scaffold_parity_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod._build_parser()


_parser = _load_doc_new_parser()


def _directive(directives: list[dict], directive_id: str) -> dict:
    for d in directives:
        if d["id"] == directive_id:
            return d
    raise AssertionError(f"no directive with id {directive_id!r} in {directives!r}")


def _write_sizing_object(tmp_path: Path, *, intent: str = "Do the thing", route: str = "plan") -> Path:
    sizing_path = tmp_path / "sizing.yaml"
    sizing_path.write_text(
        f"schema: sizing-object\nintent: {intent!r}\nroute: {route}\n",
        encoding="utf-8",
    )
    return sizing_path


class TestPlanScaffoldParity:
    def test_emits_coordinator_doc_new_cli(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        content_root = _make_residue_dir(tmp_path)
        _patch_content_root(monkeypatch, content_root)
        monkeypatch.setattr(residue_mod, "show_toplevel", lambda: str(tmp_path))

        sizing_path = _write_sizing_object(tmp_path)
        result = plan_assemble.brief(sizing_object_path=sizing_path)

        directive = _directive(result["directives"], "d-scaffold-plan")
        assert directive["cli"] == "coordinator-doc-new"

    def test_args_parse_under_the_real_parser(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        content_root = _make_residue_dir(tmp_path)
        _patch_content_root(monkeypatch, content_root)
        monkeypatch.setattr(residue_mod, "show_toplevel", lambda: str(tmp_path))

        sizing_path = _write_sizing_object(tmp_path)
        result = plan_assemble.brief(sizing_object_path=sizing_path)

        directive = _directive(result["directives"], "d-scaffold-plan")
        args = _parser.parse_args(directive["args"])
        assert args.doc_type == "plan"

    def test_title_is_computed_from_sizing_object_intent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        content_root = _make_residue_dir(tmp_path)
        _patch_content_root(monkeypatch, content_root)
        monkeypatch.setattr(residue_mod, "show_toplevel", lambda: str(tmp_path))

        sizing_path = _write_sizing_object(tmp_path, intent="Ship the scaffold emitter")
        result = plan_assemble.brief(sizing_object_path=sizing_path)

        directive = _directive(result["directives"], "d-scaffold-plan")
        args = _parser.parse_args(directive["args"])
        assert args.title == "Ship the scaffold emitter"

    def test_sizing_object_present_emits_sizing_object_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        content_root = _make_residue_dir(tmp_path)
        _patch_content_root(monkeypatch, content_root)
        monkeypatch.setattr(residue_mod, "show_toplevel", lambda: str(tmp_path))

        sizing_path = _write_sizing_object(tmp_path)
        result = plan_assemble.brief(sizing_object_path=sizing_path)

        directive = _directive(result["directives"], "d-scaffold-plan")
        args = _parser.parse_args(directive["args"])
        assert args.sizing_object == str(sizing_path)
        assert args.no_sizing_object is False

    def test_sizing_object_absent_emits_no_sizing_object_flag_and_no_title(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        content_root = _make_residue_dir(tmp_path)
        _patch_content_root(monkeypatch, content_root)

        result = plan_assemble.brief()

        directive = _directive(result["directives"], "d-scaffold-plan")
        args = _parser.parse_args(directive["args"])
        assert args.no_sizing_object is True
        assert args.sizing_object is None
        assert args.title is None

    def test_out_resolves_inside_repo_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        content_root = _make_residue_dir(tmp_path)
        _patch_content_root(monkeypatch, content_root)
        monkeypatch.setattr(residue_mod, "show_toplevel", lambda: str(tmp_path))

        sizing_path = _write_sizing_object(tmp_path)
        result = plan_assemble.brief(sizing_object_path=sizing_path)

        directive = _directive(result["directives"], "d-scaffold-plan")
        args = _parser.parse_args(directive["args"])
        resolved = (_REPO_ROOT / args.out).resolve()
        assert str(resolved).startswith(str(_REPO_ROOT.resolve()) + os.sep)

    def test_already_satisfied_key_is_boolean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        content_root = _make_residue_dir(tmp_path)
        _patch_content_root(monkeypatch, content_root)
        monkeypatch.setattr(residue_mod, "show_toplevel", lambda: str(tmp_path))

        sizing_path = _write_sizing_object(tmp_path)
        result = plan_assemble.brief(sizing_object_path=sizing_path)

        directive = _directive(result["directives"], "d-scaffold-plan")
        assert isinstance(directive["already_satisfied"], bool)


class TestSpecDispatchRouteScaffoldsNothing:
    def test_spec_dispatch_route_emits_no_plan_directive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        content_root = _make_residue_dir(tmp_path)
        _patch_content_root(monkeypatch, content_root)

        result = plan_assemble.brief(explicit_route="spec-dispatch")

        ids = {d["id"] for d in result["directives"]}
        assert "d-scaffold-plan" not in ids
        assert result["directives"] == []
