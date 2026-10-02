from __future__ import annotations

import importlib.machinery
import importlib.util
import os
from pathlib import Path

import coordinator_core.sizing_assemble as sizing_assemble

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLI_PATH = _REPO_ROOT / "coordinator" / "bin" / "coordinator-doc-new.py"


def _load_doc_new_parser():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_sizing_assemble_scaffold_parity_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_sizing_assemble_scaffold_parity_test", loader
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


class TestSizingObjectScaffoldParity:
    def test_emits_coordinator_doc_new_cli(self) -> None:
        result = sizing_assemble.route(
            estimate={"tshirt": "M"}, intent="Ship the scaffold emitter"
        )
        directive = _directive(result["directives"], "d-scaffold-sizing-object")
        assert directive["cli"] == "coordinator-doc-new"

    def test_args_parse_under_the_real_parser(self) -> None:
        result = sizing_assemble.route(
            estimate={"tshirt": "M"}, intent="Ship the scaffold emitter"
        )
        directive = _directive(result["directives"], "d-scaffold-sizing-object")
        args = _parser.parse_args(directive["args"])
        assert args.doc_type == "sizing-object"

    def test_title_is_computed_from_intent(self) -> None:
        result = sizing_assemble.route(
            estimate={"tshirt": "M"}, intent="Ship the scaffold emitter"
        )
        directive = _directive(result["directives"], "d-scaffold-sizing-object")
        args = _parser.parse_args(directive["args"])
        assert args.title == "Ship the scaffold emitter"

    def test_long_intent_caps_title_and_slug(self) -> None:
        intent = (
            "I like it and we should size it, it seems like an S fan-out job "
            "to me, so go ahead and take the whole thing including the "
            "handling of the ask end to end"
        )
        result = sizing_assemble.route(estimate={"tshirt": "M"}, intent=intent)
        directive = _directive(result["directives"], "d-scaffold-sizing-object")
        args = _parser.parse_args(directive["args"])
        assert args.title == "I like it and we should size it,..."
        slug = Path(args.out).stem.split("-", 3)[3]
        assert len(slug) <= sizing_assemble._SLUG_MAX_CHARS
        assert "handling" not in args.out

    def test_name_overrides_intent_for_title_and_slug(self) -> None:
        result = sizing_assemble.route(
            estimate={"tshirt": "M"},
            intent="I like it and we should size it, seems like an S job",
            name="Engine friction fixes",
        )
        directive = _directive(result["directives"], "d-scaffold-sizing-object")
        args = _parser.parse_args(directive["args"])
        assert args.title == "Engine friction fixes"
        assert args.out.endswith("-engine-friction-fixes.yaml")

    def test_intent_absent_omits_title_and_still_parses(self) -> None:
        result = sizing_assemble.route(estimate={"tshirt": "M"})
        directive = _directive(result["directives"], "d-scaffold-sizing-object")
        assert not any(a.startswith("--title=") for a in directive["args"])
        args = _parser.parse_args(directive["args"])
        assert args.title is None

    def test_out_resolves_inside_repo_root(self) -> None:
        result = sizing_assemble.route(
            estimate={"tshirt": "M"}, intent="Ship the scaffold emitter"
        )
        directive = _directive(result["directives"], "d-scaffold-sizing-object")
        args = _parser.parse_args(directive["args"])
        resolved = (_REPO_ROOT / args.out).resolve()
        assert str(resolved).startswith(str(_REPO_ROOT.resolve()) + os.sep)

    def test_already_satisfied_key_is_boolean(self) -> None:
        result = sizing_assemble.route(
            estimate={"tshirt": "M"}, intent="Ship the scaffold emitter"
        )
        directive = _directive(result["directives"], "d-scaffold-sizing-object")
        assert isinstance(directive["already_satisfied"], bool)


class TestSizingAndPremiseThreading:
    """The printed directive carries the resolved tshirt/route and premise, or
    names what is missing instead of printing a command doc-new refuses."""

    def test_resolved_tshirt_and_route_are_threaded(self) -> None:
        result = sizing_assemble.route(
            estimate={"tshirt": "M"},
            intent="Ship the scaffold emitter",
            premise_provenance="read",
            premise_evidence="tests/x.py:3",
        )
        directive = _directive(result["directives"], "d-scaffold-sizing-object")
        args = _parser.parse_args(directive["args"])
        assert args.tshirt == "M"
        assert args.route == result["route"] == "plan"
        assert args.premise == "read"
        assert args.premise_evidence == "tests/x.py:3"
        assert "missing" not in directive

    def test_missing_names_absent_premise_flags(self) -> None:
        result = sizing_assemble.route(
            estimate={"tshirt": "M"}, intent="Ship the scaffold emitter"
        )
        directive = _directive(result["directives"], "d-scaffold-sizing-object")
        assert directive["missing"] == ["--premise-provenance", "--premise-evidence"]

    def test_unrecorded_provenance_is_missing(self) -> None:
        result = sizing_assemble.route(
            estimate={"tshirt": "M"},
            intent="Ship the scaffold emitter",
            premise_provenance="unrecorded",
            premise_evidence="x",
        )
        directive = _directive(result["directives"], "d-scaffold-sizing-object")
        assert directive["missing"] == ["--premise-provenance"]

    def test_directive_run_verbatim_exits_zero_and_scaffolds_sizing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import subprocess
        import sys

        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        result = sizing_assemble.route(
            estimate={"tshirt": "M"},
            intent="Ship the scaffold emitter",
            premise_provenance="read",
            premise_evidence="x",
        )
        directive = _directive(result["directives"], "d-scaffold-sizing-object")
        proc = subprocess.run(
            [sys.executable, str(_CLI_PATH), *directive["args"]],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert proc.returncode == 0, proc.stderr
        out = next(
            a.split("=", 1)[1] for a in directive["args"] if a.startswith("--out=")
        )
        text = (tmp_path / out).read_text(encoding="utf-8")
        assert "tshirt: M" in text
        assert "route: plan" in text


class TestExpressLaneScaffoldsNothing:
    def test_express_lane_emits_no_directive(self) -> None:
        result = sizing_assemble.route(
            estimate={"tshirt": "XS"}, express_lane=True
        )
        assert result["directives"] == []


class TestInteractionModeAndExitCriterionThreading:
    """C6: `_SIZING_OBJECT_FLAG_SPEC` gained `--exit-criterion`/
    `--interaction-mode`, threaded through the directive per Design § Engine
    ("The directive's argv carries --exit-criterion / --interaction-mode
    when given, and parses under the real doc-new parser")."""

    def test_exit_criterion_present_when_given(self) -> None:
        result = sizing_assemble.route(
            estimate={"tshirt": "M"},
            intent="Ship the scaffold emitter",
            exit_criterion="Ship the thing",
        )
        directive = _directive(result["directives"], "d-scaffold-sizing-object")
        args = _parser.parse_args(directive["args"])
        assert args.exit_criterion == "Ship the thing"

    def test_exit_criterion_absent_when_not_given(self) -> None:
        result = sizing_assemble.route(
            estimate={"tshirt": "M"}, intent="Ship the scaffold emitter"
        )
        directive = _directive(result["directives"], "d-scaffold-sizing-object")
        assert not any(a.startswith("--exit-criterion=") for a in directive["args"])
        args = _parser.parse_args(directive["args"])
        assert args.exit_criterion is None

    def test_interaction_mode_present_when_resolved_from_flag(self) -> None:
        result = sizing_assemble.route(
            estimate={"tshirt": "M"},
            intent="Ship the scaffold emitter",
            interaction_mode="pm",
            interaction_mode_source="flag",
        )
        directive = _directive(result["directives"], "d-scaffold-sizing-object")
        args = _parser.parse_args(directive["args"])
        assert args.interaction_mode == "pm"

    def test_interaction_mode_present_when_resolved_from_fleet(self) -> None:
        result = sizing_assemble.route(
            estimate={"tshirt": "M"},
            intent="Ship the scaffold emitter",
            interaction_mode="ceo",
            interaction_mode_source="fleet",
        )
        directive = _directive(result["directives"], "d-scaffold-sizing-object")
        args = _parser.parse_args(directive["args"])
        assert args.interaction_mode == "ceo"

    def test_interaction_mode_absent_when_merely_defaulted(self) -> None:
        """`route()`'s own "hands-on" default (no source, i.e. a direct
        library caller who never resolved a mode) must NOT leak onto the
        directive -- only an explicit flag/fleet resolution does. This is
        what keeps every pre-C6 caller's directive byte-identical."""
        result = sizing_assemble.route(
            estimate={"tshirt": "M"}, intent="Ship the scaffold emitter"
        )
        directive = _directive(result["directives"], "d-scaffold-sizing-object")
        assert not any(a.startswith("--interaction-mode=") for a in directive["args"])
        args = _parser.parse_args(directive["args"])
        assert args.interaction_mode is None

    def test_interaction_mode_absent_when_source_is_default(self) -> None:
        result = sizing_assemble.route(
            estimate={"tshirt": "M"},
            intent="Ship the scaffold emitter",
            interaction_mode="hands-on",
            interaction_mode_source="default",
        )
        directive = _directive(result["directives"], "d-scaffold-sizing-object")
        assert not any(a.startswith("--interaction-mode=") for a in directive["args"])

    def test_both_flags_present_and_parse_together(self) -> None:
        result = sizing_assemble.route(
            estimate={"tshirt": "M"},
            intent="Ship the scaffold emitter",
            exit_criterion="Ship the thing",
            interaction_mode="ceo",
            interaction_mode_source="flag",
        )
        directive = _directive(result["directives"], "d-scaffold-sizing-object")
        args = _parser.parse_args(directive["args"])
        assert args.exit_criterion == "Ship the thing"
        assert args.interaction_mode == "ceo"


class TestDirectiveSatisfiesScaffoldCli:
    def _args(self, **kw):
        result = sizing_assemble.route(
            estimate={"tshirt": "M"}, intent="Ship the scaffold emitter", **kw
        )
        directive = _directive(result["directives"], "d-scaffold-sizing-object")
        return result, _parser.parse_args(directive["args"])

    def test_directive_passes_the_cli_premise_validation(self) -> None:
        _, args = self._args(premise_provenance="read", premise_evidence="tests/x.py:3")
        mod_path = _CLI_PATH
        loader = importlib.machinery.SourceFileLoader("doc_new_flags_check", str(mod_path))
        spec = importlib.util.spec_from_loader("doc_new_flags_check", loader)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        loader.exec_module(mod)
        _detents, error = mod._validate_sizing_flags(args)
        assert error is None

    def test_premise_provenance_is_carried(self) -> None:
        _, args = self._args(premise_provenance="executed", premise_evidence="pytest x")
        assert args.premise == "executed"
        assert args.premise_evidence

    def test_computed_size_and_route_are_carried(self) -> None:
        result, args = self._args()
        assert args.tshirt == result["resolved_estimate"]["tshirt"] == "M"
        assert args.route == result["route"] == "plan"


class TestScaffoldedStatus:
    def test_computed_sizing_lands_sized(self) -> None:
        result = sizing_assemble.route(
            estimate={"tshirt": "M"}, intent="Ship the scaffold emitter"
        )
        directive = _directive(result["directives"], "d-scaffold-sizing-object")
        args = _parser.parse_args(directive["args"])
        mod = importlib.machinery.SourceFileLoader(
            "doc_new_status_probe", str(_CLI_PATH)
        ).load_module()
        text = mod._scaffold_sizing(
            args.title,
            tshirt=args.tshirt,
            route=args.route,
            premise=args.premise,
            premise_evidence=args.premise_evidence,
        )
        assert "\nstatus: sized " in text
        assert "\nstatus: draft" not in text

    def test_bare_scaffold_stays_draft(self) -> None:
        mod = importlib.machinery.SourceFileLoader(
            "doc_new_status_probe2", str(_CLI_PATH)
        ).load_module()
        assert "\nstatus: draft " in mod._scaffold_sizing("x")
