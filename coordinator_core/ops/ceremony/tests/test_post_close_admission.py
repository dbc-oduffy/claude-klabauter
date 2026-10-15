"""Tests for post_close_admission: pure arithmetic, stubbed reading, tmp_path mailbox only."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import _host_probe
from coordinator_core.group_em import admission_post
from coordinator_core.ops.ceremony import post_close_admission as pca
from coordinator_core.ops.dispatch_emit.run_close import check_path_hygiene


def test_constants():
    assert pca.RESERVE_MB == 8192
    assert set(pca.DECLARED_PEAK_MB) == {"structural-index", "scip-rebuild"}


def test_admit_exactly_at_threshold():
    need = pca.DECLARED_PEAK_MB["structural-index"] + pca.RESERVE_MB
    assert pca.admit("structural-index", need, True).admitted
    assert not pca.admit("structural-index", need - 1, True).admitted


def test_admit_subtracts_already_admitted():
    need = pca.DECLARED_PEAK_MB["scip-rebuild"] + pca.RESERVE_MB
    assert pca.admit("scip-rebuild", need + 100, True, already_admitted_mb=100).admitted
    assert not pca.admit("scip-rebuild", need + 99, True, already_admitted_mb=100).admitted


def test_admit_untrusted_or_none_defers():
    assert pca.admit("structural-index", 999999, False).reason == "memory unreadable"
    v = pca.admit("structural-index", None, True)
    assert not v.admitted and v.reason == "memory unreadable"


def test_admit_unknown_label():
    v = pca.admit("nope", 999999, True)
    assert not v.admitted and v.reason == "no declared peak"


def test_reason_is_path_hygienic():
    for v in (
        pca.admit("structural-index", 4608, True),
        pca.admit("structural-index", None, False),
        pca.admit("x", 1, True),
        pca.admit("structural-index", 99999, True),
    ):
        check_path_hygiene(v.reason)
    assert pca.admit("structural-index", 4608, True).reason == "need 10240MB > avail 4608MB"


def test_notice_admitted_posts_one_start(monkeypatch):
    calls = []

    def fake_post(phase, **kw):
        calls.append((phase, kw))
        return {"posted": True}

    monkeypatch.setattr(admission_post, "post", fake_post)
    v = pca.admit("structural-index", 20000, True)
    assert pca.notice(v, "run1") is True
    assert len(calls) == 1
    phase, kw = calls[0]
    assert phase == "START" and kw["run"] == "run1" and kw["session_id"] == "close:run1"
    assert "structural-index" in kw["note"]
    assert kw["require_holder"] is False


def test_notice_without_holder_appends_to_admission_jsonl(tmp_path, monkeypatch):
    from coordinator_core.group_em import nomination

    monkeypatch.setattr(nomination, "box_holder", lambda directory=None, now=None: None)
    real = admission_post.post
    monkeypatch.setattr(admission_post, "post", lambda *a, **k: real(*a, directory=tmp_path, **k))
    assert pca.notice(pca.admit("structural-index", 20000, True), "run9") is True
    text = admission_post.admission_path(tmp_path).read_text(encoding="utf-8")
    assert '"phase":"START"' in text and '"run":"run9"' in text


def test_notice_deferred_posts_nothing(monkeypatch):
    monkeypatch.setattr(admission_post, "post", lambda *a, **k: pytest.fail("posted"))
    assert pca.notice(pca.admit("structural-index", 100, True), "r") is False


def test_notice_swallows_raise(monkeypatch):
    def boom(*a, **k):
        raise OSError("x")

    monkeypatch.setattr(admission_post, "post", boom)
    assert pca.notice(pca.admit("structural-index", 20000, True), "r") is False


def test_notice_no_holder_returns_false(monkeypatch):
    monkeypatch.setattr(admission_post, "post", lambda *a, **k: {"posted": False})
    assert pca.notice(pca.admit("structural-index", 20000, True), "r") is False


def test_read_available_maps_reading(monkeypatch):
    monkeypatch.setattr(
        _host_probe, "read_available_mb", lambda: _host_probe.MemoryReading(12345, True, "t")
    )
    assert pca.read_available() == (12345, True)


def test_read_available_raising_probe(monkeypatch):
    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(_host_probe, "read_available_mb", boom)
    assert pca.read_available() == (None, False)
