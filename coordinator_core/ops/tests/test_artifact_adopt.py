from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops import artifact_adopt as mod
from coordinator_core.ops.artifact_adopt_contract import adopt_command

REL = "docs/plans/2026-10-01-engine-op-and-cli-gaps.md"
BODY = "\n# Engine op gaps\n\nText with trailing space \n\n```yaml\nk: v\n```\n"
HAND = "---\ntitle: Engine op gaps\ncreated: 2026-10-01\nauthor: Someone\nstatus: draft\nkind: x\n---" + BODY


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "DELIVERABLE_ID"):
        monkeypatch.delenv(k, raising=False)


def _repo(tmp_path: Path) -> Path:
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/work/test\n", encoding="utf-8")
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    return git


def _put(tmp_path: Path, text: str, rel: str = REL) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return p


def _run(git: Path, path: str = REL, **kw) -> dict:
    return mod._handler({"path": path, **kw}, git)


def _tail(raw: bytes) -> bytes:
    return raw[raw.index(b"\n---", 3) + 4 :]


def test_adopt_fills_provenance_and_is_valid(tmp_path):
    git = _repo(tmp_path)
    p = _put(tmp_path, HAND)
    before = p.read_bytes()
    r = _run(git, write=True)
    assert r["changed"] and r["applied"] and not r["dry_run"]
    assert r["unfilled_required"] == []
    out = p.read_text(encoding="utf-8")
    assert "plan_id: \"pln-engine-op-gaps-" in out
    assert "branch: \"work/test\"" in out
    assert "deliverable_id: \"dlv-" in out and "initiative: null" in out
    assert "kind: x" in out
    assert _tail(p.read_bytes()) == _tail(before)
    assert r["diff"].startswith("--- a/") and "+plan_id" in r["diff"]
    assert r["command"] == adopt_command(REL)


def test_rerun_is_noop(tmp_path):
    git = _repo(tmp_path)
    p = _put(tmp_path, HAND)
    _run(git, write=True)
    after = p.read_bytes()
    mtime = p.stat().st_mtime_ns
    r = _run(git, write=True)
    assert r["changed"] is False and r["applied"] is False and r["diff"] == "" and r["changes"] == []
    assert p.read_bytes() == after and p.stat().st_mtime_ns == mtime


def test_absent_status_is_reported_not_derived(tmp_path):
    git = _repo(tmp_path)
    _put(tmp_path, "---\ntitle: T\ncreated: 2026-10-01\nauthor: A\n---\nbody\n")
    r = _run(git, write=True)
    assert [u for u in r["unfilled_required"] if u.startswith("status")]


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_body_bytes_identical_lf_and_crlf(tmp_path, eol):
    git = _repo(tmp_path)
    text = HAND.replace("\n", eol)
    p = _put(tmp_path, text)
    before = p.read_bytes()
    _run(git, write=True)
    after = p.read_bytes()
    assert after != before and _tail(after) == _tail(before)
    assert (b"\r\n" in after) == (eol == "\r\n")
    assert after.count(b"\n") == after.count(b"\r\n") or eol == "\n"


def test_no_frontmatter_takes_title_from_h1(tmp_path):
    git = _repo(tmp_path)
    original = "# My Hand Plan\n\ntext\r\nmixed\n"
    p = _put(tmp_path, original)
    r = _run(git, write=True)
    out = p.read_bytes()
    assert out.startswith(b'---\ntitle: "My Hand Plan"\n')
    assert out.endswith(b"---\n" + original.encode())
    assert any(u.startswith("status") for u in r["unfilled_required"])


def test_unparseable_yaml_refused_and_file_unchanged(tmp_path):
    git = _repo(tmp_path)
    p = _put(tmp_path, "---\ntitle: [unclosed\nkey: : :\n---\nbody\n")
    before = p.read_bytes()
    with pytest.raises(ValueError, match="does not parse"):
        _run(git, write=True)
    assert p.read_bytes() == before


def test_unterminated_frontmatter_refused(tmp_path):
    git = _repo(tmp_path)
    _put(tmp_path, "---\ntitle: T\nbody never closes\n")
    with pytest.raises(ValueError, match="not terminated"):
        _run(git, write=True)


@pytest.mark.parametrize(
    "rel", ["docs/plans/2026-10-01-x.review.md", "docs/plans/sub/x.md", "state/handoffs/x.md", "README.md"]
)
def test_sidecar_and_non_plan_refused(tmp_path, rel):
    git = _repo(tmp_path)
    _put(tmp_path, HAND, rel)
    with pytest.raises(ValueError, match="unsupported type"):
        _run(git, rel, write=True)


def test_path_escape_refused(tmp_path):
    git = _repo(tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        _run(git, "../outside.md")


@pytest.mark.parametrize("bad", ["yes", 1, "true", None])
def test_non_bool_write_refused(tmp_path, bad):
    git = _repo(tmp_path)
    p = _put(tmp_path, HAND)
    before = p.read_bytes()
    with pytest.raises(ValueError, match="write must be a bool"):
        _run(git, write=bad)
    assert p.read_bytes() == before


def test_dry_run_leaves_bytes_and_mtime(tmp_path):
    git = _repo(tmp_path)
    p = _put(tmp_path, HAND)
    before, mtime = p.read_bytes(), p.stat().st_mtime_ns
    r = _run(git)
    assert r["dry_run"] and r["changed"] and not r["applied"] and r["diff"]
    assert p.read_bytes() == before and p.stat().st_mtime_ns == mtime


def test_present_values_never_overwritten(tmp_path):
    git = _repo(tmp_path)
    p = _put(tmp_path, HAND.replace("kind: x\n", 'kind: x\nplan_id: "pln-keep-000000"\nbranch: "mine"\n'))
    _run(git, write=True)
    out = p.read_text(encoding="utf-8")
    assert 'plan_id: "pln-keep-000000"' in out and 'branch: "mine"' in out


def test_deliverable_id_env_carried(tmp_path, monkeypatch):
    git = _repo(tmp_path)
    p = _put(tmp_path, HAND)
    monkeypatch.setenv("DELIVERABLE_ID", "dlv-parent-abcdef")
    _run(git, write=True)
    assert 'deliverable_id: "dlv-parent-abcdef"' in p.read_text(encoding="utf-8")


def test_zero_spawns_and_process_time_budget(tmp_path, monkeypatch):
    git = _repo(tmp_path)
    _put(tmp_path, HAND)
    mod._schemas()
    spawns: list = []
    real = subprocess.Popen.__init__

    def counting(self, *a, **k):
        spawns.append(a)
        return real(self, *a, **k)

    monkeypatch.setattr(subprocess.Popen, "__init__", counting)
    t0 = time.process_time()
    r = _run(git, write=True)
    elapsed = time.process_time() - t0
    assert r["changed"] and spawns == []
    assert elapsed < 0.5


def test_registered_with_common_dir_scope():
    from coordinator_core.op_scopes import _OP_KEY_SCOPE

    assert _OP_KEY_SCOPE["artifact.adopt"] == "common_dir"
