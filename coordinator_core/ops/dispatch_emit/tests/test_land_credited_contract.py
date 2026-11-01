"""Pins for the dispatch.land_credited caller-facing contract."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_credited_contract as c

_OK = {"run_id": "r1", "plan_path": "docs/plans/p.md", "script_path": "s.py"}


def test_requirement_ids_are_a_to_h_each_named():
    assert c.REQUIREMENT_IDS == tuple("abcdefgh")
    assert set(c.REQUIREMENT_NAMES) == set(c.REQUIREMENT_IDS)
    assert set(c.MINIMUM_LAND_IDS) <= set(c.REQUIREMENT_IDS)


def test_parse_request_accepts_complete_params():
    assert c.parse_request(_OK) == c.LandCreditedRequest("r1", "docs/plans/p.md", "s.py")


@pytest.mark.parametrize("missing", ["run_id", "plan_path", "script_path"])
def test_parse_request_refuses_missing_or_empty_field(missing):
    assert missing in c.parse_request({k: v for k, v in _OK.items() if k != missing})
    assert missing in c.parse_request({**_OK, missing: ""})
    assert missing in c.parse_request({**_OK, missing: 3})


@pytest.mark.parametrize("params", [None, [], "x"])
def test_parse_request_refuses_non_object(params):
    assert c.parse_request(params) == "params must be an object"


def test_replies_use_declared_keys():
    landed = c.landed_reply("abc", ["b", "a"], {"p": "peer-held"})
    refused = c.refused_reply(c.REFUSED_COMMIT, "boom")
    assert landed == {"committed": True, "sha": "abc", "landed": ["a", "b"], "withheld": {"p": "peer-held"}}
    assert set(landed) | set(refused) <= c.REPLY_KEYS
    assert refused["committed"] is False and refused["landed"] == [] and refused["withheld"] == {}


def test_holds_hook_signature_is_root_request_to_dict():
    hook: c.HoldsHook = lambda root, request: {}
    assert hook(Path("."), c.LandCreditedRequest("r", "p", "s")) == {}
    assert len(inspect.signature(hook).parameters) == 2


def test_contract_imports_only_stdlib():
    tree = ast.parse(Path(c.__file__).read_text(encoding="utf-8"))
    mods = {
        (n.module or "").split(".")[0] if isinstance(n, ast.ImportFrom) else a.name.split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in (n.names if isinstance(n, ast.Import) else [None])
    }
    assert "coordinator_core" not in mods
