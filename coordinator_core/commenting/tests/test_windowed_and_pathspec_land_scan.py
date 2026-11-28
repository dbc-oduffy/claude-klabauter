"""The land comment scan lexes oversize files by window, requests only code paths, and fetches only files that can hold an agent comment."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.commenting import added, authorship
from coordinator_core.commenting.added import added_comments
from coordinator_core.commenting.windowed import OVERSIZE_CHARS, windowed_added_comments
from coordinator_core.ops import review_reverify as rr

REMARK = "# fresh untagged remark"


def _big(extra_at: int, extra: str) -> str:
    funcs = []
    for n in range(2000):
        body = f"def f{n}(x):\n    return x + {n}\n"
        if n == extra_at:
            body = f"def f{n}(x):\n    {extra}\n    return x + {n}\n"
        funcs.append(body)
    text = "\n".join(funcs) + "\n"
    assert len(text) > OVERSIZE_CHARS
    return text


def test_window_lexes_only_the_definition_around_the_seed(monkeypatch):
    text = _big(1500, REMARK)
    lexed: list[int] = []
    real = added._lex
    monkeypatch.setattr(added, "_lex", lambda s, t: (lexed.append(len(t)), real(s, t))[1])
    added._lex_cache.clear()
    found = windowed_added_comments("pkg/big.py", text, lambda ln: ln == REMARK)
    assert [(c.line, c.text) for c in found] == [(text.split("\n").index("    " + REMARK) + 1, REMARK)]
    assert lexed and max(lexed) < len(text) // 50


def test_window_agrees_with_the_full_lex_on_line_and_text():
    text = _big(700, REMARK)
    added._lex_cache.clear()
    full = added_comments("pkg/big.py", "", text)
    win = windowed_added_comments("pkg/big.py", text, lambda ln: ln == REMARK)
    assert [(c.line, c.text) for c in win] == [(c.line, c.text) for c in full]


def test_window_with_no_seed_lexes_nothing(monkeypatch):
    monkeypatch.setattr(added, "_lex", lambda *_a: (_ for _ in ()).throw(AssertionError("lexed")))
    assert windowed_added_comments("pkg/big.py", _big(5, REMARK), lambda ln: False) == []


def test_window_after_an_unbalanced_triple_quote_is_refused():
    text = 'x = """open\n' + _big(10, REMARK)
    assert windowed_added_comments("pkg/big.py", text, lambda ln: ln == REMARK) is None


def test_window_that_does_not_parse_is_refused():
    text = _big(10, REMARK).replace("def f10(x):", "def f10(x:", 1)
    assert windowed_added_comments("pkg/big.py", text, lambda ln: ln == REMARK) is None


def test_may_hold_comment_per_language():
    assert authorship.may_hold_comment("a.py", ["x = 1  # c"])
    assert authorship.may_hold_comment("a.py", ['    """doc'])
    assert not authorship.may_hold_comment("a.py", ["x = 1", "return y"])
    assert authorship.may_hold_comment("a.ts", ["let a; // c"])
    assert not authorship.may_hold_comment("a.ts", ["let a = 1;  # not a ts comment"])
    assert authorship.may_hold_comment("a.yaml", ["k: v  # c"])


def _log(path: str, line: str) -> str:
    return f"\x1e{'b' * 40}\x1f edit\n\nSession-Id: abc-123\n\x1f\ndiff --git a/{path} b/{path}\n+++ b/{path}\n@@ -1,0 +2 @@\n+{line}\n"


def test_agent_added_comments_requests_code_pathspecs_and_only_comment_bearing_files(tmp_path):
    log = _log("pkg/with.py", REMARK) + _log("pkg/without.py", "x = 1")
    calls: list[list[str]] = []
    requested: list[bytes] = []

    def runner(args, cwd, stdin):
        calls.append(args)
        if args[:1] == ["log"]:
            return log.encode()
        requested.append(stdin)
        return b""

    authorship.agent_added_comments(tmp_path, "a" * 40, "HEAD", runner=runner)
    log_args = calls[0]
    assert "*.py" in log_args[log_args.index("--") + 1:]
    assert b"pkg/with.py" in requested[0] and b"pkg/without.py" not in requested[0]


def test_repeated_log_text_is_parsed_once(monkeypatch):
    from coordinator_core.ops.ceremony import governed_by

    authorship._agent_added_lines.cache_clear()
    governed_by.pending_rows.cache_clear()
    text = _log("pkg/with.py", REMARK)
    authorship._agent_added_lines(text)
    authorship._agent_added_lines("".join(list(text)))
    governed_by.pending_rows(text)
    governed_by.pending_rows("".join(list(text)))
    assert authorship._agent_added_lines.cache_info().hits == 1
    assert governed_by.pending_rows.cache_info().hits == 1


def test_land_findings_finds_a_comment_in_an_oversize_file(monkeypatch, tmp_path: Path):
    base = _big(900, "pass")
    head = _big(900, REMARK)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/big.py").write_text(head, encoding="utf-8")
    log = _log("pkg/big.py", REMARK)

    def blob(content: str) -> bytes:
        data = content.encode()
        return b"%s blob %d\n%s\n" % (b"c" * 40, len(data), data)

    def runner(args, cwd, stdin):
        return log.encode() if args[:1] == ["log"] else blob(base) + blob(head)

    monkeypatch.setattr(rr.authorship, "default_runner", runner)
    monkeypatch.setattr(rr.governed_by, "provenance_findings", lambda *_a, **_k: [])
    added._lex_cache.clear()
    lexed: list[int] = []
    real = added._lex
    monkeypatch.setattr(added, "_lex", lambda s, t: (lexed.append(len(t)), real(s, t))[1])
    rows, _prov = rr._land_findings(tmp_path, "a" * 40)
    assert [(r["path"], r["text"]) for r in rows] == [("pkg/big.py", REMARK)]
    assert max(lexed) < len(head) // 10
