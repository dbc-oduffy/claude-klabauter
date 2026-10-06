"""Pins for the single test-verdict writer."""
from __future__ import annotations

import pytest

from coordinator_core.completion_receipts.test_verdict import (
    RECORD_VERB,
    TestVerdictRefused,
    bind_sidecar,
    derive_verdict,
    record_test_verdict,
)

SIDECAR = "---\nagent_type: coordinator:test-runner\nstatus: complete\ntarget_plan: null\n---\nbody\n"


def _res(**kw):
    base = {"status": "pass", "tests_run": 5, "tests_failed": 0, "sidecar_path": "sc.md"}
    base.update(kw)
    return base


def test_constants():
    assert RECORD_VERB == "test-verdict record"


@pytest.mark.parametrize("kw,want", [
    ({}, "pass"),
    ({"status": "fail", "tests_failed": 2}, "fail"),
    ({"status": "error", "tests_run": 0}, "errored"),
])
def test_derive_ok(kw, want):
    assert derive_verdict(_res(**kw)) == want


@pytest.mark.parametrize("kw", [
    {"tests_run": 0},
    {"status": "fail", "tests_failed": 0},
    {"status": "bogus"},
    {"tests_run": True},
    {"tests_run": "5"},
    {"tests_failed": -1},
    {"status": "pass", "tests_failed": 1},
])
def test_derive_refuses(kw):
    with pytest.raises(TestVerdictRefused):
        derive_verdict(_res(**kw))


def _write(tmp_path, text=SIDECAR, name="sc.md"):
    p = tmp_path / name
    p.write_bytes(text.encode())
    return p


def test_record_writes_once(tmp_path):
    p = _write(tmp_path)
    out = record_test_verdict(tmp_path, _res(), plan_path="docs/plans/x.md")
    assert out == p.resolve()
    t = p.read_text()
    assert "test_verdict: pass\n" in t and "run: 5\n" in t and "failed: 0\n" in t
    assert "target_plan: docs/plans/x.md" in t and "status: complete" in t
    assert t.endswith("---\nbody\n")
    before = p.read_bytes()
    with pytest.raises(TestVerdictRefused):
        record_test_verdict(tmp_path, _res())
    assert p.read_bytes() == before


def test_record_preserves_crlf(tmp_path):
    p = _write(tmp_path, SIDECAR.replace("\n", "\r\n"))
    record_test_verdict(tmp_path, _res())
    b = p.read_bytes()
    assert b"\r\n" in b and b.replace(b"\r\n", b"").count(b"\n") == 0


def test_record_never_overwrites_bound_keys(tmp_path):
    p = _write(tmp_path, SIDECAR.replace("target_plan: null", "target_plan: keep.md"))
    record_test_verdict(tmp_path, _res(), plan_path="other.md")
    assert "target_plan: keep.md" in p.read_text()


@pytest.mark.parametrize("text", [None, "no frontmatter\n", SIDECAR.replace("---\nbody", "test_verdict: pass\n---\nbody"),
                                  SIDECAR.replace("coordinator:test-runner", "coordinator:executor")])
def test_record_refusals_leave_bytes(tmp_path, text):
    p = _write(tmp_path, text) if text is not None else tmp_path / "sc.md"
    before = p.read_bytes() if p.exists() else None
    with pytest.raises(TestVerdictRefused):
        record_test_verdict(tmp_path, _res())
    assert (p.read_bytes() if p.exists() else None) == before


def test_record_allows_passed_agent_type_equal(tmp_path):
    p = _write(tmp_path, SIDECAR.replace("coordinator:test-runner", "host-native"))
    record_test_verdict(tmp_path, _res(), agent_type="host-native")
    assert "test_verdict: pass" in p.read_text()


def test_bind_sidecar(tmp_path):
    p = _write(tmp_path, SIDECAR.replace("---\nbody", "test_verdict: pass\n---\nbody"))
    assert bind_sidecar(tmp_path, "sc.md", plan_path="docs/plans/x.md") is True
    t = p.read_text()
    assert "target_plan: docs/plans/x.md" in t and "status: complete" in t
    assert bind_sidecar(tmp_path, "sc.md", plan_path="docs/plans/y.md") is False
    assert bind_sidecar(tmp_path, "missing.md", plan_path="z") is False
