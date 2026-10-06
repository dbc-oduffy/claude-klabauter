"""
coordinator_core.sizing_assemble.test_interaction_mode_cli — C6.

Covers `main()`'s `--exit-criterion`/`--interaction-mode` flags, the flag ->
fleet -> default resolution order and its `interaction_mode_source`
labelling, the CLI's exit code on an unknown mode, and the falsifier's own
`expected_when_true` shape (frontmatter § prime_exit_criterion.falsifier).

Spec: docs/plans/2026-09-27-sizing-carries-exit-criterion-and-interaction-mode.md § C6.
"""
from __future__ import annotations

import json

import pytest

import coordinator_core.sizing_assemble as sa
from coordinator_core.session import fleet_mode as fleet_mode_module
from coordinator_core.session import mode_resolution as mode_resolution_module


def _run(monkeypatch, capsys, argv):
    rc = sa.main(argv)
    captured = capsys.readouterr()
    return rc, captured


def _fleet_stub(monkeypatch, value):
    """Stubs `read_fleet_mode` everywhere this module's lazy imports reach
    it: `coordinator_core.session.fleet_mode` (the origin, and the name
    `_resolve_interaction_mode_and_source`'s own `from ... import
    read_fleet_mode` re-binds), AND `coordinator_core.session.
    mode_resolution`'s OWN already-bound module-level name -- `resolve_mode`
    calls that local name directly, never re-looking it up on the origin
    module, so patching only the origin would silently miss it."""
    fn = lambda: value if value is not None else {}
    monkeypatch.setattr(fleet_mode_module, "read_fleet_mode", fn)
    monkeypatch.setattr(mode_resolution_module, "read_fleet_mode", fn)


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
        assert "exit_criterion_pending" in out["detents"]
        assert [t["id"] for t in out["touchpoints"]] == ["accept_sizing", "accept_result"]

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
