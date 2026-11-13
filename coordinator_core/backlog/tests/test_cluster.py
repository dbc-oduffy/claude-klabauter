from coordinator_core.backlog.cluster import cluster_records, topic_key
from coordinator_core.backlog.record_schema import ArtifactRecord, token_proxy


def rec(path, *headings):
    return ArtifactRecord(path=path, headings=tuple(headings))


def test_topic_key_normalisation():
    assert topic_key("docs/plans/2026-11/2026-11-13-whole-backlog-1b3850.md") == "whole-backlog"
    assert topic_key("docs/plans/2026-11-14-Whole_Backlog.md") == "whole-backlog"
    assert topic_key("docs/decisions/DR-344-the-brightline.md") == "dr-344"
    assert topic_key("docs/decisions/DR344.md") == "dr-344"


def test_duplicates_collapse_to_one():
    out = cluster_records([rec("b/x.md", "# Same  Title"), rec("a/y.md", "# same title")])
    assert len(out) == 1 and len(out[0].records) == 1
    assert out[0].records[0].path == "a/y.md"


def test_shared_topic_key_lands_in_one_digest():
    out = cluster_records([
        rec("p/2026-11-01-foo-aaaaaa.md", "# one"),
        rec("p/2026-11-02-foo.md", "# two"),
        rec("p/bar.md", "# three"),
    ])
    assert [d.topic_key for d in out] == ["bar", "foo"]
    assert len(out[1].records) == 2
    assert out[1].tokens == token_proxy(out[1].text)


def test_output_order_is_deterministic():
    recs = [rec(f"p/{n}.md", f"# {n}") for n in ("zeta", "alpha", "mid", "alpha-2")]
    assert cluster_records(recs) == cluster_records(list(reversed(recs)))
