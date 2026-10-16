"""Tests for canvas_harvest.lift over synthetic transcripts built under tmp_path."""

import json

import pytest

from coordinator_core.canvas_harvest import lift as lift_mod
from coordinator_core.canvas_harvest import model

AID = "Qz7LmPx2NcVt9RkHdYw3Fe"
OTHER = "Zz0OtherArtifactId0000"
URL = f"https://claude.ai/artifact/{AID}"


class T:
    def __init__(self, tmp_path, name="t.jsonl"):
        self.path = tmp_path / name
        self.n = 0
        self.lines = []

    def _add(self, obj):
        self.lines.append(json.dumps(obj))

    def call(self, name, inp, tid, side=False):
        self.n += 1
        self._add({
            "type": "assistant", "isSidechain": side, "sessionId": "sess-1",
            "timestamp": f"2026-10-13T10:00:{self.n:02d}Z",
            "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]},
        })

    def result(self, tid, payload, text="rendered", error=False, side=False):
        self.n += 1
        block = {"type": "tool_result", "tool_use_id": tid, "content": text}
        if error:
            block["is_error"] = True
        self._add({
            "type": "user", "isSidechain": side, "sessionId": "sess-1",
            "timestamp": f"2026-10-13T10:00:{self.n:02d}Z",
            "message": {"content": [block]}, "toolUseResult": payload,
        })

    def write(self, torn=None):
        body = "\n".join(self.lines) + "\n"
        data = body.encode("utf-8")
        if torn is not None:
            data += torn.encode("utf-8")
        self.path.write_bytes(data)
        return self.path


def frame(path):
    return {"path": path, "saved_to": f"frames/{path}", "sha256": "ab" * 32}


def files_read(ver="v1", paths=("project/index.html",), url=URL):
    return {"files_read": {"url": url, "title": "Doc", "ver": ver, "files": [frame(p) for p in paths],
                           "type": {"url": "", "title": "plain"}}}


def file_list(ver="v1", paths=("project/index.html",), kind="dashboard"):
    return {"file_list": {"url": URL, "ver": ver, "files": [{"path": p} for p in paths],
                          "type": {"url": "", "title": kind}}}


def thread(tid):
    return {"id": tid, "comments": [{"id": tid + "c", "created_at": "2026-10-01T00:00:00Z", "text": "body " + tid}]}


def read_artifact(t, tid="r1", **kw):
    t.call("Artifact", {"action": "read", "url": URL, "paths": ["index.html"]}, tid)
    t.result(tid, files_read(**kw))


def test_whole_read(tmp_path):
    t = T(tmp_path)
    read_artifact(t, paths=("project/index.html", "project/b.html"))
    t.call("ArtifactComments", {"action": "read", "artifact_id": AID}, "c1")
    t.result("c1", {"threads": [thread("t1"), thread("t2")]}, text="THREADS")
    got = lift_mod.lift([t.write()], AID)
    assert [f.path for f in got.artifact_read.frames] == ["project/index.html", "project/b.html"]
    assert got.artifact_read.version == "v1" and got.artifact_read.type == "plain"
    assert got.artifact_read.url == URL and got.artifact_read.read_at == "2026-10-13T10:00:02Z"
    assert model.thread_ids(got.comments.threads) == ["t1", "t2"]
    assert got.comments.rendered_text == "THREADS"
    assert got.comments.read_at == "2026-10-13T10:00:04Z"
    assert got.version.version == "v1" and got.error is None
    assert got.transcript_dir == tmp_path and got.session_id == "sess-1"


def test_file_read_and_listing_type(tmp_path):
    t = T(tmp_path)
    t.call("Artifact", {"action": "list", "url": URL}, "l1")
    t.result("l1", file_list(paths=("project/index.html",)))
    t.call("Artifact", {"action": "read", "url": URL, "path": "project/index.html"}, "r1")
    t.result("r1", {"file_read": {**frame("project/index.html"), "url": URL, "title": "Doc", "ver": "v1"}})
    got = lift_mod.lift([t.write()], AID)
    assert got.artifact_read.type == "dashboard"
    assert [f.path for f in got.artifact_read.frames] == ["project/index.html"]


def test_unread_runtime_files_do_not_make_listing_incomplete(tmp_path):
    t = T(tmp_path)
    read_artifact(t, paths=("project/index.html", "app.js", "reference/x.md"))
    t.call("Artifact", {"action": "list", "url": URL}, "l1")
    t.result("l1", file_list(paths=("project/index.html", "app.js", "SKILL.md", "index.html")))
    got = lift_mod.lift([t.write()], AID)
    assert [f.path for f in got.artifact_read.frames] == ["project/index.html"]


def test_unread_project_file_still_incomplete_beside_runtime_files(tmp_path):
    t = T(tmp_path)
    read_artifact(t, paths=("app.js",))
    t.call("Artifact", {"action": "list", "url": URL}, "l1")
    t.result("l1", file_list(paths=("app.js", "project/a.html")))
    with pytest.raises(model.HarvestError) as err:
        lift_mod.lift([t.write()], AID)
    assert err.value.reason == model.FRAMES_READ_INCOMPLETE


def test_listing_not_covered_is_incomplete(tmp_path):
    t = T(tmp_path)
    read_artifact(t)
    t.call("Artifact", {"action": "list", "url": URL}, "l1")
    t.result("l1", file_list(paths=("project/index.html", "project/missing.html")))
    with pytest.raises(model.HarvestError) as err:
        lift_mod.lift([t.write()], AID)
    assert err.value.reason == model.FRAMES_READ_INCOMPLETE


def _listing_and_comments(t, comments=True):
    t.call("Artifact", {"action": "list", "url": URL}, "l1")
    t.result("l1", file_list(paths=("project/a.html", "project/b.html")))
    if comments:
        t.call("ArtifactComments", {"action": "read", "artifact_id": AID}, "c1")
        t.result("c1", {"threads": [thread("t1")]}, text="THREADS")


def test_listing_without_frame_reads_lifts_version_and_comments_when_frames_not_required(tmp_path):
    t = T(tmp_path)
    _listing_and_comments(t)
    path = t.write()
    got = lift_mod.lift([path], AID, require_frames=False)
    assert got.version.version == "v1" and got.artifact_read is None
    assert model.thread_ids(got.comments.threads) == ["t1"]
    with pytest.raises(model.HarvestError) as err:
        lift_mod.lift([path], AID)
    assert err.value.reason == model.FRAMES_READ_INCOMPLETE


def test_frames_not_required_still_refuses_incomplete_comments(tmp_path):
    t = T(tmp_path)
    _listing_and_comments(t, comments=False)
    t.call("ArtifactComments", {"action": "read", "artifact_id": AID}, "c1")
    t.result("c1", {"threads": [thread("t1")], "cursor": "CUR1"})
    with pytest.raises(model.HarvestError) as err:
        lift_mod.lift([t.write()], AID, require_frames=False)
    assert err.value.reason == model.COMMENTS_READ_INCOMPLETE


def test_api_probe_lifts_without_frames_and_harvest_requires_them(tmp_path, monkeypatch):
    from coordinator_core.canvas_harvest import api

    seen = []

    def spy(transcripts, artifact_id, **kw):
        seen.append(kw)
        raise model.HarvestError(model.NO_MAIN_LOOP_READ, "stop")

    monkeypatch.setattr(lift_mod, "lift", spy)
    t = T(tmp_path)
    _listing_and_comments(t)
    for call in (
        lambda: api.probe(URL, tmp_path / "out", session_id="sess-1", transcript=str(t.write())),
        lambda: api.harvest(URL, tmp_path / "out", session_id="sess-1", transcript=str(t.write())),
    ):
        with pytest.raises(model.HarvestError):
            call()
    assert seen[0].get("require_frames") is False
    assert seen[1].get("require_frames", True) is True


def test_api_probe_hands_stamp_the_version_and_comments_of_a_frameless_transcript(tmp_path, monkeypatch):
    from coordinator_core.canvas_harvest import api, probe_stamp

    seen = []

    def fake_stamp(lifted, out_dir, session_id, now, *, artifact_id=None):
        seen.append(lifted)
        return tmp_path / "harvest.json"

    monkeypatch.setattr(probe_stamp, "stamp_probe", fake_stamp)
    t = T(tmp_path)
    _listing_and_comments(t)
    api.probe(URL, tmp_path / "out", session_id="sess-1", transcript=str(t.write()))
    assert seen[0].version.version == "v1" and model.thread_ids(seen[0].comments.threads) == ["t1"]


def test_newer_version_drops_older_reads(tmp_path):
    t = T(tmp_path)
    read_artifact(t, ver="v1")
    t.call("Artifact", {"action": "list", "url": URL}, "l1")
    t.result("l1", file_list(ver="v2", paths=("project/index.html",)))
    with pytest.raises(model.HarvestError) as err:
        lift_mod.lift([t.write()], AID)
    assert err.value.reason == model.FRAMES_READ_INCOMPLETE


def test_cursor_chain_of_two_pages(tmp_path):
    t = T(tmp_path)
    t.call("ArtifactComments", {"action": "read", "url": URL}, "c1")
    t.result("c1", {"threads": [thread("t1")], "cursor": "CUR1"}, text="P1")
    t.call("ArtifactComments", {"action": "read", "url": URL, "cursor": "CUR1"}, "c2")
    t.result("c2", {"threads": [thread("t2")]}, text="P2")
    got = lift_mod.lift([t.write()], AID)
    assert model.thread_ids(got.comments.threads) == ["t1", "t2"]
    assert got.comments.rendered_text == "P1\nP2"
    assert got.artifact_read is None


def test_unterminated_chain(tmp_path):
    t = T(tmp_path)
    t.call("ArtifactComments", {"action": "read", "url": URL}, "c1")
    t.result("c1", {"threads": [thread("t1")], "cursor": "CUR1"})
    with pytest.raises(model.HarvestError) as err:
        lift_mod.lift([t.write()], AID)
    assert err.value.reason == model.COMMENTS_READ_INCOMPLETE


def test_thread_id_read_is_not_whole(tmp_path):
    t = T(tmp_path)
    t.call("ArtifactComments", {"action": "read", "url": URL, "thread_id": "t1"}, "c1")
    t.result("c1", {"threads": [thread("t1")], "thread_filter": "t1"})
    with pytest.raises(model.HarvestError) as err:
        lift_mod.lift([t.write()], AID)
    assert err.value.reason == model.COMMENTS_READ_INCOMPLETE


def test_thread_id_read_after_whole_read_is_ignored(tmp_path):
    t = T(tmp_path)
    t.call("ArtifactComments", {"action": "read", "url": URL}, "c1")
    t.result("c1", {"threads": [thread("t1"), thread("t2")]}, text="WHOLE")
    t.call("ArtifactComments", {"action": "read", "url": URL, "thread_id": "t1"}, "c2")
    t.result("c2", {"threads": [thread("t1")], "thread_filter": "t1"}, text="ONE")
    got = lift_mod.lift([t.write()], AID)
    assert model.thread_ids(got.comments.threads) == ["t1", "t2"]
    assert got.comments.rendered_text == "WHOLE\nONE"


@pytest.mark.parametrize(
    "text,kind",
    [
        ("Error: artifact not found -- it may have been deleted", model.NOT_FOUND),
        ("Nothing was done. This needs a yes to: \"read\"", model.NEEDS_APPROVAL),
        ("Something else broke\nsecond line", None),
    ],
)
def test_error_result_kinds(tmp_path, text, kind):
    t = T(tmp_path)
    read_artifact(t)
    t.call("ArtifactComments", {"action": "read", "artifact_id": AID}, "c1")
    t.result("c1", "Error: " + text, text=text, error=True)
    got = lift_mod.lift([t.write()], AID)
    assert got.error.kind == kind
    assert got.error.first_line == text.split("\n")[0]
    assert got.error.at == "2026-10-13T10:00:04Z"


def test_success_after_error_clears_it(tmp_path):
    t = T(tmp_path)
    t.call("ArtifactComments", {"action": "read", "artifact_id": AID}, "c1")
    t.result("c1", "Error: artifact not found", text="artifact not found", error=True)
    read_artifact(t)
    assert lift_mod.lift([t.write()], AID).error is None


def test_failed_write_is_skipped(tmp_path):
    t = T(tmp_path)
    read_artifact(t)
    t.call("Artifact", {"action": "write", "artifact_id": AID, "files": []}, "w1")
    t.result("w1", "Error: nope", text="You haven't viewed the latest version", error=True)
    assert lift_mod.lift([t.write()], AID).error is None


def test_second_artifact_ignored(tmp_path):
    t = T(tmp_path)
    t.call("Artifact", {"action": "read", "url": f"https://claude.ai/artifact/{OTHER}", "paths": ["x"]}, "r9")
    t.result("r9", files_read(ver="v9", url=f"https://claude.ai/artifact/{OTHER}"))
    read_artifact(t)
    got = lift_mod.lift([t.write()], AID)
    assert got.version.version == "v1"
    t2 = T(tmp_path, "u.jsonl")
    t2.call("ArtifactComments", {"action": "read", "artifact_id": OTHER}, "c9")
    t2.result("c9", "Error: artifact not found", text="artifact not found", error=True)
    with pytest.raises(model.HarvestError) as err:
        lift_mod.lift([t2.write()], AID)
    assert err.value.reason == model.NO_MAIN_LOOP_READ


def test_sidechain_ignored(tmp_path):
    t = T(tmp_path)
    t.call("Artifact", {"action": "read", "url": URL, "paths": ["index.html"]}, "r1", side=True)
    t.result("r1", files_read(), side=True)
    with pytest.raises(model.HarvestError) as err:
        lift_mod.lift([t.write()], AID)
    assert err.value.reason == model.NO_MAIN_LOOP_READ


def test_binding_by_url_artifact_id_and_result_url(tmp_path):
    t = T(tmp_path)
    t.call("ArtifactComments", {"action": "read", "artifact_id": AID}, "c1")
    t.result("c1", {"threads": [thread("a")]})
    t.call("ArtifactComments", {"action": "read", "url": URL}, "c2")
    t.result("c2", {"threads": [thread("b")]})
    got = lift_mod.lift([t.write()], AID)
    assert model.thread_ids(got.comments.threads) == ["b"]
    t2 = T(tmp_path, "u.jsonl")
    t2.call("Artifact", {"action": "read", "paths": ["index.html"]}, "r1")
    t2.result("r1", files_read())
    assert lift_mod.lift([t2.write()], AID).artifact_read.version == "v1"


def test_no_call_at_all(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
    with pytest.raises(model.HarvestError) as err:
        lift_mod.lift([p], AID)
    assert err.value.reason == model.NO_MAIN_LOOP_READ


def test_no_transcript(tmp_path):
    with pytest.raises(model.HarvestError) as err:
        lift_mod.lift([tmp_path / "absent.jsonl"], AID)
    assert err.value.reason == model.NO_TRANSCRIPT


def test_torn_final_line_skipped(tmp_path):
    t = T(tmp_path)
    read_artifact(t)
    torn = '{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "ArtifactComments"'
    got = lift_mod.lift([t.write(torn=torn)], AID)
    assert got.artifact_read.version == "v1" and got.comments is None


def side_call(t, agent, name, inp, tid):
    t.n += 1
    t._add({
        "type": "assistant", "isSidechain": True, "agentId": agent, "sessionId": "sess-1",
        "timestamp": f"2026-10-13T10:00:{t.n:02d}Z",
        "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]},
    })


def side_result(t, agent, tid, payload, text="rendered"):
    t.n += 1
    t._add({
        "type": "user", "isSidechain": True, "agentId": agent, "sessionId": "sess-1",
        "timestamp": f"2026-10-13T10:00:{t.n:02d}Z",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]},
        "toolUseResult": payload,
    })


def test_named_reader_sidechain_lifts(tmp_path):
    t = T(tmp_path)
    side_call(t, "aX", "Artifact", {"action": "read", "url": URL, "paths": ["index.html"]}, "r1")
    side_result(t, "aX", "r1", files_read())
    got = lift_mod.lift([t.write()], AID, reader_agent_id="aX")
    assert [f.path for f in got.artifact_read.frames] == ["project/index.html"]


def test_other_sidechain_ignored_beside_named_reader(tmp_path):
    t = T(tmp_path)
    side_call(t, "aY", "Artifact", {"action": "read", "url": URL, "paths": ["b.html"]}, "r0")
    side_result(t, "aY", "r0", files_read(paths=("project/other.html",)))
    side_call(t, "aX", "Artifact", {"action": "read", "url": URL, "paths": ["index.html"]}, "r1")
    side_result(t, "aX", "r1", files_read())
    got = lift_mod.lift([t.write()], AID, reader_agent_id="aX")
    assert [f.path for f in got.artifact_read.frames] == ["project/index.html"]


def test_default_none_over_all_sidechain_refuses(tmp_path):
    t = T(tmp_path)
    side_call(t, "aX", "Artifact", {"action": "read", "url": URL, "paths": ["index.html"]}, "r1")
    side_result(t, "aX", "r1", files_read())
    with pytest.raises(model.HarvestError) as err:
        lift_mod.lift([t.write()], AID)
    assert err.value.reason == model.NO_MAIN_LOOP_READ


def test_named_reader_cursor_chain_and_toolsearch(tmp_path):
    t = T(tmp_path)
    side_call(t, "aX", "ToolSearch", {"query": "select:Artifact"}, "ts1")
    side_call(t, "aX", "ArtifactComments", {"action": "read", "url": URL}, "c1")
    side_result(t, "aX", "c1", {"threads": [thread("t1")], "cursor": "CUR1"}, text="P1")
    side_call(t, "aX", "ToolSearch", {"query": "again Artifact"}, "ts2")
    side_call(t, "aX", "ArtifactComments", {"action": "read", "url": URL, "cursor": "CUR1"}, "c2")
    side_result(t, "aX", "c2", {"threads": [thread("t2")]}, text="P2")
    got = lift_mod.lift([t.write()], AID, reader_agent_id="aX")
    assert model.thread_ids(got.comments.threads) == ["t1", "t2"]


def test_named_reader_absent_refuses_whole(tmp_path):
    t = T(tmp_path)
    side_call(t, "aY", "Artifact", {"action": "read", "url": URL, "paths": ["index.html"]}, "r1")
    side_result(t, "aY", "r1", files_read())
    with pytest.raises(model.HarvestError) as err:
        lift_mod.lift([t.write()], AID, reader_agent_id="aX")
    assert err.value.reason == model.NO_MAIN_LOOP_READ


def test_named_reader_relative_saved_to_resolves_in_transcript_dir(tmp_path):
    sub = tmp_path / "subagents"
    sub.mkdir()
    t = T(sub, "agent-aX.jsonl")
    side_call(t, "aX", "Artifact", {"action": "read", "url": URL, "paths": ["index.html"]}, "r1")
    side_result(t, "aX", "r1", files_read())
    got = lift_mod.lift([t.write()], AID, reader_agent_id="aX")
    assert got.transcript_dir == sub
    assert [f.path for f in got.artifact_read.frames] == ["project/index.html"]
