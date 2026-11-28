"""The review.reverify listing lexes each file once and skips the base lex when no agent comment can be new."""

from __future__ import annotations

import time

from coordinator_core.commenting import added
from coordinator_core.commenting.added import added_comments
from coordinator_core.ops import review_reverify as rr

FULL_BASE = "a" * 40
COMMENT = "# explains the retry"
PROCESS_BUDGET_S = 0.5


def _blob(content: str) -> bytes:
    data = content.encode()
    return b"%s blob %d\n%s\n" % (b"c" * 40, len(data), data)


def _body(i: int, extra: str = "") -> str:
    funcs = "".join(f"def f{i}_{n}(x):\n    return x + {n}\n\n" for n in range(120))
    return funcs + extra


def _land_world(monkeypatch, tmp_path, count):
    paths = [f"pkg/m{i:02d}.py" for i in range(count)]
    log = f"\x1e{'b' * 40}\x1f edit\n\nSession-Id: abc-123\n\x1f\n" + "".join(
        f"diff --git a/{p} b/{p}\n+++ b/{p}\n@@ -1,0 +2 @@\n+{COMMENT}\n" for p in paths
    )
    pairs = b""
    for i, p in enumerate(paths):
        head = _body(i, f"{COMMENT}\n")
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p).write_text(head, encoding="utf-8")
        pairs += _blob(_body(i)) + _blob(head)

    def fake_runner(args, cwd, stdin):
        return log.encode() if args[:1] == ["log"] else pairs

    monkeypatch.setattr(rr.authorship, "default_runner", fake_runner)
    monkeypatch.setattr(rr.governed_by, "provenance_findings", lambda *_a, **_k: [])
    added._lex_cache.clear()
    return paths


def _count_lex(monkeypatch):
    calls: list = []
    real = added._lex

    def counted(suffix, text):
        calls.append(suffix)
        return real(suffix, text)

    monkeypatch.setattr(added, "_lex", counted)
    return calls


def test_land_findings_lexes_each_file_once_per_blob(monkeypatch, tmp_path):
    count = 8
    paths = _land_world(monkeypatch, tmp_path, count)
    calls = _count_lex(monkeypatch)
    start = time.process_time()
    rows, _prov = rr._land_findings(tmp_path, FULL_BASE)
    spent = time.process_time() - start
    assert [r["path"] for r in rows] == paths
    assert len(calls) == 2 * count
    assert spent < PROCESS_BUDGET_S


def test_second_land_call_over_the_same_blobs_lexes_nothing(monkeypatch, tmp_path):
    _land_world(monkeypatch, tmp_path, 4)
    rr._land_findings(tmp_path, FULL_BASE)
    calls = _count_lex(monkeypatch)
    rows, _prov = rr._land_findings(tmp_path, FULL_BASE)
    assert len(rows) == 4
    assert calls == []


def test_before_is_not_lexed_when_no_flagged_comment_is_kept(monkeypatch):
    calls = _count_lex(monkeypatch)
    added._lex_cache.clear()
    after = "x = 1\n# legacy untagged remark\n"
    assert added_comments("pkg/mod.py", "x = 1\n", after, keep=lambda _t: False) == []
    assert len(calls) == 1


def test_before_is_lexed_when_a_flagged_comment_is_kept(monkeypatch):
    calls = _count_lex(monkeypatch)
    added._lex_cache.clear()
    out = added_comments("pkg/mod.py", "x = 1\n", "x = 1\n# fresh untagged remark\n", keep=lambda _t: True)
    assert [c.text for c in out] == ["# fresh untagged remark"]
    assert len(calls) == 2


def test_after_with_nothing_flagged_never_lexes_before(monkeypatch):
    calls = _count_lex(monkeypatch)
    added._lex_cache.clear()
    assert added_comments("pkg/mod.py", "y = 2\n", "x = 1\n# trap: tagged\n") == []
    assert len(calls) == 1


def test_unparsable_base_still_yields_nothing(monkeypatch):
    added._lex_cache.clear()
    assert added_comments("pkg/mod.py", "def broken(:\n", "x = 1\n# fresh untagged remark\n") == []


def test_a_spent_scan_budget_lexes_nothing_more_and_says_so(monkeypatch, tmp_path):
    _land_world(monkeypatch, tmp_path, 4)
    monkeypatch.setattr(rr, "_LAND_SCAN_BUDGET_S", -1.0)
    calls = _count_lex(monkeypatch)
    notes: list = []
    rows, _prov = rr._land_findings(tmp_path, FULL_BASE, notes)
    assert rows == []
    assert calls == []
    assert len(notes) == 1 and notes[0].startswith("comment scan incomplete: 4 file(s)")


def test_a_spent_budget_resumes_over_the_cache_on_the_next_call(monkeypatch, tmp_path):
    _land_world(monkeypatch, tmp_path, 4)
    monkeypatch.setattr(rr, "_LAND_SCAN_BUDGET_S", -1.0)
    rr._land_findings(tmp_path, FULL_BASE, [])
    monkeypatch.setattr(rr, "_LAND_SCAN_BUDGET_S", 30.0)
    notes: list = []
    rows, _prov = rr._land_findings(tmp_path, FULL_BASE, notes)
    assert len(rows) == 4
    assert notes == []


def test_a_failed_scan_is_named_not_read_as_no_findings(monkeypatch, tmp_path):
    _land_world(monkeypatch, tmp_path, 2)

    def boom(*_a, **_k):
        raise OSError("disk")

    monkeypatch.setattr(rr, "agent_added_comments", boom)
    notes: list = []
    assert rr._land_findings(tmp_path, FULL_BASE, notes) == ([], [])
    assert notes == ["comment scan failed: OSError"]
