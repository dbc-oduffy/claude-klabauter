"""Shrink-only ratchet: replayed PreToolUse advisory emission may only go down.

The pin (fixtures/pretooluse_advisory_baseline.json) is lowered by editing it; the
C0_CEILING literal is the pre-shrink measurement and never changes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core import advisory_census

FIXTURES = Path(__file__).parent / "fixtures"
CORPUS = FIXTURES / "pretooluse_advisory_corpus.jsonl"
PIN_PATH = FIXTURES / "pretooluse_advisory_baseline.json"

C0_CEILING = {
    "advisory_sites": 27,
    "fires": 55,
    "total_bytes": 14445,
    "median_bytes": 189,
}
C0_SITE_BYTES = {
    "agent:block-unenumerated-agent-type": 190,
    "agent:nudge-em-code-dispatch": 1418,
    "bash:block-approval-sentinel-creation": 168,
    "bash:block-drive-root-new-dir-bash-write": 145,
    "bash:block-topic-branch": 564,
    "bash:block-venv-creation": 307,
    "bash:cat-heredoc-write-advise": 645,
    "bash:find-exec-rewrite": 299,
    "bash:git-commit-safe-commit-advise": 349,
    "bash:git-no-optional-locks": 129,
    "bash:grep-via-bash-rewrite": 185,
    "bash:inprocess-search": 485,
    "bash:plumbing-and-loops": 817,
    "bash:sed-range-read-advise": 146,
    "hooks.block_ungranted_opus_subagent:check": 342,
    "hooks.nudge_workflow_authoring_trampoline:_handler": 201,
    "hooks.preuse_search_dispatch:_handler": 535,
    "hooks.preuse_sendmessage_dispatch:_handler": 76,
    "write:block_drive_root_new_dir_write": 143,
    "write:block_hand_authored_handoff_creation": 123,
    "write:block_sizing_object_schema_violation": 820,
    "write:guard_doctrine_surface_edits": 429,
    "write:nudge_new_sh_file_naked_python": 260,
    "write:nudge_shell_shaped_spawn": 283,
    "write:nudge_windows_subprocess_popup": 820,
    "write:validate_frontmatter_schema_advisory": 4110,
    "write:validate_frontmatter_schema_deny": 456,
}
# Sites whose advisory now surfaces because an upstream guard's retired leg no
# longer pre-empts it; capped explicitly, the totals ceiling still binds.
FALLTHROUGH_CAPS = {
    "hooks.block_ungranted_opus_subagent:check": 350,
    "hooks.enforce_agent_dispatch_mode:_handler": 154,
}


@pytest.fixture(scope="module")
def pin():
    return json.loads(PIN_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def measured():
    return advisory_census.replay(CORPUS)


def test_pin_is_at_or_below_the_c0_ceiling(pin):
    for key, ceiling in C0_CEILING.items():
        assert pin[key] <= ceiling, key
    for site, entry in pin["sites"].items():
        cap = C0_SITE_BYTES.get(site, FALLTHROUGH_CAPS.get(site))
        assert cap is not None, f"{site}: not a C0 site and not a capped fall-through"
        assert entry["bytes"] <= max(cap, FALLTHROUGH_CAPS.get(site, 0)), site


def test_pin_shrank_strictly_below_c0(pin):
    for key in ("advisory_sites", "fires", "total_bytes"):
        assert pin[key] < C0_CEILING[key], key


def _violations(pin, measured):
    out = []
    for key in ("advisory_sites", "fires", "total_bytes", "median_bytes"):
        if measured[key] > pin[key]:
            out.append(key)
    for site, entry in measured["sites"].items():
        if site not in pin["sites"]:
            out.append(f"new:{site}")
            continue
        for field in ("bytes", "fires"):
            if entry[field] > pin["sites"][site][field]:
                out.append(f"{site}:{field}")
    return out


def test_replay_does_not_exceed_the_pin(pin, measured):
    assert _violations(pin, measured) == []


def test_pin_is_not_slack_against_the_replay(pin, measured):
    for key in ("advisory_sites", "fires", "total_bytes"):
        assert measured[key] >= pin[key], f"{key}: re-pin the baseline down to the measurement"


def test_a_grown_or_new_site_is_refused(pin, measured):
    grown = json.loads(json.dumps(measured))
    site = next(iter(grown["sites"]))
    grown["sites"][site]["bytes"] += 1
    grown["total_bytes"] += 1
    grown["sites"]["write:brand-new-site"] = {"bytes": 1, "fires": 1}
    grown["advisory_sites"] += 1
    found = _violations(pin, grown)
    assert f"{site}:bytes" in found
    assert "new:write:brand-new-site" in found
    assert "advisory_sites" in found
    assert "total_bytes" in found
