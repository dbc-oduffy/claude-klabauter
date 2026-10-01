"""Leg parity, source gating and leg isolation for `hooks.sessionstart_dispatch`."""

from __future__ import annotations

import asyncio

import pytest

from coordinator_core.hooks import sessionstart_dispatch as sd

# coordinator-content-repo@054f62ba51 sessionstart-dispatch.py REGISTRY, in order; kill_switch_detail has no
# DoE row and takes its sibling guard_settings_integrity's set.
_SSC = frozenset({"startup", "clear", "compact"})
_ALL = frozenset({"startup", "resume", "clear", "compact", "fork"})
_PINNED = [
    ("project_orientation", _SSC),
    ("guard_settings_integrity", _SSC),
    ("guard_hooks_kill_switch_detail", _SSC),
    ("guard_foreign_platform_paths", _SSC),
    ("session_start_write_bump_anchor", _ALL),
    ("bin_drift_refresh", frozenset({"startup"})),
    ("job_mode_announce", frozenset({"startup"})),
    ("governed_surface_drift", _ALL),
    ("guard_hook_generation_self_probe", _SSC),
]
_EXCLUDED = {"day_branch_assert"}


def _run(payload):
    return asyncio.run(sd._handler({"payload": payload}))


def _ctx(result):
    return result.get("hookSpecificOutput", {}).get("additionalContext", "")


@pytest.fixture
def calls(monkeypatch):
    seen = []
    for key, _srcs, fn_name in sd._LEGS:
        monkeypatch.setattr(
            sd, fn_name, lambda payload, key=key: seen.append(key) or f"text:{key}"
        )
    return seen


def test_legs_match_pinned_list_and_order():
    assert [(k, s) for k, s, _ in sd._LEGS] == _PINNED
    assert not _EXCLUDED & {k for k, _, _ in sd._LEGS}
    for _k, _s, fn_name in sd._LEGS:
        assert callable(getattr(sd, fn_name))


@pytest.mark.parametrize("source", sorted(_ALL))
def test_each_leg_runs_exactly_on_its_sources(calls, source):
    _run({"source": source})
    assert calls == [k for k, srcs in _PINNED if source in srcs]


def test_startup_only_legs_run_on_startup_not_resume(calls):
    _run({"source": "startup"})
    assert {"bin_drift_refresh", "job_mode_announce"} <= set(calls)
    calls.clear()
    _run({"source": "resume"})
    assert not {"bin_drift_refresh", "job_mode_announce"} & set(calls)


def test_missing_source_runs_nothing(calls):
    result = _run({})
    assert calls == []
    assert not _ctx(result)


def test_unmatched_source_returns_breadcrumb(calls):
    result = _run({"source": "bogus"})
    assert calls == []
    ctx = _ctx(result)
    assert "source='bogus' matches no guard in REGISTRY" in ctx
    assert "every guard skipped for this boot" in ctx


def test_raising_leg_does_not_drop_others(monkeypatch, calls):
    def boom(payload):
        raise RuntimeError("x")

    monkeypatch.setattr(sd, "_leg_guard_settings_integrity", boom)
    ctx = _ctx(_run({"source": "startup"}))
    assert "text:project_orientation" in ctx
    assert "text:guard_hook_generation_self_probe" in ctx


def test_write_bump_anchor_skips_without_session_id(monkeypatch):
    seen = []
    monkeypatch.setattr(sd, "_write_session_start_record", lambda *a, **k: seen.append((a, k)))
    sd._leg_write_bump_anchor({"cwd": "/x"})
    assert seen == []
    sd._leg_write_bump_anchor({"session_id": "s", "cwd": "/x"})
    assert seen == [(("s",), {"launch_cwd": "/x"})]


def test_governed_surface_drift_formats_header_and_lines(monkeypatch, tmp_path):
    monkeypatch.setattr(sd, "_uncommitted_surface_refusals", lambda root: ["a", "b"])
    text = sd._leg_governed_surface_drift({"cwd": str(tmp_path)})
    assert text.startswith("UNADMITTED BOOT PAYLOAD:")
    assert text.endswith("\n  - a\n  - b")
    monkeypatch.setattr(sd, "_uncommitted_surface_refusals", lambda root: [])
    assert sd._leg_governed_surface_drift({"cwd": str(tmp_path)}) is None


def test_foreign_paths_uses_config_dir_param(monkeypatch, tmp_path):
    got = {}

    def fake(settings_path, config_dir=None):
        got.update(settings_path=settings_path, config_dir=config_dir)
        return "banner"

    monkeypatch.setattr(sd, "_evaluate_foreign_platform_paths", fake)
    assert sd._leg_foreign_platform_paths({"config_dir": str(tmp_path)}) == "banner"
    assert got["settings_path"] == tmp_path / "settings.json"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "env"))
    sd._leg_foreign_platform_paths({})
    assert got["config_dir"] == tmp_path / "env"
