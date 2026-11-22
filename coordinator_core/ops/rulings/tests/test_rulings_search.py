"""coordinator_core/ops/rulings/tests/test_rulings_search.py — "rulings.search".

Claim under test: keyword (case-insensitive, all words) and scope-token intersection select
rulings; superseded rulings are excluded at default arguments.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.rulings import index as idx
from coordinator_core.ops.rulings import search as mod


def _seed(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    common = tmp_path / ".git"
    rows = [
        ("rl-private", ["repo"], "This is a PRIVATE repo, stop relitigating."),
        ("rl-apm", ["coordinator:apm", "rulings"], "The APM is the first pass; it may flag to the PM."),
        ("rl-paths", ["coordinator_core/ops/*"], "Ops are spawn free."),
    ]
    for rid, scope, text in rows:
        r = idx._handler(
            {"verb": "append", "ruling": {"id": rid, "pm_turn": "s.yaml R1", "scope": scope, "text": text}},
            common,
        )
        assert r["applied"] == [rid]
    idx._handler(
        {
            "verb": "append",
            "ruling": {
                "id": "rl-old",
                "pm_turn": "s.yaml R0",
                "scope": ["repo"],
                "text": "private repo, old wording",
                "status": "superseded",
                "superseded_by": "rl-private",
            },
        },
        common,
    )
    return common


def _ids(reply: dict) -> list:
    return [m["id"] for m in reply["matches"]]


def test_keyword_is_case_insensitive_and_all_words_must_hit(tmp_path):
    common = _seed(tmp_path)
    assert _ids(mod._handler({"query": "private REPO"}, common)) == ["rl-private"]
    assert _ids(mod._handler({"query": "private apm"}, common)) == []


def test_scope_token_intersection_incl_glob_and_case(tmp_path):
    common = _seed(tmp_path)
    assert _ids(mod._handler({"scope": ["REPO"]}, common)) == ["rl-private"]
    assert _ids(mod._handler({"scope": ["coordinator_core/ops/rulings/index.py"]}, common)) == ["rl-paths"]
    assert _ids(mod._handler({"scope": ["nothing"]}, common)) == []


def test_scope_and_query_must_both_hold(tmp_path):
    common = _seed(tmp_path)
    assert _ids(mod._handler({"scope": ["repo"], "query": "private"}, common)) == ["rl-private"]
    assert _ids(mod._handler({"scope": ["rulings"], "query": "private"}, common)) == []


def test_superseded_excluded_by_default_and_reachable_on_request(tmp_path):
    common = _seed(tmp_path)
    assert "rl-old" not in _ids(mod._handler({"scope": ["repo"]}, common))
    assert _ids(mod._handler({"scope": ["repo"], "status": "superseded"}, common)) == ["rl-old"]


def test_match_shape_and_empty_register(tmp_path):
    common = _seed(tmp_path)
    m = mod._handler({"query": "first pass"}, common)["matches"][0]
    assert set(m) == {"id", "text", "pm_turn", "scope"} and m["scope"] == ["coordinator:apm", "rulings"]
    empty = tmp_path / "e"
    (empty / ".git").mkdir(parents=True)
    assert mod._handler({}, empty / ".git") == {"matches": []}
