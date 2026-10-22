"""Pure tests for the app_session.validate contract module."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from coordinator_core.ops import _app_session_validate_contract as c


def _target(**over):
    t = {
        "build": "pnpm build",
        "start": "pnpm start",
        "port": "3000",
        "build_isolated": "true",
        "env": {"A": "1", "SECRET": "s3cr3t"},
        "request_headers": {"x-forwarded-proto": "https"},
        "routes": {
            "home": "/",
            "missing": {"path": "/nope", "expect_status": "404"},
        },
        "variants": {"flag": {"env": {"A": "2", "B": "true"}}},
    }
    t.update(over)
    return t


def test_string_leaf_coercion():
    cfg = c.parse_validate_config(_target(ready_timeout_s="5"))
    assert isinstance(cfg, c.ValidateConfig)
    assert cfg.port == 3000 and cfg.build_isolated is True
    assert cfg.ready_timeout_s == 5
    assert cfg.build == ["pnpm", "build"] and cfg.start == ["pnpm", "start"]
    assert cfg.ready_path == "/" and cfg.build_timeout_s == 900


def test_variant_env_overlay():
    cfg = c.parse_validate_config(_target(), "flag")
    assert cfg.variant == "flag"
    assert cfg.env == {"A": "2", "SECRET": "s3cr3t", "B": "true"}
    base = c.parse_validate_config(_target())
    assert base.env["A"] == "1"


@pytest.mark.parametrize(
    "target,variant,field",
    [
        (_target(port=None), None, "port"),
        ({k: v for k, v in _target().items() if k != "port"}, None, "port"),
        (_target(port="abc"), None, "port"),
        (_target(routes={}), None, "routes"),
        (_target(routes=None), None, "routes"),
        (_target(routes={"p": "people"}), None, "routes.p"),
        (_target(routes={"p": {"path": "people"}}), None, "routes.p"),
        (_target(), "nope", "variant"),
        (_target(build_isolated="maybe"), None, "build_isolated"),
    ],
)
def test_config_errors(target, variant, field):
    err = c.parse_validate_config(target, variant)
    assert isinstance(err, c.ConfigError)
    assert err.field == field and err.reason


def test_routes_scalar_and_mapping():
    cfg = c.parse_validate_config(_target())
    assert cfg.routes == (
        c.Route("home", "/", None),
        c.Route("missing", "/nope", 404),
    )


def _result(**over):
    base = dict(
        route="home",
        path="/",
        width=1440,
        status=200,
        console_errors=[],
        failed_responses=[],
        broken_images=[],
        overflow_px=0,
        overflow_culprit=None,
        screenshot="home-1440.png",
        error=None,
    )
    base.update(over)
    return c.RouteResult(**base)


_HOME = c.Route("home", "/", None)
_MISSING = c.Route("missing", "/nope", 404)


@pytest.mark.parametrize(
    "result,route,expected",
    [
        (_result(), _HOME, False),
        (_result(status=404), _MISSING, False),
        (_result(status=404), _HOME, True),
        (_result(status=200), _MISSING, True),
        (_result(status=302), _HOME, False),
        (_result(console_errors=["boom"]), _HOME, True),
        (_result(failed_responses=[{"url": "u", "status": 500}]), _HOME, True),
        (_result(broken_images=["x.png"]), _HOME, True),
        (_result(overflow_px=210), _HOME, True),
        (_result(status=None, error="net::ERR"), _HOME, True),
        (_result(status=404, console_errors=["x"]), _MISSING, True),
    ],
)
def test_is_defect_table(result, route, expected):
    assert c.is_defect(result, route) is expected


def test_report_dict_carries_env_keys_not_values():
    rep = c.report_dict(
        key="web",
        variant=None,
        started_at="t0",
        finished_at="t1",
        base_url="http://127.0.0.1:3000",
        build_ran=True,
        build_exit_code=0,
        build_log="build.log",
        env_keys=["SECRET", "A"],
        results=[_result()],
        defect_count=0,
        screenshots_dir="shots",
    )
    assert rep["schema"] == "app-session-validate-report" and rep["version"] == 1
    assert rep["env_keys"] == ["A", "SECRET"]
    assert rep["results"][0]["route"] == "home"
    assert "s3cr3t" not in json.dumps(rep)


def _spec(cfg):
    return c.RunSpec(
        key="web",
        variant=None,
        repo_root="/r",
        worktree="/r",
        config=c.config_spec_dict(cfg),
        lock_path="/r/l.lock",
        handle_key="web@validate",
        created_at="t",
    )


def test_run_spec_round_trip_and_no_env_values(tmp_path):
    cfg = c.parse_validate_config(_target())
    spec = _spec(cfg)
    c.write_run_spec(tmp_path, spec)
    assert c.read_run_spec(tmp_path) == spec
    assert spec.config["env"] == ["A", "SECRET"]
    assert "s3cr3t" not in (tmp_path / c.SPEC_FILE).read_text()
    assert not list(tmp_path.glob("*.tmp"))


def test_write_run_spec_refuses_env_mapping(tmp_path):
    cfg = c.parse_validate_config(_target())
    bad = replace(_spec(cfg), config={**c.config_spec_dict(cfg), "env": dict(cfg.env)})
    with pytest.raises(ValueError):
        c.write_run_spec(tmp_path, bad)


def test_runner_argv(tmp_path):
    assert c.runner_argv(tmp_path) == [
        sys.executable, "-m", c.RUN_MODULE, "--run-dir", str(tmp_path)
    ]


def test_read_result_missing_partial_and_whole(tmp_path):
    assert c.read_result(tmp_path) is None
    (tmp_path / c.RESULT_FILE).write_text('{"ok": tr', encoding="utf-8")
    assert c.read_result(tmp_path) is None
    (tmp_path / c.RESULT_FILE).write_text('{"ok": true}', encoding="utf-8")
    assert c.read_result(tmp_path) == {"ok": True}


def test_wait_budget_arithmetic():
    cfg = c.parse_validate_config(_target(build_timeout_s=100, ready_timeout_s=20))
    assert c.wait_budget_s(cfg) == 100 + 20 + 60 * 2 * 2
