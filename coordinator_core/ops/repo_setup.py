"""
coordinator_core.ops.repo_setup -- the "repo.setup" op: onboard ONE git work tree and return a verdict.

Purpose: the deterministic half of DoE's repo-setup mechanics (detection, CLAUDE.md and
docs/README.md render, directories, .gitignore block, hooks, git config, substrate seeds, tripwire
copies) as one in-process op. The reply is the contract verdict ``{created, already_existed, open,
needs_attention}`` (``repo_setup_contract.VERDICT_SCHEMA``).

Params: ``root`` (required; absolute path of a git work tree), ``answers`` (optional; dict or JSON
string, validated by ``repo_setup_contract.validate_answers``).

Negative-spec:
  - Does NOT spawn claude or any LLM session; the op itself spawns nothing beyond what the reused
    single-repo helpers do.
  - Does NOT write outside ``root``; a root that resolves to the Claude home is refused.
  - Does NOT call ``ensure_hooks_fleet``: hooks are installed through
    ``ensure_prepare_commit_msg_hook(root=<target>)`` only.
  - Does NOT clobber: every file step is create-if-absent (the .gitignore step appends missing lines).
  - Does NOT ask questions: a judgement call left to the PM is an ``open`` item.
  - Slow or interactive steps (exec-summary generator, fleet memo registration, strategic seeds,
    widened spawn tripwire) are ``open`` items carrying the manual command, never run here.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import stat
import sys
from pathlib import Path
from typing import Callable, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.repo_setup_contract import Verdict, make_item, validate_answers
from coordinator_core.session.declared_writes import declare_write

GENERATES: list = []

_VALID_TYPES = ("game-dev", "web-dev", "data-science", "general")
_CONSUMER_DIRS = ("docs", "docs/plans", "state/handoffs", "scratch/subagent-sandbox")
_AUTHOR_DIRS = ("docs", "scratch/subagent-sandbox")
_ORIENTATION_CACHE = "## Active workstreams\n\n## Branch\n\n## Pinboard\n"
_TRIPWIRE_BASE = (
    "test_machine_local_state_tracked.py",
    "test_foreign_platform_paths.py",
    "test_every_test_tree_is_collected.py",
    "test_no_absolute_path_literals.py",
    "test_gitattributes_line_ending_coverage.py",
    "_tripwire_root.py",
)
_WEB_CONFIG_RE = re.compile(r"^(next|vite|webpack|svelte|astro|nuxt|remix|angular)\.config\.|^angular\.json$")
_HOOK_CREATED = frozenset({"installed-absent", "rewritten-stale", "appended", "refreshed-append-form"})
_HOOK_KEPT = frozenset({"already-current", "left-append-form", "left-legacy-append-form"})


class _Ctx:
    """One onboarding run: the target, the resolved answers, and the verdict being built."""

    def __init__(self, root: Path, answers: dict, verdict: Verdict):
        self.root = root
        self.answers = answers
        self.v = verdict
        self.project_type = "general"
        self.subtypes: list = []
        self.cross_platform = False
        self.rendered_claude_md = False

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def created(self, step: str, detail: str, path: Optional[Path] = None) -> None:
        self.v.created.append(make_item(step, detail, self.rel(path) if path else None))

    def existed(self, step: str, detail: str, path: Optional[Path] = None) -> None:
        self.v.already_existed.append(make_item(step, detail, self.rel(path) if path else None))

    def open(self, step: str, detail: str, path: Optional[Path] = None) -> None:
        self.v.open.append(make_item(step, detail, self.rel(path) if path else None))

    def attention(self, step: str, detail: str, path: Optional[Path] = None) -> None:
        self.v.needs_attention.append(make_item(step, detail, self.rel(path) if path else None))


# -- resolvers (module-level so tests substitute them) ---------------------------------------------


def _plugin_root() -> Optional[Path]:
    from coordinator_core.subagent_sandbox.plugin_root import resolve_plugin_root

    found = resolve_plugin_root()
    return Path(found) if found else None


def _machine_profile() -> str:
    from coordinator_core.machine_profile import machine_profile

    return machine_profile()


def _claude_home_env() -> dict:
    return dict(os.environ)


def _git_hook_install():
    from coordinator_core.ops import doctor

    return doctor._git_hook_install()


# -- small file helpers ----------------------------------------------------------------------------


def _write_new(path: Path, text: str, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    declare_write(path)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _frontmatter_value(text: str, key: str) -> Optional[str]:
    if not text.startswith("---"):
        return None
    for line in text.splitlines()[1:]:
        if line.strip() == "---":
            return None
        m = re.match(rf"^{re.escape(key)}:\s*(.*?)\s*$", line)
        if m:
            return m.group(1)
    return None


def _templates_dir(plugin: Optional[Path]) -> Optional[Path]:
    if plugin is None:
        return None
    cand = plugin / "skills" / "repo-setup" / "templates"
    return cand if cand.is_dir() else None


def _capture(fn: Callable, *args, **kwargs) -> tuple:
    """Run a CLI-shaped helper in-process; return ``(result, stdout, stderr)``.

    Trap: redirect_stdout/stderr are process-global, so concurrent ops in the warm engine can lose output.
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        result = fn(*args, **kwargs)
    return result, out.getvalue().strip(), err.getvalue().strip()


# -- steps -----------------------------------------------------------------------------------------


def _step_detect(ctx: _Ctx) -> None:
    local = ctx.root / "coordinator.local.md"
    if local.is_file():
        text = _read(local)
        ptype = _frontmatter_value(text, "project_type")
        if ptype:
            ctx.project_type = ptype.strip("'\"")
        sub = _frontmatter_value(text, "project_subtypes")
        if sub:
            ctx.subtypes = [s.strip().strip("'\"") for s in sub.strip("[]").split(",") if s.strip()]
        ctx.cross_platform = (_frontmatter_value(text, "cross_platform") or "").lower() == "true"
        ctx.existed("detect", f"coordinator.local.md names project_type {ctx.project_type}", local)
        if ctx.project_type not in _VALID_TYPES:
            ctx.open(
                "detect",
                f"legacy project_type {ctx.project_type!r}; migrate to one of {', '.join(_VALID_TYPES)}",
                local,
            )
        return

    names = {e.name for e in os.scandir(ctx.root)}
    if any(n.endswith((".uplugin", ".uproject")) for n in names):
        derived, subs = "game-dev", ["unreal"]
    elif "package.json" in names and any(_WEB_CONFIG_RE.match(n) for n in names):
        derived, subs = "web-dev", []
    elif "requirements.txt" in names or "pyproject.toml" in names:
        derived, subs = "data-science", []
    else:
        derived, subs = "general", []
    ctx.project_type = ctx.answers.get("project_type", derived)
    ctx.subtypes = ctx.answers.get("project_subtypes", subs)
    ctx.cross_platform = bool(ctx.answers.get("cross_platform", False))
    text = (
        "---\n"
        f"project_type: {ctx.project_type}\n"
        f"project_subtypes: [{', '.join(ctx.subtypes)}]\n"
        f"cross_platform: {'true' if ctx.cross_platform else 'false'}\n"
        "---\n"
    )
    _write_new(local, text)
    ctx.created("detect", f"coordinator.local.md written (project_type {ctx.project_type})", local)


def _step_render_docs(ctx: _Ctx) -> None:
    claude_md, readme = ctx.root / "CLAUDE.md", ctx.root / "docs" / "README.md"
    todo = [p for p in (claude_md, readme) if not p.exists()]
    for p in (claude_md, readme):
        if p.exists():
            ctx.existed("render", f"{ctx.rel(p)} present", p)
    if not todo:
        return
    templates = _templates_dir(_plugin_root())
    if templates is None:
        for p in todo:
            ctx.attention("render", "repo-setup templates directory not resolvable (no plugin root)", p)
        return
    name = ctx.answers.get("project_name") or ctx.root.name
    if claude_md in todo:
        from coordinator_core._settings_home import claude_config_dir
        from coordinator_core.ops.render_template import render

        block = ""
        block_tpl = templates / f"project-type-block.{ctx.project_type}.template"
        if block_tpl.is_file():
            block = _read(block_tpl)
        extends = (
            "Extends global `~/.claude/CLAUDE.md`."
            if (claude_config_dir() / "CLAUDE.md").is_file()
            else ""
        )
        rendered, rc, err = render(
            str(templates / "CLAUDE.md.template"),
            [
                ("PROJECT_NAME", name),
                ("PROJECT_TYPE", ctx.project_type),
                ("SUBTYPES", ", ".join(ctx.subtypes)),
                ("GLOBAL_EXTENDS_LINE", extends),
                ("PROJECT_TYPE_BLOCK", block),
            ],
        )
        if err is not None:
            ctx.attention("render", f"CLAUDE.md not rendered: {err}", claude_md)
        else:
            _write_new(claude_md, rendered)
            ctx.rendered_claude_md = True
            ctx.created("render", "CLAUDE.md rendered", claude_md)
            ctx.open("render", "fill in CLAUDE.md Project Overview, Build & Test and Runtime conventions", claude_md)
    if readme in todo:
        tpl = templates / "README.md.template"
        if not tpl.is_file():
            ctx.attention("render", "README.md.template missing", readme)
        else:
            from datetime import date

            text = _read(tpl).replace("[PROJECT_NAME]", name).replace("[DATE]", date.today().isoformat())
            _write_new(readme, text)
            ctx.created("render", "docs/README.md rendered", readme)


def _step_exec_summary(ctx: _Ctx) -> None:
    path = ctx.root / "docs" / "exec-summary.md"
    if path.exists():
        ctx.existed("exec-summary", "docs/exec-summary.md present", path)
    else:
        ctx.open(
            "exec-summary",
            "generate with: repo-setup-args-and-register resolve-exec-summary-generator --run",
            path,
        )


def _step_dirs(ctx: _Ctx) -> None:
    author = _machine_profile() == "author"
    for rel in _AUTHOR_DIRS if author else _CONSUMER_DIRS:
        d = ctx.root / rel
        if d.is_dir():
            ctx.existed("dirs", f"{rel}/ present", d)
            continue
        d.mkdir(parents=True, exist_ok=True)
        declare_write(d)
        ctx.created("dirs", f"{rel}/ created", d)
    if author:
        plugin = _plugin_root()
        if plugin is None:
            ctx.attention("dirs", "engine-plane manifest root not resolvable; canonical scaffold skipped")
            return
        from coordinator_core.install.scaffold_structure import scaffold_canonical_structure

        result = scaffold_canonical_structure(ctx.root, plugin)
        made = result.created_dirs + result.created_files + result.created_readmes + result.created_gitkeeps
        if made:
            ctx.created("dirs", f"canonical scaffold created {made} item(s)")
        else:
            ctx.existed("dirs", "canonical scaffold already present")
    cache = ctx.root / "state" / "orientation_cache.md"
    if cache.exists():
        ctx.existed("dirs", "state/orientation_cache.md present", cache)
    else:
        _write_new(cache, _ORIENTATION_CACHE)
        ctx.created("dirs", "state/orientation_cache.md seeded", cache)


def _step_gitignore(ctx: _Ctx) -> None:
    path = ctx.root / ".gitignore"
    templates = _templates_dir(_plugin_root())
    tpl = templates / "gitignore.project.template" if templates else None
    if tpl is None or not tpl.is_file():
        ctx.attention("gitignore", "gitignore.project.template not resolvable", path)
        return
    template_text = _read(tpl)
    wanted = [ln.strip() for ln in template_text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not path.exists():
        _write_new(path, template_text if template_text.endswith("\n") else template_text + "\n")
        ctx.created("gitignore", ".gitignore written from the canonical block", path)
        return
    existing_text = _read(path)
    have = {ln.strip() for ln in existing_text.splitlines()}
    missing = [ln for ln in wanted if ln not in have]
    if missing:
        sep = "" if existing_text.endswith("\n") or not existing_text else "\n"
        block = sep + "\n# coordinator -- canonical block (repo-setup)\n" + "\n".join(missing) + "\n"
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(block)
        declare_write(path)
        ctx.created("gitignore", f"appended {len(missing)} missing canonical line(s)", path)
    else:
        ctx.existed("gitignore", "canonical block present", path)
    if {".claude/", ".claude", "/.claude/", "/.claude"} & have:
        ctx.attention("gitignore", ".claude/ is blanket-ignored; settings and agents under it would be lost", path)


def _step_corpus_guard(ctx: _Ctx) -> None:
    hook = ctx.root / ".git" / "hooks" / "pre-commit"
    if not (ctx.root / ".git").is_dir():
        ctx.attention("pre-commit", ".git is not a directory (linked worktree); corpus guard not installed", hook)
        return
    from coordinator_core.git.corpus_guard_hook import STAMP, hook_body

    if not hook.exists():
        _write_new(hook, hook_body(), executable=True)
        ctx.created("pre-commit", "corpus-artifact guard hook installed", hook)
    elif STAMP in _read(hook):
        ctx.existed("pre-commit", "corpus-artifact guard hook present", hook)
    else:
        ctx.attention("pre-commit", "a different pre-commit hook exists; left untouched", hook)


def _step_hook(ctx: _Ctx) -> None:
    ghi = _git_hook_install()
    if ghi is None:
        ctx.attention("prepare-commit-msg", "git_hook_install not importable; hook not installed")
        return
    from coordinator_core.machine_resolver import merged_flat_registry

    bin_dir = str(Path(ghi.__file__).resolve().parent.parent)
    outcome: list = []
    lib_dir = str(Path(ghi.__file__).resolve().parent)
    sys.path.insert(0, lib_dir)
    try:
        _, _, err = _capture(
            ghi.ensure_prepare_commit_msg_hook,
            bin_dir,
            root=str(ctx.root),
            outcome=outcome,
            registry=merged_flat_registry(),
        )
    finally:
        sys.path.remove(lib_dir)
    state = outcome[-1] if outcome else "no-outcome"
    hook = ctx.root / ".git" / "hooks" / "prepare-commit-msg"
    if state in _HOOK_CREATED:
        declare_write(hook)
        ctx.created("prepare-commit-msg", f"Session-Id trailer hook {state}", hook)
    elif state in _HOOK_KEPT:
        ctx.existed("prepare-commit-msg", f"hook {state}", hook)
    else:
        ctx.attention("prepare-commit-msg", f"hook {state}" + (f": {err}" if err else ""), hook)


def _step_git_config(ctx: _Ctx) -> None:
    from coordinator_core.install import git_perf_config

    lines = git_perf_config._apply_maintenance_keys(ctx.root)
    failed = [ln for ln in lines if ln.startswith(("FAILED", "skip"))]
    changed = [ln for ln in lines if ln.startswith("set")]
    if failed:
        ctx.attention("git-config", "; ".join(failed))
    if changed:
        ctx.created("git-config", "; ".join(changed))
    elif not failed:
        ctx.existed("git-config", "git maintenance config already set")
    ctx.open(
        "git-config",
        "core.untrackedCache needs git's multi-second filesystem probe; run coordinator-configure-git from the repo",
    )


def _step_vscode(ctx: _Ctx) -> None:
    from coordinator_core.ops import ensure_vscode_readonly as mod

    settings = ctx.root / ".vscode" / "settings.json"
    made_dir = not settings.parent.exists()
    settings.parent.mkdir(parents=True, exist_ok=True)
    (rc, line), _, _ = _capture(mod._merge_settings, settings)
    if rc != 0:
        ctx.attention("vscode", "settings.json could not be merged", settings)
    elif line.strip():
        declare_write(settings)
        ctx.created("vscode", line.strip(), settings)
    else:
        ctx.existed("vscode", "read-only guard already satisfied", settings)
    if made_dir and not any(settings.parent.iterdir()):
        settings.parent.rmdir()


def _step_seeds(ctx: _Ctx) -> None:
    from coordinator_core.install import detect_test_cmd
    from coordinator_core.ops import setup_rag_decision, setup_seed_health_ledger

    ledger = ctx.root / "state" / "health-ledger.md"
    if ledger.is_file():
        ctx.existed("seed-health-ledger", "state/health-ledger.md present", ledger)
    else:
        rc, _, err = _capture(setup_seed_health_ledger.seed_health_ledger, str(ctx.root))
        if rc == 0:
            declare_write(ledger)
            ctx.created("seed-health-ledger", "state/health-ledger.md seeded", ledger)
        else:
            ctx.attention("seed-health-ledger", err or f"exit {rc}", ledger)

    local = ctx.root / "coordinator.local.md"
    if local.is_file() and detect_test_cmd.key_present(local, "fast_test_cmd"):
        ctx.existed("seed-test-cmd", "fast_test_cmd recorded", local)
    else:
        rc, out, err = _capture(detect_test_cmd.detect_and_write_test_cmds, str(ctx.root), non_interactive=True)
        if rc == 0:
            declare_write(local)
            ctx.created("seed-test-cmd", "fast_test_cmd/full_test_cmd detected", local)
        else:
            ctx.attention("seed-test-cmd", err or out or f"exit {rc}", local)

    if (ctx.root / ".node-version").is_file() or (ctx.root / ".nvmrc").is_file():
        ctx.open("seed-fnm-pin", f"install the pinned Node version with: setup-fnm-pin {ctx.root.as_posix()}")

    if _machine_profile() == "author":
        claude_md = ctx.root / "CLAUDE.md"
        before = _read(claude_md) if claude_md.is_file() else None
        rc, out, err = _capture(setup_rag_decision.setup_rag_decision, ["--root", str(ctx.root)])
        if rc != 0:
            ctx.attention("seed-rag-decision", err or out or f"exit {rc}")
        elif claude_md.is_file() and _read(claude_md) != before:
            declare_write(claude_md)
            ctx.created("seed-rag-decision", "un-indexed tripwire recorded in CLAUDE.md", claude_md)
        else:
            ctx.existed("seed-rag-decision", "RAG decision already recorded or index offered")


def _step_manifests(ctx: _Ctx) -> None:
    manifest = ctx.root / "docs" / "install" / "agent-install-manifest.json"
    if manifest.exists():
        ctx.existed("manifest", "agent-install-manifest.json present", manifest)
    else:
        ctx.open("manifest", "seed a schema-compliant agent-install-manifest.json (contract version 3)", manifest)
    skeleton = ctx.root / "state" / "strategic" / "self-description.yaml"
    if skeleton.exists():
        ctx.existed("self-description", "self-description.yaml present", skeleton)
    else:
        ctx.open("self-description", "author state/strategic/self-description.yaml (schema-required fields)", skeleton)


def _fast_test_path(ctx: _Ctx) -> Optional[Path]:
    local = ctx.root / "coordinator.local.md"
    if not local.is_file():
        return None
    cmd = _frontmatter_value(_read(local), "fast_test_cmd") or ""
    for tok in cmd.strip("'\"").split():
        if tok.startswith("-"):
            continue
        cand = ctx.root / tok.rstrip("/")
        if "/" in tok or cand.is_dir():
            return cand if cand.is_dir() else cand.parent
    return None


def _step_tripwires(ctx: _Ctx) -> None:
    plugin = _plugin_root()
    src = plugin / "tests" / "templates" if plugin else None
    if src is None or not src.is_dir():
        ctx.attention("tripwires", "tests/templates not resolvable under the plugin root")
        return
    dest_dir = _fast_test_path(ctx)
    if dest_dir is None:
        ctx.open("tripwires", "fast_test_cmd names no test path; copy guard-regression tripwires by hand")
        return
    names = list(_TRIPWIRE_BASE)
    if (ctx.root / "registry.toml").is_file():
        names.append("test_registry_toml_machine_paths.py")
    if (ctx.root / "hooks" / "hooks.json").is_file() or (ctx.root / "coordinator" / "hooks" / "hooks.json").is_file():
        names.append("test_guard_wiring_completeness.py")
    copied, kept = [], []
    for name in names:
        s, d = src / name, dest_dir / "guards" / name
        if not s.is_file():
            ctx.attention("tripwires", f"{name} missing from tests/templates", d)
        elif d.exists():
            kept.append(name)
        else:
            _write_new(d, _read(s))
            copied.append(name)
    if copied:
        ctx.created("tripwires", f"copied {len(copied)} guard-regression test(s)", dest_dir / "guards")
    if kept:
        ctx.existed("tripwires", f"{len(kept)} guard-regression test(s) present", dest_dir / "guards")
    ctx.open("tripwires", "widened spawn tripwire (test_no_bare_python_spawn.py, spawn_detect.py) needs per-repo SCOPE_SUBDIR edits")


def _register_command(root: Path) -> str:
    # Mirrors repo-setup-args-and-register.derive_repo_key; the script is not imported on the op path.
    key = re.sub(r"[^a-z0-9]+", "_", root.name.lower()).strip("_")
    return f'repo-setup-args-and-register register-repo --path "{root.as_posix()}" (registers repos.{key})'


def _step_register(ctx: _Ctx) -> None:
    if ctx.answers.get("register_repo") is True and _machine_profile() == "author":
        ctx.open("register-repo", f"run: {_register_command(ctx.root)}")


_STEPS = (
    _step_detect,
    _step_render_docs,
    _step_exec_summary,
    _step_dirs,
    _step_gitignore,
    _step_corpus_guard,
    _step_hook,
    _step_git_config,
    _step_vscode,
    _step_seeds,
    _step_manifests,
    _step_tripwires,
    _step_register,
)


def _refuse(step: str, detail: str, key: Optional[str] = None) -> dict:
    return Verdict(needs_attention=[make_item(step, detail, key=key)]).to_dict()


@register_op("repo.setup")
def handle(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "repo.setup" handler. See module docstring; returns the verdict dict."""
    raw = (params or {}).get("root")
    if not isinstance(raw, str) or not raw.strip():
        return _refuse("root", "root is required: an absolute path of a git work tree", key="root")
    if not os.path.isabs(raw):
        return _refuse("root", "root must be an absolute path", key="root")
    root = Path(raw)
    if not root.is_dir():
        return _refuse("root", f"root does not exist: {raw}", key="root")

    from coordinator_core.bash_guards.guard_repo_setup_claude_home_refusal import resolves_to_claude_home

    if resolves_to_claude_home(str(root), _claude_home_env()):
        return _refuse("root", "root resolves to the Claude home; repo-setup never targets it", key="root")
    if not (root / ".git").exists():
        return _refuse("root", "root is not the top of a git work tree (no .git entry)", key="root")

    answers, items = validate_answers((params or {}).get("answers"))
    if items:
        return Verdict(needs_attention=items).to_dict()

    ctx = _Ctx(root.resolve(), answers, Verdict())
    for step in _STEPS:
        try:
            step(ctx)
        except Exception as exc:  # one failed step never hides the others
            ctx.attention(step.__name__.removeprefix("_step_"), f"{type(exc).__name__}: {exc}")
    return ctx.v.to_dict()
