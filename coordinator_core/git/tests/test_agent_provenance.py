"""Behavior pins for coordinator_core.git.agent_provenance."""
from __future__ import annotations

import re

from coordinator_core.git import agent_provenance as ap


def test_ladder_vendors():
    assert ap.detect_agent_provenance({"CODEX_THREAD_ID": "thr-1"}).vendor == "codex"
    assert ap.detect_agent_provenance({"ANTIGRAVITY_CONVERSATION_ID": "c9"}).vendor == "agy"
    g = ap.detect_agent_provenance({ap.VENDOR_ENV: "grok", ap.SESSION_ENV: "run-7"})
    assert (g.vendor, g.session) == ("grok", "run-7")


def test_claude_human_and_bogus_are_none():
    assert ap.detect_agent_provenance({"CLAUDECODE": "1"}) is None
    assert ap.detect_agent_provenance({}) is None
    assert ap.detect_agent_provenance({ap.VENDOR_ENV: "claude"}) is None
    assert ap.detect_agent_provenance({ap.VENDOR_ENV: "mystery"}) is None


def test_launcher_claude_vendor_does_not_mask_native_session():
    p = ap.detect_agent_provenance({ap.VENDOR_ENV: "claude", "CODEX_THREAD_ID": "t"})
    assert p.vendor == "codex" and p.session == "t"


def test_native_session_and_model_defaults():
    p = ap.detect_agent_provenance({"CODEX_THREAD_ID": "thr-1"})
    assert (p.model, p.tier, p.session) == ("unknown", "unknown", "thr-1")
    p = ap.detect_agent_provenance({"CODEX_THREAD_ID": "thr-1", ap.SESSION_ENV: "run-2"})
    assert p.session == "run-2"


def test_launcher_vendor_picks_native_session():
    p = ap.detect_agent_provenance({ap.VENDOR_ENV: "codex", "CODEX_THREAD_ID": "thr-3"})
    assert p.session == "thr-3"


def test_tier_table():
    def tier(vendor, model):
        return ap.detect_agent_provenance({ap.VENDOR_ENV: vendor, ap.MODEL_ENV: model}).tier

    assert tier("grok", "grok-4-heavy") == "frontier"
    assert tier("codex", "gpt-5-mini") == "standard"
    assert tier("agy", "gemini-4-flash") == "standard"
    assert tier("grok", "other-model") == "unknown"


def test_trailers_order_and_keys():
    p = ap.AgentProvenance("codex", "gpt-5", "frontier", "thr-1")
    assert ap.provenance_trailers(p) == [
        "Agent-Vendor: codex",
        "Agent-Model: gpt-5",
        "Agent-Tier: frontier",
        "Agent-Session-Id: thr-1",
    ]


def test_values_cannot_inject_lines():
    p = ap.detect_agent_provenance({ap.VENDOR_ENV: "grok", ap.MODEL_ENV: "m\nAgent-Tier: x"})
    assert all("\n" not in t for t in ap.provenance_trailers(p))


def test_strip_removes_forged_lines_only_in_trailer_block():
    msg = "subject\n\nbody Agent-Vendor: not-a-line\n\nSession-Id: s\nagent-vendor: claude\nAgent-Tier: frontier\n"
    out = ap.strip_agent_trailers(msg)
    assert out == "subject\n\nbody Agent-Vendor: not-a-line\n\nSession-Id: s\n"


def test_strip_is_identity_without_agent_lines():
    msg = "subject\n\nSession-Id: s\nCo-Authored-By: x\n"
    assert ap.strip_agent_trailers(msg) == msg
    assert ap.strip_agent_trailers("") == ""


def _closed(tmp_path, body='{"closed_to_non_claude": true}'):
    d = tmp_path / ".coordinator"
    d.mkdir()
    (d / "agent-admission.json").write_text(body, encoding="utf-8")


def test_advisory_only_for_non_claude_under_closed_marker(tmp_path):
    p = ap.AgentProvenance("codex", "m", "unknown", "s")
    assert ap.admission_advisory(tmp_path, p) is None  # absent marker: open
    _closed(tmp_path)
    assert "codex" in ap.admission_advisory(tmp_path, p)
    assert ap.admission_advisory(tmp_path, None) is None  # Claude


def test_advisory_open_marker_and_garbage(tmp_path):
    p = ap.AgentProvenance("codex", "m", "unknown", "s")
    _closed(tmp_path, '{"closed_to_non_claude": false}')
    assert ap.admission_advisory(tmp_path, p) is None
    (tmp_path / ".coordinator" / "agent-admission.json").write_text("{not json", encoding="utf-8")
    assert ap.admission_advisory(tmp_path, p) is None
    (tmp_path / ".coordinator" / "agent-admission.json").write_text("[1]", encoding="utf-8")
    assert ap.admission_advisory(tmp_path, p) is None


def test_grep_pattern_selects_by_vendor():
    any_re = re.compile(ap.provenance_grep_pattern(), re.M)
    codex_re = re.compile(ap.provenance_grep_pattern("codex"), re.M)
    msg = "s\n\nAgent-Vendor: codex\nAgent-Tier: x\n"
    assert any_re.search(msg) and codex_re.search(msg)
    assert not re.compile(ap.provenance_grep_pattern("grok"), re.M).search(msg)
    assert not any_re.search("s\n\nSession-Id: x\n")


def test_writer_waits_for_both_consumer_readiness_acks(monkeypatch):
    p = ap.AgentProvenance("codex", "gpt-6-astra", "frontier", "native-session")
    monkeypatch.setattr(ap, "WRITER_READINESS_ACKS", {})
    assert ap.emitted_provenance_trailers(p) == []
    monkeypatch.setattr(ap, "WRITER_READINESS_ACKS", {"project-rag": "ack-revision"})
    assert ap.emitted_provenance_trailers(p) == []
    monkeypatch.setattr(ap, "WRITER_READINESS_ACKS", {"project-rag": "ack-revision", "example-cockpit-repo": "ack-revision"})
    assert ap.emitted_provenance_trailers(p) == ap.provenance_trailers(p)


def test_live_writer_hold_is_closed_until_readiness_is_recorded():
    assert ap.WRITER_READINESS_ACKS == {}
