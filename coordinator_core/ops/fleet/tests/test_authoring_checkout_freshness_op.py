"""`fleet.authoring_checkout_freshness` reports each authoring checkout's lag and ff's a safe one."""

import os

import pytest

from coordinator_core.ops.fleet import _memo_resolver
from coordinator_core.ops.fleet import authoring_checkout_freshness as op
from coordinator_core.install import authoring_checkout_freshness_apply as apply

pytestmark = pytest.mark.complete

_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def test_unreadable_registry_is_an_advisory_not_a_raise(monkeypatch):
    def boom():
        raise _memo_resolver.RegistryReadError("bad toml")

    monkeypatch.setattr(_memo_resolver, "read_registry_repos", boom)
    out = op._handler({})
    assert out["rows"] == []
    assert "bad toml" in out["reason"] and out["advisories"]


def test_fetch_param_is_passed_and_validated(monkeypatch):
    seen = []
    monkeypatch.setattr(_memo_resolver, "read_registry_repos", lambda: {})
    monkeypatch.setattr(apply, "apply_all", lambda reg, do_fetch=False: seen.append(do_fetch) or {"rows": [], "advisories": []})
    op._handler({})
    op._handler({"fetch": True})
    assert seen == [False, True]
    with pytest.raises(ValueError):
        op._handler({"fetch": "yes"})
