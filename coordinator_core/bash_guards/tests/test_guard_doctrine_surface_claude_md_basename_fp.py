"""A path merely ending in ``claude.md`` (``coordinator-content-repo.md``) is not the CLAUDE.md-class file."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as g

_CWD = "."


def _denied(cmd: str, cwd: str = _CWD) -> bool:
    return bool(g._class_write_denied(cmd, {"agent_id": "a"}, cwd))


@pytest.mark.parametrize(
    "cmd",
    [
        "git restore install/agy.md install/codex.md install/coordinator-content-repo.md",
        "git restore install/coordinator-content-repo.md",
        "echo x > install/coordinator-content-repo.md",
        "cp a.md docs/my_claude.md",
        "sed -i s/a/b/ install/COORDINATOR-CONTENT-REPO.md",
    ],
)
def test_suffix_named_file_is_not_class_file(cmd):
    assert g._class_write_denied(cmd, {"agent_id": "a"}, _CWD) is None


@pytest.mark.parametrize(
    "cmd",
    [
        "echo x > CLAUDE.md",
        "echo x > claude.md",
        "echo x > docs/sub/CLAUDE.md",
        "echo x >> /abs/path/Claude.md",
        "git restore install/agy.md CLAUDE.md",
        "git restore install/coordinator-content-repo.md CLAUDE.md",
        "sed -i s/a/b/ a/b/c/claude.md",
        "echo x | tee 'sub/CLAU''DE.md'",
    ],
)
def test_real_class_file_still_denied(cmd, tmp_path):
    for rel in ("CLAUDE.md", "claude.md", "docs/sub/CLAUDE.md", "a/b/c/claude.md", "sub/CLAUDE.md", "Claude.md"):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    cmd = cmd.replace("/abs/path/", f"{tmp_path.as_posix()}/")
    assert _denied(cmd, str(tmp_path))
