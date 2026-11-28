"""repo.induct doctrine step: create an absent CLAUDE.md and declare project_type in coordinator.local.md."""
from __future__ import annotations

import os
import re
from pathlib import Path

from coordinator_core._settings_home import claude_config_dir
from coordinator_core.ops import repo_setup
from coordinator_core.ops.render_template import render
from coordinator_core.ops.repo_induct._contract import StepRecord, make_step
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.ue_project_detection import LOCAL_MD_RELPATHS, declared_project

CLAUDE_MD_STEP = "claude_md"
LOCAL_MD_STEP = "coordinator_local_md"

_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def fill_doctrine(root: str | os.PathLike, repo_key: str, project_type: str) -> list[StepRecord]:
    """The [claude_md, coordinator_local_md] step records for `root`."""
    base = Path(root)
    if not base.is_dir():
        detail = f"root is not a directory: {base}"
        return [make_step(CLAUDE_MD_STEP, "failed", detail), make_step(LOCAL_MD_STEP, "failed", detail)]
    return [
        _fill_claude_md(base, repo_key, project_type),
        _fill_local_md(base, project_type),
    ]


def _fill_claude_md(base: Path, repo_key: str, project_type: str) -> StepRecord:
    target = base / "CLAUDE.md"
    if target.exists():
        return make_step(CLAUDE_MD_STEP, "skipped", "CLAUDE.md exists; left unchanged")
    rendered, err = _render_claude_md(base, repo_key, project_type)
    if rendered is None:
        return make_step(CLAUDE_MD_STEP, "failed", f"CLAUDE.md not rendered: {err}")
    try:
        with open(target, "xb") as fh:
            fh.write(rendered.encode("utf-8"))
    except FileExistsError:
        return make_step(CLAUDE_MD_STEP, "skipped", "CLAUDE.md exists; left unchanged")
    except OSError as exc:
        return make_step(CLAUDE_MD_STEP, "failed", f"CLAUDE.md not written: {exc}")
    declare_write(target)
    if not target.is_file():
        return make_step(CLAUDE_MD_STEP, "failed", "CLAUDE.md missing after write")
    return make_step(CLAUDE_MD_STEP, "done", "CLAUDE.md rendered from repo-setup CLAUDE.md.template", [str(target)])


def _render_claude_md(base: Path, repo_key: str, project_type: str) -> tuple[str | None, str]:
    """(rendered text, "") or (None, reason) through repo_setup's template resolvers and render()."""
    templates = repo_setup._templates_dir(repo_setup._plugin_root())
    if templates is None:
        return None, "repo-setup templates directory not resolvable (no plugin root)"
    block_tpl = templates / f"project-type-block.{project_type}.template"
    block = block_tpl.read_text(encoding="utf-8", errors="replace") if block_tpl.is_file() else ""
    extends = "Extends global `~/.claude/CLAUDE.md`." if (claude_config_dir() / "CLAUDE.md").is_file() else ""
    rendered, _rc, err = render(
        str(templates / "CLAUDE.md.template"),
        [
            ("PROJECT_NAME", repo_key.removeprefix("repos.") if repo_key else base.name),
            ("PROJECT_TYPE", project_type),
            ("SUBTYPES", ""),
            ("GLOBAL_EXTENDS_LINE", extends),
            ("PROJECT_TYPE_BLOCK", block),
        ],
    )
    return (None, err or "render failed") if err is not None or rendered is None else (rendered, "")


def _fill_local_md(base: Path, project_type: str) -> StepRecord:
    if not _TYPE_RE.match(project_type):
        return make_step(LOCAL_MD_STEP, "failed", f"project_type is not a plain token: {project_type!r}")

    declared = declared_project(base)
    if declared is not None and declared.project_type is not None:
        if declared.project_type == project_type:
            return make_step(LOCAL_MD_STEP, "skipped", f"{declared.source_file} already declares {project_type}")
        return make_step(
            LOCAL_MD_STEP,
            "failed",
            f"project_type_conflict: {declared.source_file} declares {declared.project_type!r}, "
            f"requested {project_type!r}; not overwritten",
        )

    target = declared.source_file if declared is not None else _first_existing(base)
    try:
        if target is None:
            target = base / "coordinator.local.md"
            _create(target, f"---\nproject_type: {project_type}\n---\n")
        else:
            _add_key(target, project_type)
    except OSError as exc:
        return make_step(LOCAL_MD_STEP, "failed", f"coordinator.local.md not written: {exc}")

    after = declared_project(base)
    if after is None or after.project_type != project_type:
        got = None if after is None else after.project_type
        return make_step(LOCAL_MD_STEP, "failed", f"declared_project reads {got!r} after write, expected {project_type!r}", [str(target)])
    return make_step(LOCAL_MD_STEP, "done", f"{target} declares project_type {project_type}", [str(target)])


def _first_existing(base: Path) -> Path | None:
    for rel in LOCAL_MD_RELPATHS:
        if (base / rel).is_file():
            return base / rel
    return None


def _create(target: Path, text: str) -> None:
    with open(target, "xb") as fh:
        fh.write(text.encode("utf-8"))


def _add_key(target: Path, project_type: str) -> None:
    raw = target.read_bytes()
    nl = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8", errors="surrogateescape")
    lines = text.splitlines(keepends=True)
    if lines and lines[0].rstrip("\r\n") == "---":
        lines.insert(1, f"project_type: {project_type}{nl}")
        out = "".join(lines)
    else:
        out = f"---{nl}project_type: {project_type}{nl}---{nl}{nl}{text}"
    tmp = target.with_name(target.name + ".induct.tmp")
    tmp.write_bytes(out.encode("utf-8", errors="surrogateescape"))
    os.replace(tmp, target)
