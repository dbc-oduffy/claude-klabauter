import json

from coordinator_core.ops.dispatch_emit import docs_check_citations as dc


def test_parse_rag_and_live():
    got = dc.parse("x [rag: project_cpp_symbol AActor] y [live: inspect L_Main]")
    assert got == [
        dc.Citation("rag", "project_cpp_symbol", "AActor"),
        dc.Citation("live", "inspect", "L_Main"),
    ]


def test_parse_ignores_other_forms():
    assert dc.parse("[web: x y] [rag:] plain") == []


def _transcript(tmp_path, blocks):
    p = tmp_path / "t.jsonl"
    lines = [json.dumps({"message": {"content": [b]}}) for b in blocks]
    p.write_text("not json\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_tool_calls_skips_garbage_and_non_tool_blocks(tmp_path):
    p = _transcript(
        tmp_path,
        [
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "name": "mcp__a__b", "input": {"q": "é"}},
        ],
    )
    assert dc.tool_calls(p) == [("mcp__a__b", '{"q": "é"}')]
    assert dc.tool_calls(tmp_path / "missing") == []


def test_backed_by_exact_final_segment_and_subject():
    calls = [("mcp__project-rag__project_cpp_symbol", json.dumps({"name": "AActor"}))]
    ok = dc.Citation("rag", "project_cpp_symbol", "AActor")
    wrong_subject = dc.Citation("rag", "project_cpp_symbol", "UObject")
    suffix_only = dc.Citation("rag", "symbol", "AActor")
    assert dc.unbacked([ok, wrong_subject, suffix_only], calls) == [wrong_subject, suffix_only]


def test_live_backed_and_nonascii_subject():
    calls = [("mcp__example_game_repo-control__inspect", json.dumps({"level": "L_Mäin"}, ensure_ascii=False))]
    assert dc.unbacked([dc.Citation("live", "inspect", "L_Mäin")], calls) == []
    assert dc.unbacked([dc.Citation("live", "inspect", "L_Other")], calls) != []


def test_same_tool_call_without_the_subject_does_not_back():
    calls = [("mcp__x__inspect", json.dumps({"target": "L_Main"}))]
    assert dc.unbacked([dc.Citation("live", "inspect", "L_Other")], calls) != []
    assert dc.unbacked([dc.Citation("live", "inspect", "L_Main")], calls) == []
