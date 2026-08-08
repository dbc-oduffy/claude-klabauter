"""
coordinator_core.test_backlog_grind_assemble -- co-located pytest for
coordinator_core.backlog_grind_assemble (compute half) + .apply (mutating
half) + .directives (shared directive builders).

RED AT AUTHORING TIME, ON PURPOSE (D-5, AC21). This test imports
`coordinator_core.backlog_grind_assemble`, `.apply`, and `.directives` --
none of which exist yet at authoring time (C1 lands before C2/C3/C3a-e/C4).
Collection of this file therefore fails with `ModuleNotFoundError` until
those chunks land; that failure IS the regression net this chunk exists to
plant, not a bug to fix here. Do NOT stub the missing modules to make this
file collect -- see the plan's own D-5 and the chunk's hard constraints.

Mirrors the `test_baton_assemble.py` idiom: import the module directly,
exercise it in-process against `tmp_path` fixtures (no subprocess
round-trip to a real CLI), autouse-stub `resolve_operator_config`. Covers:

  (a) the 8-key envelope -- `artifact`/`preflight`/`gates`/`directives`/
      `judgment_points`/`decisions`/`narration`/`next_move` -- built via the
      SHARED constructors (`build_envelope`/`_emit`/`ENVELOPE_KEYS`/
      `extend_exit_codes` from `...decision_object.envelope`,
      `build_judgment_point`/`build_untrusted_gate_judgment_point`/
      `build_disposition` from `...decision_object.judgment`), never a
      hand-rolled dict (AC1) -- plus the AC1 grep proving no local
      re-derivation of those constructor names exists under the package.
  (b) `brief()` is read-only: mutates no input, writes no disk (AC2).
  (c) cadence-parameter branching -- each of the five surface cadences
      (`bug-blitz`, `mise-en-place`, `bug-sweep`, `debt-triage`, `dogfood`)
      routes to its own reader only; an unrecognized cadence raises.
  (d) a CLI smoke per subcommand (`brief`/`apply`) + the usage-error path.
  (e) a spy proving `brief()` calls `resolve_operator_config()` (AC5)
      rather than re-deriving its own roots.
  (f) `apply_base` runner WIRING only (closed-dispatch rejection, the real
      dispatch table resolving every cli this module names) -- never the
      runner's own internals a second time; those are covered directly by
      `coordinator_core/contract/test_apply_base.py`.
  (g) AC6 -- every queue read routes through `queue_family.load_family_records`;
      a grep proving no `Path(...).glob`/`yaml.safe_load` exists under the
      package.
  (h) AC7 -- `stage_and_commit`'s `granularity: per-item | per-wave` is a
      named POLICY, not a bare cardinality knob (D-3): per-item yields N
      commits for N items and re-verifies the branch before EVERY commit;
      per-wave yields 1 commit for the same N items and re-verifies the
      branch ONCE; a simulated mid-loop branch flip HALTS the wave rather
      than committing the remaining items.

Spec backlink: example-doctrine-repo docs/plans/2026-07-26-backlog-grind-computed-frontage.md,
chunk C1 (depends on nothing; C3/C3a-C3e/C4 depend on this test stabilizing
green -- see D-5, AC21, and the file-overlap/executable-shape sections).

Run: python -m pytest coordinator_core/test_backlog_grind_assemble.py -q
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

import coordinator_core.backlog_grind_assemble as bga
import coordinator_core.backlog_grind_assemble.apply as bga_apply
import coordinator_core.backlog_grind_assemble.directives as bga_directives
import coordinator_core.backlog_grind_assemble.verifier as bga_verifier
from coordinator_core.contract.decision_object.envelope import ENVELOPE_KEYS
from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_judgment_point,
    build_untrusted_gate_judgment_point,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_DIR = _REPO_ROOT / "coordinator_core" / "backlog_grind_assemble"

#: The five surface cadences (D-2's "one assembler ... call one
#: cadence-parameterized assembler" -- cadence here is which of the five
#: mirror surfaces is asking, mirroring orient_assemble's `CADENCES`
#: naming convention but over a disjoint surface set rather than a
#: session/day/week severity knob). One per C3a-C3e reader module.
_CADENCES = ("bug-blitz", "mise-en-place", "bug-sweep", "debt-triage", "dogfood")

_FAKE_OPERATOR_CONFIG = {
    "settings_home": "/fake/settings-home",
    "claude_klabauter_bin": "/fake/settings-home/bin",
    "claude_klabauter_root": "/fake/claude-klabauter-root",
    "doe_root": "/fake/doe-root",
}


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    """Every test gets a fixed, machine-independent operator config unless a
    test explicitly monkeypatches its own spy -- brief()'s own call to
    resolve_operator_config() must never depend on THIS dev machine's real
    settings-home layout."""
    monkeypatch.setattr(bga, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))


def _code_string_literals(source: str) -> list[str]:
    """Every string literal in `source` that is NOT a module/class/function
    docstring.

    The negative-spec convention this repo writes to means the retired
    machinery is NAMED in prose -- "does NOT read `MISE_RUN_ID`", "no
    `merge-base --is-ancestor` call anywhere below". A plain `in source`
    grep therefore reads a warning as a violation and would push the next
    author to delete the warning. These checks assert over executable code
    only, so prose that says what the module refuses to do is free to say
    it."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    ]


def _stub_reader(
    monkeypatch,
    module: Any,
    *,
    only_for_cadence: str,
    directive_id: str,
    jp_id: str,
    calls: Optional[list] = None,
) -> None:
    """Patch `module.collect` to self-gate exactly the way C3a-C3e's real
    readers are specced to: contribute one marker directive + one marker
    judgment point only when called with `only_for_cadence`, an empty
    ReaderResult-shaped object otherwise. The seam (C3) is specced to call
    every reader unconditionally for every cadence and trust each reader
    to self-gate -- this stub exercises exactly that contract without
    depending on any real reader's own (separately tested) logic.

    The `run_id` keyword is accepted by every reader on the same terms
    (2026-08-04 carrier ratification): the seam threads it uniformly and a
    reader with no use for it ignores it, so this stub does too. `calls`
    records `(cadence, run_id)` per invocation, which is what the seam's
    uniform-threading test asserts over.

    Returns a duck-typed `SimpleNamespace(directives=[...],
    judgment_points=[...])` rather than importing a real `ReaderResult`
    type -- this test pins the SHAPE the seam concatenates, not the
    concrete dataclass C2/C3 choose to carry it in.
    """

    def _collect(cadence: str, *, run_id: Optional[str] = None):
        if calls is not None:
            calls.append((cadence, run_id))
        if cadence != only_for_cadence:
            return SimpleNamespace(directives=[], judgment_points=[])
        directive = {
            "id": directive_id,
            "cli": "commit-per-wave",
            "args": [],
            "depends_on": None,
            "already_satisfied": False,
        }
        jp = build_untrusted_gate_judgment_point(
            id=jp_id,
            question=f"{only_for_cadence}: proceed?",
            dispositions=[build_disposition("proceed", resolves=[directive_id])],
            evidence="stub evidence",
            reason="insufficient-evidence",
        )
        return SimpleNamespace(directives=[directive], judgment_points=[jp])

    monkeypatch.setattr(module, "collect", _collect)


def _stub_all_readers(monkeypatch, calls: Optional[list] = None) -> None:
    for cadence in _CADENCES:
        reader = getattr(bga, f"readers_{cadence.replace('-', '_')}")
        _stub_reader(
            monkeypatch,
            reader,
            only_for_cadence=cadence,
            directive_id=f"d-{cadence}",
            jp_id=f"j-{cadence}",
            calls=calls,
        )


# ---------------------------------------------------------------------------
# (a) decision-object key shapes -- AC1
# ---------------------------------------------------------------------------


class TestDecisionObjectKeyShapes:
    def test_envelope_has_exactly_the_8_canonical_keys(self, tmp_path, monkeypatch):
        _stub_all_readers(monkeypatch)
        decision = bga.brief("bug-blitz", repo_root=tmp_path).decision_object
        assert set(decision.keys()) == set(ENVELOPE_KEYS)

    def test_directive_shape_has_id_cli_args_depends_on(self, tmp_path, monkeypatch):
        _stub_all_readers(monkeypatch)
        decision = bga.brief("bug-blitz", repo_root=tmp_path).decision_object
        assert decision["directives"], "expected at least one directive"
        for directive in decision["directives"]:
            assert {"id", "cli", "args", "depends_on", "already_satisfied"} <= set(directive.keys())
            assert isinstance(directive["id"], str)
            assert isinstance(directive["cli"], str)
            assert isinstance(directive["args"], list)

    def test_judgment_point_shape_has_dispositions_recommendation_reason(self, tmp_path, monkeypatch):
        _stub_all_readers(monkeypatch)
        decision = bga.brief("bug-blitz", repo_root=tmp_path).decision_object
        assert decision["judgment_points"], "expected at least one judgment point"
        for jp in decision["judgment_points"]:
            assert "dispositions" in jp
            assert "recommendation" in jp
            assert "reason" in jp
            for disposition in jp["dispositions"]:
                assert "value" in disposition
                assert "resolves" in disposition


class TestAC1NoLocalConstructorRederivation:
    """AC1's own pinned grep: `backlog_grind_assemble` must never re-derive
    `build_envelope`/`_emit`/`build_judgment_point` locally -- it imports
    and calls the shared Tier-B constructors, full stop."""

    def test_grep_no_local_envelope_or_judgment_constructors(self):
        assert _PACKAGE_DIR.is_dir(), (
            f"{_PACKAGE_DIR} does not exist yet -- this assertion is "
            "meaningless until C2/C3/C3a-C3e land; the whole file is "
            "expected to fail at COLLECTION before this point is reached "
            "(D-5/AC21 red-before-green)."
        )
        pattern = re.compile(r"def build_envelope|def _emit|def build_judgment_point")
        offending: list[str] = []
        for py_file in _PACKAGE_DIR.rglob("*.py"):
            text = py_file.read_text(encoding="utf-8")
            if pattern.search(text):
                offending.append(str(py_file.relative_to(_REPO_ROOT)))
        assert offending == [], (
            "backlog_grind_assemble re-derives a Tier-B constructor locally "
            f"in: {offending} -- import from "
            "coordinator_core.contract.decision_object.{envelope,judgment} instead"
        )


# ---------------------------------------------------------------------------
# (b) brief() is read-only -- AC2
# ---------------------------------------------------------------------------


class TestBriefIsReadOnly:
    def test_brief_mutates_no_disk_under_tmp_path(self, tmp_path, monkeypatch):
        _stub_all_readers(monkeypatch)
        (tmp_path / "state" / "bug-backlog").mkdir(parents=True)
        marker = tmp_path / "state" / "bug-backlog" / "marker.yaml"
        marker.write_text("status: open\n", encoding="utf-8")
        original = marker.read_text(encoding="utf-8")

        files_before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
        bga.brief("bug-blitz", repo_root=tmp_path)
        files_after = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())

        assert marker.read_text(encoding="utf-8") == original
        assert files_before == files_after


# ---------------------------------------------------------------------------
# (c) cadence-parameter branching
# ---------------------------------------------------------------------------


class TestCadenceBranching:
    @pytest.mark.parametrize("cadence", _CADENCES)
    def test_each_cadence_routes_only_to_its_own_reader(self, tmp_path, monkeypatch, cadence):
        _stub_all_readers(monkeypatch)
        decision = bga.brief(cadence, repo_root=tmp_path).decision_object
        directive_ids = {d["id"] for d in decision["directives"]}
        jp_ids = {jp["id"] for jp in decision["judgment_points"]}
        assert directive_ids == {f"d-{cadence}"}
        assert jp_ids == {f"j-{cadence}"}

    def test_unrecognized_cadence_raises_value_error(self, tmp_path, monkeypatch):
        _stub_all_readers(monkeypatch)
        with pytest.raises(ValueError):
            bga.brief("bogus-cadence", repo_root=tmp_path)

    def test_cadences_constant_is_exactly_the_five_surfaces(self):
        assert set(bga.CADENCES) == set(_CADENCES)


class TestRunIdThreadsUniformlyThroughTheSeam:
    """The seam constraint the 2026-08-04 carrier had to satisfy: `--run-id`
    is a new parameter, NOT a per-surface branch. `__init__.py` must not
    learn that the flag is a mise-only concept -- it forwards the value to
    all five readers for every cadence and lets each self-gate, exactly as
    it already does for `cadence` itself. (An earlier `--phase` flag was
    rejected for failing this.)"""

    @pytest.mark.parametrize("cadence", _CADENCES)
    def test_every_reader_receives_the_run_id_for_every_cadence(
        self, tmp_path, monkeypatch, cadence
    ):
        calls: list = []
        _stub_all_readers(monkeypatch, calls)

        bga.brief(cadence, run_id="run-42", repo_root=tmp_path)

        assert len(calls) == len(_CADENCES), "the seam must call all five readers"
        assert {run_id for _cadence, run_id in calls} == {"run-42"}, (
            "the run id must reach every reader identically -- a reader "
            "receiving None while another receives the value is a per-surface "
            "branch at the seam"
        )

    def test_absent_run_id_reaches_every_reader_as_none(self, tmp_path, monkeypatch):
        calls: list = []
        _stub_all_readers(monkeypatch, calls)

        bga.brief("mise-en-place", repo_root=tmp_path)

        assert {run_id for _cadence, run_id in calls} == {None}

    def test_the_seam_never_branches_on_the_run_id_or_on_a_cadence_name(self):
        # Structural, because the behavioural tests above are satisfied by a
        # branch that happens to forward the same value on both arms. Scoped
        # to `brief()` -- the seam proper. `main()` legitimately conditions
        # on `run_id` to reject a repeated `--run-id`, which is argv parsing,
        # not surface dispatch.
        source = (_PACKAGE_DIR / "__init__.py").read_text(encoding="utf-8")
        brief_fn = next(
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef) and node.name == "brief"
        )
        for node in ast.walk(brief_fn):
            if not isinstance(node, (ast.If, ast.IfExp)):
                continue
            test_src = ast.dump(node.test)
            assert "'run_id'" not in test_src and '"run_id"' not in test_src, (
                "the seam branches on run_id -- interpreting the value is the "
                "reader's job, forwarding it is the seam's"
            )
            for cadence in _CADENCES:
                assert repr(cadence) not in test_src, (
                    f"the seam branches on the cadence literal {cadence!r} -- "
                    "cadence self-gating lives inside each reader"
                )

    def test_the_seam_carries_no_mise_specific_vocabulary(self):
        # `__init__.py` must not know that `--run-id` names an inventory
        # record: the moment it does, the next surface's run parameter needs
        # a second branch here.
        code = " | ".join(
            _code_string_literals((_PACKAGE_DIR / "__init__.py").read_text(encoding="utf-8"))
        )
        assert "mise-inventory" not in code
        assert "start_sha" not in code


# ---------------------------------------------------------------------------
# (d) CLI smoke + usage-error path
# ---------------------------------------------------------------------------


class TestCliSmoke:
    def test_brief_subcommand_smoke(self, tmp_path, monkeypatch, capsys):
        import os

        _stub_all_readers(monkeypatch)
        old_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            exit_code = bga.main(["brief", "bug-blitz"])
        finally:
            os.chdir(old_cwd)
        assert exit_code in (bga.EXIT_OK, bga.EXIT_TRANSPORT_FAIL, bga.EXIT_USAGE)

    def test_no_subcommand_is_usage_error(self, capsys):
        exit_code = bga.main([])
        assert exit_code == bga.EXIT_USAGE
        err = capsys.readouterr().err
        assert "usage" in err

    def test_unknown_subcommand_is_usage_error(self, capsys):
        exit_code = bga.main(["bogus"])
        assert exit_code == bga.EXIT_USAGE

    def test_brief_missing_cadence_is_usage_error(self, capsys):
        exit_code = bga.main(["brief"])
        assert exit_code == bga.EXIT_USAGE

    def test_brief_bogus_cadence_is_usage_error(self, capsys):
        exit_code = bga.main(["brief", "not-a-real-cadence"])
        assert exit_code == bga.EXIT_USAGE

    @pytest.mark.parametrize("cadence", _CADENCES)
    def test_run_id_flag_is_accepted_after_any_cadence_and_forwarded(
        self, monkeypatch, cadence
    ):
        # Cadence-agnostic by construction: the CLI does not know which
        # surfaces read the value, so it refuses none of them.
        seen: dict = {}

        def _fake_brief(cadence_arg, *, run_id=None, repo_root=None):
            seen["cadence"] = cadence_arg
            seen["run_id"] = run_id
            return bga.BriefResult(
                decision_object={}, cadence=cadence_arg, run_id=run_id
            )

        monkeypatch.setattr(bga, "brief", _fake_brief)

        assert bga.main(["brief", cadence, "--run-id", "run-7"]) == bga.EXIT_OK
        assert seen == {"cadence": cadence, "run_id": "run-7"}

    def test_run_id_flag_with_no_value_is_a_usage_error(self, capsys):
        # Never a silently-ignored argument: a caller who believes they named
        # a run and did not is the exact caller the loud failure protects.
        assert bga.main(["brief", "mise-en-place", "--run-id"]) == bga.EXIT_USAGE
        assert "--run-id" in capsys.readouterr().err

    def test_a_repeated_run_id_flag_is_a_usage_error(self):
        assert (
            bga.main(["brief", "mise-en-place", "--run-id", "a", "--run-id", "b"])
            == bga.EXIT_USAGE
        )

    def test_an_unrecognized_trailing_token_is_a_usage_error(self):
        assert bga.main(["brief", "mise-en-place", "run-1"]) == bga.EXIT_USAGE
        assert bga.main(["brief", "mise-en-place", "--phase", "6"]) == bga.EXIT_USAGE

    def test_apply_subcommand_missing_args_is_usage_error(self, capsys):
        exit_code = bga_apply.main_apply([])
        assert exit_code == bga_apply.APPLY_EXIT_TRANSPORT_FAIL
        err = capsys.readouterr().err
        assert "usage" in err


# ---------------------------------------------------------------------------
# (d.1) `apply <cadence> --run-id` passthrough -- review finding F1
# (2026-08-04 review-integration pass): apply.py's own recompute of
# brief() previously never threaded --run-id, so mise Phase-6 resolved to
# the "missing --run-id" judgment point on every `apply mise-en-place`
# call once mise-inventory records existed. `main_apply` now parses its
# own `--run-id`, mirroring `main()`'s argv-loop semantics (missing value
# or a repeat of the flag is a usage error).
# ---------------------------------------------------------------------------


class TestApplyRunIdPassthrough:
    def test_supplied_run_id_reaches_the_recomputed_brief(self, monkeypatch):
        seen: dict = {}

        def _fake_brief(cadence_arg, *, run_id=None, repo_root=None):
            seen["cadence"] = cadence_arg
            seen["run_id"] = run_id
            return bga.BriefResult(
                decision_object={"cadence": cadence_arg, "run_id": run_id},
                cadence=cadence_arg,
                run_id=run_id,
            )

        monkeypatch.setattr(bga_apply, "brief", _fake_brief)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-run-id-passthrough")

        exit_code = bga_apply.main_apply(["mise-en-place", "--run-id", "run-42"])

        assert exit_code == bga_apply.APPLY_EXIT_OK
        assert seen == {"cadence": "mise-en-place", "run_id": "run-42"}

    def test_absent_run_id_behaves_as_before(self, monkeypatch):
        seen: dict = {}

        def _fake_brief(cadence_arg, *, run_id=None, repo_root=None):
            seen["run_id"] = run_id
            return bga.BriefResult(
                decision_object={}, cadence=cadence_arg, run_id=run_id
            )

        monkeypatch.setattr(bga_apply, "brief", _fake_brief)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-run-id-absent")

        exit_code = bga_apply.main_apply(["mise-en-place"])

        assert exit_code == bga_apply.APPLY_EXIT_OK
        assert seen == {"run_id": None}

    def test_run_id_with_no_value_is_a_usage_error(self, capsys):
        assert (
            bga_apply.main_apply(["mise-en-place", "--run-id"])
            == bga_apply.APPLY_EXIT_TRANSPORT_FAIL
        )
        assert "usage" in capsys.readouterr().err

    def test_a_repeated_run_id_flag_is_a_usage_error(self):
        assert (
            bga_apply.main_apply(
                ["mise-en-place", "--run-id", "a", "--run-id", "b"]
            )
            == bga_apply.APPLY_EXIT_TRANSPORT_FAIL
        )


# ---------------------------------------------------------------------------
# (e) resolve_operator_config spy -- AC5
# ---------------------------------------------------------------------------


class TestResolveOperatorConfigSpy:
    def test_brief_calls_resolve_operator_config_at_least_once(self, tmp_path, monkeypatch):
        _stub_all_readers(monkeypatch)
        calls: list[int] = []

        def _spy():
            calls.append(1)
            return dict(_FAKE_OPERATOR_CONFIG)

        monkeypatch.setattr(bga, "resolve_operator_config", _spy)
        bga.brief("bug-blitz", repo_root=tmp_path)

        assert len(calls) >= 1

    def test_brief_never_defines_its_own_settings_home_helper(self):
        assert not hasattr(bga, "_settings_home")
        assert not hasattr(bga, "_resolve_settings_home")


# ---------------------------------------------------------------------------
# (f) apply_base runner WIRING only -- never the runner's internals a
# second time (already covered directly by
# coordinator_core/contract/test_apply_base.py).
# ---------------------------------------------------------------------------


class TestApplyBaseWiring:
    def test_unrecognized_cli_raises_before_any_directive_dispatches(self):
        with pytest.raises(bga_apply.apply_base.UnrecognizedDirective):
            bga_apply.apply_base.resolve_cli(bga_apply._CLI_DISPATCH, "rm")

    def test_dispatch_table_contains_the_two_commit_verbs(self):
        # D-3: the commit verbs are internal, named entries in apply.py's
        # own closed dispatch table -- never coordinator-safe-commit by name.
        assert "commit-per-item" in bga_apply._CLI_DISPATCH
        assert "commit-per-wave" in bga_apply._CLI_DISPATCH
        assert "coordinator-safe-commit" not in bga_apply._CLI_DISPATCH

    def test_no_directive_arg_reaches_a_subprocess_argv_via_getattr_or_importlib(self):
        # Static negative-spec check (AC3 spirit, scoped to what C1 can
        # assert without re-testing apply_base's own internals): the
        # dispatch table is a literal dict, not built via getattr/importlib.
        source = (_PACKAGE_DIR / "apply.py").read_text(encoding="utf-8") if (
            _PACKAGE_DIR / "apply.py"
        ).is_file() else ""
        assert "getattr(sys.modules" not in source
        assert "importlib.import_module" not in source or "getattr" not in source

    def test_drop_inverse_exists_and_is_callable(self):
        assert hasattr(bga_apply, "drop")
        assert callable(bga_apply.drop)


# ---------------------------------------------------------------------------
# (g) AC6 -- every queue read routes through queue_family.load_family_records
# ---------------------------------------------------------------------------


class TestAC6NoDirectQueueParsing:
    def test_grep_no_path_glob_or_yaml_safe_load_under_the_package(self):
        assert _PACKAGE_DIR.is_dir(), (
            f"{_PACKAGE_DIR} does not exist yet -- meaningless until "
            "C3a-C3e land; expected to fail at collection first (D-5)."
        )
        pattern = re.compile(r"Path\([^)]*\)\.glob|yaml\.safe_load")
        offending: list[str] = []
        for py_file in _PACKAGE_DIR.rglob("*.py"):
            if py_file.name.startswith("test_"):
                continue
            text = py_file.read_text(encoding="utf-8")
            if pattern.search(text):
                offending.append(str(py_file.relative_to(_REPO_ROOT)))
        assert offending == [], (
            f"backlog_grind_assemble reimplements the queue_family read seam "
            f"in: {offending} -- route through "
            "coordinator_core.ops.queue_family.load_family_records instead"
        )


# ---------------------------------------------------------------------------
# (h) AC7 -- granularity is a named POLICY (D-3), not a bare cardinality
# knob: commit cardinality + branch-recheck cadence + mid-loop halt.
# ---------------------------------------------------------------------------


def _three_items() -> list[dict[str, Any]]:
    return [
        {"paths": ["state/bug-backlog/a.yaml"], "message": "close a"},
        {"paths": ["state/bug-backlog/b.yaml"], "message": "close b"},
        {"paths": ["state/bug-backlog/c.yaml"], "message": "close c"},
    ]


def _call_build_stage_and_commit(granularity: str, **overrides: Any) -> dict[str, Any]:
    """Adapt C1's `_three_items()` wave-shaped fixture onto C2's actual
    per-directive `build_stage_and_commit(*, id, paths, message, granularity,
    branch, expected_branch, ...)` signature -- flatten the three items into
    one combined paths/message pair, matching how a `per-wave` caller would
    invoke it for real. `granularity` alone drives what this class asserts
    (the `cli` verb, the `branch_recheck_cadence` cadence, and the
    `on_non_pass` sub-directive's presence/absence), so which items compose
    `paths`/`message` is immaterial to every assertion in this class."""
    items = _three_items()
    kwargs: dict[str, Any] = {
        "id": "stage-and-commit-test",
        "paths": [p for item in items for p in item["paths"]],
        "message": "; ".join(item["message"] for item in items),
        "granularity": granularity,
        "branch": "work/x",
        "expected_branch": "work/x",
    }
    kwargs.update(overrides)
    return bga_directives.build_stage_and_commit(**kwargs)


class TestStageAndCommitDirectiveBuilder:
    """`directives.build_stage_and_commit` -- C2's own file, pinned here per
    the chunk's explicit AC7 instruction; C2 adds further unit coverage of
    its own on top of this."""

    def test_granularity_is_a_named_field_not_baked_only_into_cli(self):
        d = _call_build_stage_and_commit("per-item")
        assert d["granularity"] == "per-item"
        assert d["cli"] == "commit-per-item"

    def test_per_wave_granularity_field_and_cli(self):
        d = _call_build_stage_and_commit("per-wave")
        assert d["granularity"] == "per-wave"
        assert d["cli"] == "commit-per-wave"

    def test_unknown_granularity_raises(self):
        with pytest.raises(ValueError):
            _call_build_stage_and_commit("per-batch")

    def test_per_item_directive_carries_non_pass_failure_sub_directive(self):
        # (c) of D-3: for per-item, the non-PASS failure path (`git
        # checkout -- <paths>` + a backlog note) is an explicit
        # sub-directive, not left as prose the rebuilt body has to keep.
        d = _call_build_stage_and_commit("per-item")
        assert "on_non_pass" in d
        assert d["on_non_pass"], "per-item directive must name its non-PASS failure sub-directive"

    def test_per_wave_directive_carries_no_non_pass_sub_directive(self):
        # per-wave has no per-item non-PASS failure path -- collapsing the
        # cardinalities would silently drop this distinction (D-3's own
        # named concern).
        d = _call_build_stage_and_commit("per-wave")
        assert not d.get("on_non_pass")


class _FakeGit:
    """Records every git invocation `apply.py`'s commit-verb handlers make
    and lets the test script exactly which `rev-parse --abbrev-ref HEAD`
    branch each successive check returns -- controls the mid-loop-flip
    scenario without faking real git plumbing end to end."""

    def __init__(self, branch_sequence: list[str]):
        self.log: list[tuple[str, ...]] = []
        self._branch_sequence = list(branch_sequence)
        self._branch_calls = 0

    def __call__(self, args: list[str], cwd: Path):
        self.log.append(tuple(args))
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            branch = self._branch_sequence[
                min(self._branch_calls, len(self._branch_sequence) - 1)
            ]
            self._branch_calls += 1
            return SimpleNamespace(returncode=0, stdout=f"{branch}\n", stderr="")
        if args[0] == "add":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:2] == ["diff", "--cached"]:
            # returncode 1 == staged changes present (git's own --quiet
            # convention) -- every fixture item here always has something
            # to commit.
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if args[0] == "commit":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args == ["rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout=f"fakesha{len(self.log)}\n", stderr="")
        raise AssertionError(f"_FakeGit: unexpected git invocation {args!r}")

    @property
    def commit_calls(self) -> int:
        return sum(1 for call in self.log if call and call[0] == "commit")

    @property
    def branch_check_calls(self) -> int:
        return self._branch_calls


def _commit_directive_args(granularity: str) -> list[str]:
    payload = {"items": _three_items(), "branch": "work/x", "expected_branch": "work/x"}
    return [json.dumps(payload)]


class TestGranularityCommitCardinality:
    def test_per_item_yields_n_commits_for_n_items(self, tmp_path, monkeypatch):
        fake_git = _FakeGit(branch_sequence=["work/x"])
        monkeypatch.setattr(bga_apply, "_run_git", fake_git)
        handler = bga_apply._CLI_DISPATCH["commit-per-item"]
        handler(_commit_directive_args("per-item"), tmp_path)
        assert fake_git.commit_calls == 3

    def test_per_wave_yields_1_commit_for_the_same_n_items(self, tmp_path, monkeypatch):
        fake_git = _FakeGit(branch_sequence=["work/x"])
        monkeypatch.setattr(bga_apply, "_run_git", fake_git)
        handler = bga_apply._CLI_DISPATCH["commit-per-wave"]
        handler(_commit_directive_args("per-wave"), tmp_path)
        assert fake_git.commit_calls == 1


class TestGranularityBranchRecheckCadence:
    def test_per_item_rechecks_branch_before_every_commit(self, tmp_path, monkeypatch):
        fake_git = _FakeGit(branch_sequence=["work/x"])
        monkeypatch.setattr(bga_apply, "_run_git", fake_git)
        handler = bga_apply._CLI_DISPATCH["commit-per-item"]
        handler(_commit_directive_args("per-item"), tmp_path)
        # D-3(b): per-commit cadence for per-item -- one branch-check per
        # of the 3 items, matching commit cardinality exactly.
        assert fake_git.branch_check_calls == fake_git.commit_calls == 3

    def test_per_wave_rechecks_branch_once_for_the_whole_wave(self, tmp_path, monkeypatch):
        fake_git = _FakeGit(branch_sequence=["work/x"])
        monkeypatch.setattr(bga_apply, "_run_git", fake_git)
        handler = bga_apply._CLI_DISPATCH["commit-per-wave"]
        handler(_commit_directive_args("per-wave"), tmp_path)
        # D-3(b): per-wave cadence -- ONE branch-check for the whole wave,
        # never once per item (that would silently degrade to per-item
        # cost while claiming per-wave cardinality).
        assert fake_git.branch_check_calls == 1
        assert fake_git.commit_calls == 1


class TestGranularityMidLoopBranchFlipHalts:
    def test_per_item_mid_loop_branch_flip_halts_before_committing_further(self, tmp_path, monkeypatch):
        # Branch matches expected for items 1-2, then flips before item 3's
        # pre-commit recheck -- D-3(b): "re-confirms git branch
        # --show-current == $BLITZ_BRANCH before every commit and halts
        # the wave if it flipped, because the loop spans many seconds."
        fake_git = _FakeGit(branch_sequence=["work/x", "work/x", "work/DIFFERENT"])
        monkeypatch.setattr(bga_apply, "_run_git", fake_git)
        handler = bga_apply._CLI_DISPATCH["commit-per-item"]

        with pytest.raises(bga_apply.BranchMismatch):
            handler(_commit_directive_args("per-item"), tmp_path)

        # Halts BEFORE committing the mismatched item -- only the first 2
        # items' commits actually landed, never a 3rd committed-then-failed.
        assert fake_git.commit_calls == 2

    def test_per_wave_mid_loop_branch_flip_is_a_non_issue_by_construction(self, tmp_path, monkeypatch):
        # Per-wave rechecks the branch exactly once, before the single
        # combined commit -- there is no "mid-loop" for a flip to occur
        # inside; a flip that happens strictly BEFORE that one recheck is
        # caught the same way any single directive's precondition would be.
        fake_git = _FakeGit(branch_sequence=["work/DIFFERENT"])
        monkeypatch.setattr(bga_apply, "_run_git", fake_git)
        handler = bga_apply._CLI_DISPATCH["commit-per-wave"]

        with pytest.raises(bga_apply.BranchMismatch):
            handler(_commit_directive_args("per-wave"), tmp_path)

        assert fake_git.commit_calls == 0


# ---------------------------------------------------------------------------
# (i) RED AT AUTHORING TIME, ON PURPOSE (same D-5/AC21 shape as the file's
# own top docstring, scoped to this one follow-on chunk).
#
# `main_apply` does not parse `--wave-path`/`--granularity`/`--message` yet
# -- this section is the SPEC for that CLI surface, not a report of
# something already built. See example-doctrine-repo
# docs/plans/2026-07-26-backlog-grind-computed-frontage.md's follow-on
# chunk E1/E2: E1 (this section) plants the red tests; E2 makes them green
# by extending `main_apply`'s own argv loop. Do NOT weaken an assertion
# here to get green from this session -- E2 reads this section as the
# contract.
#
# The design pinned below (do not re-litigate it from E2):
#   apply <cadence> [--session-id <id>] [--decisions <json>]
#                   [--wave-path <repo-relative-path>]...   (repeatable)
#                   [--granularity per-item|per-wave]
#                   [--message <commit message>]
# -- the CLI accepts PATHS, never directives (AC3's closed-dispatch
# property extended to this surface); the verb is chosen by the engine
# from `--granularity`, never named by the caller; the directive is built
# by `directives.build_stage_and_commit` and threaded through the EXISTING
# `apply(..., extra_directives=[...])` seam -- see this section's own
# per-class docstrings for which numbered requirement each class pins.
# ---------------------------------------------------------------------------


def _stub_brief(monkeypatch, *, directives=None, judgment_points=None) -> None:
    """Replace `bga_apply.brief` (the bare module-level name `apply()`
    calls as `brief(cadence, repo_root=root)`) with a fixed decision
    object -- lets this section's tests pin exactly which native
    directives/judgment_points `apply()` sees, without depending on any
    real C3a-C3e reader's own (separately tested) logic or a real git
    checkout. Mirrors the `_run_git` bare-name patch-point convention
    `apply.py`'s own module docstring documents."""
    fixed_directives = list(directives or [])
    fixed_judgment_points = list(judgment_points or [])

    def _fake_brief(cadence, repo_root=None, run_id=None):
        return SimpleNamespace(
            decision_object={
                "directives": list(fixed_directives),
                "judgment_points": list(fixed_judgment_points),
            }
        )

    monkeypatch.setattr(bga_apply, "brief", _fake_brief)


def _prep_wave_path_test(monkeypatch, tmp_path, *, judgment_points=None) -> None:
    """Shared setup every wave-path CLI test needs: a stubbed `brief()`
    (native directives empty unless a test says otherwise), an explicit
    session id (so `apply()` never falls through to the "no session id
    resolvable" transport failure this section isn't testing), and a cwd
    inside `tmp_path` (`apply()`'s own `repo_root` defaults to
    `Path.cwd()` -- there is no CLI flag for it)."""
    _stub_brief(monkeypatch, judgment_points=judgment_points)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-wave-path-e1")
    monkeypatch.chdir(tmp_path)


def _run_main_apply_capturing_directives(
    monkeypatch, argv: list[str], *, branch_sequence: Optional[list[str]] = None
) -> tuple[int, list[dict[str, Any]]]:
    """Runs `bga_apply.main_apply(argv)` with `_run_git` faked (so no real
    git subprocess ever runs) and `apply_base.execute_directives` spied so
    the test can inspect exactly which directives reached the execution
    seam -- the shape `_prepare_directives_for_dispatch` produces (AFTER
    prepping, BEFORE any handler dispatches; prepping preserves every
    original directive key it doesn't itself overwrite, including
    `granularity`/`on_non_pass` -- see `_prepare_directives_for_dispatch`'s
    own `dict(directive)` shallow-copy). Returns `(exit_code,
    captured_directives)`; `captured_directives` stays `[]` if the run
    never reaches `execute_directives` at all (e.g. a usage error)."""
    fake_git = _FakeGit(branch_sequence=list(branch_sequence or ["work/x"]))
    monkeypatch.setattr(bga_apply, "_run_git", fake_git)

    captured: dict[str, Any] = {"directives": []}
    real_execute = bga_apply.apply_base.execute_directives

    def _spy_execute(directives, judgment_points, repo_root, dispatch_table, **kwargs):
        captured["directives"] = directives
        return real_execute(directives, judgment_points, repo_root, dispatch_table, **kwargs)

    monkeypatch.setattr(bga_apply.apply_base, "execute_directives", _spy_execute)

    exit_code = bga_apply.main_apply(argv)
    return exit_code, captured["directives"]


class TestWavePathRepeatablePreservesOrder:
    """Requirement 1: `--wave-path` repeatable, collecting multiple paths
    in order. Exercised at `per-wave` granularity, where all wave paths
    combine into ONE directive's single item -- the least ambiguous place
    to observe collection order (see the module note above `apply.py`'s
    own docstring on the per-item-vs-per-wave items shape)."""

    def test_wave_paths_collected_in_order_for_per_wave_granularity(self, tmp_path, monkeypatch):
        _prep_wave_path_test(monkeypatch, tmp_path)

        exit_code, directives = _run_main_apply_capturing_directives(
            monkeypatch,
            [
                "dummy-cadence",
                "--wave-path", "alpha/one.txt",
                "--wave-path", "beta/two.txt",
                "--wave-path", "gamma/three.txt",
                "--granularity", "per-wave",
                "--message", "wave commit",
            ],
        )

        wave_directives = [d for d in directives if d.get("cli") == "commit-per-wave"]
        assert wave_directives, "expected a commit-per-wave directive built from --wave-path"
        payload = json.loads(wave_directives[0]["args"][0])
        collected_paths = [p for item in payload["items"] for p in item["paths"]]
        assert collected_paths == ["alpha/one.txt", "beta/two.txt", "gamma/three.txt"]
        assert exit_code == bga_apply.APPLY_EXIT_OK


class TestGranularitySelectsDispatchedVerb:
    """Requirement 2: the engine chooses the verb from `--granularity` --
    per-item dispatches `commit-per-item`, per-wave dispatches
    `commit-per-wave`. Asserted against the DISPATCHED verb (the real
    handler's own returned `detail["cli"]`, printed in `main_apply`'s JSON
    report), never merely the parsed `--granularity` value or the
    pre-dispatch directive dict."""

    def test_per_item_granularity_dispatches_commit_per_item_verb(self, tmp_path, monkeypatch, capsys):
        _prep_wave_path_test(monkeypatch, tmp_path)
        fake_git = _FakeGit(branch_sequence=["work/x"])
        monkeypatch.setattr(bga_apply, "_run_git", fake_git)

        exit_code = bga_apply.main_apply(
            [
                "dummy-cadence",
                "--wave-path", "one.txt",
                "--wave-path", "two.txt",
                "--granularity", "per-item",
                "--message", "fix things",
            ]
        )

        report = json.loads(capsys.readouterr().out)
        dispatched_clis = {r["detail"]["cli"] for r in report.get("results", []) if r.get("detail")}
        assert exit_code == bga_apply.APPLY_EXIT_OK
        assert "commit-per-item" in dispatched_clis
        assert "commit-per-wave" not in dispatched_clis

    def test_per_wave_granularity_dispatches_commit_per_wave_verb(self, tmp_path, monkeypatch, capsys):
        _prep_wave_path_test(monkeypatch, tmp_path)
        fake_git = _FakeGit(branch_sequence=["work/x"])
        monkeypatch.setattr(bga_apply, "_run_git", fake_git)

        exit_code = bga_apply.main_apply(
            [
                "dummy-cadence",
                "--wave-path", "one.txt",
                "--wave-path", "two.txt",
                "--granularity", "per-wave",
                "--message", "wave commit",
            ]
        )

        report = json.loads(capsys.readouterr().out)
        dispatched_clis = {r["detail"]["cli"] for r in report.get("results", []) if r.get("detail")}
        assert exit_code == bga_apply.APPLY_EXIT_OK
        assert "commit-per-wave" in dispatched_clis
        assert "commit-per-item" not in dispatched_clis

    @pytest.mark.parametrize(
        "granularity, expected_cli, expected_directive_count",
        [
            ("per-item", "commit-per-item", 3),
            ("per-wave", "commit-per-wave", 1),
        ],
    )
    def test_directive_and_commit_cardinality_matches_granularity(
        self, tmp_path, monkeypatch, granularity, expected_cli, expected_directive_count
    ):
        """D-3's two-cardinality rule, pinned directly rather than merely
        implied by the verb-selection tests above. `per-item` must build N
        DISTINCT directives (one per `--wave-path`, N >= 3 here) carrying N
        DISTINCT `id`s, and all N must survive `apply_base.
        order_by_depends_on` (which keys directives by `id` in a plain
        dict -- a repeated id silently drops every directive after the
        first, with no exception raised) to reach dispatch as N separate
        commits. `per-wave` must build exactly ONE directive and ONE
        commit for the SAME N paths -- the contrast is the point (D-3).

        Two silent-failure modes this guards against, both of which would
        pass every other test in this file unchanged:
          1. Collapse: per-item "simplified" into a single directive
             carrying a path list -- N commits silently become 1, quietly
             destroying bug-blitz's per-item commit audit trail.
          2. Id collision: per-item directives sharing an id -- everything
             after the first is silently dropped from dispatch by
             `order_by_depends_on`'s dict-keyed ordering, so only the
             first item's commit lands.
        """
        _prep_wave_path_test(monkeypatch, tmp_path)
        fake_git = _FakeGit(branch_sequence=["work/x"])
        monkeypatch.setattr(bga_apply, "_run_git", fake_git)

        captured: dict[str, Any] = {"directives": []}
        real_execute = bga_apply.apply_base.execute_directives

        def _spy_execute(directives, judgment_points, repo_root, dispatch_table, **kwargs):
            captured["directives"] = directives
            return real_execute(directives, judgment_points, repo_root, dispatch_table, **kwargs)

        monkeypatch.setattr(bga_apply.apply_base, "execute_directives", _spy_execute)

        exit_code = bga_apply.main_apply(
            [
                "dummy-cadence",
                "--wave-path", "alpha/one.txt",
                "--wave-path", "beta/two.txt",
                "--wave-path", "gamma/three.txt",
                "--granularity", granularity,
                "--message", "wave commit",
            ]
        )

        assert exit_code == bga_apply.APPLY_EXIT_OK

        # Cardinality: exactly N directives of the expected verb reached
        # execute_directives (post-order_by_depends_on, pre-dispatch).
        matching = [d for d in captured["directives"] if d.get("cli") == expected_cli]
        assert len(matching) == expected_directive_count, (
            f"expected {expected_directive_count} {expected_cli!r} directive(s) "
            f"for granularity={granularity!r}, got {len(matching)}"
        )

        # Id distinctness: a set-length comparison so an id-reuse
        # regression fails HERE, not as a silently-dropped dispatch.
        ids = {d.get("id") for d in matching}
        assert len(ids) == expected_directive_count, (
            f"expected {expected_directive_count} distinct directive ids for "
            f"granularity={granularity!r}, got {len(ids)} distinct out of "
            f"{len(matching)} directives -- an id collision silently drops "
            "every directive after the first from order_by_depends_on's "
            "dict-keyed dispatch"
        )

        # Dispatch survival: the real consequence an id collision would
        # have caused -- fewer commits than directives built.
        assert fake_git.commit_calls == expected_directive_count, (
            f"expected {expected_directive_count} commit(s) to actually "
            f"dispatch for granularity={granularity!r}, got "
            f"{fake_git.commit_calls} -- a lower count than the directive "
            "count means some directives were silently dropped before "
            "dispatch"
        )


class TestNoCliInjectionViaWavePathFlags:
    """Requirement 3, the negative that matters most: there is no CLI
    spelling that lets a caller name a dispatch verb or inject a raw
    directive. AC3's closed-dispatch property, extended to this surface --
    "the CLI accepts PATHS, never directives" is not just a claim, it is
    what these tests hold shut."""

    def test_no_cli_flag_exists_on_the_apply_subcommand(self):
        exit_code = bga_apply.main_apply(["dummy-cadence", "--cli", "rm"])
        assert exit_code == bga_apply.APPLY_EXIT_TRANSPORT_FAIL

    def test_no_directive_injection_flag_exists(self):
        for flag in ("--directives", "--directive", "--directives-json", "--extra-directives"):
            exit_code = bga_apply.main_apply(["dummy-cadence", flag, "[]"])
            assert exit_code == bga_apply.APPLY_EXIT_TRANSPORT_FAIL, (
                f"{flag} must not be a flag main_apply recognizes"
            )

    def test_a_json_directive_payload_smuggled_via_wave_path_never_resolves_as_a_cli(
        self, tmp_path, monkeypatch
    ):
        _prep_wave_path_test(monkeypatch, tmp_path)

        seen_clis: list[str] = []
        real_resolve_cli = bga_apply.apply_base.resolve_cli

        def _spy_resolve_cli(table, cli):
            seen_clis.append(cli)
            return real_resolve_cli(table, cli)

        monkeypatch.setattr(bga_apply.apply_base, "resolve_cli", _spy_resolve_cli)
        fake_git = _FakeGit(branch_sequence=["work/x"])
        monkeypatch.setattr(bga_apply, "_run_git", fake_git)

        malicious_path = json.dumps({"cli": "rm", "args": ["-rf", "."]})
        bga_apply.main_apply(
            [
                "dummy-cadence",
                "--wave-path", malicious_path,
                "--granularity", "per-wave",
                "--message", "wave commit",
            ]
        )

        assert seen_clis, "expected the run to reach dispatch resolution at least once"
        assert set(seen_clis) <= {"commit-per-item", "commit-per-wave"}
        assert "rm" not in seen_clis


class TestWavePathValidationIsALoudRefusal:
    """Requirement 4: an absolute path, a `..` traversal, or a path that
    resolves outside the repo root must be a LOUD refusal with a non-zero
    exit -- never a silent skip, never a filtered subset that commits the
    other, valid paths anyway."""

    @pytest.mark.parametrize(
        "bad_path",
        [
            "/etc/passwd",
            "../outside.txt",
            "sub/../../outside.txt",
        ],
    )
    def test_bad_wave_path_refuses_the_whole_run_never_a_silent_subset(
        self, tmp_path, monkeypatch, bad_path
    ):
        _prep_wave_path_test(monkeypatch, tmp_path)
        fake_git = _FakeGit(branch_sequence=["work/x"])
        monkeypatch.setattr(bga_apply, "_run_git", fake_git)

        exit_code = bga_apply.main_apply(
            [
                "dummy-cadence",
                "--wave-path", "safe/inside.txt",
                "--wave-path", bad_path,
                "--granularity", "per-wave",
                "--message", "wave commit",
            ]
        )

        assert exit_code != bga_apply.APPLY_EXIT_OK
        assert fake_git.commit_calls == 0, (
            "a bad --wave-path must refuse the WHOLE run -- never commit "
            "the other, valid paths as a filtered subset"
        )


class TestWavePathUsageErrors:
    """Requirement 5: `--wave-path` without `--granularity` is a usage
    error (the two cardinalities are semantically different -- guessing
    would silently destroy one, per the chunk brief); `--granularity` or
    `--message` with no `--wave-path` is a usage error; an unrecognized
    `--granularity` value is a usage error."""

    def test_wave_path_without_granularity_is_usage_error(self):
        exit_code = bga_apply.main_apply(["dummy-cadence", "--wave-path", "a.txt", "--message", "m"])
        assert exit_code == bga_apply.APPLY_EXIT_TRANSPORT_FAIL

    def test_granularity_without_wave_path_is_usage_error(self):
        exit_code = bga_apply.main_apply(["dummy-cadence", "--granularity", "per-wave", "--message", "m"])
        assert exit_code == bga_apply.APPLY_EXIT_TRANSPORT_FAIL

    def test_message_without_wave_path_is_usage_error(self):
        exit_code = bga_apply.main_apply(["dummy-cadence", "--message", "m"])
        assert exit_code == bga_apply.APPLY_EXIT_TRANSPORT_FAIL

    def test_unknown_granularity_value_is_usage_error(self):
        exit_code = bga_apply.main_apply(
            ["dummy-cadence", "--wave-path", "a.txt", "--granularity", "per-batch", "--message", "m"]
        )
        assert exit_code == bga_apply.APPLY_EXIT_TRANSPORT_FAIL


class TestWavePathDirectiveIsBuiltByBuildStageAndCommit:
    """Requirement 6: the directive reaching `extra_directives` is the one
    `directives.build_stage_and_commit` produces, not a hand-rolled dict --
    pinned by checking it carries that builder's own named `granularity`
    field and, for `per-item`, the `on_non_pass` sub-directive (D-3(c))."""

    def test_per_item_directive_carries_granularity_field_and_on_non_pass(self, tmp_path, monkeypatch):
        _prep_wave_path_test(monkeypatch, tmp_path)

        _exit_code, directives = _run_main_apply_capturing_directives(
            monkeypatch,
            [
                "dummy-cadence",
                "--wave-path", "one.txt",
                "--wave-path", "two.txt",
                "--granularity", "per-item",
                "--message", "fix things",
            ],
        )

        commit_directives = [d for d in directives if d.get("cli") == "commit-per-item"]
        assert commit_directives, "expected at least one commit-per-item directive"
        for d in commit_directives:
            assert d.get("granularity") == "per-item"
            assert d.get("on_non_pass"), (
                "per-item directive must carry the on_non_pass sub-directive "
                "build_stage_and_commit builds for granularity='per-item'"
            )

    def test_per_wave_directive_carries_granularity_field_and_no_on_non_pass(self, tmp_path, monkeypatch):
        _prep_wave_path_test(monkeypatch, tmp_path)

        _exit_code, directives = _run_main_apply_capturing_directives(
            monkeypatch,
            [
                "dummy-cadence",
                "--wave-path", "one.txt",
                "--wave-path", "two.txt",
                "--granularity", "per-wave",
                "--message", "wave commit",
            ],
        )

        commit_directives = [d for d in directives if d.get("cli") == "commit-per-wave"]
        assert commit_directives, "expected a commit-per-wave directive"
        for d in commit_directives:
            assert d.get("granularity") == "per-wave"
            assert not d.get("on_non_pass")


class TestBugBlitzGateNotBypassedByWavePathCli:
    """Requirement 7: bug-blitz's standing `depends_on` EM-judgment gate
    (`readers_bug_blitz.COMMIT_READINESS_JP_ID`, "bug-blitz carries no
    code-review gate of any kind ... ready to commit this run's autonomous
    fixes?") still halts before committing when the new --wave-path flags
    are used for the `bug-blitz` cadence -- the CLI path must not provide
    a way around it. Built with the SAME builder
    (`directives.build_commit_readiness_gate`) the real reader uses, with
    `resolves=[]` exactly as the real reader leaves it -- an unresolved
    judgment point that no disposition can auto-resolve away."""

    def test_wave_path_commit_halts_at_bug_blitz_commit_readiness_gate(self, tmp_path, monkeypatch):
        gate_jp = bga_directives.build_commit_readiness_gate(
            id=bga.readers_bug_blitz.COMMIT_READINESS_JP_ID,
            question="bug-blitz: ready to commit this run's autonomous fixes?",
            evidence="state/bug-backlog/ open-item count=0",
            reason="insufficient-evidence",
            resolves=[],
        )
        _prep_wave_path_test(monkeypatch, tmp_path, judgment_points=[gate_jp])
        fake_git = _FakeGit(branch_sequence=["work/x"])
        monkeypatch.setattr(bga_apply, "_run_git", fake_git)

        exit_code = bga_apply.main_apply(
            [
                "bug-blitz",
                "--wave-path", "state/bug-backlog/x.yaml",
                "--granularity", "per-item",
                "--message", "fix x",
            ]
        )

        assert exit_code == bga_apply.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert fake_git.commit_calls == 0, (
            "the wave-path CLI path must not let a bug-blitz commit land "
            "while the commit-readiness gate is unresolved"
        )


# ---------------------------------------------------------------------------
# (j) F2/F3 closing the loop -- the two template-emission builders
# (`directives.build_spinoff_handoff_template_emission` /
# `build_executor_dispatch_prompt_template_emission`) and
# `verifier.build_haiku_verifier_dispatch` shipped with no reachable-call-site
# coverage (handoff-gate code review, F2/F3). These tests exercise the REAL
# `apply()` orchestration end to end -- real (unstubbed) readers, the real
# `_CLI_DISPATCH` table, the real pass-through handlers -- never a bare unit
# call to a builder in isolation. `apply()` recomputes `brief(cadence)`
# in-process (never trusts a caller-supplied decision object), so passing
# `repo_root=tmp_path` here is belt-and-suspenders scaffolding only: the
# readers this section exercises resolve their own state/git root from
# process cwd (see `readers_blitz._repo_root`/`readers_mise._resolve_state_root`),
# not from `apply()`'s `repo_root` parameter -- see `__init__.py`'s own
# `brief()` docstring. Degrading to "repo root unresolved"/"no families
# readable" on a `tmp_path` cwd is fine: the two directives/enum sets this
# section pins are cadence-mechanical and fire unconditionally, independent
# of backlog state (see each reader module's own docstring).
# ---------------------------------------------------------------------------


class TestTemplateEmissionEndToEndThroughApply:
    """F2 -- both template-emission builders reach a real `apply()` call:
    the executor-dispatch-prompt-template via the real, unstubbed
    `brief(cadence)` path (both cadences emit it unconditionally from
    `collect()`), and the spinoff-handoff-template via the sanctioned
    `extra_directives` seam `apply()`'s own docstring names for exactly
    this shape -- a directive `collect()` cannot honestly build at boot
    time (it needs per-item data only known live, at spinoff-authoring
    time -- `readers_blitz.build_spinoff_handoff`'s own docstring)."""

    def test_bug_blitz_cadence_emits_executor_dispatch_template_with_substantial_fields(
        self, tmp_path
    ):
        exit_code, report = bga_apply.apply(
            "bug-blitz", session_id="sess-f2-blitz-executor", repo_root=tmp_path
        )
        assert exit_code == bga_apply.APPLY_EXIT_OK
        results = report.get("results", [])
        matches = [
            r["detail"] for r in results
            if r.get("detail", {}).get("cli") == "executor-dispatch-prompt-template"
        ]
        assert matches, "expected an executor-dispatch-prompt-template directive to dispatch"
        fields = matches[0]["fields"]
        assert fields, "executor-dispatch-prompt-template fields must not be empty"
        for value in fields.values():
            assert isinstance(value, str) and len(value) > 20, (
                "a template field this thin is exactly the empty-body failure "
                "mode this coverage exists to catch"
            )

    def test_mise_cadence_emits_executor_dispatch_template_with_substantial_fields(
        self, tmp_path
    ):
        exit_code, report = bga_apply.apply(
            "mise-en-place", session_id="sess-f2-mise-executor", repo_root=tmp_path
        )
        assert exit_code == bga_apply.APPLY_EXIT_OK
        results = report.get("results", [])
        matches = [
            r["detail"] for r in results
            if r.get("detail", {}).get("cli") == "executor-dispatch-prompt-template"
        ]
        assert matches, "expected an executor-dispatch-prompt-template directive to dispatch"
        fields = matches[0]["fields"]
        assert fields, "executor-dispatch-prompt-template fields must not be empty"
        for value in fields.values():
            assert isinstance(value, str) and len(value) > 20

    def test_bug_blitz_spinoff_handoff_template_renders_substantial_body_end_to_end(
        self, tmp_path
    ):
        spinoff_directive = bga.readers_bug_blitz.build_spinoff_handoff(
            id="d-spinoff-f2-test",
            title="oversized fix for item x-1",
            created="2026-07-27",
            branch="work/x",
            run_id="run-f2-test",
            item_id="x-1",
            classification_reason="footprint >= 3 files",
            scope=["coordinator_core/a.py", "coordinator_core/b.py"],
            body="the bug body text",
            cross_ref="state/bug-backlog/archive/x-1.yaml",
            why_blocked="oversized for an in-wave fix",
        )

        exit_code, report = bga_apply.apply(
            "bug-blitz",
            session_id="sess-f2-blitz-spinoff",
            repo_root=tmp_path,
            extra_directives=[spinoff_directive],
        )

        assert exit_code == bga_apply.APPLY_EXIT_OK
        results = report.get("results", [])
        matches = [
            r["detail"] for r in results
            if r.get("detail", {}).get("cli") == "spinoff-handoff-template"
        ]
        assert matches, "expected the spinoff-handoff-template directive to dispatch"
        content = matches[0]["fields"]["content"]
        # Non-empty is necessary but not sufficient -- the empty-dict failure
        # mode this coverage exists to catch would also pass a bare
        # truthiness check on a one-character string. Pin actual substance:
        # every canonical section header the ~40-line template is specced to
        # carry (module docstring, item 3), plus the backlog entry's own
        # verbatim fields threaded through.
        assert len(content) > 400, "spinoff-handoff body suspiciously short -- looks unrendered"
        for header in (
            "## What this covers",
            "## Reference materials (read first)",
            "## Specification",
            "## Acceptance criteria",
            "## Recommended next steps",
            "## Anti-scope",
        ):
            assert header in content, f"spinoff-handoff body missing canonical section {header!r}"
        assert "x-1" in content
        assert "the bug body text" in content


# ---------------------------------------------------------------------------
# (k) F3 -- the Haiku-verifier dispatch reaches a real `apply()` call for
# BOTH cadences, carrying the RIGHT enum set each time; the no-merge
# negative-spec gets its own red test (they must never be the same set).
# ---------------------------------------------------------------------------


class TestHaikuVerifierDispatchEndToEndThroughApply:
    def test_mise_cadence_verifier_dispatch_carries_mise_enum_via_real_brief(self, tmp_path):
        # readers_mise.collect() emits d-mise-haiku-verifier-dispatch
        # unconditionally from collect() itself -- no extra_directives
        # needed to reach it through the real brief()->apply() pipeline.
        exit_code, report = bga_apply.apply(
            "mise-en-place", session_id="sess-f3-mise", repo_root=tmp_path
        )
        assert exit_code == bga_apply.APPLY_EXIT_OK
        results = report.get("results", [])
        matches = [
            r["detail"] for r in results
            if r.get("detail", {}).get("cli") == bga_verifier.HAIKU_VERIFIER_CLI
        ]
        assert matches, "expected a dispatch-haiku-verifier directive to dispatch for mise-en-place"
        spec = matches[0]["spec"]
        assert spec["enum_set"] == list(bga_verifier.MISE_VERIFIER_ENUM)

    def test_bug_blitz_cadence_verifier_dispatch_carries_bug_blitz_enum_via_extra_directives(
        self, tmp_path
    ):
        # readers_blitz.build_verifier_dispatch is called at wave-verify
        # time (once a DONE summary exists), never from collect() -- reach
        # it the same sanctioned way the spinoff-handoff test above does.
        verifier_directive = bga.readers_bug_blitz.build_verifier_dispatch(
            id="d-verifier-f3-test", run_id="run-f3-test", item_id="x-1"
        )

        exit_code, report = bga_apply.apply(
            "bug-blitz",
            session_id="sess-f3-blitz",
            repo_root=tmp_path,
            extra_directives=[verifier_directive],
        )

        assert exit_code == bga_apply.APPLY_EXIT_OK
        results = report.get("results", [])
        matches = [
            r["detail"] for r in results
            if r.get("detail", {}).get("cli") == bga_verifier.HAIKU_VERIFIER_CLI
        ]
        assert matches, "expected a dispatch-haiku-verifier directive to dispatch for bug-blitz"
        spec = matches[0]["spec"]
        assert spec["enum_set"] == list(bga_verifier.BUG_BLITZ_VERIFIER_ENUM)

    def test_bug_blitz_and_mise_verifier_enums_are_not_the_same_set(self):
        # The no-merge negative-spec (verifier.py's own docstring: "Do NOT
        # add a third enum constant that merges BUG_BLITZ_VERIFIER_ENUM and
        # MISE_VERIFIER_ENUM") deserves a red test of its own, not just an
        # inline assumption baked into the two tests above.
        assert set(bga_verifier.BUG_BLITZ_VERIFIER_ENUM) != set(bga_verifier.MISE_VERIFIER_ENUM)


# ---------------------------------------------------------------------------
# (l) F3 -- verifier.py's own guard rails: an enum_set missing a shared
# verdict value raises, and an extra_fields reserved-key collision raises.
# Unit-level (no apply() round-trip needed -- these are `verifier.py`'s own
# input-validation contract, pinned directly against the builder).
# ---------------------------------------------------------------------------


class TestHaikuVerifierDispatchGuardRails:
    def _base_kwargs(self, **overrides: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "id": "d-verifier-guard-test",
            "evidence_source": ["done-summary", "diff:files"],
            "enum_set": list(bga_verifier.BUG_BLITZ_VERIFIER_ENUM),
            "output_path": "state/scratch/bug-blitz/run-1/item-1.verify.md",
        }
        kwargs.update(overrides)
        return kwargs

    def test_enum_set_missing_pass_raises(self):
        enum_set = [v for v in bga_verifier.BUG_BLITZ_VERIFIER_ENUM if v != bga_verifier.PASS]
        with pytest.raises(ValueError):
            bga_verifier.build_haiku_verifier_dispatch(**self._base_kwargs(enum_set=enum_set))

    def test_enum_set_missing_footprint_violation_raises(self):
        enum_set = [
            v for v in bga_verifier.BUG_BLITZ_VERIFIER_ENUM if v != bga_verifier.FOOTPRINT_VIOLATION
        ]
        with pytest.raises(ValueError):
            bga_verifier.build_haiku_verifier_dispatch(**self._base_kwargs(enum_set=enum_set))

    def test_empty_enum_set_raises(self):
        with pytest.raises(ValueError):
            bga_verifier.build_haiku_verifier_dispatch(**self._base_kwargs(enum_set=[]))

    def test_extra_fields_colliding_with_a_reserved_key_raises(self):
        with pytest.raises(ValueError):
            bga_verifier.build_haiku_verifier_dispatch(
                **self._base_kwargs(extra_fields={"output_path": "clobbered"})
            )

    def test_extra_fields_colliding_with_cli_key_raises(self):
        with pytest.raises(ValueError):
            bga_verifier.build_haiku_verifier_dispatch(
                **self._base_kwargs(extra_fields={"cli": "something-else"})
            )

    def test_extra_fields_non_colliding_merges_cleanly(self):
        directive = bga_verifier.build_haiku_verifier_dispatch(
            **self._base_kwargs(extra_fields={"item_id": "x-1", "run_id": "run-1"})
        )
        assert directive["item_id"] == "x-1"
        assert directive["run_id"] == "run-1"


# ---------------------------------------------------------------------------
# (m) Envelope contract still holds for the two cadences this coverage wires
# through real (unstubbed) readers -- extends TestDecisionObjectKeyShapes'
# stubbed-reader bug-blitz-only assertion rather than duplicating it.
# ---------------------------------------------------------------------------


class TestEnvelopeContractHoldsWithRealReaders:
    @pytest.mark.parametrize("cadence", ["bug-blitz", "mise-en-place"])
    def test_brief_returns_exactly_the_8_canonical_keys_with_real_readers(self, tmp_path, cadence):
        decision = bga.brief(cadence, repo_root=tmp_path).decision_object
        assert set(decision.keys()) == set(ENVELOPE_KEYS)

    @pytest.mark.parametrize("cadence", ["bug-blitz", "mise-en-place"])
    def test_main_brief_subcommand_exits_ok_with_real_readers(self, cadence, capsys):
        exit_code = bga.main(["brief", cadence])
        assert exit_code == bga.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert set(out.keys()) == set(ENVELOPE_KEYS)


# ---------------------------------------------------------------------------
# (n) Regression -- `load_family_records` returns records shaped
# `{"path": ..., "frontmatter": {...}}` (queue_family.load_family_records's
# own docstring, mirrored correctly by `queue_age_ping.py:100-101`'s
# `record.get("frontmatter") or {}` idiom). `readers_mise._read_backlog_readiness`
# and `readers_sweep._open_bug_backlog_records` previously read `status` off
# the record's TOP LEVEL instead -- always `None`, so:
#   - readers_mise: `None != "closed"` is always True, so every record counted
#     as open and the empty-queue judgment point could only fire on a
#     literally-empty family, never an all-closed one.
#   - readers_sweep: `None == "open"` is always False, so this returned an
#     EMPTY list unconditionally regardless of what was actually open.
# These tests are RED against the pre-fix top-level read and GREEN against
# the nested `frontmatter` read -- a test exercising a family whose records
# are genuinely all closed (mise) and a family with genuinely open records
# (sweep).
# ---------------------------------------------------------------------------


class TestFrontmatterNestedStatusRegression:
    def test_mise_all_closed_records_surface_empty_queue_judgment_point(self, monkeypatch):
        closed_records = [
            {"path": "state/bug-backlog/a.md", "frontmatter": {"status": "closed"}},
            {"path": "state/debt-backlog/b.md", "frontmatter": {"status": "closed"}},
        ]
        monkeypatch.setattr(
            bga.readers_mise_en_place,
            "_resolve_state_root",
            lambda: "/fake/repo/state",
        )
        monkeypatch.setattr(
            bga.readers_mise_en_place,
            "load_family_records",
            lambda family, repo_root: list(closed_records),
        )

        result = bga.readers_mise_en_place._read_backlog_readiness()

        jp_ids = [jp["id"] for jp in result.judgment_points]
        assert "j-mise-empty-queue-backlog" in jp_ids, (
            "an all-closed family must surface the empty-queue judgment "
            "point -- if this fails, `status` is being read off the "
            "record's top level again instead of `frontmatter`"
        )

    def test_sweep_open_records_are_not_dropped(self, monkeypatch):
        repo_root = Path("/fake/repo")
        open_records = [
            {"path": "state/bug-backlog/a.md", "frontmatter": {"status": "open"}},
            {"path": "state/bug-backlog/b.md", "frontmatter": {"status": "closed"}},
        ]
        monkeypatch.setattr(
            bga.readers_bug_sweep,
            "load_family_records",
            lambda family, root: list(open_records),
        )

        result = bga.readers_bug_sweep._open_bug_backlog_records(repo_root)

        assert [r["path"] for r in result] == ["state/bug-backlog/a.md"], (
            "genuinely open records must survive the filter -- if this "
            "returns [], `status` is being read off the record's top "
            "level again instead of `frontmatter`"
        )


# ---------------------------------------------------------------------------
# (o) C2 -- /mise Phase 6's review-scale verdict is COMPUTED by the engine,
# not evaluated as prose (docs/plans/2026-08-04-mise-phase-6-review-scale-is-
# computed-by.md, AC1-AC5).
#
# Four behavioural cases per AC5 -- brightline hit, below-threshold shallow
# run, unresolved range, and the Phase-0 call where the tail surface must
# stay silent -- plus AC1's no-second-oracle property asserted STRUCTURALLY,
# not only behaviourally, via two guards of unequal strength: the identity
# check (`test_reader_calls_the_shipped_decide_review_scale_by_identity`) is
# the real guard, proving the module CALLS the shipped
# `decide_review_scale` rather than binding a local re-implementation. The
# threshold-redeclaration grep alongside it is narrower than it may read --
# see that test's own docstring below for exactly what it does and does not
# catch.
#
# The git seam is faked at `_run_git_read_only` rather than at
# `_measure_range`, so the numstat/rev-list parsing this reader actually
# owns is exercised for real; only the subprocess itself is stubbed.
# ---------------------------------------------------------------------------

_MISE_START_SHA = "a1b2c3d4e5f6a7b8"

#: A finished PRIOR run's recorded start SHA -- a strictly older ancestor of
#: HEAD than `_MISE_START_SHA`. Its record stays committed forever (nothing
#: prunes `state/mise-inventory/`), which is why "2+ records" is history, not
#: a collision.
_MISE_PRIOR_SHA = "b" * 40

#: The run id the caller names via `--run-id` in these cases -- the record
#: is `state/mise-inventory/<run-id>.md` by construction (PIPELINE.md
#: Phase 1), so naming the run names the file.
_MISE_RUN_ID = "run-1"


def _write_mise_inventory_record(
    state_root: Path,
    run_id: str = "run-1",
    *,
    start_sha: Optional[str] = _MISE_START_SHA,
    record_run_id: bool = True,
) -> Path:
    """Write a Phase-1 inventory record (`PIPELINE.md` § Phase 1) under a
    tmp state root. `start_sha=None` writes a record with NO recorded start
    SHA -- the AC3 unresolvable-range case.

    Carries the REAL `identifier | spec path | disposition` row schema
    `PIPELINE.md` § Phase 1 specs (Review finding #3, 2026-08-04): the
    original two-column `item | disposition` shape here had no "spec path"
    column, so `_derive_baton_count` silently returned `None` for every
    AC5-core test using this fixture and those cases never exercised the
    reader against the record shape production actually writes. A single
    item citing one plan path yields `baton_count == 1`, which
    `decide_review_scale` treats identically to `None` (multiplier 1, no
    floor -- both require `baton_count >= 2`) -- so this fix changes what
    the AC5-core cases actually exercise without changing any of their
    expected values.

    shell-doc-ok: the backticked comparison above is a Python boolean
    expression quoted from the reader's own code, not a shell version
    constraint.

    `record_run_id=False` writes the SAME record with its `run_id:`
    frontmatter line omitted -- the pre-contract shape a Phase-1 scout
    writes today, before example-doctrine-repo agrees to produce the field. The two
    spellings are what the inert-until-produced cases below compare."""
    inventory_dir = state_root / "mise-inventory"
    inventory_dir.mkdir(parents=True, exist_ok=True)
    sha_line = f"start_sha: {start_sha}\n" if start_sha else ""
    run_id_line = f"run_id: {run_id}\n" if record_run_id else ""
    record = inventory_dir / f"{run_id}.md"
    record.write_text(
        "---\n"
        f"{run_id_line}"
        f"{sha_line}"
        "---\n\n"
        "| identifier | spec path | disposition |\n"
        "| ---------- | --------- | ----------- |\n"
        "| x-1        | docs/plans/2026-08-01-mise-baseline.md#x-1 | executed-and-PASSed |\n",
        encoding="utf-8",
    )
    return record


def _fake_git(*, commits: int, numstat_rows: list[tuple[int, int, str]], seen=None):
    """Stand in for `readers_mise._run_git_read_only`, answering the two
    read-only RANGE measurements the reader issues -- and nothing else.

    There is deliberately no `merge-base --is-ancestor` arm and no ancestry
    model any more (2026-08-04 carrier ratification, `cross-repo/inbox/
    2026-08-04-example-doctrine-repo-em-mise-run-id-carrier-env-breaks-windows.md`): the
    record is NAMED by the caller's `--run-id`, so nothing is selected by
    walking history. Any other git command reaching this stub returns `None`,
    which surfaces as the unresolved judgment point -- so a reinstated probe
    fails loudly here rather than passing on a helpful fixture.

    `seen` (a list) records every argv this stub was asked for, which is what
    `TestMiseRunIdentityInferenceIsDeletedNotDormant` asserts over."""

    def _run(args: list[str], cwd: Path) -> Optional[str]:
        if seen is not None:
            seen.append(list(args))
        if args[:2] == ["rev-list", "--count"]:
            return f"{commits}\n"
        if args[:2] == ["diff", "--numstat"]:
            return "".join(f"{a}\t{d}\t{p}\n" for a, d, p in numstat_rows)
        return None

    return _run


class TestMisePhase6ReviewScaleVerdict:
    def _arrange(
        self,
        monkeypatch,
        tmp_path,
        *,
        git=None,
        start_sha=_MISE_START_SHA,
        record=True,
        run_id=_MISE_RUN_ID,
    ):
        state_root = tmp_path / "state"
        state_root.mkdir(parents=True, exist_ok=True)
        if record:
            _write_mise_inventory_record(state_root, run_id, start_sha=start_sha)
        monkeypatch.setattr(
            bga.readers_mise_en_place, "_resolve_state_root", lambda: str(state_root)
        )
        if git is not None:
            monkeypatch.setattr(bga.readers_mise_en_place, "_run_git_read_only", git)
        return state_root

    # -- AC5 case 1: brightline hit -> partition mandatory -------------------

    def test_brightline_hit_yields_a_partition_mandatory_verdict(self, monkeypatch, tmp_path):
        self._arrange(
            monkeypatch,
            tmp_path,
            git=_fake_git(
                commits=12,
                numstat_rows=[(3000, 200, "coordinator_core/a.py")],
            ),
        )

        result = bga.readers_mise_en_place._read_phase_6_review_scale(_MISE_RUN_ID)

        assert result.judgment_points == []
        assert len(result.directives) == 1
        directive = result.directives[0]
        assert directive["verdict"]["partition_mandatory"] is True
        assert directive["verdict"]["scale"] == "partitioned"
        assert directive["verdict"]["row"] == 4
        assert directive["verdict"]["resolved"] is True
        assert directive["range"] == f"{_MISE_START_SHA}..HEAD"
        assert directive["metrics"] == {
            "gross_loc": 3200,
            "commit_count": 12,
            "surface_count": 1,
        }

    def test_brightline_hit_on_surface_count_alone(self, monkeypatch, tmp_path):
        # A tidy multi-baton mise run: small per-wave commits, few LOC, but
        # spread across many surfaces -- the exact shape the source memo says
        # under-reads as routine. >=4 distinct surfaces alone must trip it.
        self._arrange(
            monkeypatch,
            tmp_path,
            git=_fake_git(
                commits=2,
                numstat_rows=[
                    (5, 1, "coordinator_core/a.py"),
                    (5, 1, "docs/reference/b.md"),
                    (5, 1, "setup/c.json"),
                    (5, 1, "coordinator_core/tests/test_d.py"),
                ],
            ),
        )

        directive = bga.readers_mise_en_place._read_phase_6_review_scale(
            _MISE_RUN_ID
        ).directives[0]

        assert directive["metrics"]["surface_count"] == 4
        assert directive["verdict"]["partition_mandatory"] is True

    # -- AC5 case 2: below-threshold shallow run -> single reviewer ----------

    def test_below_threshold_shallow_run_yields_single_reviewer_with_justification(
        self, monkeypatch, tmp_path
    ):
        self._arrange(
            monkeypatch,
            tmp_path,
            git=_fake_git(commits=1, numstat_rows=[(9, 2, "coordinator_core/a.py")]),
        )

        result = bga.readers_mise_en_place._read_phase_6_review_scale(_MISE_RUN_ID)

        assert result.judgment_points == []
        directive = result.directives[0]
        assert directive["verdict"]["scale"] == "code-reviewer"
        assert directive["verdict"]["partition_mandatory"] is False
        assert directive["verdict"]["resolved"] is True
        # Single-reviewer is the EXCEPTION and needs its justification stated
        # (PIPELINE.md Phase 6) -- a bare `scale` string with no reason is the
        # silent fallthrough AC3 forbids.
        assert directive["verdict"]["reason"], "single-reviewer verdict must carry its justification"

    # -- AC4: the verdict names the existing CLI, and runs nothing -----------

    def test_verdict_names_the_existing_review_brightline_gate_cli_over_the_run_range(
        self, monkeypatch, tmp_path
    ):
        self._arrange(
            monkeypatch,
            tmp_path,
            git=_fake_git(commits=1, numstat_rows=[(9, 2, "coordinator_core/a.py")]),
        )

        directive = bga.readers_mise_en_place._read_phase_6_review_scale(
            _MISE_RUN_ID
        ).directives[0]

        assert directive["cli"] == "review-brightline-gate"
        assert directive["args"] == [f"{_MISE_START_SHA}..HEAD"]

    def test_the_tail_surface_writes_no_file(self, monkeypatch, tmp_path):
        # Review finding #4 (2026-08-04): calls the composed `collect()`
        # entrypoint -- what `brief()` actually invokes -- rather than the
        # private `_read_phase_6_review_scale()` sub-reader directly, so
        # AC4's "writes no file" guarantee is proven end to end through the
        # same call path production uses, not just for this one sub-reader
        # in isolation.
        state_root = self._arrange(
            monkeypatch,
            tmp_path,
            git=_fake_git(commits=1, numstat_rows=[(9, 2, "coordinator_core/a.py")]),
        )
        before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())

        bga.readers_mise_en_place.collect("mise-en-place", run_id=_MISE_RUN_ID)

        after = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
        assert before == after, f"{state_root} mutated by a read-only reader"

    # -- AC3/AC5 case 3: unresolved range -> judgment point, never a default -

    def test_missing_start_sha_yields_a_judgment_point_not_a_default_verdict(
        self, monkeypatch, tmp_path
    ):
        self._arrange(monkeypatch, tmp_path, start_sha=None)

        result = bga.readers_mise_en_place._read_phase_6_review_scale(_MISE_RUN_ID)

        assert result.directives == [], (
            "an unresolvable range must never produce a verdict directive -- "
            "that is the silent single-reviewer fallthrough AC3 forbids"
        )
        assert len(result.judgment_points) == 1
        jp = result.judgment_points[0]
        assert jp["id"] == "j-mise-phase-6-review-scale-unresolved"
        assert jp["recommendation"] is None, (
            "the source memo's mechanism 3 was a review-scale judgment point "
            "RECOMMENDING proceed-unresolved because its own verdict went "
            "unresolved -- this one carries no recommendation by construction"
        )
        assert "no recorded start SHA" in jp["evidence"]

    def test_unreadable_range_yields_a_judgment_point(self, monkeypatch, tmp_path):
        self._arrange(monkeypatch, tmp_path, git=lambda args, cwd: None)

        result = bga.readers_mise_en_place._read_phase_6_review_scale(_MISE_RUN_ID)

        assert result.directives == []
        assert [jp["id"] for jp in result.judgment_points] == [
            "j-mise-phase-6-review-scale-unresolved"
        ]

    def test_symbolic_start_sha_is_rejected_as_unrecorded(self, monkeypatch, tmp_path):
        # A stored `HEAD` re-resolves at READ time rather than naming the
        # commit the run started from -- the same defect the trail-range
        # builder rejects fail-loud in directives_review.
        self._arrange(monkeypatch, tmp_path, start_sha="HEAD")

        result = bga.readers_mise_en_place._read_phase_6_review_scale(_MISE_RUN_ID)

        assert result.directives == []
        assert len(result.judgment_points) == 1

    def test_unreadable_record_resolves_to_the_judgment_point_never_raises(
        self, monkeypatch, tmp_path
    ):
        # The record is read exactly once, in the reader; the OSError
        # direction must be ask, never raise, never a default verdict.
        state_root = self._arrange(monkeypatch, tmp_path)

        def _boom(self, *args, **kwargs):
            raise OSError("simulated unreadable inventory record")

        monkeypatch.setattr(Path, "read_text", _boom)

        result = bga.readers_mise_en_place._read_phase_6_review_scale(_MISE_RUN_ID)

        assert result.directives == []
        assert [jp["id"] for jp in result.judgment_points] == [
            "j-mise-phase-6-review-scale-unresolved"
        ]
        assert str(state_root) in result.judgment_points[0]["evidence"]
        assert "could not be read" in result.judgment_points[0]["evidence"]

    def test_nothing_raises_out_of_collect_for_any_unusable_input(
        self, monkeypatch, tmp_path
    ):
        # The sub-reader's failure modes are the reader's INTERNAL signals;
        # none may escape the composed entrypoint `brief()` actually calls.
        # (`AmbiguousRunRecordError` was the last such escape hatch and is
        # deleted along with the candidate set that could collide.)
        state_root = tmp_path / "state"
        state_root.mkdir(parents=True)
        _write_mise_inventory_record(state_root, "run-a", start_sha=None)
        monkeypatch.setattr(
            bga.readers_mise_en_place, "_resolve_state_root", lambda: str(state_root)
        )

        for run_id in (None, "run-a", "run-missing", "../escape"):
            result = bga.readers_mise_en_place.collect("mise-en-place", run_id=run_id)
            assert "d-mise-phase-6-review-scale" not in [
                d["id"] for d in result.directives
            ]
            assert "j-mise-phase-6-review-scale-unresolved" in [
                jp["id"] for jp in result.judgment_points
            ], f"run_id={run_id!r} produced no judgment point"

    # -- AC2/AC5 case 4: the Phase-0 call stays silent ----------------------

    def test_phase_0_call_with_no_inventory_directory_stays_silent(
        self, monkeypatch, tmp_path
    ):
        state_root = tmp_path / "state"
        state_root.mkdir(parents=True)
        monkeypatch.setattr(
            bga.readers_mise_en_place, "_resolve_state_root", lambda: str(state_root)
        )

        result = bga.readers_mise_en_place._read_phase_6_review_scale(None)

        assert result.directives == []
        assert result.judgment_points == [], (
            "Phase 0 runs before the inventory scout, so the tail surface has "
            "nothing to say -- nagging every Phase-0 call is what an on-disk "
            "self-gate exists to avoid"
        )

    def test_phase_0_silence_is_unchanged_even_when_a_run_id_is_supplied(
        self, monkeypatch, tmp_path
    ):
        # The new parameter must not turn the Phase-0 call into a nag: with
        # no `state/mise-inventory/` there is no Phase-6 surface to speak
        # about, whether or not the caller named a run.
        state_root = tmp_path / "state"
        state_root.mkdir(parents=True)
        monkeypatch.setattr(
            bga.readers_mise_en_place, "_resolve_state_root", lambda: str(state_root)
        )

        result = bga.readers_mise_en_place._read_phase_6_review_scale(_MISE_RUN_ID)

        assert result.directives == [] and result.judgment_points == []

    def test_collect_carries_the_tail_surface_only_for_its_own_cadence(
        self, monkeypatch, tmp_path
    ):
        self._arrange(
            monkeypatch,
            tmp_path,
            git=_fake_git(commits=12, numstat_rows=[(3000, 200, "coordinator_core/a.py")]),
        )

        mise = bga.readers_mise_en_place.collect("mise-en-place", run_id=_MISE_RUN_ID)
        other = bga.readers_mise_en_place.collect("bug-blitz", run_id=_MISE_RUN_ID)

        assert "d-mise-phase-6-review-scale" in [d["id"] for d in mise.directives]
        assert other.directives == [] and other.judgment_points == []


class TestMiseRunIdIsReadNeverInferred:
    """The consumer half of the run-identity carrier, ratified 2026-08-04
    (`cross-repo/inbox/2026-08-04-example-doctrine-repo-em-mise-run-id-carrier-env-
    breaks-windows.md`): `backlog-grind-assemble brief mise-en-place
    --run-id <run-id>` NAMES which `state/mise-inventory/<run-id>.md` record
    is the run asking, and the reader READS it.

    Three predicates preceded this flag and each picked a record nothing had
    named -- newest-mtime, one-record-else-halt, and start-SHA ancestry. The
    last was deleted rather than demoted to a fallback, on example-doctrine-repo's own
    reasoning: a fallback "quietly reactivates on any caller that forgets the
    flag", which is precisely the caller who most needs the loud failure. So
    the properties under test here are: the named record is the one measured,
    a record nothing named is NEVER measured, and a missing flag asks."""

    def _arrange_two_records(self, monkeypatch, tmp_path, *, git=None):
        """A finished PRIOR run's record beside the CURRENT one. Under the
        retired ancestry predicate these two were ORDERED and one of them
        won; now neither wins unless the caller names it."""
        state_root = tmp_path / "state"
        state_root.mkdir(parents=True, exist_ok=True)
        prior = _write_mise_inventory_record(
            state_root, "run-prior", start_sha=_MISE_PRIOR_SHA
        )
        current = _write_mise_inventory_record(
            state_root, _MISE_RUN_ID, start_sha=_MISE_START_SHA
        )
        monkeypatch.setattr(
            bga.readers_mise_en_place, "_resolve_state_root", lambda: str(state_root)
        )
        monkeypatch.setattr(
            bga.readers_mise_en_place,
            "_run_git_read_only",
            git or _fake_git(commits=1, numstat_rows=[(9, 2, "coordinator_core/a.py")]),
        )
        return state_root, prior, current

    # -- the named record is the measured record -----------------------------

    def test_the_named_record_is_selected_and_its_range_measured(
        self, monkeypatch, tmp_path
    ):
        _, prior, current = self._arrange_two_records(monkeypatch, tmp_path)

        result = bga.readers_mise_en_place._read_phase_6_review_scale(_MISE_RUN_ID)

        assert result.judgment_points == []
        directive = result.directives[0]
        assert directive["inventory_record"] == str(current)
        assert directive["range"] == f"{_MISE_START_SHA}..HEAD"
        assert str(prior) != directive["inventory_record"]

    def test_naming_the_older_record_measures_the_older_range(
        self, monkeypatch, tmp_path
    ):
        # The symmetric case, and the one no inference could ever serve: a
        # CONTINUANCE successor asking about a run whose start SHA is not the
        # most recent. The flag says which; the reader obeys.
        _, prior, _current = self._arrange_two_records(monkeypatch, tmp_path)

        result = bga.readers_mise_en_place._read_phase_6_review_scale("run-prior")

        assert result.judgment_points == []
        assert result.directives[0]["inventory_record"] == str(prior)
        assert result.directives[0]["range"] == f"{_MISE_PRIOR_SHA}..HEAD"

    def test_accumulated_history_is_irrelevant_to_the_named_lookup(
        self, monkeypatch, tmp_path
    ):
        # Records are committed, one per run, and nothing prunes them, so
        # N-historical is the steady state. Under the count predicate that
        # halted; under ancestry it forced an ordering; under a named lookup
        # it is simply not consulted.
        state_root = tmp_path / "state"
        state_root.mkdir(parents=True)
        for idx in range(6):
            _write_mise_inventory_record(
                state_root, f"run-old-{idx}", start_sha=f"{idx}" * 40
            )
        current = _write_mise_inventory_record(
            state_root, _MISE_RUN_ID, start_sha=_MISE_START_SHA
        )
        monkeypatch.setattr(
            bga.readers_mise_en_place, "_resolve_state_root", lambda: str(state_root)
        )
        monkeypatch.setattr(
            bga.readers_mise_en_place,
            "_run_git_read_only",
            _fake_git(commits=1, numstat_rows=[(9, 2, "coordinator_core/a.py")]),
        )

        result = bga.readers_mise_en_place._read_phase_6_review_scale(_MISE_RUN_ID)

        assert result.judgment_points == []
        assert result.directives[0]["inventory_record"] == str(current)

    # -- a record nothing named is never measured ----------------------------

    def test_a_run_id_naming_no_record_asks_and_falls_back_to_nothing(
        self, monkeypatch, tmp_path
    ):
        state_root, prior, current = self._arrange_two_records(monkeypatch, tmp_path)

        result = bga.readers_mise_en_place._read_phase_6_review_scale("run-typo")

        assert result.directives == [], (
            "a named record that is not on disk must never fall back to some "
            "OTHER record -- that fallback is the deleted inference wearing a "
            "typo for a costume"
        )
        assert len(result.judgment_points) == 1
        jp = result.judgment_points[0]
        assert jp["id"] == "j-mise-phase-6-review-scale-unresolved"
        assert jp["recommendation"] is None
        assert "run-typo" in jp["evidence"]
        assert str(prior) not in jp["evidence"] and str(current) not in jp["evidence"]

    def test_records_present_and_no_run_id_asks_naming_the_missing_flag(
        self, monkeypatch, tmp_path
    ):
        # THE loud failure the ratified memo asks for, in this surface's own
        # idiom: no raise, no crashed brief, no guess -- the unresolved
        # judgment point, naming the flag the caller forgot.
        self._arrange_two_records(monkeypatch, tmp_path)

        result = bga.readers_mise_en_place._read_phase_6_review_scale(None)

        assert result.directives == []
        assert len(result.judgment_points) == 1
        jp = result.judgment_points[0]
        assert jp["id"] == "j-mise-phase-6-review-scale-unresolved"
        assert jp["recommendation"] is None
        assert "--run-id" in jp["evidence"]
        assert "--run-id" in jp["question"]
        assert "name_the_run_id_and_rerun" in [
            d["value"] for d in jp["dispositions"]
        ], "the caller needs a disposition naming the fix, not just prose"

    def test_a_run_id_that_is_not_a_bare_filename_stem_asks_rather_than_reading(
        self, monkeypatch, tmp_path
    ):
        # A run id becomes a filename stem under `state/mise-inventory/`, so
        # a separator or a `..` in it would steer a read out of the inventory
        # directory. Read-only or not, the reader refuses to be steered --
        # and refuses in the same asking idiom, never by raising.
        state_root, _prior, _current = self._arrange_two_records(monkeypatch, tmp_path)
        outside = state_root.parent / "outside.md"
        outside.write_text(
            f"---\nrun_id: outside\nstart_sha: {_MISE_START_SHA}\n---\n",
            encoding="utf-8",
        )

        for hostile in ("../outside", "sub/run-1", "..", "/etc/passwd", "run-1\n"):
            # "run-1\n": review F3 -- `$` (no re.MULTILINE) matches before a
            # trailing "\n", so a naive regex would let this validate and
            # join a newline into the record path. `\Z` rejects it.
            result = bga.readers_mise_en_place._read_phase_6_review_scale(hostile)
            assert result.directives == [], f"{hostile!r} resolved to a verdict"
            assert len(result.judgment_points) == 1
            assert "not a usable run id" in result.judgment_points[0]["evidence"]

    def test_an_unreadable_named_record_asks_beside_a_perfectly_good_sibling(
        self, monkeypatch, tmp_path
    ):
        _, prior, current = self._arrange_two_records(monkeypatch, tmp_path)
        real_read_text = Path.read_text

        def _boom_for_current(self, *args, **kwargs):
            if self.name == current.name:
                raise OSError("simulated unreadable inventory record")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _boom_for_current)

        result = bga.readers_mise_en_place._read_phase_6_review_scale(_MISE_RUN_ID)

        assert result.directives == [], (
            "the NAMED record could not be read; measuring the readable "
            "sibling instead emits a resolved-looking verdict over a stale "
            "run's range -- the P1 this surface exists to prevent"
        )
        jp = result.judgment_points[0]
        assert str(current) in jp["evidence"] and "could not be read" in jp["evidence"]
        assert str(prior) not in jp["evidence"]

    def test_a_named_record_with_no_usable_start_sha_asks(self, monkeypatch, tmp_path):
        state_root, _prior, _current = self._arrange_two_records(monkeypatch, tmp_path)
        _write_mise_inventory_record(state_root, "run-nosha", start_sha=None)

        result = bga.readers_mise_en_place._read_phase_6_review_scale("run-nosha")

        assert result.directives == []
        jp = result.judgment_points[0]
        assert "run-nosha" in jp["evidence"]
        assert "no recorded start SHA" in jp["evidence"]

    # -- what the verdict discloses about its own provenance -----------------

    def test_run_identity_reports_the_named_id_and_the_flag_as_the_selector(
        self, monkeypatch, tmp_path
    ):
        self._arrange_two_records(monkeypatch, tmp_path)

        directive = bga.readers_mise_en_place._read_phase_6_review_scale(
            _MISE_RUN_ID
        ).directives[0]

        assert directive["run_identity"] == {
            "run_id": _MISE_RUN_ID,
            "recorded_run_id": _MISE_RUN_ID,
            "selected_by": bga.readers_mise_en_place._SELECTED_BY_RUN_ID_FLAG,
        }
        assert directive["run_identity"]["selected_by"] == "run-id-flag"

    def test_a_record_without_run_id_frontmatter_still_resolves_and_says_so(
        self, monkeypatch, tmp_path
    ):
        # The record's own `run_id:` is its CLAIM about itself; the caller's
        # flag is what selected it. A record that predates the scout stamping
        # the field reaches an identical verdict.
        state_root = tmp_path / "state"
        state_root.mkdir(parents=True)
        _write_mise_inventory_record(state_root, _MISE_RUN_ID, record_run_id=False)
        monkeypatch.setattr(
            bga.readers_mise_en_place, "_resolve_state_root", lambda: str(state_root)
        )
        monkeypatch.setattr(
            bga.readers_mise_en_place,
            "_run_git_read_only",
            _fake_git(commits=1, numstat_rows=[(9, 2, "coordinator_core/a.py")]),
        )

        directive = bga.readers_mise_en_place._read_phase_6_review_scale(
            _MISE_RUN_ID
        ).directives[0]

        assert directive["run_identity"]["run_id"] == _MISE_RUN_ID
        assert directive["run_identity"]["recorded_run_id"] is None
        assert directive["verdict"]["resolved"] is True

    def test_recorded_run_id_is_disclosed_not_enforced_when_it_disagrees(
        self, monkeypatch, tmp_path
    ):
        # A record whose frontmatter claims a different id than its filename
        # is a scout bug worth SEEING; it is not this reader's licence to
        # re-derive the selection from content, which is the inference.
        state_root = tmp_path / "state"
        state_root.mkdir(parents=True)
        inventory = state_root / "mise-inventory"
        inventory.mkdir()
        (inventory / f"{_MISE_RUN_ID}.md").write_text(
            f"---\nrun_id: run-something-else\nstart_sha: {_MISE_START_SHA}\n---\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            bga.readers_mise_en_place, "_resolve_state_root", lambda: str(state_root)
        )
        monkeypatch.setattr(
            bga.readers_mise_en_place,
            "_run_git_read_only",
            _fake_git(commits=1, numstat_rows=[(9, 2, "coordinator_core/a.py")]),
        )

        directive = bga.readers_mise_en_place._read_phase_6_review_scale(
            _MISE_RUN_ID
        ).directives[0]

        assert directive["run_identity"]["run_id"] == _MISE_RUN_ID
        assert directive["run_identity"]["recorded_run_id"] == "run-something-else"


class TestMiseRunIdentityInferenceIsDeletedNotDormant:
    """Deleted, not demoted -- the memo's own words: "I would rather see the
    inference path deleted than kept as a fallback that quietly reactivates
    on any caller that forgets the flag."

    A dormant fallback is invisible to every behavioural test that supplies
    the flag, so these assert over the module itself: the names are gone, no
    ancestry probe is issued on ANY path, and no second carrier (env var,
    session state) was added in its place."""

    def _arrange(self, monkeypatch, tmp_path, seen):
        state_root = tmp_path / "state"
        state_root.mkdir(parents=True)
        _write_mise_inventory_record(state_root, "run-prior", start_sha=_MISE_PRIOR_SHA)
        _write_mise_inventory_record(state_root, _MISE_RUN_ID, start_sha=_MISE_START_SHA)
        monkeypatch.setattr(
            bga.readers_mise_en_place, "_resolve_state_root", lambda: str(state_root)
        )
        monkeypatch.setattr(
            bga.readers_mise_en_place,
            "_run_git_read_only",
            _fake_git(
                commits=1,
                numstat_rows=[(9, 2, "coordinator_core/a.py")],
                seen=seen,
            ),
        )

    @pytest.mark.parametrize("run_id", [_MISE_RUN_ID, "run-prior", "run-missing", None])
    def test_no_ancestry_probe_is_issued_on_any_path(
        self, monkeypatch, tmp_path, run_id
    ):
        seen: list[list[str]] = []
        self._arrange(monkeypatch, tmp_path, seen)

        bga.readers_mise_en_place.collect("mise-en-place", run_id=run_id)

        if run_id in (_MISE_RUN_ID, "run-prior"):
            assert seen, "expected the range measurements on the resolved path"
        for args in seen:
            assert args[0] != "merge-base", (
                f"an ancestry probe was issued for run_id={run_id!r}: {args} -- "
                "the selection predicate is the caller's flag, not history"
            )
            assert "--is-ancestor" not in args

    def test_the_retired_selection_machinery_is_gone_from_the_module(self):
        reader = bga.readers_mise_en_place
        for name in (
            "_ancestry_probe",
            "_current_run_record",
            "_RecordSelection",
            "AmbiguousRunRecordError",
            "_SELECTED_BY_ANCESTRY",
            "_RUN_IDENTITY_INFERENCE_NOTE",
        ):
            assert not hasattr(reader, name), (
                f"{name} survives -- a retired inference kept as a module "
                "attribute is one edit away from being a live fallback"
            )

    def test_no_git_call_in_the_module_names_an_ancestry_verb(self):
        # AST over the CODE, so the negative-spec prose that NAMES the
        # retired probe in a docstring cannot satisfy (or trip) this check.
        source = Path(bga.readers_mise_en_place.__file__).read_text(encoding="utf-8")
        code_literals = _code_string_literals(source)
        offending = [
            lit for lit in code_literals if "merge-base" in lit or "is-ancestor" in lit
        ]
        assert offending == [], (
            f"an ancestry git verb is still spelled in executable code: {offending}"
        )

    def test_the_reader_reads_no_environment_or_session_state_for_a_run_id(self):
        """The second-carrier refusal, pinned. `MISE_RUN_ID` as an env
        fallback was ruled out on 2026-08-04 (PM call, example-doctrine-repo concurring):
        a second carrier is a second way to be wrong, an inline
        `VAR=value command` prefix is not a line `cmd.exe` parses on the
        Windows launcher path, and each EM Bash call is a fresh shell so an
        `export` never survives to the next. A future edit that reaches for
        one has to delete this test first, and read why here."""
        source = Path(bga.readers_mise_en_place.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        assert not any("session" in name for name in imported), (
            f"reader imports a session module: {sorted(imported)}"
        )
        assert "os" not in imported and "tempfile" not in imported

        env_reads = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}
        ]
        assert env_reads == [], "the reader must not resolve a run id from the environment"
        assert not [
            lit for lit in _code_string_literals(source) if "MISE_RUN_ID" in lit
        ], "no env carrier for the run id, not even behind a documented alias"


def _write_mise_inventory_record_with_item_table(
    state_root: Path,
    run_id: str,
    *,
    header: str,
    rows: list[str],
    start_sha: str = _MISE_START_SHA,
    trailing_body: str = "",
) -> Path:
    """A Phase-1 inventory record carrying a realistic item table (`|
    identifier | spec path | ... |` per `PIPELINE.md` § Phase 1) --
    the fixture `_derive_baton_count` reads its "spec path" column from.

    `trailing_body` appends further prose/tables AFTER the item table, for
    the multi-table cases: the record format is prose-authored and
    schema-unvalidated, so nothing stops a scout adding a legend or notes
    table below the item table."""
    inventory_dir = state_root / "mise-inventory"
    inventory_dir.mkdir(parents=True, exist_ok=True)
    record = inventory_dir / f"{run_id}.md"
    sep = "| " + " | ".join("---" for _ in header.split("|") if _) + " |"
    body = "\n".join(rows)
    tail = f"\n{trailing_body}\n" if trailing_body else ""
    record.write_text(
        "---\n"
        f"run_id: {run_id}\n"
        f"start_sha: {start_sha}\n"
        "---\n\n"
        f"{header}\n{sep}\n{body}\n{tail}",
        encoding="utf-8",
    )
    return record


def _baton_count_of(record: Path) -> Optional[int]:
    """`_derive_baton_count` takes the record's already-read TEXT, not its
    `Path` (2026-08-04 review-integration pass: the record is read exactly
    once, in `_read_phase_6_review_scale`, and the text threaded into both
    derivations). These cases still author a real on-disk record, so this
    reads it the same way the single production read does."""
    return bga.readers_mise_en_place._derive_baton_count(
        record.read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# (p) baton_count derivation (`_derive_baton_count`) -- 2026-08-04 sizing
# (`state/sizings/2026-08-04-mise-run-record-should-carry-baton-count.yaml`),
# source memo `cross-repo/inbox/2026-08-04-example-retrieval-repo-em-brightline-
# partition-mandatory-does-not-halt.md`. Derives from the record's OWN item
# table content -- no new authored frontmatter field -- and fails closed to
# `None`, never a default of `1`.
# ---------------------------------------------------------------------------


class TestDeriveBatonCount:
    def test_two_distinct_plans_in_spec_path_column_yield_baton_count_two(self, tmp_path):
        record = _write_mise_inventory_record_with_item_table(
            tmp_path,
            "run-joint",
            header="| identifier | spec path | disposition |",
            rows=[
                "| DOCTRINE-C1 | docs/plans/2026-07-30-doctrine.md#C1 | executed-and-PASSed |",
                "| DOCTRINE-C2 | docs/plans/2026-07-30-doctrine.md#C2 | executed-and-PASSed |",
                "| RESIDUE-C9  | docs/plans/2026-07-30-residue.md#C9  | executed-and-PASSed |",
            ],
        )

        assert _baton_count_of(record) == 2

    def test_single_plan_across_many_items_yields_baton_count_one(self, tmp_path):
        record = _write_mise_inventory_record_with_item_table(
            tmp_path,
            "run-solo",
            header="| identifier | spec path | disposition |",
            rows=[
                "| C1 | docs/plans/2026-08-01-solo.md#C1 | executed-and-PASSed |",
                "| C2 | docs/plans/2026-08-01-solo.md#C2 | executed-and-PASSed |",
                "| C3 | docs/plans/2026-08-01-solo.md#C3 | executed-and-PASSed |",
            ],
        )

        assert _baton_count_of(record) == 1

    def test_handoff_and_todo_batons_also_counted(self, tmp_path):
        record = _write_mise_inventory_record_with_item_table(
            tmp_path,
            "run-mixed",
            header="| identifier | spec path | disposition |",
            rows=[
                "| a | state/handoffs/2026-08-01-x.md | executed-and-PASSed |",
                "| b | tasks/foo/todo.md | executed-and-PASSed |",
            ],
        )

        assert _baton_count_of(record) == 2

    def test_no_spec_path_column_yields_none_never_one(self, tmp_path):
        record = _write_mise_inventory_record_with_item_table(
            tmp_path,
            "run-noheader",
            header="| identifier | disposition |",
            rows=["| x-1 | executed-and-PASSed |"],
        )

        assert _baton_count_of(record) is None

    def test_no_table_at_all_yields_none(self, tmp_path):
        state_root = tmp_path / "state"
        inventory_dir = state_root / "mise-inventory"
        inventory_dir.mkdir(parents=True)
        record = inventory_dir / "run-prose.md"
        record.write_text(
            "---\nrun_id: run-prose\nstart_sha: " + _MISE_START_SHA + "\n---\n\n"
            "Just prose, no table at all -- nothing to derive from.\n",
            encoding="utf-8",
        )

        assert _baton_count_of(record) is None

    def test_spec_path_column_present_but_unrecognizable_paths_yields_none(self, tmp_path):
        record = _write_mise_inventory_record_with_item_table(
            tmp_path,
            "run-ambiguous",
            header="| identifier | spec path | disposition |",
            rows=["| x-1 | (see PM's chat message) | executed-and-PASSed |"],
        )

        assert _baton_count_of(record) is None

    def test_empty_record_text_yields_none(self, tmp_path):
        # The former missing-file case: with the single-read refactor the
        # OSError direction is the CALLER's (see
        # `test_unreadable_record_resolves_to_the_judgment_point_never_
        # raises`), and this function's own degenerate input is empty text.
        assert bga.readers_mise_en_place._derive_baton_count("") is None

    # -- per-table scoping (P1 review finding, 2026-08-04) ------------------

    def test_second_table_after_the_item_table_does_not_extend_the_first(
        self, tmp_path
    ):
        # The original defect: `header_cells`/`spec_path_col` were derived
        # ONCE for the whole document, so every row of a second pipe table
        # was read as more body of the FIRST table, at the first table's
        # column index. Here the legend table's column 1 is prose, so the
        # pre-fix reader found nothing extra -- but it was reading unrelated
        # cells to decide that, and the shape is one edit away from the
        # sibling case below.
        record = _write_mise_inventory_record_with_item_table(
            tmp_path,
            "run-legend",
            header="| identifier | spec path | disposition |",
            rows=[
                "| C1 | docs/plans/2026-08-04-solo.md#C1 | executed-and-PASSed |",
            ],
            trailing_body=(
                "\n## Legend\n\n"
                "| code | meaning |\n"
                "| ---- | ------- |\n"
                "| P    | executed and PASSed |\n"
            ),
        )

        assert _baton_count_of(record) == 1, (
            "a legend table below the item table must not contribute to, or "
            "reshape, the item table's derived baton count"
        )

    def test_second_table_holding_baton_shaped_text_at_the_same_index_is_ignored(
        self, tmp_path
    ):
        # The P1 in its dangerous form: the second table's column at the
        # SAME index as the item table's spec-path column holds baton-shaped
        # paths (a "related specs" notes table -- entirely plausible in a
        # prose-authored record). Without the per-table reset those rows are
        # read as item rows, and the count resolves to 3 instead of 1 --
        # resolved-but-WRONG, which then multiplies decide_review_scale's
        # row-4 metrics. Only the real item table may count.
        record = _write_mise_inventory_record_with_item_table(
            tmp_path,
            "run-notes",
            header="| identifier | spec path | disposition |",
            rows=[
                "| C1 | docs/plans/2026-08-04-solo.md#C1 | executed-and-PASSed |",
            ],
            trailing_body=(
                "\n## Related reading (not items in this run)\n\n"
                "| topic | reference | note |\n"
                "| ----- | --------- | ---- |\n"
                "| prior art | docs/plans/2026-07-30-doctrine.md | background |\n"
                "| carryover | state/handoffs/2026-08-01-x.md   | background |\n"
            ),
        )

        assert _baton_count_of(record) == 1, (
            "a second table's cells at the item table's spec-path index must "
            "not be counted as batons -- that is the resolved-but-wrong count "
            "the per-table reset exists to prevent"
        )

    def test_a_later_table_that_has_its_own_spec_path_header_still_counts(
        self, tmp_path
    ):
        # The reset must scope per table, not stop scanning after the first
        # one: a record whose items are split across two item tables (each
        # with its own header) still derives from both.
        record = _write_mise_inventory_record_with_item_table(
            tmp_path,
            "run-split",
            header="| identifier | spec path | disposition |",
            rows=["| C1 | docs/plans/2026-08-04-a.md#C1 | executed-and-PASSed |"],
            trailing_body=(
                "\n## Second wave\n\n"
                "| identifier | spec path | disposition |\n"
                "| ---------- | --------- | ----------- |\n"
                "| C2 | docs/plans/2026-08-04-b.md#C2 | executed-and-PASSed |\n"
            ),
        )

        assert _baton_count_of(record) == 2

    # -- separator normalization (review finding 3, 2026-08-04) ------------

    def test_windows_shaped_spec_path_counts_rather_than_being_dropped(
        self, tmp_path
    ):
        # `_BATON_SPEC_PATH_RE`'s character class is forward-slash-only, so a
        # backslash-separated cell used to miss entirely -- dropping that
        # baton from the count while still returning a resolved number. A
        # silent UNDERCOUNT is worse than `None`: it moves the row-4 metrics
        # toward reviewing less. Windows is first-class here.
        record = _write_mise_inventory_record_with_item_table(
            tmp_path,
            "run-windows",
            header="| identifier | spec path | disposition |",
            rows=[
                r"| C1 | docs\plans\2026-08-04-a.md#C1 | executed-and-PASSed |",
            ],
        )

        assert _baton_count_of(record) == 1, (
            "a backslash-separated spec path must count, not be silently "
            "dropped from the derived baton count"
        )

    def test_windows_and_posix_spellings_of_one_plan_are_the_same_baton(
        self, tmp_path
    ):
        # Normalization, not a second alternation: the same plan written
        # both ways is ONE top-level artifact, so it must not double-count.
        record = _write_mise_inventory_record_with_item_table(
            tmp_path,
            "run-mixed-seps",
            header="| identifier | spec path | disposition |",
            rows=[
                r"| C1 | docs\plans\2026-08-04-a.md#C1 | executed-and-PASSed |",
                "| C2 | docs/plans/2026-08-04-a.md#C2 | executed-and-PASSed |",
                r"| C3 | state\handoffs\2026-08-01-x.md | executed-and-PASSed |",
            ],
        )

        assert _baton_count_of(record) == 2

    def test_end_to_end_baton_count_multiplier_flows_into_the_computed_verdict(
        self, monkeypatch, tmp_path
    ):
        # 260 gross_loc alone sits well under the 500 brightline; this
        # reader's call site always passes executor_dispatched=True, so
        # the floor is moot here -- this exercises the MULTIPLIER, the
        # part of baton_count semantics this reader's own call site can
        # actually reach: a 2-baton run's derived count doubles 260 to
        # 520, tripping row 4, where a 1-baton run of the same shape would
        # not.
        state_root = tmp_path / "state"
        state_root.mkdir(parents=True)
        _write_mise_inventory_record_with_item_table(
            state_root,
            "run-joint",
            header="| identifier | spec path | disposition |",
            rows=[
                "| A-C1 | docs/plans/2026-08-04-a.md#C1 | executed-and-PASSed |",
                "| B-C1 | docs/plans/2026-08-04-b.md#C1 | executed-and-PASSed |",
            ],
        )
        monkeypatch.setattr(
            bga.readers_mise_en_place, "_resolve_state_root", lambda: str(state_root)
        )
        monkeypatch.setattr(
            bga.readers_mise_en_place,
            "_run_git_read_only",
            _fake_git(commits=1, numstat_rows=[(260, 0, "coordinator_core/a.py")]),
        )

        directive = bga.readers_mise_en_place._read_phase_6_review_scale(
            "run-joint"
        ).directives[0]

        assert directive["verdict"]["row"] == 4, (
            "derived baton_count=2 must multiply gross_loc (260*2=520) past "
            "the brightline"
        )
        assert directive["verdict"]["scale"] == "partitioned"
        assert directive["verdict"]["partition_mandatory"] is True

    def test_single_baton_run_of_the_same_shape_does_not_trip_the_brightline(
        self, monkeypatch, tmp_path
    ):
        state_root = tmp_path / "state"
        state_root.mkdir(parents=True)
        _write_mise_inventory_record_with_item_table(
            state_root,
            "run-solo",
            header="| identifier | spec path | disposition |",
            rows=[
                "| C1 | docs/plans/2026-08-04-solo.md#C1 | executed-and-PASSed |",
                "| C2 | docs/plans/2026-08-04-solo.md#C2 | executed-and-PASSed |",
            ],
        )
        monkeypatch.setattr(
            bga.readers_mise_en_place, "_resolve_state_root", lambda: str(state_root)
        )
        monkeypatch.setattr(
            bga.readers_mise_en_place,
            "_run_git_read_only",
            _fake_git(commits=1, numstat_rows=[(260, 0, "coordinator_core/a.py")]),
        )

        directive = bga.readers_mise_en_place._read_phase_6_review_scale(
            "run-solo"
        ).directives[0]

        assert directive["verdict"]["row"] == 3, (
            "a derived baton_count of 1 must leave row-4 selection unchanged "
            "-- 260 alone never trips the brightline"
        )


class TestMisePhase6IsACallerNotASecondOracle:
    """AC1's no-second-oracle property, asserted structurally rather than
    only behaviourally. `test_reader_calls_the_shipped_decide_review_scale_
    by_identity` below is the test doing the real load-bearing work here
    (an `is`-identity check proving the module calls the shipped function
    rather than binding a local re-implementation of the same name); the
    two sibling tests are narrower lexical checks layered on top, not
    independent proofs of the same property -- each says plainly, in its
    own docstring/comment, exactly what it does and does not catch (review
    findings #1/#2, 2026-08-04)."""

    def test_reader_calls_the_shipped_decide_review_scale_by_identity(self):
        from coordinator_core.workstream_complete import directives_review

        assert (
            bga.readers_mise_en_place.decide_review_scale
            is directives_review.decide_review_scale
        ), (
            "readers_mise must CALL the shipped decide_review_scale, never "
            "bind a local re-implementation of the same name"
        )

    def test_no_brightline_threshold_constant_is_redeclared_under_the_package(self):
        # A narrow LEXICAL check, not a direct assertion of AC1's
        # no-second-oracle property: this only catches a second copy that
        # redeclares one of these four exact identifier names as an
        # assignment target. A hand-rolled predicate that inlines the
        # literals (`if gross_loc >= 500 or commit_count >= 5 ...`) or
        # shell-doc-ok: the bracketed condition above is a Python literal
        # example, not a shell version constraint.
        # names its constants differently (`_BIG_DIFF_LOC = 500`) satisfies
        # this test while reintroducing exactly the second-oracle drift AC1
        # forbids -- the sibling identity test above is the actual guard
        # against that; this one adds a cheap belt-and-braces catch for the
        # literal-restatement case only.
        pattern = re.compile(
            r"_BRIGHTLINE_(?:LOC|COMMITS|SURFACES)\s*[:=]|"
            r"_SMALL_FIX_LOC_CEILING\s*[:=]"
        )
        offending: list[str] = []
        for py_file in _PACKAGE_DIR.rglob("*.py"):
            if py_file.name.startswith("test_"):
                continue
            if pattern.search(py_file.read_text(encoding="utf-8")):
                offending.append(str(py_file.relative_to(_REPO_ROOT)))
        assert offending == [], (
            f"a second copy of the big-diff brightline thresholds exists in "
            f"{offending} -- call decide_review_scale instead of restating "
            "its constants"
        )

    def test_reader_declares_no_local_review_scale_decision_function(self):
        # Also narrow: a local re-implementation named anything other than
        # these two exact strings (`def _decide_scale(...)`,
        # `def _resolve_verdict(...)`) passes this check while being
        # exactly the second oracle AC1 forbids. Cheap and catches the
        # literal-name case, but the property this class exists to assert
        # is carried by `test_reader_calls_the_shipped_decide_review_scale_
        # by_identity` above, not by this test alone.
        source = (_PACKAGE_DIR / "readers_mise.py").read_text(encoding="utf-8")
        assert "def decide_review_scale" not in source
        assert "def _unresolved(" not in source


# ---------------------------------------------------------------------------
# (j) engine-minted /mise run identity -- AC1-AC5, AC7
# `docs/plans/2026-08-04-engine-minted-mise-run-identity.md`, chunk C1.
# ---------------------------------------------------------------------------


class TestMintRunIdSelfGating:
    """`readers_mise.mint_run_id` mirrors `collect()`'s own cadence
    self-gate exactly: `None` for every cadence but `mise-en-place`, never a
    per-cadence branch anywhere else."""

    def test_non_mise_cadence_abstains(self, tmp_path):
        for cadence in bga.CADENCES:
            if cadence == "mise-en-place":
                continue
            assert (
                bga.readers_mise_en_place.mint_run_id(cadence, repo_root=tmp_path)
                is None
            )

    def test_bogus_cadence_abstains(self, tmp_path):
        assert (
            bga.readers_mise_en_place.mint_run_id("bogus-cadence", repo_root=tmp_path)
            is None
        )


class TestMintRunIdShapeAC1:
    """AC1: a mint returns the run id plus the `state/mise-inventory/
    <run_id>.md` path it implies -- the same join `_named_run_record` would
    later derive from that same id."""

    def test_mint_returns_run_id_and_matching_inventory_path(self, tmp_path):
        minted = bga.readers_mise_en_place.mint_run_id(
            "mise-en-place", repo_root=tmp_path
        )
        assert minted is not None
        assert minted.run_id
        assert minted.inventory_path == f"state/mise-inventory/{minted.run_id}.md"

    def test_mint_inventory_path_is_repo_relative_posix(self, tmp_path):
        # AC1 specifies `state/mise-inventory/<run_id>.md` -- repo-relative,
        # forward-slash, on every platform (never a machine-absolute path:
        # backslash-separated on Windows, and meaningless to the sibling-repo
        # hook that consumes this value). Not `str(Path(...))` of an
        # absolute path, which platform-separates.
        minted = bga.readers_mise_en_place.mint_run_id(
            "mise-en-place", repo_root=tmp_path
        )
        assert "\\" not in minted.inventory_path
        assert minted.inventory_path.startswith("state/mise-inventory/")
        assert not minted.inventory_path.startswith(str(tmp_path))

    def test_cli_mint_run_id_prints_run_id_and_inventory_path(self, tmp_path, capsys, monkeypatch):
        state_root = tmp_path / "state"
        monkeypatch.setattr(
            bga.readers_mise_en_place, "_resolve_state_root", lambda: str(state_root)
        )
        exit_code = bga.main(["mint-run-id", "mise-en-place"])
        assert exit_code == bga.EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert set(payload) == {"run_id", "inventory_path"}
        assert payload["inventory_path"].endswith(
            f"mise-inventory/{payload['run_id']}.md"
        )


class TestMintRunIdSatisfiesArgRegexAC2:
    """AC2: every minted run id satisfies `_RUN_ID_ARG_RE` -- the existing
    caller-supplied-argument regex, referenced, never re-declared. A minted
    id is by construction a value the very next `brief --run-id` accepts."""

    def test_minted_id_matches_the_existing_arg_regex(self, tmp_path):
        for _ in range(20):
            minted = bga.readers_mise_en_place.mint_run_id(
                "mise-en-place", repo_root=tmp_path
            )
            assert bga.readers_mise_en_place._RUN_ID_ARG_RE.match(minted.run_id)

    def test_minted_id_is_immediately_accepted_by_named_run_record(self, tmp_path, monkeypatch):
        # A minted id, once its record exists on disk, resolves through the
        # SAME `_named_run_record` lookup a caller-supplied `--run-id` does
        # -- proving AC2's claim end to end, not just against the regex.
        minted = bga.readers_mise_en_place.mint_run_id(
            "mise-en-place", repo_root=tmp_path
        )
        state_root = tmp_path / "state"
        inventory_dir = state_root / "mise-inventory"
        inventory_dir.mkdir(parents=True)
        (inventory_dir / f"{minted.run_id}.md").write_text(
            "---\nstart_sha: 0123456789abcdef0123456789abcdef01234567\n---\n"
        )
        lookup = bga.readers_mise_en_place._named_run_record(
            inventory_dir, minted.run_id
        )
        assert lookup.record is not None, (
            f"minted id {minted.run_id!r} was rejected by _named_run_record: "
            f"{lookup.reason}"
        )

    def test_no_second_regex_is_declared_for_minting(self):
        # Belt-and-braces lexical check alongside AC7's own structural test
        # below -- `mint_run_id` must reference `_RUN_ID_ARG_RE`, never bind
        # a second pattern under a different name for the same shape.
        source = (_PACKAGE_DIR / "readers_mise.py").read_text(encoding="utf-8")
        mint_start = source.index("def mint_run_id")
        mint_body = source[mint_start : source.index("\ndef collect(")]
        assert "_RUN_ID_ARG_RE" in mint_body
        assert "re.compile" not in mint_body


class TestMintRunIdReadOnlyAC3:
    """AC3: minting is read-only -- no directory is created, no file is
    written, no git state mutates. Minted against a tmp repo root; asserts
    `state/mise-inventory/` still does not exist afterward."""

    def test_minting_creates_no_directory_or_file(self, tmp_path):
        files_before = sorted(p for p in tmp_path.rglob("*"))
        minted = bga.readers_mise_en_place.mint_run_id(
            "mise-en-place", repo_root=tmp_path
        )
        assert minted is not None
        assert not (tmp_path / "state").exists(), (
            "mint_run_id must not create state/ (and therefore not "
            "state/mise-inventory/ either) -- its own presence is the "
            "Phase-0-vs-Phase-6 self-gate this reader relies on elsewhere"
        )
        assert not (tmp_path / "state" / "mise-inventory").exists()
        files_after = sorted(p for p in tmp_path.rglob("*"))
        assert files_before == files_after == []


class TestMintRunIdUniquenessAC4:
    """AC4: two mints in the same process and the same clock second return
    different run_ids, and neither collides with an existing
    `state/mise-inventory/<id>.md` on disk."""

    def test_two_mints_in_same_process_differ(self, tmp_path):
        first = bga.readers_mise_en_place.mint_run_id(
            "mise-en-place", repo_root=tmp_path
        )
        second = bga.readers_mise_en_place.mint_run_id(
            "mise-en-place", repo_root=tmp_path
        )
        assert first.run_id != second.run_id

    def test_frozen_clock_still_yields_distinct_ids(self, tmp_path, monkeypatch):
        # Pin the timestamp stem so only the random suffix can vary --
        # the same-second case AC4 names explicitly.
        class _FrozenDatetime(bga.readers_mise_en_place.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 4, 12, 0, 0, tzinfo=tz)

        monkeypatch.setattr(
            bga.readers_mise_en_place, "datetime", _FrozenDatetime
        )
        minted_ids = {
            bga.readers_mise_en_place.mint_run_id(
                "mise-en-place", repo_root=tmp_path
            ).run_id
            for _ in range(5)
        }
        assert len(minted_ids) == 5

    def test_mint_avoids_a_run_id_with_an_existing_record_on_disk(self, tmp_path, monkeypatch):
        inventory_dir = tmp_path / "state" / "mise-inventory"
        inventory_dir.mkdir(parents=True)
        (inventory_dir / "collide-1234abcd.md").write_text("taken")

        real_generate = bga.readers_mise_en_place._generate_candidate_run_id
        calls = {"n": 0}

        def _fake_generate():
            calls["n"] += 1
            if calls["n"] == 1:
                return "collide-1234abcd"
            return real_generate()

        monkeypatch.setattr(
            bga.readers_mise_en_place, "_generate_candidate_run_id", _fake_generate
        )
        minted = bga.readers_mise_en_place.mint_run_id(
            "mise-en-place", repo_root=tmp_path
        )
        assert minted.run_id != "collide-1234abcd"
        assert calls["n"] >= 2


class TestMintRunIdCadenceDispatchAC5:
    """AC5: `mint-run-id` for a cadence no reader claims exits with the
    usage code (2) and a message naming the cadence, never exit 0 with a
    mint. Same for a missing/unknown cadence."""

    def test_unclaimed_cadence_is_usage_error_naming_the_cadence(self, capsys, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        exit_code = bga.main(["mint-run-id", "bug-blitz"])
        assert exit_code == bga.EXIT_USAGE
        err = capsys.readouterr().err
        assert "bug-blitz" in err

    def test_unknown_cadence_is_usage_error(self, capsys, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        exit_code = bga.main(["mint-run-id", "not-a-real-cadence"])
        assert exit_code == bga.EXIT_USAGE
        err = capsys.readouterr().err
        assert "not-a-real-cadence" in err

    def test_missing_cadence_is_usage_error(self, capsys):
        exit_code = bga.main(["mint-run-id"])
        assert exit_code == bga.EXIT_USAGE

    def test_extra_argument_is_usage_error(self, capsys):
        exit_code = bga.main(["mint-run-id", "mise-en-place", "extra"])
        assert exit_code == bga.EXIT_USAGE

    def test_never_exits_0_without_a_mint(self, capsys, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        for argv in (
            ["mint-run-id", "bug-blitz"],
            ["mint-run-id", "not-a-real-cadence"],
            ["mint-run-id"],
        ):
            assert bga.main(argv) != bga.EXIT_OK


class TestMintRunIdInitDoesNotKnowRunIdShapeAC7:
    """AC7: `__init__.py` still contains no branch on what a run id MEANS
    -- the mint dispatch asks readers and does not itself construct, parse,
    or validate an id. Structural test greps the module for the absence of
    a locally-declared run-id pattern."""

    def test_init_declares_no_run_id_regex_or_re_compile(self):
        source = (_PACKAGE_DIR / "__init__.py").read_text(encoding="utf-8")
        assert "re.compile" not in source
        assert "_RUN_ID_ARG_RE" not in source
        assert not re.search(r"^import re$", source, re.MULTILINE)

    def test_main_mint_run_id_never_touches_run_id_string_shape(self):
        # `_main_mint_run_id` may read `.run_id`/`.inventory_path` off the
        # reader's returned object and forward the cadence string verbatim
        # -- it must not slice, regex, or otherwise interpret the run id's
        # OWN characters.
        source = (_PACKAGE_DIR / "__init__.py").read_text(encoding="utf-8")
        start = source.index("def _main_mint_run_id")
        end = source.index("\ndef main(")
        body = source[start:end]
        assert ".run_id[" not in body
        assert "run_id.split" not in body
        assert "run_id.replace" not in body

    def test_mint_run_id_dispatch_takes_first_non_none_result(self, tmp_path, monkeypatch):
        calls: list[str] = []

        def _abstain(cadence, **_kw):
            calls.append(bga.readers_bug_blitz.__name__)
            return None

        monkeypatch.setattr(bga.readers_bug_blitz, "mint_run_id", _abstain, raising=False)
        monkeypatch.setattr(
            bga.readers_mise_en_place,
            "_resolve_state_root",
            lambda: str(tmp_path / "state"),
        )
        exit_code = bga.main(["mint-run-id", "mise-en-place"])
        assert exit_code == bga.EXIT_OK
        assert bga.readers_bug_blitz.__name__ in calls

    def test_reader_without_mint_run_id_attribute_abstains_cleanly(self, tmp_path, monkeypatch):
        # `readers_bug_sweep` (etc.) define no `mint_run_id` at all today --
        # the dispatch must not crash on `getattr` for a reader lacking the
        # attribute.
        assert not hasattr(bga.readers_bug_sweep, "mint_run_id")
        monkeypatch.chdir(tmp_path)
        exit_code = bga.main(["mint-run-id", "bug-blitz"])
        assert exit_code == bga.EXIT_USAGE


# ---------------------------------------------------------------------------
# (k) `brief`'s unchanged contract -- AC6
# `docs/plans/2026-08-04-engine-minted-mise-run-identity.md`, chunk C2.
# ---------------------------------------------------------------------------


class TestBriefContractUnchangedAC6:
    """AC6: `brief`'s stdout contract is byte-unchanged by C1's
    `mint-run-id` addition, and `--mint-run-id` is NOT a second accepted
    carrier on `brief` -- one carrier (`--run-id`) per the standing
    no-second-carrier rule."""

    @pytest.mark.parametrize("cadence", ["bug-blitz", "mise-en-place"])
    def test_brief_still_returns_exactly_the_8_canonical_keys(self, cadence, capsys):
        exit_code = bga.main(["brief", cadence])
        assert exit_code == bga.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert set(out.keys()) == set(ENVELOPE_KEYS)

    def test_brief_mint_run_id_flag_is_not_a_second_carrier(self, capsys):
        # `--mint-run-id` is NOT a recognized token on `brief` -- minting is
        # the separate `mint-run-id` verb (the design ruling this plan
        # documents), never a flag spelling accepted here too.
        exit_code = bga.main(["brief", "mise-en-place", "--mint-run-id"])
        assert exit_code == bga.EXIT_USAGE

    def test_brief_mint_run_id_flag_with_value_is_also_a_usage_error(self, capsys):
        exit_code = bga.main(["brief", "mise-en-place", "--mint-run-id", "mise-en-place"])
        assert exit_code == bga.EXIT_USAGE


# ---------------------------------------------------------------------------
# (l) `coordinator/bin/backlog-grind-assemble` trampoline dispatch routing --
# regression net for the mint-run-id fallthrough bug
# (docs/plans/2026-08-04-engine-minted-mise-run-identity.md, AC8): the
# trampoline's `main()` carries an allowlist (`if subcommand not in (...)`)
# AND a SEPARATE dispatch chain ending in a bare
# `return apply_mod.main_drop(rest)` tail. Adding `mint-run-id` to the
# allowlist alone let it fall through to `main_drop` -- an exit-0 response
# with drop's own JSON payload, silently invoking the WRONG callee. Exercised
# here through the trampoline's own `main()` (never the coordinator_core
# layer a second time -- that is TestCliSmoke/TestBriefContractUnchangedAC6
# above), and never a subprocess round-trip.
# ---------------------------------------------------------------------------

_TRAMPOLINE_PATH = _REPO_ROOT / "coordinator" / "bin" / "backlog-grind-assemble"


def _load_trampoline():
    """Import `coordinator/bin/backlog-grind-assemble` as a module.

    SourceFileLoader dance because the filename is hyphenated and carries no
    importable extension -- same idiom as
    `test_archive_stamp.py::TestArchiveStampCliCorrectHandoffBodyDispatch.
    _load_cli_module` and `test_bin_launcher_parity.py::_load_gen_launcher_shim`.
    Not registered into `sys.modules`: each test loads its own fresh module
    object, so monkeypatches on `bga`/`bga_apply` (the same cached
    `coordinator_core.backlog_grind_assemble[.apply]` objects the
    trampoline's own `_import_modules()` re-imports from `sys.modules`) are
    the only cross-module state in play.
    """
    import importlib.util
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader(
        "backlog_grind_assemble_trampoline_under_test", str(_TRAMPOLINE_PATH)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# Review: cli-and-tests reviewer (Finding 1) -- single source of truth for
# which callee each allowlisted subcommand is expected to reach. Feeds BOTH
# the parametrize list below AND
# test_every_allowlisted_subcommand_has_an_expected_callee_mapped's
# AST-derived check against the trampoline's own allowlist tuple, so a
# subcommand added to `main()`'s allowlist without a row here fails loudly
# instead of never being collected as a test case at all.
_EXPECTED_CALLEE_BY_SUBCOMMAND = {
    "brief": "brief.main",
    "mint-run-id": "brief.main",
    "apply": "apply.main_apply",
    "drop": "apply.main_drop",
}


def _extract_allowlist_from_trampoline_main() -> tuple:
    """AST-derive the subcommand allowlist tuple from the trampoline's own
    `if subcommand not in (...)` guard in `main()` -- same structural-
    introspection idiom as
    `TestTheSeamNeverBranchesOnTheRunIdOrCadence`/`test_the_seam_never_
    branches_on_the_run_id_or_on_a_cadence_name` and the env-carrier guard
    around line 2301, rather than a fourth hand-copied list of subcommand
    names living in this test file."""
    source = _TRAMPOLINE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    main_fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    for node in ast.walk(main_fn):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.NotIn)
        ):
            continue
        allowlist_node = test.comparators[0]
        if not isinstance(allowlist_node, ast.Tuple):
            continue
        return tuple(
            elt.value
            for elt in allowlist_node.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        )
    raise AssertionError(
        "could not find an `if subcommand not in (...)` allowlist guard in "
        "the trampoline's main() -- update _extract_allowlist_from_trampoline_"
        "main to match the current dispatch shape before trusting this test"
    )


class TestTrampolineDispatchRouting:
    """AC8: the trampoline's allowlist and dispatch chain must agree on
    every subcommand -- the bug this class locks down is a subcommand that
    passes the allowlist but falls through the dispatch chain's bare
    `main_drop` tail instead of reaching its intended callee."""

    def test_every_allowlisted_subcommand_has_an_expected_callee_mapped(self):
        # The structural guard for the NEXT verb: reads the allowlist tuple
        # straight out of the trampoline's own source (never hand-copied)
        # and fails loudly, naming the unmapped verb, if it has no row in
        # _EXPECTED_CALLEE_BY_SUBCOMMAND -- the exact mint-run-id incident
        # this class exists to prevent, except this time the missing verb
        # never even gets collected as a parametrize case below, so THIS
        # test is what makes that failure loud instead of silent.
        allowlist = _extract_allowlist_from_trampoline_main()
        unmapped = [
            subcommand
            for subcommand in allowlist
            if subcommand not in _EXPECTED_CALLEE_BY_SUBCOMMAND
        ]
        assert not unmapped, (
            f"{unmapped} was added to the trampoline's allowlist tuple in "
            "main() but has no entry in _EXPECTED_CALLEE_BY_SUBCOMMAND above "
            "-- add BOTH a dispatch branch in coordinator/bin/backlog-grind-"
            "assemble's main() AND a row here ('<subcommand>': "
            "'<module>.<callee>') before this test will pass."
        )

    def test_mint_run_id_routes_to_brief_module_main_not_main_drop(self, monkeypatch):
        # CLAUDE_KLABAUTER_ROOT set explicitly: the trampoline's own `_import_modules()`
        # re-runs its full resolution ladder on every call and this test box
        # has no machine-local registry entry for it.
        monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", str(_REPO_ROOT))
        trampoline = _load_trampoline()
        brief_calls: list = []
        drop_calls: list = []

        def _fake_brief_main(argv):
            brief_calls.append(list(argv))
            return 0

        def _fake_main_drop(rest):
            drop_calls.append(list(rest))
            return 0

        monkeypatch.setattr(bga, "main", _fake_brief_main)
        monkeypatch.setattr(bga_apply, "main_drop", _fake_main_drop)

        exit_code = trampoline.main(["mint-run-id", "mise-en-place"])

        assert exit_code == 0
        assert brief_calls == [["mint-run-id", "mise-en-place"]]
        assert drop_calls == []

    @pytest.mark.parametrize(
        "subcommand, expected_callee",
        list(_EXPECTED_CALLEE_BY_SUBCOMMAND.items()),
    )
    def test_every_allowlisted_subcommand_reaches_its_intended_callee(
        self, monkeypatch, subcommand, expected_callee
    ):
        # The structural guard: this is what makes the NEXT added verb fail
        # loudly (wrong callee recorded) rather than silently landing in the
        # `main_drop` fallthrough the way `mint-run-id` did.
        monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", str(_REPO_ROOT))
        trampoline = _load_trampoline()
        seen: dict = {}

        def _make_spy(name):
            def _spy(argv):
                seen["callee"] = name
                return 0

            return _spy

        monkeypatch.setattr(bga, "main", _make_spy("brief.main"))
        monkeypatch.setattr(bga_apply, "main_apply", _make_spy("apply.main_apply"))
        monkeypatch.setattr(bga_apply, "main_drop", _make_spy("apply.main_drop"))

        exit_code = trampoline.main([subcommand, "mise-en-place"])

        assert exit_code == 0
        assert seen["callee"] == expected_callee

    def test_non_allowlisted_subcommand_is_still_a_usage_error(self, capsys):
        trampoline = _load_trampoline()
        exit_code = trampoline.main(["bogus-verb", "mise-en-place"])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "usage" in err


class TestCommitOneLockRetry:
    """`_commit_one` (D-3's own commit primitive) consumes the shared
    `coordinator_core.git_lock_retry` helper on both its `add` and its
    `commit` invocation, per the handoff at
    `state/handoffs/2026-08-07-git-index-lock-retry-at-the-ceremony-commit-seam.md`."""

    def test_held_lock_then_success_completes_without_raising(self, tmp_path, monkeypatch):
        calls: list[tuple[str, ...]] = []

        def fake_git(args: list[str], cwd: Path):
            calls.append(tuple(args))
            if args[0] == "add":
                # First `git add` sees the lock held by a sibling session;
                # the second succeeds.
                if sum(1 for c in calls if c[0] == "add") == 1:
                    return SimpleNamespace(
                        returncode=128, stdout="",
                        stderr="fatal: Unable to create '.git/index.lock': File exists.",
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if args[:2] == ["diff", "--cached"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            if args[0] == "commit":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if args == ["rev-parse", "HEAD"]:
                return SimpleNamespace(returncode=0, stdout="deadbeef" * 5 + "\n", stderr="")
            raise AssertionError(f"unexpected git invocation: {args!r}")

        monkeypatch.setattr(bga_apply, "_run_git", fake_git)
        monkeypatch.setattr("coordinator_core.git_lock_retry.time.sleep", lambda *_a, **_k: None)

        sha = bga_apply._commit_one(tmp_path, ["state/h1.md"], "apply: d1")

        assert sha == "deadbeef" * 5
        assert sum(1 for c in calls if c[0] == "add") == 2

    def test_non_lock_add_failure_raises_on_first_attempt(self, tmp_path, monkeypatch):
        calls: list[tuple[str, ...]] = []

        def fake_git(args: list[str], cwd: Path):
            calls.append(tuple(args))
            if args[0] == "add":
                return SimpleNamespace(returncode=1, stdout="", stderr="disk full")
            raise AssertionError(f"unexpected call after add failure: {args!r}")

        monkeypatch.setattr(bga_apply, "_run_git", fake_git)

        with pytest.raises(RuntimeError, match="git add"):
            bga_apply._commit_one(tmp_path, ["state/h1.md"], "apply: d1")

        assert sum(1 for c in calls if c[0] == "add") == 1

    def test_exhausted_lock_contention_still_raises(self, tmp_path, monkeypatch):
        calls: list[tuple[str, ...]] = []

        def fake_git(args: list[str], cwd: Path):
            calls.append(tuple(args))
            if args[0] == "add":
                return SimpleNamespace(
                    returncode=128, stdout="",
                    stderr="fatal: Unable to create '.git/index.lock': File exists.",
                )
            raise AssertionError(f"unexpected call: {args!r}")

        monkeypatch.setattr(bga_apply, "_run_git", fake_git)
        monkeypatch.setattr("coordinator_core.git_lock_retry.time.sleep", lambda *_a, **_k: None)

        with pytest.raises(RuntimeError, match="git add"):
            bga_apply._commit_one(tmp_path, ["state/h1.md"], "apply: d1")

        from coordinator_core.git_lock_retry import DEFAULT_MAX_ATTEMPTS

        assert sum(1 for c in calls if c[0] == "add") == DEFAULT_MAX_ATTEMPTS
