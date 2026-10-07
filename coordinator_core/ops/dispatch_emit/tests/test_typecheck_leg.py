"""typecheck_leg composes one tsc --noEmit per distinct governing tsconfig, never a pass without tsc."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import typecheck_leg as mod
from coordinator_core.ops.dispatch_emit.typecheck_leg import typecheck_leg, typecheck_prompt_clause


def _touch(root, rel):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_npx(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)


def test_one_tsconfig_resolves_the_repo_local_tsc(tmp_path):
    _touch(tmp_path, "tsconfig.json")
    _touch(tmp_path, "node_modules/.bin/tsc")
    leg = typecheck_leg(["src/app/api/[entityId]/route.ts", "src/b.tsx"], tmp_path)
    assert leg.commands == ("node_modules/.bin/tsc --noEmit -p .",)
    assert leg.not_run == ()


def test_two_packages_get_one_command_each_with_a_hoisted_tsc(tmp_path):
    _touch(tmp_path, "node_modules/.bin/tsc.cmd")
    _touch(tmp_path, "packages/a/tsconfig.json")
    _touch(tmp_path, "packages/b/tsconfig.json")
    leg = typecheck_leg(
        ["packages/a/src/x.ts", "packages/a/src/y.ts", "packages/b/z.tsx"], tmp_path
    )
    assert leg.commands == (
        "node_modules/.bin/tsc.cmd --noEmit -p packages/a",
        "node_modules/.bin/tsc.cmd --noEmit -p packages/b",
    )


def test_no_ts_files_or_no_governing_tsconfig_composes_no_leg(tmp_path):
    _touch(tmp_path, "tsconfig.json")
    assert typecheck_leg(["a.py", "docs/x.md", "web/app.js"], tmp_path) is None
    assert typecheck_leg(["x.ts"], tmp_path / "other") is None
    assert typecheck_leg(["x.ts"], None) is None


def test_no_tsc_is_not_run_with_a_reason_never_a_pass(tmp_path):
    _touch(tmp_path, "tsconfig.json")
    leg = typecheck_leg(["x.ts"], tmp_path)
    assert leg.commands == ()
    assert leg.not_run[0][0] == "."
    clause = typecheck_prompt_clause(leg)
    assert "not_run" in clause and "build_clean null" in clause


def test_npx_no_install_is_the_fallback_when_no_local_tsc(tmp_path, monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/npx")
    _touch(tmp_path, "app/tsconfig.json")
    leg = typecheck_leg(["app/x.ts"], tmp_path)
    assert leg.commands == ("npx --no-install tsc --noEmit -p app",)


def test_clause_makes_a_nonzero_exit_fail_the_phase(tmp_path):
    _touch(tmp_path, "tsconfig.json")
    _touch(tmp_path, "node_modules/.bin/tsc")
    clause = typecheck_prompt_clause(typecheck_leg(["x.ts"], tmp_path))
    assert "status `fail`" in clause and "`node_modules/.bin/tsc --noEmit -p .`" in clause


def test_compose_script_folds_the_leg_into_the_terminal_test_prompt(tmp_path):
    from coordinator_core.ops.dispatch_emit.tests.test_emit import REVIEW_KW, _wave_row
    from coordinator_core.ops.dispatch_emit.emit import compose_script

    _touch(tmp_path, "tsconfig.json")
    _touch(tmp_path, "node_modules/.bin/tsc")
    waves = [[_wave_row("C1", ["src/a.ts"])]]
    script = compose_script(waves, name="wf", description="ts", repo_root=tmp_path, **REVIEW_KW)
    assert "node_modules/.bin/tsc --noEmit -p ." in script
    assert "test:terminal" in script


def test_row_build_gate_rows_are_skipped_by_the_default_tsc_leg(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit.emit import compose_script, derive_plan_context
    from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
    from coordinator_core.ops.dispatch_emit.tests.test_emit import REVIEW_KW, _wave_row

    _touch(tmp_path, "tsconfig.json")
    _touch(tmp_path, "node_modules/.bin/tsc")
    _touch(tmp_path, "web/tsconfig.json")
    plan = (
        "---\ntitle: P\nrow_build_gate:\n"
        "  - when: {change_kind: gated-kind}\n    command: 'echo k'\n"
        "  - when: {surface_glob: 'web/**/*.ts'}\n    command: 'echo g'\n"
        "---\n\n# P\n\n## Goal\n\nG.\n\n## Tasks\n\nx\n"
    )
    monkeypatch.setattr(mod.shutil, "which", lambda name, path=None: None)
    ctx = derive_plan_context(plan, fallback_title="p")
    by_kind = WaveRow(
        id="C1", title="t", surface="gated_kind.ts", writes=["gated_kind.ts"],
        reads=[], depends_on=[], change_kind="gated-kind",
    )
    by_glob = _wave_row("C2", ["web/g.ts"])
    open_row = _wave_row("C3", ["open.ts"])

    def script(rows):
        return compose_script(
            [rows], name="wf", description="ts", repo_root=tmp_path,
            plan_context=ctx, **REVIEW_KW,
        )

    assert "tsc --noEmit" not in script([by_kind, by_glob])
    assert "node_modules/.bin/tsc --noEmit -p ." in script([by_kind, by_glob, open_row])
