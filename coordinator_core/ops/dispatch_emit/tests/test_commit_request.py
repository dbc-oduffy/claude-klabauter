"""AC2: render_marker/parse_marker round-trip; one-line marker; malformed
and multi-marker refusal."""

import pytest

from coordinator_core.ops.dispatch_emit.commit_request import (
    MARKER_PREFIX,
    PREFIX_CLAIM_LABEL,
    ChunkCommit,
    CommitRequest,
    MalformedCommitRequestError,
    parse_marker,
    render_marker,
)


def _fixture_request():
    return CommitRequest(
        chunks=(
            ChunkCommit(
                id="C1",
                title="Wake digest",
                paths=("coordinator_core/contract/wake-digest.schema.json",),
                prefixes=(),
                report="",
            ),
            ChunkCommit(
                id="C2",
                title="Commit request marker",
                paths=("coordinator_core/ops/dispatch_emit/commit_request.py",),
                prefixes=("state/handoffs/",),
                report=".coordinator-local/subagent-share/dispatch-reports/plan/C2.md",
            ),
        ),
        deliverable_id="dlv-example-1234",
        session_id="c4e53fa2-2bda-51b1-833a-cf629f2b0cb6",
        repo_root="/repo",  # abs-path-ok: opaque fixture value, never resolved
        plan_path="docs/plans/example.md",
    )


def test_round_trip_equal():
    req = _fixture_request()
    marker = render_marker(req)
    assert marker is not None
    parsed = parse_marker(marker)
    assert parsed == req


def test_marker_is_one_ascii_line():
    req = _fixture_request()
    marker = render_marker(req)
    assert "\n" not in marker
    assert "\r" not in marker
    assert marker.encode("ascii")  # raises on any non-ascii byte
    assert marker.startswith(MARKER_PREFIX)


def test_u2028_and_quote_survive_round_trip():
    req = CommitRequest(
        chunks=(
            ChunkCommit(
                id="C9",
                title='Title with "quote" and   separator',
                paths=("some/path.py",),
                prefixes=(),
                report="",
            ),
        ),
        deliverable_id=None,
        session_id=None,
        repo_root=None,
        plan_path=None,
    )
    marker = render_marker(req)
    assert marker is not None
    assert "\n" not in marker
    assert " " not in marker  # ensure_ascii escapes it
    assert marker.encode("ascii")
    parsed = parse_marker(marker)
    assert parsed == req
    assert parsed.chunks[0].title == 'Title with "quote" and   separator'


def test_two_markers_raise():
    req = _fixture_request()
    marker = render_marker(req)
    script = f"{marker}\nsome js\n{marker}\n"
    with pytest.raises(MalformedCommitRequestError):
        parse_marker(script)


def test_malformed_payload_raises():
    script = f"{MARKER_PREFIX}{{not valid json\n"
    with pytest.raises(MalformedCommitRequestError):
        parse_marker(script)


def test_malformed_payload_missing_field_raises():
    script = f'{MARKER_PREFIX}{{"version":1,"chunks":[]}}\n'
    with pytest.raises(MalformedCommitRequestError):
        parse_marker(script)


def test_no_marker_returns_none():
    assert parse_marker("just some js\nconsole.log(1);\n") is None


def test_chunk_with_no_paths_and_no_prefixes_is_omitted():
    req = CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="Contributes", paths=("a.py",), prefixes=()),
            ChunkCommit(id="C2", title="Contributes nothing", paths=(), prefixes=()),
        ),
    )
    marker = render_marker(req)
    parsed = parse_marker(marker)
    assert [c.id for c in parsed.chunks] == ["C1"]


def test_no_chunk_left_means_no_marker():
    req = CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="Contributes nothing", paths=(), prefixes=()),
        ),
    )
    assert render_marker(req) is None


def test_prefix_claim_label_literal():
    assert PREFIX_CLAIM_LABEL == "created-under-prefix:"
