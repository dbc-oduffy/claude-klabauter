"""gem-inbox-apply against tmp_path fixture repos only; the real verbs never run."""
from __future__ import annotations

import json
import shlex
from pathlib import Path

from coordinator_core import gem_inbox_apply as gia
from coordinator_core.git.run import run_git

INBOX = "state/cross-repo/inbox"
ARCHIVE = "state/cross-repo/archive/2026-11"


def _git(repo: Path, *args: str) -> str:
    res = run_git(list(args), cwd=str(repo))
    assert res.returncode == 0, res.stderr
    return res.stdout


def _memo(repo: Path, name: str, status: str = "open") -> Path:
    p = repo / INBOX / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntitle: t\nstatus: {status}\n---\nbody\n", encoding="utf-8")
    return p


def _repo(tmp_path: Path, n: int = 2) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    for i in range(n):
        _memo(repo, f"m{i}.md")
    (repo / "peer.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


class FakeTool:
    """Stands in for the stamp/lint/send verbs: stamps edit the memo and commit it, as the CLI does."""

    def __init__(self, repo: Path, peer_after_first_stamp: bool = False):
        self.repo = repo
        self.calls: list[list[str]] = []
        self.ledger_seen: list[bool] = []
        self.peer = peer_after_first_stamp

    def __call__(self, argvs, cwd) -> list:
        return [self._one(a) for a in argvs]

    def _one(self, argv) -> int:
        self.calls.append(list(argv))
        self.ledger_seen.append(any((self.repo / gia.LEDGER_DIR).glob("*.json")))
        if argv[0] == "lint-frontmatter":
            return 1 if "BAD" in (self.repo / argv[2]).read_text(encoding="utf-8") else 0
        if argv[0] == "archive-stamp-cli":
            memo = Path(argv[2])
            memo.write_text(memo.read_text(encoding="utf-8").replace("status: open", "status: actioned"), encoding="utf-8")
            rel = memo.relative_to(self.repo).as_posix()
            _git(self.repo, "commit", "-q", "-m", f"memo.transition resolve: {rel}", "--", rel)
            if self.peer and len([c for c in self.calls if c[0] == "archive-stamp-cli"]) == 1:
                (self.repo / "peer.txt").write_text("v2\n", encoding="utf-8")
                _git(self.repo, "commit", "-q", "-m", "peer work", "--", "peer.txt")
        return 0


def _manifest(tmp_path: Path, repo: Path, n: int = 2, baton: str = "ok") -> Path:
    memos = [repo / INBOX / f"m{i}.md" for i in range(n)]
    baton_rel = "state/handoffs/2026-11/b.md"
    stamp = lambda m: f"archive-stamp-cli resolve-memo {m.as_posix()} --actioned-note \"x\""  # noqa: E731
    pathspec = [baton_rel]
    for m in memos:
        pathspec += [f"{INBOX}/{m.name}", f"{ARCHIVE}/{m.name}"]
    manifest = {
        "repo": str(repo),
        "closed": [m.as_posix() for m in memos],
        "left_open": [],
        "batons": [baton_rel],
        "moves": [{"from": m.as_posix(), "to": (repo / ARCHIVE / m.name).as_posix()} for m in memos],
        "stamps": [stamp(m) for m in memos],
        "writes": [{"path": baton_rel, "content": f"---\nx: {baton}\n---\n"}],
        "memos_to_send": [],
        "guard": [{"memo": m.as_posix(), "lines": [stamp(m)]} for m in memos],
        "apply_rule": "x",
        "commit": {"pathspec": pathspec, "subject": "inbox: G-EM blitz - test"},
    }
    mp = tmp_path / "shard1.json"
    mp.write_text(json.dumps(manifest), encoding="utf-8")
    return mp


def test_applies_in_order_squashes_and_commits_scoped(tmp_path):
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "peer.txt").write_text("dirty\n", encoding="utf-8")
    tool = FakeTool(repo)
    rc = gia.apply_manifest(str(_manifest(tmp_path, repo)), tool=tool, out=lambda *_: None)
    assert rc == 0
    assert _git(repo, "rev-parse", "HEAD~1").strip() == base
    assert _git(repo, "log", "-1", "--format=%s").strip() == "inbox: G-EM blitz - test"
    for i in range(2):
        assert not (repo / INBOX / f"m{i}.md").exists()
        assert "status: actioned" in (repo / ARCHIVE / f"m{i}.md").read_text(encoding="utf-8")
    assert (repo / "state/handoffs/2026-11/b.md").is_file()
    assert [c[0] for c in tool.calls] == ["lint-frontmatter", "archive-stamp-cli", "archive-stamp-cli"]
    assert all(tool.ledger_seen)
    assert not list((repo / gia.LEDGER_DIR).glob("*.json"))
    assert (repo / "peer.txt").read_text(encoding="utf-8") == "dirty\n"
    assert " M peer.txt" in _git(repo, "status", "--porcelain")
    changed = set(_git(repo, "show", "--no-renames", "--name-only", "--format=", "HEAD").split())
    assert "peer.txt" not in changed and f"{INBOX}/m0.md" in changed and f"{ARCHIVE}/m0.md" in changed


def test_ledger_lists_the_manifest_memos(tmp_path):
    repo = _repo(tmp_path)
    seen = {}

    def tool(argvs, cwd):
        for f in (repo / gia.LEDGER_DIR).glob("*.json"):
            seen[f.name] = json.loads(f.read_text(encoding="utf-8"))
        return FakeTool(repo)(argvs, cwd)

    gia.apply_manifest(str(_manifest(tmp_path, repo)), tool=tool, out=lambda *_: None)
    assert seen["shard1.json.json"]["memos"] == [(repo / INBOX / f"m{i}.md").as_posix() for i in range(2)]


def test_guard_drops_lines_for_a_memo_no_longer_open(tmp_path):
    repo = _repo(tmp_path)
    _memo(repo, "m1.md", status="actioned")
    _git(repo, "commit", "-q", "-am", "peer stamped m1")
    tool = FakeTool(repo)
    assert gia.apply_manifest(str(_manifest(tmp_path, repo)), tool=tool, out=lambda *_: None) == 0
    stamped = [shlex.split(" ".join(c))[2] for c in tool.calls if c[0] == "archive-stamp-cli"]
    assert stamped == [(repo / INBOX / "m0.md").as_posix()]


def test_baton_lint_failure_refuses_before_any_stamp(tmp_path):
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    tool = FakeTool(repo)
    rc = gia.apply_manifest(str(_manifest(tmp_path, repo, baton="BAD")), tool=tool, out=lambda *_: None)
    assert rc == 1
    assert [c[0] for c in tool.calls] == ["lint-frontmatter"]
    assert not (repo / "state/handoffs/2026-11/b.md").exists()
    assert not list((repo / gia.LEDGER_DIR).glob("*.json"))
    assert (repo / INBOX / "m0.md").exists() and _git(repo, "rev-parse", "HEAD") == head


def test_peer_commit_in_range_leaves_stamp_commits_unsquashed(tmp_path):
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD").strip()
    tool = FakeTool(repo, peer_after_first_stamp=True)
    assert gia.apply_manifest(str(_manifest(tmp_path, repo)), tool=tool, out=lambda *_: None) == 0
    subjects = _git(repo, "log", "--format=%s", f"{base}..HEAD").split("\n")
    assert sum(s.startswith("memo.transition") for s in subjects) == 2
    assert "peer work" in subjects and "inbox: G-EM blitz - test" in subjects[0]


def test_git_spawn_count_is_constant_in_memo_count(tmp_path):
    counts = []
    for n, sub in ((1, "a"), (4, "b")):
        root = tmp_path / sub
        root.mkdir()
        repo = _repo(root, n=n)
        calls = []

        def counted(args, **kw):
            calls.append(list(args))
            return run_git(args, **kw)

        mp = _manifest(root, repo, n=n)
        tool = FakeTool(repo)
        assert gia.apply_manifest(str(mp), git=counted, tool=tool, out=lambda *_: None) == 0
        counts.append(len(calls))
    assert counts[0] == counts[1] <= 6


def test_a_line_with_a_backslash_is_refused_naming_the_line(tmp_path):
    repo = _repo(tmp_path)
    mp = _manifest(tmp_path, repo)
    manifest = json.loads(mp.read_text(encoding="utf-8"))
    bad = 'archive-stamp-cli resolve-memo state\\cross-repo\\inbox\\m0.md --actioned-note "x"'
    manifest["stamps"][0] = bad
    manifest["guard"][0]["lines"] = [bad]
    mp.write_text(json.dumps(manifest), encoding="utf-8")
    tool = FakeTool(repo)
    messages: list[str] = []
    gia.apply_manifest(str(mp), tool=tool, out=messages.append)
    stamped = [c[2] for c in tool.calls if c[0] == "archive-stamp-cli"]
    assert stamped == [(repo / INBOX / "m1.md").as_posix()]
    assert any(bad in m and "backslash" in m for m in messages)


def test_a_forward_slash_line_is_not_refused(tmp_path):
    assert gia._split_line('archive-stamp-cli resolve-memo state/a/b.md --actioned-note "x y"') == [
        "archive-stamp-cli", "resolve-memo", "state/a/b.md", "--actioned-note", "x y"]


def test_a_non_verb_line_is_refused_by_the_default_runner(tmp_path):
    assert gia.run_tools([["rm", "-rf", "x"]], str(tmp_path)) == [127]
