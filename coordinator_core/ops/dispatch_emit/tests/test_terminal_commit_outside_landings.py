"""Outside-landing detection, attribution and ``outside_rows`` stamping against real commits in a tmp_path git repo."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from coordinator_core.frontmatter.body_blocks import locate_fenced_block
from coordinator_core.ops.dispatch_emit import terminal_commit

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REL = "plan.md"
_ROWS = "".join(
    f"- id: {i}\n  title: t{i}\n  change_kind: code-edit\n  surface: {i.lower()}.py\n  disposition: open\n  deferred: false\n"
    for i in ("C1", "C2")
)
_PLAN = f"---\ntitle: p\n---\n\n## Tasks\n\n```yaml plan-tasks\n{_ROWS}```\n"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _commit(root: Path, files: dict, message: str) -> str:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8", newline="\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, {"seed": "0"}, "seed")
    _commit(tmp_path, {_REL: _PLAN}, "plan")
    return tmp_path


def _chunk(cid: str, *paths: str):
    return SimpleNamespace(id=cid, paths=list(paths))


def _stamp(repo: Path, terminal: str, landings: dict, monkeypatch) -> dict:
    monkeypatch.setattr(terminal_commit, "_changed_paths", lambda _root, paths: set(paths))
    terminal_commit._stamp_coded_commit(
        lambda p, r: {"committed": True, "sha": "f" * 40}, repo, repo, {}, terminal, None,
        outside_rows={_REL: landings},
    )
    return {r["id"]: r for r in yaml.safe_load(locate_fenced_block((repo / _REL).read_text(encoding="utf-8")).body)}


def test_landings_are_newest_first_and_stamped_with_every_sha(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    older = _commit(repo, {"c1.py": "1"}, "first")
    newer = _commit(repo, {"c1b.py": "1"}, "second")
    terminal = _commit(repo, {"unrelated": "2"}, "terminal")
    chunk = _chunk("C1", "c1.py", "c1b.py")
    landings = terminal_commit._outside_landings(repo, terminal, _REL, [chunk])
    assert landings == {"C1": [newer, older]}
    assert terminal_commit._outside_landing_attribution(repo, terminal, _REL, [chunk]) == {"C1": newer}
    rows = _stamp(repo, terminal, landings, monkeypatch)
    assert rows["C1"]["disposition"] == "coded"
    assert rows["C1"]["disposition_ref"] == newer
    assert rows["C1"]["disposition_detail"] == terminal_commit._outside_landing_detail([newer, older])
    assert rows["C2"]["disposition"] == "open"


