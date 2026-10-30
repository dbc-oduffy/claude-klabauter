"""
coordinator_core.sizing_assemble.test_interaction_mode_cli — C6.

Covers `main()`'s `--exit-criterion`/`--interaction-mode` flags, the flag ->
fleet -> default resolution order and its `interaction_mode_source`
labelling, the CLI's exit code on an unknown mode, and the falsifier's own
`expected_when_true` shape (frontmatter § prime_exit_criterion.falsifier).

Spec: docs/plans/2026-09-27-sizing-carries-exit-criterion-and-interaction-mode.md § C6.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

import coordinator_core.sizing_assemble as sa
from coordinator_core.session import fleet_mode as fleet_mode_module
from coordinator_core.session import mode_resolution as mode_resolution_module


def _run(monkeypatch, capsys, argv):
    rc = sa.main(argv)
    captured = capsys.readouterr()
    return rc, captured


_BIN = Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "sizing-assemble.py"


def _cli_entry_module():
    spec = importlib.util.spec_from_file_location("sizing_assemble_cli_entry", _BIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fleet_stub(monkeypatch, value):
    """Stubs the fleet record and installs the resolver the CLI entry
    (`coordinator/bin/sizing-assemble.py`) installs, since the engine itself
    never reads the record. `resolve_mode` calls its own bound
    `read_fleet_mode`, so both names are patched."""
    fn = lambda: value if value is not None else {}
    monkeypatch.setattr(fleet_mode_module, "read_fleet_mode", fn)
    monkeypatch.setattr(mode_resolution_module, "read_fleet_mode", fn)
    monkeypatch.setattr(sa, "_interaction_mode_resolver", _cli_entry_module()._fleet_interaction_mode)


class TestFlagWins:
    def test_flag_present_reports_source_flag(self, monkeypatch, capsys):
        _fleet_stub(monkeypatch, {"interaction_mode": "ceo"})
        rc = sa.main(["--tshirt", "M", "--interaction-mode", "pm"])
        assert rc == sa.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["interaction_mode"] == "pm"
        assert out["interaction_mode_source"] == "flag"


class TestFleetWinsWhenNoFlag:
    def test_no_flag_valid_fleet_reports_source_fleet(self, monkeypatch, capsys):
        _fleet_stub(monkeypatch, {"interaction_mode": "ceo"})
        rc = sa.main(["--tshirt", "M"])
        assert rc == sa.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["interaction_mode"] == "ceo"
        assert out["interaction_mode_source"] == "fleet"


def test_engine_default_mirrors_registry_default():
    """`sizing_assemble` mirrors the registry default because it cannot import it."""
    assert sa._INTERACTION_MODE_DEFAULT == mode_resolution_module.MODE_KEYS["interaction_mode"].default


class TestDefaultWhenNeither:
    def test_no_flag_no_record_reports_source_default(self, monkeypatch, capsys):
        _fleet_stub(monkeypatch, {})
        rc = sa.main(["--tshirt", "M"])
        assert rc == sa.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["interaction_mode"] == "ceo"
        assert out["interaction_mode_source"] == "default"

    def test_malformed_fleet_value_degrades_to_default(self, monkeypatch, capsys):
        _fleet_stub(monkeypatch, {"interaction_mode": "boss"})
        rc = sa.main(["--tshirt", "M"])
        assert rc == sa.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["interaction_mode"] == "ceo"
        assert out["interaction_mode_source"] == "default"


class TestUnknownModeExitsTwo:
    def test_cli_exits_two_on_unknown_mode(self, monkeypatch, capsys):
        _fleet_stub(monkeypatch, {})
        rc = sa.main(["--tshirt", "M", "--interaction-mode", "boss"])
        assert rc == sa.EXIT_USAGE

    def test_argv_parse_error_still_usage(self, monkeypatch, capsys):
        rc = sa.main(["--tshirt", "M", "--interaction-mode"])
        assert rc == sa.EXIT_USAGE


class TestExitCriterionFlag:
    def test_exit_criterion_flag_round_trips(self, monkeypatch, capsys):
        _fleet_stub(monkeypatch, {})
        rc = sa.main(["--tshirt", "M", "--exit-criterion", "Ship the thing"])
        assert rc == sa.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["exit_criterion"] == {"statement": "Ship the thing", "accepted": None}

    @pytest.mark.parametrize("text", ["fast tier green", "full suite", "tier-U"])
    def test_suite_tier_exit_criterion_is_refused(self, monkeypatch, capsys, text):
        _fleet_stub(monkeypatch, {})
        rc = sa.main(["--tshirt", "M", "--exit-criterion", text])
        assert rc == sa.EXIT_USAGE
        assert "tests covering touched files pass" in capsys.readouterr().err

    def test_replacement_exit_criterion_is_accepted(self, monkeypatch, capsys):
        _fleet_stub(monkeypatch, {})
        rc = sa.main(["--tshirt", "M", "--exit-criterion", "tests covering touched files pass"])
        assert rc == sa.EXIT_OK

    def test_absent_exit_criterion_is_null(self, monkeypatch, capsys):
        _fleet_stub(monkeypatch, {})
        rc = sa.main(["--tshirt", "M"])
        assert rc == sa.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["exit_criterion"] is None


class TestFalsifierShape:
    """The plan's own frontmatter falsifier (§ prime_exit_criterion), re-run
    in-tree: `--tshirt M --exit-criterion x --interaction-mode pm`."""

    def test_falsifier_expected_when_true(self, monkeypatch, capsys):
        _fleet_stub(monkeypatch, {})
        rc = sa.main(
            ["--tshirt", "M", "--exit-criterion", "x", "--interaction-mode", "pm"]
        )
        assert rc == sa.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["exit_criterion"] == {"statement": "x", "accepted": None}
        assert out["interaction_mode"] == "pm"
        assert out["interaction_mode_source"] == "flag"
        assert out["route"] == "plan"
        # plan at M: the engine discharges the sizing-stage acceptance (PM ruling 2026-10-08)
        assert "exit_criterion_pending" not in out["detents"]
        assert [t["id"] for t in out["touchpoints"]] == ["accept_result"]

    def test_baseline_regression_unrecognized_argument(self, monkeypatch, capsys):
        """Confirms the pre-C6 baseline usage-error the falsifier's
        `baseline_output` describes is no longer true: `--exit-criterion`
        is now a recognized argument."""
        _fleet_stub(monkeypatch, {})
        rc = sa.main(["--tshirt", "M", "--exit-criterion", "x", "--interaction-mode", "pm"])
        assert rc != sa.EXIT_USAGE


class TestZeroNewSubprocessSpawns:
    def test_route_and_main_spawn_nothing(self, monkeypatch, capsys):
        # route()/main() never shell out (module docstring's READ-ONLY
        # clause) -- assert no subprocess.Popen/run call happens for a
        # representative flag/fleet/default trio.
        import subprocess

        calls = []
        real_run = subprocess.run
        real_popen = subprocess.Popen

        def _tripwire_run(*a, **kw):
            calls.append(("run", a, kw))
            return real_run(*a, **kw)

        def _tripwire_popen(*a, **kw):
            calls.append(("Popen", a, kw))
            return real_popen(*a, **kw)

        monkeypatch.setattr(subprocess, "run", _tripwire_run)
        monkeypatch.setattr(subprocess, "Popen", _tripwire_popen)
        _fleet_stub(monkeypatch, {})
        sa.main(["--tshirt", "M", "--interaction-mode", "pm"])
        sa.main(["--tshirt", "M"])
        assert calls == []
